import os
import re
import sys
import pytest
from spytest import st

# Add infra/ to sys.path so all test subdirectories can do bare imports
# (e.g., import qos_test_utils, import traffic_stream_ixia_api)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'infra'))


def _nuke_bgp_all_duts():
    """Remove all BGP instances from FRR on every DUT.

    Individual module teardowns SHOULD clean up after themselves, but when
    they fail partway (e.g. VRF still bound), stale 'router bgp' instances
    persist in FRR.  init_qos_on_dut only does 'config qos reload' — it
    never touches FRR.  This safety net ensures the next module starts with
    a clean BGP slate.
    """
    for dut in st.get_dut_names():
        output = st.config(dut, "vtysh -c 'show running-config' | grep '^router bgp'",
                           skip_error_check=True)
        for line in (output or '').splitlines():
            m = re.match(r'(router bgp \d+.*)', line.strip())
            if m:
                bgp_instance = m.group(1)
                st.log(f"[config_cleanup] {dut}: removing stale '{bgp_instance}'")
                st.config(dut, f"vtysh -c 'conf t' -c 'no {bgp_instance}'",
                          skip_error_check=True)


@pytest.fixture(scope="module", autouse=True)
def config_cleanup():
    yield
    _nuke_bgp_all_duts()
