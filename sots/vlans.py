# Define tenant VRFs and their L3 VNIs.
#
# external_handoff tenants require one /30 handoff per border leaf:
# - Border-1: handoff_local_ip / handoff_peer_ip
# - Border-2: handoff_local_ip_2 / handoff_peer_ip_2
#
# In this firewall-centered design:
# - handoff_peer_ip* are OPNsense interface IPs (BGP peers)
# - handoff_peer_asn is the OPNsense BGP ASN

TENANTS = [
    {
        "name": "VRF_PEDAGOGY",
        "l3_vni": 50010,
        "l3": True,
        "import_rts": [50020],
        "external_handoff": True,
        "handoff_interface": "Ethernet3",
        "handoff_vlan": 110,
        "handoff_local_ip": "10.31.0.1/30",
        "handoff_peer_ip": "10.31.0.2",
        "handoff_local_ip_2": "10.31.0.5/30",
        "handoff_peer_ip_2": "10.31.0.6",
        "handoff_peer_asn": 65050,
    },
    {
        "name": "VRF_RESEARCH",
        "l3_vni": 50020,
        "l3": True,
        "import_rts": [50010],
    },
    {
        "name": "VRF_DMZ",
        "l3_vni": 50030,
        "l3": True,
        "external_handoff": True,
        "handoff_interface": "Ethernet4",
        "handoff_vlan": 130,
        "handoff_local_ip": "10.31.10.1/30",
        "handoff_peer_ip": "10.31.10.2",
        "handoff_local_ip_2": "10.31.10.5/30",
        "handoff_peer_ip_2": "10.31.10.6",
        "handoff_peer_asn": 65050,
    },
]

VLANS = [
    {
        "vlan_id": 10,
        "name": "WEB_SERVERS",
        "vni": 10010,
        "vrf": "VRF_PEDAGOGY",
        "anycast_ip": "192.168.10.1/24",
    },
    {
        "vlan_id": 11,
        "name": "DB_SERVERS",
        "vni": 10011,
        "vrf": "VRF_PEDAGOGY",
        "anycast_ip": "192.168.11.1/24",
    },
    {
        "vlan_id": 20,
        "name": "AI_CLUSTER",
        "vni": 10020,
        "vrf": "VRF_RESEARCH",
        "anycast_ip": "192.168.20.1/24",
    },
    {
        "vlan_id": 30,
        "name": "DMZ_SERVICES",
        "vni": 10030,
        "vrf": "VRF_DMZ",
        "anycast_ip": "192.168.30.1/24",
    },
]
