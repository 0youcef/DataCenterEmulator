#!/usr/bin/env python3
"""
monitoring/exporters/eos_exporter.py
──────────────────────────────────────
Prometheus exporter for Arista EOS switches.
Polls the eAPI (HTTPS JSON-RPC) of each switch and exposes metrics on
http://localhost:9101/metrics?target=<switch-ip>

Prometheus scrapes localhost:9101 with ?target= per switch, so a single
exporter process covers all switches with no per-switch service needed.

Metrics exposed
───────────────
eos_bgp_session_up{switch,vrf,neighbor,peer_asn}       1=Established 0=other
eos_bgp_prefixes_received{switch,vrf,neighbor}         count
eos_bgp_prefixes_advertised{switch,vrf,neighbor}       count
eos_mlag_active{switch}                                1=active 0=other
eos_mlag_peer_link_up{switch}                          1=up 0=down
eos_mlag_peer_alive{switch}                            1=alive 0=not
eos_interface_up{switch,interface}                     1=connected 0=other
eos_interface_in_octets_total{switch,interface}        counter
eos_interface_out_octets_total{switch,interface}       counter
eos_interface_in_errors_total{switch,interface}        counter
eos_interface_out_errors_total{switch,interface}       counter
eos_evpn_session_up{switch,neighbor,peer_asn}          1=Established 0=other
eos_evpn_routes_received{switch,neighbor}              count

Run:
    python3 monitoring/exporters/eos_exporter.py
"""

import os
import sys
import time
import threading
import logging

import requests
import urllib3
from prometheus_client import (
    start_http_server,
    Gauge,
    Counter,
    CollectorRegistry,
    REGISTRY,
)
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily, REGISTRY

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from sots.config import SSH_USER, SSH_PASS

PORT          = 9101
SCRAPE_TIMEOUT = 8          # seconds per switch
EOS_USER      = SSH_USER
EOS_PASS      = SSH_PASS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("eos_exporter")


# ── eAPI client ──────────────────────────────────────────────────────────────

def eapi(ip, commands, timeout=SCRAPE_TIMEOUT):
    """
    Call the EOS JSON-RPC eAPI.
    Returns list of result dicts, one per command.
    Raises on any error.
    """
    url  = f"https://{ip}/command-api"
    body = {
        "jsonrpc": "2.0",
        "method":  "runCmds",
        "params":  {"version": 1, "cmds": commands, "format": "json"},
        "id":      "eos_exporter",
    }
    resp = requests.post(
        url, json=body,
        auth=(EOS_USER, EOS_PASS),
        verify=False,
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"eAPI error: {data['error']}")
    return data["result"]


# ── Metric families ──────────────────────────────────────────────────────────

