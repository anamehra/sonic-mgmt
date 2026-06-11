######################################################################
# BGP test case(s) to validate the route-map for these scenario
# 1. To verify functionality of route map application after route has been installed
#    test_ft_bgp_rmap
# 2. To verify functioning of route-map to filter incoming IPv4 prefix(s)
#    test_bgp_route_map_with_community
######################################################################

import os
import time
import yaml
import pytest
from spytest import st

import tortuga_common_utils as common_obj

# Inbound route-map changes are asynchronous (FRR route-map delay + route refresh;
# paths may be stale until peer UPDATEs are reprocessed).
BGP_RMAP_COMMUNITY_POLL_TIMEOUT_SEC = 30
BGP_RMAP_COMMUNITY_POLL_INTERVAL_SEC = 1.0

# eBGP UPDATE propagation (leaf network statement -> spine RIB) is asynchronous;
# poll instead of relying on a fixed sleep so we don't flake on slow runs.
BGP_RMAP_PREFIX_POLL_TIMEOUT_SEC = 30
BGP_RMAP_PREFIX_POLL_INTERVAL_SEC = 1.0

# Diagnostic vtysh commands. FRR rejects "show bgp ipv4 unicast neighbors <ip>"
# (it expects a sub-command like advertised-routes/routes/summary). The
# "show bgp neighbors <ip>" form is the correct one for session/Message stats.
# leaf0 -> spine at 10.1.3.1; spine0 -> leaf at 10.1.3.3 (see bgp_cfg.yaml)
BGP_RMAP_DIAG_NEIGHBOR_LEAF_TO_SPINE = (
    "vtysh -c 'show bgp neighbors 10.1.3.1'"
)
BGP_RMAP_DIAG_NEIGHBOR_SPINE_TO_LEAF = (
    "vtysh -c 'show bgp neighbors 10.1.3.3'"
)
# For prefix-propagation diagnostics, advertised-routes on the sender and
# received-routes on the peer make the missing-UPDATE case obvious.
BGP_RMAP_DIAG_ADV_LEAF_TO_SPINE = (
    "vtysh -c 'show bgp ipv4 unicast neighbors 10.1.3.1 advertised-routes'"
)
BGP_RMAP_DIAG_RX_SPINE_FROM_LEAF = (
    "vtysh -c 'show bgp ipv4 unicast neighbors 10.1.3.3 received-routes'"
)


def _log_bgp_vtysh_diag(dut, title, vtysh_cmd):
    body = st.show(dut, vtysh_cmd, skip_tmpl=True, skip_error_check=True)
    st.log("BGP diagnostics [{}]: {}".format(title, body or "(no output)"))


def _log_bgp_ft_rmap_peer_stats(nodes, title):
    """Both eBGP directions for test_ft_bgp_rmap (leaf<->spine).

    Logs neighbor session/Message stats and the advertised/received prefix
    views so a missing-prefix failure makes the sender vs receiver side clear.
    """
    _log_bgp_vtysh_diag(
        nodes["leaf0"],
        "{} [leaf0 neighbor 10.1.3.1]".format(title),
        BGP_RMAP_DIAG_NEIGHBOR_LEAF_TO_SPINE,
    )
    _log_bgp_vtysh_diag(
        nodes["spine0"],
        "{} [spine0 neighbor 10.1.3.3]".format(title),
        BGP_RMAP_DIAG_NEIGHBOR_SPINE_TO_LEAF,
    )
    _log_bgp_vtysh_diag(
        nodes["leaf0"],
        "{} [leaf0 advertised-routes to 10.1.3.1]".format(title),
        BGP_RMAP_DIAG_ADV_LEAF_TO_SPINE,
    )
    _log_bgp_vtysh_diag(
        nodes["spine0"],
        "{} [spine0 received-routes from 10.1.3.3]".format(title),
        BGP_RMAP_DIAG_RX_SPINE_FROM_LEAF,
    )


