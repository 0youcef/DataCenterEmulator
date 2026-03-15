# --- Single Source of Truth ---
# Edit this file to change topology and network settings.
# Both deploy_fabric.py and configure_switches.py read from here.

GNS3_SERVER       = "http://127.0.0.1:3080/v3"
GNS3_USER         = "admin"
GNS3_PASSWORD     = "admin"

PROJECT_NAME      = "DataCenter"

TEMPLATE_NAME_ARISTA = "Arista-vEOS"
#TEMPLATE_NAME_ESXI   = "ESXi"
TEMPLATE_NAME_SERVER  = "Debian Server"

NUM_SPINES  = 2
NUM_LEAVES  = 5
NUM_SERVERS = 1

COMPUTE_SPINE  = "local"
COMPUTE_LEAF   = "local"
COMPUTE_SERVER = "local"

MGMT_BASE_IP = "172.20.20"
MGMT_START   = 10
MGMT_BRIDGE  = "br-10699de2c093"

SSH_USER = "admin"
SSH_PASS = "admin"