class EosCollector:
    """
    Custom Prometheus collector.  Registered once; collect() is called every
    time Prometheus scrapes.  The ?target= query param selects the switch.
    """

    def describe(self):
        # Required by the custom collector protocol — return empty to avoid
        # duplicate registration checks.
        return []

    def collect(self):
        # This collector is invoked for ALL targets in one scrape cycle.
        # In practice, generate_config.py sets up one job per switch with
        # ?target=<ip>, so collect() is called once per scrape with that IP
        # available via the prometheus_client request handler.
        # For simplicity we iterate all known targets from the environment.
        targets_env = os.environ.get("EOS_TARGETS", "")
        if not targets_env:
            return
        targets = [t.strip() for t in targets_env.split(",") if t.strip()]

        for ip in targets:
            yield from self._collect_switch(ip)

    def _collect_switch(self, ip):
        switch_label = ip   # Prometheus relabeling renames this to the switch name

        try:
            results = eapi(ip, [
                "show bgp summary vrf all",
                "show mlag",
                "show interfaces",
                "show bgp evpn summary",
            ])
        except Exception as exc:
            log.warning(f"[{ip}] scrape failed: {exc}")
            # Emit an up=0 metric so alert fires
            g = GaugeMetricFamily(
                "eos_up", "1 if the switch eAPI is reachable",
                labels=["switch"]
            )
            g.add_metric([switch_label], 0)
            yield g
            return

        yield from self._bgp_metrics(switch_label, results[0])
        yield from self._mlag_metrics(switch_label, results[1])
        yield from self._interface_metrics(switch_label, results[2])
        yield from self._evpn_metrics(switch_label, results[3])

        g = GaugeMetricFamily(
            "eos_up", "1 if the switch eAPI is reachable",
            labels=["switch"]
        )
        g.add_metric([switch_label], 1)
        yield g

    # ── BGP ──────────────────────────────────────────────────────────────────

    def _bgp_metrics(self, switch, result):
        session_up = GaugeMetricFamily(
            "eos_bgp_session_up",
            "1 if BGP session is Established",
            labels=["switch", "vrf", "neighbor", "peer_asn"],
        )
        pfx_rcvd = GaugeMetricFamily(
            "eos_bgp_prefixes_received",
            "Number of prefixes received from BGP neighbor",
            labels=["switch", "vrf", "neighbor"],
        )
        pfx_adv = GaugeMetricFamily(
            "eos_bgp_prefixes_advertised",
            "Number of prefixes advertised to BGP neighbor",
            labels=["switch", "vrf", "neighbor"],
        )

        for vrf_name, vrf_data in result.get("vrfs", {}).items():
            for neighbor_ip, peer in vrf_data.get("peers", {}).items():
                state      = peer.get("peerState", "")
                peer_asn   = str(peer.get("asn", peer.get("peerAsn", "")))
                is_up      = 1.0 if state == "Established" else 0.0
                pfx_in     = float(peer.get("prefixReceived", peer.get("msgRcvd", 0)))
                pfx_out    = float(peer.get("prefixSent",     peer.get("msgSent", 0)))

                session_up.add_metric([switch, vrf_name, neighbor_ip, peer_asn], is_up)
                pfx_rcvd.add_metric([switch, vrf_name, neighbor_ip], pfx_in)
                pfx_adv.add_metric([switch, vrf_name, neighbor_ip], pfx_out)

        yield session_up
        yield pfx_rcvd
        yield pfx_adv

    # ── MLAG ─────────────────────────────────────────────────────────────────

    def _mlag_metrics(self, switch, result):
        active = GaugeMetricFamily(
            "eos_mlag_active",
            "1 if MLAG domain state is active",
            labels=["switch"],
        )
        peer_link = GaugeMetricFamily(
            "eos_mlag_peer_link_up",
            "1 if MLAG peer-link is Up",
            labels=["switch"],
        )
        peer_alive = GaugeMetricFamily(
            "eos_mlag_peer_alive",
            "1 if MLAG peer is reachable",
            labels=["switch"],
        )

        state      = result.get("state", "disabled")
        pl_status  = result.get("peerLinkStatus", "").lower()
        pa_status  = result.get("peerIsUp", result.get("peerAlive", False))

        active.add_metric([switch],     1.0 if state == "active" else 0.0)
        peer_link.add_metric([switch],  1.0 if pl_status == "up" else 0.0)
        peer_alive.add_metric([switch], 1.0 if pa_status else 0.0)

        yield active
        yield peer_link
        yield peer_alive

    # ── Interfaces ───────────────────────────────────────────────────────────

    def _interface_metrics(self, switch, result):
        iface_up  = GaugeMetricFamily(
            "eos_interface_up",
            "1 if interface line protocol is up",
            labels=["switch", "interface"],
        )
        in_bytes  = GaugeMetricFamily(
            "eos_interface_in_octets_total",
            "Cumulative inbound octets",
            labels=["switch", "interface"],
        )
        out_bytes = GaugeMetricFamily(
            "eos_interface_out_octets_total",
            "Cumulative outbound octets",
            labels=["switch", "interface"],
        )
        in_err    = GaugeMetricFamily(
            "eos_interface_in_errors_total",
            "Cumulative inbound errors",
            labels=["switch", "interface"],
        )
        out_err   = GaugeMetricFamily(
            "eos_interface_out_errors_total",
            "Cumulative outbound errors",
            labels=["switch", "interface"],
        )

        for iface_name, iface_data in result.get("interfaces", {}).items():
            # Only fabric and management interfaces — skip internal
            if not (
                iface_name.startswith("Ethernet")
                or iface_name.startswith("Loopback")
                or iface_name.startswith("Management")
                or iface_name.startswith("Vxlan")
            ):
                continue

            lp_status = iface_data.get("lineProtocolStatus", "").lower()
            is_up     = 1.0 if lp_status == "up" else 0.0
            counters  = iface_data.get("interfaceCounters", {})

            iface_up.add_metric([switch, iface_name], is_up)
            in_bytes.add_metric([switch, iface_name],
                                float(counters.get("inOctets", 0)))
            out_bytes.add_metric([switch, iface_name],
                                 float(counters.get("outOctets", 0)))
            in_err.add_metric([switch, iface_name],
                              float(counters.get("inErrors", 0)))
            out_err.add_metric([switch, iface_name],
                               float(counters.get("outErrors", 0)))

        yield iface_up
        yield in_bytes
        yield out_bytes
        yield in_err
        yield out_err

    # ── EVPN ─────────────────────────────────────────────────────────────────

    def _evpn_metrics(self, switch, result):
        session_up = GaugeMetricFamily(
            "eos_evpn_session_up",
            "1 if EVPN BGP session is Established",
            labels=["switch", "neighbor", "peer_asn"],
        )
        routes_rcvd = GaugeMetricFamily(
            "eos_evpn_routes_received",
            "Number of EVPN routes received",
            labels=["switch", "neighbor"],
        )

        for vrf_data in result.get("vrfs", {}).values():
            for neighbor_ip, peer in vrf_data.get("peers", {}).items():
                state    = peer.get("peerState", "")
                peer_asn = str(peer.get("asn", peer.get("peerAsn", "")))
                pfx      = float(peer.get("prefixReceived", 0))

                session_up.add_metric(
                    [switch, neighbor_ip, peer_asn],
                    1.0 if state == "Established" else 0.0,
                )
                routes_rcvd.add_metric([switch, neighbor_ip], pfx)

        yield session_up
        yield routes_rcvd


