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
    FIREWALL_DMZ_EXPOSED_HOST,
    FIREWALL_DMZ_EXPOSED_PORTS,
    FIREWALL_DMZ_IFACE,
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
from sots.vlans import TENANTS, VLANS

# ---------------------------------------------------------------------------
# Design notes
# ---------------------------------------------------------------------------
# Physical wiring (set in sots/config.py):
#   vtnet0  → management (br0)
#   vtnet1  → WAN  (Server-1 / FRR upstream)
#   vtnet2  → Border-2 (FIREWALL_LAN_BORDER_LEAF_INDEX = 2)
#   vtnet3  → Border-1 (FIREWALL_DMZ_BORDER_LEAF_INDEX = 1)
#
# vtnet2 and vtnet3 carry 802.1Q-tagged frames from the border leaves.
# They MUST NOT have IP addresses themselves — they are pure trunk parents.
# The real routed interfaces are the VLAN subinterfaces:
#
#   vtnet2.110  10.31.0.6/30   ↔  Border-2  10.31.0.5   (VRF_PEDAGOGY)
#   vtnet2.130  10.31.10.6/30  ↔  Border-2  10.31.10.5  (VRF_DMZ)
#   vtnet3.110  10.31.0.2/30   ↔  Border-1  10.31.0.1   (VRF_PEDAGOGY)
#   vtnet3.130  10.31.10.2/30  ↔  Border-1  10.31.10.1  (VRF_DMZ)
#
# BGP neighbors on the firewall dial TO the leaf-side IPs:
#   10.31.0.1   AS 65100  (Border-1 PEDAGOGY)
#   10.31.0.5   AS 65101  (Border-2 PEDAGOGY)
#   10.31.10.1  AS 65100  (Border-1 DMZ)
#   10.31.10.5  AS 65101  (Border-2 DMZ)
#
# PF NAT must cover the tenant VM subnets (192.168.x.x/24), not the /30
# handoff links, because traffic forwarded from tenant VMs keeps its original
# source IP all the way to the firewall.
# ---------------------------------------------------------------------------


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


def shell_single_quote(value):
    return "'" + value.replace("'", "'\"'\"'") + "'"


def build_write_lines_cmds(lines, destination):
    cmds = [f": > {destination}"]
    for line in lines:
        cmds.append(f"printf '%s\\n' {shell_single_quote(line)} >> {destination}")
    return cmds


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


def with_default_prefix(peer_ip, default_prefix=30):
    """Add /prefix to a bare IP string if not already present."""
    if "/" in peer_ip:
        return peer_ip
    return f"{peer_ip}/{default_prefix}"


# ---------------------------------------------------------------------------
# Build VLAN subinterfaces on vtnet2 and vtnet3
#
# vtnet2 → Border-2: uses handoff_peer_ip_2 (firewall IP on the Border-2 link)
# vtnet3 → Border-1: uses handoff_peer_ip   (firewall IP on the Border-1 link)
#
# Each tenant with external_handoff gets one subinterface on each parent:
#   vtnet2.<vlan>  ip = handoff_peer_ip_2/30
#   vtnet3.<vlan>  ip = handoff_peer_ip/30
# ---------------------------------------------------------------------------
def build_vlan_subif_cmds(tenants, lan_iface, dmz_iface):
    """
    lan_iface (vtnet2) is wired to Border-2 → use handoff_peer_ip_2
    dmz_iface (vtnet3) is wired to Border-1 → use handoff_peer_ip
    """
    cmds = []
    for tenant in tenants:
        if not tenant.get("external_handoff"):
            continue
        vlan = tenant["handoff_vlan"]

        # vtnet2 side → Border-2
        peer_ip_2 = tenant.get("handoff_peer_ip_2", "")
        if peer_ip_2:
            subif = f"{lan_iface}.{vlan}"
            cmds += [
                f"/bin/sh -c 'ifconfig {subif} destroy >/dev/null 2>&1 || true'",
                f"ifconfig {subif} create vlandev {lan_iface} vlan {vlan}",
                f"ifconfig {subif} inet {with_default_prefix(peer_ip_2)} up",
            ]

        # vtnet3 side → Border-1
        peer_ip = tenant.get("handoff_peer_ip", "")
        if peer_ip:
            subif = f"{dmz_iface}.{vlan}"
            cmds += [
                f"/bin/sh -c 'ifconfig {subif} destroy >/dev/null 2>&1 || true'",
                f"ifconfig {subif} create vlandev {dmz_iface} vlan {vlan}",
                f"ifconfig {subif} inet {with_default_prefix(peer_ip)} up",
            ]
    return cmds


