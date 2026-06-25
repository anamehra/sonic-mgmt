#! /usr/bin/env python3
"""SpyTest script for ACL DSCP rewrite verification with VxLAN traffic."""

import json

import pytest

from spytest import st, tgapi, SpyTestDict

import apis.routing.ip as ip_obj
import apis.system.basic as basic_obj

data = SpyTestDict()

# ACL constants
TABLE_V4 = "OVERLAY_MARK_META_TEST_V4"
TABLE_V6 = "OVERLAY_MARK_META_TEST_V6"
EXPECTED_DSCP = 40

# VxLAN constants
TUNNEL_V4 = "tunnel_v4"
TUNNEL_V6 = "tunnel_v6"
VNET_V4 = "Vnet_dscp_v4"
VNET_V6 = "Vnet_dscp_v6"
VNI_V4 = 10000
VNI_V6 = 20000
VXLAN_PORT = 4789
OVERLAY_DMAC = "25:35:45:55:65:75"

# Underlay addressing
LOOPBACK_IP = "10.1.0.32"
LOOPBACK_IPV6 = "2001:db8:1::32"
NEXTHOP_ENDPOINT_V4 = "100.0.0.1"
NEXTHOP_ENDPOINT_V6 = "2001:db8:64::1"

# Overlay destinations (one per scenario)
DEST_V4_IN_V4 = "150.0.0.1"
DEST_V6_IN_V4 = "2001:150::1"
DEST_V4_IN_V6 = "151.0.0.1"
DEST_V6_IN_V6 = "2001:151::1"

# TG port addresses
TG_SRC_MAC = "00:0a:01:00:00:01"
TG_DST_MAC = "00:0a:01:00:11:02"
INGRESS_TG_IP = "1.1.1.2"
INGRESS_DUT_IP = "1.1.1.1"
INGRESS_TG_IPV6 = "1::2"
INGRESS_DUT_IPV6 = "1::1"
EGRESS_TG_IP = "2.2.2.2"
EGRESS_DUT_IP = "2.2.2.1"
EGRESS_TG_IPV6 = "2::2"
EGRESS_DUT_IPV6 = "2::1"


SCENARIOS = [
    {
        "name": "v4_in_v4",
        "inner": "v4",
        "outer": "v4",
        "vnet": VNET_V4,
        "vni": VNI_V4,
        "dest": DEST_V4_IN_V4,
        "mask": 32,
        "endpoint": NEXTHOP_ENDPOINT_V4,
        "table": TABLE_V4,
        "rule_name": "RULE_V4_IN_V4",
        "rule_field": "DST_IP",
    },
    {
        "name": "v6_in_v4",
        "inner": "v6",
        "outer": "v4",
        "vnet": VNET_V4,
        "vni": VNI_V4,
        "dest": DEST_V6_IN_V4,
        "mask": 128,
        "endpoint": NEXTHOP_ENDPOINT_V4,
        "table": TABLE_V6,
        "rule_name": "RULE_V6_IN_V4",
        "rule_field": "DST_IPV6",
    },
    {
        "name": "v4_in_v6",
        "inner": "v4",
        "outer": "v6",
        "vnet": VNET_V6,
        "vni": VNI_V6,
        "dest": DEST_V4_IN_V6,
        "mask": 32,
        "endpoint": NEXTHOP_ENDPOINT_V6,
        "table": TABLE_V4,
        "rule_name": "RULE_V4_IN_V6",
        "rule_field": "DST_IP",
    },
    {
        "name": "v6_in_v6",
        "inner": "v6",
        "outer": "v6",
        "vnet": VNET_V6,
        "vni": VNI_V6,
        "dest": DEST_V6_IN_V6,
        "mask": 128,
        "endpoint": NEXTHOP_ENDPOINT_V6,
        "table": TABLE_V6,
        "rule_name": "RULE_V6_IN_V6",
        "rule_field": "DST_IPV6",
    },
]


# Shared utilities

IP_FAMILY = {"v4": "ipv4", "v6": "ipv6"}


def traffic_ip(family, role):
    """Return TG IP for role 'ingress_src' or 'egress_dst'."""
    if role == "ingress_src":
        return INGRESS_TG_IP if family == "v4" else INGRESS_TG_IPV6
    return EGRESS_TG_IP if family == "v4" else EGRESS_TG_IPV6


# Topology and routing

def initialize_topology():
    """Initialize 1 DUT and 2 TG ports."""
    vars = st.ensure_min_topology("D1T1:2")
    data.dut = vars.D1
    data.dut_tg_port1 = vars.D1T1P1
    data.dut_tg_port2 = vars.D1T1P2

    tg1, tg_ph_1 = tgapi.get_handle_byname("T1D1P1")
    tg2, tg_ph_2 = tgapi.get_handle_byname("T1D1P2")
    data.tg1 = tg1
    data.tg2 = tg2
    data.tg_ph_1 = tg_ph_1
    data.tg_ph_2 = tg_ph_2

    tg1.tg_traffic_control(action="reset", port_handle=tg_ph_1)
    tg2.tg_traffic_control(action="reset", port_handle=tg_ph_2)

    data.dut_mac = basic_obj.get_ifconfig_ether(data.dut, data.dut_tg_port1)
    data.ports = [data.dut_tg_port1, data.dut_tg_port2]


def configure_routing():
    """Configure v4/v6 routing TG port1 -> DUT -> TG port2."""
    dut = data.dut

    ip_obj.config_ip_addr_interface(dut, data.dut_tg_port1, INGRESS_DUT_IP, 24,
                                    family="ipv4", config='add')
    st.config(dut, "sudo arp -s {} {}".format(INGRESS_TG_IP, TG_SRC_MAC))

    ip_obj.config_ip_addr_interface(dut, data.dut_tg_port2, EGRESS_DUT_IP, 24,
                                    family="ipv4", config='add')
    st.config(dut, "sudo arp -s {} {}".format(EGRESS_TG_IP, TG_DST_MAC))

    ip_obj.create_static_route(dut, EGRESS_TG_IP,
                               "{}/32".format(NEXTHOP_ENDPOINT_V4),
                               shell="vtysh", family="ipv4")

    ip_obj.config_ip_addr_interface(dut, data.dut_tg_port1, INGRESS_DUT_IPV6, 64,
                                    family="ipv6", config='add')
    st.config(dut, "sudo ip -6 neigh replace {} lladdr {} dev {}".format(
        INGRESS_TG_IPV6, TG_SRC_MAC, data.dut_tg_port1))

    ip_obj.config_ip_addr_interface(dut, data.dut_tg_port2, EGRESS_DUT_IPV6, 64,
                                    family="ipv6", config='add')
    st.config(dut, "sudo ip -6 neigh replace {} lladdr {} dev {}".format(
        EGRESS_TG_IPV6, TG_DST_MAC, data.dut_tg_port2))

    ip_obj.create_static_route(dut, EGRESS_TG_IPV6,
                               "{}/128".format(NEXTHOP_ENDPOINT_V6),
                               shell="vtysh", family="ipv6")

    st.config(dut, "sudo config interface ip add Loopback0 {}/32".format(LOOPBACK_IP))
    st.config(dut, "sudo config interface ip add Loopback0 {}/128".format(LOOPBACK_IPV6))


