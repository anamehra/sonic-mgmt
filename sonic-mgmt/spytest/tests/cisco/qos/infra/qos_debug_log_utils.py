"""
QoS debug-log / counterpoll helpers.

Lower-level NPU debug-dump and counterpoll-interval management helpers split
out of qos_test_utils.py. These call the SONiC `counterpoll` CLI and the
laguna/carib `show platform npu` debug commands; they are used by QoS tests
to (a) shorten/restore counter poll intervals around a measurement window
and (b) snapshot NPU internal state on failure.

Functions:
    dump_npu_debug_state(nodes, phase="")
    get_counterpoll_interval(dut, counter_type)
    set_queue_counter_poll_interval(dut, interval_ms)
    restore_queue_counter_poll_interval(dut)
"""

from spytest import st

from qos_test_utils import detect_platform


# ---------------------------------------------------------------------------
# NPU debug state dump (laguna / carib only)
# ---------------------------------------------------------------------------

NPU_DEBUG_PLATFORMS = ('laguna', 'carib')
NPU_DEBUG_COMMANDS = (
    "sudo show platform npu oq-debug",
    "sudo show platform npu counters",
)


def dump_npu_debug_state(nodes, phase=""):
    """
    Dump NPU internal state on every laguna/carib node by running:
        sudo show platform npu oq-debug
        sudo show platform npu counters

    Output is verbose; logged via st.log so it shows up in the test log.
    No-op on platforms other than laguna/carib.

    Args:
        nodes: dict {node_name: dut} (e.g. from get_nodes()).
        phase: free-form label, e.g. "PRE-TRAFFIC" / "POST-TRAFFIC".
    """
    if not nodes:
        return
    label = "NPU DEBUG STATE" + (f" ({phase})" if phase else "")
    st.banner(label)
    for node_name, dut in nodes.items():
        try:
            plat = detect_platform(dut)
        except Exception as e:
            st.log(f"{node_name}: detect_platform failed: {e}")
            continue
        if plat not in NPU_DEBUG_PLATFORMS:
            continue
        st.log(f"--- {node_name} ({plat}) ---")
        for cmd in NPU_DEBUG_COMMANDS:
            try:
                output = st.config(dut, cmd, skip_error_check=True)
                st.log(f"{node_name}: {cmd}\n{output}")
            except Exception as e:
                st.log(f"{node_name}: {cmd} failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Counterpoll interval get / set / restore
# ---------------------------------------------------------------------------

# Per-DUT cache of queue counterpoll intervals captured prior to a set call,
# keyed by str(dut). Used by restore_queue_counter_poll_interval().
# The cache may hold _UNKNOWN_INTERVAL when capture failed (CLI empty /
# unparsable) so restore can distinguish "nothing was saved" from
# "saved unknown" and avoid silently leaving the shortened interval in place.
_UNKNOWN_INTERVAL = object()
_saved_queue_counter_interval = {}


def get_counterpoll_interval(dut, counter_type):
    """
    Read the current counterpoll interval (in ms) for a counter type by parsing
    'counterpoll show' output.

    The CLI's column order is not stable across SONiC versions / images.
    Observed shapes for a row whose first token is the counter type:
        QUEUE_STAT                  default (10000)     enable
        ACL                         10000               enable
        WRED_ECN_QUEUE_STAT         5000                enable
        QUEUE_STAT                  enable              10000

    Strategy: walk tokens after parts[0] and return the first token that
    parses as an integer (or matches '(N)' for the 'default (N)' form).

    Args:
        dut: DUT object
        counter_type: Row name from 'counterpoll show', e.g. 'QUEUE_STAT',
                      'QUEUE_WATERMARK_STAT', 'BUFFER_POOL_WATERMARK_STAT'.

    Returns:
        int interval in ms, or None if not found / unparsable.
    """
    output = st.show(dut, "counterpoll show", skip_tmpl=True,
                     skip_error_check=True)
    if not output:
        return None
    for line in output.splitlines():
        parts = line.split()
        if not parts or parts[0] != counter_type:
            continue
        for tok in parts[1:]:
            cleaned = tok.strip('()')
            if cleaned.isdigit():
                try:
                    return int(cleaned)
                except ValueError:
                    continue
        return None
    return None


def set_queue_counter_poll_interval(dut, interval_ms):
    """
    Set the queue (stat) counterpoll interval on a DUT.

    Captures the prior interval (so restore_queue_counter_poll_interval can put
    it back) and then issues 'counterpoll queue interval <ms>' which updates
    FLEX_COUNTER_TABLE|QUEUE_STAT POLL_INTERVAL in CONFIG_DB.

    Args:
        dut: DUT object
        interval_ms: Poll interval in milliseconds
    """
    dut_key = str(dut)
    if dut_key not in _saved_queue_counter_interval:
        prior = get_counterpoll_interval(dut, 'QUEUE_STAT')
        if prior is None:
            _saved_queue_counter_interval[dut_key] = _UNKNOWN_INTERVAL
            st.warn(f"Could not capture prior QUEUE_STAT counterpoll interval "
                    f"on {dut}; restore will not be able to undo the change")
        else:
            _saved_queue_counter_interval[dut_key] = prior
            st.log(f"Captured prior QUEUE_STAT counterpoll interval: {prior}ms")
    st.log(f"Setting queue counterpoll interval to {interval_ms}ms")
    st.config(dut, f"sudo counterpoll queue interval {interval_ms}",
              skip_error_check=True)


def restore_queue_counter_poll_interval(dut):
    """
    Restore the queue counterpoll interval to whatever was captured by the last
    set_queue_counter_poll_interval() call. If nothing was captured, query the
    current value and leave it alone.
    """
    dut_key = str(dut)
    if dut_key not in _saved_queue_counter_interval:
        st.log("No saved QUEUE_STAT counterpoll interval; skipping restore")
        return
    prior = _saved_queue_counter_interval.pop(dut_key)
    if prior is _UNKNOWN_INTERVAL:
        st.warn(f"Prior QUEUE_STAT counterpoll interval on {dut} was not "
                f"captured; cannot restore -- shortened interval remains in "
                f"effect (manual recovery may be required)")
        return
    st.log(f"Restoring queue counterpoll interval to {prior}ms")
    st.config(dut, f"sudo counterpoll queue interval {prior}",
              skip_error_check=True)
