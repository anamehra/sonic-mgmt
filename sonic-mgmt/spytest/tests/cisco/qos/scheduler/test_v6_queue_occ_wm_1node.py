"""
Single-Node L3 Queue Occupancy / Watermark Measurement Test

Measures the number of packets resident in the system while the DUT
forwards a single-stream IPv6 traffic flow at below-line-rate.  No
congestion is expected; the goal is to characterize transient queue
occupancy / buffer-pool occupancy / queue-delay watermark across a
range of packet sizes.

Topology:
    TGEN P1 ----> D3 ----> TGEN P2

    T1D3:2  --  2 TGEN ports connected to DUT D3 (L3 routed)

For each packet size in PACKET_SIZES:
    For each rate in TRAFFIC_RATES_DEFAULT (= [99, 100], or [99] on carib):
    - Configure a single IPv6 stream P1 -> P2 at the chosen rate
    - Start traffic and take NUM_READS independent measurement windows.
      Each window:
          * clear interface/queue/watermark/buffer-pool counters
          * (laguna/carib) clear NPU voq queue counters (incl. delay wm)
          * wait READ_WAIT_SECS for new watermarks to accumulate
          * read buffer-pool watermark, queue watermark, queue counters
          * (laguna/carib) read curr-occupancy and delay watermark
    - Stop and remove the stream

Pass criteria (soft):
    For every read window with frame_size >= PKTS_IN_QUEUE_CHECK_MIN_FRAME_SIZE,
    the queue is expected to hold no more than MAX_PKTS_IN_QUEUE packets.
        - laguna/carib : SAI_QUEUE_STAT_CURR_OCCUPANCY_BYTES / frame_size
        - other        : buffer-pool watermark / frame_size

    The queue watermark (`show queue watermark unicast`, UC<tc>) is captured
    for observation only -- it is quantized at the hardware level and is not
    a reliable basis for max occupancy validation.
"""

import pytest

from spytest import st, SpyTestDict
import tests.cisco.tortuga.vxlan.vxlan_utils as vxlan_obj
import qos_test_utils as qos_utils
import qos_debug_log_utils as qos_debug
import traffic_stream_ixia_api as stream_api


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ECN_ECT_0 = 0b00
PACKET_SIZES = [340, 512, 720, 1024, 2000, 4000, 6000, 8000]
# Per-platform traffic rates (% of line rate). Default is 99% + 100%; carib
# (Q200) is restricted to 99% only -- see TODO below.
TRAFFIC_RATES_DEFAULT = [99, 100]
TRAFFIC_RATES_CARIB = [99]
# TODO: enable 100%% rate on carib (Q200) once we configure PFC. Without
# PFC the Q200 NPU cannot ingest 100%% line rate without packet drops.
NUM_READS = 5
READ_WAIT_SECS = 3
WATERMARK_POLL_MS = 2000
QUEUE_COUNTER_POLL_MS = 2000
MAX_PKTS_IN_QUEUE = 15   # this is a soft threshold for pass/fail
# Frame sizes below this threshold are excluded from the pass/fail check
PKTS_IN_QUEUE_CHECK_MIN_FRAME_SIZE = 720

# IPv6 addressing  --  one /64 per DUT port
PORT_SUBNETS = {
    1: {'dut': '2001:db8:1::1', 'tgen': '2001:db8:1::2'},
    2: {'dut': '2001:db8:2::1', 'tgen': '2001:db8:2::2'},
}

TGEN_MACS = {
    1: '00:0a:01:00:01:01',
    2: '00:0a:01:00:02:01',
}

data = SpyTestDict()