def unconfigure_routing():
    """Remove routing configuration."""
    dut = data.dut
    ip_obj.delete_static_route(dut, EGRESS_TG_IP,
                               "{}/32".format(NEXTHOP_ENDPOINT_V4),
                               shell="vtysh", family="ipv4")
    ip_obj.config_ip_addr_interface(dut, data.dut_tg_port1, INGRESS_DUT_IP, 24,
                                    family="ipv4", config='remove')
    ip_obj.config_ip_addr_interface(dut, data.dut_tg_port2, EGRESS_DUT_IP, 24,
                                    family="ipv4", config='remove')
    ip_obj.delete_static_route(dut, EGRESS_TG_IPV6,
                               "{}/128".format(NEXTHOP_ENDPOINT_V6),
                               shell="vtysh", family="ipv6")
    ip_obj.config_ip_addr_interface(dut, data.dut_tg_port1, INGRESS_DUT_IPV6, 64,
                                    family="ipv6", config='remove')
    ip_obj.config_ip_addr_interface(dut, data.dut_tg_port2, EGRESS_DUT_IPV6, 64,
                                    family="ipv6", config='remove')
    st.config(dut, "sudo config interface ip remove Loopback0 {}/32 || true".format(LOOPBACK_IP))
    st.config(dut, "sudo config interface ip remove Loopback0 {}/128 || true".format(LOOPBACK_IPV6))


# VxLAN helpers

def configure_vxlan():
    """Configure VxLAN tunnels, VNETs, and per-scenario routes."""
    dut = data.dut

    switch_config = json.dumps([{
        "SWITCH_TABLE:switch": {
            "vxlan_port": str(VXLAN_PORT),
            "vxlan_router_mac": data.dut_mac
        },
        "OP": "SET"
    }])
    st.config(dut, "echo '{}' | docker exec -i swss swssconfig /dev/stdin".format(switch_config))
    st.wait(2)

    tunnel_config = json.dumps({
        "VXLAN_TUNNEL": {
            TUNNEL_V4: {"src_ip": LOOPBACK_IP},
            TUNNEL_V6: {"src_ip": LOOPBACK_IPV6},
        }
    })
    st.config(dut, "echo '{}' > /tmp/vxlan_tunnel.json && sudo config load /tmp/vxlan_tunnel.json -y".format(
        tunnel_config))
    st.wait(2)

    vnets = {}
    for vnet_name, tunnel_name, vni in ((VNET_V4, TUNNEL_V4, VNI_V4), (VNET_V6, TUNNEL_V6, VNI_V6)):
        vnets[vnet_name] = {
            "vxlan_tunnel": tunnel_name,
            "scope": "default",
            "vni": str(vni),
            "peer_list": "",
            "advertise_prefix": "false",
            "overlay_dmac": OVERLAY_DMAC,
        }
    vnet_config = json.dumps({"VNET": vnets})
    st.config(dut, "echo '{}' > /tmp/vnet.json && sudo config load /tmp/vnet.json -y".format(vnet_config))
    st.wait(2)

    route_entries = []
    for scn in SCENARIOS:
        route_key = "VNET_ROUTE_TUNNEL_TABLE:{}:{}/{}".format(
            scn["vnet"], scn["dest"], scn["mask"])
        route_entries.append({
            route_key: {
                "endpoint": scn["endpoint"],
                "vni": str(scn["vni"]),
                "mac_address": OVERLAY_DMAC,
                "profile": "",
            },
            "OP": "SET",
        })
    route_config = json.dumps(route_entries)
    st.config(dut, "echo '{}' | docker exec -i swss swssconfig /dev/stdin".format(route_config))
    st.wait(3)
    st.log("VxLAN tunnels (v4+v6), VNETs, and {} routes configured".format(len(SCENARIOS)))


def unconfigure_vxlan():
    """Remove VxLAN configuration."""
    dut = data.dut
    route_dels = []
    for scn in SCENARIOS:
        route_key = "VNET_ROUTE_TUNNEL_TABLE:{}:{}/{}".format(
            scn["vnet"], scn["dest"], scn["mask"])
        route_dels.append({
            route_key: {"endpoint": scn["endpoint"], "profile": ""},
            "OP": "DEL",
        })
    st.config(dut, "echo '{}' | docker exec -i swss swssconfig /dev/stdin".format(json.dumps(route_dels)))
    st.wait(2)

    st.config(dut, "redis-cli -n 4 DEL 'VNET|{}'".format(VNET_V4))
    st.config(dut, "redis-cli -n 4 DEL 'VNET|{}'".format(VNET_V6))
    st.config(dut, "redis-cli -n 4 DEL 'VXLAN_TUNNEL|{}'".format(TUNNEL_V4))
    st.config(dut, "redis-cli -n 4 DEL 'VXLAN_TUNNEL|{}'".format(TUNNEL_V6))
    st.wait(2)


# ACL helpers

def create_acl_table(table_name, table_type, policy_desc, ports):
    """Create ACL table in CONFIG_DB."""
    ports_str = ",".join(ports)
    cmd = ("redis-cli -n 4 HSET 'ACL_TABLE|{}' "
           "'type' '{}' 'stage' 'ingress' 'ports@' '{}' 'policy_desc' '{}'").format(
        table_name, table_type, ports_str, policy_desc)
    st.config(data.dut, cmd)


