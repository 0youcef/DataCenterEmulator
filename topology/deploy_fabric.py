import sys
import os
from gns3 import GNS3Client

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sots.config import (
    GNS3_SERVER,
    GNS3_USER,
    GNS3_PASSWORD,
    PROJECT_NAME,
    TEMPLATE_NAME_ARISTA,
    TEMPLATE_NAME_FRR,
    TEMPLATE_NAME_PROXMOX,
    TEMPLATE_NAME_FIREWALL,
    NUM_SPINES,
    NUM_LEAVES,
    NUM_SERVERS,
    COMPUTE_SPINES,
    COMPUTE_LEAVES,
    COMPUTE_SERVERS,
    COMPUTE_FIREWALLS,
    MGMT_BRIDGE,
    MLAG_PAIRS,
    MLAG_PEER_LINK_MEMBER_COUNT,
    BORDER_FIREWALL_COUNT,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_mlag_pair(pair, pair_number):
    if isinstance(pair, str):
        parts = [p.strip() for p in pair.split(",") if p.strip()]
    elif isinstance(pair, (list, tuple)):
        parts = list(pair)
    else:
        raise RuntimeError(
            f"MLAG_PAIRS entry #{pair_number} must be list/tuple or "
            f"comma-separated string, got {type(pair).__name__}"
        )
    if len(parts) != 2:
        raise RuntimeError(
            f"MLAG_PAIRS entry #{pair_number} must contain exactly two leaf indexes"
        )
    try:
        return int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise RuntimeError(
            f"MLAG_PAIRS entry #{pair_number} must contain integer leaf indexes"
        ) from exc


def validate_compute_list(name, lst):
    if not isinstance(lst, (list, tuple)) or len(lst) == 0:
        raise RuntimeError(
            f"{name} must be a non-empty list, e.g. ['local'] or ['compute-1', 'compute-2']"
        )


# ---------------------------------------------------------------------------
# Early validation
# ---------------------------------------------------------------------------

validate_compute_list("COMPUTE_SPINES",  COMPUTE_SPINES)
validate_compute_list("COMPUTE_LEAVES",  COMPUTE_LEAVES)
validate_compute_list("COMPUTE_SERVERS", COMPUTE_SERVERS)

if BORDER_FIREWALL_COUNT not in (0, 1, 2):
    raise RuntimeError("BORDER_FIREWALL_COUNT must be 0, 1, or 2.")

if BORDER_FIREWALL_COUNT > 0:
    validate_compute_list("COMPUTE_FIREWALLS", COMPUTE_FIREWALLS)
    if not MLAG_PAIRS:
        raise RuntimeError(
            "BORDER_FIREWALL_COUNT > 0 requires at least one MLAG pair. "
            "The FIRST pair is always used as the border leaf pair."
        )

# Resolve border pair leaf 1-based indexes before connecting to GNS3.
border_leaf_indices = []
if BORDER_FIREWALL_COUNT > 0:
    _left, _right = parse_mlag_pair(MLAG_PAIRS[0], 1)
    border_leaf_indices = [_left, _right]


def leaf_node_name(leaf_1based_index):
    """Return the GNS3 node name for a leaf.

    Border leaves are named Border-1, Border-2 in the order they appear in
    MLAG_PAIRS[0].  All others are named Leaf-N.
    Must match inventory.py exactly so Ansible targets the right host.
    """
    if leaf_1based_index in border_leaf_indices:
        pos = border_leaf_indices.index(leaf_1based_index) + 1
        return f"Border-{pos}"
    return f"Leaf-{leaf_1based_index}"


# ---------------------------------------------------------------------------
# Connect to GNS3
# ---------------------------------------------------------------------------

print("Authenticating with GNS3 API...")
gns3 = GNS3Client(GNS3_SERVER, GNS3_USER, GNS3_PASSWORD)
print("Authentication successful!\n")

project_id = None

try:
    # ======================================================================
    # 1. Create or open the project
    # ======================================================================
    print(f"Opening project: {PROJECT_NAME}...")
    project = gns3.create_project(PROJECT_NAME)
    if project is None:
        print(f"Project '{PROJECT_NAME}' already exists. Deleting and recreating...")
        projects = gns3.get_projects()
        old_id   = next(p["project_id"] for p in projects if p["name"] == PROJECT_NAME)
        gns3.delete_project(old_id)
        project = gns3.create_project(PROJECT_NAME)

    if project is None:
        raise RuntimeError(f"Failed to create project '{PROJECT_NAME}'")

    project_id = project["project_id"]
    print(f"Project ID: {project_id}\n")

    # ======================================================================
    # 2. Resolve compute names -> IDs
    # ======================================================================
    print("Loading computes...")
    computes    = gns3.get_computes()
    compute_map = {c["name"]: c["compute_id"] for c in computes}
    compute_map["local"] = "local"

    def resolve(name):
        if name in compute_map:
            return compute_map[name]
        raise RuntimeError(
            f"Compute '{name}' not found. Available: {list(compute_map.keys())}"
        )

    def resolve_list(names):
        return [resolve(n) for n in names]

    compute_ids_spines    = resolve_list(COMPUTE_SPINES)
    compute_ids_leaves    = resolve_list(COMPUTE_LEAVES)
    compute_ids_servers   = resolve_list(COMPUTE_SERVERS)
    compute_ids_firewalls = resolve_list(COMPUTE_FIREWALLS) if BORDER_FIREWALL_COUNT > 0 else []

    print(f"Spine     computes: {COMPUTE_SPINES}")
    print(f"Leaf      computes: {COMPUTE_LEAVES}")
    print(f"Server    computes: {COMPUTE_SERVERS}")
    if BORDER_FIREWALL_COUNT > 0:
        print(f"Firewall  computes: {COMPUTE_FIREWALLS}")
    print()

    # ======================================================================
    # 3. Fetch templates
    # ======================================================================
    templates = gns3.get_templates()

    def get_template_id(name):
        tid = next((t["template_id"] for t in templates if t["name"] == name), None)
        if not tid:
            raise RuntimeError(f"Template '{name}' not found in GNS3!")
        return tid

    template_id_arista   = get_template_id(TEMPLATE_NAME_ARISTA)
    template_id_frr      = get_template_id(TEMPLATE_NAME_FRR)
    template_id_proxmox  = get_template_id(TEMPLATE_NAME_PROXMOX)
    template_id_firewall = get_template_id(TEMPLATE_NAME_FIREWALL) if BORDER_FIREWALL_COUNT > 0 else None

    print(f"Template {TEMPLATE_NAME_ARISTA}:  {template_id_arista}")
    print(f"Template {TEMPLATE_NAME_FRR}:     {template_id_frr}")
    print(f"Template {TEMPLATE_NAME_PROXMOX}: {template_id_proxmox}")
    if template_id_firewall:
        print(f"Template {TEMPLATE_NAME_FIREWALL}: {template_id_firewall}")
    print()

    # ======================================================================
    # 4. Spawn nodes
    # ======================================================================
    spines       = []
    leaves       = []       # leaves[j] corresponds to 1-based leaf index j+1
    servers      = []
    firewalls    = []
    next_adapter = {}       # node_id -> next free data-plane adapter index

    def deploy(template_id, compute_id, name, x, y):
        node = gns3.create_node_from_template(project_id, template_id, compute_id, x, y)
        node = gns3.rename_node(project_id, node["node_id"], name)
        next_adapter[node["node_id"]] = 1
        print(f" -> Created {name} (Node ID: {node['node_id']}, compute: {compute_id})")
        return node

    print("Spawning Spines...")
    for i in range(NUM_SPINES):
        compute_id = compute_ids_spines[i % len(compute_ids_spines)]
        spines.append(deploy(template_id_arista, compute_id, f"Spine-{i+1}", i * 200, -100))

    print("Spawning Leaves...")
    for i in range(NUM_LEAVES):
        compute_id = compute_ids_leaves[i % len(compute_ids_leaves)]
        # Use the canonical Border-N / Leaf-N name at creation time.
        leaves.append(
            deploy(template_id_arista, compute_id, leaf_node_name(i + 1), i * 200, 100)
        )

    # Validate border pair indexes now that NUM_LEAVES is confirmed.
    if BORDER_FIREWALL_COUNT > 0:
        for idx in border_leaf_indices:
            if idx < 1 or idx > NUM_LEAVES:
                raise RuntimeError(
                    f"Border MLAG pair references leaf index {idx}, "
                    f"but NUM_LEAVES is {NUM_LEAVES}"
                )
        border_left_leaf  = leaves[border_leaf_indices[0] - 1]
        border_right_leaf = leaves[border_leaf_indices[1] - 1]
        print(
            f"\nBorder leaves: {border_left_leaf['name']} (left) "
            f"and {border_right_leaf['name']} (right)"
        )

    # Compute leaves — used for server assignment.
    # Border leaves never receive server downlinks.
    non_border_leaves = [
        leaves[j] for j in range(NUM_LEAVES)
        if (j + 1) not in border_leaf_indices
    ]

    print("\nSpawning Servers...")
    leaf_server_count = {}

    for i in range(NUM_SERVERS):
        compute_id  = compute_ids_servers[i % len(compute_ids_servers)]
        template_id = template_id_frr if i == 0 else template_id_proxmox

        if i == 0 and BORDER_FIREWALL_COUNT > 0:
            # FRR will be wired to the firewall, not a leaf.
            # Position it below the center of the border leaf pair.
            fw_mid_x = (border_left_leaf["x"] + border_right_leaf["x"]) // 2
            servers.append(deploy(template_id, compute_id, "Server-1", fw_mid_x, 750))
            continue

        if not non_border_leaves:
            raise RuntimeError(
                "No compute leaves available for servers. "
                "Increase NUM_LEAVES or reduce border pair count."
            )

        # Adjust index: when FRR (i==0) was skipped above, subtract 1 so
        # the round-robin over compute leaves stays contiguous.
        effective_index = i - (1 if BORDER_FIREWALL_COUNT > 0 else 0)
        leaf  = non_border_leaves[effective_index % len(non_border_leaves)]
        count = leaf_server_count.get(leaf["node_id"], 0)
        leaf_server_count[leaf["node_id"]] = count + 1

        servers.append(
            deploy(template_id, compute_id, f"Server-{i+1}", leaf["x"], 300 + count * 150)
        )

    # ======================================================================
    # 5. Spawn management switch and cloud
    # ======================================================================
    print("\nSpawning Management Switch...")
    mgmt_switch = gns3.create_node(
        project_id, "MGMT-Switch", "ethernet_switch", "local",
        (NUM_LEAVES * 200) // 2, -300,
    )
    gns3.set_switch_ports(project_id, mgmt_switch["node_id"], 24)
    mgmt_port = [0]
    print(f" -> Created MGMT-Switch (Node ID: {mgmt_switch['node_id']})")

    print("Spawning Cloud node...")
    cloud = gns3.create_node(
        project_id, "Cloud", "cloud", "local",
        (NUM_LEAVES * 200) // 2, -400,
        properties={
            "interfaces": [{"name": MGMT_BRIDGE, "type": "ethernet", "special": False}]
        },
    )
    print(f" -> Created Cloud node (Node ID: {cloud['node_id']})")

    # ======================================================================
    # 6. Wire spine-leaf fabric
    # ======================================================================
    print("\nLinking the fabric (Every Leaf to Every Spine)...")
    for leaf in leaves:
        for spine in spines:
            la = next_adapter[leaf["node_id"]]
            sa = next_adapter[spine["node_id"]]
            gns3.create_link(project_id, leaf["node_id"], la, spine["node_id"], sa)
            print(f" -> {leaf['name']} (Eth{la}) <---> {spine['name']} (Eth{sa})")
            next_adapter[leaf["node_id"]]  += 1
            next_adapter[spine["node_id"]] += 1

    # ======================================================================
    # 7. Wire servers -> compute leaves
    # ======================================================================
    print("\nLinking Servers to Leaves...")
    for i, server in enumerate(servers):
        # FRR (Server-1, i==0) is wired in the DMZ step when a firewall exists.
        if BORDER_FIREWALL_COUNT > 0 and i == 0:
            print(
                f" -> Skipping {server['name']} (will be wired to firewall in DMZ step)"
            )
            continue

        if not non_border_leaves:
            raise RuntimeError("No compute leaves available to connect servers.")

        effective_index = i - (1 if BORDER_FIREWALL_COUNT > 0 else 0)
        leaf = non_border_leaves[effective_index % len(non_border_leaves)]
        sa   = next_adapter[server["node_id"]]
        la   = next_adapter[leaf["node_id"]]
        gns3.create_link(project_id, server["node_id"], sa, leaf["node_id"], la)
        print(f" -> {server['name']} (Eth{sa}) <---> {leaf['name']} (Eth{la})")
        next_adapter[server["node_id"]] += 1
        next_adapter[leaf["node_id"]]   += 1

    # ======================================================================
    # 8. Wire MLAG peer links
    # ======================================================================
    if MLAG_PEER_LINK_MEMBER_COUNT < 1:
        raise RuntimeError("MLAG_PEER_LINK_MEMBER_COUNT must be >= 1")

    print("\nLinking MLAG peer leaves from SSOT...")
    paired_leaf_indexes = set()

    for pair_number, pair in enumerate(MLAG_PAIRS, start=1):
        left_idx, right_idx = parse_mlag_pair(pair, pair_number)

        if left_idx == right_idx:
            raise RuntimeError(
                f"MLAG_PAIRS entry #{pair_number} references the same leaf twice: {left_idx}"
            )
        for leaf_idx in (left_idx, right_idx):
            if leaf_idx < 1 or leaf_idx > NUM_LEAVES:
                raise RuntimeError(
                    f"MLAG_PAIRS entry #{pair_number} uses leaf index {leaf_idx}, "
                    f"but NUM_LEAVES is {NUM_LEAVES}"
                )
        if left_idx in paired_leaf_indexes or right_idx in paired_leaf_indexes:
            raise RuntimeError(
                f"Each leaf can belong to only one MLAG pair. "
                f"Conflict in entry #{pair_number}: {left_idx},{right_idx}"
            )
        paired_leaf_indexes.add(left_idx)
        paired_leaf_indexes.add(right_idx)

        left_leaf  = leaves[left_idx  - 1]
        right_leaf = leaves[right_idx - 1]

        for _ in range(MLAG_PEER_LINK_MEMBER_COUNT):
            la = next_adapter[left_leaf["node_id"]]
            ra = next_adapter[right_leaf["node_id"]]
            gns3.create_link(
                project_id,
                left_leaf["node_id"],  la,
                right_leaf["node_id"], ra,
            )
            print(
                f" -> Pair {pair_number}: {left_leaf['name']} (Eth{la}) "
                f"<---> {right_leaf['name']} (Eth{ra})"
            )
            next_adapter[left_leaf["node_id"]]  += 1
            next_adapter[right_leaf["node_id"]] += 1

    # ======================================================================
    # 9. Spawn and wire DMZ firewalls
    # ======================================================================
    #
    # Single FW (BORDER_FIREWALL_COUNT == 1):
    #   Border-1 (Eth?) -- FW-1 port-1 (LAN-1)
    #   Border-2 (Eth?) -- FW-1 port-2 (LAN-2)
    #                       FW-1 port-3 (WAN)  -- FRR ens1
    #
    # Dual FW (BORDER_FIREWALL_COUNT == 2):
    #   Border-1 (Eth?) -- FW-1 port-1 (LAN)
    #                       FW-1 port-2 (WAN)  -- FRR ens1
    #   Border-2 (Eth?) -- FW-2 port-1 (LAN)
    #                       FW-2 port-2 (WAN)  -- FRR ens2
    #
    # The adapter numbers printed here are ground truth — inventory.py
    # independently computes the same numbers via build_mlag_leaf_vars().

    dmz_iface_left  = None
    dmz_iface_right = None

    if BORDER_FIREWALL_COUNT > 0:
        frr  = servers[0]
        fw_y = 500
        fw_x_mid = (border_left_leaf["x"] + border_right_leaf["x"]) // 2

        print(f"\nSpawning Firewall(s) in the DMZ...")
        for i in range(BORDER_FIREWALL_COUNT):
            compute_id = compute_ids_firewalls[i % len(compute_ids_firewalls)]
            fw_x = fw_x_mid + (i * 250) - ((BORDER_FIREWALL_COUNT - 1) * 125)
            firewalls.append(
                deploy(template_id_firewall, compute_id, f"FW-{i+1}", fw_x, fw_y)
            )

        fw1 = firewalls[0]

        if BORDER_FIREWALL_COUNT == 1:
            # Border-1 -> FW-1 LAN port-1
            la = next_adapter[border_left_leaf["node_id"]]
            fa = next_adapter[fw1["node_id"]]
            gns3.create_link(project_id, border_left_leaf["node_id"], la, fw1["node_id"], fa)
            print(f" -> {border_left_leaf['name']} (Eth{la}) <---> FW-1 (port {fa}) [LAN-1]")
            dmz_iface_left = f"Ethernet{la}"
            next_adapter[border_left_leaf["node_id"]] += 1
            next_adapter[fw1["node_id"]]              += 1

            # Border-2 -> FW-1 LAN port-2
            ra = next_adapter[border_right_leaf["node_id"]]
            fa = next_adapter[fw1["node_id"]]
            gns3.create_link(project_id, border_right_leaf["node_id"], ra, fw1["node_id"], fa)
            print(f" -> {border_right_leaf['name']} (Eth{ra}) <---> FW-1 (port {fa}) [LAN-2]")
            dmz_iface_right = f"Ethernet{ra}"
            next_adapter[border_right_leaf["node_id"]] += 1
            next_adapter[fw1["node_id"]]               += 1

            # FW-1 WAN -> FRR ens1
            fa  = next_adapter[fw1["node_id"]]
            fra = next_adapter[frr["node_id"]]
            gns3.create_link(project_id, fw1["node_id"], fa, frr["node_id"], fra)
            print(f" -> FW-1 (port {fa}) [WAN] <---> {frr['name']} (ens{fra})")
            next_adapter[fw1["node_id"]] += 1
            next_adapter[frr["node_id"]] += 1

        elif BORDER_FIREWALL_COUNT == 2:
            fw2 = firewalls[1]

            # Border-1 -> FW-1 LAN
            la = next_adapter[border_left_leaf["node_id"]]
            fa = next_adapter[fw1["node_id"]]
            gns3.create_link(project_id, border_left_leaf["node_id"], la, fw1["node_id"], fa)
            print(f" -> {border_left_leaf['name']} (Eth{la}) <---> FW-1 (port {fa}) [LAN]")
            dmz_iface_left = f"Ethernet{la}"
            next_adapter[border_left_leaf["node_id"]] += 1
            next_adapter[fw1["node_id"]]              += 1

            # FW-1 WAN -> FRR ens1
            fa  = next_adapter[fw1["node_id"]]
            fra = next_adapter[frr["node_id"]]
            gns3.create_link(project_id, fw1["node_id"], fa, frr["node_id"], fra)
            print(f" -> FW-1 (port {fa}) [WAN] <---> {frr['name']} (ens{fra})")
            next_adapter[fw1["node_id"]] += 1
            next_adapter[frr["node_id"]] += 1

            # Border-2 -> FW-2 LAN
            ra  = next_adapter[border_right_leaf["node_id"]]
            fa2 = next_adapter[fw2["node_id"]]
            gns3.create_link(project_id, border_right_leaf["node_id"], ra, fw2["node_id"], fa2)
            print(f" -> {border_right_leaf['name']} (Eth{ra}) <---> FW-2 (port {fa2}) [LAN]")
            dmz_iface_right = f"Ethernet{ra}"
            next_adapter[border_right_leaf["node_id"]] += 1
            next_adapter[fw2["node_id"]]               += 1

            # FW-2 WAN -> FRR ens2
            fa2 = next_adapter[fw2["node_id"]]
            fra = next_adapter[frr["node_id"]]
            gns3.create_link(project_id, fw2["node_id"], fa2, frr["node_id"], fra)
            print(f" -> FW-2 (port {fa2}) [WAN] <---> {frr['name']} (ens{fra})")
            next_adapter[fw2["node_id"]] += 1
            next_adapter[frr["node_id"]] += 1

        fw_label = "FW-1" if BORDER_FIREWALL_COUNT == 1 else "FW-1 / FW-2"
        print(f"\n{'='*62}")
        print(f"  DMZ Interface Summary  (matches inventory.py host_vars)")
        print(f"{'='*62}")
        print(f"  {border_left_leaf['name']:14s}  dmz_handoff_interface: {dmz_iface_left}")
        print(f"  {border_right_leaf['name']:14s}  dmz_handoff_interface: {dmz_iface_right}")
        print(f"  Firewall(s): {fw_label}")
        print(f"{'='*62}\n")

    # ======================================================================
    # 10. Wire management network
    # ======================================================================
    print("\nWiring management network...")
    for node in spines + leaves + servers + firewalls:
        gns3.create_link(
            project_id,
            node["node_id"], 0,
            mgmt_switch["node_id"], 0,
            port_b=mgmt_port[0],
        )
        print(f" -> {node['name']} (Mgmt0) <---> MGMT-Switch (port {mgmt_port[0]})")
        mgmt_port[0] += 1

    gns3.create_link(
        project_id, mgmt_switch["node_id"], 0, cloud["node_id"], 0,
        port_a=mgmt_port[0],
    )
    print(f" -> MGMT-Switch (port {mgmt_port[0]}) <---> Cloud ({MGMT_BRIDGE})")

    # ======================================================================
    # 11. Start the lab
    # ======================================================================
    print("\nStarting all nodes...")
    gns3.start_nodes(project_id)
    print("Success! The Spine-Leaf fabric is deployed, wired, and booting.")
    if BORDER_FIREWALL_COUNT > 0:
        print(
            "\nNEXT STEPS:\n"
            "  1. Run border_leaf.yml -- DMZ subinterfaces auto-configured via inventory.py.\n"
            "  2. Manually configure FortiGate(s) via console (LAN/WAN IPs, BGP policy).\n"
            "  3. Run config_frr.py to configure FRR BGP toward the firewall(s)."
        )

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
