from netmiko import ConnectHandler
import ipaddress
import os
import requests
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sots.config import (
    COMPUTE_FIREWALLS,
    FIREWALL_BGP_ASN,
    FIREWALL_BGP_NEIGHBORS,
    FIREWALL_CONSOLE_PASS,
    FIREWALL_CONSOLE_USER,
    FIREWALL_DMZ_CIDR,
    FIREWALL_DMZ_EXPOSED_HOST,
    FIREWALL_DMZ_EXPOSED_PORTS,
    FIREWALL_DMZ_IFACE,
    FIREWALL_LAN_CIDR,
    FIREWALL_LAN_IFACE,
    FIREWALL_MGMT_IFACE,
    FIREWALL_NODE_NAME,
    FIREWALL_WAN_CIDR,
    FIREWALL_WAN_GATEWAY,
    FIREWALL_WAN_IFACE,
    GNS3_PASSWORD,
    GNS3_SERVER,
    GNS3_USER,
    LEAF_AS_BASE,
    MLAG_PAIRS,
    MGMT_BASE_IP,
    MGMT_START,
    NUM_LEAVES,
    NUM_SERVERS,
    NUM_SPINES,
    PROJECT_NAME,
)
from sots.vlans import TENANTS


def get_console_endpoint(node_name):
    session = requests.Session()
    auth = session.post(
        f"{GNS3_SERVER}/access/users/login",
        data={"username": GNS3_USER, "password": GNS3_PASSWORD},
    )
    if auth.status_code != 200:
        raise RuntimeError(f"GNS3 authentication failed: {auth.text}")

    token = auth.json().get("access_token")
    session.headers.update({"Authorization": f"Bearer {token}"})

    projects = session.get(f"{GNS3_SERVER}/projects").json()
    project = next((p for p in projects if p["name"] == PROJECT_NAME), None)
    if not project:
        raise RuntimeError(f"Project '{PROJECT_NAME}' not found")
    project_id = project["project_id"]

    computes = session.get(f"{GNS3_SERVER}/computes").json()
    compute_by_id = {c.get("compute_id"): c for c in computes if c.get("compute_id")}
    api_host = GNS3_SERVER.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]

    nodes = session.get(f"{GNS3_SERVER}/projects/{project_id}/nodes").json()
    node = next((n for n in nodes if n["name"] == node_name), None)
    if not node:
        raise RuntimeError(f"Node '{node_name}' not found in project")

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

    return host, node["console"]


def wait_for_telnet(host, port, retries=12, delay=10):
    for attempt in range(retries):
        try:
            with socket.create_connection((host, port), timeout=5):
                return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            print(
                f"  Waiting for firewall console {host}:{port} ... ({attempt + 1}/{retries})"
            )
            time.sleep(delay)
    return False


def wait_for_ssh(host, port=22, retries=10, delay=3):
    for attempt in range(retries):
        try:
            with socket.create_connection((host, port), timeout=5):
                return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            print(f"  Waiting for SSH {host}:{port} ... ({attempt + 1}/{retries})")
            time.sleep(delay)
    return False


def send_lines(conn, lines, delay=0.5):
    for line in lines:
        conn.write_channel(line + "\n")
        time.sleep(delay)


def apply_over_ssh(mgmt_ip, shell_cmds):
    conn = ConnectHandler(
        device_type="linux",
        host=mgmt_ip,
        username=FIREWALL_CONSOLE_USER,
        password=FIREWALL_CONSOLE_PASS,
        global_delay_factor=2,
    )
    send_lines(conn, shell_cmds, delay=0.4)
    time.sleep(2)
    output = conn.read_channel().strip()
    if output:
        print(output)
    conn.disconnect()


def build_pf_rules():
    lan_net = str(ipaddress.ip_interface(FIREWALL_LAN_CIDR).network)
    dmz_net = str(ipaddress.ip_interface(FIREWALL_DMZ_CIDR).network)
    ports = ",".join(str(p) for p in FIREWALL_DMZ_EXPOSED_PORTS)

    return [
        "set skip on lo0",
        f"nat on {FIREWALL_WAN_IFACE} inet from {{{lan_net},{dmz_net}}} to any -> ({FIREWALL_WAN_IFACE})",
        (
            f"rdr on {FIREWALL_WAN_IFACE} inet proto tcp from any to ({FIREWALL_WAN_IFACE}) "
            f"port {{{ports}}} -> {FIREWALL_DMZ_EXPOSED_HOST}"
        ),
        "pass in quick inet proto icmp",
        f"pass in quick on {FIREWALL_LAN_IFACE} inet from {lan_net} to any keep state",
        f"pass in quick on {FIREWALL_DMZ_IFACE} inet from {dmz_net} to any keep state",
        (
            f"pass in quick on {FIREWALL_WAN_IFACE} inet proto tcp from any "
            f"to {FIREWALL_DMZ_EXPOSED_HOST} port {{{ports}}} keep state"
        ),
        "pass out quick inet all keep state",
    ]


def build_frr_bgp_config(neighbors):
    lines = [
        f"router bgp {FIREWALL_BGP_ASN}",
        " no bgp ebgp-requires-policy",
    ]
    activate_lines = []
    for neighbor in neighbors:
        peer_ip = neighbor["ip"]
        remote_as = neighbor["remote_as"]
        description = neighbor.get("description", f"BORDER-{peer_ip}")
        lines.extend(
            [
                f" neighbor {peer_ip} remote-as {remote_as}",
                f" neighbor {peer_ip} description {description}",
            ]
        )
        activate_lines.append(f"  neighbor {peer_ip} activate")

    lines.append(" address-family ipv4 unicast")
    lines.extend(activate_lines)
    lines.append(" exit-address-family")
    return "\n".join(lines)


