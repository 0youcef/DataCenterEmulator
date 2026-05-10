from netmiko import ConnectHandler
import requests
import time
import socket
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import sots.config as config
import sots.vlans as vlans

FRR_NODE_NAME = "Server-1"

# ── Physical interfaces ────────────────────────────────────────────────────
# Server-1 now has TWO fabric uplinks (wired in deploy_fabric.py).
# The adapter allocation order from deploy_fabric.py is:
#   adapter 0 → management (enp2s0)
#   adapter 1 → Border-1   (ens1)   ← first link in the dual-uplink loop
#   adapter 2 → Border-2   (ens2)   ← second link in the dual-uplink loop
FRR_IFACE   = "ens1"   # uplink to Border-1 (left peer of MLAG_PAIRS[0])
FRR_IFACE_2 = "ens2"   # uplink to Border-2 (right peer of MLAG_PAIRS[0])

# INTERNET_PREFIX is what FRR advertises into BGP.
# Both a /32 route AND an IP address are added to lo so pings are answered.
INTERNET_PREFIX = "8.8.8.8/32"
INTERNET_IP     = "8.8.8.8"

# ── Border-leaf ASN ────────────────────────────────────────────────────────
# Both MLAG peers share the primary leaf's ASN (standard Arista practice).
# The primary leaf is the left entry of MLAG_PAIRS[0], which is 1-based.
# Example: MLAG_PAIRS[0] = [2, 3]  →  Border-1 = Leaf-2
#          BORDER_LEAF_ASN = LEAF_AS_BASE + (2 - 1) = 65101
#
# Previously this was hardcoded to config.LEAF_AS_BASE (= 65100, Leaf-1's
# ASN), which is wrong whenever the border pair isn't the very first leaf.
def _parse_pair_ints(pair):
    if isinstance(pair, str):
        parts = [p.strip() for p in pair.split(",") if p.strip()]
    else:
        parts = list(pair)
    return int(parts[0]), int(parts[1])

_border_left_idx, _border_right_idx = _parse_pair_ints(config.MLAG_PAIRS[0])
BORDER_LEAF_ASN = config.LEAF_AS_BASE + (_border_left_idx - 1)
# In an MLAG pair both leaves share this ASN, so no separate ASN for Border-2.


def get_gns3_host_and_port(node_name):
    session = requests.Session()

    print("Authenticating with GNS3...")
    auth_response = session.post(f"{config.GNS3_SERVER}/access/users/login", data={
        "username": config.GNS3_USER,
        "password": config.GNS3_PASSWORD
    })
    if auth_response.status_code != 200:
        raise RuntimeError(f"GNS3 authentication failed: {auth_response.text}")

    auth_data = auth_response.json()
    token = auth_data.get("access_token") or auth_data.get("token")
    if token:
        session.headers.update({"Authorization": f"Bearer {token}"})

    server_ip = config.GNS3_SERVER.split("://")[1].split(":")[0]

    projects_resp = session.get(f"{config.GNS3_SERVER}/projects")
    if projects_resp.status_code != 200:
        raise RuntimeError(f"Failed to get projects: {projects_resp.text}")

    projects   = projects_resp.json()
    project_id = next((p["project_id"] for p in projects if p["name"] == config.PROJECT_NAME), None)
    if not project_id:
        raise RuntimeError(f"Project '{config.PROJECT_NAME}' not found.")

    nodes_resp = session.get(f"{config.GNS3_SERVER}/projects/{project_id}/nodes")
    for node in nodes_resp.json():
        if node["name"] == node_name:
            return server_ip, node["console"]

    raise RuntimeError(f"Node '{node_name}' not found in project.")


def wait_for_telnet(host, port, retries=5, delay=2):
    for _ in range(retries):
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            time.sleep(delay)
    return False


