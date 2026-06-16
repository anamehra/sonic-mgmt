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

####################
#                  #
#    D1 = spine0      #
#    D2 = spine1      #
#    D3 = leaf0      #
#    D4 = leaf1      #
#                  #
####################

def config_frr(dut, commands):

    if not isinstance(commands, list):
        commands = [commands]

    st.log("Configuring on frr: {}".format(commands))
    with open("/tmp/spytest_frr.conf", "w") as fd:
        fd.write("\n".join(commands))
    st.upload_file_to_dut(dut, "/tmp/spytest_frr.conf", "/tmp/spytest_frr.conf")

    st.config(dut, "docker cp /tmp/spytest_frr.conf bgp:/")
    st.config(dut, "docker exec bgp bash -c 'vtysh -f /spytest_frr.conf'")


# FRR defers BGP route-map processing by ~bgp route-map delay-timer (default 5s),
# then may use route refresh; with enhanced RR paths can be marked stale until
# refresh UPDATEs arrive. A fixed sleep races that pipeline.
BGP_VRF_RMAP_POLL_TIMEOUT_SEC = 30
BGP_VRF_RMAP_POLL_INTERVAL_SEC = 1.0

# After 'no router bgp ...' commands, FRR's bgpd/zebra may still hold kernel
# netlink locks on the VRF/interface bindings for several seconds. If we run
# 'sudo config vrf del Vrf01' / 'sudo config interface ip rem ...' immediately
# after the FRR deconfig, those SONiC commands can silently fail and leave
# residue: Vrf01 alive, D3D2P1 still bound to Vrf01, 20.1.1.1/24 still
# configured. The next module's setup (e.g. test_l2vni_v6_vtep.py which
# expects D3D2P1 in the default VRF with link-local-only IPv6) then fails
# to bring up TRANSIT/OVERLAY BGP, cascading into every test in that module.
FRR_DRAIN_TIMEOUT_SEC = 30
FRR_DRAIN_INTERVAL_SEC = 1.0

def _wait_bgp_vrf_neighbor_routes_contains(dut, show_cmd, needle, diag_title=None):
    """Poll neighbor routes output until needle appears or timeout."""
    vty = "vtysh -c '{}'".format(show_cmd)
    deadline = time.time() + BGP_VRF_RMAP_POLL_TIMEOUT_SEC
    while time.time() < deadline:
        out = st.show(dut, vty, skip_tmpl=True, skip_error_check=True)
        if needle in str(out or ""):
            return True
        time.sleep(BGP_VRF_RMAP_POLL_INTERVAL_SEC)
    st.log(
        "BGP VRF neighbor routes poll timeout: {} (cmd={})".format(
            diag_title or "no detail", show_cmd
        )
    )
    return False


def _wait_for_frr_bgp_drained(dut, timeout=FRR_DRAIN_TIMEOUT_SEC, interval=FRR_DRAIN_INTERVAL_SEC):
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


