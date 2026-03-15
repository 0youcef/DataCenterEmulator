# GNS3 Spine-Leaf Automation

Automated deployment and configuration of a spine-leaf data center topology in GNS3, using Arista vEOS switches and ESXi servers. Includes topology deployment, switch configuration via Netmiko, and ongoing management via Ansible.

---

## Project Structure

```
.
├── config.py               # Single source of truth — edit this file only
├── gns3.py                 # GNS3 API client library
├── deploy.py               # Deploy the initial topology
├── add_node.py             # Add nodes to a running topology
├── config_switches.py   # Configure switches via Telnet (Netmiko)
└── ansible/
    ├── inventory.py        # Dynamic Ansible inventory (reads config.py)
    └── playbook.yml        # Ansible playbook for switch configuration
```

---

## Prerequisites

### System

```bash
# GNS3 server
sudo pacman -S gns3-server mtools

# Python dependencies
pip install requests netmiko ansible

# Arista Ansible collection
ansible-galaxy collection install arista.eos

# Management bridge (persistent)
sudo ip link add br0 type bridge
sudo ip link set br0 up
sudo ip addr add 192.168.100.1/24 dev br0

# ubridge permissions (required for GNS3 TAP devices)
sudo setcap cap_net_admin,cap_net_raw=eip /usr/bin/ubridge
```

### GNS3 Templates

Two templates must exist in GNS3 before deploying:

**Arista vEOS:**
- Image: `vEOS-lab-x.x.x.qcow2` + `Aboot-veos-x.x.x.iso`
- RAM: 2048MB
- NICs: `e1000`, 12 adapters
- QEMU options: `-nographic`
- Place images in `~/GNS3/images/QEMU/`

**ESXi:**
- Image: pre-installed `ESXi.qcow2` (see below)
- RAM: 4096MB
- NICs: `e1000e`, 4 adapters
- QEMU options: `-machine q35 -enable-kvm -cpu host -smp 4 -usb -device usb-tablet`
- Enable "Use as a linked base VM"

### Preparing the ESXi image

Install ESXi outside GNS3 first so all nodes share one golden image:

```bash
# Create blank disk
qemu-img create -f qcow2 ~/GNS3/images/QEMU/ESXi.qcow2 16G

# Install ESXi (connect via VNC on localhost:5900)
qemu-system-x86_64 \
  -machine q35 -enable-kvm -cpu host -smp 4 -m 8192 \
  -hda ~/GNS3/images/QEMU/ESXi.qcow2 \
  -cdrom VMware-VMvisor-*.iso \
  -boot order=dc \
  -device e1000e,netdev=net0 -netdev user,id=net0 \
  -usb -device usb-tablet -vnc :0

vncviewer localhost:5900

# Keep a backup of the clean install
cp ~/GNS3/images/QEMU/ESXi.qcow2 ~/GNS3/images/QEMU/ESXi-bare.qcow2
```

Each GNS3 node gets a linked clone (delta) backed by this image — changes in one VM never affect others.

---

## Configuration

All settings live in `config.py`. Edit this file only — all scripts read from it automatically.

```python
GNS3_SERVER   = "http://127.0.0.1:3080/v3"  # GNS3 server address
GNS3_USER     = "admin"
GNS3_PASSWORD = "admin"

PROJECT_NAME  = "DataCenter"

TEMPLATE_NAME_ARISTA = "Arista-vEOS"  # Must match GNS3 template name exactly
TEMPLATE_NAME_SERVER = "ESXi"

NUM_SPINES  = 2   # Updated automatically by add_node.py
NUM_LEAVES  = 3
NUM_SERVERS = 2

COMPUTE_SPINE  = "local"   # Use the name shown in GNS3 GUI, not the ID
COMPUTE_LEAF   = "local"
COMPUTE_SERVER = "local"

MGMT_BASE_IP = "192.168.100"  # Management subnet
MGMT_START   = 10             # First host — IPs start at 192.168.100.10
MGMT_BRIDGE  = "br0"          # Host bridge interface for management access

SSH_USER = "admin"
SSH_PASS = ""
```

### Management IP assignment

IPs are assigned sequentially starting from `MGMT_BASE_IP.MGMT_START`:
- Spine-1 → `.10`, Spine-2 → `.11`
- Leaf-1 → `.12`, Leaf-2 → `.13`, Leaf-3 → `.14`
- Servers are not assigned IPs by the scripts (ESXi manages its own via DCUI)

### Remote computes

To distribute nodes across multiple machines, register them in GNS3 and set the compute names in `config.py`:

```python
COMPUTE_SPINE  = "local"
COMPUTE_LEAF   = "local"
COMPUTE_SERVER = "PC"       # name of a registered remote compute
```

All compute machines need the same images in `~/GNS3/images/QEMU/`. Using a shared NFS mount avoids duplication:

```bash
mount <controller-ip>:/home/user/GNS3/images /home/pc/GNS3/images
```

---

## Workflow

### 1. Deploy the topology

```bash
python deploy.py
```

