#!/usr/bin/env python3
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sots.config import (
    NUM_SPINES,
    NUM_LEAVES,
    NUM_SERVERS,
    MGMT_BASE_IP,
    MGMT_START,
    SSH_USER,
    SSH_PASS,
    FABRIC_SUBNET,
    LOOPBACK_SPINE,
    LOOPBACK_LEAF,
    SPINE_AS_BASE,
    LEAF_AS_BASE,
    UNDERLAY_LOOPBACK,
    VTEP_LOOPBACK,
    VTEP_SUBNET,
    VXLAN_INTERFACE,
    MLAG_PAIRS,
    MLAG_DOMAIN_PREFIX,
    MLAG_PEER_LINK_CHANNEL,
    MLAG_PEER_LINK_MEMBER_COUNT,
    MLAG_PEER_VLAN,
    MLAG_PEER_VLAN_NAME,
    MLAG_TRUNK_GROUP,
    MLAG_PEER_IP_SUBNET,
    MLAG_RELOAD_DELAY_MLAG,
    ENABLE_DMZ_FIREWALL,
)
from sots.vlans import VLANS, TENANTS

inventory = {
    "spines": {"hosts": []},
    "border_leaves": {"hosts": []},
    "compute_leaves": {"hosts": []},
    "mlag_leaves": {"hosts": []},
    "leaves": {"children": ["border_leaves", "compute_leaves"]},
    "switches": {"children": ["spines", "leaves"]},
    "_meta": {"hostvars": {}},
}


def base_vars(mgmt_ip):
    return {
        "ansible_host": mgmt_ip,
        "ansible_user": SSH_USER,
        "ansible_password": SSH_PASS,
        "ansible_network_os": "eos",
        "ansible_connection": "httpapi",
        "ansible_httpapi_use_ssl": True,
        "ansible_httpapi_validate_certs": False,
        "ansible_httpapi_port": 443,
        "ansible_become": True,
        "ansible_become_method": "enable",
    }


mgmt = MGMT_START
spine_fabric = [[] for _ in range(NUM_SPINES)]
leaf_fabric = [[] for _ in range(NUM_LEAVES)]

spine_loopbacks = [
    {"ip": f"{LOOPBACK_SPINE}.{i+1}", "asn": SPINE_AS_BASE} for i in range(NUM_SPINES)
]
leaf_loopbacks = [
    {"ip": f"{LOOPBACK_LEAF}.{j+1}", "asn": LEAF_AS_BASE + j} for j in range(NUM_LEAVES)
]

for i in range(NUM_SPINES):
    for j in range(NUM_LEAVES):
        offset = (i * NUM_LEAVES + j) * 2
        spine_ip = f"{FABRIC_SUBNET}.{offset}"
        leaf_ip = f"{FABRIC_SUBNET}.{offset + 1}"
        spine_iface = f"Ethernet{j + 1}"
        leaf_iface = f"Ethernet{i + 1}"

        peer_leaf_name = f"Leaf-{j + 1}"

        spine_fabric[i].append(
            {
                "interface": spine_iface,
                "ip": spine_ip,
                "peer_ip": leaf_ip,
                "peer_name": peer_leaf_name,
                "peer_asn": LEAF_AS_BASE + j,
            }
        )
        leaf_fabric[j].append(
            {
                "interface": leaf_iface,
                "ip": leaf_ip,
                "peer_ip": spine_ip,
                "peer_name": f"Spine-{i + 1}",
                "peer_asn": SPINE_AS_BASE,
            }
        )


# FIX: Build external_handoffs dynamically from TENANTS so that
# vrf names and l3_vnis never drift from the vlans.py SSOT.
# Each tenant that has "external_handoff: true" gets an entry.
# The interface, IPs and peer ASN come from config.py constants
# (BORDER_HANDOFFS) — see config.py for how to add new ones.
def build_external_handoffs(tenants, border_number):
    handoffs = []
    for t in tenants:
        if not t.get("external_handoff"):
            continue
        if border_number == 1:
            local_ip = t["handoff_local_ip"]
            peer_ip = t["handoff_peer_ip"]
        else:
            local_ip = t.get("handoff_local_ip_2")
            peer_ip = t.get("handoff_peer_ip_2")
        if not local_ip or not peer_ip:
            continue
        handoffs.append(
            {
                "vrf": t["name"],
                "l3_vni": t["l3_vni"],
                "originate_default_route": t.get("originate_default_route", False),
                "interface": t["handoff_interface"],
                "vlan": t[
                    "handoff_vlan"
                ],  # 802.1Q tag — was missing, causing item.vlan error
                "local_ip": local_ip,
                "peer_ip": peer_ip,
                "peer_asn": t["handoff_peer_asn"],
            }
        )
    return handoffs


