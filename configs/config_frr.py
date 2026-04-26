#!/usr/bin/env python3
"""
config_frr.py — Configure the FRR (Debian) internet simulator via GNS3 telnet.

Architecture change from the original
───────────────────────────────────────────────────────────────────────────────
Previously FRR peered directly with the border leaf over VLAN subinterfaces.
With the DMZ in place the topology is now:

  FRR  ←→  FortiGate (WAN)  ←→  FortiGate (LAN)  ←→  Border leaves

FRR no longer talks to the border leaves at all.  It only peers with the
FortiGate WAN port(s).  Because the FortiGate WAN port is a plain L3
interface (no 802.1Q), FRR also uses plain physical interfaces — no VLAN
subinterfaces.

Interface mapping (GNS3 adapter → Linux interface)
───────────────────────────────────────────────────
  adapter 0  →  enp2s0  management
  adapter 1  →  ens1    FW-1 WAN  (always present)
  adapter 2  →  ens2    FW-2 WAN  (dual-FW mode only)

FRR advertises INTERNET_PREFIX (8.8.8.8/32) and assigns INTERNET_IP to lo
so the prefix is actually pingable — validating end-to-end reachability.
"""

from netmiko import ConnectHandler
import requests
import time
import socket
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import sots.config as cfg

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FRR_NODE_NAME = "Server-1"

# Management interface inside the Debian VM.
MGMT_IFACE = "enp2s0"

# Data interfaces — plain physical (no VLAN tags, matching FortiGate WAN).
FRR_IFACE_1 = "ens1"   # always: faces FW-1 WAN
FRR_IFACE_2 = "ens2"   # dual-FW only: faces FW-2 WAN

# The prefix FRR originates into BGP and assigns to lo (makes it pingable).
INTERNET_PREFIX = "8.8.8.8/32"
INTERNET_IP     = "8.8.8.8"

# FRR's own BGP ASN — must match FRR_ASN in config_firewalls.py.
FRR_ASN = 65999

# ---------------------------------------------------------------------------
# Derived addressing from config.py
# ---------------------------------------------------------------------------

def _ip(cidr: str) -> str:
    return cidr.split("/")[0]

def _prefix_len(cidr: str) -> str:
    return cidr.split("/")[1]


# FRR's own IP + prefix length on each WAN link.
FRR_IP1_CIDR = f"{cfg.DMZ_FRR_IP1}/{_prefix_len(cfg.DMZ_FW1_WAN_IP)}"
FRR_IP2_CIDR = f"{cfg.DMZ_FRR_IP2}/{_prefix_len(cfg.DMZ_FW2_WAN_IP)}"

# Firewall WAN IPs that FRR peers with.
FW1_WAN_IP   = _ip(cfg.DMZ_FW1_WAN_IP)
FW2_WAN_IP   = _ip(cfg.DMZ_FW2_WAN_IP)

# FortiGate ASN (FRR's remote-as for both FW peers).
FW_ASN = cfg.DMZ_FW_ASN


# ---------------------------------------------------------------------------
# GNS3 console discovery
# ---------------------------------------------------------------------------

def get_gns3_console(node_name: str):
    session = requests.Session()

    resp = session.post(f"{cfg.GNS3_SERVER}/access/users/login", data={
        "username": cfg.GNS3_USER,
        "password": cfg.GNS3_PASSWORD,
    })
    if resp.status_code != 200:
        raise RuntimeError(f"GNS3 auth failed: {resp.text}")
    token = (resp.json().get("access_token") or resp.json().get("token"))
    if token:
        session.headers.update({"Authorization": f"Bearer {token}"})

    api_host = cfg.GNS3_SERVER.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]

    projects = session.get(f"{cfg.GNS3_SERVER}/projects").json()
    project  = next((p for p in projects if p["name"] == cfg.PROJECT_NAME), None)
    if not project:
        raise RuntimeError(f"Project '{cfg.PROJECT_NAME}' not found.")
    project_id = project["project_id"]

    computes      = session.get(f"{cfg.GNS3_SERVER}/computes").json()
    compute_by_id = {c.get("compute_id"): c for c in computes if c.get("compute_id")}

    nodes = session.get(f"{cfg.GNS3_SERVER}/projects/{project_id}/nodes").json()
    for node in nodes:
        if node["name"] == node_name:
            compute = compute_by_id.get(node.get("compute_id"), {})
            host = (
                compute.get("host") or compute.get("address")
                or compute.get("ip_address") or compute.get("ip") or ""
            ).strip()
            if host in ("", "0.0.0.0", "::", "localhost", "127.0.0.1"):
                host = api_host
            return host, node["console"]

    raise RuntimeError(f"Node '{node_name}' not found in project.")


def wait_for_telnet(host, port, retries=10, delay=10):
    for attempt in range(retries):
        try:
            with socket.create_connection((host, port), timeout=3):
                return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            print(f"  Waiting for telnet {host}:{port} ({attempt+1}/{retries})...")
            time.sleep(delay)
    return False


# ---------------------------------------------------------------------------
# Main configuration
# ---------------------------------------------------------------------------

