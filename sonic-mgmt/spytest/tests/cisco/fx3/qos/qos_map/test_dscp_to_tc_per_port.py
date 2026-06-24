#!/usr/bin/env python3
# BEGIN_LEGAL
#
# Copyright (c) 2026-current, Cisco Systems, Inc. ("Cisco"). All Rights Reserved.
#
# This file and all technical concepts, proprietary knowledge, algorithms and
# intellectual property rights it contains (collectively the "Confidential Information"),
# are the sole propriety information of Cisco and shall remain at Cisco's ownership.
# You shall not disclose the Confidential Information to any third party and you
# shall use it solely in connection with operating and/or maintaining of Cisco's
# products and pursuant to the terms and conditions of the license agreement you
# entered into with Cisco.
#
# THE SOURCE CODE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED.
# IN NO EVENT SHALL CISCO BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN
# AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH
# THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#
# END_LEGAL

"""
DSCP-to-TC Per-Port Isolation Tests (quarantined)

Coverage for behaviour that requires per-port DSCP-to-TC classification:
binding two different maps to two ingress ports and asserting they take
effect independently in ASIC_DB, TCAM, and the data plane.

This file is dormant.  The module-level pytest.skip(..., allow_module_level=True)
below skips the whole file because the underlying SAI support landed in
cisco-nx-sai PRs #494 + #514 and was subsequently reverted.
DSCP-to-TC classification is currently global (the L3QOS TCAM key has no
port discriminator), so binding two maps to two ports does not isolate
their effect.  The SAI_PORT_ATTR_QOS_DSCP_TO_TC_MAP bind/unbind path
still functions as bookkeeping; coverage for that path lives in
test_dscp_to_tc.py (Test 28, F2, F3, F5).

Quarantined here:
  F6 — Custom (non-AZURE) map bound alongside the default AZURE on other
       ports.  Requires two maps to coexist with isolated effect.
  G1 — Two distinct maps → two distinct SAI_QOS_MAP OIDs + per-port bindings.
  G2 — Two distinct maps → distinct TCAM region (label) allocation.
  G3 — Same DSCP from two ports under two maps lands on two queues.
  G4 — Rebinding one port does not disturb the other (state + data plane).
  G5 — Unbound port falls through to default TC0.
  G6 — Per-port classification survives an admin-down/up cycle.

To re-enable: remove the module-level ``pytest.skip(allow_module_level=True)``
block in the imports section once per-port DSCP-to-TC classification is
restored in SAI.  For local bring-up without editing the file, set the env
var ``FX3_QOS_RUN_PER_PORT=1`` to bypass the module-level skip.
"""

import json
import os
import re
import warnings

import pytest

warnings.filterwarnings(
    "ignore", r".*currentThread\(\) is deprecated.*", DeprecationWarning)
warnings.filterwarnings(
    "ignore", r".*ssl\.PROTOCOL_TLS is deprecated.*", DeprecationWarning)

# ─── Module-level skip: file is dormant pending SAI restoration ───────
# The module-level skip below is the single quarantine gate.  It makes
# pytest report one skip for this file instead of one skip per test while
# the whole suite is dormant.
#
# When SAI #494 + #514 land again, remove this `pytest.skip(...)` block.
#
# The opt-out env var `FX3_QOS_RUN_PER_PORT=1` lets a developer run or
# collect the file for SAI bring-up without editing source.
if not os.environ.get("FX3_QOS_RUN_PER_PORT"):
    pytest.skip(
        "Per-port DSCP-to-TC isolation tests are dormant: cisco-nx-sai "
        "PRs #494 + #514 (the L3QOS per-port TCAM discriminator) were "
        "reverted, so per-port classification cannot pass. Re-enable by "
        "removing this module-level pytest.skip(...) block once the SAI "
        "support is restored.",
        allow_module_level=True,
    )

from spytest import st, tgapi                       # noqa: E402

from qos_helpers import (                            # noqa: E402
    GOLDEN_DSCP_TO_TC,
    IXIA_INGRESS_A_IP, IXIA_INGRESS_A_IP6,
    IXIA_INGRESS_B_IP, IXIA_INGRESS_B_IP6,
    IXIA_EGRESS_IP,    IXIA_EGRESS_IP6,
    print_section,
    reload_qos,
    get_port_dscp_tc_map,
    asic_dscp_to_tc_map_oids,
    per_port_dscp_to_tc_oid,
    has_per_port_binding,
    deploy_dchal_helper,
    dchal_tcam_info,
    get_dchal_queue_counters,
    get_dut_mac,
    setup_topo_common,
)

# ─── Constants (subset needed by F6 + G1–G6) ─────────────────────────────────
_TCAM_START_IDX  = 1792
_TCAM_DUMP_COUNT = 256

_IXIA_DST_V4  = IXIA_EGRESS_IP
_IXIA_DST_V6  = IXIA_EGRESS_IP6

_PKT_SIZE        = 128
_PKTS_PER_DSCP   = 250
_STREAM_RATE_PPS = 50
_TRAFFIC_TIMEOUT = 20

_EXPECTED_Q_PKTS = {}
for _ds, _tc in GOLDEN_DSCP_TO_TC.items():
    _qi = int(_tc)
    _EXPECTED_Q_PKTS[_qi] = _EXPECTED_Q_PKTS.get(_qi, 0) + _PKTS_PER_DSCP


# ─── Module-level state (mirrors test_dscp_to_tc.py / overlay pattern) ───────
dut         = None
test_intf   = None
test_intf2  = None
tg          = None
tg_ph       = {}
port_info   = {}
port_speeds = {}
topo_mode           = None
tg_ph_ingress_b     = None
port_info_ingress_b = None