def leaf_name_from_index(leaf_index):
    if leaf_index == _border_left_idx:
        return "Border-1"
    if leaf_index == _border_right_idx:
        return "Border-2"
    return f"Leaf-{leaf_index}"


def parse_pair(pair, pair_number):
    if isinstance(pair, str):
        parts = [p.strip() for p in pair.split(",") if p.strip()]
    elif isinstance(pair, (list, tuple)):
        parts = list(pair)
    else:
        raise ValueError(
            f"MLAG_PAIRS entry #{pair_number} must be list/tuple or comma-separated string, got {type(pair).__name__}"
        )

    if len(parts) != 2:
        raise ValueError(
            f"MLAG_PAIRS entry #{pair_number} must contain exactly two leaf indexes"
        )

    try:
        first = int(parts[0])
        second = int(parts[1])
    except ValueError as exc:
        raise ValueError(
            f"MLAG_PAIRS entry #{pair_number} must contain integer leaf indexes"
        ) from exc

    return first, second


_border_left_idx, _border_right_idx = parse_pair(MLAG_PAIRS[0], 1)


def ethernet_to_adapter(interface_name):
    if not isinstance(interface_name, str) or not interface_name.startswith("Ethernet"):
        return None
    suffix = interface_name[len("Ethernet") :]
    return int(suffix) if suffix.isdigit() else None


def build_reserved_mlag_adapters():
    reserved = {}
    if not ENABLE_DMZ_FIREWALL:
        return reserved

    handoff_adapters = set()
    for tenant in TENANTS:
        if not tenant.get("external_handoff"):
            continue
        adapter = ethernet_to_adapter(tenant.get("handoff_interface"))
        if adapter is not None:
            handoff_adapters.add(adapter)

    if not handoff_adapters:
        return reserved

    reserved[_border_left_idx] = set(handoff_adapters)
    reserved[_border_right_idx] = set(handoff_adapters)
    return reserved


def next_mlag_member_interface(leaf_index, next_adapter_by_leaf, reserved_adapters):
    current = next_adapter_by_leaf[leaf_index - 1]
    reserved_for_leaf = reserved_adapters.get(leaf_index, set())
    while current in reserved_for_leaf:
        current += 1
    next_adapter_by_leaf[leaf_index - 1] = current + 1
    return f"Ethernet{current}"


def build_server_interface_plan():
    server_interfaces_by_leaf = [[] for _ in range(NUM_LEAVES)]
    # Leaf-spine links consume Ethernet1..EthernetNUM_SPINES.
    next_adapter_by_leaf = [NUM_SPINES + 1 for _ in range(NUM_LEAVES)]

    # Match deploy_fabric.py server placement (round-robin across leaves).
    start_index = 1 if ENABLE_DMZ_FIREWALL else 0
    for server_index in range(start_index, NUM_SERVERS):
        leaf_index = (server_index - start_index) % NUM_LEAVES
        server_interfaces_by_leaf[leaf_index].append(
            f"Ethernet{next_adapter_by_leaf[leaf_index]}"
        )
        next_adapter_by_leaf[leaf_index] += 1

    return server_interfaces_by_leaf, next_adapter_by_leaf


def allocate_mlag_peer_link_members(next_adapter_by_leaf):
    if MLAG_PEER_LINK_MEMBER_COUNT < 1:
        raise ValueError("MLAG_PEER_LINK_MEMBER_COUNT must be >= 1")

    pair_details = []
    members_by_leaf = {}
    paired_leaf_names = set()
    reserved_adapters = build_reserved_mlag_adapters()

    for pair_number, pair in enumerate(MLAG_PAIRS, start=1):
        left_idx, right_idx = parse_pair(pair, pair_number)

        if left_idx == right_idx:
            raise ValueError(
                f"MLAG_PAIRS entry #{pair_number} references the same leaf twice: {left_idx}"
            )

        for leaf_idx in (left_idx, right_idx):
            if leaf_idx < 1 or leaf_idx > NUM_LEAVES:
                raise ValueError(
                    f"MLAG_PAIRS entry #{pair_number} uses leaf index {leaf_idx}, but NUM_LEAVES is {NUM_LEAVES}"
                )

        left_name = leaf_name_from_index(left_idx)
        right_name = leaf_name_from_index(right_idx)

        if left_name in paired_leaf_names or right_name in paired_leaf_names:
            raise ValueError(
                f"Each leaf can belong to only one MLAG pair. Conflict in entry #{pair_number}: {left_idx},{right_idx}"
            )

        left_members = []
        right_members = []
        for _ in range(MLAG_PEER_LINK_MEMBER_COUNT):
            left_members.append(
                next_mlag_member_interface(
                    left_idx, next_adapter_by_leaf, reserved_adapters
                )
            )
            right_members.append(
                next_mlag_member_interface(
                    right_idx, next_adapter_by_leaf, reserved_adapters
                )
            )

        members_by_leaf[left_name] = left_members
        members_by_leaf[right_name] = right_members
        paired_leaf_names.add(left_name)
        paired_leaf_names.add(right_name)
        pair_details.append((pair_number, left_idx, right_idx, left_name, right_name))

    return pair_details, members_by_leaf