# ---------------------------------------------------------------------------
# Build PF rules
#
# NAT covers the tenant VM subnets (192.168.x.x/24) from VLANS, because
# traffic from tenant VMs keeps the original VM source IP all the way to the
# firewall — the /30 handoff link IPs are only used for BGP peering, not as
# source IPs of tenant data traffic.
#
# Pass rules allow traffic arriving on the LAN/DMZ parent interfaces
# (which includes all their subinterfaces on FreeBSD).
# ---------------------------------------------------------------------------
def build_pf_rules():
    # Collect tenant VM subnets from VLANS for NAT
    tenant_nets = sorted(
        {
            str(ipaddress.ip_interface(v["anycast_ip"]).network)
            for v in VLANS
            if v.get("anycast_ip")
        }
    )
    nat_sources = "{" + ",".join(tenant_nets) + "}"

    ports = ",".join(str(p) for p in FIREWALL_DMZ_EXPOSED_PORTS)

    return [
        "set skip on lo0",
        # NAT: tenant VM subnets going out to WAN
        f"nat on {FIREWALL_WAN_IFACE} inet from {nat_sources} to any -> ({FIREWALL_WAN_IFACE})",
        # DNAT: inbound from WAN to the DMZ exposed host
        (
            f"rdr on {FIREWALL_WAN_IFACE} inet proto tcp from any to ({FIREWALL_WAN_IFACE}) "
            f"port {{{ports}}} -> {FIREWALL_DMZ_EXPOSED_HOST}"
        ),
        # Allow ICMP everywhere (ping, traceroute)
        "pass in quick inet proto icmp",
        # Allow all traffic entering from LAN side (vtnet2 + all its subinterfaces)
        f"pass in quick on {FIREWALL_LAN_IFACE} inet all keep state",
        # Allow all traffic entering from DMZ side (vtnet3 + all its subinterfaces)
        f"pass in quick on {FIREWALL_DMZ_IFACE} inet all keep state",
        # Allow inbound from WAN to the DMZ exposed host on specified ports
        (
            f"pass in quick on {FIREWALL_WAN_IFACE} inet proto tcp from any "
            f"to {FIREWALL_DMZ_EXPOSED_HOST} port {{{ports}}} keep state"
        ),
        # Allow all outbound traffic
        "pass out quick inet all keep state",
    ]


# ---------------------------------------------------------------------------
# Build FRR BGP config
#
# Neighbors are the LEAF-side IPs (handoff_local_ip / handoff_local_ip_2).
# The firewall dials outward TO those IPs.
# The remote-as values come from LEAF_AS_BASE + (leaf_index - 1).
# ---------------------------------------------------------------------------
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


def build_clear_existing_bgp_cmd():
    """Remove any stale BGP instance (e.g. ASN 65551 baked into the OPNsense template)."""
    return (
        "/bin/sh -c 'if command -v vtysh >/dev/null 2>&1; then "
        'CUR_ASN=$(vtysh -c "show running-config" 2>/dev/null | awk "/^router bgp /{print \\$3; exit}"); '
        'if [ -n "$CUR_ASN" ] && [ "$CUR_ASN" != "'
        + str(FIREWALL_BGP_ASN)
        + '" ]; then '
        'echo "Removing stale BGP ASN $CUR_ASN"; '
        'vtysh -c "configure terminal" -c "no router bgp $CUR_ASN" -c "end" -c "write memory"; '
        "fi; fi'"
    )


def parse_pair(pair):
    if isinstance(pair, str):
        parts = [p.strip() for p in pair.split(",") if p.strip()]
    else:
        parts = list(pair)
    return int(parts[0]), int(parts[1])


def build_neighbors_from_tenants():
    """
    Build BGP neighbor list from tenant handoff definitions.

    The firewall peers TO the border leaf IPs:
      handoff_local_ip   = Border-1 leaf IP  (e.g. 10.31.0.1/30)
      handoff_local_ip_2 = Border-2 leaf IP  (e.g. 10.31.0.5/30)

    remote-as is derived from LEAF_AS_BASE + (border_leaf_index - 1).
    """
    if not MLAG_PAIRS:
        return []

    border_left_idx, border_right_idx = parse_pair(MLAG_PAIRS[0])
    border_left_asn = LEAF_AS_BASE + (border_left_idx - 1)  # 65100
    border_right_asn = LEAF_AS_BASE + (border_right_idx - 1)  # 65101

    neighbors = []
    for tenant in TENANTS:
        if not tenant.get("external_handoff"):
            continue

        # Border-1 side: handoff_local_ip is the leaf's IP on the /30 link
        left_ip = tenant.get("handoff_local_ip", "").split("/")[0]
        if left_ip:
            neighbors.append(
                {
                    "ip": left_ip,
                    "remote_as": border_left_asn,
                    "description": f"{tenant['name']}-Border-{border_left_idx}",
                }
            )

        # Border-2 side: handoff_local_ip_2 is the leaf's IP on the /30 link
        right_ip = tenant.get("handoff_local_ip_2", "").split("/")[0]
        if right_ip:
            neighbors.append(
                {
                    "ip": right_ip,
                    "remote_as": border_right_asn,
                    "description": f"{tenant['name']}-Border-{border_right_idx}",
                }
            )
    return neighbors


