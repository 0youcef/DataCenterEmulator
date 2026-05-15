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
    COMPUTE_SPINES,  # list — round-robined across spines
    COMPUTE_LEAVES,  # list — round-robined across leaves
    COMPUTE_SERVERS,  # list — round-robined across servers
    COMPUTE_FIREWALLS,  # list — round-robined across firewalls
    MGMT_BRIDGE,
    MLAG_PAIRS,
    MLAG_PEER_LINK_MEMBER_COUNT,
    ENABLE_DMZ_FIREWALL,
    FIREWALL_NODE_NAME,
    FIREWALL_WAN_UPSTREAM_NODE_NAME,
    FIREWALL_BORDER2_LEAF_INDEX,
    FIREWALL_BORDER1_LEAF_INDEX,
)
from sots.vlans import TENANTS


def parse_mlag_pair(pair, pair_number):
    if isinstance(pair, str):
        parts = [p.strip() for p in pair.split(",") if p.strip()]
    elif isinstance(pair, (list, tuple)):
        parts = list(pair)
    else:
        raise RuntimeError(
            f"MLAG_PAIRS entry #{pair_number} must be list/tuple or comma-separated string, "
            f"got {type(pair).__name__}"
        )

    if len(parts) != 2:
        raise RuntimeError(
            f"MLAG_PAIRS entry #{pair_number} must contain exactly two leaf indexes"
        )

    try:
        left_idx = int(parts[0])
        right_idx = int(parts[1])
    except ValueError as exc:
        raise RuntimeError(
            f"MLAG_PAIRS entry #{pair_number} must contain integer leaf indexes"
        ) from exc

    return left_idx, right_idx


def ethernet_to_adapter(interface_name):
    if not isinstance(interface_name, str) or not interface_name.startswith("Ethernet"):
        return None
    suffix = interface_name[len("Ethernet") :]
    return int(suffix) if suffix.isdigit() else None


def leaf_node_name(leaf_1based_index, border_left_idx, border_right_idx, enable_dmz):
    """Return the canonical GNS3 node name for a leaf.
    Mirrors the same logic in config_switches.py and inventory.py.
    """
    if enable_dmz:
        if leaf_1based_index == border_left_idx:
            return "Border-1"
        if leaf_1based_index == border_right_idx:
            return "Border-2"
    return f"Leaf-{leaf_1based_index}"


def validate_compute_list(name, lst):
    """Ensure compute list is a non-empty list of strings."""
    if not isinstance(lst, (list, tuple)) or len(lst) == 0:
        raise RuntimeError(
            f"{name} must be a non-empty list, e.g. ['local'] or ['compute-1', 'compute-2']"
        )


validate_compute_list("COMPUTE_SPINES", COMPUTE_SPINES)
validate_compute_list("COMPUTE_LEAVES", COMPUTE_LEAVES)
validate_compute_list("COMPUTE_SERVERS", COMPUTE_SERVERS)
if ENABLE_DMZ_FIREWALL:
    validate_compute_list("COMPUTE_FIREWALLS", COMPUTE_FIREWALLS)

