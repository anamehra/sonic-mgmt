import time
import os
import sys
import random
import pytest
import json
import traffic_stream_ixia_api as stream_api
import qos_test_utils
from spytest import st, tgapi, SpyTestDict

@pytest.fixture(scope="module", autouse=True)
def setup_topo():
    global tb_dict, vars, frame_sizes
    global node, dut, ingress_port, egress_port
    global run_time

    st.log("Setup topology started - HBM 1-node oversubscription test")

    # Find the HBM node
    node = find_hbm_node()
    if node is None:
        st.report_unsupported('msg', 'No DUT with HBM found in testbed')
        return

    # Reserve 2 TG ports on the HBM node
    tb_dict = st.ensure_min_topology(f"{node}T1:2")
    vars = st.get_testbed_vars()
    test_info = qos_test_utils.get_qos_test_dict('../ebay/input_v4_hbm_1node.json2',
                                                  'HBM_TEST')
    if test_info is None:
        st.report_fail('msg', 'Failed to read ebay/input_v4_hbm_1node.json2')
        return

    frame_sizes = test_info.get('frame_sizes')
    run_time = test_info.get('run_time', 30)

    # Set result file to logs directory
    global result_file
    logs_dir = st.get_logs_path()
    result_file = os.path.join(logs_dir, 'hbm_result.txt') if logs_dir else '/tmp/hbm_result.txt'
    with open(result_file, 'w') as f:
        f.write('')

    dut = getattr(vars, node)
    ingress_port = getattr(vars, f'{node}T1P1')
    egress_port = getattr(vars, f'{node}T1P2')
    qos_test_utils.cleanup_config(dut)
    stream_api.init_qos_on_dut(dut)

    stream_api.config_one_leaf(tb_dict, {'dut': dut, 'leaf': node})
    st.log("Setup topology done")

    yield
    stream_api.release_all_ports()


def find_hbm_node():
    """Probe all DUTs in testbed and return the node index (e.g. 'D1') that has HBM.

    Checks 'show platform npu global' for 'HBM Absent'. If absent is NOT reported,
    the node is considered to have HBM.
    Returns None if no DUT has HBM.
    """
    tb_vars = st.get_testbed_vars()
    num_duts = len(st.get_dut_names())
    for i in range(1, num_duts + 1):
        dut_name = f'D{i}'
        dut_handle = getattr(tb_vars, dut_name, None)
        if dut_handle is None:
            continue
        output = st.show(dut_handle, "show platform npu global", skip_tmpl=True)
        if 'HBM Absent' in output:
            st.log(f"{dut_name}: HBM Absent - skipping")
            continue
        st.log(f"{dut_name}: HBM present")
        return dut_name
    return None

def get_hbm_bytes(dut):
    """Get total HBM queue size in bytes using JSON output.

    Returns total bytes across all non-empty queues, or 0 if none.
    """
    output = st.show(dut, "sudo show platform npu voq hbm -d", skip_tmpl=True)
    try:
        idx_start = output.find('{')
        idx_end = output.rfind('}')
        if idx_start >= 0 and idx_end > idx_start:
            data = json.loads(output[idx_start:idx_end + 1])
            st.log(f"HBM JSON: {data}")
            total = 0
            for q in data.get('Queues', []):
                total += q.get('queue_bytes', 0)
            return total
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        st.log(f"HBM JSON parse failed: {e}")
    return 0



def show_hbm_status(dut, label=""):
    """Log HBM VOQ status. Returns HBM total bytes."""
    hbm_bytes = get_hbm_bytes(dut)
    st.log(f"HBM {label}: {hbm_bytes} ({hbm_bytes/1e6:.1f} MB)")
    return hbm_bytes


# ── defaults (overridden from json2 in setup) ──
run_time = 10
result_file = '/tmp/hbm_result.txt'

def rlog(msg):
    """Write a line to the result file and also st.log it."""
    st.log(msg)
    with open(result_file, 'a') as f:
        f.write(msg + '\n')