@pytest.fixture(scope="module", autouse=True)
def setup_topo():
    """Shared topology setup for the quarantined per-port suite.

    Duplicates the test_dscp_to_tc.py fixture rather than sharing through
    a helper module: test_dscp_to_tc_overlay.py uses the same duplication
    pattern, and keeping this file self-contained avoids coupling a
    dormant suite to the actively-evolving main file.
    """
    global dut, tg, tg_ph, port_info, port_speeds
    global topo_mode, tg_ph_ingress_b, port_info_ingress_b
    global test_intf, test_intf2

    for result in setup_topo_common(tgapi, target_queue=0):
        dut       = result['dut']
        tg        = result['tg']
        topo_mode = result['mode']

        raw_ph = result['tg_ph']
        raw_pi = result['port_info']

        port_info = {
            'ingress': raw_pi['ingress_a'],
            'egress':  raw_pi['egress'],
        }
        tg_ph = {
            'ingress': raw_ph['ingress_a'],
            'egress':  raw_ph.get('egress_sink', raw_ph['egress']),
        }
        port_speeds = {
            'ingress': result['port_speeds'].get('ingress_a', 'N/A'),
            'egress':  result['port_speeds'].get('egress', 'N/A'),
        }

        if 'ingress_b' in raw_pi:
            tg_ph_ingress_b     = raw_ph.get('ingress_b')
            port_info_ingress_b = raw_pi['ingress_b']
        else:
            tg_ph_ingress_b     = None
            port_info_ingress_b = None

        test_intf  = raw_pi['ingress_a']
        test_intf2 = raw_pi.get('ingress_b')

        deploy_dchal_helper(dut)
        yield


# ─── Internal helpers ────────────────────────────────────────────────────────

def _compute_deltas(q_before, q_after):
    """Return {qi: {'pkts': delta, 'drop_pkts': delta}} for all 8 queues."""
    deltas = {}
    for qi in range(8):
        b = q_before.get(qi, {})
        a = q_after.get(qi, {})
        deltas[qi] = {
            'pkts':      max(0, a.get('pkts',      0) - b.get('pkts',      0)),
            'drop_pkts': max(0, a.get('drop_pkts', 0) - b.get('drop_pkts', 0)),
        }
    return deltas


def _log_queue_placement_table(deltas, label="", expected=None):
    """Print per-queue results table: expected vs actual packet counts."""
    exp_map = _EXPECTED_Q_PKTS if expected is None else expected
    noise = max(int(max(exp_map.values()) * 0.05), 1) if expected is not None else 0
    hdr = "  {:<6} {:>12}  {:>12}  {:>12}  {:>10}  {:>6}".format(
        "Queue", "Expected", "Actual", "Drop", "Status", "Delta%")
    st.log("")
    st.log("  DSCP Queue-Placement Results {}".format(label))
    st.log("  " + "-" * 75)
    st.log(hdr)
    st.log("  " + "-" * 75)
    for qi in range(8):
        exp  = exp_map.get(qi, 0)
        act  = deltas[qi]['pkts']
        drp  = deltas[qi]['drop_pkts']
        if exp == 0:
            if expected is None:
                status = "N/A"
                dpct   = "N/A"
            else:
                status = "PASS" if act <= noise else "FAIL"
                dpct   = "—"
        else:
            lo = int(exp * 0.85)
            hi = int(exp * 1.15)
            status = "PASS" if lo <= act <= hi else "FAIL"
            dpct   = "{:+.1f}%".format((act - exp) / float(exp) * 100)
        st.log("  Q{:<5} {:>12,}  {:>12,}  {:>12,}  {:>10}  {:>6}".format(
            qi, exp, act, drp, status, dpct))
    st.log("  " + "-" * 75)
    st.log("")


def _g_expect_single_q(q, pkts):
    """Expectation map for a single-DSCP burst landing on queue `q`."""
    return {qi: (pkts if qi == q else 0) for qi in range(8)}


def _g_send_dscp_burst(ph, src_ip, dst_ip, dut_ingress_port, label,
                       dscp, pkts=250, rate=50, egress_intf=None):
    """Send *pkts* at *dscp* from IXIA port *ph*; return egress queue deltas."""
    if egress_intf is None:
        egress_intf = port_info['egress']
    dst_mac = get_dut_mac(dut, dut_ingress_port)
    tg.tg_traffic_control(action='reset')
    tg.tg_traffic_config(
        mode='create',
        port_handle=ph,
        l3_protocol='ipv4',
        l4_protocol='udp',
        ip_src_addr=src_ip,
        ip_dst_addr=dst_ip,
        mac_dst=dst_mac,
        ip_dscp=dscp,
        ip_ttl=64,
        udp_src_port=10000,
        udp_dst_port=5000,
        frame_size=_PKT_SIZE,
        rate_pps=rate,
        pkts_per_burst=pkts,
        transmit_mode='single_burst',
        high_speed_result_analysis=0,
    )
    q_before = get_dchal_queue_counters(dut, egress_intf,
                                        "BEFORE {}".format(label))
    tg.tg_traffic_control(action='clear_stats')
    tg.tg_traffic_control(action='apply')
    tg.tg_traffic_control(action='run')
    st.wait(int(pkts / float(rate)) + 5)
    tg.tg_traffic_control(action='stop')
    st.wait(2)
    q_after = get_dchal_queue_counters(dut, egress_intf,
                                       "AFTER {}".format(label))
    return _compute_deltas(q_before, q_after)


def _g_setup_azure_plus_custom():
    """Bind AZURE on test_intf and a fresh CUSTOM_GB (all DSCP→7) on
    test_intf2.  Returns (map_a, map_b); caller must call
    _g_teardown_azure_plus_custom() to restore baseline.

    FX3 TCAM constraint: the ing-l3-vlan-qos region holds 512 entries.
    AZURE alone uses 192 (64 IPv4 + 64 IPv6 + 64 IPv6 wide-key sibling)
    and is bound to all default ports.  Each additional 64-entry custom
    map costs another 192.  Two new custom maps would push the region
    to 576 (>512); syncd's bind_to_port allocator silently fails the
    second bind without propagating the error to orchagent, leaving
    SAI_PORT_ATTR_QOS_DSCP_TO_TC_MAP=oid:0x0 on the affected port and
    breaking G1/G2/G3/G6.  Using AZURE on intf1 (already programmed)
    plus one new custom map on intf2 keeps usage at 384, well under
    the 512 budget, while still exercising distinct OIDs / per-port
    label isolation on the two ports.
    """
    map_a = 'AZURE'
    map_b = 'CUSTOM_GB'
    for d in range(64):
        st.config(dut,
            'sonic-db-cli CONFIG_DB HSET "DSCP_TO_TC_MAP|{}" "{}" "{}"'.format(
                map_b, d, 7),
            skip_error_check=False)
    # intf1 keeps its system-default AZURE binding from the prior reload.
    # Touching it (HDEL+HSET AZURE) trips qosorch's value-equality dedupe:
    # the HDEL pushes oid:0x0 to SAI, but the follow-up HSET AZURE is
    # skipped because orchagent's cached field value still equals AZURE,
    # leaving the port at SAI port-attr=oid:0x0 (no per-port binding).
    # Only intf2 needs the unbind+bind cycle to flip AZURE -> CUSTOM_GB,
    # which is a real value transition orchagent always programs.
    st.config(dut,
        'sonic-db-cli CONFIG_DB HDEL "PORT_QOS_MAP|{}" "dscp_to_tc_map"'.format(test_intf2),
        skip_error_check=True)
    st.wait(3)
    st.config(dut,
        'sonic-db-cli CONFIG_DB HSET "PORT_QOS_MAP|{}" "dscp_to_tc_map" "{}"'.format(
            test_intf2, map_b),
        skip_error_check=False)
    st.wait(10)
    return map_a, map_b


