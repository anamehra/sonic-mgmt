"""
2x2 VXLAN L2VNI Queue Occupancy / Watermark Measurement Test

Measures queue occupancy / watermarks at every hop on the VXLAN data
path while a single below-line-rate IPv6 stream traverses the fabric
in an L2VNI (bridged) deployment. Counterpart to
scheduler/test_v6_vxlan_queue_occ_wm_l3_2x2.py.

Topology (same as test_v6_ecn_vxlan_l2vni_2x2.py):
    SD1 -- Spine0   - D1
    SD2 -- Spine1   - D2
    SD3 -- Leaf0    - D3 (ingress leaf)
    SD4 -- Leaf1    - D4 (egress leaf)

Data path (single L2 bridged flow over EVPN Type-2 / L2VNI):
    T1D3P1 --> Leaf0 (Vlan100) --VXLAN--> Spine --> Leaf1 (Vlan100) --> T1D4P1
    Both TGEN endpoints share subnet 2001::/64.

For every (frame_size, rate) combination in PACKET_SIZES x TRAFFIC_RATES
(rates = [90, 95]), traffic is started and NUM_READS independent
measurement windows are sampled. Each window:
    * clear interface/queue/watermark/buffer-pool counters on every node
    * (laguna/carib) clear NPU voq queue counters on the egress port
    * wait READ_WAIT_SECS for new watermarks to accumulate
    * read buffer-pool watermark, queue watermark, NPU voq counters
      on the egress port of leaf0 / spine0 / leaf1

Watermark egress ports (each node's downstream-facing port):
    leaf0   : D3D1P1 -- to spine
    spine0  : D1D4P1 -- to leaf1
    leaf1   : D4T1P1 -- to TGEN

Pass criteria (soft):
    For every read with frame_size >= PKTS_IN_QUEUE_CHECK_MIN_FRAME_SIZE,
    the queue on every monitored node is expected to hold no more than
    MAX_PKTS_IN_QUEUE packets.
        - laguna/carib : SAI_QUEUE_STAT_CURR_OCCUPANCY_BYTES / frame_size
        - other        : buffer-pool egr_lossless watermark / frame_size
"""

import yaml
import pytest

from spytest import st, SpyTestDict
import tests.cisco.tortuga.vxlan.vxlan_utils as vxlan_obj
import qos_test_utils as qos_utils
import qos_debug_log_utils as qos_debug
import traffic_stream_ixia_api as stream_api
from vxlan_ecn_base import get_nodes, config_static


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ECN_ECT_0 = 0b00
PACKET_SIZES = [1024, 2048, 4096]
TRAFFIC_RATES = [90, 94]

NUM_READS = 5
READ_WAIT_SECS = 3
WATERMARK_POLL_MS = 2000
QUEUE_COUNTER_POLL_MS = 2000

MAX_PKTS_IN_QUEUE = 15
PKTS_IN_QUEUE_CHECK_MIN_FRAME_SIZE = 720

# VXLAN encapsulation overhead over IPv6 underlay:
# outer Ethernet (14) + outer IPv6 (40) + UDP (8) + VXLAN (8) = 70 bytes.
VXLAN_IPV6_OVERHEAD = 70

CONFIGS_FILE = '../../qos/ecn/vxlan_ecn_l2vni_2x1.yaml'

# ---------------------------------------------------------------------------
# TGEN endpoint addressing -- L2VNI uses the same subnet on both leaves
# (bridged), matching test_v6_ecn_vxlan_l2vni_2x2.py.
# ---------------------------------------------------------------------------
T1D3P1_IP6 = "2001::1"
T1D3P1_MAC = "00:0a:01:00:11:01"
T1D4P1_IP6 = "2001::2"
T1D4P1_MAC = "00:0a:01:00:12:01"
GW         = "2001::254"  # shared gateway (not really used for L2 bridged)

