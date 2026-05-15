# --- Single Source of Truth ---
# Edit this file to change topology and network settings.
# Both deploy_fabric.py and configure_switches.py read from here.

GNS3_SERVER = "http://127.0.0.1:3080/v3"
GNS3_USER = "admin"
GNS3_PASSWORD = "admin"

PROJECT_NAME = "DataCenter"

TEMPLATE_NAME_ARISTA = "Arista-vEOS"
# Server-1 (attached to Border-1) acts as the FRR internet simulator — Debian.
# All other servers are compute nodes — Proxmox.
TEMPLATE_NAME_FRR = "Debian Server"
TEMPLATE_NAME_PROXMOX = "Proxmox VE"
TEMPLATE_NAME_FIREWALL = "OPNsense"

NUM_SPINES = 1
NUM_LEAVES = 2
NUM_SERVERS = 1

MGMT_BASE_IP = "172.20.20"
MGMT_START = 10
MGMT_BRIDGE = "br0"

SSH_USER = "admin"
SSH_PASS = "admin"

# --- Compute node assignment ---
# Each list is round-robined across the corresponding node type.
# Use "local" for the GNS3 server itself.
# Examples:
#   ["local"]                       → all on the local machine
#   ["local", "compute-1"]          → alternates between local and compute-1
#   ["compute-1", "compute-2"]      → spreads across two remote computes
#
# The names must exactly match the compute names shown in GNS3 preferences.
COMPUTE_SPINES = ["local"]  # round-robined across all spines
COMPUTE_LEAVES = ["local", "local"]  # round-robined across all leaves
COMPUTE_SERVERS = ["local", "local", "local"]  # round-robined across all servers
COMPUTE_FIREWALLS = ["local"]  # round-robined across firewalls

# --- Firewall / DMZ ---
ENABLE_DMZ_FIREWALL = True
FIREWALL_NODE_NAME = "Firewall-1"
FIREWALL_WAN_UPSTREAM_NODE_NAME = "Server-1"

# Adapter map in deploy_fabric.py:
#   adapter 0 = management, 1 = WAN, 2 = LAN, 3 = DMZ
FIREWALL_LAN_BORDER_LEAF_INDEX = 2
FIREWALL_DMZ_BORDER_LEAF_INDEX = 1

# OPNsense console login (used by configs/config_firewalls.py)
FIREWALL_CONSOLE_USER = "root"
FIREWALL_CONSOLE_PASS = "opnsense"

# OPNsense interface names (vtnet* for VirtIO templates by default)
FIREWALL_MGMT_IFACE = "vtnet0"
FIREWALL_WAN_IFACE = "vtnet1"
#!!!! missleading comment: the "LAN" interface is actually linking to one border leaf,
# and the "DMZ" interface is linking to the other border leaf. Both are trunking multiple VLANs.

FIREWALL_LAN_IFACE = "vtnet2"
FIREWALL_DMZ_IFACE = "vtnet3"

# Non-conflicting default subnets (change as needed)
FIREWALL_WAN_CIDR = "10.2.0.2/24"
FIREWALL_WAN_GATEWAY = "10.2.0.1"
FIREWALL_LAN_CIDR = "10.31.0.254/24"  # changed from 172.31.0.1/24
FIREWALL_DMZ_CIDR = "10.31.10.254/24"  # changed from 172.31.10.1/24


# Basic inbound DNAT from WAN to DMZ host (optional placeholder)
FIREWALL_DMZ_EXPOSED_HOST = "10.31.10.10"  # changed from 172.31.10.10
FIREWALL_DMZ_EXPOSED_PORTS = [22, 80, 443]

# eBGP on OPNsense FRR (placeholder-friendly; adjust as needed)
FIREWALL_BGP_ASN = 65050
# Keep empty to auto-build neighbors from sots/vlans.py external_handoff
# values (Border-1/Border-2 handoff_local_ip and *_2).
FIREWALL_BGP_NEIGHBORS = []

# FRR (Server-1) upstream peering toward OPNsense WAN
FRR_WAN_IFACE = "ens1"
FRR_WAN_CIDR = "10.2.0.1/24"
FRR_WAN_PEER_IP = "10.2.0.2"
FRR_BGP_ASN = 65999

# --- Underlay ---
FABRIC_SUBNET = "10.0.0"  # Base for P2P /31 links: 10.0.0.0/31, 10.0.0.2/31, ...
LOOPBACK_SPINE = "10.255.0"  # Spine underlay loopbacks: .1, .2, ...
LOOPBACK_LEAF = "10.255.1"  # Leaf  underlay loopbacks: .1, .2, ...
SPINE_AS_BASE = 65000  # All Spines share this ASN
LEAF_AS_BASE = 65100  # Leaf-1=65100, Leaf-2=65101, ...
UNDERLAY_LOOPBACK = "Loopback0"

# --- Overlay ---
VTEP_LOOPBACK = (
    "Loopback1"  # Source interface for VXLAN tunnels (must differ from Loopback0)
)
VTEP_SUBNET = "10.254.0"  # Base for per-leaf VTEP /32 addresses: .1, .2, ...
VXLAN_INTERFACE = "Vxlan1"

# --- MLAG ---
# MLAG_PAIRS = [[1, 2], [3, 4], [5, 6], [7, 8]]
MLAG_PAIRS = [[1, 2]]
MLAG_DOMAIN_PREFIX = "MLAG"
MLAG_PEER_LINK_CHANNEL = 2000
MLAG_PEER_LINK_MEMBER_COUNT = 2
MLAG_PEER_VLAN = 4094
MLAG_PEER_VLAN_NAME = "MLAG_PEER"
MLAG_TRUNK_GROUP = "MLAG_PEER"
MLAG_PEER_IP_SUBNET = "169.254.0"
MLAG_RELOAD_DELAY_MLAG = 300