def build_mlag_leaf_vars():
    mlag_by_leaf = {}
    server_interfaces_by_leaf, next_adapter_by_leaf = build_server_interface_plan()
    pair_details, mlag_members_by_leaf = allocate_mlag_peer_link_members(
        next_adapter_by_leaf
    )

    for pair_number, left_idx, right_idx, left_name, right_name in pair_details:

        ip_offset = (pair_number - 1) * 2
        left_ip = f"{MLAG_PEER_IP_SUBNET}.{ip_offset}"
        right_ip = f"{MLAG_PEER_IP_SUBNET}.{ip_offset + 1}"
        domain_id = f"{MLAG_DOMAIN_PREFIX}-{left_idx}-{right_idx}"

        common = {
            "domain_id": domain_id,
            "peer_link_channel_id": MLAG_PEER_LINK_CHANNEL,
            "peer_vlan": MLAG_PEER_VLAN,
            "peer_vlan_name": MLAG_PEER_VLAN_NAME,
            "trunk_group": MLAG_TRUNK_GROUP,
            "reload_delay_mlag": MLAG_RELOAD_DELAY_MLAG,
        }

        mlag_by_leaf[left_name] = {
            **common,
            "peer_link_members": mlag_members_by_leaf[left_name],
            "local_ip": f"{left_ip}/31",
            "peer_ip": right_ip,
            "peer_name": right_name,
        }
        mlag_by_leaf[right_name] = {
            **common,
            "peer_link_members": mlag_members_by_leaf[right_name],
            "local_ip": f"{right_ip}/31",
            "peer_ip": left_ip,
            "peer_name": left_name,
        }

    return server_interfaces_by_leaf, mlag_by_leaf


leaf_server_interfaces, mlag_leaf_vars = build_mlag_leaf_vars()

# Build Spines
for i in range(NUM_SPINES):
    name = f"Spine-{i + 1}"
    mgmt_ip = f"{MGMT_BASE_IP}.{mgmt}"
    mgmt += 1

    vars_ = base_vars(mgmt_ip)
    vars_.update(
        {
            "loopback_ip": spine_loopbacks[i]["ip"],
            "loopback_interface": UNDERLAY_LOOPBACK,
            "bgp_asn": spine_loopbacks[i]["asn"],
            "fabric_interfaces": spine_fabric[i],
            "is_vtep": False,
            "overlay_peers": leaf_loopbacks,
        }
    )
    inventory["spines"]["hosts"].append(name)
    inventory["_meta"]["hostvars"][name] = vars_

# Build Leaves (Border & Compute)
for j in range(NUM_LEAVES):
    leaf_index = j + 1
    is_border_1 = leaf_index == _border_left_idx
    is_border_2 = leaf_index == _border_right_idx
    is_border = is_border_1 or is_border_2
    name = leaf_name_from_index(leaf_index)
    mgmt_ip = f"{MGMT_BASE_IP}.{mgmt}"
    mgmt += 1

    vars_ = base_vars(mgmt_ip)
    server_interfaces = leaf_server_interfaces[j]

    vars_.update(
        {
            "hostname": name,
            "loopback_ip": leaf_loopbacks[j]["ip"],
            "loopback_interface": UNDERLAY_LOOPBACK,
            "bgp_asn": leaf_loopbacks[j]["asn"],
            "fabric_interfaces": leaf_fabric[j],
            "is_vtep": True,
            "tenants": TENANTS,
            "vlans": VLANS,
            "vtep_interface": VTEP_LOOPBACK,
            "vtep_ip": f"{VTEP_SUBNET}.{j + 1}",
            "vxlan_interface": VXLAN_INTERFACE,
            "server_interfaces": server_interfaces,
            "overlay_peers": spine_loopbacks,
        }
    )

    if is_border:
        vars_["external_handoffs"] = build_external_handoffs(
            TENANTS, 1 if is_border_1 else 2
        )
        inventory["border_leaves"]["hosts"].append(name)
    else:
        inventory["compute_leaves"]["hosts"].append(name)

    if name in mlag_leaf_vars:
        vars_["mlag"] = mlag_leaf_vars[name]
        inventory["mlag_leaves"]["hosts"].append(name)

    inventory["_meta"]["hostvars"][name] = vars_

print(json.dumps(inventory, indent=2))
