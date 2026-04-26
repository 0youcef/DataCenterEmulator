#!/usr/bin/env python3
"""
config_firewalls.py — Configure FortiGate VM firewall(s) via GNS3 telnet console.

KEY DESIGN POINT — 802.1Q tagging
───────────────────────────────────────────────────────────────────────────────
Border leaves send 802.1Q-tagged frames (VLAN 99) on the wire toward the FW.
FortiGate LAN ports must be configured as VLAN subinterfaces (port2.99 etc.)
to strip the tag. The WAN port toward FRR is plain L3 (no tagging).

POLICY COUNT — EVAL LICENSE (max 3 policies)
───────────────────────────────────────────────────────────────────────────────
Zones group multiple interfaces behind a single name. A zone-based policy
covers all member interfaces simultaneously, so we always need exactly 2:
  LAN_ZONE → WAN_ZONE  (outbound, NAT enabled)
  WAN_ZONE → LAN_ZONE  (inbound/BGP return, NAT disabled)

The Intra-LAN policy is NOT needed: MLAG peers communicate directly over
their peer-link, not through the firewall.

GNS3 adapter → FortiGate port mapping  (FW_PORT_OFFSET = 1)
───────────────────────────────────────
  adapter 0 → port1   management (MGMT-Switch)
  adapter 1 → port2   first data port
  adapter 2 → port3   second data port
  adapter 3 → port4   third data port  (single-FW only)

Single FW (BORDER_FIREWALL_COUNT=1):
  FW-1 port2.99  LAN-1 ↔ Border-1  (DMZ_FW1_LAN_IP1)
  FW-1 port3.99  LAN-2 ↔ Border-2  (DMZ_FW1_LAN_IP2)
  FW-1 port4     WAN   ↔ FRR ens1

Dual FW (BORDER_FIREWALL_COUNT=2):
  FW-1 port2.99  LAN ↔ Border-1   FW-1 port3 WAN ↔ FRR ens1
  FW-2 port2.99  LAN ↔ Border-2   FW-2 port3 WAN ↔ FRR ens2
"""

from netmiko import ConnectHandler
import requests
import time
import socket
import sys
import os
from ipaddress import ip_interface

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import sots.config as cfg

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Adapter N → port(N + FW_PORT_OFFSET). Default: adapter 0 = port1.
FW_PORT_OFFSET = 1

FW_USERNAME = "admin"
FW_PASSWORD = "wtpns4C@wtpns4C@"   # adjust if yours differs

# FRR's BGP ASN — must match config_frr.py.
FRR_ASN = 65999

CMD_DELAY = 0.5   # seconds between console commands


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def data_port(adapter_index: int) -> str:
    """GNS3 data-plane adapter N (1-based) → FortiGate port name."""
    return f"port{adapter_index + FW_PORT_OFFSET}"


def vlan_port(adapter_index: int, vlan_id: int) -> str:
    return f"{data_port(adapter_index)}.{vlan_id}"


def _prefix_len(cidr: str) -> str:
    return cidr.split("/")[1]


def _netmask(cidr: str) -> str:
    return str(ip_interface(cidr).netmask)


def _ip(cidr: str) -> str:
    return str(ip_interface(cidr).ip)


# ---------------------------------------------------------------------------
# Per-firewall configuration plans
# ---------------------------------------------------------------------------
#
# Interface entry:  (logical_name, cidr, description, parent_port|None, vlan_id|None)
#   parent=None, vlan_id=None → plain L3 interface (WAN side, no 802.1Q)
#   parent=str,  vlan_id=int  → VLAN subinterface  (LAN side, strips 802.1Q tag)
#
# zones:            {"ZONE_NAME": [iface, ...], ...}
#   Zones group interfaces so 2 policies cover all directions.
#
# fw_policy_pairs:  [(src_zone, dst_zone, nat_enable, policy_name), ...]
#   Always exactly 2 entries — within the 3-policy eval license limit.