def _poll_prefix_on_spine(spine_node, prefix, present,
                          timeout=BGP_RMAP_PREFIX_POLL_TIMEOUT_SEC,
                          interval=BGP_RMAP_PREFIX_POLL_INTERVAL_SEC):
    """Poll 'show ip route' on spine until 'prefix' presence matches 'present'.

    Returns True if the desired state is observed within 'timeout'; False
    otherwise. Polling avoids fixed sleeps which flake on slow runs while
    capping the total wait so a real propagation regression still fails.
    """
    cmd = 'show ip route'
    deadline = time.time() + timeout
    while time.time() < deadline:
        cmd_output = st.config(spine_node, cmd)
        parsed_output = st.parse_show(spine_node, cmd, cmd_output,
                                      'show_ip_route.tmpl')
        seen = any(path.get('ip_address') == prefix for path in parsed_output)
        if seen == present:
            return True
        time.sleep(interval)
    return False

pytest.fixture(scope='module', autouse=True)
def box_service_module_hooks(request):
    global vars
    global bgp_cli_type, vtysh_cli_type
    global dut_list
    dut_list = [vars.D1, vars.D2, vars.D3]

    bgp_cli_type = st.get_ui_type()
    # bgp_cli_type = "click"
    if bgp_cli_type == 'click':
        bgp_cli_type = 'vtysh'
    vtysh_cli_type = bgp_cli_type
    yield

@pytest.fixture(scope='function', autouse=True)
def box_service_func_hooks(request):
    yield

CONFIGS_FILE = 'bgp_cfg.yaml'

######################################################################
#                                                                    #
#  spine0 ---default--- leaf0 ---default--- spine1                   #
#                                                                    #
######################################################################

@pytest.fixture(scope="module", autouse=True)
def setup_teardown_bgp():
    vars = st.get_testbed_vars()

    nodes = {}
    nodes['spine0'] = vars.D1
    nodes['leaf0'] = vars.D3

    dir_path = os.path.dirname(os.path.realpath(__file__))

    update_path = common_obj.modify_config_file(dir_path + '/' + CONFIGS_FILE, vars)

    with open(dir_path + '/' + CONFIGS_FILE) as c:
        config_list = yaml.load(c, Loader=yaml.FullLoader)
        for node, config in config_list.items():
            common_obj.config_frr(node, config['bgp']['config'])
            common_obj.config_static(node, 'sonic', True, update_path)

    count = 5    
    st.show(nodes['spine0'], 'sudo ping -c {} {} -q'.format(count, '10.1.3.3'), skip_tmpl=True, skip_error_check=True)
    st.show(nodes['leaf0'], 'sudo ping -c {} {} -q'.format(count, '10.1.3.1'), skip_tmpl=True, skip_error_check=True)

    yield 'setup_teardown_bgp'

    with open(dir_path + '/' + CONFIGS_FILE) as c:
        config_list = yaml.load(c, Loader=yaml.FullLoader)
        for node, config in config_list.items():
            common_obj.config_frr(node, config['bgp']['deconfig'])
            common_obj.config_static(node, 'sonic', False, update_path)


######################################################################
# Test Cases
######################################################################

