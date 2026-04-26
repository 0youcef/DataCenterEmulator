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
    PROJECT_NAME,
    TEMPLATE_NAME_ARISTA,
    TEMPLATE_NAME_FRR,       # Debian — Server-1 only
    TEMPLATE_NAME_PROXMOX,   # Proxmox — all other servers
    COMPUTE_SPINES,
    COMPUTE_LEAVES,
    COMPUTE_SERVERS,
    MGMT_BASE_IP, MGMT_START,
)

# BUG FIX: anchor CONFIG_FILE to the script's own directory, not CWD.
# The old "../sots/config.py" broke whenever the script was run from any
# directory other than the repo root.
CONFIG_FILE = os.path.join(os.path.dirname(__file__), '..', 'sots', 'config.py')


# ---------------------------------------------------------------------------
# Wireshark capture helper
# ---------------------------------------------------------------------------

def start_capture(gns3, project_id, link, peer_name: str):
    """Start a packet capture on *link* and report the result.

    GNS3 returns the capture file path in the response; we print it so the
    operator knows where Wireshark should pick it up.  A missing link_id is
    treated as a non-fatal warning so a single failed capture never aborts
    the whole operation.
    """
    link_id = link.get("link_id") if link else None
    if not link_id:
        print(f"   [Capture] WARNING: no link_id returned for link to {peer_name} — skipping.")
        return

    try:
        result  = gns3.start_capture(project_id, link_id)
        pcap    = result.get("capture_file_path", "<unknown path>")
        print(f"   [Capture] ▶  {peer_name}  →  {pcap}")
    except Exception as exc:
        # Capture failure must not abort node deployment.
        print(f"   [Capture] WARNING: could not start capture to {peer_name}: {exc}")


# ---------------------------------------------------------------------------
# Project / compute helpers
# ---------------------------------------------------------------------------

def get_project_id(gns3):
    projects = gns3.get_projects()
    project  = next((p for p in projects if p["name"] == PROJECT_NAME), None)
    if not project:
        raise RuntimeError(f"Project '{PROJECT_NAME}' not found. Deploy it first.")
    return project["project_id"]


def build_compute_map(gns3):
    """Fetch the compute list ONCE and return a name→id dict.

    BUG FIX: the old resolve_compute() called get_computes() on every
    invocation, issuing one API round-trip per link.  Now callers build the
    map a single time and pass it down.
    """
    computes    = gns3.get_computes()
    compute_map = {c["name"]: c["compute_id"] for c in computes}
    compute_map["local"] = "local"
    return compute_map


def resolve_compute(compute_map: dict, name: str) -> str:
    if name not in compute_map:
        raise RuntimeError(
            f"Compute '{name}' not found. Available: {list(compute_map.keys())}"
        )
    return compute_map[name]


def pick_compute(compute_map: dict, compute_list: list, index: int) -> str:
    """Round-robin across compute_list using 0-based *index*."""
    name = compute_list[index % len(compute_list)]
    return resolve_compute(compute_map, name)


# ---------------------------------------------------------------------------
# Existing-node discovery
# ---------------------------------------------------------------------------

def get_existing_nodes(gns3, project_id):
    """Return categorized nodes, a next-adapter closure, links, and next mgmt port.

    Returns
    -------
    spines, leaves, servers, mgmt_switch, next_adapter, next_mgmt_port, links
    """
    nodes = gns3.get_nodes(project_id)
    links = gns3.get_links(project_id)

    spines = sorted(
        [n for n in nodes if n["name"].startswith("Spine-")],
        key=lambda n: n["name"],
    )

    # BUG FIX: the old filter only matched "Border-1", missing "Border-2"
    # (and any future border leaves).  startswith("Border-") is correct.
    leaves = sorted(
        [n for n in nodes
         if n["name"].startswith("Leaf-") or n["name"].startswith("Border-")],
        key=lambda n: n["name"],
    )

    servers = sorted(
        [n for n in nodes if n["name"].startswith("Server-")],
        key=lambda n: n["name"],
    )

    mgmt_switch = next((n for n in nodes if n["name"] == "MGMT-Switch"), None)
    if not mgmt_switch:
        raise RuntimeError(
            "MGMT-Switch not found in project. Has the topology been deployed?"
        )

    # Build used-adapter map: node_id → highest adapter number in use.
    used_adapters: dict[str, int] = {}
    for link in links:
        for endpoint in link["nodes"]:
            nid     = endpoint["node_id"]
            adapter = endpoint["adapter_number"]
            if nid not in used_adapters or adapter > used_adapters[nid]:
                used_adapters[nid] = adapter

    def next_adapter(node_id: str) -> int:
        """Return the next free adapter index for *node_id* and advance the counter."""
        current = used_adapters.get(node_id, 0) + 1
        used_adapters[node_id] = current
        return current

    # Find next free management-switch port.
    mgmt_ports_used = {
        endpoint["port_number"]
        for link in links
        for endpoint in link["nodes"]
        if endpoint["node_id"] == mgmt_switch["node_id"]
    }
    next_mgmt_port = (max(mgmt_ports_used) + 1) if mgmt_ports_used else 0

    # BUG FIX: return *links* so callers don't have to issue a second
    # get_links() API call (as add_server previously did).
    return spines, leaves, servers, mgmt_switch, next_adapter, next_mgmt_port, links