def _wait_for_frr_bgp_vrf_drained(dut, vrf, timeout=FRR_DRAIN_TIMEOUT_SEC,
                                  interval=FRR_DRAIN_INTERVAL_SEC):
    """Same drain signal as `_wait_for_frr_bgp_drained` but scoped to ONE
    VRF. Used by per-test cleanups that must `config vrf del <vrf>` while
    other `router bgp ... vrf <other>` instances are still expected to be
    running (so we cannot wait for ALL `router bgp` to drain).

    Polls for the absence of any `router bgp <ASN> vrf <vrf>` line in
    bgpd's running-config on the given DUT, returning True as soon as the
    instance is gone, False on timeout (logged-only, caller decides what
    to do)."""
    deadline = time.time() + timeout
    vrf_token = "vrf {}".format(vrf)
    while time.time() < deadline:
        out = st.show(dut, "vtysh -c 'show running-config bgpd'",
                      skip_tmpl=True, skip_error_check=True)
        out_s = str(out or "")
        # Anchor on `router bgp` at the start of the (stripped) line so we
        # don't match inner directives like `import vrf <name>`.
        present = False
        for line in out_s.splitlines():
            s = line.strip()
            if s.startswith("router bgp") and vrf_token in s:
                present = True
                break
        if not present:
            return True
        time.sleep(interval)
    st.log("FRR drain timeout on {}: 'router bgp ... {}' still present in "
           "bgpd running-config; SONiC 'config vrf del {}' may no-op and "
           "leave residue".format(dut, vrf_token, vrf))
    return False


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
            config_frr(node, config['bgp']['config'])
            common_obj.config_static(node, 'sonic', True, update_path)

    count = 5    
    st.show(nodes['spine0'], 'sudo ping -c {} {} -q'.format(count, '10.1.1.2'), skip_tmpl=True, skip_error_check=True)
    st.show(nodes['leaf0'], 'sudo ping -I Vrf01 -c {} {} -q'.format(count, '20.1.1.2'), skip_tmpl=True, skip_error_check=True)
    st.show(nodes['spine1'], 'sudo ping -I Vrf02 -c {} {} -q'.format(count, '30.1.1.2'), skip_tmpl=True, skip_error_check=True)
    st.show(nodes['spine0'], 'sudo ping -c {} {} -q'.format(count, '10::2'), skip_tmpl=True, skip_error_check=True)
    st.show(nodes['leaf0'], 'sudo ping -I Vrf01 -c {} {} -q'.format(count, '20::2'), skip_tmpl=True, skip_error_check=True)

    yield 'setup_teardown_bgp_vrf'

    # Two-stage teardown: FRR deconfig on every node first, then poll each
    # node until bgpd has no 'router bgp' left, THEN run SONiC deconfig. This
    # avoids the FRR drain race where 'sudo config vrf del' silently no-ops
    # because zebra still has the VRF / interface IPs pinned to kernel
    # netlink. Each step is guarded so a single failure does not abort the
    # rest of the unwind.
    nodes_map = {
        'spine0': vars.D1,
        'spine1': vars.D2,
        'leaf0': vars.D3,
        'leaf1': vars.D4,
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
    # ("bgp_vrf_set_2 module unwound with N issue(s)") to suspect when a
    # downstream module then fails with stale Vrf01 / D3D2P1 / `router bgp`
    # state. With this PR's drain-poll + 3-phase ordering the issue list is
    # expected to be empty on healthy runs; it surfaces only the rare race.
    teardown_issues = []
    with open(dir_path + '/' + CONFIGS_FILE) as c:
        config_list = yaml.load(c, Loader=yaml.FullLoader)
        for node, config in config_list.items():
            try:
                config_frr(node, config['bgp']['deconfig'])
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
            "teardown: bgp_vrf_set_2 module unwound with {} issue(s); "
            "downstream modules may inherit Vrf01 / D3D2P1 / 'router bgp' "
            "residue. Details:\n  - {}"
        ).format(len(teardown_issues), "\n  - ".join(teardown_issues)))

def test_bgp_vrf_interface_flap_check():
    vars = st.get_testbed_vars()

    nodes = {}
    nodes['spine0'] = vars.D1
    nodes['spine1'] = vars.D2
    nodes['leaf0'] = vars.D3
    nodes['leaf1'] = vars.D4

    # Wrap disrupt (rem IP + shutdown) + check in try/finally so that the
    # restore (startup + vrf bind + ip add) on D3D2P1 always runs even if the
    # st.parse_show / st.report_fail path raises. Without this, D3D2P1 stays
    # shut and detached from Vrf01, the module teardown's 'ip rem 20.1.1.1/24'
    # and 'vrf del Vrf01' fail, and the next module (e.g. test_l2vni_v6_vtep)
    # sees a port that is not in the default VRF.
    restore_cmds = ['sudo config interface startup {}'.format(vars.D3D2P1),
                    'sudo config interface vrf bind {} Vrf01'.format(vars.D3D2P1),
                    'sudo config interface ip add {} 20.1.1.1/24'.format(vars.D3D2P1)]
    try:
        cmds = ['sudo config interface ip rem {} 20.1.1.1/24'.format(vars.D3D2P1),
               'sudo config interface shutdown {}'.format(vars.D3D2P1)]
        for cmd in cmds:
            cmd_output = st.config(nodes['leaf0'], cmd)

        time.sleep(1)

        # check route table for Vrf01 instance is not exists
        cmd = 'show ip route vrf Vrf01'
        cmd_output = st.config(nodes['leaf0'], cmd)

        parsed_output = st.parse_show(nodes['leaf0'], cmd, cmd_output, 'show_ip_route.tmpl')
        for path in parsed_output:
            if path['type'] == 'B' and path['ip_address'] == "192.168.1.3/32":
                st.report_fail("test_case_failed", nodes['leaf0'])
    finally:
        for cleanup_cmd in restore_cmds:
            try:
                st.config(nodes['leaf0'], cleanup_cmd)
            except Exception as e:
                st.log("cleanup: '{}' on leaf0 failed: {}".format(cleanup_cmd, e))

    time.sleep(1)
    st.show(nodes['leaf0'], 'sudo ping -I Vrf01 -c 5 {} -q'.format('20.1.1.2'), skip_tmpl=True, skip_error_check=True)

    prefix_present = False
    cmd = 'show ip route vrf Vrf01'
    cmd_output = st.config(nodes['leaf0'], cmd)
    parsed_output = st.parse_show(nodes['leaf0'], cmd, cmd_output, 'show_ip_route.tmpl')
    for path in parsed_output:
        if path['type'] == 'B' and path['selected'] == '>' and path['ip_address'] == "192.168.1.3/32" and path['nexthop'] == "20.1.1.2":
            prefix_present = True

    if prefix_present != True:
        st.report_fail("test_case_failed", nodes['leaf0'])

    st.report_pass('test_case_passed', nodes['spine0'])
    st.report_pass('test_case_passed', nodes['spine1'])
    st.report_pass('test_case_passed', nodes['leaf0'])
    st.report_pass('test_case_passed', nodes['leaf1'])