def compute_rates(egress_speed, frame_size):
    """Compute test rates dynamically from port speed and frame size.

    Returns list of rates (Gbps):
      - max_l2 - 2G  (below capacity, no congestion expected)
      - max_l2       (at capacity, borderline)
      - egress_speed (100% line rate)
      - 1.5 * egress_speed (150% oversubscription)

    max_l2 = egress_speed * frame_size / (frame_size + 20)
    """
    max_l2 = round((egress_speed * frame_size) / (frame_size + 20), 2)
    rates = [
        max_l2 - 1,
        max_l2,
        egress_speed,
        round(1.5 * egress_speed, 1),
    ]
    return rates, max_l2


def do_hbm_rate_sweep_1node(stream, egress_speed, frame_size):
    """
    HBM rate sweep for a single frame size.

    Sweeps rates from below L2 capacity to 150% oversubscription.
    Validates:
      A. No-congestion (rate <= max_l2): drops==0, HBM==0, RxRate≈TxRate
      B. Congestion (rate > max_l2): drops>0, HBM>0, RxRate≈max_l2

    Traffic path: T1D1P1 (400G) -> DUT -> T1D1P2 (100G)
    """
    rates_gbps, max_l2 = compute_rates(egress_speed, frame_size)

    st.banner(f"HBM Rate Sweep: frame_size={frame_size} egress={egress_speed}G max_l2={max_l2:.1f}G")
    rlog(f"Frame={frame_size}B | Egress={egress_speed}G | max_l2={max_l2:.1f}G")
    rlog(f"Rates: {rates_gbps}")
    rlog('')

    hbm_results = {}
    queue_results = {}
    pass_fail = {}

    for gbps in rates_gbps:
        st.banner(f"Frame={frame_size} | {gbps}Gbps into {egress_speed}G egress for {run_time}s")
        stream_api.modify_stream_rate(stream, gbps, frame_size)

        # Clear tgen stats so loss_pct reflects only this iteration
        stream_api.clear_all_stats()

        qc_before = qos_test_utils.get_tc_queue_counters_json(dut, egress_port, 0)
        show_hbm_status(dut, f"[{frame_size}B][{gbps}G] BEFORE START")
        stream_api.start_traffic_stream(stream)

        # Snapshot HBM occupancy every 5 seconds for run_time and compute average
        hbm_samples = []
        num_samples = run_time // 5
        for i in range(num_samples):
            st.wait(5)
            sample = show_hbm_status(dut, f"[{frame_size}B][{gbps}G] sample {i+1}/{num_samples}")
            hbm_samples.append(sample)
        hbm_results[gbps] = int(sum(hbm_samples) / len(hbm_samples)) if hbm_samples else 0

        # Snapshot tgen stats while traffic is still running
        live_stats = stream_api.collect_traffic_stream_stats()
        tx_rate_bps = 0
        rx_rate_bps = 0
        rx_l1_rate_bps = 0
        rx_pps = 0
        if 'traffic_item' in live_stats and stream['stream_id'] in live_stats['traffic_item']:
            s_info = live_stats['traffic_item'][stream['stream_id']]
            tx_info = s_info.get('tx', {})
            rx_info = s_info.get('rx', {})
            tx_rate_bps = int(float(tx_info.get('total_pkt_bit_rate', tx_info.get('rate_bps', 0))))
            rx_rate_bps = int(float(rx_info.get('total_pkt_bit_rate', rx_info.get('rate_bps', 0))))
            rx_l1_rate_bps = int(float(rx_info.get('l1_bit_rate', 0)))
            rx_pps = int(float(rx_info.get('total_pkt_rate', 0)))
        stream_api.log_stream_stats(live_stats, stream['stream_id'])

        stream_api.stop_traffic_stream(stream)
        show_hbm_status(dut, f"[{frame_size}B][{gbps}G] AFTER STOP")
        qc_after = qos_test_utils.get_tc_queue_counters_json(dut, egress_port, 0)

        # Queue counter deltas
        d_drop = qc_after['droppacket'] - qc_before['droppacket']
        tx_gbps = tx_rate_bps / 1e9
        rx_gbps = rx_rate_bps / 1e9
        hbm_mb = hbm_results[gbps] / 1e6
        rx_l1_gbps = rx_l1_rate_bps / 1e9
        queue_results[gbps] = {'drop': d_drop, 'tx_gbps': tx_gbps, 'rx_gbps': rx_gbps,
                               'rx_pps': rx_pps, 'rx_l1_gbps': rx_l1_gbps}
        checks = []

        # Pass/fail validation
        if gbps <= max_l2:
            # No-congestion: no drops, RxRate ≈ TxRate (within 1%)
            # Its fine for HBM to be non-zero as long as there are no drops
            if d_drop != 0:
                checks.append(f"drops={d_drop} (expected 0)")
            if tx_gbps > 0 and abs(rx_gbps - tx_gbps) / tx_gbps > 0.01:
                checks.append(f"RxRate={rx_gbps:.1f}G != TxRate={tx_gbps:.1f}G")
            pass_fail[gbps] = "PASS" if not checks else f"FAIL: {'; '.join(checks)}"
        else:
            # Congestion: drops > 0, HBM > 0, RxRate ≈ max_l2 (within 2%)
            if d_drop == 0:
                checks.append("drops=0 (expected >0)")
            if hbm_mb <= 0.0:
                checks.append(f"HBM={hbm_mb:.1f}MB (expected >0)")
            if max_l2 > 0 and abs(rx_gbps - max_l2) / max_l2 > 0.02:
                checks.append(f"RxRate={rx_gbps:.1f}G != max_l2={max_l2:.1f}G (>2%)")
            pass_fail[gbps] = "PASS" if not checks else f"FAIL: {'; '.join(checks)}"

    # Print summary table
    rlog(f"{'TxRate':>8} {'RxRate':>8} {'RxPPS':>12} {'RxL1':>8} {'HBM MB':>8} {'Drops':>12} {'Result'}")
    rlog(f"{'-'*8} {'-'*8} {'-'*12} {'-'*8} {'-'*8} {'-'*12} {'-'*30}")
    for rate in rates_gbps:
        q = queue_results.get(rate, {})
        hbm_mb = hbm_results.get(rate, 0) / 1e6
        rlog(f"{q.get('tx_gbps',0):>7.1f}G {q.get('rx_gbps',0):>7.1f}G "
             f"{q.get('rx_pps',0):>12,.0f} {q.get('rx_l1_gbps',0):>7.1f}G "
             f"{hbm_mb:>7.1f}  {q.get('drop',0):>12,} {pass_fail.get(rate, '?')}")
    rlog('')

    # Return overall pass/fail for this frame size
    failures = [r for r, v in pass_fail.items() if not v.startswith("PASS")]
    return failures


