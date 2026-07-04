# GNS3 Spine-Leaf Data Center Emulator

Automated deployment of a production-style spine-leaf network in **GNS3** using Python, Ansible, and Terraform. Deploys Arista vEOS switches, OPNsense firewall, FRR internet simulator, and Proxmox VE compute nodes — wired with **MLAG**, **eBGP underlay**, and **EVPN/VXLAN overlay**.

All settings live in a **Single Source of Truth (SSOT)** — edit two Python files to resize or re-address the entire fabric, then re-run the pipeline.

> Study/lab project for data center networking and automation.

---

## Table of Contents

- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration (SSOT)](#configuration-ssot)
- [Step-by-Step Usage Guide](#step-by-step-usage-guide)
- [One-Shot Launch](#one-shot-launch)
- [Adding Nodes to a Running Topology](#adding-nodes-to-a-running-topology)
- [Optional: Provisioning VPS Compute with Terraform](#optional-provisioning-vps-compute-with-terraform)
- [Optional: Monitoring Stack (Prometheus + Grafana)](#optional-monitoring-stack-prometheus--grafana)
- [GNS3 API Reference](#gns3-api-reference)
- [File Structure](#file-structure)
- [Troubleshooting](#troubleshooting)
- [Security Notes](#security-notes)

---

## Architecture

```
                          ┌──────────────┐
                          │   Cloud (br0)│
                          └──────┬───────┘
                                 │
                          ┌──────┴─────────┐
                          │  MGMT-Switch   │  (24-port ethernet switch)
                          └──┬────┬────┬───┘
                             │    │    │   ...
              ┌──────────────┘    │    └──────────────┐
              │                   │                   │
         ┌────┴────┐         ┌────┴────┐         ┌────┴────┐
         │ Spine-1 │  ...    │ Spine-N │         │ FW /    │
         └─┬──┬──┬─┘         └─┬──┬──┬─┘         │ OPNsense│
           │  │  │             │  │  │           └────┬────┘
     ┌─────┘  │  └─────────┐   │  │  │                │
     │        │            │  ... ... ...      vtnet2 │ vtnet3
 ┌───┴───┐  ┌─┴─────┐  ┌───┴──┐                   │         │
 │Border │  │Border │  │Leaf-3│                Border-2  Border-1
 │  -1   │  │  -2   │  │(MLAG)│                 (MLAG)    (MLAG)
 └───┬───┘  └─┬─────┘  └───┬──┘
     │        │            │
     │        │        ┌───┴───┐
     │        │        │Server-│ (Proxmox VE, dual-homed)
     │        │        │  2    │
     │        │        └───────┘
 ┌───┴────────┴──┐
 │   Server-1    │ (Debian + FRR internet simulator)
 └───────────────┘
         │  vtnet1 (WAN)
         └────── OPNsense WAN
```

### Node Types

| Node               | Template        | Role                                                             |
| ------------------ | --------------- | ---------------------------------------------------------------- |
| Spine-N            | Arista-vEOS     | BGP underlay transit, EVPN route reflectors                      |
| Leaf-N             | Arista-vEOS     | VTEPs, symmetric IRB, anycast gateways                           |
| Border-1, Border-2 | Arista-vEOS     | First MLAG pair, external handoffs via 802.1Q trunks to OPNsense |
| Firewall-1         | OPNsense        | DMZ firewall, PF NAT/rules, FRR BGP peering to border leaves     |
| Server-1           | Debian Server   | FRR internet simulator, eBGP upstream toward OPNsense WAN        |
| Server-N           | Proxmox VE      | Compute nodes, dual-homed to MLAG leaf pairs                     |
| MGMT-Switch        | ethernet_switch | Management network (bridged to host `br0`)                       |
| Cloud              | cloud           | Host bridge (`br0`) for management access                        |

### VRFs, VLANs, and IP Addressing

The fabric supports multiple tenant **VRFs** (virtual routing and forwarding) with **VXLAN** encapsulation, and **VLANs** mapped into those VRFs. Border leaves can optionally peer with the firewall via BGP for external connectivity. All addressing — management, fabric P2P, loopbacks, VTEPs, MLAG peer links, and external handoffs — is fully configurable. See [Configuration (SSOT)](#configuration-ssot) for how to define tenants, VLANs, and IP schemes.

---

## Prerequisites

### System Requirements

- Linux host with **GNS3 2.x+** (server + QEMU support)
- **Python 3.10+**
- **Ansible 13+** (with the Arista EOS collection)
- 16 GB+ RAM recommended (each vEOS ≈ 512 MB, Proxmox ≈ 2 GB, OPNsense ≈ 1 GB)
- Root/sudo access (bridge creation, `ubridge` capabilities)
- (Optional) **Terraform 1.x** if you plan to provision extra compute VMs on Proxmox
- (Optional) **Docker + Docker Compose** if you plan to run the monitoring stack

### Required GNS3 Templates / Images

Import these QEMU appliances into GNS3 **before** deploying anything. Names must match exactly (or be updated in `sots/config.py`):

| Template Name   | Suggested Image      | Purpose                           |
| --------------- | -------------------- | --------------------------------- |
| `Arista-vEOS`   | vEOS-lab-4.x.qcow2   | Spine/Leaf/Border switches        |
| `Debian Server` | debian-12.qcow2      | FRR internet simulator (Server-1) |
| `Proxmox VE`    | proxmox-ve-8.x.qcow2 | Compute nodes (Server-2+)         |
| `OPNsense`      | opnsense-24.x.qcow2  | Firewall                          |

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/0youcef/DataCenterEmulator.git
cd DataCenterEmulator

# 2. (Recommended) create a virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Install the Arista Ansible collection
ansible-galaxy collection install arista.eos

# 5. Install the GNS3 server (example: Arch Linux)
sudo pacman -S gns3-server mtools
# Debian/Ubuntu: follow https://docs.gns3.com/docs/getting-started/installation/linux

# 6. Create a persistent management bridge on the GNS3 host
sudo ip link add br0 type bridge
sudo ip link set br0 up
sudo ip addr add 172.20.20.1/24 dev br0

# 7. Grant ubridge the permissions GNS3 needs for TAP devices
sudo setcap cap_net_admin,cap_net_raw=eip /usr/bin/ubridge
```

Start (or confirm) the GNS3 server is running and reachable at the address you'll configure in the next step (default `http://127.0.0.1:3080`).

---

## Configuration (SSOT)

All topology and network parameters live in two files. **Edit these before deploying** — everything else reads from them.

### `sots/config.py` — Topology, IPs, Credentials

```python
GNS3_SERVER = "http://127.0.0.1:3080/v3"   # your GNS3 server URL
GNS3_USER   = "admin"                       # GNS3 controller username
GNS3_PASSWORD = "admin"                     # GNS3 controller password

PROJECT_NAME = "DataCenter"

# Topology size
NUM_SPINES  = 2
NUM_LEAVES  = 8
NUM_SERVERS = 2

# Management network
MGMT_BASE_IP = "172.20.20"
MGMT_START   = 10
MGMT_BRIDGE  = "br0"

# Device credentials pushed to switches/firewall/servers
SSH_USER = "admin"
SSH_PASS = "admin"

# Compute assignment (round-robin across GNS3 compute machines)
COMPUTE_SPINES = ["local"]
COMPUTE_LEAVES = ["local", "remotePC1"]
COMPUTE_SERVERS = ["local", "remotePC2", "remotePC3"]
COMPUTE_FIREWALLS = ["local"]

# Firewall / DMZ
ENABLE_DMZ_FIREWALL = True
FIREWALL_BGP_ASN = 65050

# Underlay
FABRIC_SUBNET = "10.0.0"
SPINE_AS_BASE = 65000
LEAF_AS_BASE  = 65100

# Overlay
VTEP_SUBNET = "10.254.0"

# MLAG
MLAG_PAIRS = [[1, 2]]
```

### `sots/vlans.py` — Tenants, VLANs, External Handoffs

```python
TENANTS = [
    {
        "name": "VRF_PEDAGOGY",
        "l3_vni": 50010,
        "external_handoff": True,
        "handoff_vlan": 110,
        "handoff_local_ip": "10.31.0.1/30",   # Border-1
        "handoff_peer_ip": "10.31.0.2",        # OPNsense
        "handoff_local_ip_2": "10.31.0.5/30",  # Border-2
        "handoff_peer_ip_2": "10.31.0.6",      # OPNsense
    },
    # ...
]

VLANS = [
    {"vlan_id": 10, "name": "WEB_SERVERS", "vni": 10010, "vrf": "VRF_PEDAGOGY", "anycast_ip": "192.168.10.1/24"},
    # ...
]
```

### Distributing Nodes Across Multiple GNS3 Computes

```python
COMPUTE_SPINES  = ["local"]
COMPUTE_LEAVES  = ["local", "remote-gpu-server"]
COMPUTE_SERVERS = ["remote-gpu-server", "remote-storage-server"]
```

Every compute machine listed must have identical QEMU images available under `~/GNS3/images/QEMU/`, and must already be registered as a compute in your GNS3 controller.

---

## Step-by-Step Usage Guide

Run each step from the project root, in order.

### 1. Deploy the Topology

```bash
python topology/deploy_fabric.py
```

Creates the GNS3 project, spawns all nodes from their templates, wires the spine-leaf full mesh, deploys the OPNsense firewall with WAN/LAN/DMZ links, connects everything to a management switch bridged to `br0`, and starts all nodes.

**What gets wired:**

- Every spine ↔ every leaf (full mesh)
- Server-1 ↔ both border leaves (when the DMZ firewall is disabled)
- Server-2+ ↔ both leaves in their MLAG pair (dual-homed)
- OPNsense `vtnet1` ↔ Server-1 WAN (FRR upstream)
- OPNsense `vtnet2` ↔ Border-2 (802.1Q trunk)
- OPNsense `vtnet3` ↔ Border-1 (802.1Q trunk)
- Every node's adapter 0 ↔ MGMT-Switch ↔ Cloud (`br0`)

### 2. Wait for Boot

vEOS switches take 1–3 minutes to boot, Proxmox and OPNsense take 2–5 minutes.

```bash
ping 172.20.20.10  # Spine-1
ping 172.20.20.12  # Leaf-1
```

### 3. Configure Switches via Telnet

```bash
python configs/config_switches.py
```

Connects to each switch console over Telnet, sets hostname, configures `Management1` with an IP, enables IP routing, eAPI (HTTPS), and SSH. The script polls console ports automatically and retries while switches finish booting.

### 4. Configure the OPNsense Firewall

```bash
python configs/config_firewalls.py
```

Configures firewall interfaces (WAN/LAN/DMZ), creates VLAN sub-interfaces on the trunk parents, applies PF NAT/rule policy, configures FRR BGP peering to the border leaves, and enables SSH.

### 5. Configure the FRR Internet Simulator

```bash
python configs/config_frr.py
```

Configures Server-1 (Debian + FRR) with a management IP, a WAN interface toward OPNsense, BGP peering, and a default-route advertisement (`8.8.8.8/32`) simulating an upstream ISP.

### 6. Configure Proxmox Servers

```bash
python configs/config_proxmox.py
```

Assigns management IPs to Server-2+ (Proxmox VE), creates the `vmbr0` bridge, and writes persistent network config to `/etc/network/interfaces`.

### 7. Apply Ansible Configuration

```bash
cd configs/ansible/
chmod +x inventory.py

# Underlay: BFD, BGP peer groups, route filtering
ansible-playbook -i inventory.py underlay.yml

# Overlay: EVPN/VXLAN, VRFs, anycast gateways, inter-VRF leaking
ansible-playbook -i inventory.py overlay.yml

# MLAG: peer-link, keepalive, domain
ansible-playbook -i inventory.py mlag.yml

# Border leaf: external handoffs, eBGP to firewall
ansible-playbook -i inventory.py border_leaf.yml
```

`inventory.py` is a dynamic inventory script that builds its host list directly from the SSOT (`sots/config.py` / `sots/vlans.py`), so it always matches the deployed topology.

---

## One-Shot Launch

Once the topology has been deployed once (step 1) and node templates/images are confirmed working, you can re-run the full configuration pipeline in one command:

```bash
bash launch.sh
```

---

## Adding Nodes to a Running Topology

```bash
python topology/add_node.py spine   # Adds Spine-N+1, wires to all leaves
python topology/add_node.py leaf    # Adds Leaf-N+1, wires to all spines
python topology/add_node.py server  # Adds Server-N+1, dual-homed to next MLAG pair
```

Each command:

1. Discovers the live topology from GNS3
2. Creates the node from the appropriate template
3. Wires it correctly (full mesh for spines/leaves, round-robin for servers)
4. Connects it to MGMT-Switch
5. Increments the relevant `NUM_*` counter in `sots/config.py`
6. Starts the node (the project stays open)

After adding a node, re-run the relevant config script(s) and Ansible playbooks so it gets configured.

---

## Optional: Provisioning VPS Compute with Terraform

`configs/terraform/` contains a Terraform configuration that clones an Ubuntu template on **Proxmox** and provisions a variable number of VPS instances, with the VM count read directly from the SSOT (`NUM_SERVERS` in `sots/config.py`). Server-1 is always the FRR internet simulator (not a Proxmox VM), so effective VPS count is `max(NUM_SERVERS - 1, 0)`.

```bash
cd configs/terraform
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` with your Proxmox values (`proxmox_endpoint`, an API token **or** username/password, `proxmox_node_name`, `proxmox_template_vm_id`, `proxmox_datastore_id`, `proxmox_bridge`), then:

```bash
terraform init
terraform plan
terraform apply
```

To scale VM count, edit `NUM_SERVERS` in `sots/config.py` and re-run `terraform plan && terraform apply`. To tear everything down:

```bash
terraform destroy
```

Full details, including how to change VM profiles (CPU/memory/disk/VLAN/cloud-init) without touching the SSOT, are in [`configs/terraform/README.md`](configs/terraform/README.md).

**Never commit `terraform.tfvars` or `*.tfstate` files** — they may contain credentials and are already covered by `configs/terraform/.gitignore`.

---

## Optional: Monitoring Stack (Prometheus + Grafana)

`configs/monitoring/` provides a Docker Compose stack (Prometheus, Alertmanager, Grafana) plus a custom Prometheus exporter for Arista EOS switches.

```bash
# Start the custom EOS exporter (polls each switch's eAPI)
python3 configs/monitoring/eos_exporter.py &

# Start Prometheus / Alertmanager / Grafana
cd configs/monitoring
docker compose up -d
```

- Prometheus: `http://localhost:9090`
- Alertmanager: `http://localhost:9093`
- Grafana: `http://localhost:3000` (default login `admin` / `admin` — **change this immediately**)
- EOS exporter metrics: `http://localhost:9101/metrics?target=<switch-mgmt-ip>`

Metrics exposed include BGP session state, EVPN session state/route counts, MLAG status, and per-interface counters/errors — see the docstring in `configs/monitoring/eos_exporter.py` for the full metric list. Point `configs/monitoring/prometheus/prometheus.yml` at your switches' management IPs before starting the stack.

---

## GNS3 API Reference

`topology/gns3.py` is a thin wrapper around the GNS3 v3 API:

```python
from topology.gns3 import GNS3Client
gns3 = GNS3Client(server, user, password)
```

| Method                                                                          | Description                               |
| ------------------------------------------------------------------------------- | ----------------------------------------- |
| `get_projects()`                                                                | List all projects                         |
| `create_project(name)`                                                          | Create project (returns `None` on 409)    |
| `open_project(id)`                                                              | Open a project                            |
| `close_project(id)`                                                             | Close a project (stops all nodes)         |
| `delete_project(id)`                                                            | Delete a project                          |
| `get_computes()`                                                                | List registered computes                  |
| `get_templates()`                                                               | List all templates                        |
| `get_nodes(project_id)`                                                         | List nodes in a project                   |
| `get_links(project_id)`                                                         | List links in a project                   |
| `create_node_from_template(project_id, template_id, compute_id, x, y)`          | Spawn a node from template                |
| `create_node(project_id, name, node_type, compute_id, x, y, properties)`        | Create a built-in node (switch, cloud)    |
| `rename_node(project_id, node_id, name)`                                        | Rename a node                             |
| `set_switch_ports(project_id, node_id, num_ports)`                              | Set number of ports on an ethernet switch |
| `start_nodes(project_id)`                                                       | Start all nodes in a project              |
| `start_node(project_id, node_id)`                                               | Start a single node                       |
| `create_link(project_id, node_a, adapter_a, node_b, adapter_b, port_a, port_b)` | Wire two nodes                            |

**Note on ethernet switches:** use `adapter_number=0, port_number=N` — pass `port_a`/`port_b` when one end is a switch.

---

## File Structure

```
DataCenterEmulator/
├── sots/                          # Single Source of Truth
│   ├── config.py                  # Topology params, IPs, ASNs, credentials, MLAG
│   └── vlans.py                   # Tenant VRFs, VLANs, external handoffs
├── topology/                      # GNS3 topology management
│   ├── gns3.py                    # GNS3 v3 API wrapper
│   ├── deploy_fabric.py           # Create project, spawn nodes, wire fabric
│   └── add_node.py                # Add spine/leaf/server to a running topology
├── configs/                       # Device configuration
│   ├── config_switches.py         # Initial switch config (Telnet → eAPI/SSH)
│   ├── config_firewalls.py        # OPNsense config (interfaces, PF, FRR BGP)
│   ├── config_frr.py              # FRR internet simulator config
│   ├── config_proxmox.py          # Proxmox server mgmt network config
│   ├── generate_config.py         # Generate prometheus.yml from SSOT
│   ├── ansible/                   # Ansible playbooks & dynamic inventory
│   │   ├── inventory.py           # Dynamic inventory built from the SSOT
│   │   ├── underlay.yml           # BGP underlay, BFD, ECMP, route filtering
│   │   ├── overlay.yml            # EVPN/VXLAN, VRFs, anycast GW, inter-VRF
│   │   ├── border_leaf.yml        # External handoffs, eBGP to firewall
│   │   └── mlag.yml               # MLAG peer-link, keepalive, domain
│   ├── terraform/                 # Optional: Proxmox VPS provisioning
│   │   ├── main.tf                # VM creation from Proxmox template
│   │   ├── variables.tf           # Proxmox/VM input variables
│   │   ├── scripts/read_ssot.py   # Reads NUM_SERVERS from SSOT
│   │   └── README.md              # Terraform usage guide
│   └── monitoring/                # Prometheus/Grafana stack
│       ├── docker-compose.yml
│       ├── prometheus/
│       │   ├── prometheus.yml     # Scrape configs (edit before use)
│       │   └── alerts.yml         # BGP, EVPN, MLAG, interface alerts
│       ├── alertmanager/
│       │   └── alertmanager.yml
│       └── eos_exporter.py        # Custom Arista EOS Prometheus exporter
├── launch.sh                      # One-shot: wait for CPU idle, run all configs
├── requirements.txt               # Python dependencies
└── README.md
```

---

## Troubleshooting

### Switch console not reachable

vEOS may take 1–3 minutes to boot. The config scripts poll with retries (10–12 attempts, 10–15s delay). If it still fails:

```bash
# Check if the node is running in the GNS3 GUI
# Verify the console port in the GNS3 node properties
# Try a manual telnet:
telnet 127.0.0.1 <console_port>
```

### BGP neighbors not establishing

```bash
# On the switch:
show bgp summary
show ip bgp

# Verify interface IPs:
show ip interface brief

# Check BFD:
show bfd neighbors
```

### Firewall not reachable via SSH

```bash
# Verify mgmt IP (after step 3):
ping 172.20.20.15  # OPNsense mgmt (10 + NUM_SPINES + NUM_LEAVES + NUM_SERVERS)

# Check the OPNsense console in GNS3 for boot errors
```

### VTEP tunnels not forming

```bash
# Verify Loopback1 IPs:
show ip interface brief | grep Loopback1

# Verify the VXLAN interface:
show vxlan summary

# Check EVPN neighbors:
show bgp evpn summary
```

### `ansible-playbook` can't reach devices

Confirm `configs/ansible/inventory.py` is executable (`chmod +x inventory.py`) and that `SSH_USER`/`SSH_PASS` in `sots/config.py` match what was pushed to devices in steps 3–4.

---

## Security Notes

This project is built for an isolated lab/study environment, not production:

- Default credentials (`GNS3_USER`/`GNS3_PASSWORD`, `SSH_USER`/`SSH_PASS`, Grafana `admin`/`admin`) are placeholders — **change them** before exposing anything beyond `localhost`.
- Never commit real secrets: `.env`, `password.txt`, `.keys.*`, `terraform.tfvars`, and `*.tfstate*` are already excluded via `.gitignore` — double-check before your first push that no such files are staged (`git status`).
- The management bridge (`br0`) and GNS3 controller are assumed to be reachable only from a trusted local network.

---