def _g_teardown_azure_plus_custom(map_a, map_b):
    """Unbind, delete CUSTOM_GB, and restore the AZURE baseline."""
    for intf in (test_intf, test_intf2):
        st.config(dut,
            'sonic-db-cli CONFIG_DB HDEL "PORT_QOS_MAP|{}" "dscp_to_tc_map"'.format(intf),
            skip_error_check=True)
    st.config(dut,
        'sonic-db-cli CONFIG_DB DEL "DSCP_TO_TC_MAP|{}"'.format(map_b),
        skip_error_check=True)
    reload_qos(dut, wait=15)


# ══════════════════════════════════════════════════════════════════════════════
# F6 — Custom (non-AZURE) map alongside the default AZURE
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.config_only
def test_custom_map_tcam_vs_azure():
    """#F6 — New custom DSCP-to-TC map applied to an interface; ASIC_DB and
    TCAM reflect the custom TC values.

    Creates a brand-new 64-entry map CUSTOM_64 (identical to AZURE except for
    4 swapped DSCP→TC entries) and binds it to test_intf via PORT_QOS_MAP.
    The test then verifies:
      a) A new SAI_QOS_MAP OID is created in ASIC_DB for CUSTOM_64.
      b) The port has per-port binding state in ASIC_DB
         (SAI_PORT_ATTR_QOS_DSCP_TO_TC_MAP non-default).
      c) TCAM usage changes (a new TCAM region is allocated for the custom
         map's distinct TC assignments).
      d) The 4 swapped DSCP entries in ASIC_DB reflect the custom TC values,
         not the AZURE TC values.
      e) One non-swapped DSCP (DSCP 16) still carries its AZURE TC value.

    Custom swaps in CUSTOM_64 vs AZURE:
      DSCP  1  → TC 7  (AZURE: TC 1)
      DSCP 46  → TC 0  (AZURE: TC 5)
      DSCP 48  → TC 1  (AZURE: TC 6)
      DSCP 49  → TC 0  (AZURE: TC 7)

    Steps:
      0. Clean AZURE baseline via 'config qos reload'; back up
         /etc/sonic/config_db.json for safe restore at teardown.
      1. Baseline: snapshot ASIC_DB QOS_MAP OID count and TCAM usage.
      2. Write all 64 entries of CUSTOM_64 to CONFIG_DB; bind to test_intf
         via PORT_QOS_MAP; persist with 'config save -y'.
      3. Wait for orchagent to process CONFIG_DB notifications (no reload —
         orchagent subscribes directly and 'config qos reload' would restore
         AZURE from the .j2 template, destroying CUSTOM_64).
      4. Verify a NEW SAI_QOS_MAP OID appeared in ASIC_DB for CUSTOM_64.
      5. Verify SAI_PORT_ATTR_QOS_DSCP_TO_TC_MAP on test_intf points to the
         new OID (not oid:0x0 / global default).
      6. Verify TCAM used count increased vs baseline (a new map with distinct
         TC assignments must allocate a new TCAM region; delta=0 is a failure).
      7. Verify the 4 swapped DSCP→TC values in the CUSTOM_64 OID.
      8. Verify DSCP 16 (unchanged) has the AZURE TC in the CUSTOM_64 OID.
      9. Cleanup: HDEL PORT_QOS_MAP binding, DEL CUSTOM_64; restore backed-up
         config_db.json; run 'config qos reload' to restore AZURE everywhere.
    """
    print_section("F6 — New CUSTOM_64 map on interface: ASIC_DB + TCAM verification",
                  art_key='dscp_to_tc')

    _CUSTOM_MAP = 'CUSTOM_64'
    _SWAPS      = {1: 7, 46: 0, 48: 1, 49: 0}
    _SPOT_DSCP  = 16
    _BACKUP     = '/tmp/config_db_f6_backup.json'
    _WAIT       = 10
    _RETRIES    = 9
    failures    = []

    st.log("  Step 0: clean AZURE baseline via 'config qos reload'...")
    reload_qos(dut, wait=10)
    st.config(dut,
        'cp /etc/sonic/config_db.json {}'.format(_BACKUP),
        skip_error_check=True)
    st.log("  Backed up config_db.json → {}".format(_BACKUP))

    def _list_qos_map_oids():
        raw = st.show(dut,
            'sonic-db-cli ASIC_DB KEYS "ASIC_STATE:SAI_OBJECT_TYPE_QOS_MAP:*"',
            skip_tmpl=True)
        return [l.strip() for l in (raw or '').splitlines()
                if re.match(r'^ASIC_STATE:SAI_OBJECT_TYPE_QOS_MAP:oid:', l.strip())]

    oids_before = _list_qos_map_oids()
    st.log("  Baseline ASIC_DB QOS_MAP OIDs: {}  ({})".format(
        len(oids_before), oids_before))

    tcam_before = dchal_tcam_info(dut)
    used_before = tcam_before.get('used', -1)
    st.log("  Baseline TCAM used = {}".format(used_before))

    custom_entries = dict(GOLDEN_DSCP_TO_TC)
    for dscp, tc in _SWAPS.items():
        custom_entries[str(dscp)] = str(tc)

    st.log("  Writing DSCP_TO_TC_MAP|{} ({} entries) to CONFIG_DB...".format(
        _CUSTOM_MAP, len(custom_entries)))
    for dscp_str, tc_str in custom_entries.items():
        st.config(dut,
            'sonic-db-cli CONFIG_DB HSET "DSCP_TO_TC_MAP|{}" "{}" "{}"'.format(
                _CUSTOM_MAP, dscp_str, tc_str),
            skip_error_check=True)

    st.log("  Binding {} to {} via PORT_QOS_MAP...".format(_CUSTOM_MAP, test_intf))
    st.config(dut,
        'sonic-db-cli CONFIG_DB HDEL "PORT_QOS_MAP|{}" "dscp_to_tc_map"'.format(test_intf),
        skip_error_check=True)
    st.wait(3)
    st.config(dut,
        'sonic-db-cli CONFIG_DB HSET "PORT_QOS_MAP|{}" "dscp_to_tc_map" "{}"'.format(
            test_intf, _CUSTOM_MAP),
        skip_error_check=True)
    st.wait(1)
    st.config(dut, 'config save -y', skip_error_check=True)
    st.log("  Saved CONFIG_DB to disk via 'config save -y'")

    st.log("  Waiting {}s for orchagent to process notifications...".format(_WAIT))
    st.wait(_WAIT)

    oids_after = []
    new_oid    = None
    for _attempt in range(_RETRIES):
        oids_after = _list_qos_map_oids()
        new_oids   = [o for o in oids_after if o not in oids_before]
        if new_oids:
            new_oid = new_oids[0]
            break
        if _attempt < _RETRIES - 1:
            st.log("  No new QOS_MAP OID yet ({}/{}) — waiting {}s...".format(
                _attempt + 1, _RETRIES, _WAIT))
            st.wait(_WAIT)

    st.log("  ASIC_DB QOS_MAP OIDs after binding: {} (was {})".format(
        len(oids_after), len(oids_before)))
    if new_oid:
        st.log("  New CUSTOM_64 OID created: {}  PASS".format(new_oid))
    else:
        msg = ("No new SAI_QOS_MAP OID created for CUSTOM_64 — "
               "orchagent did not honour PORT_QOS_MAP HSET (per-port map "
               "binding broken)")
        failures.append(msg)
        st.log("  FAIL: {}".format(msg))

    port_map_oid = per_port_dscp_to_tc_oid(dut, test_intf)
    st.log("  Per-port binding on {}: qos_map={}".format(
        test_intf, port_map_oid or '(nil)'))
    if not has_per_port_binding(port_map_oid):
        msg = ("No per-port binding in ASIC_DB after binding {} to {}: "
               "SAI_PORT_ATTR_QOS_DSCP_TO_TC_MAP={}".format(
                   _CUSTOM_MAP, test_intf, port_map_oid))
        failures.append(msg)
        st.log("  FAIL: {}".format(msg))
    else:
        st.log("  Port has per-port binding  PASS")
        if (port_map_oid and port_map_oid != 'oid:0x0'
                and new_oid and port_map_oid not in new_oid):
            st.log("  WARN: port qos_map OID {} differs from new OID {}".format(
                port_map_oid, new_oid))

    tcam_after  = dchal_tcam_info(dut)
    used_after  = tcam_after.get('used', -1)
    tcam_delta  = used_after - used_before
    st.log("  TCAM used: before={} after={} delta={}".format(
        used_before, used_after, tcam_delta))
    if tcam_delta > 0:
        st.log("  TCAM usage increased by {} — new region allocated  PASS".format(
            tcam_delta))
    elif tcam_delta == 0:
        msg = ("TCAM usage UNCHANGED after CUSTOM_64 map creation (delta=0). "
               "A new map with distinct DSCP→TC mappings must allocate a new "
               "TCAM region; delta=0 indicates the custom map was not "
               "programmed into hardware.")
        failures.append(msg)
        st.log("  FAIL: {}".format(msg))
    else:
        msg = ("TCAM usage DECREASED by {} after CUSTOM_64 map creation — "
               "unexpected; entries may have been removed instead of added.".format(
               abs(tcam_delta)))
        failures.append(msg)
        st.log("  FAIL: {}".format(msg))

    if new_oid:
        raw_map = st.show(dut,
            'sonic-db-cli ASIC_DB HGET "{}" "SAI_QOS_MAP_ATTR_MAP_TO_VALUE_LIST"'.format(
                new_oid),
            skip_tmpl=True)
        custom_asic = {}
        for line in (raw_map or '').splitlines():
            line = line.strip()
            if not line or (line.endswith('$') and '@' in line):
                continue
            try:
                data = json.loads(line)
                custom_asic = {e['key']['dscp']: e['value']['tc']
                               for e in data.get('list', [])}
            except (ValueError, KeyError):
                pass
            break

        st.log("  CUSTOM_64 OID has {} ASIC entries".format(len(custom_asic)))

        st.log("  Verifying swapped DSCP→TC values in CUSTOM_64 OID:")
        for dscp, custom_tc in sorted(_SWAPS.items()):
            azure_tc = int(GOLDEN_DSCP_TO_TC.get(str(dscp), -1))
            got_tc   = custom_asic.get(dscp)
            if got_tc is None:
                failures.append("CUSTOM_64 OID missing DSCP {}".format(dscp))
                st.log("    DSCP {:2d}: MISSING  FAIL".format(dscp))
                continue
            ok = (got_tc == custom_tc)
            st.log("    DSCP {:2d}: azure={} custom={} got={}  {}".format(
                dscp, azure_tc, custom_tc, got_tc, 'PASS' if ok else 'FAIL'))
            if not ok:
                failures.append(
                    "DSCP {} → TC {} (expected custom={}, AZURE={})".format(
                        dscp, got_tc, custom_tc, azure_tc))

        exp_spot = int(GOLDEN_DSCP_TO_TC.get(str(_SPOT_DSCP), -1))
        got_spot = custom_asic.get(_SPOT_DSCP, -1)
        ok_spot  = (got_spot == exp_spot)
        st.log("  DSCP {} (unchanged): got={} expected={}  {}".format(
            _SPOT_DSCP, got_spot, exp_spot, 'PASS' if ok_spot else 'FAIL'))
        if not ok_spot:
            failures.append(
                "DSCP {} (unchanged) → TC {} (expected AZURE {})".format(
                    _SPOT_DSCP, got_spot, exp_spot))
    else:
        st.log("  Skipping OID content check — no new OID was created")

    st.config(dut,
        'sonic-db-cli CONFIG_DB HDEL "PORT_QOS_MAP|{}" "dscp_to_tc_map"'.format(test_intf),
        skip_error_check=True)
    st.config(dut,
        'sonic-db-cli CONFIG_DB DEL "DSCP_TO_TC_MAP|{}"'.format(_CUSTOM_MAP),
        skip_error_check=True)
    st.config(dut,
        'cp {} /etc/sonic/config_db.json'.format(_BACKUP),
        skip_error_check=True)
    st.log("  Restored config_db.json from backup")
    reload_qos(dut, wait=30)

    if failures:
        st.report_fail('msg', "F6 failures:\n  " + "\n  ".join(failures))

    tcam_note = "TCAM +{}".format(tcam_delta)
    st.report_pass('msg',
        "F6: CUSTOM_64 ({} swaps) created OID={}, port bound OID={}, "
        "{}; AZURE restored".format(
            len(_SWAPS),
            new_oid.split(':')[-1] if new_oid else 'none',
            port_map_oid,
            tcam_note))