def test_bgp_vrf_delete_vrf_instance():
    """
    Verify delete and add of vrf instance of same name has no impact
    """
    vars = st.get_testbed_vars()

    nodes = {}
    nodes['spine0'] = vars.D1
    nodes['spine1'] = vars.D2
    nodes['leaf0'] = vars.D3
    nodes['leaf1'] = vars.D4

    # Wrap disrupt (no router bgp + vrf del Vrf01) + check in try/finally.
    # If the 'Vrf01 still in show vrf' check raises, Vrf01 would otherwise
    # stay deleted for the rest of the run, breaking every subsequent test
    # in this module AND poisoning the next module (test_l2vni_v6_vtep.py
    # uses D3D2P1 which is bound to Vrf01 by bgp_basic_cfg.yaml setup).
    sonic_restore_cmds = ['sudo config vrf add Vrf01',
                          'sudo config interface vrf bind {} Vrf01'.format(vars.D3D2P1),
                          'sudo config interface ip add {} 20.1.1.1/24'.format(vars.D3D2P1)]
    frr_restore_cmds = ['router bgp 2002 vrf Vrf01',
        'no bgp ebgp-requires-policy',
        'no bgp network import-check',
        'neighbor 20.1.1.2 remote-as 1003',
        'neighbor 20.1.1.2 update-source 20.1.1.1',
        'neighbor 20.1.1.2 timers 3 10',
        'neighbor 20::2 remote-as 1003',
        'neighbor 20::2 update-source 20::1',
        'neighbor 20::2 timers 3 10',
        'address-family ipv4 unicast',
        'redistribute connected',
        'exit-address-family',
        'address-family ipv6 unicast',
        'neighbor 20::2 activate',
        'redistribute connected',
        'exit-address-family',
        'exit']
    try:
        cmd = 'no router bgp 2002 vrf Vrf01'
        config_frr(nodes['leaf0'], cmd)

        cmd = 'sudo config vrf del Vrf01'
        st.config(nodes['leaf0'], cmd)

        time.sleep(10)

        cmd = 'show vrf'
        cmd_output = st.config(nodes['leaf0'], cmd)
        if "Vrf01" in  str(cmd_output.encode('ascii','ignore')):
            st.report_fail("test_case_failed", nodes['leaf0'])
    finally:
        # SONiC re-add. Each step is guarded.
        for cleanup_cmd in sonic_restore_cmds:
            try:
                st.config(nodes['leaf0'], cleanup_cmd)
            except Exception as e:
                st.log("cleanup: '{}' on leaf0 failed: {}".format(cleanup_cmd, e))
        # IMPORTANT: the original test waited 10s here for the VRF kernel
        # device to finish coming up before configuring BGP against it.
        # Without this wait FRR's 'router bgp 2002 vrf Vrf01' races the
        # netlink VRF creation and can fail to establish the neighbor,
        # which then breaks the route-presence check below.
        time.sleep(10)
        st.show(nodes['leaf0'], 'sudo ping -I Vrf01 -c 5 {} -q'.format('20.1.1.2'),
                skip_tmpl=True, skip_error_check=True)
        # FRR BGP re-add (after the VRF interface is up).
        try:
            config_frr(nodes['leaf0'], frr_restore_cmds)
        except Exception as e:
            st.log("cleanup: restore Vrf01 BGP on leaf0 failed: {}".format(e))

    time.sleep(5)

    prefix_present = False
    cmd = 'show ip route vrf Vrf01'
    cmd_output = st.config(nodes['leaf0'], cmd)
    parsed_output = st.parse_show(nodes['leaf0'], cmd, cmd_output, 'show_ip_route.tmpl')
    for path in parsed_output:
        if path['type'] == 'B' and path['selected'] == '>' and path['ip_address'] == "192.168.1.3/32" and path['nexthop'] == "20.1.1.2":
            prefix_present = True

    if prefix_present != True:
        st.report_fail("test_case_failed", nodes['leaf0'])

    st.report_pass('test_case_passed', nodes['spine0'])
    st.report_pass('test_case_passed', nodes['spine1'])
    st.report_pass('test_case_passed', nodes['leaf0'])
    st.report_pass('test_case_passed', nodes['leaf1'])