def find_offlink_neighbors(neighbors):
    """
    Warn about BGP neighbors not reachable via any locally configured network.
    Checks both the firewall's subinterface IPs (from tenant handoff_peer_ip*)
    so /30 peer subnets are included in the check.
    """
    connected_networks = []
    for tenant in TENANTS:
        if not tenant.get("external_handoff"):
            continue
        for key in ("handoff_peer_ip", "handoff_peer_ip_2"):
            pip = tenant.get(key)
            if pip:
                connected_networks.append(
                    ipaddress.ip_interface(with_default_prefix(pip)).network
                )

    offlink = []
    for neighbor in neighbors:
        peer_ip = ipaddress.ip_address(neighbor["ip"])
        if not any(peer_ip in net for net in connected_networks):
            offlink.append(neighbor["ip"])
    return offlink


def configure_firewall():
    mgmt_ip = f"{MGMT_BASE_IP}.{MGMT_START + NUM_SPINES + NUM_LEAVES + NUM_SERVERS}"
    mgmt_cidr = f"{mgmt_ip}/24"

    # ------------------------------------------------------------------
    # Shell commands sent to OPNsense
    # ------------------------------------------------------------------
    shell_cmds = [
        # Management interface
        f"ifconfig {FIREWALL_MGMT_IFACE} inet {mgmt_cidr} up",
        # WAN interface (toward Server-1 / FRR upstream)
        f"ifconfig {FIREWALL_WAN_IFACE} inet {FIREWALL_WAN_CIDR} up",
        # LAN/DMZ parent interfaces — NO IP, they are 802.1Q trunk parents only.
        # IPs live on the VLAN subinterfaces below.
        f"ifconfig {FIREWALL_LAN_IFACE} up",
        f"ifconfig {FIREWALL_DMZ_IFACE} up",
        # Default route via WAN gateway
        f"route -n add default {FIREWALL_WAN_GATEWAY} || route -n change default {FIREWALL_WAN_GATEWAY}",
        # Enable IP forwarding
        "sysctl net.inet.ip.forwarding=1",
        # Enable and start SSH
        "sysrc sshd_enable=YES",
        '/bin/sh -c \'if ! sockstat -4l | grep -q ":22"; then'
        " SSHD_BIN=$(command -v sshd);"
        ' if [ -n "$SSHD_BIN" ]; then "$SSHD_BIN"; else echo "sshd binary not found"; fi;'
        " fi'",
    ]

    # Create VLAN subinterfaces (vtnet2.<vlan> and vtnet3.<vlan>)
    # vtnet2 → Border-2: firewall IP = handoff_peer_ip_2
    # vtnet3 → Border-1: firewall IP = handoff_peer_ip
    vlan_subif_cmds = build_vlan_subif_cmds(
        TENANTS, FIREWALL_LAN_IFACE, FIREWALL_DMZ_IFACE
    )
    shell_cmds.extend(vlan_subif_cmds)

    # PF rules
    rules = build_pf_rules()
    pf_rules_write_cmds = build_write_lines_cmds(rules, "/tmp/dmz_rules.conf")
    shell_cmds.extend(pf_rules_write_cmds)
    shell_cmds.extend(["pfctl -f /tmp/dmz_rules.conf", "pfctl -e"])

    # BGP config via FRR vtysh
    resolved_neighbors = FIREWALL_BGP_NEIGHBORS or build_neighbors_from_tenants()
    if resolved_neighbors:
        offlink_neighbors = find_offlink_neighbors(resolved_neighbors)
        if offlink_neighbors:
            print(
                "Warning: these BGP neighbors are not reachable via any subinterface subnet: "
                + ", ".join(offlink_neighbors)
            )

        bgp_config = build_frr_bgp_config(resolved_neighbors)
        bgp_config_write_cmds = build_write_lines_cmds(
            bgp_config.splitlines(), "/tmp/opnsense_bgp.conf"
        )
        shell_cmds.append(build_clear_existing_bgp_cmd())
        shell_cmds.extend(bgp_config_write_cmds)
        shell_cmds.append(
            "/bin/sh -c 'if command -v vtysh >/dev/null 2>&1;"
            ' then vtysh -f /tmp/opnsense_bgp.conf; vtysh -c "write memory";'
            ' else echo "vtysh not found: install/enable OPNsense FRR plugin for BGP"; fi\''
        )

    # ------------------------------------------------------------------
    # Delivery: telnet console first, SSH fallback
    # ------------------------------------------------------------------
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
