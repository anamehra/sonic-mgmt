import os
import time
import pytest
import traffic_stream_ixia_api as stream_api
import qos_test_utils
from spytest import st, tgapi

FRAME_SIZE = 1350
L1_OVERHEAD = 20  # preamble(7) + SFD(1) + IFG(12)
result_file = '/tmp/l1_overhead_result.txt'  # default; updated in setup



@pytest.fixture(scope="module", autouse=True)
def setup_topo():
    global tb_dict, node, frame_sizes, result_file

    # Read from input json2 — defaults to D2
    test_info = qos_test_utils.get_qos_test_dict('../ebay/input_v4_l1_overhead_1node.json2',
                                                  'L1_OVERHEAD_TEST')
    if test_info is None:
        st.report_fail('msg',
                       'Failed to read input dictionary ebay/l1_overhead_input.json2')
        return

    node = test_info.get('node')
    frame_sizes = test_info.get('frame_sizes')
    logs_dir = st.get_logs_path()
    if logs_dir:
        result_file = os.path.join(logs_dir, 'l1_overhead_result.txt')
    else:
        result_file = '/tmp/l1_overhead_result.txt'
    # Clear result file at start of run
    with open(result_file, 'w') as f:
        f.write('')

    st.log(f"Setup: 1-node topology on {node} with 2 tgen ports")
    tb_dict = st.ensure_min_topology(f"{node}T1:2")

    for dut in st.get_dut_names():
        qos_test_utils.cleanup_config(dut)
        stream_api.init_qos_on_dut(dut, [])

    # Simple 1-node config: IPs on both tgen-facing ports
    dut_handle = getattr(tb_dict, node)
    stream_api.config_one_leaf(tb_dict, {'leaf': node, 'dut': dut_handle})

    st.log("Setup done")
    yield
    stream_api.release_all_ports()


def rlog(msg):
    st.log(msg)
    with open(result_file, 'a') as f:
        f.write(msg + '\n')


