"""
Cisco VXR SIM-only helper for test_static_route.py.

wait_for_traffic_ready() verifies four conditions before allowing a traffic
test to run.  This is needed because after warmboot the VS bridge takes
~120-170 s to stabilize and deliver ARP broadcasts to PTF interfaces, and
routeorch may still be updating ECMP nexthop groups with new RIF OIDs even
after ARP is REACHABLE.

Injected by infra/sim_patches/add_sim_hooks.py (--static_route_wait).
NOT upstreamed – applies only to Cisco VXR SIM via add_sim_hooks.
"""

import logging

from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import wait_until


def wait_for_traffic_ready(duthost, prefix, nexthop_addrs, ipv6=False, label="",
                           ptfadapter=None, tbinfo=None, ip_dst=None, nexthop_devs=None):
    """
    Wait until the DUT is fully ready to forward traffic for the given static
    route.  Four conditions must hold simultaneously:

      1. ARP/NDP resolved (kernel neighbor REACHABLE/STALE) for every nexthop
      2. Static route programmed in the ASIC (ASIC_DB ROUTE_ENTRY present)
      3. Every nexthop has an ASIC neighbor entry (ASIC_DB NEIGHBOR_ENTRY)
      4. End-to-end traffic forwarding works (packet arrives at expected PTF ports)
         — only checked when ptfadapter/tbinfo/ip_dst/nexthop_devs are provided.
         Catches VS bridge veth delivery failures that software state checks miss.

    Needed because after warmboot:
      - VS bridge takes ~120-170 s to stabilize for Vlan1000 ARP
      - routeorch may still be updating ECMP nexthop groups (wrong port until done)
      - The software state (ASIC_DB) can look correct while veth delivery is broken

    Args:
        duthost:       DUT host object
        prefix:        Static route prefix e.g. "5.5.5.0/24"
        nexthop_addrs: List of nexthop IPs
        ipv6:          True for IPv6 (NDP), False for IPv4 (ARP)
        label:         Tag for log messages ("pre-op", "post-op", etc.)
        ptfadapter:    PTF adapter (optional, enables check 4)
        tbinfo:        Testbed info dict (optional, enables check 4)
        ip_dst:        Destination IP for traffic probe (optional, enables check 4)
        nexthop_devs:  Expected PTF port indices (optional, enables check 4)
    """
    tag = "[{}] ".format(label) if label else ""
    net_addr = prefix.split('/')[0]
    ping_cmd = "timeout 2 ping{} -c 1 -w 2 {{}}".format(" -6" if ipv6 else "")
    do_traffic_probe = all(x is not None
                           for x in [ptfadapter, tbinfo, ip_dst, nexthop_devs])

    # Flush ARP/NDP ONCE before the polling loop so the kernel gets clean
    # entries.  Flushing INSIDE _ready() would tear down the ECMP nexthop
    # group on every 10-second retry: orchagent deletes nexthops when
    # neighbors are removed, and the ECMP group rebuild takes time — so
    # the traffic probe would always run against a partially-built group.
    duthost.shell("sonic-clear {}".format("ndp" if ipv6 else "arp"),
                  module_ignore_errors=True)
    for nh in nexthop_addrs:
        duthost.shell(ping_cmd.format(nh), module_ignore_errors=True)

    def _ready():
        # 1. Check kernel neighbor state — do NOT flush here
        for nh in nexthop_addrs:
            state = duthost.shell(
                "ip{} neigh show | grep '{}'".format(" -6" if ipv6 else "", nh),
                module_ignore_errors=True
            )["stdout"]
            if not any(s in state for s in
                       ("REACHABLE", "STALE", "DELAY", "PROBE")):
                logging.debug("%sARP/NDP not resolved: %s -> %s",
                              tag, nh, state.strip() or "<not found>")
                return False

        # 2. Check ASIC route entry
        asic_route = duthost.shell(
            "sonic-db-cli ASIC_DB KEYS "
            "'ASIC_STATE:SAI_OBJECT_TYPE_ROUTE_ENTRY*' | grep -F '{}'".format(net_addr),
            module_ignore_errors=True
        )["stdout"].strip()
        if not asic_route:
            logging.debug("%sASIC route for %s not yet programmed", tag, prefix)
            return False

        # 3. Check ASIC neighbor entries
        for nh in nexthop_addrs:
            asic_neigh = duthost.shell(
                "sonic-db-cli ASIC_DB KEYS "
                "'ASIC_STATE:SAI_OBJECT_TYPE_NEIGHBOR_ENTRY*' | grep -F '{}'".format(nh),
                module_ignore_errors=True
            )["stdout"].strip()
            if not asic_neigh:
                logging.debug("%sASIC neighbor for %s not yet programmed", tag, nh)
                return False

        # 4. End-to-end traffic probe (catches veth delivery failures and
        #    routeorch ECMP nexthop group updates with wrong RIF OIDs that
        #    software-only checks cannot detect).
        if do_traffic_probe:
            try:
                from tests.route.test_static_route import generate_and_verify_traffic
                generate_and_verify_traffic(
                    duthost, ptfadapter, tbinfo, ip_dst, nexthop_devs, ipv6=ipv6)
                logging.info("%sAll ready – ARP, ASIC, and end-to-end traffic "
                             "verified for %s nexthops %s",
                             tag, prefix, nexthop_addrs)
            except Exception as e:
                logging.debug("%sTraffic probe failed (will retry): %s", tag, e)
                return False
        else:
            logging.info("%sARP + ASIC state ready for %s nexthops %s "
                         "(no traffic probe – ptfadapter not provided)",
                         tag, prefix, nexthop_addrs)

        return True

    pytest_assert(
        wait_until(600, 10, 0, _ready),
        "{}Not ready within 600 s (prefix={}, nexthops={})".format(
            tag, prefix, nexthop_addrs)
    )
