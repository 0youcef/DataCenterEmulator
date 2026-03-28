"""
Usage:
    python add_node.py spine
    python add_node.py leaf
    python add_node.py server
"""
import sys
import re
import os
from gns3 import GNS3Client
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from sots.config import (
    GNS3_SERVER, GNS3_USER, GNS3_PASSWORD,
    PROJECT_NAME, TEMPLATE_NAME_ARISTA, TEMPLATE_NAME_SERVER,
    COMPUTE_SPINE, COMPUTE_LEAF, COMPUTE_SERVER,
    MGMT_BASE_IP, MGMT_START,
)

CONFIG_FILE = "../sots/config.py"


def get_project_id(gns3):
    projects = gns3.get_projects()
    project = next((p for p in projects if p['name'] == PROJECT_NAME), None)
    if not project:
        raise RuntimeError(f"Project '{PROJECT_NAME}' not found. Deploy it first.")
    return project['project_id']


def resolve_compute(gns3, name):
    computes = gns3.get_computes()
    compute_map = {c['name']: c['compute_id'] for c in computes}
    compute_map['local'] = 'local'
    if name not in compute_map:
        raise RuntimeError(f"Compute '{name}' not found. Available: {list(compute_map.keys())}")
    return compute_map[name]


def get_existing_nodes(gns3, project_id):
    """Return categorized nodes and next available adapters per node."""
    nodes = gns3.get_nodes(project_id)
    links = gns3.get_links(project_id)

    spines  = sorted([n for n in nodes if n['name'].startswith('Spine-')],  key=lambda n: n['name'])
    leaves  = sorted([n for n in nodes if n['name'].startswith('Leaf-')],   key=lambda n: n['name'])
    servers = sorted([n for n in nodes if n['name'].startswith('Server-')], key=lambda n: n['name'])
    mgmt_switch = next((n for n in nodes if n['name'] == 'MGMT-Switch'), None)

    if not mgmt_switch:
        raise RuntimeError("MGMT-Switch not found in project. Has the topology been deployed?")

    used_adapters = {}
    for link in links:
        for endpoint in link['nodes']:
            nid = endpoint['node_id']
            adapter = endpoint['adapter_number']
            if nid not in used_adapters or adapter > used_adapters[nid]:
                used_adapters[nid] = adapter

    def next_adapter(node_id):
        current = used_adapters.get(node_id, 0) + 1
        used_adapters[node_id] = current
        return current

    mgmt_ports_used = set()
    for link in links:
        for endpoint in link['nodes']:
            if endpoint['node_id'] == mgmt_switch['node_id']:
                mgmt_ports_used.add(endpoint['port_number'])
    next_mgmt_port = max(mgmt_ports_used) + 1 if mgmt_ports_used else 0

    return spines, leaves, servers, mgmt_switch, next_adapter, next_mgmt_port


def next_ip(spines, leaves, servers):
    """Calculate the next free management IP based on existing node count."""
    total = len(spines) + len(leaves) + len(servers)
    return f"{MGMT_BASE_IP}.{MGMT_START + total}"


def update_config(key, new_value):
    """Increment a NUM_ variable in config.py."""
    with open(CONFIG_FILE, 'r') as f:
        content = f.read()
    updated = re.sub(
        rf'^({key}\s*=\s*)(\d+)',
        lambda m: f"{m.group(1)}{new_value}",
        content,
        flags=re.MULTILINE
    )
    with open(CONFIG_FILE, 'w') as f:
        f.write(updated)
    print(f" -> Updated {key} = {new_value} in {CONFIG_FILE}")



def add_spine(gns3, project_id):
    spines, leaves, servers, mgmt_switch, next_adapter, next_mgmt_port = get_existing_nodes(gns3, project_id)
    templates = gns3.get_templates()
    template_id = next((t['template_id'] for t in templates if t['name'] == TEMPLATE_NAME_ARISTA), None)
    if not template_id:
        raise RuntimeError(f"Template '{TEMPLATE_NAME_ARISTA}' not found")

    name = f"Spine-{len(spines) + 1}"
    compute = resolve_compute(gns3, COMPUTE_SPINE)
    x = len(spines) * 200
    print(f"Adding {name}...")

    node = gns3.create_node_from_template(project_id, template_id, compute, x, -100)
    node = gns3.rename_node(project_id, node['node_id'], name)

    # Wire to every existing leaf
    for leaf in leaves:
        la = next_adapter(leaf['node_id'])
        sa = next_adapter(node['node_id'])
        gns3.create_link(project_id, leaf['node_id'], la, node['node_id'], sa)
        print(f" -> {leaf['name']} (Eth{la}) <---> {name} (Eth{sa})")

    gns3.create_link(project_id, node['node_id'], 0, mgmt_switch['node_id'], 0,
                     port_b=next_mgmt_port)
    print(f" -> {name} (Mgmt0) <---> MGMT-Switch (port {next_mgmt_port})")

    gns3.start_node(project_id,node['node_id'])
    print(f" -> Started {name}")
    update_config("NUM_SPINES", len(spines) + 1)
    print(f"Done. {name} added, wired and booted.")