def build_fw_plans():
    def _parse_pair(pair):
        if isinstance(pair, str):
            parts = [p.strip() for p in pair.split(",") if p.strip()]
        else:
            parts = list(pair)
        return int(parts[0]), int(parts[1])

    border_left_idx, border_right_idx = _parse_pair(cfg.MLAG_PAIRS[0])
    left_asn  = cfg.LEAF_AS_BASE + (border_left_idx  - 1)
    right_asn = cfg.LEAF_AS_BASE + (border_right_idx - 1)

    border1_ip = _ip(cfg.DMZ_LEAF1_LOCAL_IP)
    border2_ip = _ip(cfg.DMZ_LEAF2_LOCAL_IP)
    vlan       = cfg.DMZ_HANDOFF_VLAN

    if cfg.BORDER_FIREWALL_COUNT == 1:
        lan1_cidr  = f"{cfg.DMZ_FW1_LAN_IP1}/{_prefix_len(cfg.DMZ_LEAF1_LOCAL_IP)}"
        lan2_cidr  = f"{cfg.DMZ_FW1_LAN_IP2}/{_prefix_len(cfg.DMZ_LEAF2_LOCAL_IP)}"
        wan_cidr   = cfg.DMZ_FW1_WAN_IP
        lan1_iface = vlan_port(1, vlan)   # port2.99
        lan2_iface = vlan_port(2, vlan)   # port3.99
        wan_iface  = data_port(3)         # port4

        return [{
            "name":      "FW-1",
            "hostname":  "FW-1",
            "bgp_asn":   cfg.DMZ_FW_ASN,
            "router_id": cfg.DMZ_FW1_LAN_IP1,
            "interfaces": [
                (lan1_iface, lan1_cidr, "LAN-1 to Border-1", data_port(1), vlan),
                (lan2_iface, lan2_cidr, "LAN-2 to Border-2", data_port(2), vlan),
                (wan_iface,  wan_cidr,  "WAN to FRR",        None,         None),
            ],
            "bgp_neighbors": [
                (border1_ip,      left_asn,  "Border-1"),
                (border2_ip,      right_asn, "Border-2"),
                (cfg.DMZ_FRR_IP1, FRR_ASN,   "FRR"),
            ],
            # Zone-based policies: 2 total, within the 3-policy eval limit.
            # LAN_ZONE covers both LAN subinterfaces simultaneously.
            "zones": {
                "LAN_ZONE": [lan1_iface, lan2_iface],
                "WAN_ZONE": [wan_iface],
            },
            # (src_zone, dst_zone, nat_enable, policy_name)
            "fw_policy_pairs": [
                ("LAN_ZONE", "WAN_ZONE", True,  "LAN-to-WAN"),
                ("WAN_ZONE", "LAN_ZONE", False, "WAN-to-LAN"),
            ],
        }]

    elif cfg.BORDER_FIREWALL_COUNT == 2:
        fw1_lan_iface = vlan_port(1, vlan)   # port2.99
        fw1_wan_iface = data_port(2)          # port3
        fw2_lan_iface = vlan_port(1, vlan)   # port2.99
        fw2_wan_iface = data_port(2)          # port3

        return [
            {
                "name":      "FW-1",
                "hostname":  "FW-1",
                "bgp_asn":   cfg.DMZ_FW_ASN,
                "router_id": cfg.DMZ_FW1_LAN_IP1,
                "interfaces": [
                    (fw1_lan_iface, f"{cfg.DMZ_FW1_LAN_IP1}/{_prefix_len(cfg.DMZ_LEAF1_LOCAL_IP)}",
                     "LAN to Border-1", data_port(1), vlan),
                    (fw1_wan_iface, cfg.DMZ_FW1_WAN_IP, "WAN to FRR", None, None),
                ],
                "bgp_neighbors": [
                    (border1_ip,      left_asn, "Border-1"),
                    (cfg.DMZ_FRR_IP1, FRR_ASN,  "FRR"),
                ],
                "zones": {
                    "LAN_ZONE": [fw1_lan_iface],
                    "WAN_ZONE": [fw1_wan_iface],
                },
                "fw_policy_pairs": [
                    ("LAN_ZONE", "WAN_ZONE", True,  "LAN-to-WAN"),
                    ("WAN_ZONE", "LAN_ZONE", False, "WAN-to-LAN"),
                ],
            },
            {
                "name":      "FW-2",
                "hostname":  "FW-2",
                "bgp_asn":   cfg.DMZ_FW_ASN,
                "router_id": cfg.DMZ_FW2_LAN_IP1,
                "interfaces": [
                    (fw2_lan_iface, f"{cfg.DMZ_FW2_LAN_IP1}/{_prefix_len(cfg.DMZ_LEAF2_LOCAL_IP)}",
                     "LAN to Border-2", data_port(1), vlan),
                    (fw2_wan_iface, cfg.DMZ_FW2_WAN_IP, "WAN to FRR", None, None),
                ],
                "bgp_neighbors": [
                    (border2_ip,      right_asn, "Border-2"),
                    (cfg.DMZ_FRR_IP2, FRR_ASN,   "FRR"),
                ],
                "zones": {
                    "LAN_ZONE": [fw2_lan_iface],
                    "WAN_ZONE": [fw2_wan_iface],
                },
                "fw_policy_pairs": [
                    ("LAN_ZONE", "WAN_ZONE", True,  "LAN-to-WAN"),
                    ("WAN_ZONE", "LAN_ZONE", False, "WAN-to-LAN"),
                ],
            },
        ]
    else:
        return []


