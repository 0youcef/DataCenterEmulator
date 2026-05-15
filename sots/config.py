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
COMPUTE_SERVERS = ["local"]  # round-robined across all servers (NUM_SERVERS = 1)
COMPUTE_FIREWALLS = ["local"]  # round-robined across firewalls

# --- Firewall / DMZ ---
ENABLE_DMZ_FIREWALL = True
FIREWALL_NODE_NAME = "Firewall-1"
FIREWALL_WAN_UPSTREAM_NODE_NAME = "Server-1"

# Firewall adapter wiring (set by deploy_fabric.py):
#   adapter 0 → vtnet0  management (br0)
#   adapter 1 → vtnet1  WAN        (Server-1 / FRR upstream)
#   adapter 2 → vtnet2  Border-2   (FIREWALL_BORDER2_LEAF_INDEX)
#   adapter 3 → vtnet3  Border-1   (FIREWALL_BORDER1_LEAF_INDEX)
#
# Both vtnet2 and vtnet3 are 802.1Q trunk parents — they carry ALL tenant
# VLANs via subinterfaces.  Neither interface carries a single "zone".
FIREWALL_BORDER2_LEAF_INDEX = 2  # Leaf index wired to vtnet2
FIREWALL_BORDER1_LEAF_INDEX = 1  # Leaf index wired to vtnet3

# OPNsense console login (used by configs/config_firewalls.py)
FIREWALL_CONSOLE_USER = "root"
FIREWALL_CONSOLE_PASS = "opnsense"

# OPNsense interface names (vtnet* for VirtIO templates by default)
FIREWALL_MGMT_IFACE = "vtnet0"  # management
FIREWALL_WAN_IFACE = "vtnet1"  # WAN (toward FRR / Server-1)
FIREWALL_BORDER2_IFACE = "vtnet2"  # 802.1Q trunk → Border-2 (uses handoff_peer_ip_2)
FIREWALL_BORDER1_IFACE = "vtnet3"  # 802.1Q trunk → Border-1 (uses handoff_peer_ip)

# WAN interface addressing
FIREWALL_WAN_CIDR = "10.2.0.2/24"
FIREWALL_WAN_GATEWAY = "10.2.0.1"

# Inbound DNAT from WAN to a DMZ host (placeholder — adjust to real VM IP).
# The target must be a host in the DMZ_SERVICES VLAN overlay subnet
# (192.168.30.x/24 as defined by the DMZ_SERVICES anycast_ip in sots/vlans.py).
FIREWALL_DMZ_EXPOSED_HOST = "192.168.30.10"
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