def test_l1_overhead_1node():
    """
    Send traffic T1<node>P1 (high-speed) -> DUT -> T1<node>P2 (low-speed egress).
    Prove that zero-loss threshold = egress_speed * frame / (frame + 20).

    Works on both Spirent and IXIA — just needs 1 DUT with 2 tgen ports.
    The high-speed ingress ensures we can oversubscribe the egress.
    """
    st.banner(f"Test: L1 overhead validation (1-node, {node}, T1{node}P1 -> T1{node}P2)")

    rlog(f"L1 Overhead Test — {time.strftime('%Y-%m-%d %H:%M:%S')}")
    rlog('=' * 60)
    rlog('')

    dut = getattr(tb_dict, node)
    egress_intf = tb_dict[f'{node}T1P2']
    dst_speed = qos_test_utils.get_if_speed(dut, egress_intf)
    tc = 0

    ingress_intf = tb_dict[f'{node}T1P1']
    ingress_speed = qos_test_utils.get_if_speed(dut, ingress_intf)

    if ingress_speed <= dst_speed:
        st.report_unsupported('msg',
            f'Test requires ingress speed > egress speed, got {ingress_speed}G -> {dst_speed}G')

    rlog(f"Ingress port: {ingress_intf} ({ingress_speed}G)")
    rlog(f"Egress port: {egress_intf} ({dst_speed}G)")
    rlog(f"Tgen type: {stream_api.tgen_handle.tg_type}")
    rlog(f"L1 overhead: {L1_OVERHEAD} bytes (preamble + SFD + IFG)")
    rlog('')

    stream = None
    all_pass = True
    results = []  # collect per-frame results for deferred printing

    for frame_size in frame_sizes:
        max_l2_gbps = dst_speed * frame_size / (frame_size + L1_OVERHEAD)
        below_gbps = round(max_l2_gbps - 1.0, 2)
        above_gbps = round(max_l2_gbps + 1.0, 2)

        st.banner(f"Frame={frame_size}B | max_l2={max_l2_gbps:.2f}G | "
                  f"testing {below_gbps}G and {above_gbps}G")

        pps = stream_api.gbps_to_pps(below_gbps, frame_size)
        if stream is None:
            stream = stream_api.create_traffic_stream(
                tb_dict, f'T1{node}P1', f'T1{node}P2', frame_size, pps, tc)
            if stream is None:
                st.report_fail('msg', f'Failed to create stream (frame={frame_size})')
        else:
            # Modify rate and frame size
            stream_api.tgen_handle.tg_traffic_config(
                mode='modify', stream_id=stream['stream_id'],
                rate_pps=pps, frame_size=frame_size,
                length_mode='fixed',
                high_speed_result_analysis=1)

        # --- Below threshold ---
        qc_before = qos_test_utils.get_tc_queue_counters_json(dut, egress_intf, tc)
        stream_api.clear_all_stats()
        stream_api.start_traffic_stream(stream)
        st.wait(30)
        # Live stats for rates (traffic still running)
        live_below = stream_api.collect_traffic_stream_stats()
        tx_gbps_below = rx_gbps_below = 0.0
        if 'traffic_item' in live_below and stream['stream_id'] in live_below['traffic_item']:
            s = live_below['traffic_item'][stream['stream_id']]
            tx_gbps_below = float(s.get('tx', {}).get('total_pkt_bit_rate', 0)) / 1e9
            rx_gbps_below = float(s.get('rx', {}).get('total_pkt_bit_rate', 0)) / 1e9
        st.wait(30)
        stream_api.stop_traffic_stream(stream)
        st.wait(5)
        # Final stats after stop (all packets drained)
        stats_below = stream_api.collect_traffic_stream_stats()
        qc_after = qos_test_utils.get_tc_queue_counters_json(dut, egress_intf, tc)

        tx_below = '0'
        rx_below = '0'
        tgen_loss_below = 0
        if 'traffic_item' in stats_below and stream['stream_id'] in stats_below['traffic_item']:
            s = stats_below['traffic_item'][stream['stream_id']]
            tx_below = s.get('tx', {}).get('total_pkts', '0')
            rx_below = s.get('rx', {}).get('total_pkts', '0')
            tgen_loss_below = int(tx_below) - int(rx_below)
            if tgen_loss_below < 0:
                tgen_loss_below = 0
        drop_below = qc_after['droppacket'] - qc_before['droppacket']

        # --- Above threshold ---
        pps_above = stream_api.gbps_to_pps(above_gbps, frame_size)
        stream_api.tgen_handle.tg_traffic_config(
            mode='modify', stream_id=stream['stream_id'],
            rate_pps=pps_above, frame_size=frame_size,
            length_mode='fixed',
            high_speed_result_analysis=1)

        qc_before = qos_test_utils.get_tc_queue_counters_json(dut, egress_intf, tc)
        stream_api.clear_all_stats()
        stream_api.start_traffic_stream(stream)
        st.wait(30)
        # Live stats for rates (traffic still running)
        live_above = stream_api.collect_traffic_stream_stats()
        tx_gbps_above = rx_gbps_above = 0.0
        if 'traffic_item' in live_above and stream['stream_id'] in live_above['traffic_item']:
            s = live_above['traffic_item'][stream['stream_id']]
            tx_gbps_above = float(s.get('tx', {}).get('total_pkt_bit_rate', 0)) / 1e9
            rx_gbps_above = float(s.get('rx', {}).get('total_pkt_bit_rate', 0)) / 1e9
        st.wait(30)
        stream_api.stop_traffic_stream(stream)
        st.wait(5)
        # Final stats after stop (all packets drained)
        stats_above = stream_api.collect_traffic_stream_stats()
        qc_after = qos_test_utils.get_tc_queue_counters_json(dut, egress_intf, tc)

        tx_above = '0'
        rx_above = '0'
        tgen_loss_above = 0
        if 'traffic_item' in stats_above and stream['stream_id'] in stats_above['traffic_item']:
            s = stats_above['traffic_item'][stream['stream_id']]
            tx_above = s.get('tx', {}).get('total_pkts', '0')
            rx_above = s.get('rx', {}).get('total_pkts', '0')
            tgen_loss_above = int(tx_above) - int(rx_above)
            if tgen_loss_above < 0:
                tgen_loss_above = 0
        drop_above = qc_after['droppacket'] - qc_before['droppacket']

        verdict = "PASS" if (drop_below == 0 and tgen_loss_below == 0 and drop_above > 0) else "FAIL"
        if verdict == "FAIL":
            all_pass = False

        results.append({
            'frame_size': frame_size, 'max_l2_gbps': max_l2_gbps,
            'below_gbps': below_gbps, 'above_gbps': above_gbps,
            'tx_gbps_below': tx_gbps_below, 'rx_gbps_below': rx_gbps_below,
            'tx_gbps_above': tx_gbps_above, 'rx_gbps_above': rx_gbps_above,
            'tx_below': tx_below, 'rx_below': rx_below,
            'tgen_loss_below': tgen_loss_below, 'drop_below': drop_below,
            'tx_above': tx_above, 'rx_above': rx_above,
            'tgen_loss_above': tgen_loss_above, 'drop_above': drop_above,
            'verdict': verdict,
        })

    # --- Print live rates table ---
    rlog(f"  {'Frame':<6} {'Target':>8} {'TxGbps':>8} {'RxGbps':>8}  {'Target':>8} {'TxGbps':>8} {'RxGbps':>8}")
    rlog(f"  {'':.<6} {'--Below-':>8} {'--------':>8} {'--------':>8}  {'--Above-':>8} {'--------':>8} {'--------':>8}")
    for r in results:
        rlog(f"  {r['frame_size']:<6} {r['below_gbps']:>7.1f}G {r['tx_gbps_below']:>7.2f}G {r['rx_gbps_below']:>7.2f}G  "
             f"{r['above_gbps']:>7.1f}G {r['tx_gbps_above']:>7.2f}G {r['rx_gbps_above']:>7.2f}G")
    rlog('')

    # --- Print summary table ---
    rlog(f"  {'Frame':<6} {'MaxL2':>8} {'Below':>8} {'Above':>8} "
         f"{'TX@Below':>10} {'RX@Below':>10} {'TgenLoss':>11} {'DUTDrop':>11} "
         f"{'TX@Above':>10} {'RX@Above':>10} {'TgenLoss':>11} {'DUTDrop':>11} {'Verdict'}")
    rlog(f"  {'-'*6} {'-'*8} {'-'*8} {'-'*8} "
         f"{'-'*10} {'-'*10} {'-'*11} {'-'*11} "
         f"{'-'*10} {'-'*10} {'-'*11} {'-'*11} {'-'*7}")
    for r in results:
        rlog(f"  {r['frame_size']:<6} {r['max_l2_gbps']:>7.2f}G {r['below_gbps']:>7.1f}G {r['above_gbps']:>7.1f}G "
             f"{r['tx_below']:>10} {r['rx_below']:>10} {r['tgen_loss_below']:>11,} {r['drop_below']:>11,} "
             f"{r['tx_above']:>10} {r['rx_above']:>10} {r['tgen_loss_above']:>11,} {r['drop_above']:>11,} {r['verdict']}")

    rlog('')
    rlog(f"Tgen type: {stream_api.tgen_handle.tg_type}")
    rlog(f"Conclusion: {'L1 overhead theory CONFIRMED' if all_pass else 'UNEXPECTED — tgen may account for L1 internally'}")

    stream_api.delete_traffic_stream(stream)

    if all_pass:
        st.report_pass('msg',
            f'L1 overhead confirmed on {stream_api.tgen_handle.tg_type}: '
            f'max_l2 = speed * frame/(frame+20)')
    else:
        st.report_fail('msg',
            f'L1 overhead NOT confirmed on {stream_api.tgen_handle.tg_type} — '
            f'tgen may handle rate differently')


