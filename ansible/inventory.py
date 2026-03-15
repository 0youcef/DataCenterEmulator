#!/usr/bin/env python3
import json
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config import (
    NUM_SPINES, NUM_LEAVES,
    MGMT_BASE_IP, MGMT_START,
    SSH_USER, SSH_PASS,
)

inventory = {
    "spines": {"hosts": []},
    "leaves": {"hosts": []},
    "switches": {"children": ["spines", "leaves"]},
    "_meta": {"hostvars": {}}
}

counter = MGMT_START

for i in range(1, NUM_SPINES + 1):
    name = f"Spine{i}"
    ip = f"{MGMT_BASE_IP}.{counter}"
    inventory["spines"]["hosts"].append(name)
    inventory["_meta"]["hostvars"][name] = {
        "ansible_host": ip,
        "ansible_user": SSH_USER,
        "ansible_password": SSH_PASS,
        "ansible_network_os": "eos",
        "ansible_connection": "httpapi",
        "ansible_httpapi_use_ssl": True,
        "ansible_httpapi_validate_certs": False,
        "ansible_httpapi_port": 443,
    }
    counter += 1

for i in range(1, NUM_LEAVES + 1):
    name = f"Leaf{i}"
    ip = f"{MGMT_BASE_IP}.{counter}"
    inventory["leaves"]["hosts"].append(name)
    inventory["_meta"]["hostvars"][name] = {
        "ansible_host": ip,
        "ansible_user": SSH_USER,
        "ansible_password": SSH_PASS,
        "ansible_network_os": "eos",
        "ansible_connection": "httpapi",
        "ansible_httpapi_use_ssl": True,
        "ansible_httpapi_validate_certs": False,
        "ansible_httpapi_port": 443,
    }
    counter += 1

print(json.dumps(inventory, indent=2))