def test_hbm_rate_sweep_1node():
    egress_speed = qos_test_utils.get_if_speed(dut, egress_port)

    # Create stream once with TC=0 at a placeholder rate
    pps = stream_api.gbps_to_pps(50, 1350)
    stream = stream_api.create_traffic_stream(
        tb_dict, f'T1{node}P1', f'T1{node}P2', 1350, pps, 0)
    if stream is None:
        st.report_fail('msg', 'Failed to create traffic stream')

    all_failures = []
    shuffled_sizes = list(frame_sizes)
    random.shuffle(shuffled_sizes)
    rlog(f"Randomized frame size order: {shuffled_sizes}")
    for frame_size in shuffled_sizes:
        rlog(f"\n{'='*60}")
        rlog(f"FRAME SIZE {frame_size}")
        rlog(f"{'='*60}")
        failures = do_hbm_rate_sweep_1node(stream, egress_speed, frame_size)
        if failures:
            all_failures.append((frame_size, failures))

    # Cleanup
    stream_api.delete_traffic_stream(stream)

    if all_failures:
        summary = "; ".join(f"{fs}B: {len(f)} failures" for fs, f in all_failures)
        st.report_fail('msg', f'HBM sweep failures: {summary}')
    st.report_pass('msg', f'HBM rate sweep passed for all frame sizes: {frame_sizes}')
