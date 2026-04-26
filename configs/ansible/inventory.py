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
    BORDER_FIREWALL_COUNT,
    DMZ_FW_ASN,
    DMZ_HANDOFF_VLAN,
    DMZ_LEAF1_LOCAL_IP,
    DMZ_FW1_LAN_IP1,
    DMZ_LEAF2_LOCAL_IP,
    DMZ_FW1_LAN_IP2,
    DMZ_FW2_LAN_IP1,
)
from sots.vlans import VLANS, TENANTS

# ---------------------------------------------------------------------------
# parse_pair must be defined before border_leaf_indices is computed.
# ---------------------------------------------------------------------------

def parse_pair(pair, pair_number):
    if isinstance(pair, str):
        parts = [p.strip() for p in pair.split(",") if p.strip()]
    elif isinstance(pair, (list, tuple)):
        parts = list(pair)
    else:
        raise ValueError(
            f"MLAG_PAIRS entry #{pair_number} must be list/tuple or "
            f"comma-separated string, got {type(pair).__name__}"
        )
    if len(parts) != 2:
        raise ValueError(
            f"MLAG_PAIRS entry #{pair_number} must contain exactly two leaf indexes"
        )
    try:
        return int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(
            f"MLAG_PAIRS entry #{pair_number} must contain integer leaf indexes"
        ) from exc


# ---------------------------------------------------------------------------
# Border leaf index resolution — computed ONCE, used everywhere below.
#
# border_leaf_indices is an ordered list: [left_idx, right_idx]
# The position in this list determines the "Border-N" name:
#   border_leaf_indices[0] → Border-1
#   border_leaf_indices[1] → Border-2
# ---------------------------------------------------------------------------

border_leaf_indices = []
if MLAG_PAIRS:
    _left, _right = parse_pair(MLAG_PAIRS[0], 1)
    border_leaf_indices = [_left, _right]


def leaf_name_from_index(leaf_index):
    """Return the canonical name for a leaf given its 1-based index.

    Border leaves are named Border-1, Border-2 … in the order they appear in
    MLAG_PAIRS[0].  All other leaves keep the Leaf-N convention.
    """
    if leaf_index in border_leaf_indices:
        position = border_leaf_indices.index(leaf_index) + 1
        return f"Border-{position}"
    return f"Leaf-{leaf_index}"


# ---------------------------------------------------------------------------
# Inventory skeleton
# ---------------------------------------------------------------------------

inventory = {
    "spines":         {"hosts": []},
    "border_leaves":  {"hosts": []},
    "compute_leaves": {"hosts": []},
    "mlag_leaves":    {"hosts": []},
    "leaves":         {"children": ["border_leaves", "compute_leaves"]},
    "switches":       {"children": ["spines", "leaves"]},
    "_meta":          {"hostvars": {}},
}


def base_vars(mgmt_ip):
    return {
        "ansible_host":                 mgmt_ip,
        "ansible_user":                 SSH_USER,
        "ansible_password":             SSH_PASS,
        "ansible_network_os":           "eos",
        "ansible_connection":           "httpapi",
        "ansible_httpapi_use_ssl":      True,
        "ansible_httpapi_validate_certs": False,
        "ansible_httpapi_port":         443,
        "ansible_become":               True,
        "ansible_become_method":        "enable",
    }


# ---------------------------------------------------------------------------
# Fabric P2P address plan
# ---------------------------------------------------------------------------

mgmt_counter = [MGMT_START]

spine_loopbacks = [
    {"ip": f"{LOOPBACK_SPINE}.{i+1}", "asn": SPINE_AS_BASE}
    for i in range(NUM_SPINES)
]
leaf_loopbacks = [
    {"ip": f"{LOOPBACK_LEAF}.{j+1}", "asn": LEAF_AS_BASE + j}
    for j in range(NUM_LEAVES)
]

spine_fabric = [[] for _ in range(NUM_SPINES)]
leaf_fabric   = [[] for _ in range(NUM_LEAVES)]

for i in range(NUM_SPINES):
    for j in range(NUM_LEAVES):
        offset    = (i * NUM_LEAVES + j) * 2
        spine_ip  = f"{FABRIC_SUBNET}.{offset}"
        leaf_ip   = f"{FABRIC_SUBNET}.{offset + 1}"
        spine_iface = f"Ethernet{j + 1}"
        leaf_iface  = f"Ethernet{i + 1}"

        spine_fabric[i].append({
            "interface": spine_iface,
            "ip":        spine_ip,
            "peer_ip":   leaf_ip,
            "peer_name": leaf_name_from_index(j + 1),
            "peer_asn":  LEAF_AS_BASE + j,
        })
        leaf_fabric[j].append({
            "interface": leaf_iface,
            "ip":        leaf_ip,
            "peer_ip":   spine_ip,
            "peer_name": f"Spine-{i + 1}",
            "peer_asn":  SPINE_AS_BASE,
        })