def test_bgp_vrf_verify_route_map_change_in_vrf():
    """
    Verify that Changing route-map configurations(match/set clauses) on
    the fly it takes immediate effect.

    Inbound policy is applied asynchronously (FRR route-map delay + route
    refresh and peer UPDATEs); checks poll until the expected AS path appears.
    """
    vars = st.get_testbed_vars()

    nodes = {}
    nodes['spine0'] = vars.D1
    nodes['spine1'] = vars.D2
    nodes['leaf0'] = vars.D3
    nodes['leaf1'] = vars.D4

    # The original test had no cleanup at all - the ALLOW_PREFIX route-map,
    # the allow_list prefix-list and the 'neighbor 20.1.1.2 route-map ... in'
    # binding all persisted past the end of the test, prepending an AS to
    # every route from spine1. Wrap in try/finally and remove all three in
    # finally so subsequent tests (and the next module) see clean BGP policy.
    cleanup_cmds = ['router bgp 2002 vrf Vrf01',
                    'address-family ipv4 unicast',
                    'no neighbor 20.1.1.2 route-map ALLOW_PREFIX in',
                    'exit-address-family',
                    'exit',
                    'no route-map ALLOW_PREFIX permit 10',
                    'no ip prefix-list allow_list permit any']
    try:
        # Add route-map to prepend 9009 as for all incoming routes from 20.1.1.2 peer
        cmds = ['ip prefix-list allow_list permit any',
            'route-map ALLOW_PREFIX permit 10',
            'match ip address prefix-list allow_list',
            'set as-path prepend 9009',
            'router bgp 2002 vrf Vrf01',
            'address-family ipv4 unicast',
            'neighbor 20.1.1.2 route-map ALLOW_PREFIX in']

        config_frr(nodes['leaf0'], cmds)

        cmd = 'show bgp vrf Vrf01 ipv4 unicast neighbors 20.1.1.2 routes'
        if not _wait_bgp_vrf_neighbor_routes_contains(
            nodes['leaf0'],
            cmd,
            "9009 1003",
            diag_title="prepend 9009 not seen after poll",
        ):
            st.report_fail("test_case_failed", nodes['leaf0'])

        # configure to prepend AS value to 8008
        cmds = ['route-map ALLOW_PREFIX permit 10',
            'set as-path prepend 8008']

        config_frr(nodes['leaf0'], cmds)

        if not _wait_bgp_vrf_neighbor_routes_contains(
            nodes['leaf0'],
            cmd,
            "8008 1003",
            diag_title="prepend 8008 not seen after poll",
        ):
            st.report_fail("test_case_failed", nodes['leaf0'])
    finally:
        try:
            config_frr(nodes['leaf0'], cleanup_cmds)
        except Exception as e:
            st.log("cleanup: remove ALLOW_PREFIX route-map / allow_list on leaf0 failed: {}".format(e))

    st.report_pass('test_case_passed', nodes['spine0'])
    st.report_pass('test_case_passed', nodes['spine1'])
    st.report_pass('test_case_passed', nodes['leaf0'])
    st.report_pass('test_case_passed', nodes['leaf1'])

def test_bgp_vrf_verify_dynamic_imported_routes_adv_to_iBGP():
    """
    Verify that dynamically imported routes are further advertised
    to iBGP peers
    """
    vars = st.get_testbed_vars()

    nodes = {}
    nodes['spine0'] = vars.D1
    nodes['spine1'] = vars.D2
    nodes['leaf0'] = vars.D3
    nodes['leaf1'] = vars.D4

    # The original test never removed the 'import vrf Vrf01' it added to
    # spine1's router bgp 1004 vrf Vrf02 / address-family ipv4 - it persisted
    # for the rest of the run. Wrap in try/finally and undo it in finally.
    cleanup_cmds = ['router bgp 1004 vrf Vrf02',
                    'address-family ipv4 unicast',
                    'no import vrf Vrf01',
                    'exit-address-family',
                    'exit']
    try:
        # import Vrf01 vrf routes to Vrf02 of iBGP
        cmds = ['router bgp 1004 vrf Vrf02',
            'address-family ipv4 unicast',
            'import vrf Vrf01',
            'exit-address-family',
            'exit']

        config_frr(nodes['spine1'], cmds)

        time.sleep(2)

        prefix_present = False
        cmd = 'show ip route vrf Vrf02'
        cmd_output = st.config(nodes['leaf1'], cmd)
        parsed_output = st.parse_show(nodes['leaf1'], cmd, cmd_output, 'show_ip_route.tmpl')
        for path in parsed_output:
            if path['type'] == 'B' and path['selected'] == '>' and path['ip_address'] == "20.1.1.0/24" and path['nexthop'] == "30.1.1.1":
                prefix_present = True

        if prefix_present != True:
            st.report_fail("test_case_failed", nodes['leaf1'])
    finally:
        try:
            config_frr(nodes['spine1'], cleanup_cmds)
        except Exception as e:
            st.log("cleanup: 'no import vrf Vrf01' on spine1 failed: {}".format(e))

    st.report_pass('test_case_passed', nodes['spine0'])
    st.report_pass('test_case_passed', nodes['spine1'])
    st.report_pass('test_case_passed', nodes['leaf0'])
    st.report_pass('test_case_passed', nodes['leaf1'])