# VTEP IPs (only used for log; we don't rely on them for verification here)
LEAF0_VTEP_IP = 'fd27::280:10f1:25f'
LEAF1_VTEP_IP = 'fd27::22d:b87f:214b'

data = SpyTestDict()


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------

def _wait_for_vxlan_tunnel(nodes):
    for node_name in ('leaf0', 'leaf1'):
        dut = nodes[node_name]
        for attempt in range(12):
            output = st.config(dut, "show vxlan remotevtep")
            if 'oper_up' in output or 'fd27::' in output:
                st.log(f"{node_name}: VXLAN tunnel UP")
                break
            st.log(f"{node_name}: Waiting for VXLAN tunnel... attempt {attempt + 1}/12")
            st.wait(10)


# ---------------------------------------------------------------------------
# Module-level fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def vxlan_queue_occ_l2vni_module_setup():
    global updated_config_file

    vars = st.get_testbed_vars()
    nodes = get_nodes()

    # Step 1: Init QoS on all DUTs (does config reload on Gamut)
    st.banner("STEP 1: Initializing QoS on all DUTs")
    for dut in st.get_dut_names():
        stream_api.init_qos_on_dut(dut)
        qos_utils.load_config_db(dut)

    # Step 2: Cleanup any leftover IP/VRF/BGP config on leaves
    st.banner("STEP 2: Cleaning up existing leaf config")
    for node_name in ('leaf0', 'leaf1'):
        qos_utils.cleanup_config(nodes[node_name])
    qos_utils.cleanup_leftover_vrf_bgp(nodes)

    # Step 3: Build per-node topology + speeds; detect platform per node
    st.banner("STEP 3: Building per-node topology and detecting platforms")
    data.topology = qos_utils.build_node_topology(vars)
    qos_utils.populate_topology_speeds(data.topology, nodes)
    data.node_meta = {}
    for node_name, dut in nodes.items():
        plat = qos_utils.detect_platform(dut)
        data.node_meta[node_name] = {'platform_type': plat}
        st.log(f"{node_name}: platform_type={plat}")
        if plat == 'n9164e':
            st.config(dut, "sudo counterpoll wredqueue enable",
                      skip_tmpl=True, trace_log=1)

    # Step 4: Generate and apply L2VNI VXLAN/BGP config
    st.banner("STEP 4: Applying L2VNI VXLAN/BGP configuration")
    updated_config_file = vxlan_obj.modify_config_file(CONFIGS_FILE, vars)
    with open(updated_config_file) as fh:
        config_list = yaml.load(fh, Loader=yaml.FullLoader)
    for node in config_list.keys():
        config_static(node, 'sonic', config_list)
        st.wait(5)
        config_static(node, 'bgp', config_list)
    data.config_list = config_list

    # Step 5: Wait for BGP/EVPN/VXLAN convergence
    st.banner("STEP 5: Waiting for BGP convergence + VXLAN tunnels")
    st.wait(60)
    _wait_for_vxlan_tunnel(nodes)
    qos_utils.dump_vxlan_debug_info(nodes, "post-config")

    # Step 6: Discover lossless queue config on each monitored egress port
    st.banner("STEP 6: Discovering egress queue config from CONFIG_DB per node")
    monitored_intfs = {
        'leaf0':  vars.D3D1P1,
        'spine0': vars.D1D4P1 if hasattr(vars, 'D1D4P1') else None,
        'leaf1':  vars.D4T1P1,
    }
    data.monitored_intfs = monitored_intfs
    data.ecn_cfg_per_node = {}
    for node_name, intf in monitored_intfs.items():
        if not intf:
            st.log(f"{node_name}: no monitored egress port available; skipping discovery")
            continue
        cfg = qos_utils.discover_ecn_queue_config(nodes[node_name], intf)
        data.ecn_cfg_per_node[node_name] = cfg
        st.log(f"{node_name} {intf}: tc={cfg['tc']} dscp={cfg['dscp']} "
               f"queue={cfg['queue']} wred={cfg.get('wred_profile')}")

    leaf0_cfg = data.ecn_cfg_per_node.get('leaf0')
    if not leaf0_cfg:
        st.report_fail('msg', "leaf0 lossless queue config discovery failed")
    data.tc = int(leaf0_cfg['tc'])
    data.dscp = int(leaf0_cfg['dscp'])
    st.log(f"Stream tagging: TC={data.tc} DSCP={data.dscp}")

    # Step 7: Reduce poll intervals on monitored DUTs
    st.banner("STEP 7: Reducing watermark/queue counter poll intervals")
    for node_name in ('leaf0', 'spine0', 'leaf1'):
        if node_name not in nodes:
            continue
        dut = nodes[node_name]
        try:
            qos_utils.set_queue_watermark_poll_interval(dut, WATERMARK_POLL_MS)
            qos_debug.set_queue_counter_poll_interval(dut, QUEUE_COUNTER_POLL_MS)
        except Exception as e:
            st.log(f"{node_name}: poll interval set failed (non-fatal): {e}")

    # Step 8: TGEN NGPF setup (L2 bridged, both endpoints in same subnet)
    st.banner("STEP 8: Configuring TGEN NGPF interfaces (L2VNI bridged)")
    int_dict = {
        'T1D3P1': {'host_ip': T1D3P1_IP6, 'gateway': GW, 'mac': T1D3P1_MAC},
        'T1D4P1': {'host_ip': T1D4P1_IP6, 'gateway': GW, 'mac': T1D4P1_MAC},
    }
    handles = vxlan_obj.config_tgen_interface(int_dict, 'ipv6')
    data.tgen_handles = handles
    data.int_dict = int_dict

    tg = handles['T1D3P1']['tg_handle']
    data.tg = tg

    tg.tg_topology_test_control(action='start_all_protocols')
    st.wait(10)
    # Validate the L2VNI data path before declaring setup complete. A failure
    # here means traffic-driven readings would all be zero and the test would
    # falsely pass; one retry covers ND/MAC-learn races right after start.
    ping_kwargs = dict(
        src_obj=tg,
        dev_handle=handles['T1D3P1']['int_handle'],
        dst_ip=T1D4P1_IP6,
    )
    if not stream_api.verify_interface_ping(**ping_kwargs):
        st.wait(10)
        if not stream_api.verify_interface_ping(**ping_kwargs):
            st.report_fail('msg',
                "L2VNI E2E ping T1D3P1 -> T1D4P1 failed; data path is broken")

    # TGEN-facing ports -- used per-iteration to confirm traffic actually
    # ingresses leaf0 and egresses leaf1 (otherwise occupancy readings are
    # not trustworthy).
    data.tgen_ingress_port = {'node': 'leaf0', 'intf': vars.D3T1P1}
    data.tgen_egress_port = {'node': 'leaf1', 'intf': vars.D4T1P1}

    yield

    # ---- Module teardown ----
    st.banner("MODULE TEARDOWN: Stopping traffic and removing config")
    try:
        tg.tg_traffic_control(action='stop')
    except Exception as e:
        st.log(f"tg stop error: {e}")
    st.wait(2)

    for node_name in ('leaf0', 'spine0', 'leaf1'):
        if node_name not in nodes:
            continue
        dut = nodes[node_name]
        try:
            qos_utils.restore_queue_watermark_poll_interval(dut)
            qos_debug.restore_queue_counter_poll_interval(dut)
        except Exception as e:
            st.log(f"{node_name}: poll restore failed (non-fatal): {e}")

    for port_key, h in handles.items():
        try:
            tg.tg_interface_config(port_handle=h['port_handle'],
                                   handle=h['int_handle'], mode='destroy')
        except Exception as e:
            st.log(f"TGEN cleanup {port_key}: {e}")

    try:
        for node in reversed(list(data.config_list.keys())):
            config_static(node, 'bgp', data.config_list, add=False)
            st.wait(3)
            config_static(node, 'sonic', data.config_list, add=False)
        vxlan_obj.remove_temp_config(updated_config_file)
    except Exception as e:
        st.log(f"VXLAN/BGP teardown error (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Per-node measurement helper
# ---------------------------------------------------------------------------

def _effective_frame_size(node_name, plat, frame_size):
    """Frame size to use for byte->packet conversion at this hop.

    Whether the monitored queue holds encapsulated or decapsulated frames
    depends on (node role, ASIC queueing model):
      leaf0 (ingress):
        carib (Q200, VOQ-based) : VOQ enqueues pre-encap frames -> frame_size
        laguna / n9164e (OQ)    : OQ holds post-encap frames    -> + overhead
      spine0 (transit) : packets always encap on egress         -> + overhead
      leaf1 (egress)   : decap on egress to TGEN                -> frame_size
    """
    if node_name == 'leaf1':
        return frame_size
    if node_name.startswith('spine'):
        return frame_size + VXLAN_IPV6_OVERHEAD
    if plat == 'carib':
        return frame_size
    return frame_size + VXLAN_IPV6_OVERHEAD


def _take_one_reading_per_node(nodes, frame_size, rate, read_idx):
    """Clear, wait, then snapshot watermarks on every monitored node.

    Returns dict: {node_name: reading_dict}.
    """
    label = f"frame={frame_size}B rate={rate}%% read#{read_idx}"
    st.banner(f"--- {label} ---")

    # 1) Clear counters on every monitored node
    for node_name, intf in data.monitored_intfs.items():
        if not intf or node_name not in nodes:
            continue
        dut = nodes[node_name]
        qos_utils.clear_all_counters(dut, wait_time=1)
        plat = data.node_meta[node_name]['platform_type']
        if plat in ('laguna', 'carib'):
            cfg = data.ecn_cfg_per_node.get(node_name)
            if cfg:
                qos_utils.clear_ecn_counters_on_port(dut, intf, int(cfg['tc']))

    st.wait(READ_WAIT_SECS, "Accumulating watermarks while traffic flows")

    # 2) Read watermarks per node
    out = {}
    for node_name, intf in data.monitored_intfs.items():
        if not intf or node_name not in nodes:
            continue
        dut = nodes[node_name]
        plat = data.node_meta[node_name]['platform_type']
        cfg = data.ecn_cfg_per_node.get(node_name) or {}
        tc = int(cfg.get('tc', data.tc))

        # Buffer-pool watermark (egress + ingress lossless)
        bp_raw = qos_utils.get_buffer_pool_watermark(dut)
        bp_parsed = qos_utils.parse_buffer_pool_watermark(bp_raw) if bp_raw else {}
        try:
            bp_egr_lossless = int(str(bp_parsed.get('egr_lossless', 0)).replace(',', ''))
        except (TypeError, ValueError):
            bp_egr_lossless = 0
        try:
            bp_ing_lossless = int(str(bp_parsed.get('ing_lossless', 0)).replace(',', ''))
        except (TypeError, ValueError):
            bp_ing_lossless = 0

        # Queue watermark on the monitored egress port
        q_wm_map = qos_utils.capture_queue_watermark_values(
            {node_name: dut}, {node_name: [intf]}, tc)
        q_wm_raw = q_wm_map.get(node_name, {}).get(intf, 0)
        try:
            q_wm = int(q_wm_raw)
        except (TypeError, ValueError):
            q_wm = 0

        # NPU VOQ counters (laguna/carib)
        voq = {}
        if plat in ('laguna', 'carib'):
            voq = qos_utils.get_ecn_counters_on_port(dut, intf, tc, clear=False)

        curr_occ_bytes = int(voq.get('curr_occupancy_bytes', 0)) if voq else 0
        voq_wm_bytes = int(voq.get('watermark_bytes', 0)) if voq else 0
        delay_wm = int(voq.get('delay_watermark', 0)) if voq else 0

        if plat in ('laguna', 'carib'):
            pkts_src = 'voq.curr_occupancy_bytes'
            occ_bytes = curr_occ_bytes
        else:
            pkts_src = 'buffer_pool.egr_lossless'
            occ_bytes = bp_egr_lossless
        eff_fs = _effective_frame_size(node_name, plat, frame_size)
        pkts_in_queue = (occ_bytes + eff_fs - 1) // eff_fs if eff_fs > 0 else 0

        out[node_name] = {
            'label': label,
            'intf': intf,
            'platform': plat,
            'tc': tc,
            'frame_size': frame_size,
            'rate': rate,
            'bp_egr_lossless': bp_egr_lossless,
            'bp_ing_lossless': bp_ing_lossless,
            'q_wm_bytes': q_wm,
            'voq_curr_occ_bytes': curr_occ_bytes,
            'voq_wm_bytes': voq_wm_bytes,
            'voq_delay_wm': delay_wm,
            'pkts_in_queue': pkts_in_queue,
            'pkts_in_queue_src': pkts_src,
            'effective_frame_size': eff_fs,
        }

        st.log(f"  >> {node_name} {intf} (plat={plat} tc={tc}): "
               f"bp_egr_lossless={bp_egr_lossless}B "
               f"bp_ing_lossless={bp_ing_lossless}B q_wm={q_wm}B "
               f"voq_curr_occ={curr_occ_bytes}B voq_wm={voq_wm_bytes}B "
               f"delay_wm={delay_wm} -> pkts_in_queue~{pkts_in_queue} "
               f"(src={pkts_src}, eff_fs={eff_fs}B)")

    # Capture TGEN-facing port counters to prove the stream actually flows.
    # leaf0 ingress rx_ok > 0  and  leaf1 egress tx_ok > 0 are required
    # before the occupancy/watermark readings can be trusted.
    ing = data.tgen_ingress_port
    egr = data.tgen_egress_port
    if ing and ing['node'] in nodes:
        pc = qos_utils._get_port_counters_simple(nodes[ing['node']], ing['intf'])
        out.setdefault('_traffic', {})['ing'] = {
            'node': ing['node'], 'intf': ing['intf'],
            'rx_ok': pc.get('rx_ok', 0), 'rx_drp': pc.get('rx_drp', 0),
        }
    if egr and egr['node'] in nodes:
        pc = qos_utils._get_port_counters_simple(nodes[egr['node']], egr['intf'])
        out.setdefault('_traffic', {})['egr'] = {
            'node': egr['node'], 'intf': egr['intf'],
            'tx_ok': pc.get('tx_ok', 0), 'tx_drp': pc.get('tx_drp', 0),
        }
    if '_traffic' in out:
        t = out['_traffic']
        st.log(f"  >> traffic: ing={t.get('ing', {})} egr={t.get('egr', {})}")

    return out


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def test_l2vni_vxlan_queue_occupancy_watermark():
    nodes = get_nodes()
    handles = data.tgen_handles
    tg = data.tg

    # IPv6 traffic class byte = (DSCP << 2) | ECT
    ip_tos = qos_utils.compute_ip_tos(data.dscp, ECN_ECT_0)

    st.log(f"Test config: TC={data.tc} DSCP={data.dscp} "
           f"frame_sizes={PACKET_SIZES} rates={TRAFFIC_RATES}%% "
           f"reads/iter={NUM_READS} read_wait={READ_WAIT_SECS}s")
    st.log(f"Monitored ports: {data.monitored_intfs}")

    # results[(frame_size, rate)] = {node_name: [reading, ...]}
    results = {}

    try:
        for frame_size in PACKET_SIZES:
            for rate in TRAFFIC_RATES:
                st.banner(f"=== Packet size {frame_size}B @ {rate}%% ===")

                tg_kwargs = dict(
                    port_handle=handles['T1D3P1']['port_handle'],
                    port_handle2=handles['T1D4P1']['port_handle'],
                    mode='create',
                    transmit_mode='continuous',
                    rate_percent=rate,
                    frame_size=frame_size,
                    circuit_endpoint_type='ipv6',
                    ipv6_traffic_class=ip_tos,
                    emulation_src_handle=handles['T1D3P1']['int_handle'],
                    emulation_dst_handle=handles['T1D4P1']['int_handle'],
                )
                stream_result = tg.tg_traffic_config(**tg_kwargs)
                if stream_result.get('status') != '1':
                    st.report_fail('msg',
                        f"Failed to create stream at frame_size={frame_size} "
                        f"rate={rate}%%: {stream_result}")
                stream_id = stream_result['stream_id']
                stream_api.set_pfc_priority_group(tg, stream_result, data.tc)

                key = (frame_size, rate)
                results[key] = {n: [] for n in data.monitored_intfs
                                if data.monitored_intfs[n]}
                traffic_per_iter = []
                try:
                    tg.tg_traffic_control(action='stop')
                    st.wait(2)
                    tg.tg_traffic_control(action='run')

                    for k in range(NUM_READS):
                        per_node = _take_one_reading_per_node(
                            nodes, frame_size, rate, k + 1)
                        traf = per_node.pop('_traffic', None)
                        if traf:
                            traffic_per_iter.append(traf)
                        for n, r in per_node.items():
                            results[key].setdefault(n, []).append(r)

                    tg.tg_traffic_control(action='stop')
                    st.wait(2)
                finally:
                    try:
                        tg.tg_traffic_config(mode='remove', stream_id=stream_id)
                    except Exception as e:
                        st.log(f"stream remove error (frame={frame_size} "
                               f"rate={rate}): {e}")
                results.setdefault('_traffic_stats', {})[key] = traffic_per_iter
    finally:
        try:
            tg.tg_traffic_control(action='stop')
        except Exception as e:
            st.log(f"final tg stop error: {e}")

    if not results:
        st.report_fail('msg', "No iterations completed")

    traffic_stats = results.pop('_traffic_stats', {})

    # ---- Per-read summary table ----
    st.banner("SUMMARY: VXLAN L2VNI queue occ/watermark (per read)")
    hdr = (f"{'Node':>7} {'Intf':>14} {'Plat':>8} "
           f"{'Frame':>6} {'Rate':>5} {'Read':>5} "
           f"{'BP_egr_lossless':>17} {'BP_ing_lossless':>17} {'Q_wm_B':>10} "
           f"{'VOQ_curr_B':>12} {'VOQ_wm_B':>10} "
           f"{'Delay_wm':>10} {'Pkts_q':>7} {'Src':>22}")
    st.log(hdr)
    st.log("-" * len(hdr))
    for (frame_size, rate), per_node in sorted(results.items()):
        for node_name, readings in per_node.items():
            for k, r in enumerate(readings, start=1):
                st.log(f"{node_name:>7} {r['intf']:>14} {r['platform']:>8} "
                       f"{frame_size:>6} {rate:>5} {k:>5} "
                       f"{r['bp_egr_lossless']:>17} "
                       f"{r['bp_ing_lossless']:>17} {r['q_wm_bytes']:>10} "
                       f"{r['voq_curr_occ_bytes']:>12} {r['voq_wm_bytes']:>10} "
                       f"{r['voq_delay_wm']:>10} {r['pkts_in_queue']:>7} "
                       f"{r['pkts_in_queue_src']:>22}")

    # ---- Aggregate (max across reads) summary ----
    st.banner(f"AGGREGATE SUMMARY (max over {NUM_READS} reads, per node)")
    agg_hdr = (f"{'Node':>7} {'Intf':>14} {'Frame':>6} {'Rate':>5} "
               f"{'MaxQOcc_B':>12} {'MaxQ_wm_B':>10} "
               f"{'MaxBPegr_B':>12} {'MaxBPing_B':>12} {'MaxQ_lat':>10}")
    st.log(agg_hdr)
    st.log("-" * len(agg_hdr))
    for (frame_size, rate), per_node in sorted(results.items()):
        for node_name, readings in per_node.items():
            if not readings:
                continue
            intf = readings[0]['intf']
            max_q_occ = max(r['voq_curr_occ_bytes'] for r in readings)
            max_q_wm = max(r['voq_wm_bytes'] for r in readings)
            max_bp_egr = max(r['bp_egr_lossless'] for r in readings)
            max_bp_ing = max(r['bp_ing_lossless'] for r in readings)
            max_q_lat = max(r['voq_delay_wm'] for r in readings)
            st.log(f"{node_name:>7} {intf:>14} {frame_size:>6} {rate:>5} "
                   f"{max_q_occ:>12} {max_q_wm:>10} "
                   f"{max_bp_egr:>12} {max_bp_ing:>12} {max_q_lat:>10}")

    # ---- Soft pass/fail ----
    # Confirm the stream actually forwarded *every* read. Without per-read
    # gating a transient flap mid-iteration would leave one window with
    # zero occupancy that summed against the others would still look fine,
    # masking the broken data path.
    no_traffic = []
    drop_violations = []
    for key, per_iter in sorted(traffic_stats.items()):
        frame_size, rate = key
        for read_idx, t in enumerate(per_iter, start=1):
            ing_rx = t.get('ing', {}).get('rx_ok', 0)
            egr_tx = t.get('egr', {}).get('tx_ok', 0)
            ing_drp = t.get('ing', {}).get('rx_drp', 0)
            egr_drp = t.get('egr', {}).get('tx_drp', 0)
            if ing_rx == 0 or egr_tx == 0:
                no_traffic.append(
                    f"frame={frame_size}B rate={rate}%% read#{read_idx}: "
                    f"ing rx_ok={ing_rx} egr tx_ok={egr_tx}")
            if ing_drp or egr_drp:
                drop_violations.append(
                    f"frame={frame_size}B rate={rate}%% read#{read_idx}: "
                    f"ing rx_drp={ing_drp} egr tx_drp={egr_drp}")

    if no_traffic:
        for v in no_traffic:
            st.log(f"NO-TRAFFIC: {v}")
        st.report_fail('msg',
            f"{len(no_traffic)} read(s) saw zero ingress or egress "
            f"packets; occupancy results are not trustworthy")

    if drop_violations:
        for v in drop_violations:
            st.log(f"DROP: {v}")
        st.report_fail('msg',
            f"{len(drop_violations)} read(s) saw unexpected drops; "
            f"occupancy results are not trustworthy")

    violations = []
    for (frame_size, rate), per_node in sorted(results.items()):
        if frame_size < PKTS_IN_QUEUE_CHECK_MIN_FRAME_SIZE:
            st.log(f"Skipping pkts-in-queue check for frame={frame_size}B "
                   f"rate={rate}%% (< {PKTS_IN_QUEUE_CHECK_MIN_FRAME_SIZE}B)")
            continue
        for node_name, readings in per_node.items():
            for r in readings:
                if r['pkts_in_queue'] > MAX_PKTS_IN_QUEUE:
                    violations.append(
                        f"{node_name} {r['intf']} {r['label']}: "
                        f"pkts_in_queue={r['pkts_in_queue']} > "
                        f"{MAX_PKTS_IN_QUEUE} (src={r['pkts_in_queue_src']})")

    if violations:
        for v in violations:
            st.log(f"VIOLATION: {v}")
        st.report_fail('msg',
            f"{len(violations)} reading(s) exceeded {MAX_PKTS_IN_QUEUE} "
            f"packets in queue (see log)")

    st.report_pass('test_case_passed')