def delete_acl_table(table_name):
    """Delete ACL table from CONFIG_DB."""
    st.config(data.dut, "redis-cli -n 4 DEL 'ACL_TABLE|{}'".format(table_name))
    st.wait(2)


def create_acl_rule(table_name, rule_name, fields):
    """Create ACL rule in CONFIG_DB."""
    rule_key = "ACL_RULE|{}|{}".format(table_name, rule_name)
    cmd_parts = ["'{}' '{}'".format(k, v) for k, v in fields.items()]
    cmd = "redis-cli -n 4 HSET '{}' {}".format(rule_key, " ".join(cmd_parts))
    st.config(data.dut, cmd)


def delete_acl_rule(table_name, rule_name):
    """Delete ACL rule from CONFIG_DB."""
    st.config(data.dut, "redis-cli -n 4 DEL 'ACL_RULE|{}|{}'".format(table_name, rule_name))
    st.wait(2)


def verify_acl_status(state_key, label, expected_status="Active", retries=6, interval=5):
    """Poll ACL status in STATE_DB with retries."""
    for attempt in range(retries):
        status = st.config(data.dut, "redis-cli -n 6 hget '{}' 'status'".format(state_key))
        if expected_status in str(status):
            return True
        if attempt < retries - 1:
            st.wait(interval)
    st.log("ERROR: ACL {} not {} after {} attempts".format(label, expected_status, retries))
    return False


def setup_acl(table_specs, rule_builder, ports):
    created_tables = []
    created_rules = []
    for name, ttype, desc in table_specs:
        create_acl_table(name, ttype, desc, ports)
        created_tables.append(name)
    for scn in SCENARIOS:
        spec = rule_builder(scn)
        if spec is None:
            continue
        # Single 3-tuple or iterable of (table, rule_name, fields) tuples.
        if (isinstance(spec, tuple) and len(spec) == 3
                and isinstance(spec[2], dict)):
            specs = [spec]
        else:
            specs = list(spec)
        for tbl, rule_name, fields in specs:
            create_acl_rule(tbl, rule_name, fields)
            created_rules.append((tbl, rule_name))
    return created_tables, created_rules


def verify_acl_setup_active(tables, rules):
    """Verify tables/rules are Active; dump show acl and clear counters."""
    ok = True
    for tbl in tables:
        if not verify_acl_status(
                "ACL_TABLE_TABLE|{}".format(tbl), "table {}".format(tbl)):
            ok = False
    for tbl, rule in rules:
        if not verify_acl_status(
                "ACL_RULE_TABLE|{}|{}".format(tbl, rule),
                "rule {}|{}".format(tbl, rule)):
            ok = False
    if ok and tables:
        st.log("---- show acl table ----\n{}".format(
            st.show(data.dut, "show acl table", skip_tmpl=True)))
        st.log("---- show acl rule ----\n{}".format(
            st.show(data.dut, "show acl rule", skip_tmpl=True)))
        st.config(data.dut, "sudo counterpoll acl enable || true")
        st.config(data.dut, "aclshow -c || true")
    return ok


def cleanup_acl(tables, rules):
    """Delete ACL rules then tables."""
    for tbl, rule in rules:
        delete_acl_rule(tbl, rule)
    for tbl in tables:
        delete_acl_table(tbl)


# Traffic helpers

def parse_ip_dscp(frame_data, eth_offset=0):
    """Parse DSCP from IPv4/IPv6 at eth_offset. Returns (family, dscp, tc)."""
    if len(frame_data) < eth_offset + 16:
        return (None, None, None)
    eth_type = ((int(frame_data[eth_offset + 12], 16) << 8)
                | int(frame_data[eth_offset + 13], 16))
    ip_off = eth_offset + 14
    if eth_type == 0x0800:
        if len(frame_data) < ip_off + 2:
            return (None, None, None)
        tos = int(frame_data[ip_off + 1], 16)
        return ("ipv4", tos >> 2, tos)
    if eth_type == 0x86DD:
        if len(frame_data) < ip_off + 2:
            return (None, None, None)
        b0 = int(frame_data[ip_off], 16)
        b1 = int(frame_data[ip_off + 1], 16)
        tc = ((b0 & 0x0F) << 4) | ((b1 >> 4) & 0x0F)
        return ("ipv6", tc >> 2, tc)
    return (None, None, None)


def is_vxlan_frame(frame_data):
    """Return (is_vxlan, outer_family, vxlan_off, inner_off)."""
    outer_family, _, _ = parse_ip_dscp(frame_data, eth_offset=0)
    if outer_family == "ipv4":
        if len(frame_data) < 14 + 20 + 8 + 8:
            return (False, None, None, None)
        if int(frame_data[23], 16) != 0x11:
            return (False, None, None, None)
        udp_off = 14 + 20
    elif outer_family == "ipv6":
        if len(frame_data) < 14 + 40 + 8 + 8:
            return (False, None, None, None)
        if int(frame_data[20], 16) != 0x11:
            return (False, None, None, None)
        udp_off = 14 + 40
    else:
        return (False, None, None, None)

    udp_dport = (int(frame_data[udp_off + 2], 16) << 8) | int(frame_data[udp_off + 3], 16)
    if udp_dport != VXLAN_PORT:
        return (False, None, None, None)

    vxlan_off = udp_off + 8
    if (int(frame_data[vxlan_off], 16) & 0x08) == 0:
        return (False, None, None, None)

    inner_off = vxlan_off + 8
    return (True, outer_family, vxlan_off, inner_off)


def hex_dump(frame_data, bytes_per_row=16):
    """Hex+ASCII dump of a captured frame."""
    lines = []
    for i in range(0, len(frame_data), bytes_per_row):
        row = frame_data[i:i + bytes_per_row]
        hex_part = " ".join(row).ljust(bytes_per_row * 3 - 1)
        ascii_part = "".join(
            chr(int(b, 16)) if 0x20 <= int(b, 16) < 0x7F else "." for b in row
        )
        lines.append("  {:04X}  {}  |{}|".format(i, hex_part, ascii_part))
    return "\n".join(lines)


