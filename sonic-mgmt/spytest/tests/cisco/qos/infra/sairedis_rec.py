"""
SairedisRec - Read and parse /var/log/swss/sairedis.rec on a DUT.

Provides access to SAI operations recorded by syncd, allowing tests to
verify that CONFIG_DB changes propagate correctly to the SAI layer.

Usage:
    from sairedis_rec import SairedisRec, SairedisEntry

    rec = SairedisRec(dut)

    # sync/since_last_sync pattern
    rec.sync()
    do_config_change()
    time.sleep(3)
    for entry in rec.since_last_sync():
        if entry.op == SairedisEntry.Set and "SAI_PORT_ATTR_MTU" in entry.data:
            print(entry.attrs["SAI_PORT_ATTR_MTU"])

"""
import time
from contextlib import contextmanager
from spytest import st


class SairedisEntry:
    """A single parsed line from sairedis.rec.

    Attributes:
        op: Operation code character: 'c' create, 'r' remove, 's' set,
            'g' get, 'G' get-response, 'E' error, '#' comment, etc.
        data: Everything after the op code (the raw payload).
        key: The object key, e.g. "SAI_OBJECT_TYPE_PORT:oid:0x1000000000005"
        attrs: Dict of {attr_name: value} for entries that have attributes.
        raw: The full original line.
    """

    # Constants for op codes used in tests
    Create = "c"
    Remove = "r"
    Set = "s"
    Get = "g"
    GetResponse = "G"
    Error = "E"
    Comment = "#"

    def __init__(self, raw_line):
        self.raw = raw_line
        self.op = None
        self.data = ""
        self.key = ""
        self.attrs = {}
        self._parse(raw_line)

    def _parse(self, line):
        # Format: TIMESTAMP|OP|DATA...
        parts = line.split("|")
        if len(parts) < 2:
            return
        # parts[0] = timestamp, parts[1] = op code
        self.op = parts[1]
        self.data = "|".join(parts[2:])

        # For object operations (c, r, s, g), first data field is the key
        # Note: uppercase C/R/S are bulk operations with a different layout
        # (split on || not |), so we exclude them here.
        if self.op in ("c", "r", "s", "g") and len(parts) > 2:
            self.key = parts[2]
            # Parse attr=value pairs from remaining fields
            for field in parts[3:]:
                if "=" in field:
                    name, _, val = field.partition("=")
                    self.attrs[name] = val

        # For responses (G), first field is status, rest are attrs
        elif self.op == "G" and len(parts) > 2:
            self.key = parts[2]  # status
            for field in parts[3:]:
                if "=" in field:
                    name, _, val = field.partition("=")
                    self.attrs[name] = val

        # For errors (E), data is the status
        elif self.op == "E" and len(parts) > 2:
            self.key = parts[2]

    def __repr__(self):
        return f"SairedisEntry(op='{self.op}', key='{self.key}')"


class SairedisRec:
    """
    Read head into /var/log/swss/sairedis.rec on a DUT.

    Call sync() to mark current position, then since_last_sync() to get
    only new entries. Handles log rotation (file shorter than sync point).
    """

    def __init__(self, dut, path="/var/log/swss/sairedis.rec"):
        self._dut = dut
        self._path = path
        self._sync_pos = None
        self._recording = None

    def sync(self):
        """Mark current end of file as the read head position."""
        self._sync_pos = self._line_count()
        st.log(f"SairedisRec: synced at line {self._sync_pos}")
        return self

    @contextmanager
    def recording(self, settle):
        """Context manager: sync before, sleep + collect after.

        Usage:
            with rec.recording(settle=3):
                st.config(dut, ...)
            entries = rec.last_recording
        """
        self.sync()
        yield
        time.sleep(settle)
        self._recording = self.since_last_sync()

    @property
    def last_recording(self):
        """Entries captured by the most recent recording() context."""
        return self._recording or []

    def find(self, entries=None, op=None, key_contains=None, attr=None):
        """Filter entries by op type, key substring, or attribute name.

        Args:
            entries: List to filter (defaults to last_recording).
            op: Op code to match (e.g. SairedisEntry.Set).
            key_contains: Substring that must appear in entry.key.
            attr: Attribute name that must exist in entry.attrs.
        Returns:
            Filtered list of SairedisEntry.
        """
        results = entries if entries is not None else self.last_recording
        if op:
            results = [e for e in results if e.op == op]
        if key_contains:
            results = [e for e in results if key_contains in e.key]
        if attr:
            results = [e for e in results if attr in e.attrs]
        return results

    def since_last_sync(self):
        """Return list of SairedisEntry for all lines added since last sync().

        Handles log rotation: if file is now shorter, reads entire file.
        """
        if self._sync_pos is None:
            raise RuntimeError("Call sync() first")

        current = self._line_count()
        if current < self._sync_pos:
            # Log rotation happened - read remaining lines from rotated file,
            # then read entire new file
            st.log("SairedisRec: log rotation detected, reading rotated + new file")
            rotated_path = f"{self._path}.1"
            lines = self._run(
                f"tail -n +{self._sync_pos + 1} {rotated_path} 2>/dev/null"
            )
            lines += self._run(f"cat {self._path}")
        else:
            lines = self._run(f"tail -n +{self._sync_pos + 1} {self._path}")

        return [e for e in (SairedisEntry(l) for l in lines if l.strip()) if e.op is not None]

    def _line_count(self):
        """Return current line count of the sairedis.rec file, or 0 if missing."""
        output = self._run(f"wc -l < {self._path} 2>/dev/null || echo 0")
        try:
            return int(output[0].strip())
        except (IndexError, ValueError):
            return 0

    def _run(self, cmd):
        """Run a shell command on the DUT and return output as list of lines."""
        output = st.config(self._dut, cmd, skip_error_check=True)
        if not output:
            return []
        return output.strip().splitlines()