def add_leaf(gns3, project_id):
    spines, leaves, servers, mgmt_switch, next_adapter, next_mgmt_port = get_existing_nodes(gns3, project_id)
    templates = gns3.get_templates()
    template_id = next((t['template_id'] for t in templates if t['name'] == TEMPLATE_NAME_ARISTA), None)
    if not template_id:
        raise RuntimeError(f"Template '{TEMPLATE_NAME_ARISTA}' not found")

    name = f"Leaf-{len(leaves) + 1}"
    compute = resolve_compute(gns3, COMPUTE_LEAF)
    x = len(leaves) * 200
    print(f"Adding {name}...")

    node = gns3.create_node_from_template(project_id, template_id, compute, x, 100)
    node = gns3.rename_node(project_id, node['node_id'], name)

    for spine in spines:
        la = next_adapter(node['node_id'])
        sa = next_adapter(spine['node_id'])
        gns3.create_link(project_id, node['node_id'], la, spine['node_id'], sa)
        print(f" -> {name} (Eth{la}) <---> {spine['name']} (Eth{sa})")

    gns3.create_link(project_id, node['node_id'], 0, mgmt_switch['node_id'], 0,
                     port_b=next_mgmt_port)
    print(f" -> {name} (Mgmt0) <---> MGMT-Switch (port {next_mgmt_port})")
    gns3.start_node(project_id, node['node_id'])
    print(f" -> Started {name}")
    update_config("NUM_LEAVES", len(leaves) + 1)
    print(f"Done. {name} added, wired and booted.")


def add_server(gns3, project_id):
    spines, leaves, servers, mgmt_switch, next_adapter, next_mgmt_port = get_existing_nodes(gns3, project_id)
    templates = gns3.get_templates()
    template_id = next((t['template_id'] for t in templates if t['name'] == TEMPLATE_NAME_SERVER), None)
    if not template_id:
        raise RuntimeError(f"Template '{TEMPLATE_NAME_SERVER}' not found")

    name = f"Server-{len(servers) + 1}"
    compute = resolve_compute(gns3, COMPUTE_SERVER)
    print(f"Adding {name}...")

    leaf = leaves[len(servers) % len(leaves)]
    leaf_x = leaf['x']

    servers_on_leaf = sum(
        1 for link in gns3.get_links(project_id)
        for ep in link['nodes']
        if ep['node_id'] == leaf['node_id']
        and any(s['node_id'] == next(
            (e['node_id'] for e in link['nodes'] if e['node_id'] != leaf['node_id']), None
        ) for s in servers)
    )

    node = gns3.create_node_from_template(project_id, template_id, compute, leaf_x, 300 + servers_on_leaf * 150)
    node = gns3.rename_node(project_id, node['node_id'], name)

    leaf = leaves[len(servers) % len(leaves)]
    sa = next_adapter(node['node_id'])
    la = next_adapter(leaf['node_id'])
    gns3.create_link(project_id, node['node_id'], sa, leaf['node_id'], la)
    print(f" -> {name} (Eth{sa}) <---> {leaf['name']} (Eth{la})")

    gns3.create_link(project_id, node['node_id'], 0, mgmt_switch['node_id'], 0,
                     port_b=next_mgmt_port)
    print(f" -> {name} (Mgmt0) <---> MGMT-Switch (port {next_mgmt_port})")

    gns3.start_node(project_id,node['node_id'])
    print(f" -> Started {name}")
    update_config("NUM_SERVERS", len(servers) + 1)
    print(f"Done. {name} added and wired.")



COMMANDS = {
    "spine":  add_spine,
    "leaf":   add_leaf,
    "server": add_server,
}