# ---------------------------------------------------------------------------
# GNS3 console discovery
# ---------------------------------------------------------------------------

def get_console_endpoint(node_name: str):
    session = requests.Session()
    resp = session.post(f"{cfg.GNS3_SERVER}/access/users/login", data={
        "username": cfg.GNS3_USER,
        "password": cfg.GNS3_PASSWORD,
    })
    if resp.status_code != 200:
        raise RuntimeError(f"GNS3 auth failed: {resp.text}")

    data  = resp.json()
    token = data.get("access_token") or data.get("token")
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
            print(f"  Waiting for {host}:{port} ({attempt+1}/{retries})...")
            time.sleep(delay)
    return False


# ---------------------------------------------------------------------------
# FortiGate console interaction
# ---------------------------------------------------------------------------

def _read(conn, wait=0.8):
    time.sleep(wait)
    return conn.read_channel()


def _send(conn, line, wait=CMD_DELAY):
    conn.write_channel(line + "\n")
    return _read(conn, wait)


def _login(conn):
    conn.write_channel("\n\n")
    out = _read(conn, 2)

    if "login:" in out.lower():
        out = _send(conn, FW_USERNAME, 1.5)

    if "password:" in out.lower():
        out = _send(conn, FW_PASSWORD, 1.5)

    for _ in range(6):
        if any(tok in out for tok in ("#", "$", ">", "FortiGate")):
            break
        out = _send(conn, "a", 1)

    if not any(tok in out for tok in ("#", "$", "FortiGate")):
        raise RuntimeError(f"Unexpected login output:\n{out}")
    print("  Logged in to FortiGate console.")