# ---------------------------------------------------------------------------
# config.py updater
# ---------------------------------------------------------------------------

def update_config(key: str, new_value: int):
    """Increment a NUM_ variable in the source-of-truth config.py."""
    with open(CONFIG_FILE, "r") as f:
        content = f.read()
    updated = re.sub(
        rf"^({key}\s*=\s*)(\d+)",
        lambda m: f"{m.group(1)}{new_value}",
        content,
        flags=re.MULTILINE,
    )
    with open(CONFIG_FILE, "w") as f:
        f.write(updated)
    print(f" -> Updated {key} = {new_value} in config.py")


# ---------------------------------------------------------------------------
# add_spine
# ---------------------------------------------------------------------------

def add_spine(gns3, project_id):
    spines, leaves, servers, mgmt_switch, next_adapter, next_mgmt_port, _ = \
        get_existing_nodes(gns3, project_id)

    compute_map = build_compute_map(gns3)

    templates   = gns3.get_templates()
    template_id = next(
        (t["template_id"] for t in templates if t["name"] == TEMPLATE_NAME_ARISTA), None
    )
    if not template_id:
        raise RuntimeError(f"Template '{TEMPLATE_NAME_ARISTA}' not found")

    name       = f"Spine-{len(spines) + 1}"
    compute_id = pick_compute(compute_map, COMPUTE_SPINES, len(spines))
    x          = len(spines) * 200
    print(
        f"Adding {name} on compute "
        f"'{COMPUTE_SPINES[len(spines) % len(COMPUTE_SPINES)]}'..."
    )

    node = gns3.create_node_from_template(project_id, template_id, compute_id, x, -100)
    node = gns3.rename_node(project_id, node["node_id"], name)

    # Wire to every existing leaf and immediately start a capture on each link.
    print(f" Wiring {name} to {len(leaves)} leaf(ves) + starting captures...")
    for leaf in leaves:
        la   = next_adapter(leaf["node_id"])
        sa   = next_adapter(node["node_id"])
        link = gns3.create_link(project_id, leaf["node_id"], la, node["node_id"], sa)
        print(f" -> {leaf['name']} (Eth{la}) <---> {name} (Eth{sa})")
        # BUG FIX: capture was completely missing from add_spine.
        start_capture(gns3, project_id, link, f"{leaf['name']}↔{name}")

    # Wire management (no capture on mgmt links — not useful for data-plane debug).
    link = gns3.create_link(
        project_id, node["node_id"], 0,
        mgmt_switch["node_id"], 0, port_b=next_mgmt_port,
    )
    print(f" -> {name} (Mgmt0) <---> MGMT-Switch (port {next_mgmt_port})")

    gns3.start_node(project_id, node["node_id"])
    print(f" -> Started {name}")

    update_config("NUM_SPINES", len(spines) + 1)
    print(f"Done. {name} added, wired, captures running, and booted.")


# ---------------------------------------------------------------------------
# add_leaf
# ---------------------------------------------------------------------------

