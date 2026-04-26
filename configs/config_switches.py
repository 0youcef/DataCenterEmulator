from netmiko import ConnectHandler
from netmiko.exceptions import NetMikoTimeoutException, NetMikoAuthenticationException
import requests
import time
import socket
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from sots.config import (
    GNS3_SERVER, GNS3_USER, GNS3_PASSWORD,
    PROJECT_NAME,
    NUM_SPINES, NUM_LEAVES,
    MGMT_BASE_IP, MGMT_START,
    SSH_USER, SSH_PASS,
    COMPUTE_SPINES, COMPUTE_LEAVES,
    MLAG_PAIRS,
    BORDER_FIREWALL_COUNT,
)


# ---------------------------------------------------------------------------
# Border leaf naming — must match deploy_fabric.py and inventory.py exactly.
# ---------------------------------------------------------------------------

def _parse_pair(pair):
    if isinstance(pair, str):
        parts = [p.strip() for p in pair.split(",") if p.strip()]
    else:
        parts = list(pair)
    return int(parts[0]), int(parts[1])


border_leaf_indices = []
if MLAG_PAIRS:
    _left, _right = _parse_pair(MLAG_PAIRS[0])
    border_leaf_indices = [_left, _right]


def leaf_node_name(leaf_1based_index):
    """Return the canonical GNS3 node name for a leaf.

    Mirrors deploy_fabric.py and inventory.py — border leaves are named
    Border-1, Border-2 in the order they appear in MLAG_PAIRS[0];
    all other leaves are named Leaf-N.
    """
    if leaf_1based_index in border_leaf_indices:
        pos = border_leaf_indices.index(leaf_1based_index) + 1
        return f"Border-{pos}"
    return f"Leaf-{leaf_1based_index}"


# ---------------------------------------------------------------------------
# Build switch name -> mgmt IP mapping using the canonical names.
# Order matches deploy_fabric.py: spines first, then leaves in index order.
# ---------------------------------------------------------------------------

SWITCHES = {}
counter = MGMT_START

for i in range(1, NUM_SPINES + 1):
    SWITCHES[f"Spine-{i}"] = f"{MGMT_BASE_IP}.{counter}"
    counter += 1

for i in range(1, NUM_LEAVES + 1):
    SWITCHES[leaf_node_name(i)] = f"{MGMT_BASE_IP}.{counter}"
    counter += 1


# ---------------------------------------------------------------------------
# GNS3 console endpoint discovery
# ---------------------------------------------------------------------------

def get_console_ports():
    """Fetch console host+port for each switch node from the GNS3 API."""
    session = requests.Session()

    auth = session.post(f"{GNS3_SERVER}/access/users/login", data={
        "username": GNS3_USER,
        "password": GNS3_PASSWORD,
    })
    if auth.status_code != 200:
        raise RuntimeError(f"GNS3 authentication failed: {auth.text}")
    token = auth.json().get("access_token")
    session.headers.update({"Authorization": f"Bearer {token}"})

    projects = session.get(f"{GNS3_SERVER}/projects").json()
    project  = next((p for p in projects if p["name"] == PROJECT_NAME), None)
    if not project:
        raise RuntimeError(f"Project '{PROJECT_NAME}' not found in GNS3")
    project_id = project["project_id"]

    computes_resp = session.get(f"{GNS3_SERVER}/computes")
    if computes_resp.status_code != 200:
        raise RuntimeError(f"Failed to get computes: {computes_resp.text}")
    computes      = computes_resp.json()
    compute_by_id   = {c.get("compute_id"): c for c in computes if c.get("compute_id")}
    compute_by_name = {c.get("name"): c for c in computes if c.get("name")}

    for compute_name in COMPUTE_SPINES + COMPUTE_LEAVES:
        if compute_name != "local" and compute_name not in compute_by_name:
            raise RuntimeError(
                f"Configured compute '{compute_name}' not found. "
                f"Available: {sorted(compute_by_name.keys())}"
            )

    nodes    = session.get(f"{GNS3_SERVER}/projects/{project_id}/nodes").json()
    api_host = GNS3_SERVER.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]

    console_endpoints = {}
    for node in nodes:
        if node["name"] not in SWITCHES:
            continue
        compute = compute_by_id.get(node.get("compute_id"), {})
        host = (
            compute.get("host")
            or compute.get("address")
            or compute.get("ip_address")
            or compute.get("ip")
            or ""
        ).strip()
        if host in ("", "0.0.0.0", "::", "localhost"):
            host = api_host
        console_endpoints[node["name"]] = {
            "host": host,
            "port": node["console"],
        }

    return console_endpoints


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def wait_for_telnet(host, port, retries=10, delay=15):
    for attempt in range(retries):
        try:
            sock = socket.create_connection((host, port), timeout=5)
            sock.close()
            return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            print(f"  Waiting for console {host}:{port} ... ({attempt + 1}/{retries})")
            time.sleep(delay)
    return False


def configure_switch(name, mgmt_ip, console_host, console_port):
    print(f"Connecting to {name} via telnet ({console_host}:{console_port})...")

    device = {
        "device_type": "arista_eos_telnet",
        "host":        console_host,
        "port":        console_port,
        "username":    SSH_USER,
        "password":    SSH_PASS,
        "timeout":     30,
    }

    try:
        with ConnectHandler(**device) as conn:
            conn.enable()
            commands = [
                f"hostname {name}",
                "zerotouch disable",
                "ip routing",
                "interface Management1",
                f"ip address {mgmt_ip}/24",
                "no shutdown",
                "exit",
                "management api http-commands",
                "no shutdown",
                "protocol https",
                "no shutdown",
                "exit",
                "management ssh",
                "no shutdown",
                "exit",
                f"username {SSH_USER} privilege 15 role network-admin secret {SSH_PASS}",
            ]
            conn.send_config_set(commands)
            conn.save_config()
            print(f" -> {name} configured successfully (mgmt: {mgmt_ip})")

    except NetMikoTimeoutException:
        print(f" -> {name}: connection timed out")
    except NetMikoAuthenticationException:
        print(f" -> {name}: authentication failed")
    except Exception as e:
        print(f" -> {name}: error — {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Switch name -> mgmt IP mapping:")
    for name, ip in SWITCHES.items():
        print(f"  {name:16s} {ip}")
    print()

    print("Fetching console endpoints from GNS3...\n")
    try:
        console_endpoints = get_console_ports()
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)

    for name, mgmt_ip in SWITCHES.items():
        endpoint = console_endpoints.get(name)
        if not endpoint:
            print(f" -> {name}: console endpoint not found in GNS3, skipping")
            continue

        host = endpoint["host"]
        port = endpoint["port"]
        if wait_for_telnet(host, port):
            configure_switch(name, mgmt_ip, host, port)
        else:
            print(f" -> {name}: console {host}:{port} unreachable after all retries, skipping")

    print("\nDone. eAPI is now available on all configured switches.")
    print("Access via: https://<switch-ip>/command-api")