# ── HTTP server that proxies ?target= ────────────────────────────────────────

from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from prometheus_client import exposition

class TargetHandler(BaseHTTPRequestHandler):
    """
    Handles /metrics?target=<ip>.
    Collects metrics for that single switch and returns them.
    """

    def log_message(self, fmt, *args):
        log.debug(f"HTTP {self.address_string()} {fmt % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return

        params = parse_qs(parsed.query)
        target_list = params.get("target", [])
        if not target_list:
            self.send_error(400, "Missing ?target= parameter")
            return

        ip = target_list[0]
        log.info(f"Scraping {ip}")

        # Collect for this single switch
        collector = EosCollector()
        # Override collect to only do this IP
        metrics = list(collector._collect_switch(ip))

        # Encode to Prometheus text format
        output = exposition.choose_encoder("text/plain; version=0.0.4")
        # Use a temporary registry
        from prometheus_client.core import CollectorRegistry as CR
        reg = CR()
        # Directly format the already-collected metric families
        body = ""
        for mf in metrics:
            body += exposition.core.generate_latest_target(mf)

        # Fallback: use generate_latest style
        if not body:
            lines = []
            for mf in metrics:
                lines.append(f"# HELP {mf.name} {mf.documentation}")
                lines.append(f"# TYPE {mf.name} {mf.type}")
                for sample in mf.samples:
                    label_str = ",".join(
                        f'{k}="{v}"' for k, v in sample.labels.items()
                    )
                    label_part = f"{{{label_str}}}" if label_str else ""
                    lines.append(f"{sample.name}{label_part} {sample.value}")
            body = "\n".join(lines) + "\n"

        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def render_metrics(metrics):
    """Format a list of MetricFamily objects into Prometheus text."""
    lines = []
    for mf in metrics:
        lines.append(f"# HELP {mf.name} {mf.documentation}")
        lines.append(f"# TYPE {mf.name} {mf.type}")
        for sample in mf.samples:
            label_str = ",".join(f'{k}="{v}"' for k, v in sample.labels.items())
            label_part = f"{{{label_str}}}" if label_str else ""
            lines.append(f"{sample.name}{label_part} {sample.value}")
    return "\n".join(lines) + "\n"


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug(f"HTTP {self.address_string()} {fmt % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return

        params    = parse_qs(parsed.query)
        target_ip = (params.get("target") or [""])[0]

        if not target_ip:
            self.send_error(400, "Missing ?target=")
            return

        log.info(f"Scraping EOS switch: {target_ip}")
        collector = EosCollector()
        metrics   = list(collector._collect_switch(target_ip))
        body      = render_metrics(metrics).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type",   "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    log.info(f"EOS Prometheus exporter starting on port {PORT}")
    log.info("Endpoint: http://localhost:%d/metrics?target=<switch-ip>", PORT)

    server = HTTPServer(("0.0.0.0", PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")
