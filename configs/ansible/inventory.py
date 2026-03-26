#!/usr/bin/env python3
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from sots.config import (
    NUM_SPINES, NUM_LEAVES, NUM_SERVERS,
    MGMT_BASE_IP, MGMT_START,
    SSH_USER, SSH_PASS,
    FABRIC_SUBNET, LOOPBACK_SPINE, LOOPBACK_LEAF,
    SPINE_AS_BASE, LEAF_AS_BASE,
    UNDERLAY_LOOPBACK, VTEP_LOOPBACK, VTEP_SUBNET, VXLAN_INTERFACE,
)
from sots.vlans import VLANS, TENANTS

inventory = {
    "spines":         {"hosts": []},
    "border_leaves":  {"hosts": []},
    "compute_leaves": {"hosts": []},
    "leaves":         {"children": ["border_leaves", "compute_leaves"]},
    "switches":       {"children": ["spines", "leaves"]},
    "_meta":          {"hostvars": {}}
}

def base_vars(mgmt_ip):
    return {
        "ansible_host":                   mgmt_ip,
        "ansible_user":                   SSH_USER,
        "ansible_password":               SSH_PASS,
        "ansible_network_os":             "eos",
        "ansible_connection":             "httpapi",
        "ansible_httpapi_use_ssl":        True,
        "ansible_httpapi_validate_certs": False,
        "ansible_httpapi_port":           443,
        "ansible_become":                 True,
        "ansible_become_method":          "enable",
    }

mgmt = MGMT_START
spine_fabric = [[] for _ in range(NUM_SPINES)]
leaf_fabric  = [[] for _ in range(NUM_LEAVES)]

spine_loopbacks = [{"ip": f"{LOOPBACK_SPINE}.{i+1}", "asn": SPINE_AS_BASE} for i in range(NUM_SPINES)]
leaf_loopbacks  = [{"ip": f"{LOOPBACK_LEAF}.{j+1}",  "asn": LEAF_AS_BASE + j} for j in range(NUM_LEAVES)]

for i in range(NUM_SPINES):
    for j in range(NUM_LEAVES):
        offset      = (i * NUM_LEAVES + j) * 2
        spine_ip    = f"{FABRIC_SUBNET}.{offset}"
        leaf_ip     = f"{FABRIC_SUBNET}.{offset + 1}"
        spine_iface = f"Ethernet{j + 1}"
        leaf_iface  = f"Ethernet{i + 1}"

        peer_leaf_name = "Border-1" if j == 0 else f"Leaf-{j + 1}"

        spine_fabric[i].append({
            "interface": spine_iface, "ip": spine_ip, "peer_ip": leaf_ip,
            "peer_name": peer_leaf_name, "peer_asn": LEAF_AS_BASE + j,
        })
        leaf_fabric[j].append({
            "interface": leaf_iface, "ip": leaf_ip, "peer_ip": spine_ip,
            "peer_name": f"Spine-{i + 1}", "peer_asn": SPINE_AS_BASE,
        })

# FIX: Build external_handoffs dynamically from TENANTS so that
# vrf names and l3_vnis never drift from the vlans.py SSOT.
# Each tenant that has "external_handoff: true" gets an entry.
# The interface, IPs and peer ASN come from config.py constants
# (BORDER_HANDOFFS) — see config.py for how to add new ones.
def build_external_handoffs(tenants):
    handoffs = []
    for t in tenants:
        if not t.get("external_handoff"):
            continue
        handoffs.append({
            "vrf":       t["name"],
            "l3_vni":    t["l3_vni"],
            "interface": t["handoff_interface"],
            "vlan":      t["handoff_vlan"],   # 802.1Q tag — was missing, causing item.vlan error
            "local_ip":  t["handoff_local_ip"],
            "peer_ip":   t["handoff_peer_ip"],
            "peer_asn":  t["handoff_peer_asn"],
        })
    return handoffs

# Build Spines
for i in range(NUM_SPINES):
    name    = f"Spine-{i + 1}"
    mgmt_ip = f"{MGMT_BASE_IP}.{mgmt}"
    mgmt   += 1

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

# Build Leaves (Border & Compute)
for j in range(NUM_LEAVES):
    is_border = (j == 0)
    name      = "Border-1" if is_border else f"Leaf-{j + 1}"
    mgmt_ip   = f"{MGMT_BASE_IP}.{mgmt}"
    mgmt     += 1

    vars_ = base_vars(mgmt_ip)
    server_interfaces = [f"Ethernet{NUM_SPINES + k + 1}" for k in range(NUM_SERVERS // NUM_LEAVES + 1)]

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
        "server_interfaces":  server_interfaces,
        "overlay_peers":      spine_loopbacks,
    })

    if is_border:
        # Derived from TENANTS — no hardcoded vrf names or l3_vnis here
        vars_["external_handoffs"] = build_external_handoffs(TENANTS)
        inventory["border_leaves"]["hosts"].append(name)
    else:
        inventory["compute_leaves"]["hosts"].append(name)

    inventory["_meta"]["hostvars"][name] = vars_

print(json.dumps(inventory, indent=2))