def test_bgp_verify_local_routes_as_bestpath_over_eBGP():
    """
    Verify that locally imported routes are selected as best path over eBGP imported
    """
    vars = st.get_testbed_vars()

    nodes = {}
    nodes['spine0'] = vars.D1
    nodes['spine1'] = vars.D2
    nodes['leaf0'] = vars.D3
    nodes['leaf1'] = vars.D4

    # check best path for 192.168.1.3 is set as 20.1.1.2
    prefix_present = False
    cmd = 'show ip route vrf Vrf01'
    cmd_output = st.show(nodes['leaf0'], "vtysh -c '{}'".format(cmd), skip_tmpl=True)
    parsed_output = st.parse_show(nodes['leaf0'], cmd, cmd_output, 'show_ip_route.tmpl')
    for path in parsed_output:
        if ((path['type'] == 'B') and (path['selected'] == '>') and (path['ip_address'] == "192.168.1.3/32") and (path['nexthop'] == "20.1.1.2")):
            prefix_present = True

    if prefix_present != True:
        st.report_fail("test_case_failed", nodes['leaf0'])

    # Original test only did 'vrf unbind Loopback4' as cleanup and didn't
    # remove the 192.168.1.3/32 IP first - that's wrong and on top of that
    # the cleanup wasn't in a finally block, so a mid-test st.report_fail
    # left Loopback4 bound to Vrf01 with 192.168.1.3/32 configured, which
    # collides with spine1's Loopback0 192.168.1.3/32 for the rest of the
    # run. Wrap in try/finally and remove IP-before-unbind.
    try:
        # Add a static route of IP learned through eBGP
        cmds = ['sudo config interface vrf bind Loopback4 Vrf01',
                'sudo config interface ip add Loopback4 192.168.1.3/32']

        for cmd in cmds:
            st.config(nodes['leaf0'], cmd)

        time.sleep(10)
        prefix_present = False

        # check BGP route is un-selected local route is seleted.
        cmd = 'show ip route vrf Vrf01'
        cmd_output = st.show(nodes['leaf0'], "vtysh -c '{}'".format(cmd), skip_tmpl=True)
        parsed_output = st.parse_show(nodes['leaf0'], cmd, cmd_output, 'show_ip_route.tmpl')
        for path in parsed_output:
            if ((path['type'] == 'C') and (path['selected'] == '>') and (path['ip_address'] == "192.168.1.3/32") and (path['nexthop'] != "20.1.1.2")):
                prefix_present = True

        if prefix_present != True:
            st.report_fail("test_case_failed", nodes['leaf0'])
    finally:
        for cleanup_cmd in [
            'sudo config interface ip rem Loopback4 192.168.1.3/32',
            'sudo config interface vrf unbind Loopback4',
        ]:
            try:
                st.config(nodes['leaf0'], cleanup_cmd)
            except Exception as e:
                st.log("cleanup: '{}' on leaf0 failed: {}".format(cleanup_cmd, e))

    st.report_pass('test_case_passed', nodes['spine0'])
    st.report_pass('test_case_passed', nodes['spine1'])
    st.report_pass('test_case_passed', nodes['leaf0'])
    st.report_pass('test_case_passed', nodes['leaf1'])