def configure_debian_telnet():
    try:
        host, port = get_gns3_host_and_port(FRR_NODE_NAME)
        print(f"Found {FRR_NODE_NAME} console at telnet://{host}:{port}")
    except Exception as e:
        print(f"Error fetching GNS3 details: {e}")
        return

    if not wait_for_telnet(host, port):
        print(f"Telnet port {port} is not reachable.")
        return

    try:
        print(f"Connecting to {FRR_NODE_NAME} console...")
        net_connect = ConnectHandler(**{
            'device_type':         'generic_termserver_telnet',
            'host':                host,
            'port':                port,
            'global_delay_factor': 2,
        })

        print("Waking up console...")
        net_connect.write_channel("\n\n")
        time.sleep(2)
        output = net_connect.read_channel()

        if "login:" in output.lower():
            net_connect.write_channel("root\n")
            time.sleep(1)
            net_connect.write_channel("root\n")
            time.sleep(2)

    except Exception as e:
        print(f"Failed to connect via Telnet: {e}")
        return

    # ---------------------------------------------------------
    # 1. Linux OS commands
    # ---------------------------------------------------------
    mgmt_ip = (
        f"{config.MGMT_BASE_IP}."
        f"{config.MGMT_START + config.NUM_SPINES + config.NUM_LEAVES}"
    )
    linux_cmds = [
        # Management interface
        f"ip addr add {mgmt_ip}/24 dev enp2s0 2>/dev/null || true",
        "systemctl enable ssh",
        "systemctl start ssh",

        # Load 802.1Q module once — needed by both fabric interfaces
        "modprobe 8021q",

        # ── Bring up BOTH physical fabric interfaces ────────────────
        # ens1 connects to Border-1 (left peer in MLAG_PAIRS[0])
        # ens2 connects to Border-2 (right peer in MLAG_PAIRS[0])
        f"ip link set {FRR_IFACE} up",
        f"ip link set {FRR_IFACE_2} up",

        # Assign the advertised internet IP to lo so pings are answered.
        # "ip route add 8.8.8.8/32 dev lo" only adds a route — it does NOT
        # make lo own the address.  This "ip addr add" makes FRR own it.
        f"ip addr add {INTERNET_IP}/32 dev lo 2>/dev/null || true",
    ]

    # ── Per-tenant subinterfaces on BOTH uplinks ──────────────────────
    # vlans.TENANTS entries with external_handoff=True must provide:
    #   handoff_vlan        — 802.1Q VLAN ID (same on both links)
    #   handoff_peer_ip     — FRR's IP on the Border-1 subinterface
    #   handoff_local_ip    — Border-1's IP (FRR's BGP peer on ens1)
    #   handoff_peer_ip_2   — FRR's IP on the Border-2 subinterface
    #   handoff_local_ip_2  — Border-2's IP (FRR's BGP peer on ens2)
    for tenant in vlans.TENANTS:
        if not tenant.get("external_handoff"):
            continue

        vlan_id = tenant["handoff_vlan"]
        mask    = tenant["handoff_local_ip"].split('/')[1]

        # ── ens1.VLAN → Border-1 ───────────────────────────────────
        iface_1   = f"{FRR_IFACE}.{vlan_id}"
        peer_ip_1 = tenant["handoff_peer_ip"]
        linux_cmds.extend([
            f"ip link add link {FRR_IFACE} name {iface_1} type vlan id {vlan_id} 2>/dev/null || true",
            f"ip link set {iface_1} up",
            f"ip addr add {peer_ip_1}/{mask} dev {iface_1} 2>/dev/null || true",
        ])

        # ── ens2.VLAN → Border-2 ───────────────────────────────────
        # Requires handoff_peer_ip_2 / handoff_local_ip_2 in vlans.py.
        # These are the IPs for the mirrored session toward Border-2.
        if tenant.get("handoff_peer_ip_2") and tenant.get("handoff_local_ip_2"):
            iface_2   = f"{FRR_IFACE_2}.{vlan_id}"
            peer_ip_2 = tenant["handoff_peer_ip_2"]
            linux_cmds.extend([
                f"ip link add link {FRR_IFACE_2} name {iface_2} type vlan id {vlan_id} 2>/dev/null || true",
                f"ip link set {iface_2} up",
                f"ip addr add {peer_ip_2}/{mask} dev {iface_2} 2>/dev/null || true",
            ])

    # ---------------------------------------------------------
    # 2. FRR vtysh config
    #    No "configure terminal" or "end" — invalid in vtysh -f mode.
    #    Single address-family block containing ALL neighbor activates.
    # ---------------------------------------------------------
    frr_asn = next(
        (t["handoff_peer_asn"] for t in vlans.TENANTS if t.get("external_handoff")),
        65999
    )

    neighbor_lines = []
    activate_lines = []

    for tenant in vlans.TENANTS:
        if not tenant.get("external_handoff"):
            continue

        # ── Border-1 BGP session (ens1.VLAN) ───────────────────────
        # handoff_local_ip is Border-1's IP — FRR peers toward it.
        border1_ip = tenant["handoff_local_ip"].split('/')[0]
        neighbor_lines.extend([
            f" neighbor {border1_ip} remote-as {BORDER_LEAF_ASN}",
            f" neighbor {border1_ip} description Border-1-{tenant['name']}",
        ])
        activate_lines.append(f"  neighbor {border1_ip} activate")

        # ── Border-2 BGP session (ens2.VLAN) ───────────────────────
        # handoff_local_ip_2 is Border-2's IP — FRR peers toward it.
        # Both MLAG peers share BORDER_LEAF_ASN (standard practice).
        if tenant.get("handoff_local_ip_2"):
            border2_ip = tenant["handoff_local_ip_2"].split('/')[0]
            neighbor_lines.extend([
                f" neighbor {border2_ip} remote-as {BORDER_LEAF_ASN}",
                f" neighbor {border2_ip} description Border-2-{tenant['name']}",
            ])
            activate_lines.append(f"  neighbor {border2_ip} activate")

    vtysh_lines = (
        [
            f"router bgp {frr_asn}",
            f" no bgp ebgp-requires-policy",
        ]
        + neighbor_lines
        + [f" address-family ipv4 unicast"]
        + activate_lines
        + [
            # Advertise the internet prefix — FRR only announces this if
            # the address exists locally (ensured by ip addr add above).
            f"  network {INTERNET_PREFIX}",
            f" exit-address-family",
        ]
    )

    frr_config_text = "\n".join(vtysh_lines)

    # ---------------------------------------------------------
    # 3. Execute Linux commands
    # ---------------------------------------------------------
    print("\n--- Applying Linux Interface Configs ---")
    for cmd in linux_cmds:
        net_connect.write_channel(cmd + "\n")
        time.sleep(0.5)
        print(f"  {cmd}")

    # ---------------------------------------------------------
    # 4. Write and apply FRR config
    # ---------------------------------------------------------
    print("\n--- Applying FRR BGP Configs via vtysh -f ---")
    bash_script = f"""cat << 'FRREOF' > /tmp/frr_bgp.conf
{frr_config_text}
FRREOF
vtysh -f /tmp/frr_bgp.conf
vtysh -c "write memory"
"""
    for line in bash_script.split('\n'):
        net_connect.write_channel(line + "\n")
        time.sleep(0.1)

    time.sleep(3)
    output = net_connect.read_channel()
    if output.strip():
        print(output)

    net_connect.disconnect()
    print("\n✅ FRR Configuration Applied Successfully!")
    print(f"\nPingable targets from tenant servers:")
    print(f"  {INTERNET_IP}  (advertised internet prefix)")


if __name__ == "__main__":
    configure_debian_telnet()