# ══════════════════════════════════════════════════════════════════════════════
# Section G — Per-Port DSCP-to-TC Isolation Tests (G1–G6)
#
# Regression coverage for cisco-nx-sai PRs #494 (per-port PORT_LAG_LABEL on
# PQOS entries) and #514 (acl_bind_to_interface() wrapper).  Bind two
# different DSCP_TO_TC maps to two ingress ports and verify isolation at
# CONFIG_DB, ASIC_DB, TCAM, and traffic level.  G5 covers the unbound-port
# default-TC fall-through; G6 covers per-port label persistence across a
# port flap.  Skipped in breakout mode (only one ingress port available).
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.config_only
def test_g1_distinct_maps_distinct_asic_oids():
    """#G1 — AZURE on intf1 and a fresh custom map on intf2 produce two
    distinct SAI_QOS_MAP OIDs in ASIC_DB, and both ports show per-port
    binding state (qos_map OID non-default).
    """
    print_section("G1 — AZURE on intf1 + custom map on intf2 → distinct ASIC OIDs",
                  art_key='dscp_to_tc')

    if test_intf2 is None:
        st.report_skip('msg',
            "G1 requires a second ingress port (ixia/peer_link mode); "
            "current topology mode '{}' has only one ingress.".format(topo_mode))

    failures = []

    oids_before = set(asic_dscp_to_tc_map_oids(dut))
    st.log("  Baseline DSCP_TO_TC OIDs: {} ({})".format(
        len(oids_before), sorted(oids_before)))

    map_a, map_b = _g_setup_azure_plus_custom()
    try:
        oids_after = set(asic_dscp_to_tc_map_oids(dut))
        new_oids   = oids_after - oids_before
        st.log("  After bind: {} DSCP_TO_TC OIDs total ({} new)".format(
            len(oids_after), len(new_oids)))
        st.log("  New OIDs: {}".format(sorted(new_oids)))

        if len(new_oids) < 1:
            failures.append(
                "Expected ≥1 new DSCP_TO_TC OID after binding {} on a "
                "second port (alongside {} on the first), got {} new OIDs "
                "(total {})".format(map_b, map_a, len(new_oids), len(oids_after)))
        else:
            st.log("  ≥1 new DSCP_TO_TC OID created for {}  PASS".format(map_b))

        snap1 = per_port_dscp_to_tc_oid(dut, test_intf)
        snap2 = per_port_dscp_to_tc_oid(dut, test_intf2)
        st.log("  intf1={} binding: qos_map={}".format(
            test_intf, snap1 or '(nil)'))
        st.log("  intf2={} binding: qos_map={}".format(
            test_intf2, snap2 or '(nil)'))
        if not has_per_port_binding(snap1):
            failures.append("intf1={} has no per-port binding".format(test_intf))
        if not has_per_port_binding(snap2):
            failures.append("intf2={} has no per-port binding".format(test_intf2))
        if (has_per_port_binding(snap1) and has_per_port_binding(snap2)
                and snap1 == snap2):
            failures.append(
                "intf1 and intf2 share the same per-port qos_map OID {} — "
                "expected distinct OIDs for {} vs {}".format(snap1, map_a, map_b))
    finally:
        _g_teardown_azure_plus_custom(map_a, map_b)

    if failures:
        st.report_fail('msg', "G1 failures:\n  " + "\n  ".join(failures))
    st.report_pass('msg',
        "G1: distinct per-port DSCP_TO_TC OIDs on intf1 ({}) and intf2 ({}); "
        "per-port binding present on both ports".format(map_a, map_b))