def test_bgp_vrf_bestpath_selection_algo_for_import():
    """
    Verify BGP best path selection algorithm works fine when
    routes are imported from default to Vrf01.
    """
    vars = st.get_testbed_vars()

    nodes = {}
    nodes['spine0'] = vars.D1
    nodes['spine1'] = vars.D2
    nodes['leaf0'] = vars.D3
    nodes['leaf1'] = vars.D4

    # check best path for 192.168.1.3 is set as 20.1.1.2
    cmd = 'show ip route vrf Vrf01'
    prefix_present = False
    cmd_output = st.show(nodes['leaf0'], "vtysh -c '{}'".format(cmd), skip_tmpl=True)
    parsed_output = st.parse_show(nodes['leaf0'], cmd, cmd_output, 'show_ip_route.tmpl')
    for path in parsed_output:
        if ((path['type'] == 'B') and (path['ip_address'] == "192.168.1.3/32") and (path['nexthop'] == "20.1.1.2")):
            prefix_present = True

    if prefix_present != True:
        st.report_fail("test_case_failed", nodes['leaf0'])

    # Original test removed the Loopback4 IP at the end but NEVER removed
    # the 'import vrf default' it added to router bgp 2002 vrf Vrf01. That
    # import leaked default-VRF routes into Vrf01 for every subsequent test.
    # On top of that, the 'ip rem Loopback4 ...' was outside any try/finally,
    # so a mid-test st.report_fail left the IP configured. Wrap in
    # try/finally and remove both in finally.
    frr_cleanup_cmds = ['router bgp 2002 vrf Vrf01',
                        'address-family ipv4 unicast',
                        'no import vrf default',
                        'exit-address-family',
                        'exit']
    try:
        # configure same loopback address which is learnt from BGP peer.
        cmd = 'sudo config interface ip add Loopback4 192.168.1.3/32'
        st.config(nodes['leaf0'], cmd)

        time.sleep(5)

        # import default vrf to Vrf01, check learned BGP peer is overwirtten with loopback addres
        cmds = ['router bgp 2002 vrf Vrf01',
                'address-family ipv4 unicast',
                'import vrf default']

        config_frr(nodes['leaf0'], cmds)

        prefix_present = False
        cmd = 'show ip route vrf Vrf01'
        cmd_output = st.config(nodes['leaf0'], cmd)
        parsed_output = st.parse_show(nodes['leaf0'], cmd, cmd_output, 'show_ip_route.tmpl')
        for path in parsed_output:
            if path['type'] == 'B' and  path['ip_address'] == "192.168.1.3/32" and path['interface'] == "Loopback4":
                prefix_present = True

        if prefix_present != True:
            st.report_fail("test_case_failed", nodes['leaf0'])

        # verify that all vrf instances fall back
        #     to backup path, if primary link goes down.

        # remove loopback address, check it fallback to BGP route
        cmd = 'sudo config interface ip rem Loopback4 192.168.1.3/32'
        st.config(nodes['leaf0'], cmd)

        time.sleep(5)

        # Route will fall back to backup path
        prefix_present = False
        cmd = 'show ip route vrf Vrf01'
        cmd_output = st.config(nodes['leaf0'], cmd)
        parsed_output = st.parse_show(nodes['leaf0'], cmd, cmd_output, 'show_ip_route.tmpl')
        for path in parsed_output:
            if ((path['type'] == 'B') and (path['ip_address'] == "192.168.1.3/32") and (path['nexthop'] == "20.1.1.2")):
                prefix_present = True

        if prefix_present != True:
            st.report_fail("test_case_failed", nodes['leaf0'])
    finally:
        # Idempotent: 'ip rem' is a no-op if the IP was already removed in
        # the success path. 'no import vrf default' undoes the leak.
        try:
            st.config(nodes['leaf0'], 'sudo config interface ip rem Loopback4 192.168.1.3/32')
        except Exception as e:
            st.log("cleanup: 'ip rem Loopback4 192.168.1.3/32' on leaf0 failed: {}".format(e))
        try:
            config_frr(nodes['leaf0'], frr_cleanup_cmds)
        except Exception as e:
            st.log("cleanup: 'no import vrf default' on leaf0 failed: {}".format(e))

    st.report_pass('test_case_passed', nodes['spine0'])
    st.report_pass('test_case_passed', nodes['spine1'])
    st.report_pass('test_case_passed', nodes['leaf0'])
    st.report_pass('test_case_passed', nodes['leaf1'])

def test_bgp_vrf_verify_ecmp_on_vrf():
    """
    Verify ECMP for imported routes from different VRFs, check with max-paths <>
    and choosing the path limit based on that number
    """
    vars = st.get_testbed_vars()

    nodes = {}
    nodes['spine0'] = vars.D1
    nodes['spine1'] = vars.D2
    nodes['leaf0'] = vars.D3
    nodes['leaf1'] = vars.D4

    # Original test added 'import vrf default' and 'maximum-paths 1' to
    # router bgp 2002 vrf Vrf01 / address-family ipv4 and never removed
    # either. After this test, Vrf01 stays with default-VRF imports and
    # ECMP capped at 1 path for the rest of the run. Wrap in try/finally
    # and undo both.
    cleanup_cmds = ['router bgp 2002 vrf Vrf01',
                    'address-family ipv4 unicast',
                    'no maximum-paths',
                    'no import vrf default',
                    'exit-address-family',
                    'exit']
    try:
        # import default vrf to Vrf01, check learned BGP peer is overwirtten with loopback addres
        cmds = ['router bgp 2002 vrf Vrf01',
                'address-family ipv4 unicast',
                'import vrf default']

        config_frr(nodes['leaf0'], cmds)

        time.sleep(20)

        cmd = 'show ip route vrf Vrf01 192.168.1.1'
        cmd_output = st.config(nodes['leaf0'], cmd)

        if str(cmd_output.encode('ascii','ignore')).count(', via') == 2:
            st.log("ECMP has two path to reach 192.168.1.1")
        else:
            st.report_fail("test_case_failed", nodes['leaf0'])

        #check both nexthop are present in route table
        if ('* 10.1.1.1, via {}'.format(vars.D3D1P1) not in str(cmd_output.encode('ascii','ignore')) or
            'via {}'.format(vars.D3D1P2) not in str(cmd_output.encode('ascii','ignore'))):
            st.report_fail("test_case_failed", nodes['leaf0'])

        # configure ECMP to select one path
        cmds = ['router bgp 2002 vrf Vrf01',
                'address-family ipv4 unicast',
                'maximum-paths 1']

        config_frr(nodes['leaf0'], cmds)

        time.sleep(20)

        cmd = 'show ip route vrf Vrf01 192.168.1.1'
        cmd_output = st.config(nodes['leaf0'], cmd)

        if str(cmd_output.encode('ascii','ignore')).count(', via') > 1:
            st.report_fail("test_case_failed", nodes['leaf0'])
    finally:
        try:
            config_frr(nodes['leaf0'], cleanup_cmds)
        except Exception as e:
            st.log("cleanup: 'no maximum-paths / no import vrf default' on leaf0 failed: {}".format(e))

    st.report_pass('test_case_passed', nodes['spine0'])
    st.report_pass('test_case_passed', nodes['spine1'])
    st.report_pass('test_case_passed', nodes['leaf0'])
    st.report_pass('test_case_passed', nodes['leaf1'])

