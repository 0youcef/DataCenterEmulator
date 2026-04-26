from netmiko import ConnectHandler
import requests
import time
import socket
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import sots.config as config
import sots.vlans as vlans

FRR_NODE_NAME   = "Server-1"
FRR_IFACE       = "ens1"
# INTERNET_PREFIX is what FRR advertises into BGP.
# It must also be assigned as an IP on lo so pings to it are answered.
# Both the /32 route AND the IP address are added below.
INTERNET_PREFIX = "8.8.8.8/32"
INTERNET_IP     = "8.8.8.8"       # assigned to lo — makes it pingable

BORDER_LEAF_ASN = config.LEAF_AS_BASE  # Border-1 is leaf index 0 → LEAF_AS_BASE + 0


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
    mgmt_ip = f"{config.MGMT_BASE_IP}.{config.MGMT_START + config.NUM_SPINES + config.NUM_LEAVES}"
    linux_cmds = [
        # Management interface
        f"ip addr add {mgmt_ip}/24 dev enp2s0 2>/dev/null || true",
        "systemctl enable ssh",
        "systemctl start ssh",


        # Physical interface up
        "modprobe 8021q",
        f"ip link set {FRR_IFACE} up",

        # CRITICAL FIX: assign the advertised IP to lo so pings are answered.
        # "ip route add 8.8.8.8/32 dev lo" only adds a routing entry — it does
        # NOT assign 8.8.8.8 as an address. Pings arriving at FRR destined for
        # 8.8.8.8 would be delivered to lo but immediately discarded because no
        # process owns that address. "ip addr add" makes lo own it.
        f"ip addr add {INTERNET_IP}/32 dev lo 2>/dev/null || true",
    ]

    # Subinterface setup per tenant
    for tenant in vlans.TENANTS:
        if not tenant.get("external_handoff"):
            continue
        vlan_id = tenant["handoff_vlan"]
        peer_ip = tenant["handoff_peer_ip"]
        mask    = tenant["handoff_local_ip"].split('/')[1]
        iface   = f"{FRR_IFACE}.{vlan_id}"
        linux_cmds.extend([
            f"ip link add link {FRR_IFACE} name {iface} type vlan id {vlan_id} 2>/dev/null || true",
            f"ip link set {iface} up",
            f"ip addr add {peer_ip}/{mask} dev {iface} 2>/dev/null || true",
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
        leaf_ip = tenant["handoff_local_ip"].split('/')[0]
        neighbor_lines.extend([
            f" neighbor {leaf_ip} remote-as {BORDER_LEAF_ASN}",
            f" neighbor {leaf_ip} description Border-1-{tenant['name']}",
        ])
        activate_lines.append(f"  neighbor {leaf_ip} activate")

    vtysh_lines = (
        [
            f"router bgp {frr_asn}",
            f" no bgp ebgp-requires-policy",
        ]
        + neighbor_lines
        + [f" address-family ipv4 unicast"]
        + activate_lines
        + [
            # Advertise the internet prefix — FRR will only announce this if
            # the address exists locally (ensured by ip addr add above)
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