@pytest.mark.config_only
def test_g2_distinct_maps_distinct_tcam_labels():
    """#G2 — Binding a fresh DSCP_TO_TC map alongside AZURE on a different
    port allocates a distinct TCAM region (label isolation per PR #494).

    intf1 stays on AZURE (already programmed: ~192 entries) and intf2
    gets a fresh CUSTOM_GB; each full 64-entry map costs 64 × 3 = 192
    TCAM entries (IPv4 + IPv6 + IPv6 wide_key paired sibling).  We
    expect the region to grow by ≈192 entries; floor at 150 to absorb
    minor orchagent/SAI quantization.  Delta=0 is the pre-#494 silent
    regression signal (no per-port label allocation).

    Why not bind two fresh custom maps: AZURE (192) + 2 × custom (384)
    = 576 entries, which overflows the FX3 ing-l3-vlan-qos region (512
    entries).  syncd silently drops the second port-bind without
    propagating the SAI failure; see _g_setup_azure_plus_custom.
    """
    print_section("G2 — AZURE on intf1 + new custom map on intf2 → distinct TCAM labels",
                  art_key='dscp_to_tc')

    if test_intf2 is None:
        st.report_skip('msg',
            "G2 requires a second ingress port (ixia/peer_link mode); "
            "current topology mode '{}' has only one ingress.".format(topo_mode))

    failures = []

    _EXPECTED_NEW_MAP = 64 * 3
    _MIN_DELTA = 150

    tcam_before = dchal_tcam_info(dut)
    used_before = tcam_before.get('used', -1)
    st.log("  Baseline TCAM used = {}".format(used_before))

    map_a, map_b = _g_setup_azure_plus_custom()
    try:
        tcam_after = dchal_tcam_info(dut, min_used=used_before + _MIN_DELTA)
        used_after = tcam_after.get('used', -1)
        delta      = used_after - used_before
        st.log("  After bind: TCAM used = {}  delta = {}  (expected ≈{}, floor {})".format(
            used_after, delta, _EXPECTED_NEW_MAP, _MIN_DELTA))

        if delta < _MIN_DELTA:
            failures.append(
                "TCAM used delta={} after binding a new 64-entry map ({}) "
                "alongside AZURE — expected ≥{} (≈{} for the new map). "
                "Delta<{} indicates PR #494 regression or the per-port bind "
                "failing to program (delta=0 = no per-port label allocation).".format(
                    delta, map_b, _MIN_DELTA, _EXPECTED_NEW_MAP, _MIN_DELTA))
        else:
            st.log("  TCAM grew by {} entries for the new map  PASS".format(
                delta))
    finally:
        _g_teardown_azure_plus_custom(map_a, map_b)

    if failures:
        st.report_fail('msg', "G2 failures:\n  " + "\n  ".join(failures))
    st.report_pass('msg',
        "G2: TCAM grew by {} entries when binding {} alongside AZURE on a "
        "different port (per-port label allocation working)".format(delta, map_b))


