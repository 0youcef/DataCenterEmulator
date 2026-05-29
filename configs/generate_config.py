#!/usr/bin/env python3
"""
monitoring/generate_config.py
──────────────────────────────
Reads the live Ansible inventory (configs/ansible/inventory.py) and
writes monitoring/prometheus/prometheus.yml with every scrape target
derived from the SSOT.

Run from the project root:
    python3 monitoring/generate_config.py

Re-run any time config.py or vlans.py changes — it is idempotent.
"""

import json
import os
import subprocess
import sys
import yaml           # pip install pyyaml

PROJECT_ROOT   = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
INVENTORY_SCRIPT = os.path.join(PROJECT_ROOT, "configs", "ansible", "inventory.py")
OUT_DIR          = os.path.join(PROJECT_ROOT, "configs", "monitoring", "prometheus")
OUT_FILE         = os.path.join(OUT_DIR, "prometheus.yml")

# Port that eos_exporter.py listens on (on the host).
EOS_EXPORTER_PORT = 9101
# Port node_exporter listens on (on each Linux server).
NODE_EXPORTER_PORT = 9100
# Port frr_exporter listens on (on Server-1).
FRR_EXPORTER_PORT = 9342


# ── Helpers ──────────────────────────────────────────────────────────────────

def load_inventory():
    """Call inventory.py --list and return the parsed JSON."""
    result = subprocess.run(
        [sys.executable, INVENTORY_SCRIPT],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )
    if result.returncode != 0:
        print("ERROR: inventory.py failed:")
        print(result.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def hostvars(inv, name):
    return inv["_meta"]["hostvars"].get(name, {})


def hosts_in_group(inv, group):
    """Return flat list of host names for a group (handles children)."""
    g = inv.get(group, {})
    names = list(g.get("hosts", []))
    for child in g.get("children", []):
        names.extend(hosts_in_group(inv, child))
    return names


# ── Scrape config builders ────────────────────────────────────────────────────

def eos_targets(inv):
    """
    All Arista EOS switches are scraped by the local eos_exporter.py process.
    The exporter accepts ?target=<ip> and fans out to all switches itself,
    but we use relabeling so Prometheus shows per-switch metrics.
    """
    switches = hosts_in_group(inv, "switches")
    targets  = []
    for name in switches:
        ip = hostvars(inv, name).get("ansible_host", "")
        if ip:
            targets.append({"targets": [f"{ip}:{EOS_EXPORTER_PORT}"],
                            "labels":  {"job": "eos", "switch": name}})
    return targets


def node_exporter_targets(inv):
    """
    node_exporter runs on all Linux servers (FRR + Proxmox).
    Installed by configs/ansible/monitoring.yml.
    """
    servers = hosts_in_group(inv, "servers") or []

    # inventory.py doesn't have a "servers" group — derive from _meta.
    # Any host whose name starts with "Server-" is a Linux host.
    if not servers:
        servers = [
            n for n in inv["_meta"]["hostvars"]
            if n.startswith("Server-")
        ]

    targets = []
    for name in servers:
        # hostvars for servers are not populated by the current inventory.py
        # (it only covers switches). We derive IPs from the known mgmt range.
        # If you add servers to inventory.py later this still works.
        ip = hostvars(inv, name).get("ansible_host", "")
        if ip:
            targets.append({
                "targets": [f"{ip}:{NODE_EXPORTER_PORT}"],
                "labels":  {"job": "node", "host": name},
            })
    return targets


def frr_exporter_targets(inv):
    """
    frr_exporter runs on Server-1 (FRR / Debian internet simulator).
    """
    ip = hostvars(inv, "Server-1").get("ansible_host", "")
    if not ip:
        return []
    return [{
        "targets": [f"{ip}:{FRR_EXPORTER_PORT}"],
        "labels":  {"job": "frr", "host": "Server-1"},
    }]


# ── Main ─────────────────────────────────────────────────────────────────────

def build_prometheus_config(inv):
    switches = hosts_in_group(inv, "switches")

    # Build a flat list of switch IPs for eos_exporter scraping.
    # Prometheus uses one job per switch so labels are clean.
    eos_scrape_configs = []
    for name in switches:
        ip = hostvars(inv, name).get("ansible_host", "")
        if not ip:
            continue
        eos_scrape_configs.append({
            "job_name": f"eos_{name.lower().replace('-', '_')}",
            "metrics_path": "/metrics",
            "params":        {"target": [ip]},
            "static_configs": [{"targets": [f"host.docker.internal:{EOS_EXPORTER_PORT}"],
                                "labels":  {"switch": name}}],
        })

    # Server scrape configs (node_exporter)
    server_names = [
        n for n in inv["_meta"]["hostvars"] if n.startswith("Server-")
    ]
    node_scrape_configs = []
    for name in server_names:
        ip = hostvars(inv, name).get("ansible_host", "")
        if not ip:
            continue
        node_scrape_configs.append({
            "job_name": f"node_{name.lower().replace('-', '_')}",
            "static_configs": [{"targets": [f"{ip}:{NODE_EXPORTER_PORT}"],
                                "labels":  {"host": name}}],
        })

    # Firewall scrape configs
    fw_names = [
        n for n in inv["_meta"]["hostvars"] if n.startswith("Firewall-")
    ]
    fw_scrape_configs = []
    for name in fw_names:
        ip = hostvars(inv, name).get("ansible_host", "")
        if not ip:
            continue
        fw_scrape_configs.append({
            "job_name": f"node_{name.lower().replace('-', '_')}",
            "static_configs": [{"targets": [f"{ip}:{NODE_EXPORTER_PORT}"],
                                "labels":  {"host": name}}],
        })

    config = {
        "global": {
            "scrape_interval":      "15s",
            "evaluation_interval":  "15s",
            "scrape_timeout":       "10s",
        },
        "alerting": {
            "alertmanagers": [{
                "static_configs": [{"targets": ["alertmanager:9093"]}],
            }],
        },
        "rule_files": ["/etc/prometheus/alerts.yml"],
        "scrape_configs": (
            eos_scrape_configs
            + node_scrape_configs
            + fw_scrape_configs
            + [{
                "job_name":       "prometheus",
                "static_configs": [{"targets": ["localhost:9090"]}],
            }]
        ),
    }

    return config


def main():
    print("Loading inventory...")
    inv = load_inventory()

    switches = hosts_in_group(inv, "switches")
    print(f"  Switches found: {len(switches)} — {', '.join(switches)}")

    print("Building prometheus.yml...")
    prom_config = build_prometheus_config(inv)

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_FILE, "w") as f:
        yaml.dump(prom_config, f, default_flow_style=False, sort_keys=False)

    print(f"  Written: {OUT_FILE}")
    print("Done. Run 'docker compose -f monitoring/docker-compose.yml up -d' to start.")


if __name__ == "__main__":
    main()