def test_l1_overhead_fanin():
    """
    2:1 fan-in test: 2 ingress ports -> DUT -> 1 egress port (all same speed).
    Two streams (T1<node>P1 and T1<node>P2) both target T1<node>P3.
    Ramp each stream from (max_l2/2 - 5G) upward until aggregate exceeds
    the egress L1 line rate and drops appear.

    Proves L1 overhead even when all ports are the same speed (e.g., 400G).
    Sweeps all frame sizes from input json2.
    """
    st.banner(f"Test: L1 overhead fan-in (2:1, {node}, "
              f"T1{node}P1+T1{node}P2 -> T1{node}P3)")

    # Need at least 3 tgen ports for fan-in
    port_cnt = stream_api.get_tgen_port_count(node)
    if port_cnt < 3:
        st.report_unsupported('msg',
            f'Fan-in test requires 3 tgen ports, only {port_cnt} available on {node}')

    with open(result_file, 'a') as f:
        f.write(f"\n\nL1 Overhead Fan-in Test — {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write('=' * 60 + '\n\n')

    dut = getattr(tb_dict, node)
    egress_intf = tb_dict[f'{node}T1P3']
    egress_speed = qos_test_utils.get_if_speed(dut, egress_intf)
    tc = 0
    step = 1.0

    rlog(f"Fan-in: egress={egress_intf} ({egress_speed}G)")
    rlog(f"  Frame sizes: {frame_sizes}")
    rlog(f"  Tgen type: {stream_api.tgen_handle.tg_type}")
    rlog('')

    # Create 2 streams with first frame size (will modify per iteration)
    first_frame = frame_sizes[0]
    max_l2_first = egress_speed * first_frame / (first_frame + L1_OVERHEAD)
    pps_init = stream_api.gbps_to_pps(round(max_l2_first / 2 - 5.0, 2), first_frame)

    stream1 = stream_api.create_traffic_stream(
        tb_dict, f'T1{node}P1', f'T1{node}P3', first_frame, pps_init, tc)
    if stream1 is None:
        st.report_fail('msg', 'Failed to create stream1 (P1->P3)')

    stream2 = stream_api.create_traffic_stream(
        tb_dict, f'T1{node}P2', f'T1{node}P3', first_frame, pps_init, tc)
    if stream2 is None:
        st.report_fail('msg', 'Failed to create stream2 (P2->P3)')

    all_pass = True

    for frame_size in frame_sizes:
        max_l2_gbps = egress_speed * frame_size / (frame_size + L1_OVERHEAD)
        per_stream_start = round(max_l2_gbps / 2 - 5.0, 2)
        per_stream_end = round(max_l2_gbps / 2 + 3.0, 2)

        st.banner(f"Fan-in: frame={frame_size}B, max_l2={max_l2_gbps:.2f}G, "
                  f"each stream {per_stream_start}G..{per_stream_end}G")

        results = []
        rate = per_stream_start

        while rate <= per_stream_end + 0.01:
            aggregate = 2 * rate
            pps = stream_api.gbps_to_pps(rate, frame_size)

            # Modify both streams to current rate and frame size
            stream_api.tgen_handle.tg_traffic_config(
                mode='modify', stream_id=stream1['stream_id'],
                rate_pps=pps, frame_size=frame_size,
                length_mode='fixed', high_speed_result_analysis=1)
            stream_api.tgen_handle.tg_traffic_config(
                mode='modify', stream_id=stream2['stream_id'],
                rate_pps=pps, frame_size=frame_size,
                length_mode='fixed', high_speed_result_analysis=1)

            # Run traffic
            qc_before = qos_test_utils.get_tc_queue_counters_json(dut, egress_intf, tc)
            stream_api.clear_all_stats()
            stream_api.start_traffic_stream(None)  # start all
            st.wait(30)

            # Live stats
            live = stream_api.collect_traffic_stream_stats()
            tx_gbps1 = tx_gbps2 = rx_gbps1 = rx_gbps2 = 0.0
            if 'traffic_item' in live:
                if stream1['stream_id'] in live['traffic_item']:
                    s = live['traffic_item'][stream1['stream_id']]
                    tx_gbps1 = float(s.get('tx', {}).get('total_pkt_bit_rate', 0)) / 1e9
                    rx_gbps1 = float(s.get('rx', {}).get('total_pkt_bit_rate', 0)) / 1e9
                if stream2['stream_id'] in live['traffic_item']:
                    s = live['traffic_item'][stream2['stream_id']]
                    tx_gbps2 = float(s.get('tx', {}).get('total_pkt_bit_rate', 0)) / 1e9
                    rx_gbps2 = float(s.get('rx', {}).get('total_pkt_bit_rate', 0)) / 1e9

            st.wait(30)
            stream_api.stop_traffic_stream(None)  # stop all
            st.wait(5)

            # Final stats
            stats = stream_api.collect_traffic_stream_stats()
            qc_after = qos_test_utils.get_tc_queue_counters_json(dut, egress_intf, tc)

            tx_total = rx_total = 0
            for sid in [stream1['stream_id'], stream2['stream_id']]:
                if 'traffic_item' in stats and sid in stats['traffic_item']:
                    s = stats['traffic_item'][sid]
                    tx_total += int(s.get('tx', {}).get('total_pkts', '0'))
                    rx_total += int(s.get('rx', {}).get('total_pkts', '0'))

            tgen_loss = max(0, tx_total - rx_total)
            dut_drop = qc_after['droppacket'] - qc_before['droppacket']

            results.append({
                'rate': rate, 'aggregate': aggregate,
                'tx_gbps1': tx_gbps1, 'rx_gbps1': rx_gbps1,
                'tx_gbps2': tx_gbps2, 'rx_gbps2': rx_gbps2,
                'tx_total': tx_total, 'rx_total': rx_total,
                'tgen_loss': tgen_loss, 'dut_drop': dut_drop,
            })

            rate = round(rate + step, 2)

        # --- Print consolidated table for this frame size ---
        rlog(f"  Frame={frame_size}B, Theoretical max L2 = {max_l2_gbps:.2f}G (egress={egress_speed}G)")
        rlog(f"    {'TxRate1':>8} {'TxRate2':>8} {'RxRate1':>8} {'RxRate2':>8} "
             f"{'Aggr':>7} {'TgenLoss':>11} {'DUTDrop':>11} {'Verdict'}")
        rlog(f"    {'-'*8} {'-'*8} {'-'*8} {'-'*8} "
             f"{'-'*7} {'-'*11} {'-'*11} {'-'*7}")
        found_crossover = False
        for r in results:
            expect_drop = r['aggregate'] > max_l2_gbps
            if expect_drop:
                verdict = "PASS" if r['dut_drop'] > 0 else "FAIL"
            else:
                verdict = "PASS" if (r['dut_drop'] == 0 and r['tgen_loss'] == 0) else "FAIL"
            if verdict == "FAIL":
                all_pass = False
            if not found_crossover and r['dut_drop'] > 0:
                found_crossover = True
            rlog(f"    {r['tx_gbps1']:>7.2f}G {r['tx_gbps2']:>7.2f}G "
                 f"{r['rx_gbps1']:>7.2f}G {r['rx_gbps2']:>7.2f}G "
                 f"{r['aggregate']:>6.1f}G "
                 f"{r['tgen_loss']:>11,} {r['dut_drop']:>11,} {verdict}")
        rlog('')

        if not found_crossover:
            rlog(f"    WARNING: No crossover found for frame={frame_size}B")
            all_pass = False

    rlog(f"Tgen type: {stream_api.tgen_handle.tg_type}")
    rlog(f"Formula: max_l2 = speed * frame / (frame + 20)")

    stream_api.delete_traffic_stream(stream1)
    stream_api.delete_traffic_stream(stream2)

    if all_pass:
        st.report_pass('msg',
            f'Fan-in L1 overhead confirmed for all frame sizes on {stream_api.tgen_handle.tg_type}')
    else:
        st.report_fail('msg',
            f'Fan-in L1 overhead NOT fully confirmed on {stream_api.tgen_handle.tg_type}')