def add_leaf(gns3, project_id):
    spines, leaves, servers, mgmt_switch, next_adapter, next_mgmt_port, _ = \
        get_existing_nodes(gns3, project_id)

    compute_map = build_compute_map(gns3)

    templates   = gns3.get_templates()
    template_id = next(
        (t["template_id"] for t in templates if t["name"] == TEMPLATE_NAME_ARISTA), None
    )
    if not template_id:
        raise RuntimeError(f"Template '{TEMPLATE_NAME_ARISTA}' not found")

    name       = f"Leaf-{len(leaves) + 1}"
    compute_id = pick_compute(compute_map, COMPUTE_LEAVES, len(leaves))
    x          = len(leaves) * 200
    print(
        f"Adding {name} on compute "
        f"'{COMPUTE_LEAVES[len(leaves) % len(COMPUTE_LEAVES)]}'..."
    )

    node = gns3.create_node_from_template(project_id, template_id, compute_id, x, 100)
    node = gns3.rename_node(project_id, node["node_id"], name)

    # Wire to every existing spine and immediately start captures.
    print(f" Wiring {name} to {len(spines)} spine(s) + starting captures...")
    for spine in spines:
        sa   = next_adapter(spine["node_id"])
        la   = next_adapter(node["node_id"])
        link = gns3.create_link(project_id, node["node_id"], la, spine["node_id"], sa)
        print(f" -> {name} (Eth{la}) <---> {spine['name']} (Eth{sa})")
        start_capture(gns3, project_id, link, f"{name}↔{spine['name']}")

    # Wire management.
    gns3.create_link(
        project_id, node["node_id"], 0,
        mgmt_switch["node_id"], 0, port_b=next_mgmt_port,
    )
    print(f" -> {name} (Mgmt0) <---> MGMT-Switch (port {next_mgmt_port})")

    gns3.start_node(project_id, node["node_id"])
    print(f" -> Started {name}")

    update_config("NUM_LEAVES", len(leaves) + 1)
    print(f"Done. {name} added, wired, captures running, and booted.")


# ---------------------------------------------------------------------------
# add_server
# ---------------------------------------------------------------------------

def add_server(gns3, project_id):
    spines, leaves, servers, mgmt_switch, next_adapter, next_mgmt_port, links = \
        get_existing_nodes(gns3, project_id)

    compute_map = build_compute_map(gns3)

    templates = gns3.get_templates()

    def get_tid(tname):
        tid = next((t["template_id"] for t in templates if t["name"] == tname), None)
        if not tid:
            raise RuntimeError(f"Template '{tname}' not found")
        return tid

    # Server-1 is the FRR internet simulator (Debian); every other server is Proxmox.
    is_first_server = (len(servers) == 0)
    template_id     = get_tid(TEMPLATE_NAME_FRR if is_first_server else TEMPLATE_NAME_PROXMOX)

    name       = f"Server-{len(servers) + 1}"
    compute_id = pick_compute(compute_map, COMPUTE_SERVERS, len(servers))
    print(
        f"Adding {name} ({'FRR/Debian' if is_first_server else 'Proxmox'}) on compute "
        f"'{COMPUTE_SERVERS[len(servers) % len(COMPUTE_SERVERS)]}'..."
    )

    # Target leaf — same round-robin as deploy_fabric.py.
    leaf = leaves[len(servers) % len(leaves)]

    # BUG FIX: the old code called gns3.get_links() a second time here.
    # We now reuse the *links* already returned by get_existing_nodes().
    server_ids = {s["node_id"] for s in servers}
    servers_on_leaf = sum(
        1
        for link in links
        for ep in link["nodes"]
        if ep["node_id"] == leaf["node_id"]
        for other_ep in link["nodes"]
        if other_ep["node_id"] != leaf["node_id"]
        and other_ep["node_id"] in server_ids
    )

    node = gns3.create_node_from_template(
        project_id, template_id, compute_id,
        leaf["x"], 300 + servers_on_leaf * 150,
    )
    node = gns3.rename_node(project_id, node["node_id"], name)

    # Wire server → leaf and start capture.
    sa   = next_adapter(node["node_id"])
    la   = next_adapter(leaf["node_id"])
    link = gns3.create_link(project_id, node["node_id"], sa, leaf["node_id"], la)
    print(f" -> {name} (Eth{sa}) <---> {leaf['name']} (Eth{la})")
    # BUG FIX: capture was completely missing from add_server.
    start_capture(gns3, project_id, link, f"{name}↔{leaf['name']}")

    # Wire management.
    gns3.create_link(
        project_id, node["node_id"], 0,
        mgmt_switch["node_id"], 0, port_b=next_mgmt_port,
    )
    print(f" -> {name} (Mgmt0) <---> MGMT-Switch (port {next_mgmt_port})")

    gns3.start_node(project_id, node["node_id"])
    print(f" -> Started {name}")

    update_config("NUM_SERVERS", len(servers) + 1)
    print(f"Done. {name} added, wired, capture running, and booted.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

COMMANDS = {
    "spine":  add_spine,
    "leaf":   add_leaf,
    "server": add_server,
}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(f"Usage: python add_node.py [{' | '.join(COMMANDS)}]")
        sys.exit(1)

    node_type = sys.argv[1]

    print("Authenticating with GNS3 API...")
    gns3 = GNS3Client(GNS3_SERVER, GNS3_USER, GNS3_PASSWORD)
    print("Authentication successful!\n")

    project_id = get_project_id(gns3)
    gns3.open_project(project_id)
    print(f"Project ID: {project_id}\n")

    try:
        COMMANDS[node_type](gns3, project_id)
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)
