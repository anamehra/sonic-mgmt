"""
Test for SairedisRec: validate CONFIG_DB PFC changes propagate to SAI.
"""
from spytest import st

import os
import sys
import time
current_dir = os.path.dirname(os.path.abspath(__file__))
infra_dir = os.path.join(current_dir, '..', 'infra')
if infra_dir not in sys.path:
    sys.path.insert(0, infra_dir)

from sairedis_rec import SairedisRec, SairedisEntry
from qos_test_utils import fail_test, pass_test


SAI_PROPAGATION_DELAY = 3  # seconds for CONFIG_DB -> orchagent -> syncd -> SAI
HSET_PROPAGATION_DELAY = 10  # seconds for HSET via redis-cli -> orchagent -> syncd -> SAI


def _get_dut():
    dut = getattr(st.get_testbed_vars(), 'D1', None)
    if not dut:
        fail_test("No DUT available")
    return dut


def _get_test_port(dut):
    """Discover the first Ethernet port from CONFIG_DB on the DUT."""
    out = st.config(dut, "redis-cli -n 4 KEYS 'PORT|Ethernet*'", skip_error_check=True, skip_tmpl=True)
    for line in (out or '').splitlines():
        line = line.strip().strip('"')
        # redis-cli output: '1) "PORT|Ethernet1_4"' or plain 'PORT|Ethernet1_4'
        if "PORT|Ethernet" in line:
            # Extract the part after the last "PORT|"
            idx = line.rfind("PORT|")
            port_key = line[idx:]  # "PORT|Ethernet1_4"
            port_key = port_key.strip('"')
            return port_key.split("|", 1)[1]
    fail_test("No Ethernet port found in CONFIG_DB")



class TestSync:
    """Exercise sync/since_last_sync edge cases."""

    def test_since_last_sync_without_sync_raises(self):
        """since_last_sync() without sync() should raise RuntimeError."""
        dut = _get_dut()
        rec = SairedisRec(dut)
        try:
            rec.since_last_sync()
            fail_test("Expected RuntimeError from since_last_sync() without sync()")
        except RuntimeError as e:
            st.log(f"Correctly raised: {e}")

        pass_test("since_last_sync() without sync() raises RuntimeError")

    def test_log_rotation_reads_entire_file(self):
        """
        Simulate log rotation by syncing, then forcing the sync position
        past the current file length. since_last_sync() should detect
        the file is shorter and read the entire file.
        """
        dut = _get_dut()
        rec = SairedisRec(dut)

        rec.sync()
        # Fake a rotation: pretend we synced at a position far beyond current file
        rec._sync_pos = 999999999

        ops = rec.since_last_sync()
        st.log(f"Log rotation path returned {len(ops)} entries")

        if not ops:
            fail_test("Log rotation path returned 0 entries (expected entire file)")

        # Verify they're all valid parsed entries
        for op in ops:
            assert op.op is not None

        pass_test(f"Log rotation detected and read entire file ({len(ops)} entries)")

    def test_nonexistent_file_returns_empty(self):
        """
        Point SairedisRec at a file that doesn't exist.
        _line_count() should return 0 (ValueError path),
        and since_last_sync() should return [] (_run empty output path).
        """
        dut = _get_dut()
        rec = SairedisRec(dut, path="/tmp/nonexistent_sairedis_test.rec")

        rec.sync()
        assert rec._sync_pos == 0, f"Expected 0, got {rec._sync_pos}"

        ops = rec.since_last_sync()
        assert ops == [], f"Expected empty list, got {len(ops)} entries"

        pass_test("Nonexistent file: _line_count()=0 and _run() returns []")

    def test_recording_and_find(self):
        """
        Use recording() context manager to capture SAI entries from a
        config change, then use find() to filter the results.
        """
        dut = _get_dut()
        test_port = _get_test_port(dut)
        rec = SairedisRec(dut)

        # Get current MTU and toggle to a different value to guarantee a SAI change
        cur_mtu_output = st.config(dut, f'redis-cli -n 4 HGET "PORT|{test_port}" mtu',
                            skip_error_check=True, skip_tmpl=True)
        # redis-cli output may include extra lines; extract the numeric value
        cur_mtu = None
        for line in (cur_mtu_output or "").splitlines():
            line = line.strip().strip('"')
            if line.isdigit():
                cur_mtu = line
                break
        if not cur_mtu:
            fail_test("Cannot read current MTU from CONFIG_DB — aborting to avoid state corruption")
        new_mtu = "9200" if cur_mtu != "9200" else "9100"

        try:
            # recording() should sync, wait, and populate last_recording
            with rec.recording(settle=HSET_PROPAGATION_DELAY):
                st.config(dut, f'redis-cli -n 4 HSET "PORT|{test_port}" mtu "{new_mtu}"')

            entries = rec.last_recording
            st.log(f"recording() captured {len(entries)} entries")
            assert len(entries) > 0, "recording() captured nothing"

            # find() with op filter
            sets_only = rec.find(op=SairedisEntry.Set)
            assert all(e.op == SairedisEntry.Set for e in sets_only)

            # find() with key_contains filter
            port_entries = rec.find(key_contains="SAI_OBJECT_TYPE_PORT")
            assert all("SAI_OBJECT_TYPE_PORT" in e.key for e in port_entries)

            # find() with attr filter
            mtu_entries = rec.find(op=SairedisEntry.Set, attr="SAI_PORT_ATTR_MTU")
            st.log(f"find(op=Set, attr=MTU) returned {len(mtu_entries)} entries")
            assert len(mtu_entries) > 0, "MTU change did not propagate to SAI"
            # Verify all returned entries actually contain the filtered attribute
            assert all("SAI_PORT_ATTR_MTU" in e.attrs for e in mtu_entries), \
                "find(attr=...) returned entries missing the requested attribute"

            # find() with explicit entries arg (not using last_recording)
            filtered = rec.find(entries=entries, op=SairedisEntry.Set)
            assert filtered == sets_only

            # find() with no matches returns empty list
            empty = rec.find(op=SairedisEntry.Error)
            assert empty == []
        finally:
            st.config(dut, f'redis-cli -n 4 HSET "PORT|{test_port}" mtu "{cur_mtu}"',
                      skip_error_check=True, skip_tmpl=True)

        pass_test("recording() and find() work correctly")


