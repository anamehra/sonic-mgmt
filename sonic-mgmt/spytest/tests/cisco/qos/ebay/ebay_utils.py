"""
Shared utilities for eBay QoS tests (HBM, DWRR, etc.)
"""
import qos_test_utils
from spytest import st

SUPPORTED_PLATFORMS = ['8201_32fh']


def check_platform_supported(dut):
    """Check if DUT matches a supported platform. Returns True if supported."""
    platform = qos_test_utils.get_dut_platform(dut) or ""
    if any(p in platform for p in SUPPORTED_PLATFORMS):
        st.log(f"Platform '{platform}' supported")
        return True

    st.log(f"Platform '{platform}' not in {SUPPORTED_PLATFORMS}")
    return False
