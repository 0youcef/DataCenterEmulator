# Define the Tenant VRFs and their dedicated L3 transit tunnels.
#
# Route-leaking strategy
# ----------------------
#   VRF_PEDAGOGY  imports [VRF_RESEARCH, VRF_DMZ]
#     → east-west to research hosts + internet routes via DMZ
#
#   VRF_RESEARCH  imports [VRF_PEDAGOGY, VRF_DMZ]
#     → east-west to pedagogy hosts + internet routes via DMZ
#
#   VRF_DMZ       imports [VRF_PEDAGOGY, VRF_RESEARCH]
#     → sees tenant prefixes so the firewall can route return traffic
#       without NAT; also redistributed as EVPN type-5 from border leaves
#
# All internet-bound traffic is forced through VRF_DMZ → FortiGate → FRR.
# The FortiGate is the only entity that directly peers with FRR over BGP.
# Tenants never have a direct external handoff; they reach the internet
# only via route leaking from VRF_DMZ.

TENANTS = [
    {
        "name":       "VRF_PEDAGOGY",
        "l3_vni":     50010,
        "l3":         True,
        # 50020 = east-west to VRF_RESEARCH
        # 50099 = internet routes learned via VRF_DMZ (from FortiGate/FRR)
        "import_rts": [50020, 50099],
    },
    {
        "name":       "VRF_RESEARCH",
        "l3_vni":     50020,
        "l3":         True,
        # 50010 = east-west to VRF_PEDAGOGY
        # 50099 = internet routes learned via VRF_DMZ
        "import_rts": [50010, 50099],
    },
    {
        # ---------------------------------------------------------------
        # VRF_DMZ — DMZ choke-point, border-leaf only.
        # ---------------------------------------------------------------
        # Configured ONLY on the border leaves (first MLAG pair).
        # Each border leaf has a subinterface in this VRF facing the
        # FortiGate LAN port.  The exact per-leaf IP addresses come from
        # config.py (DMZ_LEAF1_LOCAL_IP / DMZ_LEAF2_LOCAL_IP) because
        # each leaf uses its own /30 — even in single-firewall mode.
        #
        # BGP peer ASN  → config.DMZ_FW_ASN
        # Handoff VLAN  → config.DMZ_HANDOFF_VLAN  (= vlan_id 99 below)
        # Interface name → computed dynamically during deploy; written
        #                  into the Ansible host_vars by configure_switches.py
        # ---------------------------------------------------------------
        "name":             "VRF_DMZ",
        "l3_vni":           50099,
        "l3":               True,
        # Import tenant prefixes so FortiGate can route return traffic back
        "import_rts":       [50010, 50020],
        "external_handoff":  True,
        "handoff_vlan":      99,
        # handoff_interface and per-leaf IPs are injected from config.py
        # at configure-time; do not hardcode them here.
    },
]

# Define the local subnets and map them to the Tenant VRFs.
VLANS = [
    # ── Pedagogy Networks ──────────────────────────────────────────────
    {
        "vlan_id":    10,
        "name":       "WEB_SERVERS",
        "vni":        10010,
        "vrf":        "VRF_PEDAGOGY",
        "anycast_ip": "192.168.10.1/24",
    },
    {
        "vlan_id":    11,
        "name":       "DB_SERVERS",
        "vni":        10011,
        "vrf":        "VRF_PEDAGOGY",
        "anycast_ip": "192.168.11.1/24",
    },
    # ── Research Networks ──────────────────────────────────────────────
    {
        "vlan_id":    20,
        "name":       "AI_CLUSTER",
        "vni":        10020,
        "vrf":        "VRF_RESEARCH",
        "anycast_ip": "192.168.20.1/24",
    },
    # ── DMZ Transit VLAN ───────────────────────────────────────────────
    # Carries subinterface traffic between border leaves and the FortiGate.
    # No anycast_ip: each border leaf uses its own /30 address (config.py).
    # vni 10099 is the L2 VNI; the L3 VNI for this VRF is 50099 above.
    {
        "vlan_id":    99,
        "name":       "DMZ_TRANSIT",
        "vni":        10099,
        "vrf":        "VRF_DMZ",
        "anycast_ip": None,
    },
]