def build_tg_stream_params(family, src_ip, dst_ip, dscp,
                            pkts_per_burst=10, rate_pps=100,
                            mac_src=None, mac_dst=None,
                            udp_src_port="12345", udp_dst_port="54321"):
    """Build TG traffic_config params for a single burst."""
    params = {
        "port_handle": data.tg_ph_1,
        "port_handle2": data.tg_ph_2,
        "mode": "create",
        "transmit_mode": "single_burst",
        "pkts_per_burst": pkts_per_burst,
        "rate_pps": rate_pps,
        "frame_size": 128,
        "l2_encap": "ethernet_ii",
        "mac_src": mac_src or TG_SRC_MAC,
        "mac_dst": mac_dst or data.dut_mac,
        "l4_protocol": "udp",
        "udp_src_port": udp_src_port,
        "udp_dst_port": udp_dst_port,
    }
    if family == "v4":
        params.update({
            "l3_protocol": "ipv4",
            "ip_src_addr": src_ip,
            "ip_dst_addr": dst_ip,
            "ip_dscp": str(dscp),
        })
    else:
        params.update({
            "l3_protocol": "ipv6",
            "ipv6_src_addr": src_ip,
            "ipv6_dst_addr": dst_ip,
            "ipv6_traffic_class": str(dscp << 2),
        })
    return params


def send_and_capture(stream_params, wait_seconds=3):
    """Run TG burst and return egress packet stats."""
    tg1, tg2 = data.tg1, data.tg2
    tg_ph_1, tg_ph_2 = data.tg_ph_1, data.tg_ph_2

    tg1.tg_traffic_control(action="reset", port_handle=tg_ph_1)
    stream = tg1.tg_traffic_config(**stream_params)
    stream_id = stream['stream_id']

    tg2.tg_packet_control(port_handle=tg_ph_2, action='start')
    tg1.tg_traffic_control(action='run', handle=stream_id)
    st.wait(wait_seconds)
    tg1.tg_traffic_control(action='stop', handle=stream_id)
    tg2.tg_packet_control(port_handle=tg_ph_2, action='stop')

    return tg2.tg_packet_stats(port_handle=tg_ph_2, format='var', output_type='hex')


def capture_and_parse_traffic(label, inner_family, src_ip, dst_ip, sent_dscp,
                              mode="vxlan", outer_family=None,
                              pkts_per_burst=10, rate_pps=100, wait_seconds=None,
                              bulk_sample=None, expected_outer_dscp=None):
    """Send traffic and parse captured frames (vxlan, plain, or bulk sample)."""
    if wait_seconds is None:
        wait_seconds = 3
    if mode == "bulk":
        wait_seconds = max(5, int(pkts_per_burst / max(rate_pps, 1)) + 5)

    stream_params = build_tg_stream_params(
        inner_family, src_ip, dst_ip, sent_dscp,
        pkts_per_burst=pkts_per_burst, rate_pps=rate_pps)
    pkts_captured = send_and_capture(stream_params, wait_seconds=wait_seconds)

    if mode == "bulk":
        expected_outer_fam = outer_family
        checked = 0
        failed = 0
        for key in pkts_captured:
            if key == 'status':
                continue
            agg = pkts_captured[key].get('aggregate', {})
            num_frames = agg.get('num_frames', '0')
            st.log("[{}] bulk: TG sent={} captured_total={}".format(
                label, pkts_per_burst, num_frames))
            if num_frames in ['0', 'N/A']:
                return (False, 0, 0)
            frames_dict = pkts_captured[key].get('frame', {})
            for fkey in sorted(frames_dict.keys()):
                if checked >= bulk_sample:
                    break
                fdata = frames_dict[fkey].get('frame_pylist')
                if not fdata:
                    continue
                is_vxlan_pkt, outer_fam, _vx, inner_off = is_vxlan_frame(fdata)
                if not is_vxlan_pkt or outer_fam != expected_outer_fam:
                    continue
                _, outer_dscp, _ = parse_ip_dscp(fdata, eth_offset=0)
                _, inner_dscp, _ = parse_ip_dscp(fdata, eth_offset=inner_off)
                checked += 1
                if outer_dscp != expected_outer_dscp:
                    failed += 1
                    if failed <= 5:
                        st.log("[{}] bulk MISMATCH #{}: outer={} (expected {})".format(
                            label, failed, outer_dscp, expected_outer_dscp))
                elif inner_dscp is not None and inner_dscp != sent_dscp:
                    failed += 1
                    if failed <= 5:
                        st.log("[{}] bulk MISMATCH #{}: inner={} (expected {})".format(
                            label, failed, inner_dscp, sent_dscp))
            break
        if checked == 0:
            st.log("[{}] bulk: no VxLAN frames captured".format(label))
            return (False, 0, 0)
        st.log("[{}] bulk: validated {} sampled frames -> {} mismatches".format(
            label, checked, failed))
        return (failed == 0, checked, failed)

    if mode == "plain":
        result = {"captured": False, "is_vxlan": False, "dscp": None, "raw_frame": None}
        expected_fam = outer_family
        for key in pkts_captured:
            if key == 'status':
                continue
            num_frames = pkts_captured[key]['aggregate'].get('num_frames', '0')
            if num_frames in ['0', 'N/A']:
                st.log("[{}] No packets captured".format(label))
                break
            frames_dict = pkts_captured[key].get('frame', {})
            for fkey in sorted(frames_dict.keys()):
                fdata = frames_dict[fkey].get('frame_pylist')
                if not fdata:
                    continue
                outer_fam, dscp, tos = parse_ip_dscp(fdata, eth_offset=0)
                if outer_fam != expected_fam:
                    continue
                is_vxlan_pkt, _, _, _ = is_vxlan_frame(fdata)
                result.update({"captured": True, "is_vxlan": is_vxlan_pkt,
                               "dscp": dscp, "raw_frame": fdata})
                st.log("[{}] family={} TC/TOS=0x{:02X} DSCP={} is_vxlan={}".format(
                    label, outer_fam, tos, dscp, is_vxlan_pkt))
                st.log("[{}] HEX DUMP ({} bytes):\n{}".format(
                    label, len(fdata), hex_dump(fdata)))
                break
            break
        return result

    # mode == "vxlan"
    result = {"captured": False, "actual_dscp": None,
              "outer_family": None, "raw_frame": None,
              "inner_dscp": None, "inner_family": None}
    try:
        for key in pkts_captured:
            if key == 'status':
                continue
            num_frames = pkts_captured[key]['aggregate']['num_frames']
            if num_frames in ['0', 'N/A']:
                st.log("[{}] No packets captured on egress port".format(label))
                break
            st.log("[{}] Captured {} packets on egress port".format(label, num_frames))
            frames_dict = pkts_captured[key].get('frame', {})
            chosen = None
            chosen_inner_off = None
            for fkey in sorted(frames_dict.keys()):
                fdata = frames_dict[fkey].get('frame_pylist')
                if not fdata:
                    continue
                is_vxlan_pkt, outer_fam, _vx_off, inner_off = is_vxlan_frame(fdata)
                if not is_vxlan_pkt or outer_fam != outer_family:
                    continue
                chosen = fdata
                chosen_inner_off = inner_off
                break
            if chosen is None:
                st.log("[{}] No VxLAN-encapsulated frame found in capture".format(label))
                break
            outer_family_val, actual_dscp, tc_byte = parse_ip_dscp(chosen, eth_offset=0)
            inner_family_val, inner_dscp, inner_tc = parse_ip_dscp(
                chosen, eth_offset=chosen_inner_off)
            result.update({
                "captured": True,
                "actual_dscp": actual_dscp,
                "outer_family": outer_family_val,
                "raw_frame": chosen,
                "inner_dscp": inner_dscp,
                "inner_family": inner_family_val,
            })
            st.log("[{}] OUTER: family={} TC/TOS=0x{:02X} DSCP={}".format(
                label, outer_family_val, tc_byte, actual_dscp))
            if inner_family_val is not None:
                st.log("[{}] INNER: family={} TC/TOS=0x{:02X} DSCP={}".format(
                    label, inner_family_val, inner_tc, inner_dscp))
            else:
                st.log("[{}] INNER: could not parse (frame too short or unknown ethertype)".format(
                    label))
            st.log("[{}] HEX DUMP ({} bytes):\n{}".format(
                label, len(chosen), hex_dump(chosen)))
            break
    except Exception as exc:
        st.log("[{}] Error parsing captured packet: {}".format(label, exc))
    return result