# ---------------------------------------------------------------------------
# Module-level fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def queue_occ_module_setup():
    st.ensure_min_topology('T1D3:2')
    vars = st.get_testbed_vars()
    dut = vars.D3

    dut_ports = {1: vars.D3T1P1, 2: vars.D3T1P2}
    tgen_ports = {1: 'T1D3P1', 2: 'T1D3P2'}

    speeds = {idx: qos_utils.get_if_speed(dut, p) for idx, p in dut_ports.items()}
    if len(set(speeds.values())) != 1:
        st.report_fail('msg', f"Port speeds are not uniform: {speeds}")
    port_speed = list(speeds.values())[0]
    st.log(f"Both DUT ports are {port_speed}G")

    stream_api.init_qos_on_dut(dut)
    qos_utils.load_config_db(dut)

    # Discover lossless/ECN queue config on the egress port (P2)
    ecn_cfg = qos_utils.discover_ecn_queue_config(dut, dut_ports[2])
    platform = qos_utils.detect_platform(dut)

    data.dut = dut
    data.dut_ports = dut_ports
    data.tgen_ports = tgen_ports
    data.port_speed = port_speed
    data.ecn_cfg = ecn_cfg
    data.platform = platform

    # ---- Reduce poll intervals so watermark/queue counters refresh quickly ----
    qos_utils.set_queue_watermark_poll_interval(dut, WATERMARK_POLL_MS)
    qos_debug.set_queue_counter_poll_interval(dut, QUEUE_COUNTER_POLL_MS)

    # ---- Clean any existing IP/VLAN config and add IPv6 on P1, P2 ----
    st.banner("Cleaning existing IP/VLAN configuration")
    qos_utils.cleanup_config(dut)

    st.banner("Configuring IPv6 addresses on DUT ports")
    ip_cfg = ''
    for idx, port in dut_ports.items():
        ip_cfg += f'sudo config interface ip add {port} {PORT_SUBNETS[idx]["dut"]}/64\n'
    st.config(dut, ip_cfg, skip_tmpl=True, skip_error_check=True)
    st.wait(3)

    # ---- TGEN NGPF (IPv6) ----
    st.banner("Configuring TGEN NGPF IPv6 device groups")
    int_dict = {}
    for idx in (1, 2):
        int_dict[tgen_ports[idx]] = {
            'host_ip': PORT_SUBNETS[idx]['tgen'],
            'gateway': PORT_SUBNETS[idx]['dut'],
            'mac': TGEN_MACS[idx],
        }
    handles = vxlan_obj.config_tgen_interface(int_dict, addr_family='ipv6')
    data.tgen_handles = handles

    tg = handles[tgen_ports[1]]['tg_handle']
    data.tg = tg

    tg.tg_topology_test_control(action='start_all_protocols')
    st.wait(10)

    for idx in (1, 2):
        gw = PORT_SUBNETS[idx]['dut']
        int_h = handles[tgen_ports[idx]]['int_handle']
        if not vxlan_obj.ping_gateway(handles, tgen_ports[idx], gw, int_h):
            st.report_fail('msg', f"Ping failed: TGEN {tgen_ports[idx]} -> {gw}")

    yield

    # ---- Teardown ----
    st.banner("queue_occ_wm module teardown")
    try:
        tg.tg_traffic_control(action='stop')
    except Exception as e:
        st.log(f"tg stop error: {e}")
    st.wait(2)

    qos_utils.restore_queue_watermark_poll_interval(dut)
    qos_debug.restore_queue_counter_poll_interval(dut)

    for port_key, h in handles.items():
        try:
            tg.tg_interface_config(port_handle=h['port_handle'],
                                   handle=h['int_handle'], mode='destroy')
        except Exception as e:
            st.log(f"TGEN cleanup {port_key}: {e}")

    ip_rm = ''
    for idx, port in dut_ports.items():
        ip_rm += f'sudo config interface ip remove {port} {PORT_SUBNETS[idx]["dut"]}/64\n'
    st.config(dut, ip_rm, skip_tmpl=True, skip_error_check=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _take_one_reading(dut, ingress_intf, egress_intf, tc, platform,
                      frame_size, label):
    """
    Clear all relevant counters, wait, then read everything.  Return a dict
    suitable for the results table.
    """
    st.banner(f"--- {label} ---")

    # Clear interface / queue / pg / buffer-pool / drop counters
    qos_utils.clear_all_counters(dut, wait_time=1)

    # On laguna / carib also clear NPU voq queue counters (clears
    # CURR_OCCUPANCY snapshot and DELAY_WATERMARK for this <intf, tc>)
    if platform in ('laguna', 'carib'):
        qos_utils.clear_ecn_counters_on_port(dut, egress_intf, tc)

    st.wait(READ_WAIT_SECS, "Accumulating watermarks while traffic flows")

    # Interface counters since the clear above. tx_ok on egress proves the
    # stream actually forwarded; non-zero rx_drp/tx_drp means we measured
    # occupancy under unexpected loss.
    ing_pc = qos_utils._get_port_counters_simple(dut, ingress_intf)
    egr_pc = qos_utils._get_port_counters_simple(dut, egress_intf)

    # Buffer pool watermark
    bp_raw = qos_utils.get_buffer_pool_watermark(dut)
    bp_parsed = qos_utils.parse_buffer_pool_watermark(bp_raw) if bp_raw else {}
    bp_egr_lossless = 0
    try:
        bp_egr_lossless = int(str(bp_parsed.get('egr_lossless', 0)).replace(',', ''))
    except (TypeError, ValueError):
        bp_egr_lossless = 0

    # Queue watermark on egress port for this TC
    nodes = {'dut': dut}
    intf_map = {'dut': [egress_intf]}
    q_wm_map = qos_utils.capture_queue_watermark_values(nodes, intf_map, tc)
    q_wm = q_wm_map.get('dut', {}).get(egress_intf, 0)
    try:
        q_wm = int(q_wm)
    except (TypeError, ValueError):
        q_wm = 0

    # Queue counters CLI for visibility
    q_cnt_raw = st.show(dut, f"show queue counters {egress_intf}",
                        skip_tmpl=True, skip_error_check=True)
    st.log(f"show queue counters {egress_intf}:\n{q_cnt_raw}")

    # Platform-specific NPU read (laguna / carib only)
    voq = {}
    if platform in ('laguna', 'carib'):
        voq = qos_utils.get_ecn_counters_on_port(dut, egress_intf, tc, clear=False)

    curr_occ_bytes = int(voq.get('curr_occupancy_bytes', 0)) if voq else 0
    voq_wm_bytes = int(voq.get('watermark_bytes', 0)) if voq else 0
    delay_wm = int(voq.get('delay_watermark', 0)) if voq else 0

    # Estimate "packets in queue" based on the platform's authoritative source.
    # Ceiling division so any byte count above MAX_PKTS_IN_QUEUE * frame_size
    # is reported as exceeding the threshold (floor division undercounts).
    if platform in ('laguna', 'carib'):
        pkts_in_queue_src = 'voq.curr_occupancy_bytes'
        occ_bytes = curr_occ_bytes
    else:
        pkts_in_queue_src = 'buffer_pool.egr_lossless'
        occ_bytes = bp_egr_lossless
    pkts_in_queue = (occ_bytes + frame_size - 1) // frame_size if frame_size > 0 else 0

    reading = {
        'label': label,
        'frame_size': frame_size,
        'bp_egr_lossless': bp_egr_lossless,
        'bp_pools': bp_parsed,
        'q_wm_bytes': q_wm,
        'voq_curr_occ_bytes': curr_occ_bytes,
        'voq_wm_bytes': voq_wm_bytes,
        'voq_delay_wm': delay_wm,
        'pkts_in_queue': pkts_in_queue,
        'pkts_in_queue_src': pkts_in_queue_src,
        'ing_rx_ok': ing_pc.get('rx_ok', 0),
        'ing_rx_drp': ing_pc.get('rx_drp', 0),
        'egr_tx_ok': egr_pc.get('tx_ok', 0),
        'egr_tx_drp': egr_pc.get('tx_drp', 0),
    }

    st.log(f"  >> {label}: bp_egr_lossless={bp_egr_lossless}B "
           f"q_wm={q_wm}B voq_curr_occ={curr_occ_bytes}B "
           f"voq_wm={voq_wm_bytes}B voq_delay_wm={delay_wm} "
           f"-> pkts_in_queue~{pkts_in_queue} (src={pkts_in_queue_src}) "
           f"ing_rx={reading['ing_rx_ok']}/drp={reading['ing_rx_drp']} "
           f"egr_tx={reading['egr_tx_ok']}/drp={reading['egr_tx_drp']}")

    return reading


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def test_l3_queue_occupancy_watermark():
    dut = data.dut
    tg = data.tg
    ecn_cfg = data.ecn_cfg
    tc = ecn_cfg['tc']
    dscp = ecn_cfg['dscp']
    platform = data.platform
    ingress_intf = data.dut_ports[1]
    egress_intf = data.dut_ports[2]
    tgen_ports = data.tgen_ports
    handles = data.tgen_handles

    ip_tos = qos_utils.compute_ip_tos(dscp, ECN_ECT_0)

    rates = TRAFFIC_RATES_CARIB if platform == 'carib' else TRAFFIC_RATES_DEFAULT
    st.log(f"Test config: TC={tc} DSCP={dscp} egress={egress_intf} "
           f"platform={platform} rates={rates}%% "
           f"reads/iter={NUM_READS} read_wait={READ_WAIT_SECS}s")

    all_results = []  # list of (frame_size, rate, [reading, ...])

    try:
        for frame_size in PACKET_SIZES:
            for rate in rates:
                st.banner(f"=== Packet size {frame_size}B @ {rate}%% ===")

                tg_kwargs = dict(
                    port_handle=handles[tgen_ports[1]]['port_handle'],
                    port_handle2=handles[tgen_ports[2]]['port_handle'],
                    mode='create',
                    transmit_mode='continuous',
                    rate_percent=rate,
                    frame_size=frame_size,
                    circuit_endpoint_type='ipv6',
                    ipv6_traffic_class=ip_tos,
                    emulation_src_handle=handles[tgen_ports[1]]['int_handle'],
                    emulation_dst_handle=handles[tgen_ports[2]]['int_handle'],
                )
                stream_result = tg.tg_traffic_config(**tg_kwargs)
                if stream_result.get('status') != '1':
                    st.report_fail('msg',
                        f"Failed to create stream at frame_size={frame_size} "
                        f"rate={rate}%%: {stream_result}")
                stream_id = stream_result['stream_id']
                stream_api.set_pfc_priority_group(tg, stream_result, tc)

                try:
                    # Make sure no leftover traffic before each iteration
                    tg.tg_traffic_control(action='stop')
                    st.wait(2)

                    # Start continuous traffic
                    tg.tg_traffic_control(action='run')

                    readings = []
                    for k in range(NUM_READS):
                        label = f"frame={frame_size}B rate={rate}%% read#{k + 1}"
                        r = _take_one_reading(dut, ingress_intf, egress_intf,
                                              tc, platform, frame_size, label)
                        readings.append(r)

                    tg.tg_traffic_control(action='stop')
                    st.wait(2)

                    all_results.append((frame_size, rate, readings))

                finally:
                    try:
                        tg.tg_traffic_config(mode='remove', stream_id=stream_id)
                    except Exception as e:
                        st.log(f"stream remove error (frame={frame_size} "
                               f"rate={rate}): {e}")

    finally:
        try:
            tg.tg_traffic_control(action='stop')
        except Exception as e:
            st.log(f"final tg stop error: {e}")

    # ---- Summary table ----
    platform_name = qos_utils.get_dut_platform(dut) or "unknown"
    st.banner(f"SUMMARY: queue_occ_wm 1node (platform_tag={platform} "
              f"platform_str={platform_name})")
    hdr = (f"{'Frame':>6} {'Rate':>5} {'Read':>5} {'BP_egr_lossless':>17} "
           f"{'Q_wm_B':>10} {'VOQ_curr_B':>12} {'VOQ_wm_B':>10} "
           f"{'Delay_wm':>10} {'Pkts_q':>7} {'Src':>22}")
    st.log(hdr)
    st.log("-" * len(hdr))
    for frame_size, rate, readings in all_results:
        for k, r in enumerate(readings, start=1):
            st.log(f"{frame_size:>6} {rate:>5} {k:>5} "
                   f"{r['bp_egr_lossless']:>17} "
                   f"{r['q_wm_bytes']:>10} "
                   f"{r['voq_curr_occ_bytes']:>12} "
                   f"{r['voq_wm_bytes']:>10} "
                   f"{r['voq_delay_wm']:>10} "
                   f"{r['pkts_in_queue']:>7} "
                   f"{r['pkts_in_queue_src']:>22}")

    # ---- Aggregate summary (max across reads per frame/rate) ----
    st.banner(f"AGGREGATE SUMMARY (max over {NUM_READS} reads)")
    agg_hdr = (f"{'Frame':>6} {'Rate':>5} "
               f"{'MaxQOcc_B':>12} {'MaxQ_wm_B':>10} "
               f"{'MaxBP_wm_B':>12} {'MaxQ_lat':>10}")
    st.log(agg_hdr)
    st.log("-" * len(agg_hdr))
    for frame_size, rate, readings in all_results:
        max_q_occ = max((r['voq_curr_occ_bytes'] for r in readings), default=0)
        max_q_wm = max((r['voq_wm_bytes'] for r in readings), default=0)
        max_bp_wm = max((r['bp_egr_lossless'] for r in readings), default=0)
        max_q_lat = max((r['voq_delay_wm'] for r in readings), default=0)
        st.log(f"{frame_size:>6} {rate:>5} "
               f"{max_q_occ:>12} {max_q_wm:>10} "
               f"{max_bp_wm:>12} {max_q_lat:>10}")

    # ---- Soft pass/fail ----
    if not all_results:
        st.report_fail('msg', "No iterations completed")

    # First, prove that the data stream actually forwarded each read window.
    # Per-read gating ensures a transient flap that leaves one window with
    # zero rx/tx cannot be hidden by other healthy reads in the same
    # iteration -- the zero-traffic window would otherwise contribute a
    # spuriously empty queue sample to the verdict.
    no_traffic = []
    drop_violations = []
    for frame_size, rate, readings in all_results:
        for read_idx, r in enumerate(readings, start=1):
            ing_rx = r.get('ing_rx_ok', 0)
            egr_tx = r.get('egr_tx_ok', 0)
            ing_drp = r.get('ing_rx_drp', 0)
            egr_drp = r.get('egr_tx_drp', 0)
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
    for frame_size, rate, readings in all_results:
        if frame_size < PKTS_IN_QUEUE_CHECK_MIN_FRAME_SIZE:
            st.log(f"Skipping pkts-in-queue check for frame={frame_size}B "
                   f"rate={rate}%% (< {PKTS_IN_QUEUE_CHECK_MIN_FRAME_SIZE}B threshold)")
            continue
        for r in readings:
            if r['pkts_in_queue'] > MAX_PKTS_IN_QUEUE:
                violations.append(
                    f"frame={frame_size}B rate={rate}%% {r['label']}: "
                    f"pkts_in_queue={r['pkts_in_queue']} > {MAX_PKTS_IN_QUEUE} "
                    f"(src={r['pkts_in_queue_src']})")

    if violations:
        for v in violations:
            st.log(f"VIOLATION: {v}")
        st.report_fail('msg',
            f"{len(violations)} reading(s) exceeded {MAX_PKTS_IN_QUEUE} "
            f"packets in queue (see log)")

    st.report_pass('test_case_passed')
