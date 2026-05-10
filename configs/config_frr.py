from netmiko import ConnectHandler
import requests
import time
import socket
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import sots.config as config

FRR_NODE_NAME = "Server-1"
MGMT_IFACE = "enp2s0"
MGMT_GATEWAY = f"{config.MGMT_BASE_IP}.1"
INTERNET_PREFIX = "8.8.8.8/32"
INTERNET_IP = "8.8.8.8"


def get_gns3_host_and_port(node_name):
    session = requests.Session()

    auth_response = session.post(
        f"{config.GNS3_SERVER}/access/users/login",
        data={"username": config.GNS3_USER, "password": config.GNS3_PASSWORD},
    )
    if auth_response.status_code != 200:
        raise RuntimeError(f"GNS3 authentication failed: {auth_response.text}")

    token = auth_response.json().get("access_token") or auth_response.json().get("token")
    if token:
        session.headers.update({"Authorization": f"Bearer {token}"})

    server_ip = config.GNS3_SERVER.split("://")[1].split(":")[0]

    projects_resp = session.get(f"{config.GNS3_SERVER}/projects")
    if projects_resp.status_code != 200:
        raise RuntimeError(f"Failed to get projects: {projects_resp.text}")

    projects = projects_resp.json()
    project_id = next(
        (p["project_id"] for p in projects if p["name"] == config.PROJECT_NAME),
        None,
    )
    if not project_id:
        raise RuntimeError(f"Project '{config.PROJECT_NAME}' not found.")

    nodes_resp = session.get(f"{config.GNS3_SERVER}/projects/{project_id}/nodes")
    for node in nodes_resp.json():
        if node["name"] == node_name:
            return server_ip, node["console"]

    raise RuntimeError(f"Node '{node_name}' not found in project.")


def wait_for_telnet(host, port, retries=8, delay=3):
    for _ in range(retries):
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            time.sleep(delay)
    return False


def configure_frr_telnet():
    try:
        host, port = get_gns3_host_and_port(FRR_NODE_NAME)
    except Exception as exc:
        print(f"Error fetching GNS3 details: {exc}")
        return

    if not wait_for_telnet(host, port):
        print(f"Telnet port {port} is not reachable.")
        return

    try:
        net_connect = ConnectHandler(
            **{
                "device_type": "generic_termserver_telnet",
                "host": host,
                "port": port,
                "global_delay_factor": 2,
            }
        )
        net_connect.write_channel("\n\n")
        time.sleep(2)
        output = net_connect.read_channel()

        if "login:" in output.lower():
            net_connect.write_channel("root\n")
            time.sleep(1)
            net_connect.write_channel("root\n")
            time.sleep(2)
    except Exception as exc:
        print(f"Failed to connect via Telnet: {exc}")
        return

    mgmt_ip = (
        f"{config.MGMT_BASE_IP}."
        f"{config.MGMT_START + config.NUM_SPINES + config.NUM_LEAVES}"
    )

    linux_cmds = [
        f"ip addr add {mgmt_ip}/24 dev {MGMT_IFACE} 2>/dev/null || true",
        "systemctl enable ssh",
        "systemctl start ssh",
        f"ip link set {config.FRR_WAN_IFACE} up",
        f"ip addr add {config.FRR_WAN_CIDR} dev {config.FRR_WAN_IFACE} 2>/dev/null || true",
        f"ip addr add {INTERNET_IP}/32 dev lo 2>/dev/null || true",
        f"ip route del default 2>/dev/null || true",
        f"ip route add default via {config.FRR_WAN_PEER_IP} dev {config.FRR_WAN_IFACE}",
        f"ip route add {INTERNET_PREFIX} dev lo 2>/dev/null || true",
    ]

    vtysh_lines = [
        f"router bgp {config.FRR_BGP_ASN}",
        " no bgp ebgp-requires-policy",
        f" neighbor {config.FRR_WAN_PEER_IP} remote-as {config.FIREWALL_BGP_ASN}",
        " neighbor {peer} description OPNsense-WAN".format(peer=config.FRR_WAN_PEER_IP),
        " address-family ipv4 unicast",
        f"  neighbor {config.FRR_WAN_PEER_IP} activate",
        f"  network {INTERNET_PREFIX}",
        " exit-address-family",
    ]
    frr_config_text = "\n".join(vtysh_lines)

    for cmd in linux_cmds:
        net_connect.write_channel(cmd + "\n")
        time.sleep(0.4)

    bash_script = f"""cat << 'FRREOF' > /tmp/frr_bgp.conf
{frr_config_text}
FRREOF
vtysh -f /tmp/frr_bgp.conf
vtysh -c "write memory"
"""
    for line in bash_script.split("\n"):
        net_connect.write_channel(line + "\n")
        time.sleep(0.1)

    time.sleep(2)
    output = net_connect.read_channel().strip()
    if output:
        print(output)

    net_connect.disconnect()
    print("FRR upstream (to OPNsense WAN) configured.")


if __name__ == "__main__":
    configure_frr_telnet()