# ---------------------------------------------------------------------------
# Adapter counter helpers
#
# The adapter numbering on each leaf must exactly mirror what deploy_fabric.py
# wires up, in the same order:
#
#   Eth1 … Eth{NUM_SPINES}  → spine uplinks  (always first)
#   Eth{NUM_SPINES+1} …     → server downlinks (border leaves get NONE)
#   … continuing …          → MLAG peer-link members
#   next free               → DMZ handoff (border leaves only)
#
# border leaves intentionally receive no server links so that the MLAG
# peer-link and DMZ adapters start from the correct port number.
# ---------------------------------------------------------------------------

def build_server_interface_plan():
    """Return (server_interfaces_by_leaf, next_adapter_by_leaf).

    server_interfaces_by_leaf[j] is the list of EOS interface names on
    leaf index j+1 (0-based) that face servers.  Border leaves get an
    empty list — servers are never attached to border leaves.

    next_adapter_by_leaf[j] is the next free adapter index after all
    server links have been accounted for, ready for MLAG peer-link
    allocation.
    """
    # All leaves start with NUM_SPINES adapters consumed by spine uplinks.
    next_adapter_by_leaf   = [NUM_SPINES + 1 for _ in range(NUM_LEAVES)]
    server_interfaces_by_leaf = [[] for _ in range(NUM_LEAVES)]

    # Non-border leaves only — round-robin server assignment.
    non_border_leaf_indices = [
        j for j in range(NUM_LEAVES)
        if (j + 1) not in border_leaf_indices
    ]

    if not non_border_leaf_indices:
        # Edge case: all leaves are border leaves — no compute leaves exist.
        return server_interfaces_by_leaf, next_adapter_by_leaf

    for server_index in range(NUM_SERVERS):
        # Server-1 (index 0) is FRR — wired to the firewall, not a leaf.
        if server_index == 0 and BORDER_FIREWALL_COUNT > 0:
            continue

        # Round-robin across compute (non-border) leaves.
        bucket = server_index % len(non_border_leaf_indices)
        leaf_j = non_border_leaf_indices[bucket]

        iface = f"Ethernet{next_adapter_by_leaf[leaf_j]}"
        server_interfaces_by_leaf[leaf_j].append(iface)
        next_adapter_by_leaf[leaf_j] += 1

    return server_interfaces_by_leaf, next_adapter_by_leaf


def allocate_mlag_peer_link_members(next_adapter_by_leaf):
    """Allocate MLAG peer-link member interfaces for every pair in MLAG_PAIRS.

    Mutates next_adapter_by_leaf in place and returns:
      pair_details      — list of (pair_number, left_idx, right_idx, left_name, right_name)
      members_by_leaf   — dict leaf_name → [member interface names]
    """
    if MLAG_PEER_LINK_MEMBER_COUNT < 1:
        raise ValueError("MLAG_PEER_LINK_MEMBER_COUNT must be >= 1")

    pair_details    = []
    members_by_leaf = {}

    for pair_number, pair in enumerate(MLAG_PAIRS, start=1):
        left_idx, right_idx = parse_pair(pair, pair_number)

        for leaf_idx in (left_idx, right_idx):
            if leaf_idx < 1 or leaf_idx > NUM_LEAVES:
                raise ValueError(
                    f"MLAG_PAIRS entry #{pair_number} uses leaf index {leaf_idx}, "
                    f"but NUM_LEAVES is {NUM_LEAVES}"
                )

        left_name  = leaf_name_from_index(left_idx)
        right_name = leaf_name_from_index(right_idx)

        left_members  = []
        right_members = []
        for _ in range(MLAG_PEER_LINK_MEMBER_COUNT):
            left_members.append( f"Ethernet{next_adapter_by_leaf[left_idx  - 1]}")
            right_members.append(f"Ethernet{next_adapter_by_leaf[right_idx - 1]}")
            next_adapter_by_leaf[left_idx  - 1] += 1
            next_adapter_by_leaf[right_idx - 1] += 1

        members_by_leaf[left_name]  = left_members
        members_by_leaf[right_name] = right_members
        pair_details.append((pair_number, left_idx, right_idx, left_name, right_name))

    return pair_details, members_by_leaf


def build_mlag_leaf_vars():
    """Return (server_interfaces_by_leaf, mlag_by_leaf, next_adapter_by_leaf).

    next_adapter_by_leaf after this call points to the next free adapter on
    each leaf — used to assign the DMZ handoff interface for border leaves.
    """
    server_interfaces_by_leaf, next_adapter_by_leaf = build_server_interface_plan()
    pair_details, mlag_members_by_leaf = allocate_mlag_peer_link_members(
        next_adapter_by_leaf  # mutated in place
    )

    mlag_by_leaf = {}
    for pair_number, left_idx, right_idx, left_name, right_name in pair_details:
        ip_offset = (pair_number - 1) * 2
        left_ip   = f"{MLAG_PEER_IP_SUBNET}.{ip_offset}"
        right_ip  = f"{MLAG_PEER_IP_SUBNET}.{ip_offset + 1}"
        domain_id = f"{MLAG_DOMAIN_PREFIX}-{left_idx}-{right_idx}"

        common = {
            "domain_id":             domain_id,
            "peer_link_channel_id":  MLAG_PEER_LINK_CHANNEL,
            "peer_vlan":             MLAG_PEER_VLAN,
            "peer_vlan_name":        MLAG_PEER_VLAN_NAME,
            "trunk_group":           MLAG_TRUNK_GROUP,
            "reload_delay_mlag":     MLAG_RELOAD_DELAY_MLAG,
        }

        mlag_by_leaf[left_name] = {
            **common,
            "peer_link_members": mlag_members_by_leaf[left_name],
            "local_ip":          f"{left_ip}/31",
            "peer_ip":           right_ip,
            "peer_name":         right_name,
        }
        mlag_by_leaf[right_name] = {
            **common,
            "peer_link_members": mlag_members_by_leaf[right_name],
            "local_ip":          f"{right_ip}/31",
            "peer_ip":           left_ip,
            "peer_name":         left_name,
        }

    return server_interfaces_by_leaf, mlag_by_leaf, next_adapter_by_leaf