def configure_frr():
    try:
        host, port = get_gns3_console(FRR_NODE_NAME)
        print(f"Found {FRR_NODE_NAME} console at telnet://{host}:{port}")
    except Exception as e:
        print(f"Error fetching GNS3 details: {e}")
        return

    if not wait_for_telnet(host, port):
        print(f"Telnet {host}:{port} unreachable.")
        return

    print(f"Connecting to {FRR_NODE_NAME} console...")
    try:
        conn = ConnectHandler(**{
            "device_type":         "generic_termserver_telnet",
            "host":                host,
            "port":                port,
            "global_delay_factor": 2,
        })
    except Exception as e:
        print(f"Connection failed: {e}")
        return

    # Wake console and log in.
    conn.write_channel("\n\n")
    time.sleep(2)
    out = conn.read_channel()

    if "login:" in out.lower():
        conn.write_channel("root\n")
        time.sleep(1)
        out = conn.read_channel()

    if "password:" in out.lower():
        conn.write_channel("root\n")
        time.sleep(2)

    # ──────────────────────────────────────────────────────────────────
    # 1. Linux interface configuration
    # ──────────────────────────────────────────────────────────────────
    mgmt_ip = (
        f"{cfg.MGMT_BASE_IP}."
        f"{cfg.MGMT_START + cfg.NUM_SPINES + cfg.NUM_LEAVES}"
    )

    linux_cmds = [
        # Management
        f"ip addr add {mgmt_ip}/24 dev {MGMT_IFACE} 2>/dev/null || true",
        "systemctl enable ssh",
        "systemctl start ssh",

        # Assign 8.8.8.8 to lo so pings arriving here are answered.
        # "ip addr add" owns the address; "ip route add ... dev lo" alone
        # does NOT — incoming packets would be silently dropped.
        f"ip addr add {INTERNET_IP}/32 dev lo 2>/dev/null || true",

        # WAN interface toward FW-1
        f"ip link set {FRR_IFACE_1} up",
        f"ip addr add {FRR_IP1_CIDR} dev {FRR_IFACE_1} 2>/dev/null || true",
    ]

    if cfg.BORDER_FIREWALL_COUNT == 2:
        # Second WAN interface toward FW-2
        linux_cmds += [
            f"ip link set {FRR_IFACE_2} up",
            f"ip addr add {FRR_IP2_CIDR} dev {FRR_IFACE_2} 2>/dev/null || true",
        ]

    # ──────────────────────────────────────────────────────────────────
    # 2. FRR BGP configuration (vtysh -f)
    #
    # FRR peers with the FortiGate WAN port(s).
    # It does NOT peer with border leaves directly.
    #
    # "no bgp ebgp-requires-policy" disables the strict inbound/outbound
    # route-map requirement that would otherwise block all advertisements.
    # ──────────────────────────────────────────────────────────────────

    neighbor_lines  = []
    activate_lines  = []

    # FW-1 WAN peer (always present)
    neighbor_lines += [
        f" neighbor {FW1_WAN_IP} remote-as {FW_ASN}",
        f" neighbor {FW1_WAN_IP} description FW-1-WAN",
    ]
    activate_lines.append(f"  neighbor {FW1_WAN_IP} activate")

    if cfg.BORDER_FIREWALL_COUNT == 2:
        # FW-2 WAN peer
        neighbor_lines += [
            f" neighbor {FW2_WAN_IP} remote-as {FW_ASN}",
            f" neighbor {FW2_WAN_IP} description FW-2-WAN",
        ]
        activate_lines.append(f"  neighbor {FW2_WAN_IP} activate")

    vtysh_lines = (
        [
            f"router bgp {FRR_ASN}",
            f" no bgp ebgp-requires-policy",
        ]
        + neighbor_lines
        + ["  address-family ipv4 unicast"]
        + activate_lines
        + [
            # FRR originates 8.8.8.8/32 — only announced when the address
            # exists locally (guaranteed by "ip addr add" to lo above).
            f"  network {INTERNET_PREFIX}",
            " exit-address-family",
        ]
    )

    frr_config_text = "\n".join(vtysh_lines)

    # ──────────────────────────────────────────────────────────────────
    # 3. Apply Linux commands
    # ──────────────────────────────────────────────────────────────────
    print("\n--- Applying Linux interface config ---")
    for cmd in linux_cmds:
        conn.write_channel(cmd + "\n")
        time.sleep(0.5)
        print(f"  {cmd}")

    # ──────────────────────────────────────────────────────────────────
    # 4. Write and apply FRR config via vtysh -f
    # ──────────────────────────────────────────────────────────────────
    print("\n--- Applying FRR BGP config via vtysh -f ---")
    bash_script = (
        "cat << 'FRREOF' > /tmp/frr_bgp.conf\n"
        + frr_config_text
        + "\nFRREOF\n"
        "vtysh -f /tmp/frr_bgp.conf\n"
        "vtysh -c 'write memory'\n"
    )
    for line in bash_script.split("\n"):
        conn.write_channel(line + "\n")
        time.sleep(0.15)

    time.sleep(3)
    out = conn.read_channel()
    if out.strip():
        print(out)

    conn.disconnect()

    print("\n✅ FRR configured successfully!")
    print(f"\nFRR BGP peers:")
    print(f"  {FW1_WAN_IP}  (FW-1 WAN)  remote-as {FW_ASN}")
    if cfg.BORDER_FIREWALL_COUNT == 2:
        print(f"  {FW2_WAN_IP}  (FW-2 WAN)  remote-as {FW_ASN}")
    print(f"\nAdvertised prefix: {INTERNET_PREFIX}")
    print(f"Pingable target:   {INTERNET_IP}  (assigned to lo)")
    print(f"\nFRR interface IPs:")
    print(f"  {FRR_IFACE_1}  {FRR_IP1_CIDR}")
    if cfg.BORDER_FIREWALL_COUNT == 2:
        print(f"  {FRR_IFACE_2}  {FRR_IP2_CIDR}")


if __name__ == "__main__":
    if cfg.BORDER_FIREWALL_COUNT == 0:
        print("BORDER_FIREWALL_COUNT is 0 — no DMZ, FRR connects directly to border leaf.")
        print("Use the legacy config_frr.py for that mode.")
        sys.exit(1)

    configure_frr()
