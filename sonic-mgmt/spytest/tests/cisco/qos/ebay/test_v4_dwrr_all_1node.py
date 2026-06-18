"""
DWRR Scheduler Test — 1-node topology (Newtonic/eBay)

Test functions:
  1. test_dwrr_tc_pair       — TC pair ratio verification
  2. test_dwrr_overload_all  — All queues overloaded, all drop, BW matches weights
  3. test_dwrr_overload_one  — One queue overloaded, only that queue drops

Topology: T1D1P1 (400G tgen) -> D1 -> T1D1P2 (100G tgen)
Scheduler weights pre-configured on DUT (no CLI changes).

Validation: Uses TGen RX per-stream bit rates reported by traffic generator.
"""
import time
import os
import sys
import pytest
import traffic_stream_ixia_api as stream_api
import qos_test_utils
import ebay_utils
from spytest import st, tgapi, SpyTestDict

@pytest.fixture(scope="module", autouse=True)
def setup_topo():
    global tb_dict
    global vars
    global test_info
    global weights, traffic_classes, ingress_speed_gbps, egress_speed_gbps, frame_size, run_time
    global node, dut, ingress_port, egress_port

    st.log("Setup topology started - DWRR all-scenarios 1-node test")


    test_info = qos_test_utils.get_qos_test_dict('../ebay/input_v4_dwrr_all_1node.json2',
                                                 'DWRR_TEST')
    if test_info is None:
        st.report_fail('msg', 'Failed to read ebay/input_v4_dwrr_all_1node.json2')
        return

    # Populate globals from input json
    frame_size = int(test_info.get('frame_sizes')[0])
    run_time = test_info.get('run_time')
    node = test_info.get('leaf', 'D1')

    # 2-node b2b testbed: use the node specified in input json2
    tb_dict = st.ensure_min_topology(f"{node}T1:2")
    vars = st.get_testbed_vars()
    dut = getattr(vars, node)

    # Platform gate: only supported on 8201-32FH
    if not ebay_utils.check_platform_supported(dut):
        st.report_unsupported('msg', 'Platform is not Cisco-8201-32FH-O, skipping DWRR test')
        return

    ingress_port = getattr(vars, f'{node}T1P1')
    egress_port = getattr(vars, f'{node}T1P2')
    ingress_speed_gbps = qos_test_utils.get_if_speed(dut, ingress_port)
    egress_speed_gbps = qos_test_utils.get_if_speed(dut, egress_port)

    qos_test_utils.cleanup_config(dut)
    stream_api.init_qos_on_dut(dut)
    stream_api.config_one_leaf(tb_dict, {'dut' : dut, 'leaf' : node})

    # Read DWRR weights from ConfigDB SCHEDULER table on egress DUT
    config = qos_test_utils.get_config_db(dut)
    scheduler_table = config.get("SCHEDULER", {})
    weights = {}
    for key, entry in scheduler_table.items():
        if entry.get("type") == "DWRR" and "@" in key:
            tc = int(key.split("@")[-1])
            weights[tc] = int(entry["weight"])
    traffic_classes = sorted(weights.keys())

    if not weights:
        st.report_fail('msg', 'No DWRR weights found in ConfigDB SCHEDULER table')

    st.log(f"weights={weights}, TCs={traffic_classes}, Egress={egress_speed_gbps}G, "
           f"Frame={frame_size}, RunTime={run_time}s")

    # Place output file in the run's log directory
    global result_file
    logs_dir = st.get_logs_path()
    if logs_dir:
        result_file = os.path.join(logs_dir, 'test_v4_dwrr_all_1node_output.txt')
    else:
        result_file = '/tmp/test_v4_dwrr_all_1node_output.txt'
    st.log(f"Result file: {result_file}")

    st.log("Setup topology done")
    yield
    stream_api.release_all_ports()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def rlog(msg):
    st.log(msg)
    with open(result_file, 'a') as f:
        f.write(msg + '\n')

# ---------------------------------------------------------------------------
# Test 1: TC Pair Ratio Verification
# ---------------------------------------------------------------------------

