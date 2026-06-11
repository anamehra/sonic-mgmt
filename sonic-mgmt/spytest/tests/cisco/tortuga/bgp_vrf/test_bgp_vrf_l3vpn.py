import os
import time
import yaml
import pytest
import sys
from spytest import st
script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(script_dir, '../common/'))

import tortuga_common_utils as common_obj

CONFIGS_FILE = 'bgp_basic_cfg.yaml'

# How long to wait for FRR's bgpd to fully drop 'router bgp' instances during
# module teardown before we let SONiC deconfig run. The drain race manifests
# as 'sudo config interface ip rem ...' failing with
# "Cannot remove the last IP entry of interface ... A static ip route is
# still bound to the RIF" because zebra is still holding the kernel netlink
# binding on that interface. Polling here, rather than a fixed sleep, lets
# fast hardware finish quickly and gives slow hardware a chance to converge.
FRR_DRAIN_TIMEOUT_SEC = 60
FRR_DRAIN_INTERVAL_SEC = 2


def _wait_for_frr_bgp_drained(dut, timeout=FRR_DRAIN_TIMEOUT_SEC,
                              interval=FRR_DRAIN_INTERVAL_SEC):
    """Poll until FRR's bgpd running-config no longer has any 'router bgp'
    instance on the given DUT. This is the signal we use that FRR has fully
    released the kernel-side VRF/interface bindings so that subsequent SONiC
    'vrf del' / 'ip rem' commands will actually take effect."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = st.show(dut, "vtysh -c 'show running-config bgpd'",
                      skip_tmpl=True, skip_error_check=True)
        if "router bgp" not in str(out or ""):
            return True
        time.sleep(interval)
    st.log("FRR drain timeout on {}: 'router bgp' still present in bgpd "
           "running-config; SONiC deconfig may leave residue".format(dut))
    return False

####################
#                  #
#    D1 = spine0      #
#    D2 = spine1      #
#    D3 = leaf0      #
#    D4 = leaf1      #
#                  #
####################

######################################################################
#          eBGP             eBGP           iBGP                      #
#  spine0 ---default--- leaf0 ---Vrf01--- spine1 ---Vrf02--- leaf1             #
#                                                                    #
######################################################################

@pytest.fixture(scope="module", autouse=True)
def setup_teardown_bgp_vrf():
    vars = st.get_testbed_vars()

    nodes = {}
    nodes['spine0'] = vars.D1
    nodes['spine1'] = vars.D2
    nodes['leaf0'] = vars.D3
    nodes['leaf1'] = vars.D4

    dir_path = os.path.dirname(os.path.realpath(__file__))

    update_path = common_obj.modify_config_file(dir_path + '/' + CONFIGS_FILE, vars)

    with open(dir_path + '/' + CONFIGS_FILE) as c:
        config_list = yaml.load(c, Loader=yaml.FullLoader)
        for node, config in config_list.items():
            common_obj.config_frr(node, config['bgp']['config'])
            common_obj.config_static(node, 'sonic', True, update_path)

    count = 5    
    st.show(nodes['spine0'], 'sudo ping -c {} {} -q'.format(count, '10.1.1.2'), skip_tmpl=True, skip_error_check=True)
    st.show(nodes['leaf0'], 'sudo ping -I Vrf01 -c {} {} -q'.format(count, '20.1.1.2'), skip_tmpl=True, skip_error_check=True)
    st.show(nodes['spine1'], 'sudo ping -I Vrf02 -c {} {} -q'.format(count, '30.1.1.2'), skip_tmpl=True, skip_error_check=True)
    st.show(nodes['spine0'], 'sudo ping -c {} {} -q'.format(count, '10::2'), skip_tmpl=True, skip_error_check=True)
    st.show(nodes['leaf0'], 'sudo ping -I Vrf01 -c {} {} -q'.format(count, '20::2'), skip_tmpl=True, skip_error_check=True)

    yield 'setup_teardown_bgp_vrf'

    # Three-phase teardown to avoid the FRR drain race that surfaced as
    # "Cannot remove the last IP entry of interface ... A static ip route is
    # still bound to the RIF" during the May-12 run (test_bgp_vrf_l3vpn
    # module-teardown chain bailed and the failure was attributed to the
    # last test, test_bgp_vrf_check_ibgp_vrf_conn, leaving Vrf01 / D3D2P1 /
    # D3D2P2 in a polluted state for downstream modules):
    #   1) FRR deconfig on every node first (so all 'router bgp' instances
    #      are removed and zebra starts releasing kernel netlink bindings),
    #   2) poll each DUT until bgpd no longer reports any 'router bgp',
    #   3) THEN run SONiC deconfig. Each step is guarded so a single failure
    #      does not abort the rest of the unwind.
    nodes_map = {
        'spine0': nodes['spine0'],
        'spine1': nodes['spine1'],
        'leaf0': nodes['leaf0'],
        'leaf1': nodes['leaf1'],
    }
    # Accumulate any teardown problems (raised exceptions OR a False return
    # from the drain poll) and emit a single, greppable st.error at the end
    # if anything went wrong. We intentionally do NOT mark the module failed
    # via st.report_fail here:
    #   - the test bodies themselves passed,
    #   - failing the teardown would mark every test in the module errored
    #     in Allure, which is noisier than the actual problem,
    #   - the residue-cleanup itself is best-effort and already continues
    #     past individual failures so each remaining step still gets a
    #     chance to run.
    # The st.error gives a human reading the run log a clear, named anchor
    # ("bgp_vrf_l3vpn module unwound with N issue(s)") to suspect when a
    # downstream module then fails with stale Vrf01 / IP / `router bgp`
    # state. With this PR's drain-poll + 3-phase ordering the issue list is
    # expected to be empty on healthy runs; it surfaces only the rare race.
    teardown_issues = []
    with open(dir_path + '/' + CONFIGS_FILE) as c:
        config_list = yaml.load(c, Loader=yaml.FullLoader)

        for node, config in config_list.items():
            try:
                common_obj.config_frr(node, config['bgp']['deconfig'])
            except Exception as e:
                msg = "FRR deconfig on {} failed: {}".format(node, e)
                st.log("teardown: " + msg)
                teardown_issues.append(msg)

        for node in config_list.keys():
            try:
                drained = _wait_for_frr_bgp_drained(nodes_map[node])
            except Exception as e:
                msg = "FRR drain poll on {} raised: {}".format(node, e)
                st.log("teardown: " + msg)
                teardown_issues.append(msg)
                continue
            if not drained:
                # _wait_for_frr_bgp_drained returns False on timeout (and
                # already logs the per-node detail). Record it so the
                # banner below names the node.
                teardown_issues.append(
                    "FRR drain timed out on {}: 'router bgp' still present, "
                    "SONiC deconfig that follows may no-op".format(node))

        for node, config in config_list.items():
            try:
                common_obj.config_static(node, 'sonic', False, update_path)
            except Exception as e:
                msg = "SONiC deconfig on {} failed: {}".format(node, e)
                st.log("teardown: " + msg)
                teardown_issues.append(msg)

    if teardown_issues:
        st.error(msg=(
            "teardown: bgp_vrf_l3vpn module unwound with {} issue(s); "
            "downstream modules may inherit Vrf01 / IP / 'router bgp' "
            "residue. Details:\n  - {}"
        ).format(len(teardown_issues), "\n  - ".join(teardown_issues)))

def setup_bgp_vrf_network_scale(node, add=True):
    vars = st.get_testbed_vars()

    nodes = {}
    nodes['spine0'] = vars.D1
    nodes['spine1'] = vars.D2
    nodes['leaf0'] = vars.D3
    nodes['leaf1'] = vars.D4

    dir_path = os.path.dirname(os.path.realpath(__file__))

    domain = 'vtysh'

    with open(dir_path + '/' + 'bgp_vrf_route_scale.yaml') as c:
        config_list = yaml.load(c, Loader=yaml.FullLoader)
        if add:
            common_obj.config_node(nodes[node], config_list[node]['bgp']['config'], domain)
        else:
            common_obj.config_node(nodes[node], config_list[node]['bgp']['deconfig'], domain)

# This testcases are added intended to check BGP VRF feature.
#########################################
# Testcases
#########################################
@pytest.mark.system_box
@pytest.mark.community
@pytest.mark.community_pass
def test_bgp_vfr_nbr_reach():
    vars = st.get_testbed_vars()

    nodes = {}
    nodes['spine0'] = vars.D1
    nodes['spine1'] = vars.D2
    nodes['leaf0'] = vars.D3
    nodes['leaf1'] = vars.D4

    #1. Ping end to end loopback address of BGP neighbours
    # Ping Loopback address of BGP neighbour
    cmd = "ping -c 5 192.168.1.1"
    cmd_output = st.config(nodes['leaf0'], cmd)
    if " 0% packet loss" in str(cmd_output.encode('ascii','ignore')):
        st.log("Ping to spine0 is Sucessful")
    else:
        st.report_fail("test_case_failed", nodes['leaf0'])

    cmd = "ping -I Vrf01 -c 5 192.168.1.3"
    cmd_output = st.config(nodes['leaf0'], cmd)
    if " 0% packet loss" in str(cmd_output.encode('ascii','ignore')):
        st.log("Ping to spine1 is Sucessful")
    else:
        st.report_fail("test_case_failed", nodes['leaf0'])

    st.report_pass('test_case_passed', nodes['spine0'])
    st.report_pass('test_case_passed', nodes['spine1'])
    st.report_pass('test_case_passed', nodes['leaf0'])
    st.report_pass('test_case_passed', nodes['leaf1'])

def test_bgp_vrf_validate_route():
    """
    2. Check routes are installed in respective VRF 
       Check Loopback route is installed in repsective VRF of neighbouring BGP peer
    """
    vars = st.get_testbed_vars()

    nodes = {}
    nodes['spine0'] = vars.D1
    nodes['spine1'] = vars.D2
    nodes['leaf0'] = vars.D3
    nodes['leaf1'] = vars.D4

    cmd = "show ip route vrf Vrf01 192.168.1.3"
    cmd_output = st.config(nodes['leaf0'], cmd)

    if len(cmd_output) > 0 and '20.1.1.2, via {}'.format(vars.D3D2P1) in str(cmd_output.encode('ascii','ignore')):
        st.log("VRF route is available")
    else:
        st.report_fail("test_case_failed", nodes['leaf0'])

    st.report_pass('test_case_passed', nodes['spine0'])
    st.report_pass('test_case_passed', nodes['spine1'])
    st.report_pass('test_case_passed', nodes['leaf0'])
    st.report_pass('test_case_passed', nodes['leaf1'])

def test_bgp_vrf_scale_check():
    """
    3. scale test, installing 10 prefix in 20 milli sec
       Need to check how to delete installed prefix.
    """
    vars = st.get_testbed_vars()

    nodes = {}
    nodes['spine0'] = vars.D1
    nodes['spine1'] = vars.D2
    nodes['leaf0'] = vars.D3
    nodes['leaf1'] = vars.D4

    setup_bgp_vrf_network_scale('spine1')
    time.sleep(1/50)

    for i in range(1, 11, 1):
        cmd = 'show ip route vrf Vrf01 1.1.1.{}'.format(i)
        cmd_output = st.config(nodes['leaf0'], cmd)
        if len(cmd_output) > 0 and '20.1.1.2, via {}'.format(vars.D3D2P1) in str(cmd_output.encode('ascii','ignore')):
            st.log("VRF route is available")
        else:
            st.report_fail("test_case_failed", nodes['leaf0'])

    st.report_pass('test_case_passed', nodes['spine0'])
    st.report_pass('test_case_passed', nodes['spine1'])
    st.report_pass('test_case_passed', nodes['leaf0'])
    st.report_pass('test_case_passed', nodes['leaf1'])

def test_check_routers_are_unambiguous():
    """
    5. Within each VRF, each address must be unambiguous on DUT
       Add two static route of same IP with different static route to VRF
       should install only one IP in the RIB table.
    """
    vars = st.get_testbed_vars()

    nodes = {}
    nodes['spine0'] = vars.D1
    nodes['spine1'] = vars.D2
    nodes['leaf0'] = vars.D3
    nodes['leaf1'] = vars.D4

    # All disrupt-then-verify steps below are wrapped in try/finally so that
    # the trailing FRR cleanup (no route-map / no prefix-list / no ip route)
    # always runs even if an intermediate st.report_fail (or any other
    # exception) short-circuits the test. Without this, a fail in the middle
    # leaves stale 13.1.1.0/24 static routes, the ALLOW_PREFIX route-map and
    # allow_list prefix-list on leaf0's FRR, which then poison subsequent
    # tests in this module (and downstream modules that rely on clean VRF
    # state).
    try:
        cmds = ['ip route 13.1.1.0/24 Null0 vrf Vrf01',
                'router bgp 2002 vrf Vrf01',
                'address-family ipv4 unicast',
                'redistribute static',
                'exit-address-family',
                'exit']

        common_obj.config_frr(nodes['leaf0'], cmds)

        cmd_output = st.show(nodes['leaf0'], "vtysh -c 'show ip route vrf Vrf01'")
        if "13.1.1.0/24" not in str(cmd_output):
            st.report_fail("test_case_failed", nodes['leaf0'])

        cmds = ['ip route 13.1.1.0/24 Null0 tag 100 vrf Vrf01',
                'router bgp 2002 vrf Vrf01',
                'address-family ipv4 unicast',
                'redistribute static',
                'exit-address-family',
                'exit']

        common_obj.config_frr(nodes['leaf0'], cmds)

        cmd_output = st.show(nodes['leaf0'], "vtysh -c 'show ip route vrf Vrf01 13.1.1.0'")
        if str(cmd_output).count("13.1.1.0") > 1 or "tag 100" not in str(cmd_output):
            st.report_fail("test_case_failed", nodes['leaf0'])

        #6. check adding of same route in default VRF is allowed
        cmds = ['ip route 13.1.1.0/24 Null0',
            'router bgp 1002',
            'address-family ipv4 unicast',
            'redistribute static',
            'exit-address-family',
            'exit']
        common_obj.config_frr(nodes['leaf0'], cmds)

        cmd_output = st.show(nodes['leaf0'], "vtysh -c 'show ip route'")
        if "13.1.1.0/24" not in str(cmd_output):
            st.report_fail("test_case_failed", nodes['leaf0'])

        #7. check this routes are learnt by respective BGP
        # Check in spine0 route is installed in default instance.
        cmd = "show ip route 13.1.1.0"
        cmd_output = st.config(nodes['spine0'], cmd)

        if len(cmd_output) > 0 and '10.1.1.2, via {}'.format(vars.D1D3P1) in str(cmd_output):
            st.log("default vrf route is available")
        else:
            st.report_fail("test_case_failed", nodes['spine0'])

        cmd = "show ip route vrf Vrf01 13.1.1.0"
        cmd_output = st.config(nodes['spine1'], cmd)
        if len(cmd_output) > 0 and '20.1.1.1, via {}'.format(vars.D2D3P1) in str(cmd_output):
            st.log("VRF route is available")
        else:
            st.report_fail("test_case_failed", nodes['spine1'])

        #Add route map to match 13.1.1.0 and prepend AS with 9009 to Vrf01
        cmds = ['ip prefix-list allow_list permit 13.1.1.0/24',
            'route-map ALLOW_PREFIX permit 10',
            'match ip address prefix-list allow_list',
            'set as-path prepend 9009',
            'router bgp 2002 vrf Vrf01',
            'address-family ipv4 unicast',
            'neighbor 20.1.1.2 route-map ALLOW_PREFIX out']

        common_obj.config_frr(nodes['leaf0'], cmds)

        #check 9009 is present in spine1
        cmd = "vtysh -c 'show bgp vrf Vrf01 ipv4 neighbors 20.1.1.1 routes'"
        cmd_output = st.show(nodes['spine1'], cmd)

        if "2002 9009" not in str(cmd_output):
            st.report_fail("test_case_failed", nodes['spine1'])

        #check 9009 is not present in spine0
        cmd = "vtysh -c 'show bgp ipv4 neighbors 10.1.1.2 routes'"
        cmd_output = st.show(nodes['spine0'], cmd)

        if "2002 9009" in str(cmd_output):
            st.report_fail("test_case_failed", nodes['spine0'])
    finally:
        # Unbind the outbound route-map from the Vrf01 BGP neighbor BEFORE
        # deleting the route-map definition. FRR keeps `neighbor X route-map
        # ... out` references separate from the route-map itself, so just
        # deleting the route-map leaves the (now-empty) outbound policy on
        # the peer and later tests inherit a dangling reference that can
        # silently stop advertisements.
        try:
            common_obj.config_frr(nodes['leaf0'], [
                'router bgp 2002 vrf Vrf01',
                'address-family ipv4 unicast',
                'no neighbor 20.1.1.2 route-map ALLOW_PREFIX out',
            ])
        except Exception as e:
            st.log("cleanup: unbind ALLOW_PREFIX from Vrf01 peer failed: {}".format(e))
        # Idempotent cleanup. Each step is guarded so a single failure does
        # not skip the rest of the unwind.
        for cleanup_cmd in [
            'no route-map ALLOW_PREFIX',
            'no ip prefix-list allow_list permit 13.1.1.0/24',
            # The test first installs the UNTAGGED Vrf01 variant (line ~296)
            # and only then converts it to the `tag 100` variant. If the
            # test exits between those two steps (e.g., the verify at
            # line ~306 raises `st.report_fail`), the untagged route is
            # left and the `no ... tag 100 ...` cleanup below does not
            # match it. Remove the untagged variant first; FRR no-ops if
            # the prefix was already replaced by the tagged variant.
            'no ip route 13.1.1.0/24 Null0 vrf Vrf01',
            'no ip route 13.1.1.0/24 Null0 tag 100 vrf Vrf01',
            'no ip route 13.1.1.0/24 Null0',
        ]:
            try:
                common_obj.config_frr(nodes['leaf0'], cleanup_cmd)
            except Exception as e:
                st.log("cleanup: '{}' on leaf0 failed: {}".format(cleanup_cmd, e))

    st.report_pass('test_case_passed', nodes['spine0'])
    st.report_pass('test_case_passed', nodes['spine1'])
    st.report_pass('test_case_passed', nodes['leaf0'])
    st.report_pass('test_case_passed', nodes['leaf1'])

def test_bgp_vrf_check_static_route_redist():
    """
     9. Advertise same set of prefixes from different VRFs
    10. Redistribute Static routes and verify on remote routers
    """
    vars = st.get_testbed_vars()

    nodes = {}
    nodes['spine0'] = vars.D1
    nodes['spine1'] = vars.D2
    nodes['leaf0'] = vars.D3
    nodes['leaf1'] = vars.D4

    # See note in test_check_routers_are_unambiguous: wrap disrupt/verify in
    # try/finally so the 15.1.1.0/24 static routes are always removed from
    # both spines, even if an intermediate st.report_fail short-circuits.
    try:
        # configure 15.1.1.0/24 in Vrf01 instance of spine1.
        cmds = ['ip route 15.1.1.0/24 Null0 vrf Vrf01',
                'router bgp 1003 vrf Vrf01',
                'address-family ipv4 unicast',
                'redistribute static',
                'exit-address-family',
                'exit']
        common_obj.config_frr(nodes['spine1'], cmds)

        # configure 15.1.1.0/24 in default instance of spine0.
        cmds = ['ip route 15.1.1.0/24 Null0',
                'router bgp 1001',
                'address-family ipv4 unicast',
                'redistribute static',
                'exit-address-family',
                'exit']
        common_obj.config_frr(nodes['spine0'], cmds)

        # check same route from spine1 and spine0 of different instance is
        # installed in leaf0 of default and Vrf02 instance
        cmd = "show ip route vrf Vrf01 15.1.1.0"

        cmd_output = st.config(nodes['leaf0'], cmd)
        if len(cmd_output) > 0 and '20.1.1.2, via {}'.format(vars.D3D2P1) in str(cmd_output):
            st.log("VRF route is available")
        else:
            st.report_fail("test_case_failed", nodes['leaf0'])

        cmd = "show ip route 15.1.1.0"
        cmd_output = st.config(nodes['leaf0'], cmd)

        if len(cmd_output) > 0 and '10.1.1.1, via {}'.format(vars.D3D1P1) in str(cmd_output):
            st.log("default vrf route is available")
        else:
            st.report_fail("test_case_failed", nodes['leaf0'])
    finally:
        for node, cleanup_cmd in [
            (nodes['spine0'], 'no ip route 15.1.1.0/24 Null0'),
            (nodes['spine1'], 'no ip route 15.1.1.0/24 Null0 vrf Vrf01'),
        ]:
            try:
                common_obj.config_frr(node, cleanup_cmd)
            except Exception as e:
                st.log("cleanup: '{}' on {} failed: {}".format(cleanup_cmd, node, e))

    st.report_pass('test_case_passed', nodes['spine0'])
    st.report_pass('test_case_passed', nodes['spine1'])
    st.report_pass('test_case_passed', nodes['leaf0'])
    st.report_pass('test_case_passed', nodes['leaf1'])

def test_bgp_vrf_static_route_inter_vrf_comm():
    """
    14. Verify inter-vrf communication between eBGP peer
    """
    vars = st.get_testbed_vars()

    nodes = {}
    nodes['spine0'] = vars.D1
    nodes['spine1'] = vars.D2
    nodes['leaf0'] = vars.D3
    nodes['leaf1'] = vars.D4

    # See note in test_check_routers_are_unambiguous: wrap disrupt/verify in
    # try/finally so the 30.1.1.0/24 nexthop-vrf static route on leaf0 is
    # always removed, even if an intermediate st.report_fail short-circuits.
    try:
        cmds = ['ip route 30.1.1.0/24 {} nexthop-vrf Vrf01'.format(vars.D3D2P1),
                'router bgp 1002',
                'address-family ipv4 unicast',
                'redistribute static',
                'exit-address-family',
                'exit']
        common_obj.config_frr(nodes['leaf0'], cmds)

        cmd = "show ip route 30.1.1.0"

        cmd_output = st.config(nodes['leaf0'], cmd)

        if len(cmd_output) > 0 and 'directly connected, {}'.format(vars.D3D2P1) in str(cmd_output):
            st.log("VRF route is available")
        else:
            st.report_fail("test_case_failed", nodes['leaf0'])

        cmd = "show ip route 30.1.1.0"
        cmd_output = st.config(nodes['spine0'], cmd)

        if len(cmd_output) > 0 and '10.1.1.2, via {}'.format(vars.D1D3P1) in str(cmd_output):
            st.log("default vrf route is available")
        else:
            st.report_fail("test_case_failed", nodes['spine0'])
    finally:
        cleanup_cmd = 'no ip route 30.1.1.0/24 {} nexthop-vrf Vrf01'.format(vars.D3D2P1)
        try:
            common_obj.config_frr(nodes['leaf0'], cleanup_cmd)
        except Exception as e:
            st.log("cleanup: '{}' on leaf0 failed: {}".format(cleanup_cmd, e))

    st.report_pass('test_case_passed', nodes['spine0'])
    st.report_pass('test_case_passed', nodes['spine1'])
    st.report_pass('test_case_passed', nodes['leaf0'])
    st.report_pass('test_case_passed', nodes['leaf1'])

def test_bgp_vrf_check_ibgp_vrf_conn():
    """
    13. Verify intra-vrf and inter-vrf communication between
    """
    vars = st.get_testbed_vars()

    nodes = {}
    nodes['spine0'] = vars.D1
    nodes['spine1'] = vars.D2
    nodes['leaf0'] = vars.D3
    nodes['leaf1'] = vars.D4

    # check leaf1 loopback address is present in spine1 Vrf02 route table
    cmd = 'show ip route vrf Vrf02 192.168.1.4'
    cmd_output = st.config(nodes['spine1'], cmd)
    if len(cmd_output) > 0 and '* 30.1.1.2, via {}'.format(vars.D2D4P1) in str(cmd_output):
        st.log("vrf route is available")
    else:
        st.report_fail("test_case_failed", nodes['spine1'])

    # ping loopback addres of leaf1
    cmd = "ping -I Vrf02 -c 5 192.168.1.4"
    cmd_output = st.config(nodes['spine1'], cmd)
    if " 0% packet loss" in str(cmd_output.encode('ascii','ignore')):
        st.log("Ping to spine1 is Sucessful")
    else:
        st.report_fail("test_case_failed", nodes['spine1'])

    st.report_pass('test_case_passed', nodes['spine0'])
    st.report_pass('test_case_passed', nodes['spine1'])
    st.report_pass('test_case_passed', nodes['leaf0'])
    st.report_pass('test_case_passed', nodes['leaf1'])