# testcase #1: To verify functionality of route map application after route has been installed
def test_ft_bgp_rmap(setup_teardown_bgp):
    """
    Verify a route map application after route has been installed
    """
    retries = 4;
    vars = st.get_testbed_vars()

    nodes = {}
    nodes['spine0'] = vars.D1
    nodes['leaf0'] = vars.D3

    cmd = 'show bgp neighbors 10.1.3.1'

    for attempt in range(retries):
        parsed_output = st.vtysh_show(nodes['leaf0'], cmd)

        if not parsed_output:
            _log_bgp_vtysh_diag(
                nodes["leaf0"],
                "test_ft_bgp_rmap: vtysh_show returned no data",
                BGP_RMAP_DIAG_NEIGHBOR_LEAF_TO_SPINE,
            )
            st.report_fail("test_case_failed", nodes['leaf0'])

        if parsed_output[0]['state'] != 'Established':
            st.log("BGP peer is still not UP, wait for 30s")
            time.sleep(30)
        else:
            break

    if parsed_output[0]['state'] != 'Established':
        _log_bgp_vtysh_diag(
            nodes["leaf0"],
            "test_ft_bgp_rmap: peer not Established after retries",
            BGP_RMAP_DIAG_NEIGHBOR_LEAF_TO_SPINE,
        )
        st.report_fail("test_case_failed", nodes['leaf0'])

    cmds = ['router bgp 3003',
            'network 134.5.6.0/24']
    common_obj.config_frr(nodes['leaf0'], cmds)

    if not _poll_prefix_on_spine(nodes['spine0'], "134.5.6.0/24", present=True):
        _log_bgp_ft_rmap_peer_stats(
            nodes, "test_ft_bgp_rmap: 134.5.6.0/24 missing on spine after network"
        )
        st.report_fail("test_case_failed", nodes['spine0'])

    cmds = ['access-list test-access-list1 seq 5 deny 134.5.6.0/24',
            'route-map test-rmap deny 10',
            'match ip address test-access-list1',
            'router bgp 3003',
            'network 134.5.6.0/24 route-map test-rmap']
    common_obj.config_frr(nodes['leaf0'], cmds)

    if not _poll_prefix_on_spine(nodes['spine0'], "134.5.6.0/24", present=False):
        _log_bgp_ft_rmap_peer_stats(
            nodes,
            "test_ft_bgp_rmap: 134.5.6.0/24 still on spine after deny route-map",
        )
        st.report_fail("test_case_failed", nodes['spine0'])

    cmds = ['no route-map test-rmap deny 10',
            'no access-list test-access-list1 seq 5 deny 134.5.6.0/24']

    common_obj.config_frr(nodes['leaf0'], cmds)

    st.report_pass("test_case_passed")
    
# testcase #2: To verify functioning of route-map to filter incoming IPv4 prefix(s)
def test_bgp_route_map_with_community(setup_teardown_bgp):
    """
    Verify functioning of route-map to filter incoming IPv4 prefix(s)

    Community is visible only after inbound policy is reapplied on refreshed
    routes; poll until present (see module constants).
    """
    vars = st.get_testbed_vars()
    nodes = {}
    nodes['spine0'] = vars.D1
    nodes['leaf0'] = vars.D3

    cmds = ['ip route 40.1.1.1/32 blackhole',
            'router bgp 1001',
            'address-family ipv4 unicast',
            'redistribute static']
    common_obj.config_frr(nodes['spine0'], cmds)

    cmds = ['route-map rmap1 permit 10',
            'set community 100:100',
            'router bgp 3003',
            'address-family ipv4 unicast',
            'neighbor 10.1.3.1 route-map rmap1 in']
    common_obj.config_frr(nodes['leaf0'], cmds)

    cmd = "vtysh -c 'show bgp ipv4 40.1.1.1/32'"
    deadline = time.time() + BGP_RMAP_COMMUNITY_POLL_TIMEOUT_SEC
    community_ok = False
    while time.time() < deadline:
        cmd_output = st.config(nodes['leaf0'], cmd)
        parsed_output = st.parse_show(
            nodes['leaf0'], cmd, cmd_output, 'show_bgp_ipv4_prefix.tmpl'
        )
        for path in parsed_output:
            if path.get('community') == '100:100':
                community_ok = True
                break
        if community_ok:
            break
        time.sleep(BGP_RMAP_COMMUNITY_POLL_INTERVAL_SEC)
    if not community_ok:
        st.report_fail("test_case_failed", nodes['spine0'])

    cmd = 'no route-map rmap1 permit 10'
    common_obj.config_frr(nodes['leaf0'], cmd)

    cmd = 'no ip route 40.1.1.1/32 blackhole'
    common_obj.config_frr(nodes['spine0'], cmd)

    st.report_pass("test_case_passed")