if not MLAG_PAIRS:
    raise RuntimeError(
        "MLAG_PAIRS must be non-empty — first pair defines the border leaves."
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
        old_id = next(p["project_id"] for p in projects if p["name"] == PROJECT_NAME)
        gns3.delete_project(old_id)
        project = gns3.create_project(PROJECT_NAME)

    if project is None:
        raise RuntimeError(f"Failed to create project '{PROJECT_NAME}'")

    project_id = project["project_id"]
    print(f"Project ID: {project_id}\n")

    # ==========================================
    # 2. Resolve compute names → IDs
    # ==========================================
    print("Loading computes...")
    computes = gns3.get_computes()
    compute_map = {c["name"]: c["compute_id"] for c in computes}
    compute_map["local"] = "local"

    def resolve(name):
        if name in compute_map:
            return compute_map[name]
        raise RuntimeError(
            f"Compute '{name}' not found. Available: {list(compute_map.keys())}"
        )

    def resolve_list(names):
        """Resolve a list of compute names, validating each one exists."""
        return [resolve(n) for n in names]

    compute_ids_spines = resolve_list(COMPUTE_SPINES)
    compute_ids_leaves = resolve_list(COMPUTE_LEAVES)
    compute_ids_servers = resolve_list(COMPUTE_SERVERS)
    compute_ids_firewalls = (
        resolve_list(COMPUTE_FIREWALLS) if ENABLE_DMZ_FIREWALL else []
    )

    print(f"Spine  computes: {COMPUTE_SPINES}")
    print(f"Leaf   computes: {COMPUTE_LEAVES}")
    print(f"Server computes: {COMPUTE_SERVERS}\n")

    # ==========================================
    # 3. Fetch templates
    # ==========================================
    templates = gns3.get_templates()

    def get_template_id(name):
        tid = next((t["template_id"] for t in templates if t["name"] == name), None)
        if not tid:
            raise RuntimeError(f"Template '{name}' not found in GNS3!")
        return tid

    template_id_arista = get_template_id(TEMPLATE_NAME_ARISTA)
    template_id_frr = get_template_id(TEMPLATE_NAME_FRR)
    template_id_proxmox = get_template_id(TEMPLATE_NAME_PROXMOX)
    template_id_firewall = (
        get_template_id(TEMPLATE_NAME_FIREWALL) if ENABLE_DMZ_FIREWALL else None
    )

    print(f"Template {TEMPLATE_NAME_ARISTA}:  {template_id_arista}")
    print(f"Template {TEMPLATE_NAME_FRR}:     {template_id_frr}")
    print(f"Template {TEMPLATE_NAME_PROXMOX}: {template_id_proxmox}")
    if ENABLE_DMZ_FIREWALL:
        print(f"Template {TEMPLATE_NAME_FIREWALL}: {template_id_firewall}")
    print()

    # ==========================================
    # 4. Spawn nodes
    # ==========================================
    spines = []
    leaves = []
    servers = []
    firewalls = []
    next_adapter = {}

    def deploy(template_id, compute_id, name, x, y):
        node = gns3.create_node_from_template(project_id, template_id, compute_id, x, y)
        node = gns3.rename_node(project_id, node["node_id"], name)
        next_adapter[node["node_id"]] = 1
        print(f" -> Created {name} (Node ID: {node['node_id']}, compute: {compute_id})")
        return node

    print("Spawning Spines...")
    for i in range(NUM_SPINES):
        # Round-robin across COMPUTE_SPINES list
        compute_id = compute_ids_spines[i % len(compute_ids_spines)]
        spines.append(
            deploy(template_id_arista, compute_id, f"Spine-{i+1}", i * 200, -100)
        )

    # Resolve border-leaf indexes before naming leaves so Border-1/2 labels
    # are applied consistently during node creation.
    _bl_idx, _br_idx = parse_mlag_pair(MLAG_PAIRS[0], 1)

    print("Spawning Leaves...")
    for i in range(NUM_LEAVES):
        # Round-robin across COMPUTE_LEAVES list
        compute_id = compute_ids_leaves[i % len(compute_ids_leaves)]
        name = leaf_node_name(i + 1, _bl_idx, _br_idx, ENABLE_DMZ_FIREWALL)
        leaves.append(deploy(template_id_arista, compute_id, name, i * 200, 100))

    if ENABLE_DMZ_FIREWALL:
        print("Spawning Firewall...")
        firewalls.append(
            deploy(
                template_id_firewall,
                compute_ids_firewalls[0 % len(compute_ids_firewalls)],
                FIREWALL_NODE_NAME,
                (NUM_LEAVES * 200) // 2,
                500,
            )
        )

    # ------------------------------------------------------------------
    # Derive border and compute leaf lists from MLAG_PAIRS.
    #
    # MLAG_PAIRS[0]   → border leaves (the two leaves that dual-home
    #                   Server-1 / FRR and carry external handoffs).
    # MLAG_PAIRS[1:]  → compute MLAG pairs that host Proxmox servers.
    #
    # All indices in MLAG_PAIRS are 1-based (matching Leaf-N names).
    # ------------------------------------------------------------------

    border_left_leaf = leaves[_bl_idx - 1]
    border_right_leaf = leaves[_br_idx - 1]
    if ENABLE_DMZ_FIREWALL:
        for idx_name, idx_value in (
            ("FIREWALL_BORDER2_LEAF_INDEX", FIREWALL_BORDER2_LEAF_INDEX),
            ("FIREWALL_BORDER1_LEAF_INDEX", FIREWALL_BORDER1_LEAF_INDEX),
        ):
            if idx_value < 1 or idx_value > NUM_LEAVES:
                raise RuntimeError(
                    f"{idx_name}={idx_value} is outside valid leaf range 1..{NUM_LEAVES}"
                )

    # Flat, ordered list of leaves belonging to non-border MLAG pairs.
    # Server-2+ are round-robined across this list, so they always land
    # on an MLAG pair (never on a standalone leaf or a border leaf).
    compute_leaf_list = []
    for _pair_num, _pair in enumerate(MLAG_PAIRS[1:], start=2):
        _l, _r = parse_mlag_pair(_pair, _pair_num)
        compute_leaf_list.extend([leaves[_l - 1], leaves[_r - 1]])

    # if not compute_leaf_list:
    #     raise RuntimeError(
    #         "No compute MLAG pairs found (MLAG_PAIRS has only one entry). "
    #         "Add at least one more pair for Proxmox servers."
    #     )

    print("Spawning Servers...")
    leaf_server_count = {}

    # ── Server-1: FRR internet simulator ──────────────────────────────
    # Positioned horizontally between the two border leaves.
    # Gets TWO fabric uplinks (one to each border leaf) — see section 6.
    _frr_x = (border_left_leaf["x"] + border_right_leaf["x"]) // 2
    servers.append(
        deploy(
            template_id_frr,
            compute_ids_servers[0 % len(compute_ids_servers)],
            "Server-1",
            _frr_x,
            300,
        )
    )

    # ── Server-2+: Proxmox compute nodes ──────────────────────────────
    # Round-robined across compute_leaf_list (MLAG_PAIRS[1:]).
    # Server-2 always lands on the first leaf of the second MLAG pair.
    for i in range(1, NUM_SERVERS):
        leaf = compute_leaf_list[(i - 1) % len(compute_leaf_list)]
        leaf_x = leaf["x"]
        count = leaf_server_count.get(leaf["node_id"], 0)
        leaf_server_count[leaf["node_id"]] = count + 1
        servers.append(
            deploy(
                template_id_proxmox,
                compute_ids_servers[i % len(compute_ids_servers)],
                f"Server-{i + 1}",
                leaf_x,
                300 + count * 150,
            )
        )

    # ==========================================
    # 5. Spawn management switch and cloud
    # ==========================================
    print("Spawning Management Switch...")
    mgmt_switch = gns3.create_node(
        project_id,
        "MGMT-Switch",
        "ethernet_switch",
        "local",
        (NUM_LEAVES * 200) // 2,
        -300,
    )
    gns3.set_switch_ports(project_id, mgmt_switch["node_id"], 24)
    mgmt_port = [0]
    print(f" -> Created MGMT-Switch (Node ID: {mgmt_switch['node_id']})")

    print("Spawning Cloud node...")
    cloud = gns3.create_node(
        project_id,
        "Cloud",
        "cloud",
        "local",
        (NUM_LEAVES * 200) // 2,
        -400,
        properties={
            "interfaces": [{"name": MGMT_BRIDGE, "type": "ethernet", "special": False}]
        },
    )
    print(f" -> Created Cloud node (Node ID: {cloud['node_id']})")

    # ==========================================
    # 6. Wire spine-leaf fabric
    # ==========================================
    print("\nLinking the fabric (Every Leaf to Every Spine)...")
    for leaf in leaves:
        for spine in spines:
            la, sa = next_adapter[leaf["node_id"]], next_adapter[spine["node_id"]]
            gns3.create_link(project_id, leaf["node_id"], la, spine["node_id"], sa)
            print(f" -> {leaf['name']} (Eth{la}) <---> {spine['name']} (Eth{sa})")
            next_adapter[leaf["node_id"]] += 1
            next_adapter[spine["node_id"]] += 1

    print("\nLinking Servers to Leaves...")

    # ── Server-1 (FRR): upstream router node ───────────────────────────
    # In firewall-centered mode, FRR peers only with OPNsense WAN and is
    # not directly linked to border leaves.
    _frr = servers[0]
    if not ENABLE_DMZ_FIREWALL:
        for _bl in [border_left_leaf, border_right_leaf]:
            _sa = next_adapter[_frr["node_id"]]
            _la = next_adapter[_bl["node_id"]]
            gns3.create_link(project_id, _frr["node_id"], _sa, _bl["node_id"], _la)
            print(f" -> {_frr['name']} (Eth{_sa}) <---> {_bl['name']} (Eth{_la})")
            next_adapter[_frr["node_id"]] += 1
            next_adapter[_bl["node_id"]] += 1

    # ── Server-2+ (Proxmox): single uplink, round-robined across ──────
    # compute_leaf_list (MLAG_PAIRS[1:]).  Server-2 always connects to
    # the first leaf of the second MLAG pair, matching the spawn order.
    for i, server in enumerate(servers[1:]):
        leaf = compute_leaf_list[i % len(compute_leaf_list)]
        sa = next_adapter[server["node_id"]]
        la = next_adapter[leaf["node_id"]]
        gns3.create_link(project_id, server["node_id"], sa, leaf["node_id"], la)
        print(f" -> {server['name']} (Eth{sa}) <---> {leaf['name']} (Eth{la})")
        next_adapter[server["node_id"]] += 1
        next_adapter[leaf["node_id"]] += 1

    # ==========================================
    # 7. Wire MLAG peer links
    # ==========================================
    if MLAG_PEER_LINK_MEMBER_COUNT < 1:
        raise RuntimeError("MLAG_PEER_LINK_MEMBER_COUNT must be >= 1")

    print("\nLinking MLAG peer leaves from SSOT...")
    # No adapter reservation needed: each border leaf has exactly ONE physical
    # link to the firewall, and all VRFs trunk their VLANs over that single link
    # via 802.1Q subinterfaces.  MLAG peer links simply use the next available
    # adapter after the spine uplink; the firewall link gets whatever comes next.
    #
    # handoff_interface in sots/vlans.py must be set to "Ethernet4" to match.

    # Resulting adapter layout (with NUM_SPINES=1, MLAG_PEER_LINK_MEMBER_COUNT=2):
    #   Eth0  management
    #   Eth1  spine uplink
    #   Eth2  MLAG peer link 1
    #   Eth3  MLAG peer link 2
    #   Eth4  firewall uplink  ← trunks ALL VRFs over VLAN 110 + 130 + any additional tenant VLANs

    def next_peer_link_adapter(leaf_index, node_id):
        adapter = next_adapter[node_id]
        next_adapter[node_id] = adapter + 1
        return adapter

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
        left_leaf = leaves[left_idx - 1]
        right_leaf = leaves[right_idx - 1]

        for _ in range(MLAG_PEER_LINK_MEMBER_COUNT):
            left_adapter = next_peer_link_adapter(left_idx, left_leaf["node_id"])
            right_adapter = next_peer_link_adapter(right_idx, right_leaf["node_id"])
            gns3.create_link(
                project_id,
                left_leaf["node_id"],
                left_adapter,
                right_leaf["node_id"],
                right_adapter,
            )
            print(
                f" -> Pair {pair_number}: {left_leaf['name']} (Eth{left_adapter}) "
                f"<---> {right_leaf['name']} (Eth{right_adapter})"
            )

    if ENABLE_DMZ_FIREWALL:
        print("\nLinking Firewall interfaces...")
        firewall = firewalls[0]
        upstream_node = next(
            (
                node
                for node in servers
                if node["name"] == FIREWALL_WAN_UPSTREAM_NODE_NAME
            ),
            None,
        )
        if upstream_node is None:
            raise RuntimeError(
                f"FIREWALL_WAN_UPSTREAM_NODE_NAME '{FIREWALL_WAN_UPSTREAM_NODE_NAME}' "
                "not found among deployed servers."
            )
        border2_leaf = leaves[FIREWALL_BORDER2_LEAF_INDEX - 1]
        border1_leaf = leaves[FIREWALL_BORDER1_LEAF_INDEX - 1]

        # adapter 1 (WAN) -> upstream FRR router
        fw_wan_adapter = next_adapter[firewall["node_id"]]
        upstream_adapter = next_adapter[upstream_node["node_id"]]
        gns3.create_link(
            project_id,
            firewall["node_id"],
            fw_wan_adapter,
            upstream_node["node_id"],
            upstream_adapter,
        )
        print(
            f" -> {firewall['name']} (Eth{fw_wan_adapter}/WAN) "
            f"<---> {upstream_node['name']} (Eth{upstream_adapter})"
        )
        next_adapter[firewall["node_id"]] += 1
        next_adapter[upstream_node["node_id"]] += 1

        # adapter 2 (vtnet2/BORDER2) → Border-2 trunk
        fw_border2_adapter = next_adapter[firewall["node_id"]]
        border2_leaf_adapter = next_adapter[border2_leaf["node_id"]]
        gns3.create_link(
            project_id,
            firewall["node_id"],
            fw_border2_adapter,
            border2_leaf["node_id"],
            border2_leaf_adapter,
        )
        print(
            f" -> {firewall['name']} (Eth{fw_border2_adapter}/BORDER2) "
            f"<---> {border2_leaf['name']} (Eth{border2_leaf_adapter})"
        )
        next_adapter[firewall["node_id"]] += 1
        next_adapter[border2_leaf["node_id"]] += 1

        # adapter 3 (vtnet3/BORDER1) → Border-1 trunk
        fw_border1_adapter = next_adapter[firewall["node_id"]]
        border1_leaf_adapter = next_adapter[border1_leaf["node_id"]]
        gns3.create_link(
            project_id,
            firewall["node_id"],
            fw_border1_adapter,
            border1_leaf["node_id"],
            border1_leaf_adapter,
        )
        print(
            f" -> {firewall['name']} (Eth{fw_border1_adapter}/BORDER1) "
            f"<---> {border1_leaf['name']} (Eth{border1_leaf_adapter})"
        )
        next_adapter[firewall["node_id"]] += 1
        next_adapter[border1_leaf["node_id"]] += 1

    # ==========================================
    # 8. Wire management network
    # ==========================================
    print("\nWiring management network...")
    for node in spines + leaves + servers + firewalls:
        gns3.create_link(
            project_id,
            node["node_id"],
            0,
            mgmt_switch["node_id"],
            0,
            port_b=mgmt_port[0],
        )
        print(f" -> {node['name']} (Mgmt0) <---> MGMT-Switch (port {mgmt_port[0]})")
        mgmt_port[0] += 1

    gns3.create_link(
        project_id, mgmt_switch["node_id"], 0, cloud["node_id"], 0, port_a=mgmt_port[0]
    )
    print(f" -> MGMT-Switch (port {mgmt_port[0]}) <---> Cloud ({MGMT_BRIDGE})")

    # ==========================================
    # 9. Start the lab
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