@pytest.mark.traffic
def test_g3_per_port_traffic_isolation_dscp():
    """#G3 — Same DSCP from two different ingress ports, with two different
    DSCP_TO_TC maps, lands on two different egress queues.

    The cornerstone test: end-to-end proof that per-port DSCP-to-TC
    classification works.  Setup keeps AZURE (DSCP 0 → TC 0) on intf1
    and binds CUSTOM_GB (all→TC 7) on intf2, then sends DSCP 0 from each
    ingress and asserts the two streams land on different egress queues.
    Skipped in breakout mode.
    """
    print_section("G3 — Per-port traffic isolation: same DSCP, different maps, different queues",
                  art_key='dscp_to_tc')

    if tg_ph_ingress_b is None or port_info_ingress_b is None:
        st.report_skip('msg',
            "G3 requires two Ixia ingress ports (ixia/peer_link mode); "
            "current topology mode '{}' has only one ingress.".format(topo_mode))

    _PKTS = 250
    _RATE = 50
    _DSCP = 0
    failures = []

    map_a, map_b = _g_setup_azure_plus_custom()
    try:
        egress_intf = port_info['egress']

        st.log("  Step 1: send DSCP {} from ingress_a (mapped via {} → TC 0)".format(
            _DSCP, map_a))
        deltas_a = _g_send_dscp_burst(
            tg_ph['ingress'], IXIA_INGRESS_A_IP, _IXIA_DST_V4,
            port_info['ingress'], "G3/intf1_DSCP{}".format(_DSCP),
            dscp=_DSCP, pkts=_PKTS, rate=_RATE, egress_intf=egress_intf)
        _log_queue_placement_table(deltas_a, "[A→intf1]",
            expected=_g_expect_single_q(0, _PKTS))
        q0_pkts_a = deltas_a[0]['pkts']
        q7_pkts_a = deltas_a[7]['pkts']
        lo, hi = int(_PKTS * 0.85), int(_PKTS * 1.15)
        if not (lo <= q0_pkts_a <= hi):
            failures.append(
                "Step 1 (ingress_a, {} → TC 0): Q0 received {} pkts, expected "
                "{}±15% [{},{}]".format(map_a, q0_pkts_a, _PKTS, lo, hi))
        if q7_pkts_a > int(_PKTS * 0.05):
            failures.append(
                "Step 1 (ingress_a, {} → TC 0): Q7 received {} pkts (expected "
                "≤{} = 5% noise) — cross-classification leak".format(
                    map_a, q7_pkts_a, int(_PKTS * 0.05)))

        st.log("  Step 2: send DSCP {} from ingress_b (mapped via {} → TC 7)".format(
            _DSCP, map_b))
        deltas_b = _g_send_dscp_burst(
            tg_ph_ingress_b, IXIA_INGRESS_B_IP, _IXIA_DST_V4,
            port_info_ingress_b, "G3/intf2_DSCP{}".format(_DSCP),
            dscp=_DSCP, pkts=_PKTS, rate=_RATE, egress_intf=egress_intf)
        _log_queue_placement_table(deltas_b, "[B→intf2]",
            expected=_g_expect_single_q(7, _PKTS))
        q0_pkts_b = deltas_b[0]['pkts']
        q7_pkts_b = deltas_b[7]['pkts']
        if not (lo <= q7_pkts_b <= hi):
            failures.append(
                "Step 2 (ingress_b, {} → TC 7): Q7 received {} pkts, expected "
                "{}±15% [{},{}]".format(map_b, q7_pkts_b, _PKTS, lo, hi))
        if q0_pkts_b > int(_PKTS * 0.05):
            failures.append(
                "Step 2 (ingress_b, {} → TC 7): Q0 received {} pkts (expected "
                "≤{} = 5% noise) — cross-classification leak (per-port "
                "isolation broken)".format(
                    map_b, q0_pkts_b, int(_PKTS * 0.05)))
    finally:
        _g_teardown_azure_plus_custom(map_a, map_b)

    if failures:
        st.report_fail('msg', "G3 failures:\n  " + "\n  ".join(failures))
    st.report_pass('msg',
        "G3: DSCP {} from intf1 ({}) → Q0={}; DSCP {} from intf2 ({}) → Q7={}; "
        "per-port classification isolation confirmed".format(
            _DSCP, map_a, q0_pkts_a, _DSCP, map_b, q7_pkts_b))