def test_bgp_vrf_changing_vrf_locally():
    """
    Verify VRF name is locally significant, delete existing VRF and
    add VRF with different name
    """
    vars = st.get_testbed_vars()

    nodes = {}
    nodes['spine0'] = vars.D1
    nodes['spine1'] = vars.D2
    nodes['leaf0'] = vars.D3
    nodes['leaf1'] = vars.D4

    # This test deletes Vrf01, creates Vrf10, then tears down Vrf10 and
    # rebuilds Vrf01 from scratch. The original rebuild was at the bottom of
    # the function with no try/finally - so any mid-test st.report_fail (or
    # parse_show exception) left Vrf01 deleted AND possibly Vrf10 lingering,
    # bricking leaf0 for every test that follows (including the next module
    # test_l2vni_v6_vtep.py which expects D3D2P1 in the default VRF without
    # any IPv4 address, but ALSO depends on bgp_basic_cfg.yaml's leaf0 setup
    # having put D3D2P1 into Vrf01 - so we MUST leave Vrf01 in the YAML's
    # expected state before module teardown runs).
    #
    # The finally block below is idempotent: each step is guarded so a single
    # partial-state failure does not skip the rest of the unwind.
    vrf10_sonic_cleanup = ['sudo config vrf del Vrf10']
    vrf01_sonic_rebuild = [
        'sudo config vrf add Vrf01',
        'sudo config interface vrf bind {} Vrf01'.format(vars.D3D2P1),
        'sudo config interface vrf bind Loopback1 Vrf01',
        'sudo config interface vrf bind {} Vrf01'.format(vars.D3D2P2),
        'sudo config interface vrf bind Loopback3 Vrf01',
        'sudo config interface ip add {} 20.1.1.1/24'.format(vars.D3D2P1),
        'sudo config interface ip add {} 20::1/64'.format(vars.D3D2P2),
        'sudo config interface ip add Loopback1 192.168.0.2/32',
        'sudo config interface ip add Loopback3 192::1:2/128'
    ]
    vrf01_bgp_rebuild = [
        'router bgp 2002 vrf Vrf01',
        'no bgp ebgp-requires-policy',
        'no bgp network import-check',
        'neighbor 20.1.1.2 remote-as 1003',
        'neighbor 20.1.1.2 update-source 20.1.1.1',
        'neighbor 20.1.1.2 timers 3 10',
        'neighbor 20::2 remote-as 1003',
        'neighbor 20::2 update-source 20::1',
        'neighbor 20::2 timers 3 10',
        'address-family ipv4 unicast',
        'redistribute connected',
        'exit-address-family',
        'address-family ipv6 unicast',
        'neighbor 20::2 activate',
        'redistribute connected',
        'exit-address-family',
        'exit'
    ]
    try:
        cmd = 'no router bgp 2002 vrf Vrf01'
        config_frr(nodes['leaf0'], cmd)

        time.sleep(1)

        cmd = 'sudo config vrf del Vrf01'
        st.config(nodes['leaf0'], cmd)

        time.sleep(10)

        cmd = 'show vrf'
        cmd_output = st.config(nodes['leaf0'], cmd)
        if "Vrf01" in  str(cmd_output.encode('ascii','ignore')):
            st.report_fail("test_case_failed", nodes['leaf0'])

        cmds = ['sudo config vrf add Vrf10',
                'sudo config interface vrf bind {} Vrf10'.format(vars.D3D2P1),
                'sudo config interface ip add {} 20.1.1.1/24'.format(vars.D3D2P1)]
        for cmd in cmds:
            st.config(nodes['leaf0'], cmd)

        # deleting a vrf and adding different vrf for an interface is
        # taking time for an interface to come up. Added a precautionary time check to avoid
        # failure of testcase, can be optimised.
        cmd = 'show ip interface'
        for i in range (0, 12, 1):
            cmd_output = st.config(nodes['leaf0'], cmd)
            if "Vrf10" not in  str(cmd_output.encode('ascii','ignore')):
                time.sleep(10)
            else:
                break

        cmd = 'show ip interface'
        cmd_output = st.config(nodes['leaf0'], cmd)
        if "Vrf10" not in  str(cmd_output.encode('ascii','ignore')):
            st.report_fail("test_case_failed", nodes['leaf0'])

        st.show(nodes['leaf0'], 'sudo ping -I Vrf10 -c 5 {} -q'.format('20.1.1.2'), skip_tmpl=True, skip_error_check=True)

        cmds = [
            'router bgp 2002 vrf Vrf10',
            'no bgp ebgp-requires-policy',
            'no bgp network import-check',
            'neighbor 20.1.1.2 remote-as 1003',
            'neighbor 20.1.1.2 update-source 20.1.1.1',
            'neighbor 20.1.1.2 timers 3 10',
            'neighbor 20::2 remote-as 1003',
            'neighbor 20::2 update-source 20::1',
            'neighbor 20::2 timers 3 10',
            'address-family ipv4 unicast',
            'redistribute connected',
            'exit-address-family',
            'address-family ipv6 unicast',
            'neighbor 20::2 activate',
            'redistribute connected',
            'exit-address-family'
        ]

        config_frr(nodes['leaf0'], cmds)

        time.sleep(10)
        cmd = 'show ip route vrf Vrf10'

        # Wait for DF election to complete
        prefix_present = False
        for i in range(0, 5):
            prefix_present = False
            cmd_output = st.config(nodes['leaf0'], cmd)
            parsed_output = st.parse_show(nodes['leaf0'], cmd, cmd_output, 'show_ip_route.tmpl')
            for path in parsed_output:
                if path['type'] == 'B' and path['selected'] == '>' and path['ip_address'] == "192.168.1.3/32":
                    prefix_present = True

            if prefix_present == True:
                st.log("DF election completed successfully")
                break
            st.log("Waiting for DF election to complete, retrying in 10 seconds")
            time.sleep(10)

        if prefix_present != True:
            st.report_fail("test_case_failed", nodes['leaf0'])
    finally:
        # 1) Drop Vrf10's BGP from FRR (if still present).
        try:
            config_frr(nodes['leaf0'], 'no router bgp 2002 vrf Vrf10')
        except Exception as e:
            st.log("cleanup: 'no router bgp 2002 vrf Vrf10' on leaf0 failed: {}".format(e))
        # Poll for bgpd to actually drop the Vrf10 instance before we ask
        # SONiC to delete the VRF. zebra can retain VRF/interface bindings
        # for several seconds after `no router bgp ... vrf Vrf10`; with a
        # naked `time.sleep(1)` here, `sudo config vrf del Vrf10` can fail
        # or no-op and leave Vrf10 residue on D3 - and Vrf10 is NOT covered
        # by the YAML module teardown (only Vrf01 is), so the residue would
        # cascade. Logged-only on timeout: the unwind continues either way
        # so the subsequent Vrf01 rebuild still gets a chance to run.
        _wait_for_frr_bgp_vrf_drained(nodes['leaf0'], 'Vrf10')
        # 2) Drop Vrf10 from SONiC (idempotent: no-op if it was never created).
        for cleanup_cmd in vrf10_sonic_cleanup:
            try:
                st.config(nodes['leaf0'], cleanup_cmd)
            except Exception as e:
                st.log("cleanup: '{}' on leaf0 failed: {}".format(cleanup_cmd, e))
        time.sleep(10)
        # 3) Rebuild Vrf01 in SONiC. Each step is guarded; some may fail
        #    with 'already exists' if the success path already created them,
        #    which is fine.
        for cleanup_cmd in vrf01_sonic_rebuild:
            try:
                st.config(nodes['leaf0'], cleanup_cmd)
            except Exception as e:
                st.log("cleanup: '{}' on leaf0 failed: {}".format(cleanup_cmd, e))
        time.sleep(10)
        # 4) Rebuild Vrf01 BGP in FRR. The bgp_basic_cfg.yaml deconfig in the
        #    module teardown expects 'no router bgp 2002 vrf Vrf01' to find an
        #    instance, so without this rebuild the teardown can leave residue.
        try:
            config_frr(nodes['leaf0'], vrf01_bgp_rebuild)
        except Exception as e:
            st.log("cleanup: rebuild Vrf01 BGP on leaf0 failed: {}".format(e))

    st.report_pass('test_case_passed', nodes['spine0'])
    st.report_pass('test_case_passed', nodes['spine1'])
    st.report_pass('test_case_passed', nodes['leaf0'])
    st.report_pass('test_case_passed', nodes['leaf1'])
