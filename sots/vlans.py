# Define the Tenant VRFs and their dedicated L3 transit tunnels.
#
# import_rts: list of l3_vni values to import from other VRFs.
# Adding VRF_B's l3_vni to VRF_A's import_rts makes VRF_A's
# leaves install VRF_B's prefixes — enabling inter-VRF routing.
# Both sides must import each other (symmetric leaking).
#
# external_handoff tenants require two /30 sessions — one per border leaf:
#
#   Border-1 session  (FRR ens1.<vlan> ↔ Border-1 Ethernet3.<vlan>)
#     handoff_local_ip   — Border-1's IP/prefix length
#     handoff_peer_ip    — FRR's IP on ens1.<vlan>  (Border-1's BGP peer)
#
#   Border-2 session  (FRR ens2.<vlan> ↔ Border-2 Ethernet3.<vlan>)
#     handoff_local_ip_2 — Border-2's IP/prefix length
#     handoff_peer_ip_2  — FRR's IP on ens2.<vlan>  (Border-2's BGP peer)
#
#   handoff_peer_asn is FRR's own ASN (same value for both sessions).

TENANTS = [
    {
        "name":             "VRF_PEDAGOGY",
        "l3_vni":           50010,
        "l3":               True,
        # Import VRF_RESEARCH routes so pedagogy hosts can reach research hosts
        "import_rts":       [50020],
        "external_handoff":  True,
        "handoff_interface": "Ethernet3",
        "handoff_vlan":      10,

        # ── Border-1 session: 10.1.0.0/30 ─────────────────────────────────
        # FRR ens1.10  ↔  Border-1 Ethernet3.10
        "handoff_local_ip":   "10.1.0.1/30",   # Border-1's IP
        "handoff_peer_ip":    "10.1.0.2",       # FRR's IP on ens1.10

        # ── Border-2 session: 10.1.0.4/30 ─────────────────────────────────
        # FRR ens2.10  ↔  Border-2 Ethernet3.10
        # inventory.py uses these when building external_handoffs for Border-2.
        "handoff_local_ip_2": "10.1.0.5/30",   # Border-2's IP
        "handoff_peer_ip_2":  "10.1.0.6",       # FRR's IP on ens2.10

        "handoff_peer_asn":  65999,
    },
    {
        "name":       "VRF_RESEARCH",
        "l3_vni":     50020,
        "l3":         True,
        # Import VRF_PEDAGOGY routes so research hosts can reach pedagogy hosts
        "import_rts": [50010],
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
    # Research Networks
    {
        "vlan_id":    20,
        "name":       "AI_CLUSTER",
        "vni":        10020,
        "vrf":        "VRF_RESEARCH",
        "anycast_ip": "192.168.20.1/24"
    },
]