@pytest.mark.traffic
def test_g4_rebind_one_port_does_not_affect_other():
    """#G4 — Rebinding a custom map on one port must not disturb the
    binding of an unrelated port.  Setup leaves AZURE on intf1 and
    binds CUSTOM_GB on intf2; the rebind step then moves intf2 from
    CUSTOM_GB back to AZURE and verifies intf1's CONFIG_DB binding and
    per-port ASIC_DB state are preserved.

    Data-plane proof: after the rebind, sending DSCP 0 from intf2 must
    land on Q0 (AZURE: DSCP 0 → TC 0), not Q7 (the previous CUSTOM_GB
    mapping).  This catches a rebind that updates CONFIG_DB/ASIC_DB
    state but fails to reprogram the per-port classifier.
    """
    print_section("G4 — Rebind one port doesn't disturb the other",
                  art_key='dscp_to_tc')

    if test_intf2 is None:
        st.report_skip('msg',
            "G4 requires a second ingress port (ixia/peer_link mode); "
            "current topology mode '{}' has only one ingress.".format(topo_mode))

    _PKTS = 250
    _RATE = 50
    failures = []

    map_a, map_b = _g_setup_azure_plus_custom()
    try:
        snap1_before = per_port_dscp_to_tc_oid(dut, test_intf)
        config_before = get_port_dscp_tc_map(dut, test_intf)
        st.log("  intf1={} before rebind: cfg={}  qos_map={}".format(
            test_intf, config_before,
            snap1_before or '(nil)'))

        # Rebind intf2 from CUSTOM_GB to AZURE.  HDEL first, then HSET, so
        # the AZURE bind is an absent->present transition rather than a
        # plain CUSTOM_GB->AZURE field update: qosorch's value-equality
        # dedupe can otherwise treat the HSET as "AZURE == cached AZURE,
        # no change" (orchagent's per-port cache can disagree with the
        # CUSTOM_GB OID syncd actually programmed) and silently skip the
        # SAI port-attr write, leaving the CUSTOM_GB classifier active.
        st.config(dut,
            'sonic-db-cli CONFIG_DB HDEL "PORT_QOS_MAP|{}" "dscp_to_tc_map"'.format(
                test_intf2),
            skip_error_check=True)
        st.wait(3)
        st.config(dut,
            'sonic-db-cli CONFIG_DB HSET "PORT_QOS_MAP|{}" "dscp_to_tc_map" "AZURE"'.format(
                test_intf2),
            skip_error_check=False)
        st.wait(8)

        snap1_after = per_port_dscp_to_tc_oid(dut, test_intf)
        config_after = get_port_dscp_tc_map(dut, test_intf)
        st.log("  intf1={} after  rebind: cfg={}  qos_map={}".format(
            test_intf, config_after,
            snap1_after or '(nil)'))

        if config_after != config_before:
            failures.append(
                "intf1 CONFIG_DB dscp_to_tc_map changed from '{}' to '{}' "
                "after rebinding intf2".format(config_before, config_after))
        if not has_per_port_binding(snap1_after):
            failures.append(
                "intf1 lost per-port binding in ASIC_DB after rebinding intf2: "
                "before={} after={}".format(snap1_before, snap1_after))
        elif snap1_after != snap1_before:
            st.log("  INFO: intf1 ASIC OIDs changed (rebind ripple) but "
                   "per-port binding intent preserved: before={} after={}".format(
                       snap1_before, snap1_after))

        intf2_cfg = get_port_dscp_tc_map(dut, test_intf2)
        if 'AZURE' not in (intf2_cfg or '').upper():
            failures.append(
                "intf2 CONFIG_DB shows '{}' after rebind to AZURE".format(intf2_cfg))

        if tg_ph_ingress_b is not None and port_info_ingress_b is not None:
            st.log("  Sending DSCP 0 from intf2 to confirm AZURE classification "
                   "is now active (expect Q0)")
            deltas = _g_send_dscp_burst(
                tg_ph_ingress_b, IXIA_INGRESS_B_IP, _IXIA_DST_V4,
                port_info_ingress_b, "G4/intf2_post_rebind_DSCP0",
                dscp=0, pkts=_PKTS, rate=_RATE)
            _log_queue_placement_table(deltas, "[G4 post-rebind]",
                expected=_g_expect_single_q(0, _PKTS))
            q0 = deltas[0]['pkts']
            q7 = deltas[7]['pkts']
            lo, hi = int(_PKTS * 0.85), int(_PKTS * 1.15)
            if not (lo <= q0 <= hi):
                failures.append(
                    "After rebind, DSCP 0 from intf2: Q0={} pkts (expected "
                    "{}±15% [{},{}]) — AZURE classification not active".format(
                        q0, _PKTS, lo, hi))
            if q7 > int(_PKTS * 0.05):
                failures.append(
                    "After rebind, DSCP 0 from intf2: Q7={} pkts (expected "
                    "≤{}) — stale CUSTOM_GB mapping still active".format(
                        q7, int(_PKTS * 0.05)))
        else:
            st.log("  Skipping G4 data-plane step (no second IXIA ingress in "
                   "topo mode '{}')".format(topo_mode))
    finally:
        _g_teardown_azure_plus_custom(map_a, map_b)

    if failures:
        st.report_fail('msg', "G4 failures:\n  " + "\n  ".join(failures))
    st.report_pass('msg',
        "G4: rebinding intf2 left intf1's CONFIG_DB and ASIC_DB binding state "
        "unchanged (per-port binding isolation under reconfig)")


