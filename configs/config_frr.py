from netmiko import ConnectHandler
import requests
import time
import socket
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import sots.config as config
import sots.vlans as vlans

FRR_NODE_NAME  = "Server-1"   # exact GNS3 node name
FRR_IFACE      = "ens1"       # physical interface on the Debian VM facing Border-1
INTERNET_PREFIX = "8.8.8.8/32"

# BUG 2 FIX: Border-1 is leaf index 0, so its ASN = LEAF_AS_BASE + 0.
# Was hardcoded as 65101 (leaf index 1) which caused BGP sessions to
# never establish because the remote-as didn't match.
BORDER_LEAF_ASN = config.LEAF_AS_BASE  # 65100

def get_gns3_host_and_port(node_name):
    """Fetch the GNS3 server IP and the node's console port via the GNS3 API."""
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

    projects = projects_resp.json()
    project_id = next((p["project_id"] for p in projects if p["name"] == config.PROJECT_NAME), None)
    if not project_id:
        raise RuntimeError(f"Project '{config.PROJECT_NAME}' not found.")

    nodes_resp = session.get(f"{config.GNS3_SERVER}/projects/{project_id}/nodes")
    nodes = nodes_resp.json()
    for node in nodes:
        if node["name"] == node_name:
            return server_ip, node["console"]

    raise RuntimeError(f"Node '{node_name}' not found in project.")

def wait_for_telnet(host, port, retries=5, delay=2):
    for i in range(retries):
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

    debian_vm = {
        'device_type': 'generic_termserver_telnet',
        'host': host,
        'port': port,
        'global_delay_factor': 2,
    }

    try:
        print(f"Connecting to {FRR_NODE_NAME} console...")
        net_connect = ConnectHandler(**debian_vm)

        print("Waking up console...")
        net_connect.write_channel("\n\n")
        time.sleep(2)
        output = net_connect.read_channel()

        if "login:" in output.lower():
            print("Sending username...")
            net_connect.write_channel(config.SSH_USER + "\n")
            time.sleep(1)
            print("Sending password...")
            net_connect.write_channel(config.SSH_PASS + "\n")
            time.sleep(2)

    except Exception as e:
        print(f"Failed to connect via Telnet: {e}")
        return

    # ---------------------------------------------------------
    # 1. Linux OS commands — interfaces and FRR daemon
    # ---------------------------------------------------------
    linux_cmds = [
        f"ip addr add {config.MGMT_BASE_IP}.{config.MGMT_START+config.NUM_LEAVES+config.NUM_SPINES}/24 dev enp2s0",
        "systemctl enable ssh",
        "systemctl start ssh",
        "modprobe 8021q",
        "sleep 1",
        f"ip link set {FRR_IFACE} up",
        # BUG 4 FIX: Add a blackhole route so FRR's RIB contains the prefix.
        # BGP will not advertise a "network X" statement unless the prefix
        # exists as a route in the kernel routing table. Without this,
        # the network command is accepted by FRR but silently not advertised.
        f"ip route add {INTERNET_PREFIX} dev lo 2>/dev/null || true",
        "ip addr add 172.16.0.1/32 dev lo 2>/dev/null || true",  # reachable ping target
    ]

    # Add subinterface setup per tenant
    for tenant in vlans.TENANTS:
        if not tenant.get("external_handoff"):
            continue
        vlan_id  = tenant["handoff_vlan"]
        peer_ip  = tenant["handoff_peer_ip"]
        mask     = tenant["handoff_local_ip"].split('/')[1]
        iface    = f"{FRR_IFACE}.{vlan_id}"
        linux_cmds.extend([
            f"ip link add link {FRR_IFACE} name {iface} type vlan id {vlan_id} 2>/dev/null || true",
            f"ip link set {iface} up",
            f"ip addr add {peer_ip}/{mask} dev {iface} 2>/dev/null || true",
        ])

    # ---------------------------------------------------------
    # 2. FRR vtysh config file
    #
    # BUG 1 + 5 FIX: vtysh -f must NOT contain "configure terminal"
    # or "end". The -f flag feeds the file directly to the config
    # parser which is already in configuration mode. Including these
    # commands causes "no configure terminal command" parse errors
    # that abort processing of everything after them.
    #
    # BUG 3 FIX: "address-family ipv4 unicast" must appear ONCE and
    # contain ALL neighbor activate lines. Repeating the block per
    # tenant causes FRR to re-enter and exit the AF multiple times —
    # only the last block's neighbors end up active; earlier ones are
    # silently dropped.
    # ---------------------------------------------------------
    frr_asn = next(
        (t["handoff_peer_asn"] for t in vlans.TENANTS if t.get("external_handoff")),
        65999
    )

    # Build the neighbor declarations (outside address-family)
    neighbor_lines = []
    activate_lines = []
    for tenant in vlans.TENANTS:
        if not tenant.get("external_handoff"):
            continue
        leaf_ip = tenant["handoff_local_ip"].split('/')[0]
        neighbor_lines.extend([
            f" neighbor {leaf_ip} remote-as {BORDER_LEAF_ASN}",
            f" neighbor {leaf_ip} description Border-1-{tenant['name']}",
        ])
        activate_lines.append(f"  neighbor {leaf_ip} activate")

    # Assemble into a single clean router bgp block
    vtysh_lines = (
        [
            f"router bgp {frr_asn}",
            f" no bgp ebgp-requires-policy",
        ]
        + neighbor_lines
        + [
            f" address-family ipv4 unicast",
        ]
        + activate_lines
        + [
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
    # 4. Write vtysh config file and apply it
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
    print(output)

    net_connect.disconnect()
    print("\n✅ FRR Configuration Applied Successfully!")

if __name__ == "__main__":
    configure_debian_telnet()
