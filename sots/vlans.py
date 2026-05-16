# Define tenant VRFs and their L3 VNIs.
#
# external_handoff tenants require one /30 handoff per border leaf:
# - Border-1: handoff_local_ip / handoff_peer_ip
# - Border-2: handoff_local_ip_2 / handoff_peer_ip_2
#
# In this firewall-centered design:
# - handoff_local_ip*  are the border leaf IPs on the /30 link (BGP local)
# - handoff_peer_ip*   are the OPNsense subinterface IPs (BGP peer)
# - handoff_peer_asn   is the OPNsense BGP ASN
#
# Physical wiring (with NUM_SPINES=1, MLAG_PEER_LINK_MEMBER_COUNT=2):
#   Both VRFs (PEDAGOGY and DMZ) trunk their VLANs over a SINGLE physical
#   link per border leaf using 802.1Q subinterfaces.
#
#   Per-leaf adapter layout:
#     Eth1  → Spine uplink
#     Eth2  → MLAG peer link member 1
#     Eth3  → MLAG peer link member 2
#     Eth4  → Firewall trunk  ← handoff_interface for all external_handoff tenants
#
#   Firewall vtnet mapping (from sots/config.py):
#     vtnet2 (FIREWALL_BORDER2_IFACE) → Border-2  →  uses handoff_peer_ip_2
#     vtnet3 (FIREWALL_BORDER1_IFACE) → Border-1  →  uses handoff_peer_ip

TENANTS = [
    {
        "name": "VRF_PEDAGOGY",
        "l3_vni": 50010,
        "l3": True,
        # When true, originate 0.0.0.0/0 toward external peers for this VRF.
        "originate_default_route": True,
        # Import VRF_RESEARCH routes into this VRF (symmetric with VRF_RESEARCH below).
        "import_rts": [50020],
        "external_handoff": True,
        # Both tenants share Ethernet4 — the single physical trunk to the firewall.
        # VLAN 110 tags this tenant's traffic on that trunk.
        "handoff_interface": "Ethernet4",
        "handoff_vlan": 110,
        "handoff_local_ip": "10.31.0.1/30",  # Border-1 leaf IP
        "handoff_peer_ip": "10.31.0.2",  # OPNsense vtnet3.110 (FIREWALL_BORDER1_IFACE)
        "handoff_local_ip_2": "10.31.0.5/30",  # Border-2 leaf IP
        "handoff_peer_ip_2": "10.31.0.6",  # OPNsense vtnet2.110 (FIREWALL_BORDER2_IFACE)
        "handoff_peer_asn": 65050,
    },
    {
        "name": "VRF_RESEARCH",
        "l3_vni": 50020,
        "l3": True,
        # Import VRF_PEDAGOGY routes into this VRF (symmetric with VRF_PEDAGOGY above).
        # Both directions must be declared for inter-VRF traffic to flow symmetrically.
        "import_rts": [50010],
    },
    {
        "name": "VRF_DMZ",
        "l3_vni": 50030,
        "l3": True,
        # When true, originate 0.0.0.0/0 toward external peers for this VRF.
        "originate_default_route": True,
        "external_handoff": True,
        # Same physical interface as PEDAGOGY — VLAN 130 distinguishes DMZ traffic.
        "handoff_interface": "Ethernet4",
        "handoff_vlan": 130,
        "handoff_local_ip": "10.31.10.1/30",  # Border-1 leaf IP
        "handoff_peer_ip": "10.31.10.2",  # OPNsense vtnet3.130 (FIREWALL_BORDER1_IFACE)
        "handoff_local_ip_2": "10.31.10.5/30",  # Border-2 leaf IP
        "handoff_peer_ip_2": "10.31.10.6",  # OPNsense vtnet2.130 (FIREWALL_BORDER2_IFACE)
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