@pytest.mark.traffic
def test_g5_unbound_port_default_tc():
    """#G5 — Unbound ingress port falls through to default TC0.

    Bind AZURE on intf1, leave intf2 unbound (HDEL its
    PORT_QOS_MAP|dscp_to_tc_map).  Send DSCP 49 from intf2: AZURE would
    map it to TC 7, but with no per-port binding there is no L3QOS TCAM
    entry fired for this ingress, so the packet falls through to default
    classification (TC 0).  This is the third leg of the per-port
    behavior table (bound-to-A / bound-to-B / unbound).
    """
    print_section("G5 — Unbound port → default TC0 (per-port behavior third leg)",
                  art_key='dscp_to_tc')

    if tg_ph_ingress_b is None or port_info_ingress_b is None:
        st.report_skip('msg',
            "G5 requires two IXIA ingress ports (ixia/peer_link mode); "
            "current topology mode '{}' has only one ingress.".format(topo_mode))

    _PKTS = 250
    _RATE = 50
    _DSCP = 49
    failures = []

    initial1 = get_port_dscp_tc_map(dut, test_intf)
    initial2 = get_port_dscp_tc_map(dut, test_intf2)

    st.config(dut,
        'sonic-db-cli CONFIG_DB HDEL "PORT_QOS_MAP|{}" "dscp_to_tc_map"'.format(test_intf),
        skip_error_check=True)
    st.config(dut,
        'sonic-db-cli CONFIG_DB HDEL "PORT_QOS_MAP|{}" "dscp_to_tc_map"'.format(test_intf2),
        skip_error_check=True)
    st.wait(3)
    st.config(dut,
        'sonic-db-cli CONFIG_DB HSET "PORT_QOS_MAP|{}" "dscp_to_tc_map" "AZURE"'.format(test_intf),
        skip_error_check=False)
    st.wait(8)

    try:
        st.log("  intf2={} is unbound; sending DSCP {} (would be TC 7 under "
               "AZURE) — expect Q0 (default fall-through)".format(test_intf2, _DSCP))
        deltas = _g_send_dscp_burst(
            tg_ph_ingress_b, IXIA_INGRESS_B_IP, _IXIA_DST_V4,
            port_info_ingress_b, "G5/intf2_unbound_DSCP{}".format(_DSCP),
            dscp=_DSCP, pkts=_PKTS, rate=_RATE)
        _log_queue_placement_table(deltas, "[G5 unbound→intf2]",
            expected=_g_expect_single_q(0, _PKTS))
        q0 = deltas[0]['pkts']
        q7 = deltas[7]['pkts']
        lo, hi = int(_PKTS * 0.85), int(_PKTS * 1.15)
        if not (lo <= q0 <= hi):
            failures.append(
                "Unbound intf2: Q0={} pkts for DSCP {} (expected {}±15% "
                "[{},{}]) — default TC0 fall-through not active".format(
                    q0, _DSCP, _PKTS, lo, hi))
        if q7 > int(_PKTS * 0.05):
            failures.append(
                "Unbound intf2: Q7={} pkts for DSCP {} (expected ≤{}) — "
                "global/stale DSCP-to-TC classification leaked through".format(
                    q7, _DSCP, int(_PKTS * 0.05)))
    finally:
        for intf, init in ((test_intf, initial1), (test_intf2, initial2)):
            st.config(dut,
                'sonic-db-cli CONFIG_DB HDEL "PORT_QOS_MAP|{}" "dscp_to_tc_map"'.format(intf),
                skip_error_check=True)
            if init and init not in ('', 'nil', 'None'):
                st.config(dut,
                    'sonic-db-cli CONFIG_DB HSET "PORT_QOS_MAP|{}" '
                    '"dscp_to_tc_map" "{}"'.format(intf, init),
                    skip_error_check=True)
        st.wait(5)

    if failures:
        st.report_fail('msg', "G5 failures:\n  " + "\n  ".join(failures))
    st.report_pass('msg',
        "G5: unbound intf2 DSCP {} → Q0={} (default fall-through "
        "confirmed; no L3QOS TCAM hit on unbound port)".format(_DSCP, q0))


@pytest.mark.traffic
def test_g6_per_port_classification_survives_port_flap():
    """#G6 — Per-port DSCP-to-TC binding survives an admin-down/up cycle.

    With AZURE on intf1 and CUSTOM_GB bound on intf2, admin-down then
    admin-up intf2 and confirm DSCP 0 from intf2 still lands on Q7
    (CUSTOM_GB).  This catches a regression where a port flap clears
    the per-port classifier label (which would silently regress to
    default TC0 on that port).
    """
    print_section("G6 — Per-port classification survives port flap",
                  art_key='dscp_to_tc')

    if tg_ph_ingress_b is None or port_info_ingress_b is None:
        st.report_skip('msg',
            "G6 requires two IXIA ingress ports (ixia/peer_link mode); "
            "current topology mode '{}' has only one ingress.".format(topo_mode))

    _PKTS = 250
    _RATE = 50
    _DSCP = 0
    failures = []

    map_a, map_b = _g_setup_azure_plus_custom()
    try:
        st.log("  Flapping intf2={} (shutdown / startup)".format(test_intf2))
        st.config(dut, 'sudo config interface shutdown {}'.format(test_intf2),
                  skip_error_check=True)
        st.wait(5)
        st.config(dut, 'sudo config interface startup {}'.format(test_intf2),
                  skip_error_check=True)
        st.wait(15)

        oid_after = per_port_dscp_to_tc_oid(dut, test_intf2)
        st.log("  intf2 per-port qos_map after flap: {}".format(oid_after or '(nil)'))
        if not has_per_port_binding(oid_after):
            failures.append(
                "intf2 lost per-port DSCP-to-TC binding after flap: "
                "SAI_PORT_ATTR_QOS_DSCP_TO_TC_MAP={}".format(oid_after or 'nil'))

        st.log("  Sending DSCP {} from intf2 after flap (expect Q7 via "
               "{})".format(_DSCP, map_b))
        deltas = _g_send_dscp_burst(
            tg_ph_ingress_b, IXIA_INGRESS_B_IP, _IXIA_DST_V4,
            port_info_ingress_b, "G6/intf2_post_flap_DSCP{}".format(_DSCP),
            dscp=_DSCP, pkts=_PKTS, rate=_RATE)
        _log_queue_placement_table(deltas, "[G6 post-flap]",
            expected=_g_expect_single_q(7, _PKTS))
        q0 = deltas[0]['pkts']
        q7 = deltas[7]['pkts']
        lo, hi = int(_PKTS * 0.85), int(_PKTS * 1.15)
        if not (lo <= q7 <= hi):
            failures.append(
                "After flap, DSCP {} from intf2: Q7={} pkts (expected "
                "{}±15% [{},{}]) — CUSTOM_GB classification lost".format(
                    _DSCP, q7, _PKTS, lo, hi))
        if q0 > int(_PKTS * 0.05):
            failures.append(
                "After flap, DSCP {} from intf2: Q0={} pkts (expected ≤{}) "
                "— per-port classifier reverted to default TC0".format(
                    _DSCP, q0, int(_PKTS * 0.05)))
    finally:
        _g_teardown_azure_plus_custom(map_a, map_b)

    if failures:
        st.report_fail('msg', "G6 failures:\n  " + "\n  ".join(failures))
    st.report_pass('msg',
        "G6: per-port classification on intf2 survived admin-down/up; "
        "DSCP {} → Q7={} via {} (label intact across flap)".format(
            _DSCP, q7, map_b))