def check_dscp_result(scn, capture_result, expected_outer_dscp, sent_dscp):
    """Validate outer/inner DSCP on a captured VxLAN frame."""
    if not capture_result["captured"]:
        st.log("[{}] FAIL: No VxLAN packet captured".format(scn["name"]))
        return False
    expected_outer_fam = IP_FAMILY[scn["outer"]]
    if capture_result["outer_family"] != expected_outer_fam:
        st.log("[{}] FAIL: outer family expected={} got={}".format(
            scn["name"], expected_outer_fam, capture_result["outer_family"]))
        return False
    outer = capture_result["actual_dscp"]
    inner = capture_result["inner_dscp"]
    if outer != expected_outer_dscp:
        st.log("[{}] FAIL: outer DSCP expected={} actual={}".format(
            scn["name"], expected_outer_dscp, outer))
        return False
    if inner is not None and inner != sent_dscp:
        st.log("[{}] FAIL: inner DSCP expected={} actual={}".format(
            scn["name"], sent_dscp, inner))
        return False
    st.log("[{}] PASS: outer={} inner={}".format(scn["name"], outer, inner))
    return True


def run_dscp_scenarios_multi(dscp_map):
    """Run all SCENARIOS for each sent_dscp -> expected_outer_dscp mapping."""
    multi = len(dscp_map) > 1
    per_results = {}
    for scn in SCENARIOS:
        st.log("Scenario {}: inner={} outer={} vnet={} vni={} dest={}".format(
            scn["name"], scn["inner"], scn["outer"], scn["vnet"], scn["vni"], scn["dest"]))
        for sent_dscp, expected_outer_dscp in dscp_map.items():
            capture_result = capture_and_parse_traffic(
                scn["name"], scn["inner"],
                traffic_ip(scn["inner"], "ingress_src"), scn["dest"], sent_dscp,
                mode="vxlan", outer_family=IP_FAMILY[scn["outer"]])
            key = "{}:{}".format(scn["name"], sent_dscp) if multi else scn["name"]
            per_results[key] = check_dscp_result(
                scn, capture_result, expected_outer_dscp, sent_dscp)
    return per_results


@pytest.fixture(scope="module", autouse=True)
def acl_dscp_module_hooks(request):
    """Module setup/teardown for topology, routing, and VxLAN."""
    initialize_topology()
    configure_routing()
    configure_vxlan()
    yield
    unconfigure_vxlan()
    unconfigure_routing()


# Test cases

@pytest.mark.acl
def test_acl_dscp_rewrite_vxlan():
    """Positive: ACL rewrites outer DSCP to EXPECTED_DSCP for all 4 encap combos."""
    sent_dscp = 33

    table_specs = [
        (TABLE_V4, "UNDERLAY_SET_DSCP",   "DSCP rewrite v4-inner"),
        (TABLE_V6, "UNDERLAY_SET_DSCPV6", "DSCP rewrite v6-inner"),
    ]

    def rule_builder(scn):
        return (scn["table"], scn["rule_name"], {
            "PRIORITY": "9999",
            "DSCP_ACTION": str(EXPECTED_DSCP),
            scn["rule_field"]: "{}/{}".format(scn["dest"], scn["mask"]),
        })

    created_tables, created_rules = setup_acl(table_specs, rule_builder, data.ports)

    if not verify_acl_setup_active(created_tables, created_rules):
        cleanup_acl(created_tables, created_rules)
        st.report_fail("test_case_failed")

    per_scenario_results = run_dscp_scenarios_multi({sent_dscp: EXPECTED_DSCP})

    st.log("PER-SCENARIO RESULTS:")
    for name, passed in per_scenario_results.items():
        st.log("  {:12s}: {}".format(name, "PASS" if passed else "FAIL"))
    st.config(data.dut, "sudo counterpoll acl enable || true")
    try:
        aclshow_out = st.show(data.dut, "sudo aclshow -a", skip_tmpl=True)
    except Exception as exc:
        aclshow_out = "<aclshow -a failed: {}>".format(exc)
    st.log("---- aclshow -a (post-traffic) ----\n{}".format(aclshow_out))
    cleanup_acl(created_tables, created_rules)

    if all(per_scenario_results.values()):
        st.report_pass("test_case_passed")
    else:
        st.report_fail("test_case_failed")


