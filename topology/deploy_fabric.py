import sys
from gns3 import GNS3Client
from config import (
    GNS3_SERVER, GNS3_USER, GNS3_PASSWORD,
    PROJECT_NAME, TEMPLATE_NAME_ARISTA, TEMPLATE_NAME_SERVER,
    NUM_SPINES, NUM_LEAVES, NUM_SERVERS,
    COMPUTE_SPINE, COMPUTE_LEAF, COMPUTE_SERVER,
    MGMT_BRIDGE,
)

print("Authenticating with GNS3 API...")
gns3 = GNS3Client(GNS3_SERVER, GNS3_USER, GNS3_PASSWORD)
print("Authentication successful!\n")

project_id = None

try:
    # ==========================================
    # 1. Create or open the project
    # ==========================================
    print(f"Opening project: {PROJECT_NAME}...")
    project = gns3.create_project(PROJECT_NAME)
    if project is None:
        print(f"Project '{PROJECT_NAME}' already exists. Deleting and recreating...")
        projects = gns3.get_projects()
        old_id = next(p['project_id'] for p in projects if p['name'] == PROJECT_NAME)
        gns3.delete_project(old_id)
        project = gns3.create_project(PROJECT_NAME)
        project_id = project['project_id']
    else:
        project_id = project['project_id']
    print(f"Project ID: {project_id}\n")

    # ==========================================
    # Resolve compute names to IDs
    # ==========================================
    print("Loading computes...")
    computes = gns3.get_computes()
    for c in computes:
        print(c['compute_id'], c['name'])

    compute_map = {c['name']: c['compute_id'] for c in computes}
    compute_map['local'] = 'local'

    def resolve(name):
        if name in compute_map:
            return compute_map[name]
        raise RuntimeError(f"Compute '{name}' not found. Available: {list(compute_map.keys())}")

    compute_spine  = resolve(COMPUTE_SPINE)
    compute_leaf   = resolve(COMPUTE_LEAF)
    compute_server = resolve(COMPUTE_SERVER)

    # ==========================================
    # 2. Fetch templates
    # ==========================================
    templates = gns3.get_templates()
    template_id_arista = next((t['template_id'] for t in templates if t['name'] == TEMPLATE_NAME_ARISTA), None)
    template_id_server = next((t['template_id'] for t in templates if t['name'] == TEMPLATE_NAME_SERVER), None)

    if not template_id_arista:
        raise RuntimeError(f"Template '{TEMPLATE_NAME_ARISTA}' not found in GNS3!")
    if not template_id_server:
        raise RuntimeError(f"Template '{TEMPLATE_NAME_SERVER}' not found in GNS3!")

    print(f"Found template {TEMPLATE_NAME_ARISTA}: {template_id_arista}")
    print(f"Found template {TEMPLATE_NAME_SERVER}: {template_id_server}\n")

    # ==========================================
    # 3. Spawn nodes
    # ==========================================
    spines  = []
    leaves  = []
    servers = []
    next_adapter = {}

    def deploy(template_id, compute_id, name, x, y):
        node = gns3.create_node_from_template(project_id, template_id, compute_id, x, y)
        node = gns3.rename_node(project_id, node['node_id'], name)
        next_adapter[node['node_id']] = 1
        print(f" -> Created {name} (Node ID: {node['node_id']})")
        return node

    print("Spawning Spines...")
    for i in range(NUM_SPINES):
        spines.append(deploy(template_id_arista, compute_spine, f"Spine-{i+1}", i * 200, -100))

    print("Spawning Leaves...")
    for i in range(NUM_LEAVES):
        leaves.append(deploy(template_id_arista, compute_leaf, f"Leaf-{i+1}", i * 200, 100))

    print("Spawning Servers...")
    leaf_server_count = {}
    for i in range(NUM_SERVERS):
        leaf = leaves[i % NUM_LEAVES]
        leaf_x = leaf['x']
        count = leaf_server_count.get(leaf['node_id'], 0)
        leaf_server_count[leaf['node_id']] = count + 1
        servers.append(deploy(template_id_server, compute_server, f"Server-{i+1}", leaf_x, 300 + count * 150))

    # ==========================================
    # 4. Spawn management switch and cloud
    # ==========================================
    print("Spawning Management Switch...")
    mgmt_switch = gns3.create_node(project_id, "MGMT-Switch", "ethernet_switch", "local",
                                   (NUM_LEAVES * 200) // 2, -300)
    gns3.set_switch_ports(project_id, mgmt_switch['node_id'], 24)
    mgmt_port = [0]
    print(f" -> Created MGMT-Switch (Node ID: {mgmt_switch['node_id']})")

    print("Spawning Cloud node...")
    cloud = gns3.create_node(project_id, "Cloud", "cloud", "local",
                             (NUM_LEAVES * 200) // 2, -400,
                             properties={"interfaces": [{"name": MGMT_BRIDGE, "type": "ethernet", "special": False}]})
    print(f" -> Created Cloud node (Node ID: {cloud['node_id']})")

    # ==========================================
    # 5. Wire spine-leaf fabric
    # ==========================================
    print("\nLinking the fabric (Every Leaf to Every Spine)...")
    for leaf in leaves:
        for spine in spines:
            la, sa = next_adapter[leaf['node_id']], next_adapter[spine['node_id']]
            gns3.create_link(project_id, leaf['node_id'], la, spine['node_id'], sa)
            print(f" -> {leaf['name']} (Eth{la}) <---> {spine['name']} (Eth{sa})")
            next_adapter[leaf['node_id']] += 1
            next_adapter[spine['node_id']] += 1

    print("\nLinking Servers to Leaves...")
    for i, server in enumerate(servers):
        leaf = leaves[i % NUM_LEAVES]
        sa, la = next_adapter[server['node_id']], next_adapter[leaf['node_id']]
        gns3.create_link(project_id, server['node_id'], sa, leaf['node_id'], la)
        print(f" -> {server['name']} (Eth{sa}) <---> {leaf['name']} (Eth{la})")
        next_adapter[server['node_id']] += 1
        next_adapter[leaf['node_id']] += 1

    # ==========================================
    # 6. Wire management network
    # ==========================================
    print("\nWiring management network...")
    for node in spines + leaves + servers:
        gns3.create_link(project_id, node['node_id'], 0, mgmt_switch['node_id'], 0,
                         port_b=mgmt_port[0])
        print(f" -> {node['name']} (Mgmt0) <---> MGMT-Switch (port {mgmt_port[0]})")
        mgmt_port[0] += 1

    gns3.create_link(project_id, mgmt_switch['node_id'], 0, cloud['node_id'], 0,
                     port_a=mgmt_port[0])
    print(f" -> MGMT-Switch (port {mgmt_port[0]}) <---> Cloud ({MGMT_BRIDGE})")

    # ==========================================
    # 7. Start the lab
    # ==========================================
    print("\nStarting all nodes...")
    gns3.start_nodes(project_id)
    print("Success! The Spine-Leaf fabric is deployed, wired, and booting.")

except Exception as e:
    print(f"\nError: {e}")
    if project_id:
        print("Cleaning up: deleting project...")
        gns3.delete_project(project_id)
    sys.exit(1)

finally:
    input()
    if project_id:
        print(f"\nClosing project: {PROJECT_NAME}...")
        gns3.close_project(project_id)
