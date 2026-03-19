#!/usr/bin/env python3
import json
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import (
    NUM_SPINES, NUM_LEAVES, NUM_SERVERS,
    MGMT_BASE_IP, MGMT_START,
    SSH_USER, SSH_PASS,
    FABRIC_SUBNET, LOOPBACK_SPINE, LOOPBACK_LEAF,
    SPINE_AS_BASE, LEAF_AS_BASE,
    UNDERLAY_LOOPBACK, VTEP_LOOPBACK, VTEP_SUBNET, VXLAN_INTERFACE,
)
from vlans import VLANS

inventory = {
    "spines":   {"hosts": []},
    "leaves":   {"hosts": []},
    "switches": {"children": ["spines", "leaves"]},
    "_meta":    {"hostvars": {}}
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

# Pre-calculate all Loopbacks and ASNs for the EVPN Overlay
spine_loopbacks = []
leaf_loopbacks  = []

for i in range(NUM_SPINES):
    spine_loopbacks.append({
        "ip":  f"{LOOPBACK_SPINE}.{i + 1}",
        "asn": SPINE_AS_BASE,          # All Spines share the same ASN
    })

for j in range(NUM_LEAVES):
    leaf_loopbacks.append({
        "ip":  f"{LOOPBACK_LEAF}.{j + 1}",
        "asn": LEAF_AS_BASE + j,       # Every Leaf gets a unique ASN
    })

# Build per-switch fabric interface lists
for i in range(NUM_SPINES):
    for j in range(NUM_LEAVES):
        offset      = (i * NUM_LEAVES + j) * 2
        spine_ip    = f"{FABRIC_SUBNET}.{offset}"
        leaf_ip     = f"{FABRIC_SUBNET}.{offset + 1}"
        spine_iface = f"Ethernet{j + 1}"
        leaf_iface  = f"Ethernet{i + 1}"

        spine_fabric[i].append({
            "interface": spine_iface,
            "ip":        spine_ip,
            "peer_ip":   leaf_ip,
            "peer_name": f"Leaf-{j + 1}",
            "peer_asn":  LEAF_AS_BASE + j,
        })
        leaf_fabric[j].append({
            "interface": leaf_iface,
            "ip":        leaf_ip,
            "peer_ip":   spine_ip,
            "peer_name": f"Spine-{i + 1}",
            "peer_asn":  SPINE_AS_BASE,
        })

# Spines
for i in range(NUM_SPINES):
    name    = f"Spine-{i + 1}"
    mgmt_ip = f"{MGMT_BASE_IP}.{mgmt}"
    mgmt   += 1

    vars_ = base_vars(mgmt_ip)
    vars_.update({
        "loopback_ip":         spine_loopbacks[i]["ip"],
        "loopback_interface":  UNDERLAY_LOOPBACK,
        "bgp_asn":             spine_loopbacks[i]["asn"],
        "fabric_interfaces":   spine_fabric[i],
        "is_vtep":             False,
        "overlay_peers":       leaf_loopbacks,      # Spines peer with all Leaves
    })

    inventory["spines"]["hosts"].append(name)
    inventory["_meta"]["hostvars"][name] = vars_

# Leaves
for j in range(NUM_LEAVES):
    name    = f"Leaf-{j + 1}"
    mgmt_ip = f"{MGMT_BASE_IP}.{mgmt}"
    mgmt   += 1

    vars_ = base_vars(mgmt_ip)

    # Server-facing interfaces start after the spine uplinks
    server_interfaces = [
        f"Ethernet{NUM_SPINES + k + 1}"
        for k in range(NUM_SERVERS // NUM_LEAVES + 1)
    ]

    vars_.update({
        "loopback_ip":         leaf_loopbacks[j]["ip"],
        "loopback_interface":  UNDERLAY_LOOPBACK,
        "bgp_asn":             leaf_loopbacks[j]["asn"],
        "fabric_interfaces":   leaf_fabric[j],
        "is_vtep":             True,
        "vlans":               VLANS,
        "vtep_interface":      VTEP_LOOPBACK,               # Loopback1 — distinct from underlay loopback
        "vtep_ip":             f"{VTEP_SUBNET}.{j + 1}",    # e.g. 10.254.0.1
        "vxlan_interface":     VXLAN_INTERFACE,              # Vxlan1
        "server_interfaces":   server_interfaces,
        "overlay_peers":       spine_loopbacks,             # Leaves peer with all Spines
    })

    inventory["leaves"]["hosts"].append(name)
    inventory["_meta"]["hostvars"][name] = vars_

print(json.dumps(inventory, indent=2))
