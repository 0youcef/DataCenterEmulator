# --- Single Source of Truth ---
# Edit this file to change topology and network settings.
# Both deploy_fabric.py and configure_switches.py read from here.

GNS3_SERVER       = "http://127.0.0.1:3080/v3"
GNS3_USER         = "admin"
GNS3_PASSWORD     = "admin"

PROJECT_NAME      = "DataCenter"

TEMPLATE_NAME_ARISTA  = "Arista-vEOS"
TEMPLATE_NAME_SERVER  = "Debian Server"

NUM_SPINES  = 2
NUM_LEAVES  = 5
NUM_SERVERS = 2

COMPUTE_SPINE  = "local"
COMPUTE_LEAF   = "local"
COMPUTE_SERVER = "local"

MGMT_BASE_IP = "172.20.20"
MGMT_START   = 10
MGMT_BRIDGE  = "br-10699de2c093"

SSH_USER = "admin"
SSH_PASS = "admin"

# --- Underlay ---
FABRIC_SUBNET      = "10.0.0"    # Base for P2P /31 links: 10.0.0.0/31, 10.0.0.2/31, ...
LOOPBACK_SPINE     = "10.255.0"  # Spine underlay loopbacks: .1, .2, ...
LOOPBACK_LEAF      = "10.255.1"  # Leaf  underlay loopbacks: .1, .2, ...
SPINE_AS_BASE      = 65000       # All Spines share this ASN
LEAF_AS_BASE       = 65100       # Leaf-1=65100, Leaf-2=65101, ...
UNDERLAY_LOOPBACK  = "Loopback0" # BGP router-id / underlay loopback interface

# --- Overlay ---
# VTEP_LOOPBACK *must* differ from UNDERLAY_LOOPBACK.
# Loopback0 is already consumed by the underlay BGP router-id; use Loopback1 for VTEPs.
VTEP_LOOPBACK   = "Loopback1"   # Source interface for VXLAN tunnels
VTEP_SUBNET     = "10.254.0"    # Base for per-leaf VTEP /32 addresses: .1, .2, ...
VXLAN_INTERFACE = "Vxlan1"      # VXLAN logical interface name on Arista EOS