@pytest.mark.acl
def test_acl_dscp_no_rewrite_on_mismatch():
    """Negative: non-matching SRC_IP/SRC_IPV6 -- outer DSCP equals sent inner DSCP."""
    sent_dscp = 25

    neg_cfg = {
        "v4": {
            "table": "ACL_DSCP_NEG_V4",
            "src_field": "SRC_IP",
            "src_value": "99.99.99.99/32",
            "dst_field": "DST_IP",
        },
        "v6": {
            "table": "ACL_DSCP_NEG_V6",
            "src_field": "SRC_IPV6",
            "src_value": "9999::9999/128",
            "dst_field": "DST_IPV6",
        },
    }

    table_specs = [
        ("ACL_DSCP_NEG_V4", "UNDERLAY_SET_DSCP",   "DSCP rewrite negative v4"),
        ("ACL_DSCP_NEG_V6", "UNDERLAY_SET_DSCPV6", "DSCP rewrite negative v6"),
    ]

    def rule_builder(scn):
        cfg = neg_cfg[scn["inner"]]
        return (cfg["table"], "RULE_NOMATCH_{}".format(scn["name"].upper()), {
            "PRIORITY": "9999",
            "DSCP_ACTION": str(EXPECTED_DSCP),
            cfg["src_field"]: cfg["src_value"],
            cfg["dst_field"]: "{}/{}".format(scn["dest"], scn["mask"]),
        })

    created_tables, created_rules = setup_acl(table_specs, rule_builder, data.ports)

    if not verify_acl_setup_active(created_tables, created_rules):
        cleanup_acl(created_tables, created_rules)
        st.report_fail("test_case_failed")

    per_scenario_results = run_dscp_scenarios_multi({sent_dscp: sent_dscp})

    st.log("PER-SCENARIO RESULTS:")
    for name, passed in per_scenario_results.items():
        st.log("  {:12s}: {}".format(name, "PASS" if passed else "FAIL"))
    st.config(data.dut, "sudo counterpoll acl enable || true")
    try:
        aclshow_out = st.show(data.dut, "sudo aclshow -a", skip_tmpl=True)
    except Exception as exc:
        aclshow_out = "<aclshow -a failed: {}>".format(exc)
    st.log("---- aclshow -a (post-traffic) ----\n{}".format(aclshow_out))
    cleanup_acl(created_tables, created_rules)

    if all(per_scenario_results.values()):
        st.report_pass("test_case_passed")
    else:
        st.report_fail("test_case_failed")


@pytest.mark.acl
def test_acl_dscp_no_rewrite_partial_match():
    """Negative: DST matches but L4_SRC_PORT does not -- no DSCP rewrite."""
    sent_dscp = 27
    NOMATCH_L4_SRC_PORT = "9999"

    table_specs = [
        ("ACL_DSCP_PARTIAL_V4", "UNDERLAY_SET_DSCP",   "DSCP rewrite partial v4"),
        ("ACL_DSCP_PARTIAL_V6", "UNDERLAY_SET_DSCPV6", "DSCP rewrite partial v6"),
    ]

    def rule_builder(scn):
        tbl = "ACL_DSCP_PARTIAL_V4" if scn["inner"] == "v4" else "ACL_DSCP_PARTIAL_V6"
        return (tbl, "RULE_PARTIAL_{}".format(scn["name"].upper()), {
            "PRIORITY": "9999",
            "DSCP_ACTION": str(EXPECTED_DSCP),
            scn["rule_field"]: "{}/{}".format(scn["dest"], scn["mask"]),
            "L4_SRC_PORT": NOMATCH_L4_SRC_PORT,
        })

    created_tables, created_rules = setup_acl(table_specs, rule_builder, data.ports)

    if not verify_acl_setup_active(created_tables, created_rules):
        cleanup_acl(created_tables, created_rules)
        st.report_fail("test_case_failed")

    per_scenario_results = run_dscp_scenarios_multi({sent_dscp: sent_dscp})

    st.log("PER-SCENARIO RESULTS:")
    for name, passed in per_scenario_results.items():
        st.log("  {:12s}: {}".format(name, "PASS" if passed else "FAIL"))
    st.config(data.dut, "sudo counterpoll acl enable || true")
    try:
        aclshow_out = st.show(data.dut, "sudo aclshow -a", skip_tmpl=True)
    except Exception as exc:
        aclshow_out = "<aclshow -a failed: {}>".format(exc)
    st.log("---- aclshow -a (post-traffic) ----\n{}".format(aclshow_out))
    cleanup_acl(created_tables, created_rules)

    if all(per_scenario_results.values()):
        st.report_pass("test_case_passed")
    else:
        st.report_fail("test_case_failed")


@pytest.mark.acl
def test_acl_dscp_default_no_acl():
    """Default: no ACL -- outer DSCP copies inner DSCP."""
    sent_dscp = 33
    table_specs = []

    def rule_builder(scn):
        return None

    created_tables, created_rules = setup_acl(table_specs, rule_builder, data.ports)

    if not verify_acl_setup_active(created_tables, created_rules):
        cleanup_acl(created_tables, created_rules)
        st.report_fail("test_case_failed")

    per_scenario_results = run_dscp_scenarios_multi({sent_dscp: sent_dscp})

    st.log("PER-SCENARIO RESULTS:")
    for name, passed in per_scenario_results.items():
        st.log("  {:12s}: {}".format(name, "PASS" if passed else "FAIL"))
    st.config(data.dut, "sudo counterpoll acl enable || true")
    try:
        aclshow_out = st.show(data.dut, "sudo aclshow -a", skip_tmpl=True)
    except Exception as exc:
        aclshow_out = "<aclshow -a failed: {}>".format(exc)
    st.log("---- aclshow -a (post-traffic) ----\n{}".format(aclshow_out))
    cleanup_acl(created_tables, created_rules)

    if all(per_scenario_results.values()):
        st.report_pass("test_case_passed")
    else:
        st.report_fail("test_case_failed")


