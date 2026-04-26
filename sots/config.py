# --- Single Source of Truth ---
# Edit this file to change topology and network settings.
# deploy_fabric.py, configure_switches.py, config_frr.py, and border_leaf.yml
# all read from here (directly or via generated Ansible host_vars).

GNS3_SERVER   = "http://127.0.0.1:3080/v3"
GNS3_USER     = "admin"
GNS3_PASSWORD = "admin"

PROJECT_NAME = "DataCenter"

TEMPLATE_NAME_ARISTA   = "Arista-vEOS"
TEMPLATE_NAME_FRR      = "Debian Server"
TEMPLATE_NAME_PROXMOX  = "Proxmox VE"
TEMPLATE_NAME_FIREWALL = "FortiGate-VM"

NUM_SPINES  = 2
NUM_LEAVES  = 4
NUM_SERVERS = 3

MGMT_BASE_IP = "172.20.20"
MGMT_START   = 10
MGMT_BRIDGE  = "br-10699de2c093"

SSH_USER = "admin"
SSH_PASS = "admin"

# --- Compute node assignment ---
# Each list is round-robined across the corresponding node type.
# Use "local" for the GNS3 server itself.
COMPUTE_SPINES    = ["local"]
COMPUTE_LEAVES    = ["local"]
COMPUTE_SERVERS   = ["local"]
COMPUTE_FIREWALLS = ["local"]   # round-robined across firewall instance(s)

# --- Underlay ---
FABRIC_SUBNET     = "10.0.0"   # Base for P2P /31 links
LOOPBACK_SPINE    = "10.255.0" # Spine loopbacks: .1, .2, ...
LOOPBACK_LEAF     = "10.255.1" # Leaf  loopbacks: .1, .2, ...
SPINE_AS_BASE     = 65000
LEAF_AS_BASE      = 65100      # Leaf-1=65100, Leaf-2=65101, ...
UNDERLAY_LOOPBACK = "Loopback0"

# --- Overlay ---
VTEP_LOOPBACK   = "Loopback1"
VTEP_SUBNET     = "10.254.0"
VXLAN_INTERFACE = "Vxlan1"

# --- MLAG ---
# The FIRST pair in MLAG_PAIRS is always treated as the border leaf pair
# when BORDER_FIREWALL_COUNT > 0.  MLAG_PAIRS must be non-empty in that case.
MLAG_PAIRS                   = [[1,2]]
MLAG_DOMAIN_PREFIX           = "MLAG"
MLAG_PEER_LINK_CHANNEL       = 2000
MLAG_PEER_LINK_MEMBER_COUNT  = 2
MLAG_PEER_VLAN               = 4094
MLAG_PEER_VLAN_NAME          = "MLAG_PEER"
MLAG_TRUNK_GROUP             = "MLAG_PEER"
MLAG_PEER_IP_SUBNET          = "169.254.0"
MLAG_RELOAD_DELAY_MLAG       = 300

# ---------------------------------------------------------------------------
# DMZ / Border Firewall
# ---------------------------------------------------------------------------
# BORDER_FIREWALL_COUNT controls how many FortiGate VMs are deployed:
#
#   0  →  No DMZ. FRR is wired directly to the border leaf (legacy mode).
#          MLAG_PAIRS may be empty.
#
#   1  →  Single shared firewall.
#          Both border leaves (first MLAG pair) each get one physical link
#          to FW-1. FW-1 has two LAN-facing ports and one WAN port to FRR.
#          Path: Border-Leaf-left/right → FW-1 (LAN) → FW-1 (WAN) → FRR
#
#   2  →  Dedicated firewall per border leaf.
#          Border-Leaf-left  → FW-1 (LAN) → FW-1 (WAN) → FRR (ens1)
#          Border-Leaf-right → FW-2 (LAN) → FW-2 (WAN) → FRR (ens2)
#          FRR peers with both FW WAN ports.
#
BORDER_FIREWALL_COUNT = 1

# BGP ASN used by all firewall instances.
DMZ_FW_ASN = 65200

# 802.1Q VLAN tag used on the border-leaf subinterface facing the firewall.
# Must match the DMZ_TRANSIT VLAN in vlans.py (vlan_id: 99).
DMZ_HANDOFF_VLAN = 99

# ---------------------------------------------------------------------------
# Leaf → FW LAN /30 addressing
# ---------------------------------------------------------------------------
# Border-Leaf-LEFT (first index in MLAG_PAIRS[0]) always connects to FW-1.
DMZ_LEAF1_LOCAL_IP = "10.99.0.1/30"   # subinterface IP on Border-Leaf-left
DMZ_FW1_LAN_IP1   = "10.99.0.2"       # FW-1 LAN port-1 peer IP

# Border-Leaf-RIGHT (second index in MLAG_PAIRS[0]):
#   BORDER_FIREWALL_COUNT == 1 → connects to FW-1 LAN port-2
#   BORDER_FIREWALL_COUNT == 2 → connects to FW-2 LAN port-1
DMZ_LEAF2_LOCAL_IP = "10.99.0.5/30"   # subinterface IP on Border-Leaf-right
DMZ_FW1_LAN_IP2   = "10.99.0.6"       # FW-1 LAN port-2 peer IP (single-FW mode)
DMZ_FW2_LAN_IP1   = "10.99.0.6"       # FW-2 LAN port-1 peer IP (dual-FW mode)

# ---------------------------------------------------------------------------
# FW WAN → FRR /30 addressing
# ---------------------------------------------------------------------------
# Single-FW : FW-1 WAN ↔ FRR ens1
# Dual-FW   : FW-1 WAN ↔ FRR ens1,  FW-2 WAN ↔ FRR ens2
DMZ_FW1_WAN_IP = "10.99.1.1/30"
DMZ_FRR_IP1    = "10.99.1.2"    # FRR address on the ens1 /30 (faces FW-1)

DMZ_FW2_WAN_IP = "10.99.1.5/30"
DMZ_FRR_IP2    = "10.99.1.6"    # FRR address on the ens2 /30 (faces FW-2; dual-FW only)