def configure_fortigate(plan: dict, console_host: str, console_port: int):
    name = plan["name"]
    print(f"\n{'='*62}")
    print(f"  Configuring {name}  ({console_host}:{console_port})")
    print(f"{'='*62}")

    device = {
        "device_type":         "generic_termserver_telnet",
        "host":                console_host,
        "port":                console_port,
        "global_delay_factor": 2,
    }

    try:
        conn = ConnectHandler(**device)
    except Exception as e:
        print(f"  ERROR: could not connect — {e}")
        return

    try:
        _login(conn)

        # ── 1. Hostname & global settings ─────────────────────────────
        print("  Setting hostname and global settings...")
        _send(conn, "config system global")
        _send(conn, f"    set hostname {plan['hostname']}")
        _send(conn, "end")

        _send(conn, "config system settings")
        _send(conn, "    set allow-subnet-overlap enable")
        _send(conn, "end")

        # ── 2. Interfaces ─────────────────────────────────────────────
        # Interface entry: (logical, cidr, desc, parent, vlan_id)
        #   parent=None → plain L3 (WAN, no 802.1Q)
        #   parent=str  → VLAN subinterface (LAN, strips 802.1Q tag from border leaf)
        print("  Configuring interfaces...")
        enabled_parents = set()

        for (logical, cidr, desc, parent, vlan_id) in plan["interfaces"]:
            ip   = _ip(cidr)
            mask = _netmask(cidr)

            if vlan_id is not None:
                # Enable parent port as trunk (no IP) once.
                if parent not in enabled_parents:
                    print(f"    Enabling parent {parent} as trunk for VLAN {vlan_id}")
                    _send(conn, "config system interface")
                    _send(conn, f"    edit {parent}")
                    _send(conn, f"        set status up")
                    _send(conn, f"        set allowaccess ping")
                    _send(conn, "    next")
                    _send(conn, "end")
                    enabled_parents.add(parent)

                # Create VLAN subinterface.
                print(f"    {logical:12s}  {cidr}  ({desc})  [VLAN subif]")
                _send(conn, "config system interface")
                _send(conn, f"    edit {logical}")
                _send(conn, '        set vdom "root"')
                _send(conn, f"        set vlanid {vlan_id}")
                _send(conn, f"        set interface {parent}")
                _send(conn, f"        set mode static")
                _send(conn, f"        set ip {ip} {mask}")
                _send(conn, f"        set allowaccess ping")
                _send(conn, f"        set description \"{desc}\"")
                # Disable anti-spoofing RPF check: with MLAG the return path
                # may arrive on a different border leaf than the one that sent
                # the packet, so the source IP appears to come from the wrong
                # interface if RPF is strict.
                _send(conn, f"        set src-check disable")
                _send(conn, f"        set status up")
                _send(conn, "    next")
                _send(conn, "end")

            else:
                # Plain L3 (WAN side, no 802.1Q).
                print(f"    {logical:12s}  {cidr}  ({desc})  [plain L3]")
                _send(conn, "config system interface")
                _send(conn, f"    edit {logical}")
                _send(conn, f"        set mode static")
                _send(conn, f"        set ip {ip} {mask}")
                _send(conn, f"        set allowaccess ping")
                _send(conn, f"        set description \"{desc}\"")
                _send(conn, f"        set status up")
                _send(conn, "    next")
                _send(conn, "end")

        # ── 3. Zones ──────────────────────────────────────────────────
        # Zones let one policy cover multiple interfaces. This is what
        # keeps the total policy count at 2, within the eval limit.

        print("  Purging old policies to free up interfaces...")
        _send(conn, "config firewall policy")
        _send(conn, "    purge")
        _send(conn, "end")
        print("  Configuring zones...")
        _send(conn, "config system zone")
        for zone_name, members in plan["zones"].items():
            member_str = " ".join(f'"{m}"' for m in members)
            print(f"    {zone_name}: {members}")
            _send(conn, f"    edit \"{zone_name}\"")
            # intrazone allow: traffic between members of the same zone
            # (e.g. Border-1 ↔ Border-2 via FW in single-FW mode) is
            # permitted without a separate policy.
            _send(conn, f"        set intrazone allow")
            _send(conn, f"        set interface {member_str}")
            _send(conn, "    next")
        _send(conn, "end")

        # ── 4. BGP ────────────────────────────────────────────────────
        print(f"  Configuring BGP AS {plan['bgp_asn']}...")
        _send(conn, "config router bgp")
        _send(conn, f"    set as {plan['bgp_asn']}")
        _send(conn, f"    set router-id {plan['router_id']}")
        # ECMP: allow equal-cost multipath from both MLAG border leaves.
        _send(conn, "    set ebgp-multipath enable")
        # Redistribute connected subnets (/30 link networks) into BGP.
        _send(conn, "    config redistribute connected")
        _send(conn, "        set status enable")
        _send(conn, "    end")
        _send(conn, "    config neighbor")
        for (peer_ip, remote_as, desc) in plan["bgp_neighbors"]:
            print(f"    Neighbor {peer_ip}  remote-as {remote_as}  ({desc})")
            _send(conn, f"        edit {peer_ip}")
            _send(conn, f"            set remote-as {remote_as}")
            _send(conn, f"            set description \"{desc}\"")
            # next-hop-self: border leaves see the FW as next-hop for
            # internet routes, not FRR's IP directly.
            _send(conn, f"            set next-hop-self enable")
            _send(conn, "        next")
        _send(conn, "    end")
        _send(conn, "end")

        # ── 5. Firewall policies (zone-based, 2 total) ─────────────────
        # (src_zone, dst_zone, nat_enable, policy_name)
        # LAN→WAN: NAT enabled so outbound traffic has a routable source.
        # WAN→LAN: NAT disabled so BGP and return traffic use real IPs.
        print(f"  Configuring {len(plan['fw_policy_pairs'])} firewall policies (zone-based)...")
        _send(conn, "config firewall policy")
        for policy_id, (src_zone, dst_zone, nat_enable, pol_name) in \
                enumerate(plan["fw_policy_pairs"], start=1):
            nat_str = "enable" if nat_enable else "disable"
            print(f"    Policy {policy_id}: {src_zone} → {dst_zone}  NAT={nat_str}  ({pol_name})")
            _send(conn, f"    edit {policy_id}")
            _send(conn, f"        set name \"{pol_name}\"")
            _send(conn, f"        set srcintf \"{src_zone}\"")
            _send(conn, f"        set dstintf \"{dst_zone}\"")
            _send(conn, f"        set srcaddr \"all\"")
            _send(conn, f"        set dstaddr \"all\"")
            _send(conn, f"        set action accept")
            _send(conn, f"        set schedule \"always\"")
            _send(conn, f"        set service \"ALL\"")
            _send(conn, f"        set nat {nat_str}")
            _send(conn, f"        set logtraffic all")
            _send(conn, "    next")
        _send(conn, "end")

        # ── 6. Reset BGP and save ──────────────────────────────────────
        print("  Resetting BGP sessions and saving...")
        _send(conn, "execute router clear bgp all", wait=2)
        _send(conn, "execute backup config flash", wait=3)

        print(f"  ✅  {name} configured successfully.")

    except Exception as e:
        print(f"  ERROR during configuration of {name}: {e}")
    finally:
        conn.disconnect()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if cfg.BORDER_FIREWALL_COUNT == 0:
        print("BORDER_FIREWALL_COUNT is 0 — no firewalls to configure.")
        sys.exit(0)

    plans = build_fw_plans()

    print(f"\nFortiGate configuration plan ({cfg.BORDER_FIREWALL_COUNT} firewall(s)):")
    for plan in plans:
        print(f"\n  {plan['name']}  BGP AS {plan['bgp_asn']}  router-id {plan['router_id']}")
        for (logical, cidr, desc, parent, vlan_id) in plan["interfaces"]:
            tag = f"VLAN {vlan_id} on {parent}" if vlan_id else "plain L3"
            print(f"    {logical:12s}  {cidr:18s}  {desc}  [{tag}]")
        for zone_name, members in plan["zones"].items():
            print(f"    Zone {zone_name}: {members}")
        for (src, dst, nat, pol) in plan["fw_policy_pairs"]:
            print(f"    Policy: {src} → {dst}  NAT={'on' if nat else 'off'}  ({pol})")
        for (peer, asn, desc) in plan["bgp_neighbors"]:
            print(f"    BGP neighbor  {peer:16s}  remote-as {asn}  ({desc})")
    print()

    for plan in plans:
        fw_name = plan["name"]
        try:
            host, port = get_console_endpoint(fw_name)
            print(f"Found {fw_name} console at telnet://{host}:{port}")
        except RuntimeError as e:
            print(f"ERROR: {e}")
            continue

        if not wait_for_telnet(host, port):
            print(f"  {fw_name}: console unreachable after all retries, skipping.")
            continue

        configure_fortigate(plan, host, port)

    print("\nAll firewalls processed.")
