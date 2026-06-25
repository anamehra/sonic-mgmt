"""
Validate that DSCP_TO_TC_MAP and TC_TO_QUEUE_MAP changes in CONFIG_DB
propagate to the SAI layer (visible in sairedis.rec).

Covers:
1. Creating a new map + binding to port -> SAI_OBJECT_TYPE_QOS_MAP create
   with correct type and value list
2. Modifying a field value in an existing map -> SAI QOS_MAP set/re-create
3. Changing PORT_QOS_MAP binding to a new map -> SAI_PORT_ATTR_QOS_* set
4. Binding same map to multiple ports -> shared SAI OID (no duplicate create)
"""
import time

from spytest import st

from config_db import ConfigDb
from sairedis_rec import SairedisRec, SairedisEntry
from qos_test_utils import fail_test, pass_test


HSET_PROPAGATION_DELAY = 10  # seconds for CONFIG_DB -> orchagent -> syncd -> SAI


def _get_dut():
    dut = getattr(st.get_testbed_vars(), 'D3', None)
    if not dut:
        fail_test("No DUT available")
    return dut


class TestQosMapSaiPropagation:
    """Validate that DSCP_TO_TC_MAP and TC_TO_QUEUE_MAP changes in CONFIG_DB
    propagate to the SAI layer (visible in sairedis.rec)."""

    TEST_MAP_NAME = "TEST_QOS_MAP_SAI"

    # Test map entries for each table type
    DSCP_MAP_ENTRIES = {str(i): ("3" if i == 3 else "4" if i == 4 else "1")
                        for i in range(64)}
    TC_QUEUE_MAP_ENTRIES = {str(i): str(i) for i in range(8)}

    # SAI map type expected per CONFIG_DB table
    SAI_MAP_TYPES = {
        "DSCP_TO_TC_MAP": "SAI_QOS_MAP_TYPE_DSCP_TO_TC",
        "TC_TO_QUEUE_MAP": "SAI_QOS_MAP_TYPE_TC_TO_QUEUE",
    }

    def _setup(self):
        dut = _get_dut()
        config = ConfigDb(dut)
        rec = SairedisRec(dut)
        if "PORT_QOS_MAP" not in config:
            fail_test("PORT_QOS_MAP table not found in CONFIG_DB")
        port = next((k for k in config["PORT_QOS_MAP"].keys()
                     if k.startswith("Ethernet")), None)
        if not port:
            fail_test("No Ethernet port with PORT_QOS_MAP entry found")
        return config, rec, port

    def _get_two_ports(self, config):
        """Return two Ethernet ports that have PORT_QOS_MAP entries."""
        ports = [k for k in config["PORT_QOS_MAP"].keys()
                 if k.startswith("Ethernet")]
        if len(ports) < 2:
            fail_test("Need at least 2 ports with PORT_QOS_MAP entries")
        return ports[0], ports[1]

    def _cleanup_map(self, config, table):
        config.refresh()
        if self.TEST_MAP_NAME in config.get(table, {}):
            del config[table][self.TEST_MAP_NAME]

    def _map_params(self):
        """Yield (table, entries, port_field, sai_attr) for each map type."""
        return [
            ("DSCP_TO_TC_MAP", self.DSCP_MAP_ENTRIES,
             "dscp_to_tc_map", "SAI_PORT_ATTR_QOS_DSCP_TO_TC_MAP"),
            ("TC_TO_QUEUE_MAP", self.TC_QUEUE_MAP_ENTRIES,
             "tc_to_queue_map", "SAI_PORT_ATTR_QOS_TC_TO_QUEUE_MAP"),
        ]

    def test_create_map_propagates_to_sai(self):
        """Creating a new map and binding to a port propagates to SAI with correct content."""
        config, rec, port = self._setup()

        for table, entries, port_field, _ in self._map_params():
            st.banner(f"Creating new {table} and binding to {port}")
            orig_binding = config["PORT_QOS_MAP"][port].get(port_field)
            self._cleanup_map(config, table)
            time.sleep(1)

            try:
                with rec.recording(settle=HSET_PROPAGATION_DELAY):
                    config[table][self.TEST_MAP_NAME] = entries
                    config["PORT_QOS_MAP"][port][port_field] = self.TEST_MAP_NAME

                creates = rec.find(op=SairedisEntry.Create,
                                   key_contains="SAI_OBJECT_TYPE_QOS_MAP")
                if not creates:
                    fail_test(f"New {table} did not propagate to SAI")

                # Verify SAI entry has correct map type and value list
                expected_type = self.SAI_MAP_TYPES[table]
                found_type = any(e.attrs.get("SAI_QOS_MAP_ATTR_TYPE") == expected_type
                                 for e in creates)
                found_values = any("SAI_QOS_MAP_ATTR_MAP_TO_VALUE_LIST" in e.attrs
                                   for e in creates)

                st.log(f"{table}: creates={len(creates)}, "
                       f"correct_type={found_type}, has_value_list={found_values}")

                if not found_type:
                    fail_test(f"{table}: SAI map type {expected_type} not found")
                if not found_values:
                    fail_test(f"{table}: SAI_QOS_MAP_ATTR_MAP_TO_VALUE_LIST missing")
            finally:
                if orig_binding:
                    config["PORT_QOS_MAP"][port][port_field] = orig_binding
                time.sleep(HSET_PROPAGATION_DELAY)
                self._cleanup_map(config, table)

        pass_test("New QoS map creation propagated to SAI with correct content")

    def test_field_update_propagates_to_sai(self):
        """Modifying a field in an active map propagates to SAI."""
        config, rec, port = self._setup()

        for table, port_field, test_field in [
            ("DSCP_TO_TC_MAP", "dscp_to_tc_map", "40"),
            ("TC_TO_QUEUE_MAP", "tc_to_queue_map", "2"),
        ]:
            active_map = config["PORT_QOS_MAP"][port].get(port_field)
            if not active_map:
                fail_test(f"Port {port} has no {port_field} binding")

            orig_value = config[table][active_map][test_field]
            new_value = "7" if orig_value != "7" else "6"
            st.banner(f"Updating {table}|{active_map} field {test_field}: "
                      f"{orig_value} -> {new_value}")

            try:
                with rec.recording(settle=HSET_PROPAGATION_DELAY):
                    config[table][active_map][test_field] = new_value

                sets = rec.find(op=SairedisEntry.Set,
                                key_contains="SAI_OBJECT_TYPE_QOS_MAP")
                creates = rec.find(op=SairedisEntry.Create,
                                   key_contains="SAI_OBJECT_TYPE_QOS_MAP")
                st.log(f"{table}: QOS_MAP sets={len(sets)}, creates={len(creates)}")

                if not sets and not creates:
                    fail_test(f"{table} field update did not propagate to SAI")
            finally:
                config[table][active_map][test_field] = orig_value
                time.sleep(HSET_PROPAGATION_DELAY)

        pass_test("QoS map field updates propagated to SAI for both map types")

    def test_port_binding_propagates_to_sai(self):
        """Changing PORT_QOS_MAP binding to a new map propagates to SAI."""
        config, rec, port = self._setup()

        for table, entries, port_field, sai_attr in self._map_params():
            st.banner(f"Rebinding {port} {port_field} to new map")
            orig_binding = config["PORT_QOS_MAP"][port].get(port_field)
            self._cleanup_map(config, table)
            time.sleep(1)

            try:
                # Pre-create the map, then bind separately
                config[table][self.TEST_MAP_NAME] = entries
                time.sleep(HSET_PROPAGATION_DELAY)

                with rec.recording(settle=HSET_PROPAGATION_DELAY):
                    config["PORT_QOS_MAP"][port][port_field] = self.TEST_MAP_NAME

                port_sets = rec.find(op=SairedisEntry.Set,
                                     key_contains="SAI_OBJECT_TYPE_PORT")
                attr_sets = [e for e in port_sets if sai_attr in e.attrs]
                st.log(f"{table}: PORT sets={len(port_sets)}, "
                       f"with {sai_attr}={len(attr_sets)}")

                if not attr_sets:
                    fail_test(f"PORT_QOS_MAP {port_field} binding did not "
                              f"propagate {sai_attr} to SAI")

                st.log(f"{sai_attr} set to OID: {attr_sets[0].attrs[sai_attr]}")
            finally:
                if orig_binding:
                    config["PORT_QOS_MAP"][port][port_field] = orig_binding
                time.sleep(HSET_PROPAGATION_DELAY)
                self._cleanup_map(config, table)

        pass_test("PORT_QOS_MAP binding changes propagated to SAI for both map types")

    def test_shared_map_across_ports(self):
        """Binding same map to a second port reuses SAI OID (no duplicate create)."""
        config, rec, _ = self._setup()
        port1, port2 = self._get_two_ports(config)

        for table, entries, port_field, sai_attr in self._map_params():
            st.banner(f"Binding same {table} to {port1} and {port2}")
            orig_binding1 = config["PORT_QOS_MAP"][port1].get(port_field)
            orig_binding2 = config["PORT_QOS_MAP"][port2].get(port_field)
            self._cleanup_map(config, table)
            time.sleep(1)

            try:
                # Create the map and bind to first port
                config[table][self.TEST_MAP_NAME] = entries
                config["PORT_QOS_MAP"][port1][port_field] = self.TEST_MAP_NAME
                time.sleep(HSET_PROPAGATION_DELAY)

                # Bind same map to second port and capture SAI
                with rec.recording(settle=HSET_PROPAGATION_DELAY):
                    config["PORT_QOS_MAP"][port2][port_field] = self.TEST_MAP_NAME

                port_sets = rec.find(op=SairedisEntry.Set,
                                     key_contains="SAI_OBJECT_TYPE_PORT")
                new_creates = rec.find(op=SairedisEntry.Create,
                                       key_contains="SAI_OBJECT_TYPE_QOS_MAP")
                attr_sets = [e for e in port_sets if sai_attr in e.attrs]

                st.log(f"{table}: PORT sets={len(port_sets)}, "
                       f"new QOS_MAP creates={len(new_creates)}, "
                       f"{sai_attr} sets={len(attr_sets)}")

                if new_creates:
                    fail_test(f"{table}: second port binding created a new "
                              "QOS_MAP instead of reusing existing OID")
                if not attr_sets:
                    fail_test(f"{table}: second port binding did not set "
                              f"{sai_attr}")

                st.log(f"{table}: second port reuses OID "
                       f"{attr_sets[0].attrs[sai_attr]} (shared LUT)")
            finally:
                if orig_binding1:
                    config["PORT_QOS_MAP"][port1][port_field] = orig_binding1
                if orig_binding2:
                    config["PORT_QOS_MAP"][port2][port_field] = orig_binding2
                time.sleep(HSET_PROPAGATION_DELAY)
                self._cleanup_map(config, table)

        pass_test("Shared map binding to multiple ports reuses SAI OID")