class TestSairedisEntryParsing:
    """Exercise all parse branches of SairedisEntry with known data."""

    def test_all_op_types_parsed(self):
        """
        Feed representative lines for each op type and verify
        SairedisEntry parses them correctly. Covers: c, r, s, g, G, E, #,
        short/malformed lines, key, attrs, __repr__, and all constants.
        """
        _get_dut()  # ensure DUT is available

        # Create (c) - key + attrs
        e = SairedisEntry("2026-01-01.00:00:00.000|c|SAI_OBJECT_TYPE_ROUTE:10.0.0.0/24|SAI_ROUTE_ATTR_NEXT_HOP=oid:0x5")
        assert e.op == SairedisEntry.Create
        assert e.key == "SAI_OBJECT_TYPE_ROUTE:10.0.0.0/24"
        assert e.attrs["SAI_ROUTE_ATTR_NEXT_HOP"] == "oid:0x5"

        # Remove (r) - key only, no attrs
        e = SairedisEntry("2026-01-01.00:00:00.000|r|SAI_OBJECT_TYPE_ROUTE:10.0.0.0/24")
        assert e.op == SairedisEntry.Remove
        assert e.key == "SAI_OBJECT_TYPE_ROUTE:10.0.0.0/24"
        assert e.attrs == {}

        # Set (s)
        e = SairedisEntry("2026-01-01.00:00:00.000|s|SAI_OBJECT_TYPE_PORT:oid:0x1|SAI_PORT_ATTR_ADMIN_STATE=true")
        assert e.op == SairedisEntry.Set
        assert e.attrs["SAI_PORT_ATTR_ADMIN_STATE"] == "true"

        # Get (g)
        e = SairedisEntry("2026-01-01.00:00:00.000|g|SAI_OBJECT_TYPE_SWITCH:oid:0x21|SAI_SWITCH_ATTR_DEFAULT_VIRTUAL_ROUTER_ID=oid:0x0")
        assert e.op == SairedisEntry.Get
        assert "SAI_SWITCH_ATTR_DEFAULT_VIRTUAL_ROUTER_ID" in e.attrs

        # Get response (G) - status as key, attrs follow
        e = SairedisEntry("2026-01-01.00:00:00.000|G|SAI_STATUS_SUCCESS|SAI_SWITCH_ATTR_DEFAULT_VIRTUAL_ROUTER_ID=oid:0x3")
        assert e.op == SairedisEntry.GetResponse
        assert e.key == "SAI_STATUS_SUCCESS"
        assert e.attrs["SAI_SWITCH_ATTR_DEFAULT_VIRTUAL_ROUTER_ID"] == "oid:0x3"

        # Error (E) - status as key
        e = SairedisEntry("2026-01-01.00:00:00.000|E|SAI_STATUS_INSUFFICIENT_RESOURCES")
        assert e.op == SairedisEntry.Error
        assert e.key == "SAI_STATUS_INSUFFICIENT_RESOURCES"

        # Comment (#)
        e = SairedisEntry("2026-01-01.00:00:00.000|#|logrotate on: /var/log/swss/sairedis.rec")
        assert e.op == SairedisEntry.Comment
        assert "logrotate" in e.data

        # Short/malformed line - should not crash
        e = SairedisEntry("garbage")
        assert e.op is None

        # __repr__ coverage
        e = SairedisEntry("2026-01-01.00:00:00.000|s|SAI_OBJECT_TYPE_PORT:oid:0x1|SAI_PORT_ATTR_MTU=9100")
        r = repr(e)
        assert "SairedisEntry" in r
        assert "op='s'" in r

        st.log("All op types parsed correctly")
        pass_test("SairedisEntry covers all parse branches and constants")