# ---------------------------------------------------------------------------
# Run the plan
# ---------------------------------------------------------------------------

leaf_server_interfaces, mlag_leaf_vars, next_adapter_by_leaf = build_mlag_leaf_vars()

# Grab VRF_DMZ l3_vni for Ansible vars
dmz_tenant = next((t for t in TENANTS if t["name"] == "VRF_DMZ"), None)
dmz_l3_vni = dmz_tenant["l3_vni"] if dmz_tenant else 50099

# ---------------------------------------------------------------------------
# Build Spines
# ---------------------------------------------------------------------------

for i in range(NUM_SPINES):
    name    = f"Spine-{i + 1}"
    mgmt_ip = f"{MGMT_BASE_IP}.{mgmt_counter[0]}"
    mgmt_counter[0] += 1

    vars_ = base_vars(mgmt_ip)
    vars_.update({
        "loopback_ip":        spine_loopbacks[i]["ip"],
        "loopback_interface": UNDERLAY_LOOPBACK,
        "bgp_asn":            spine_loopbacks[i]["asn"],
        "fabric_interfaces":  spine_fabric[i],
        "is_vtep":            False,
        "overlay_peers":      leaf_loopbacks,
    })
    inventory["spines"]["hosts"].append(name)
    inventory["_meta"]["hostvars"][name] = vars_

# ---------------------------------------------------------------------------
# Build Leaves
# ---------------------------------------------------------------------------

for j in range(NUM_LEAVES):
    leaf_idx  = j + 1
    is_border = leaf_idx in border_leaf_indices
    name      = leaf_name_from_index(leaf_idx)
    mgmt_ip   = f"{MGMT_BASE_IP}.{mgmt_counter[0]}"
    mgmt_counter[0] += 1

    vars_ = base_vars(mgmt_ip)
    vars_.update({
        "hostname":           name,
        "loopback_ip":        leaf_loopbacks[j]["ip"],
        "loopback_interface": UNDERLAY_LOOPBACK,
        "bgp_asn":            leaf_loopbacks[j]["asn"],
        "fabric_interfaces":  leaf_fabric[j],
        "is_vtep":            True,
        "tenants":            TENANTS,
        "vlans":              VLANS,
        "vtep_interface":     VTEP_LOOPBACK,
        "vtep_ip":            f"{VTEP_SUBNET}.{j + 1}",
        "vxlan_interface":    VXLAN_INTERFACE,
        "server_interfaces":  leaf_server_interfaces[j],
        "overlay_peers":      spine_loopbacks,
    })

    if is_border:
        inventory["border_leaves"]["hosts"].append(name)

        # Inject DMZ vars expected by border_leaf.yml.
        # The interface is the next free adapter AFTER spine + MLAG links.
        if BORDER_FIREWALL_COUNT > 0:
            position = border_leaf_indices.index(leaf_idx)   # 0 = left, 1 = right
            dmz_iface = f"Ethernet{next_adapter_by_leaf[j]}"

            vars_["dmz_handoff_interface"] = dmz_iface
            vars_["dmz_handoff_vlan"]      = DMZ_HANDOFF_VLAN
            vars_["dmz_fw_asn"]            = DMZ_FW_ASN
            vars_["dmz_l3_vni"]            = dmz_l3_vni

            if position == 0:   # Border-1 (left)
                vars_["dmz_local_ip"]   = DMZ_LEAF1_LOCAL_IP
                vars_["dmz_fw_peer_ip"] = DMZ_FW1_LAN_IP1
            else:               # Border-2 (right)
                vars_["dmz_local_ip"]   = DMZ_LEAF2_LOCAL_IP
                vars_["dmz_fw_peer_ip"] = (
                    DMZ_FW1_LAN_IP2 if BORDER_FIREWALL_COUNT == 1 else DMZ_FW2_LAN_IP1
                )
    else:
        inventory["compute_leaves"]["hosts"].append(name)

    if name in mlag_leaf_vars:
        vars_["mlag"] = mlag_leaf_vars[name]
        inventory["mlag_leaves"]["hosts"].append(name)

    inventory["_meta"]["hostvars"][name] = vars_

print(json.dumps(inventory, indent=2))