@pytest.mark.acl
def test_acl_dscp_priority():
    """Highest-priority matching rule wins (DSCP=40 from priority 800)."""
    sent_dscp = 33
    PRIORITY_DSCPS = [
        ("LOW",  "100",  10),
        ("MID",  "500",  20),
        ("HIGH", "800", EXPECTED_DSCP),
    ]

    table_specs = [
        ("ACL_DSCP_PRIO_V4", "UNDERLAY_SET_DSCP",   "DSCP rewrite priority v4"),
        ("ACL_DSCP_PRIO_V6", "UNDERLAY_SET_DSCPV6", "DSCP rewrite priority v6"),
    ]

    def rule_builder(scn):
        tbl = "ACL_DSCP_PRIO_V4" if scn["inner"] == "v4" else "ACL_DSCP_PRIO_V6"
        rules = []
        for tag, prio, dscp in PRIORITY_DSCPS:
            rules.append((tbl, "RULE_PRIO_{}_{}".format(scn["name"].upper(), tag), {
                "PRIORITY": prio,
                "DSCP_ACTION": str(dscp),
                scn["rule_field"]: "{}/{}".format(scn["dest"], scn["mask"]),
            }))
        return rules

    created_tables, created_rules = setup_acl(table_specs, rule_builder, data.ports)

    if not verify_acl_setup_active(created_tables, created_rules):
        cleanup_acl(created_tables, created_rules)
        st.report_fail("test_case_failed")

    per_scenario_results = run_dscp_scenarios_multi({sent_dscp: EXPECTED_DSCP})

    st.log("PER-SCENARIO RESULTS:")
    for name, passed in per_scenario_results.items():
        st.log("  {:12s}: {}".format(name, "PASS" if passed else "FAIL"))
    st.config(data.dut, "sudo counterpoll acl enable || true")
    try:
        aclshow_out = st.show(data.dut, "sudo aclshow -a", skip_tmpl=True)
    except Exception as exc:
        aclshow_out = "<aclshow -a failed: {}>".format(exc)
    st.log("---- aclshow -a (post-traffic) ----\n{}".format(aclshow_out))
    cleanup_acl(created_tables, created_rules)

    if all(per_scenario_results.values()):
        st.report_pass("test_case_passed")
    else:
        st.report_fail("test_case_failed")


@pytest.mark.acl
def test_acl_dscp_match_on_inner_dscp():
    """Match on inner DSCP; priority resolves overlaps (DSCP=8 -> outer 48)."""
    DSCP_RULES = [
        ("RULE1", "100",  8, 48),
        ("RULE2", "200", 16, 46),
        ("RULE3", "300", 24, 34),
        ("RULE4", "400", 32, 26),
        ("RULE5", "500", 40, 18),
        ("RULE6", "50",   8, 63),
    ]
    expected_map = {
        8:  48,
        16: 46,
        24: 34,
        32: 26,
        40: 18,
        4:  4,
        56: 56,
    }

    table_specs = [
        ("ACL_DSCP_MATCH_V4", "UNDERLAY_SET_DSCP",   "DSCP match v4-inner"),
        ("ACL_DSCP_MATCH_V6", "UNDERLAY_SET_DSCPV6", "DSCP match v6-inner"),
    ]

    def rule_builder(scn):
        tbl = "ACL_DSCP_MATCH_V4" if scn["inner"] == "v4" else "ACL_DSCP_MATCH_V6"
        rules = []
        for rname, prio, dscp_in, dscp_out in DSCP_RULES:
            rules.append((tbl, "{}_{}".format(rname, scn["name"].upper()), {
                "PRIORITY": prio,
                "DSCP": str(dscp_in),
                "DSCP_ACTION": str(dscp_out),
                scn["rule_field"]: "{}/{}".format(scn["dest"], scn["mask"]),
            }))
        return rules

    created_tables, created_rules = setup_acl(table_specs, rule_builder, data.ports)

    if not verify_acl_setup_active(created_tables, created_rules):
        cleanup_acl(created_tables, created_rules)
        st.report_fail("test_case_failed")

    per_results = run_dscp_scenarios_multi(expected_map)

    st.log("PER-SCENARIO RESULTS:")
    for name, passed in per_results.items():
        st.log("  {:12s}: {}".format(name, "PASS" if passed else "FAIL"))
    st.config(data.dut, "sudo counterpoll acl enable || true")
    try:
        aclshow_out = st.show(data.dut, "sudo aclshow -a", skip_tmpl=True)
    except Exception as exc:
        aclshow_out = "<aclshow -a failed: {}>".format(exc)
    st.log("---- aclshow -a (post-traffic) ----\n{}".format(aclshow_out))
    cleanup_acl(created_tables, created_rules)

    if all(per_results.values()):
        st.report_pass("test_case_passed")
    else:
        st.report_fail("test_case_failed")


@pytest.mark.acl
def test_acl_dscp_high_volume():
    """High-volume burst; sample captured frames for consistent DSCP rewrite."""
    sent_dscp = 33
    PKTS_PER_BURST = 50000
    RATE_PPS = 10000
    SAMPLE = 300

    table_specs = [
        ("ACL_DSCP_BULK_V4", "UNDERLAY_SET_DSCP",   "DSCP rewrite high volume v4"),
        ("ACL_DSCP_BULK_V6", "UNDERLAY_SET_DSCPV6", "DSCP rewrite high volume v6"),
    ]

    def rule_builder(scn):
        tbl = "ACL_DSCP_BULK_V4" if scn["inner"] == "v4" else "ACL_DSCP_BULK_V6"
        return (tbl, "RULE_BULK_{}".format(scn["name"].upper()), {
            "PRIORITY": "9999",
            "DSCP_ACTION": str(EXPECTED_DSCP),
            scn["rule_field"]: "{}/{}".format(scn["dest"], scn["mask"]),
        })

    created_tables, created_rules = setup_acl(table_specs, rule_builder, data.ports)

    if not verify_acl_setup_active(created_tables, created_rules):
        cleanup_acl(created_tables, created_rules)
        st.report_fail("test_case_failed")

    per_scenario_results = {}
    for scn in SCENARIOS:
        st.log("Scenario {}: bulk send {} pkts at {} pps, sample {}".format(
            scn["name"], PKTS_PER_BURST, RATE_PPS, SAMPLE))
        passed, checked, failed = capture_and_parse_traffic(
            scn["name"], scn["inner"],
            traffic_ip(scn["inner"], "ingress_src"), scn["dest"], sent_dscp,
            mode="bulk", outer_family=IP_FAMILY[scn["outer"]],
            pkts_per_burst=PKTS_PER_BURST, rate_pps=RATE_PPS,
            bulk_sample=SAMPLE, expected_outer_dscp=EXPECTED_DSCP)
        st.log("[{}] bulk result: passed={} checked={} failed={}".format(
            scn["name"], passed, checked, failed))
        per_scenario_results[scn["name"]] = passed

    st.log("PER-SCENARIO RESULTS:")
    for name, passed in per_scenario_results.items():
        st.log("  {:12s}: {}".format(name, "PASS" if passed else "FAIL"))
    st.config(data.dut, "sudo counterpoll acl enable || true")
    try:
        aclshow_out = st.show(data.dut, "sudo aclshow -a", skip_tmpl=True)
    except Exception as exc:
        aclshow_out = "<aclshow -a failed: {}>".format(exc)
    st.log("---- aclshow -a (post-traffic) ----\n{}".format(aclshow_out))
    cleanup_acl(created_tables, created_rules)

    if all(per_scenario_results.values()):
        st.report_pass("test_case_passed")
    else:
        st.report_fail("test_case_failed")