class TestPfcPropagation:
    """Validate that PFC CONFIG_DB writes propagate to SAI via sairedis.rec."""
    def test_pfc_propagates_to_sai(self):
        """
        Test PFC propagation to SAI via CLI and redis-cli:
        1. CLI: config interface pfc priority on  → bit set
        2. CLI: config interface pfc priority off → bit cleared
        3. HSET pfc_enable="3,4"                  → bitmask=24
        4. HSET pfc_enable=""                     → bitmask=0
        """
        dut = _get_dut()
        test_port = _get_test_port(dut)
        priority = 3
        rec = SairedisRec(dut)
        failures = []
        PFC_ATTR = "SAI_PORT_ATTR_PRIORITY_FLOW_CONTROL"
        # Verify the port has a PORT_QOS_MAP entry (PFC requires QoS config)
        qos_check = st.config(dut, f'redis-cli -n 4 EXISTS "PORT_QOS_MAP|{test_port}"',
                              skip_error_check=True, skip_tmpl=True)
        if "0" in (qos_check or ""):
            st.log(f"SairedisRec: PORT_QOS_MAP|{test_port} does not exist, skipping PFC test")
            pass_test("Skipped - no PORT_QOS_MAP entry for test port")
            return
        def _get_pfc_val(retry_timeout=0):
            """Return the last PFC bitmask from the most recent recording, or None.
            If retry_timeout > 0, keep re-reading sairedis.rec until the
            PFC attribute appears or the timeout expires.
            """
            deadline = time.time() + retry_timeout
            while True:
                entries = rec.find(op=SairedisEntry.Set, attr=PFC_ATTR)
                if entries:
                    return int(entries[-1].attrs[PFC_ATTR])
                if time.time() >= deadline:
                    return None
                time.sleep(1)
                rec._recording = rec.since_last_sync()
        # Save original pfc_enable to restore later
        orig_pfc = st.config(dut, f'redis-cli -n 4 HGET "PORT_QOS_MAP|{test_port}" pfc_enable',
                             skip_error_check=True, skip_tmpl=True)
        orig_pfc_val = None  # None means field was absent
        for line in (orig_pfc or "").splitlines():
            line = line.strip().strip('"')
            if line and line != "(nil)":
                orig_pfc_val = line
                break
        try:
            # --- 0. Ensure priority is OFF first so ON causes a real change ---
            st.config(dut, f"sudo config interface pfc priority {test_port} {priority} off",
                      skip_error_check=True)
            time.sleep(SAI_PROPAGATION_DELAY)
            # --- 1. CLI ON ---
            st.banner(f"STEP 1: config interface pfc priority {test_port} {priority} on")
            with rec.recording(settle=SAI_PROPAGATION_DELAY):
                st.config(dut, f"sudo config interface pfc priority {test_port} {priority} on")
            val = _get_pfc_val()
            if val is not None and val & (1 << priority):
                st.log(f"  PASS: bit {priority} set (value={val})")
            else:
                st.log(f"  FAIL: bit {priority} not set (value={val})")
                failures.append(f"CLI ON: bit {priority} not set (value={val})")
            # --- 2. CLI OFF ---
            st.banner(f"STEP 2: config interface pfc priority {test_port} {priority} off")
            with rec.recording(settle=SAI_PROPAGATION_DELAY):
                st.config(dut, f"sudo config interface pfc priority {test_port} {priority} off")
            val = _get_pfc_val()
            if val is not None and not (val & (1 << priority)):
                st.log(f"  PASS: bit {priority} cleared (value={val})")
            else:
                st.log(f"  FAIL: bit {priority} not cleared (value={val})")
                failures.append(f"CLI OFF: bit {priority} not cleared (value={val})")
            # --- 3. HSET pfc_enable="3,4" ---
            test_pfc = "3,4"
            expected_bitmask = (1 << 3) | (1 << 4)  # 24
            st.banner(f'STEP 3: HSET PORT_QOS_MAP|{test_port} pfc_enable "{test_pfc}"')
            with rec.recording(settle=HSET_PROPAGATION_DELAY):
                st.config(dut, f'redis-cli -n 4 HSET "PORT_QOS_MAP|{test_port}" pfc_enable "{test_pfc}"')
            val = _get_pfc_val(retry_timeout=HSET_PROPAGATION_DELAY)
            if val is not None and val == expected_bitmask:
                st.log(f"  PASS: bitmask={val} (expected {expected_bitmask})")
            else:
                st.log(f"  FAIL: bitmask={val} (expected {expected_bitmask})")
                failures.append(f"HSET '{test_pfc}': got {val}, expected {expected_bitmask}")
            # --- 4. HSET pfc_enable="" via EVAL (shell-safe empty string) ---
            st.banner(f'STEP 4: HSET PORT_QOS_MAP|{test_port} pfc_enable ""')
            with rec.recording(settle=HSET_PROPAGATION_DELAY):
                # Use Lua EVAL to pass empty string to HSET — avoids shell eating ""
                st.config(dut,
                    f"redis-cli -n 4 EVAL \"redis.call('HSET', KEYS[1], 'pfc_enable', '')\" 1 \"PORT_QOS_MAP|{test_port}\"")
            val = _get_pfc_val(retry_timeout=HSET_PROPAGATION_DELAY)
            if val is not None and val == 0:
                st.log(f"  PASS: bitmask=0")
            else:
                st.log(f"  FAIL: bitmask={val} (expected 0)")
                failures.append(f"HSET empty: got {val}, expected 0")
        finally:
            # Restore original pfc_enable
            if orig_pfc_val is None:
                st.config(dut, f'redis-cli -n 4 HDEL "PORT_QOS_MAP|{test_port}" pfc_enable',
                          skip_error_check=True, skip_tmpl=True)
            else:
                st.config(dut, f'redis-cli -n 4 HSET "PORT_QOS_MAP|{test_port}" pfc_enable "{orig_pfc_val}"',
                          skip_error_check=True, skip_tmpl=True)
        # --- Result ---
        if failures:
            fail_test("PFC propagation failures: " + "; ".join(failures))
        pass_test("All PFC propagation steps passed")