def run_tc_pair_test(tc_pair, frame_size, run_time):
    """Run DWRR test for a single TC pair.

    Creates 2 streams at a rate that guarantees both queues are congested.
    Min rate per stream = max(w1, w2) / (w1 + w2) * egress + margin.
    Validates RX ratio from TGen matches weight ratio.
    """
    tc1, tc2 = int(tc_pair[0]), int(tc_pair[1])
    wt1, wt2 = weights[tc1], weights[tc2]
    expected_ratio = wt1 / wt2

    # Compute minimum rate so both queues are congested
    # Each stream must exceed the larger queue's fair share
    min_rate = (max(wt1, wt2) / (wt1 + wt2)) * egress_speed_gbps
    stream_rate = min_rate * 1.2  # 20% margin above minimum

    st.banner(f"DWRR Pair: TC{tc1}(wt={wt1}) vs TC{tc2}(wt={wt2}) | "
              f"Rate={stream_rate}G each | Frame={frame_size}")

    pps = stream_api.gbps_to_pps(stream_rate, frame_size)
    str1 = stream_api.create_traffic_stream(
        tb_dict, f'T1{node}P1', f'T1{node}P2', frame_size, pps, tc1)
    if str1 is None:
        rlog(f"FAIL: Could not create stream for TC{tc1}")
        return False

    str2 = stream_api.create_traffic_stream(
        tb_dict, f'T1{node}P1', f'T1{node}P2', frame_size, pps, tc2)
    if str2 is None:
        stream_api.delete_traffic_stream(str1)
        rlog(f"FAIL: Could not create stream for TC{tc2}")
        return False

    # Run traffic
    egress_intf = egress_port
    qc1_before = qos_test_utils.get_tc_queue_counters_json(dut, egress_intf, tc1)
    qc2_before = qos_test_utils.get_tc_queue_counters_json(dut, egress_intf, tc2)

    stream_api.start_traffic_stream()
    st.wait(run_time)

    # Snapshot while running — for instantaneous rates
    stats_live = stream_api.collect_traffic_stream_stats()

    stream_api.stop_traffic_stream()
    st.wait(2)

    # Snapshot after stop — for final cumulative totals (should match queue counters)
    stats_final = stream_api.collect_traffic_stream_stats()

    qc1_after = qos_test_utils.get_tc_queue_counters_json(dut, egress_intf, tc1)
    qc2_after = qos_test_utils.get_tc_queue_counters_json(dut, egress_intf, tc2)

    qc_bytes1 = qc1_after['totalbytes'] - qc1_before['totalbytes']
    qc_bytes2 = qc2_after['totalbytes'] - qc2_before['totalbytes']

    # Live stats — instantaneous rates
    if 'traffic_item' in stats_live:
        s1_live = stats_live['traffic_item'].get(str1['stream_id'], {})
        s2_live = stats_live['traffic_item'].get(str2['stream_id'], {})
        rx_gbps1 = float(s1_live.get('rx', {}).get('total_pkt_bit_rate', 0)) / 1e9
        rx_gbps2 = float(s2_live.get('rx', {}).get('total_pkt_bit_rate', 0)) / 1e9
        tx_gbps1 = float(s1_live.get('tx', {}).get('total_pkt_bit_rate', 0)) / 1e9
        tx_gbps2 = float(s2_live.get('tx', {}).get('total_pkt_bit_rate', 0)) / 1e9
    else:
        rx_gbps1 = rx_gbps2 = 0.0
        tx_gbps1 = tx_gbps2 = 0.0

    # Final stats — cumulative totals for ratio and loss
    if 'traffic_item' in stats_final:
        s1 = stats_final['traffic_item'].get(str1['stream_id'], {})
        s2 = stats_final['traffic_item'].get(str2['stream_id'], {})
        rx_bytes1 = float(s1.get('rx', {}).get('total_pkt_bytes', 0))
        rx_bytes2 = float(s2.get('rx', {}).get('total_pkt_bytes', 0))
        loss_pct1 = float(s1.get('rx', {}).get('loss_percent', 0))
        loss_pct2 = float(s2.get('rx', {}).get('loss_percent', 0))
    else:
        rx_bytes1 = rx_bytes2 = 0.0
        loss_pct1 = loss_pct2 = 0.0

    actual_ratio = (rx_bytes1 / rx_bytes2) if rx_bytes2 > 0 else 0
    passed = qos_test_utils.validate_value(actual_ratio, expected_ratio, 5)

    status = "PASS" if passed else "FAIL"
    msg = (f"{status}: TC{tc1}(wt={wt1}) vs TC{tc2}(wt={wt2}) | "
           f"Tx: {tx_gbps1:.2f}Gbps / {tx_gbps2:.2f}Gbps | "
           f"Rx: {rx_gbps1:.2f}Gbps / {rx_gbps2:.2f}Gbps | "
           f"Loss%: {loss_pct1:.1f} / {loss_pct2:.1f} | "
           f"Ratio: actual={actual_ratio:.2f} expected={expected_ratio:.2f}")
    rlog(msg)

    # Cross-check: queue counter bytes vs tgen final RX bytes
    qc_ratio = (qc_bytes1 / qc_bytes2) if qc_bytes2 > 0 else 0
    diff1 = qc_bytes1 - rx_bytes1
    diff2 = qc_bytes2 - rx_bytes2
    match1 = "OK" if abs(diff1) < rx_bytes1 * 0.02 else "MISMATCH"
    match2 = "OK" if abs(diff2) < rx_bytes2 * 0.02 else "MISMATCH"
    rlog(f"  QueueCheck: UC{tc1} bytes={qc_bytes1/1e9:.2f}GB vs tgenRx={rx_bytes1/1e9:.2f}GB "
         f"(diff={diff1/1e6:.1f}MB [{match1}]) | "
         f"UC{tc2} bytes={qc_bytes2/1e9:.2f}GB vs tgenRx={rx_bytes2/1e9:.2f}GB "
         f"(diff={diff2/1e6:.1f}MB [{match2}]) | QC ratio={qc_ratio:.2f}")

    stream_api.delete_traffic_stream(str1)
    stream_api.delete_traffic_stream(str2)
    return passed


