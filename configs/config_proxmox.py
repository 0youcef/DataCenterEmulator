from netmiko import ConnectHandler
import requests
import time
import socket
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import sots.config as config

# ----------------------------------------------------------------
# Server IP allocation — must mirror inventory.py's mgmt counter.
# Spines are assigned first, then leaves, then servers follow.
# Changing NUM_SPINES or NUM_LEAVES in config.py automatically
# shifts server IPs to the correct position.
# ----------------------------------------------------------------
SERVER_IFACE   = "enp2s0"
MGMT_NETMASK   = "24"
# Gateway is the host bridge — typically the .1 of the mgmt subnet
MGMT_GATEWAY   = f"{config.MGMT_BASE_IP}.1"

username="root"
password="rootroot"

def get_server_mgmt_ips():
    """
    Derive server IPs from config.py, starting after all switches.
    Returns a list of (node_name, ip) tuples in allocation order.
    """
    mgmt = config.MGMT_START
    mgmt += config.NUM_SPINES   # skip spines
    mgmt += config.NUM_LEAVES   # skip leaves (including Border-1)
    servers = []
    for k in range(config.NUM_SERVERS):
        name = f"Server-{k + 1}"
        ip   = f"{config.MGMT_BASE_IP}.{mgmt}"
        servers.append((name, ip))
        mgmt += 1
    return servers

# ----------------------------------------------------------------

def get_gns3_console(node_name):
    """Return (server_ip, console_port) for a GNS3 node."""
    session = requests.Session()

    print(f"  Authenticating with GNS3...")
    auth_resp = session.post(
        f"{config.GNS3_SERVER}/access/users/login",
        data={"username": config.GNS3_USER, "password": config.GNS3_PASSWORD}
    )
    if auth_resp.status_code != 200:
        raise RuntimeError(f"GNS3 auth failed: {auth_resp.text}")

    token = auth_resp.json().get("access_token") or auth_resp.json().get("token")
    if token:
        session.headers.update({"Authorization": f"Bearer {token}"})

    server_ip = config.GNS3_SERVER.split("://")[1].split(":")[0]

    projects_resp = session.get(f"{config.GNS3_SERVER}/projects")
    if projects_resp.status_code != 200:
        raise RuntimeError(f"Failed to get projects: {projects_resp.text}")

    project_id = next(
        (p["project_id"] for p in projects_resp.json() if p["name"] == config.PROJECT_NAME),
        None
    )
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

def configure_server(node_name, ip):
    print(f"\n{'='*60}")
    print(f"  Configuring {node_name}  →  {ip}/{MGMT_NETMASK}")
    print(f"{'='*60}")

    try:
        host, port = get_gns3_console(node_name)
        print(f"  Console at telnet://{host}:{port}")
    except Exception as e:
        print(f"  [SKIP] Could not get console for {node_name}: {e}")
        return

    if not wait_for_telnet(host, port):
        print(f"  [SKIP] Telnet port {port} unreachable for {node_name}")
        return

    try:
        net_connect = ConnectHandler(**{
            'device_type':        'generic_termserver_telnet',
            'host':               host,
            'port':               port,
            'global_delay_factor': 2,
        })

        # Wake up console
        net_connect.write_channel("\n\n")
        time.sleep(2)
        output = net_connect.read_channel()

        if "login:" in output.lower():
            net_connect.write_channel(username + "\n")
            time.sleep(1)
            net_connect.write_channel(password + "\n")
            time.sleep(2)


    except Exception as e:
        print(f"  [FAIL] Telnet connection failed: {e}")
        return

    # ----------------------------------------------------------------
    # Build interface commands
    # Use 2>/dev/null || true so re-runs don't fail if already set.
    # ----------------------------------------------------------------
    cmds = [
        # Bring the interface up first — required before assigning IP
        f"ip link set {SERVER_IFACE} up",
        # Flush any existing IPs so re-runs are idempotent
        f"ip addr flush dev {SERVER_IFACE} 2>/dev/null || true",
        # Assign the IP
        f"ip addr add {ip}/{MGMT_NETMASK} dev {SERVER_IFACE}",
        # Set the default route via the management gateway
        f"ip route del default 2>/dev/null || true",
        f"ip route add default via {MGMT_GATEWAY} dev {SERVER_IFACE}",
    ]

    # ----------------------------------------------------------------
    # Make config persistent across reboots (/etc/network/interfaces)
    # Writes only if not already present to avoid duplicates.
    # ----------------------------------------------------------------
    persistent_block = (
        f"auto {SERVER_IFACE}\\n"
        f"iface {SERVER_IFACE} inet static\\n"
        f"    address {ip}/{MGMT_NETMASK}\\n"
        f"    gateway {MGMT_GATEWAY}\\n"
    )
    cmds.append(
        f"grep -q 'address {ip}' /etc/network/interfaces || "
        f"printf '{persistent_block}' >> /etc/network/interfaces"
    )

    print(f"\n  --- Applying interface config ---")
    for cmd in cmds:
        net_connect.write_channel(cmd + "\n")
        time.sleep(0.5)
        print(f"  {cmd}")

    time.sleep(1)
    output = net_connect.read_channel()
    if output.strip():
        print(f"\n  Console output:\n{output}")

    net_connect.disconnect()
    print(f"\n  ✅ {node_name} configured with {ip}/{MGMT_NETMASK}")

def main():
    servers = get_server_mgmt_ips()

    print("=== Server IP Plan (derived from config.py) ===")
    for name, ip in servers:
        print(f"  {name}  →  {ip}/{MGMT_NETMASK}  (gateway: {MGMT_GATEWAY})")
    print()

    for node_name, ip in servers:
        configure_server(node_name, ip)

    print(f"\n{'='*60}")
    print("  All servers configured.")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