This creates the GNS3 project, spawns all nodes, wires the spine-leaf fabric, connects everything to a management switch bridged to `br0`, and starts all nodes. The script waits for a keypress before closing the project.

**Topology layout:**
```
         Cloud (br0)
             |
        MGMT-Switch
      /    |    |    \
  Spine-1 ... Spine-N   (fabric adapters 1+, mgmt on adapter 0)
      \    |    |    /
   Leaf-1  ...  Leaf-N
      |              |
  Server-1       Server-2   (stacked vertically under their leaf)
```

Each spine is wired to every leaf (full mesh). Servers connect to leaves in round-robin. Adapter 0 on every node is reserved for management.

### 2. Configure switches via Telnet

Run after nodes have booted (vEOS takes 3-5 minutes):

```bash
python config_switches.py
```

Connects to each switch via its GNS3 console port over Telnet, sets the hostname, configures Management1 IP, enables IP routing, eAPI (HTTPS), and SSH. The script polls the console port automatically and waits for the switch to be ready before connecting.

### 3. Configure switches via Ansible (eAPI)

After step 2, switches are reachable via eAPI. Run the playbook:

```bash
cd ansible/
chmod +x inventory.py
ansible-playbook -i inventory.py playbook.yml
```

The dynamic inventory (`inventory.py`) reads `config.py` and builds the host list automatically — no hardcoded IPs or hostnames. Uses `httpapi` connection over HTTPS port 443.

To push additional config, add tasks to `playbook.yml` using `eos_config`:

```yaml
- name: Configure an interface
  arista.eos.eos_config:
    lines:
      - description Link to Spine-1
      - no switchport
      - ip address 10.0.0.1/31
      - no shutdown
    parents: interface Ethernet1
```

`parents` sets the config context — the list represents the path from global config down to where the lines should be applied. Config is idempotent: running the playbook multiple times won't duplicate lines.

### 4. Add nodes to a running topology

```bash
python add_node.py spine
python add_node.py leaf
python add_node.py server
```

Discovers the live topology from GNS3, adds the new node, wires it correctly (new spine → all leaves, new leaf → all spines, new server → next leaf round-robin), connects it to the management switch, and increments the relevant `NUM_*` counter in `config.py` automatically. Does **not** close the project so running nodes stay up.

---

## gns3.py API Reference

`gns3.py` is a thin wrapper around the GNS3 v3 API. Import it in any script:

```python
from gns3 import GNS3Client
gns3 = GNS3Client(server, user, password)
```

| Method | Description |
|---|---|
| `get_projects()` | List all projects |
| `create_project(name)` | Create project, returns `None` on 409 |
| `open_project(id)` | Open a project |
| `close_project(id)` | Close a project (stops all nodes) |
| `delete_project(id)` | Delete a project |
| `get_computes()` | List registered computes |
| `get_templates()` | List all templates |
| `get_nodes(project_id)` | List nodes in a project |
| `get_links(project_id)` | List links in a project |
| `create_node_from_template(project_id, template_id, compute_id, x, y)` | Spawn a node from template |
| `create_node(project_id, name, node_type, compute_id, x, y, properties)` | Create a built-in node (switch, cloud) |
| `rename_node(project_id, node_id, name)` | Rename a node |
| `set_switch_ports(project_id, node_id, num_ports)` | Set number of ports on ethernet switch |
| `start_nodes(project_id)` | Start all nodes in project |
| `start_node(project_id, node_id)` | Start a single node |
| `create_link(project_id, node_a, adapter_a, node_b, adapter_b, port_a, port_b)` | Wire two nodes |

**Note on ethernet switches:** switches use `adapter_number=0, port_number=N` — pass `port_a` or `port_b` when one end is a switch.

---

## Troubleshooting

**Port 53 already in use (dnsmasq)**
```bash
# disable systemd-resolved stub listener
echo "DNSStubListener=no" | sudo tee -a /etc/systemd/resolved.conf
sudo systemctl restart systemd-resolved
```

**GNS3 500 on project load (`TypeError: NoneType not iterable`)**
A port in the project file has a null name. Patch `port.py`:
```python
# /usr/lib/python3.14/site-packages/gns3server/controller/ports/port.py
@property
def short_name(self):
    if self._name is None:
        return ""
    elif "/" in self._name:
    ...
```
Or fix the project file: `grep -n '"name": null' ~/GNS3/projects/*/*.gns3`

**TAP device error on start**
`br0` is down. Bring it up and ensure ubridge has capabilities:
```bash
sudo ip link set br0 up
sudo setcap cap_net_admin,cap_net_raw=eip /usr/bin/ubridge
sudo systemctl restart gns3server
```

**SSH host key changed after redeployment**
```bash
ssh-keygen -R <switch-ip>
# or disable checking for the lab subnet in ~/.ssh/config:
# Host 192.168.100.*
#     StrictHostKeyChecking no
#     UserKnownHostsFile /dev/null
```

**vEOS console: `[Agent ar.Aaa not responding]`**
Not an error — AAA service is still initializing. Wait for the prompt to return cleanly (no warnings) then log in with `admin` and no password.
