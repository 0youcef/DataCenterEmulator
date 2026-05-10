# GNS3 Spine-Leaf Automation

Automated deployment and configuration of a spine-leaf data center topology in GNS3, using Arista vEOS switches and ESXi servers. Includes topology deployment, switch configuration via Netmiko, and ongoing management via Ansible.

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
sudo ip addr add 172.20.20.1/24 dev br0

# ubridge permissions (required for GNS3 TAP devices)
sudo setcap cap_net_admin,cap_net_raw=eip /usr/bin/ubridge
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

All compute machines need the same images in `~/GNS3/images/QEMU/`.

---

## Workflow

### 1. Deploy the topology

```bash
python topology/deploy_fabric.py
```

This creates the GNS3 project, spawns all nodes, wires the spine-leaf fabric, deploys an OPNsense firewall with WAN/LAN/DMZ links, connects everything to a management switch bridged to `br0`, and starts all nodes. The script waits for a keypress before closing the project.

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

Run after nodes have booted (vEOS takes 1-3 minutes):

```bash
python configs/config_switches.py
```

Connects to each switch via its GNS3 console port over Telnet, sets the hostname, configures Management1 IP, enables IP routing, eAPI (HTTPS), and SSH. The script polls the console port automatically and waits for the switch to be ready before connecting.

### 3. Configure OPNsense firewall (Telnet)

After switches are reachable, configure firewall interfaces (WAN/LAN/DMZ) and base DMZ policy:

```bash
python configs/config_firewalls.py
```

All firewall parameters are in `sots/config.py` (template name, interface names, credentials, subnets, exposed DMZ host/ports).

### 4. Configure switches via Ansible (eAPI)

After step 2, switches are reachable via eAPI. Run a playbook:

```bash
cd configs/ansible/
chmod +x inventory.py
ansible-playbook -i inventory.py underlay.yml
ansible-playbook -i inventory.py overlay.yml
ansible-playbook -i inventory.py border_leaf.yml
```

The dynamic inventory (`inventory.py`) reads `config.py` and builds the host list automatically — no hardcoded IPs or hostnames. Uses `httpapi` connection over HTTPS port 443.

### 5. Add nodes to a running topology

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