def parse_pair(pair):
    if isinstance(pair, str):
        parts = [p.strip() for p in pair.split(",") if p.strip()]
    else:
        parts = list(pair)
    return int(parts[0]), int(parts[1])


def build_neighbors_from_tenants():
    if not MLAG_PAIRS:
        return []

    border_left_idx, _ = parse_pair(MLAG_PAIRS[0])
    border_asn = LEAF_AS_BASE + (border_left_idx - 1)

    neighbors = []
    for tenant in TENANTS:
        if not tenant.get("external_handoff"):
            continue

        left_ip = tenant.get("handoff_local_ip", "").split("/")[0]
        if left_ip:
            neighbors.append(
                {
                    "ip": left_ip,
                    "remote_as": border_asn,
                    "description": f"{tenant['name']}-Border-1",
                }
            )

        right_ip = tenant.get("handoff_local_ip_2", "").split("/")[0]
        if right_ip:
            neighbors.append(
                {
                    "ip": right_ip,
                    "remote_as": border_asn,
                    "description": f"{tenant['name']}-Border-2",
                }
            )
    return neighbors


def configure_firewall():
    mgmt_ip = f"{MGMT_BASE_IP}.{MGMT_START + NUM_SPINES + NUM_LEAVES + NUM_SERVERS}"
    mgmt_cidr = f"{mgmt_ip}/24"

    rules = build_pf_rules()
    shell_cmds = [
        f"ifconfig {FIREWALL_MGMT_IFACE} inet {mgmt_cidr} up",
        f"ifconfig {FIREWALL_WAN_IFACE} inet {FIREWALL_WAN_CIDR} up",
        f"ifconfig {FIREWALL_LAN_IFACE} inet {FIREWALL_LAN_CIDR} up",
        f"ifconfig {FIREWALL_DMZ_IFACE} inet {FIREWALL_DMZ_CIDR} up",
        f"route -n add default {FIREWALL_WAN_GATEWAY} || route -n change default {FIREWALL_WAN_GATEWAY}",
        "sysctl net.inet.ip.forwarding=1",
        "cat > /tmp/dmz_rules.conf <<'PFEOF'",
        *rules,
        "PFEOF",
        "pfctl -f /tmp/dmz_rules.conf",
        "pfctl -e",
    ]

    resolved_neighbors = FIREWALL_BGP_NEIGHBORS or build_neighbors_from_tenants()
    if resolved_neighbors:
        bgp_config = build_frr_bgp_config(resolved_neighbors)
        shell_cmds.extend(
            [
                "if command -v vtysh >/dev/null 2>&1; then",
                "cat > /tmp/opnsense_bgp.conf <<'BGPEOF'",
                *bgp_config.splitlines(),
                "BGPEOF",
                "vtysh -f /tmp/opnsense_bgp.conf",
                'vtysh -c "write memory"',
                "else",
                "echo 'vtysh not found: install/enable OPNsense FRR plugin for BGP'",
                "fi",
            ]
        )

    configured = False
    try:
        host, port = get_console_endpoint(FIREWALL_NODE_NAME)
        print(f"Connecting to {FIREWALL_NODE_NAME} via telnet ({host}:{port})...")
        if wait_for_telnet(host, port):
            try:
                conn = ConnectHandler(
                    device_type="generic_termserver_telnet",
                    host=host,
                    port=port,
                    global_delay_factor=2,
                )
                conn.write_channel("\n\n")
                time.sleep(2)
                output = conn.read_channel()

                if "login:" in output.lower():
                    send_lines(
                        conn, [FIREWALL_CONSOLE_USER, FIREWALL_CONSOLE_PASS], delay=1
                    )
                    time.sleep(2)
                    output = conn.read_channel()

                if "enter an option" in output.lower() or "opnsense" in output.lower():
                    conn.write_channel("8\n")
                    time.sleep(2)
                    conn.read_channel()

                print("Applying OPNsense interface and DMZ policy configuration...")
                send_lines(conn, shell_cmds, delay=0.4)
                time.sleep(2)
                final_output = conn.read_channel().strip()
                if final_output:
                    print(final_output)
                conn.disconnect()
                configured = True
            except Exception as exc:
                print(f"Telnet configuration failed: {exc}")
        else:
            print("Firewall console is not reachable.")
    except Exception as exc:
        print(f"Error discovering firewall console: {exc}")

    if not configured:
        print(f"Trying SSH fallback to firewall management IP ({mgmt_ip}:22)...")
        if not wait_for_ssh(mgmt_ip):
            print("Firewall SSH is not reachable.")
            return
        try:
            print("Applying OPNsense interface and DMZ policy configuration via SSH...")
            apply_over_ssh(mgmt_ip, shell_cmds)
            configured = True
        except Exception as exc:
            print(f"SSH fallback failed: {exc}")
            return

    print("Firewall DMZ configuration applied.")

    try:
        print(f"\nVerifying firewall is reachable via SSH ({mgmt_ip}:22)...")
        for attempt in range(5):
            try:
                with socket.create_connection((mgmt_ip, 22), timeout=3):
                    print("✓ SSH is now reachable on firewall")
                    break
            except (socket.timeout, ConnectionRefusedError, OSError):
                if attempt < 4:
                    print(f"  SSH not ready, retrying... ({attempt + 1}/5)")
                    time.sleep(3)
        else:
            print("  ⚠ SSH verification timed out (may still be configuring)")
    except Exception as exc:
        print(f"  ⚠ SSH verification failed: {exc}")


if __name__ == "__main__":
    if len(COMPUTE_FIREWALLS) == 0:
        print("COMPUTE_FIREWALLS is empty in sots/config.py.")
        sys.exit(1)
    configure_firewall()
