# sots/vlans.py

# Define the Tenant VRFs and their dedicated L3 transit tunnels
TENANTS = [
    {
        "name":              "VRF_PEDAGOGY",
        "l3_vni":            50010,
        "external_handoff":  True,
        "handoff_interface": "Ethernet3",   # physical parent interface on Border-1
        "handoff_vlan":      10,             # 802.1Q tag — subinterface will be Ethernet10.10
        "handoff_local_ip":  "10.1.0.1/30",
        "handoff_peer_ip":   "10.1.0.2",
        "handoff_peer_asn":  65999,
    },
    {
        "name":              "VRF_RESEARCH",
        "l3_vni":            50020,
        "external_handoff":  True,
        "handoff_interface": "Ethernet3",   # same physical interface
        "handoff_vlan":      20,             # different tag — subinterface will be Ethernet10.20
        "handoff_local_ip":  "10.1.1.1/30",
        "handoff_peer_ip":   "10.1.1.2",
        "handoff_peer_asn":  65999,
    },
]

# Define the local subnets and map them to the Tenant VRFs
VLANS = [
    # Pedagogy Networks
    {
        "vlan_id":    10,
        "name":       "WEB_SERVERS",
        "vni":        10010,
        "vrf":        "VRF_PEDAGOGY",
        "anycast_ip": "192.168.10.1/24"
    },
    {
        "vlan_id":    11,
        "name":       "DB_SERVERS",
        "vni":        10011,
        "vrf":        "VRF_PEDAGOGY",
        "anycast_ip": "192.168.11.1/24"
    },
    # Research Networks (Isolated)
    {
        "vlan_id":    20,
        "name":       "AI_CLUSTER",
        "vni":        10020,
        "vrf":        "VRF_RESEARCH",
        "anycast_ip": "192.168.20.1/24"
    }
]
