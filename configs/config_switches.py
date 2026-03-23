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
)

# Build switch name -> mgmt IP mapping dynamically from config
SWITCHES = {}
counter = MGMT_START
for i in range(1, NUM_SPINES + 1):
    SWITCHES[f"Spine-{i}"] = f"{MGMT_BASE_IP}.{counter}"
    counter += 1
for i in range(1, NUM_LEAVES + 1):
    SWITCHES[f"Leaf-{i}"] = f"{MGMT_BASE_IP}.{counter}"
    counter += 1


def get_console_ports():
    """Fetch console port for each switch node from GNS3 API."""
    session = requests.Session()

    # Authenticate
    auth = session.post(f"{GNS3_SERVER}/access/users/login", data={
        "username": GNS3_USER,
        "password": GNS3_PASSWORD
    })
    if auth.status_code != 200:
        raise RuntimeError(f"GNS3 authentication failed: {auth.text}")
    token = auth.json().get("access_token")
    session.headers.update({"Authorization": f"Bearer {token}"})

    # Get project ID
    projects = session.get(f"{GNS3_SERVER}/projects").json()
    project = next((p for p in projects if p['name'] == PROJECT_NAME), None)
    if not project:
        raise RuntimeError(f"Project '{PROJECT_NAME}' not found in GNS3")
    project_id = project['project_id']

    # Get all nodes and extract console ports for switches
    nodes = session.get(f"{GNS3_SERVER}/projects/{project_id}/nodes").json()
    console_ports = {}
    for node in nodes:
        if node['name'] in SWITCHES:
            console_ports[node['name']] = node['console']

    return console_ports


def wait_for_telnet(host, port, retries=10, delay=15):
    """Keep retrying until the console port accepts connections."""
    for attempt in range(retries):
        try:
            sock = socket.create_connection((host, port), timeout=5)
            sock.close()
            return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            print(f"  Waiting for console port {port} to be ready... ({attempt + 1}/{retries})")
            time.sleep(delay)
    return False


def configure_switch(name, mgmt_ip, console_port):
    print(f"Connecting to {name} via telnet (port {console_port})...")

    device = {
        "device_type": "arista_eos_telnet",
        "host": "127.0.0.1",
        "port": console_port,
        "username": SSH_USER,
        "password": SSH_PASS,
        "timeout": 30,
    }

    try:
        with ConnectHandler(**device) as conn:
            conn.enable()
            commands = [
                f"hostname {name}",
                "zerotouch disable"
                "ip routing",
                "interface Management1",
                f"ip address {mgmt_ip}/24",
                "no shutdown",
                "exit",           # explicitly exit interface context
                "management api http-commands",
                "no shutdown",
                "protocol https",
                "no shutdown",
                "exit",           # exit management api context
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


if __name__ == "__main__":
    print("Fetching console ports from GNS3...\n")
    try:
        console_ports = get_console_ports()
    except RuntimeError as e:
        print(f"Error: {e}")
        exit(1)

    for name, mgmt_ip in SWITCHES.items():
        port = console_ports.get(name)
        if not port:
            print(f" -> {name}: console port not found in GNS3, skipping")
            continue

        if wait_for_telnet("127.0.0.1", port):
            configure_switch(name, mgmt_ip, port)
        else:
            print(f" -> {name}: console unreachable after all retries, skipping")

    print("\nDone. eAPI is now available on all configured switches.")
    print("Access via: https://<switch-ip>/command-api")