@pytest.mark.acl
def test_acl_dscp_non_tunneled():
    """Non-tunneled v4/v6: no-match preserves DSCP; match rewrites without encap."""
    sent_dscp = 50

    scenarios = [
        {
            "name": "scn1_random_non_tunnel_no_match",
            "v4_table": "ACL_NT1_V4",
            "v6_table": "ACL_NT1_V6",
            "v4_rule": "RULE_NT1_V4",
            "v6_rule": "RULE_NT1_V6",
            "v4_fields": {"DST_IP": "{}/32".format(DEST_V4_IN_V4)},
            "v6_fields": {"DST_IPV6": "{}/128".format(DEST_V6_IN_V4)},
            "expect_rewrite": False,
        },
        {
            "name": "scn2_match_no_encap_rewrite_fires",
            "v4_table": "ACL_NT2_V4",
            "v6_table": "ACL_NT2_V6",
            "v4_rule": "RULE_NT2_V4",
            "v6_rule": "RULE_NT2_V6",
            "v4_fields": {"DST_IP": "{}/32".format(EGRESS_TG_IP)},
            "v6_fields": {"DST_IPV6": "{}/128".format(EGRESS_TG_IPV6)},
            "expect_rewrite": True,
        },
        {
            "name": "scn3_l4_dst_port_vxlan_no_match",
            "v4_table": "ACL_NT3_V4",
            "v6_table": "ACL_NT3_V6",
            "v4_rule": "RULE_NT3_V4",
            "v6_rule": "RULE_NT3_V6",
            "v4_fields": {"L4_DST_PORT": str(VXLAN_PORT)},
            "v6_fields": {"L4_DST_PORT": str(VXLAN_PORT)},
            "expect_rewrite": False,
        },
    ]

    per_results = {}

    for scn in scenarios:
        st.log("===== {} =====".format(scn["name"]))

        fam_specs = [
            ("v4", scn["v4_table"], "UNDERLAY_SET_DSCP",   scn["v4_rule"], scn["v4_fields"]),
            ("v6", scn["v6_table"], "UNDERLAY_SET_DSCPV6", scn["v6_rule"], scn["v6_fields"]),
        ]

        for _, tbl, ttype, rule, fields in fam_specs:
            create_acl_table(tbl, ttype, scn["name"], data.ports)
            full_fields = {
                "PRIORITY": "9999",
                "DSCP_ACTION": str(EXPECTED_DSCP),
            }
            full_fields.update(fields)
            create_acl_rule(tbl, rule, full_fields)

        created_tables = [tbl for _, tbl, _, _, _ in fam_specs]
        created_rules = [(tbl, rule) for _, tbl, _, rule, _ in fam_specs]

        if not verify_acl_setup_active(created_tables, created_rules):
            cleanup_acl(created_tables, created_rules)
            for fam, _, _, _, _ in fam_specs:
                per_results["{}:{}".format(scn["name"], fam)] = False
            continue

        for fam, tbl, _, rule, _ in fam_specs:
            key = "{}:{}".format(scn["name"], fam)
            st.log("  -- {} --".format(key))
            cap = capture_and_parse_traffic(
                key, fam,
                traffic_ip(fam, "ingress_src"), traffic_ip(fam, "egress_dst"),
                sent_dscp, mode="plain", outer_family=IP_FAMILY[fam])
            st.config(data.dut, "sudo counterpoll acl enable || true")
            try:
                aclshow_out = st.show(data.dut, "sudo aclshow -a", skip_tmpl=True)
            except Exception as exc:
                aclshow_out = "<aclshow -a failed: {}>".format(exc)
            st.log("---- aclshow -a ({} {}) ----\n{}".format(
                scn["name"], fam, aclshow_out))

            expected_dscp = EXPECTED_DSCP if scn["expect_rewrite"] else sent_dscp
            scn_pass = True

            if not cap["captured"]:
                st.log("[{}] FAIL: no packet captured on egress".format(key))
                scn_pass = False
            else:
                if cap["is_vxlan"]:
                    st.log("[{}] FAIL: packet is VxLAN-encapsulated "
                           "(should be plain routed -- no VNET route for dst)".format(key))
                    scn_pass = False
                if cap["dscp"] != expected_dscp:
                    st.log("[{}] FAIL: DSCP expected={} (rewrite={}) got={}".format(
                        key, expected_dscp, scn["expect_rewrite"], cap["dscp"]))
                    scn_pass = False
                if scn_pass:
                    st.log("[{}] PASS: dscp={} (expect_rewrite={})".format(
                        key, cap["dscp"], scn["expect_rewrite"]))

            per_results[key] = scn_pass

        cleanup_acl(created_tables, created_rules)

    st.log("PER-SCENARIO RESULTS:")
    for name, passed in per_results.items():
        st.log("  {:12s}: {}".format(name, "PASS" if passed else "FAIL"))
    st.config(data.dut, "sudo counterpoll acl enable || true")
    try:
        aclshow_out = st.show(data.dut, "sudo aclshow -a", skip_tmpl=True)
    except Exception as exc:
        aclshow_out = "<aclshow -a failed: {}>".format(exc)
    st.log("---- aclshow -a (post-traffic) ----\n{}".format(aclshow_out))

    if all(per_results.values()):
        st.report_pass("test_case_passed")
    else:
        st.report_fail("test_case_failed")