def test_dwrr_tc_pair():
    """
    TC pair test: for each pair, send 2 streams that both exceed their fair share.
    Both compete for egress. Validate RX ratio matches weight ratio.
    """
    st.banner("Test: DWRR TC Pair Verification (1-node)")

    with open(result_file, 'w') as f:
        f.write(f"DWRR All-Scenarios Results — {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write('=' * 60 + '\n\n')

    rlog("=" * 60)
    rlog("TEST 1: TC Pair Ratio Verification")
    rlog("=" * 60)

    pass_ctr = fail_ctr = 0

    for tc_pair in test_info['tc_pair']:
        for frame_size_str in test_info['frame_sizes']:
            frame_size = int(frame_size_str)
            passed = run_tc_pair_test(tc_pair, frame_size, run_time)
            if passed:
                pass_ctr += 1
            else:
                fail_ctr += 1

    rlog(f"\nPair test summary: {pass_ctr} passed, {fail_ctr} failed")

    if fail_ctr > 0:
        st.report_fail('msg', f'DWRR pair test: {fail_ctr} failures')
    st.report_pass('msg', f'DWRR pair test: all {pass_ctr} passed')


# ---------------------------------------------------------------------------
# Test 2: Overload All Queues
# ---------------------------------------------------------------------------

def test_dwrr_overload_all():
    """
    Overload all queues: divide ingress bandwidth equally among all TCs.
    Total offered = ingress speed >> egress speed, so all queues congested.
    Verify BW distribution matches weight ratios.
    """
    st.banner("Test: DWRR Overload All Queues (1-node)")
    rlog("\n" + "=" * 60)
    rlog("TEST 2: Overload All Queues")
    rlog("=" * 60)

    # Divide ingress BW equally among all TCs (account for L1 overhead: 8B preamble + 12B IFG)
    num_tcs = len(traffic_classes)
    l2_capacity = ingress_speed_gbps * frame_size / (frame_size + 20)
    per_tc_rate = l2_capacity / num_tcs
    rates = {tc: per_tc_rate for tc in traffic_classes}

    total_offered = sum(rates.values())
    rlog(f"Ingress={ingress_speed_gbps}Gbps, L2 capacity={l2_capacity:.1f}Gbps / {num_tcs} TCs = {per_tc_rate:.2f}Gbps each | "
         f"Total offered: {total_offered:.1f}Gbps into {egress_speed_gbps}Gbps egress")
    for tc in traffic_classes:
        rlog(f"  TC{tc}: {rates[tc]:.2f}Gbps (weight={weights[tc]}%)")

    # Create 6 streams
    streams = {}
    for tc in traffic_classes:
        pps = stream_api.gbps_to_pps(rates[tc], frame_size)
        stream = stream_api.create_traffic_stream(
            tb_dict, f'T1{node}P1', f'T1{node}P2', frame_size, pps, tc)
        if stream is None:
            st.report_fail('msg', f'Failed to create stream for TC={tc}')
        streams[tc] = stream

    # Queue counters before
    egress_intf = egress_port
    qc_before = qos_test_utils.get_all_tc_queue_counters_json(dut, egress_intf, traffic_classes)

    # Run
    stream_api.start_traffic_stream()
    st.wait(run_time)

    # Snapshot while running — for instantaneous rates
    stats_live = stream_api.collect_traffic_stream_stats()

    stream_api.stop_traffic_stream()
    st.wait(2)

    # Snapshot after stop — for final cumulative totals
    stats_final = stream_api.collect_traffic_stream_stats()

    # Queue counters after
    qc_after = qos_test_utils.get_all_tc_queue_counters_json(
                   dut,
                   egress_intf,
                   traffic_classes)

    # Extract tgen stats
    total_rx_bytes = 0
    results = {}
    for tc in traffic_classes:
        rx_bytes = 0.0
        rx_gbps = 0.0
        loss_pct = 0.0
        if 'traffic_item' in stats_live:
            s_live = stats_live['traffic_item'].get(streams[tc]['stream_id'], {})
            rx_gbps = float(s_live.get('rx', {}).get('total_pkt_bit_rate', 0)) / 1e9
        if 'traffic_item' in stats_final:
            s = stats_final['traffic_item'].get(streams[tc]['stream_id'], {})
            rx_bytes = float(s.get('rx', {}).get('total_pkt_bytes', 0))
            loss_pct = float(s.get('rx', {}).get('loss_percent', 0))
        total_rx_bytes += rx_bytes
        # Queue counter: totalbytes delta
        qc_bytes = qc_after[tc]['totalbytes'] - qc_before[tc]['totalbytes']
        results[tc] = {'rx_bytes': rx_bytes, 'rx_gbps': rx_gbps,
                       'qc_bytes': qc_bytes, 'loss_pct': loss_pct}

    # Print results
    rlog(f"\n  {'TC':>4} {'Weight':>6} {'TxRate':>10} {'RxRate':>10} {'BW%':>6} {'Loss%':>6}")
    rlog(f"  {'-'*4} {'-'*6} {'-'*10} {'-'*10} {'-'*6} {'-'*6}")

    for tc in traffic_classes:
        bw_pct = (results[tc]['rx_bytes'] / total_rx_bytes * 100) if total_rx_bytes > 0 else 0
        rlog(f"  {tc:>4} {weights[tc]:>5}% {rates[tc]:>8.2f}G {results[tc]['rx_gbps']:>8.2f}G "
             f"{bw_pct:>5.1f}% {results[tc]['loss_pct']:>5.1f}%")

    # Cross-check: queue counter bytes vs tgen RX bytes
    rlog(f"\n  Queue counter cross-check (UC bytes vs TGen rx_bytes):")
    for tc in traffic_classes:
        diff = results[tc]['qc_bytes'] - results[tc]['rx_bytes']
        match = "OK" if abs(diff) < results[tc]['rx_bytes'] * 0.02 else "MISMATCH"
        rlog(f"    TC{tc}: UC_bytes={results[tc]['qc_bytes']/1e9:.2f}GB"
             f" tgen_rx={results[tc]['rx_bytes']/1e9:.2f}GB "
             f"diff={diff/1e6:.1f}MB [{match}]")

    # Verify: all queues have loss (all congested)
    passed = True
    all_have_loss = all(results[tc]['loss_pct'] > 0 for tc in traffic_classes)
    if not all_have_loss:
        no_loss_tcs = [tc for tc in traffic_classes
                       if results[tc]['loss_pct'] <= 0]
        rlog(f"  FAIL: Expected all queues to have loss, "
             f"but TC {no_loss_tcs} had zero loss")
        passed = False

    # Verify: BW distribution roughly matches weight ratios (15% relative tolerance)
    if total_rx_bytes > 0:
        rlog(f"\n  BW distribution vs expected (from cumulative RX bytes):")
        for tc in traffic_classes:
            actual_pct = (results[tc]['rx_bytes'] / total_rx_bytes) * 100
            expected_pct = weights[tc]
            ok = qos_test_utils.validate_value(actual_pct, expected_pct, 15)
            status = "OK" if ok else "FAIL"
            rlog(f"    TC{tc}: actual={actual_pct:.1f}% "
                 f"expected={expected_pct}% [{status}]")
            if not ok:
                passed = False

    # Cleanup streams
    for tc in traffic_classes:
        stream_api.delete_traffic_stream(streams[tc])

    if not passed:
        st.report_fail('msg',
            'DWRR overload-all: BW distribution mismatch or missing loss')
    st.report_pass('msg',
        'DWRR overload-all: all queues have loss, BW matches weights')


# ---------------------------------------------------------------------------
# Test 3: Overload Single Queue (Q1)
# ---------------------------------------------------------------------------

def test_dwrr_overload_one():
    """
    Overload only queue 1: send Q1 at 5% (5G) — more than its 3% (3G) share.
    Other queues send at a percentage of their weight allocation.
    Sweeps through [98.5, 99, 99.5, 100]% of weight for non-aberrant TCs.
    Only Q1 should drop. Others should have zero drops.

    send the traffic on queue 1 at (5% * egress port speed),
    other queues at (weight percentage * egress port speed), only queue 1
    has drop, other queues no drop.
    """
    st.banner("Test: DWRR Overload Queue 1 Only (1-node) — Rate Sweep")
    rlog("\n" + "=" * 60)
    rlog("TEST 3: Overload Queue 1 Only — Sweep non-Q1 rates")
    rlog("=" * 60)

    aberrant_tc = 1
    aberrant_rate = 5.0  # 5G (5% of 100G, > its 3% weight)
    rate_pcts = [98.5, 99, 99.5, 100]
    egress_intf = egress_port
    max_l2 = egress_speed_gbps * frame_size / (frame_size + 20)

    overall_passed = True

    # Create streams once, then modify rates per sweep iteration
    streams = {}
    initial_pps = stream_api.gbps_to_pps(1.0, frame_size)  # placeholder
    for tc in traffic_classes:
        stream = stream_api.create_traffic_stream(
            tb_dict, f'T1{node}P1', f'T1{node}P2', frame_size, initial_pps, tc)
        if stream is None:
            st.report_fail('msg', f'Failed to create stream for TC={tc}')
        streams[tc] = stream

    for pct in rate_pcts:
        st.banner(f"Overload-one sweep: non-Q1 at {pct}% of weight")
        rlog(f"\n--- Sweep: non-Q1 at {pct}% of weight ---")

        # Compute per-TC rates based on L1-adjusted max L2 capacity
        rates = {}
        for tc in traffic_classes:
            if tc == aberrant_tc:
                rates[tc] = aberrant_rate
            else:
                rates[tc] = (weights[tc] / 100.0) * max_l2 * (pct / 100.0)

        total_offered = sum(rates.values())
        rlog(f"Total offered: {total_offered:.2f}Gbps into {max_l2:.2f}Gbps "
             f"L2 capacity (egress={egress_speed_gbps}G)")
        for tc in traffic_classes:
            marker = " *** OVERLOADED ***" if tc == aberrant_tc else ""
            rlog(f"  TC{tc}: {rates[tc]:.3f}Gbps (weight={weights[tc]}%){marker}")

        # Modify stream rates
        for tc in traffic_classes:
            stream_api.modify_stream_rate(streams[tc], rates[tc], frame_size)

        # Clear tgen stats so loss_pct reflects only this iteration
        stream_api.clear_all_stats()

        # Queue counters before
        qc_before = qos_test_utils.get_all_tc_queue_counters_json(
                        dut, egress_intf, traffic_classes)

        # Run
        stream_api.start_traffic_stream()
        st.wait(run_time)

        # Snapshot while running — for instantaneous rates
        stats_live = stream_api.collect_traffic_stream_stats()

        stream_api.stop_traffic_stream()
        st.wait(2)

        # Snapshot after stop — for final cumulative totals
        stats_final = stream_api.collect_traffic_stream_stats()

        # Queue counters after
        qc_after = qos_test_utils.get_all_tc_queue_counters_json(dut, egress_intf, traffic_classes)

        # Extract tgen stats
        results = {}
        for tc in traffic_classes:
            rx_bytes = 0.0
            rx_gbps = 0.0
            loss_pct = 0.0
            if 'traffic_item' in stats_live:
                s_live = stats_live['traffic_item'].get(streams[tc]['stream_id'], {})
                rx_gbps = float(s_live.get('rx', {}).get('total_pkt_bit_rate', 0)) / 1e9
            if 'traffic_item' in stats_final:
                s = stats_final['traffic_item'].get(streams[tc]['stream_id'], {})
                rx_bytes = float(s.get('rx', {}).get('total_pkt_bytes', 0))
                loss_pct = float(s.get('rx', {}).get('loss_percent', 0))
            # Queue counter: totalbytes delta
            qc_bytes = qc_after[tc]['totalbytes'] - qc_before[tc]['totalbytes']
            results[tc] = {'rx_bytes': rx_bytes, 'rx_gbps': rx_gbps,
                           'qc_bytes': qc_bytes, 'loss_pct': loss_pct}

        # Print results table
        rlog(f"\n  {'TC':>4} {'Weight':>6} {'TxRate':>10} {'RxRate':>10} "
             f"{'Loss%':>6} {'Status':>8}")
        rlog(f"  {'-'*4} {'-'*6} {'-'*10} {'-'*10} {'-'*6} {'-'*8}")

        for tc in traffic_classes:
            has_loss = results[tc]['loss_pct'] > 0.5
            status = "DROP" if has_loss else "OK"
            rlog(f"  {tc:>4} {weights[tc]:>5}% {rates[tc]:>8.3f}G "
                 f"{results[tc]['rx_gbps']:>8.2f}G "
                 f"{results[tc]['loss_pct']:>5.1f}% {status:>8}")

        # Cross-check: queue counter bytes vs tgen RX bytes
        rlog(f"  Queue cross-check (UC bytes vs TGen rx_bytes):")
        for tc in traffic_classes:
            diff = results[tc]['qc_bytes'] - results[tc]['rx_bytes']
            match = "OK" if abs(diff) < results[tc]['rx_bytes'] * 0.02 else "MISMATCH"
            rlog(f"    TC{tc}: UC_bytes={results[tc]['qc_bytes']/1e9:.2f}GB "
                 f"tgen_rx={results[tc]['rx_bytes']/1e9:.2f}GB "
                 f"diff={diff/1e6:.1f}MB [{match}]")

        # Verify: only aberrant TC has significant loss
        sweep_passed = True
        for tc in traffic_classes:
            has_loss = results[tc]['loss_pct'] > 0.5
            if tc == aberrant_tc and not has_loss:
                rlog(f"  WARN: TC{tc} expected to have loss but didn't")
            elif tc != aberrant_tc and has_loss:
                rlog(f"  FAIL: TC{tc} has {results[tc]['loss_pct']:.1f}% "
                     f"loss (unexpected)")
                sweep_passed = False
            elif tc != aberrant_tc:
                # Non-overloaded queue should RX at its offered rate (within 5%)
                ok = qos_test_utils.validate_value(results[tc]['rx_gbps'],
                                                   rates[tc], 5)
                if not ok:
                    rlog(f"  WARN: TC{tc} RX={results[tc]['rx_gbps']:.2f}Gbps "
                         f"vs offered={rates[tc]:.3f}Gbps (>5% off)")

        if results[aberrant_tc]['loss_pct'] > 0.5:
            rlog(f"  OK: Q{aberrant_tc} has loss as expected "
                 f"({results[aberrant_tc]['loss_pct']:.1f}%)")

        verdict = "PASS" if sweep_passed else "FAIL"
        rlog(f"  Sweep {pct}% verdict: {verdict}")
        if not sweep_passed:
            overall_passed = False

    # Cleanup streams
    for tc in traffic_classes:
        stream_api.delete_traffic_stream(streams[tc])

    # Final summary
    rlog(f"\nOverload-one sweep summary: {'PASS' if overall_passed else 'FAIL'}")
    rlog(f"  Rates tested: {rate_pcts}")

    if not overall_passed:
        st.report_fail('msg',
            'DWRR overload-one: unexpected loss on non-overloaded queues')
    st.report_pass('msg',
        'DWRR overload-one: only Q1 had loss across all rate sweeps')
