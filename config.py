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
NUM_LEAVES  = 3
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
FABRIC_SUBNET    = "10.0.0"      # Base for P2P /31 links. 10.0.0.0/31, 10.0.0.2/31, ...
LOOPBACK_SPINE   = "10.255.0"    # Spine loopbacks: .1, .2, ...
LOOPBACK_LEAF    = "10.255.1"    # Leaf loopbacks:  .1, .2, ...
SPINE_AS_BASE    = 65000         # Spine-1=65000, Spine-2=65001, ...
LEAF_AS_BASE     = 65100         # Leaf-1=65100,  Leaf-2=65101, ...

# --- Overlay ---
# List of tenant VLANs to provision. Add more dicts to extend.
# vni must be unique per VLAN
VLANS = [
    {"name": "tenant-1", "vlan_id": 10, "vni": 10010},
    {"name": "tenant-2", "vlan_id": 20, "vni": 10020},
]
VTEP_LOOPBACK = "Loopback0"     # Source interface for VXLAN tunnels
