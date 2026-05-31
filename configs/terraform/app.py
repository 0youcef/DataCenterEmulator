"""
vps/app.py — DC Fabric VPS Manager
────────────────────────────────────
Run:
    pip install fastapi uvicorn requests paramiko websockets
    uvicorn app:app --host 0.0.0.0 --port 8080

Fixed in this version
─────────────────────
• SPICE/console/delete: on-demand VMs stored with key 'vmid', vmCard read
  'vm_id' → always undefined. Both keys are now normalised to vm_id.
• noVNC blank/loading: jsdelivr @novnc/novnc has broken relative module
  imports. Replaced with esm.sh which bundles the whole package. Import
  is now a proper async await so errors surface instead of silently stalling.
• SSH '<resolving…>': fetchIP only updated the card, never the open SSH
  modal. Added pollIPForModal() which retries every 3 s while the modal
  is open and updates both the card and the modal.
• Credentials: cloud-init only runs if the template has a ci drive and
  only on first boot. Added ensure_cloudinit_drive() + after the VM boots,
  the QEMU guest agent is used to set the password directly — this works
  regardless of template cloud-init configuration.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Optional

import requests
import urllib3
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Paths ──────────────────────────────────────────────────────────────────────
PROJECT_ROOT  = Path(__file__).parent.parent
TF_DIR        = PROJECT_ROOT / "configs" / "terraform"
CONFIG_FILE   = Path(__file__).parent / "vps_config.json"
ONDEMAND_FILE = Path(__file__).parent / "vps_ondemand.json"
TFVARS_FILE   = TF_DIR / "terraform.tfvars"

_tf_lock = asyncio.Lock()
_vnc_sessions: dict[int, dict] = {}   # vmid → {port, ticket, px}


# =============================================================================
# Models
# =============================================================================

class VpsConfig(BaseModel):
    proxmox_endpoint:       str           = "https://pve.example.com:8006/"
    proxmox_insecure:       bool          = True
    proxmox_api_token:      Optional[str] = None
    proxmox_username:       Optional[str] = None
    proxmox_password:       Optional[str] = None
    proxmox_node_name:      str           = "pve"
    proxmox_template_vm_id: int           = 9000
    proxmox_datastore_id:   str           = "local-lvm"
    proxmox_bridge:         str           = "vmbr0"
    vm_name_prefix:         Optional[str] = None
    vm_tags:                list[str]     = ["vps", "terraform"]
    vm_started:             bool          = True
    vm_on_boot:             bool          = True
    vm_full_clone:          bool          = True
    vm_cpu_cores:           int           = 2
    vm_cpu_sockets:         int           = 1
    vm_cpu_type:            str           = "x86-64-v2-AES"
    vm_memory_mb:           int           = 2048
    vm_memory_floating_mb:  Optional[int] = None
    vm_disk_interface:      str           = "scsi0"
    vm_disk_size_gb:        int           = 20
    vm_disk_iothread:       bool          = True
    vm_network_model:       str           = "virtio"
    vm_network_vlan_id:     Optional[int] = None
    vm_ipv4_address:        str           = "dhcp"
    vm_ipv4_gateway:        Optional[str] = None
    vm_ci_username:         Optional[str] = None
    vm_ci_password:         Optional[str] = None
    vm_ssh_public_keys:     list[str]     = []
    vm_qemu_agent_enabled:  bool          = True


# =============================================================================
# Proxmox client
# =============================================================================

class ProxmoxClient:
    def __init__(self, cfg: dict):
        self.base     = cfg.get("proxmox_endpoint", "").rstrip("/")
        self.node     = cfg.get("proxmox_node_name", "pve")
        self.ds       = cfg.get("proxmox_datastore_id", "local-lvm")
        self.bridge   = cfg.get("proxmox_bridge", "vmbr0")
        self.insecure = cfg.get("proxmox_insecure", True)
        self.session  = requests.Session()
        self._csrf    = None
        self._ticket  = None
        self._token   = None
        self._authenticate(cfg)

    def _authenticate(self, cfg):
        token = cfg.get("proxmox_api_token")
        if token:
            self._token = token
            self.session.headers["Authorization"] = f"PVEAPIToken={token}"
            return
        user = cfg.get("proxmox_username")
        pw   = cfg.get("proxmox_password")
        if not user or not pw:
            raise ValueError("Provide api_token OR username+password")
        r = self.session.post(
            f"{self.base}/api2/json/access/ticket",
            data={"username": user, "password": pw},
            verify=not self.insecure, timeout=10,
        )
        r.raise_for_status()
        d = r.json()["data"]
        self._ticket = d["ticket"]
        self._csrf   = d["CSRFPreventionToken"]
        self.session.cookies.set("PVEAuthCookie", self._ticket)

    def _csrf_hdr(self):
        return {"CSRFPreventionToken": self._csrf} if self._csrf else {}

    def ws_headers(self):
        """Headers for the WebSocket upgrade (VNC proxy)."""
        if self._token:
            return {"Authorization": f"PVEAPIToken={self._token}"}
        if self._ticket:
            return {"Cookie": f"PVEAuthCookie={self._ticket}"}
        return {}

    def pve_host(self):
        return self.base.split("://", 1)[1].split(":")[0].split("/")[0]

    def pve_port(self):
        part = self.base.split("://", 1)[1]
        return part.split(":")[1].rstrip("/") if ":" in part else "8006"

    # ── low-level ─────────────────────────────────────────────────────────────

    def get(self, path):
        r = self.session.get(
            f"{self.base}/api2/json{path}",
            headers=self._csrf_hdr(), verify=not self.insecure, timeout=15,
        )
        r.raise_for_status()
        return r.json().get("data")

    def post_json(self, path, **data):
        r = self.session.post(
            f"{self.base}/api2/json{path}",
            headers=self._csrf_hdr(),
            json={k: v for k, v in data.items() if v is not None},
            verify=not self.insecure, timeout=30,
        )
        r.raise_for_status()
        return r.json().get("data")

    def post_form(self, path, **data):
        """
        POST as application/x-www-form-urlencoded.

        Required by: vncproxy, spiceproxy, status/start, status/stop,
        agent/set-user-password.  These endpoints silently ignore JSON bodies.
        """
        r = self.session.post(
            f"{self.base}/api2/json{path}",
            headers=self._csrf_hdr(),
            data={k: v for k, v in data.items() if v is not None},
            verify=not self.insecure, timeout=30,
        )
        r.raise_for_status()
        return r.json().get("data")

    def put(self, path, **data):
        r = self.session.put(
            f"{self.base}/api2/json{path}",
            headers=self._csrf_hdr(),
            json={k: v for k, v in data.items() if v is not None},
            verify=not self.insecure, timeout=15,
        )
        r.raise_for_status()
        return r.json().get("data")

    def delete(self, path):
        r = self.session.delete(
            f"{self.base}/api2/json{path}",
            headers=self._csrf_hdr(),
            verify=not self.insecure, timeout=15,
        )
        r.raise_for_status()
        return r.json().get("data")

    # ── VM lifecycle ──────────────────────────────────────────────────────────

    def next_vmid(self): return int(self.get("/cluster/nextid"))

    def wait_task(self, upid, timeout=180):
        node     = upid.split(":")[1] if ":" in upid else self.node
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = self.get(f"/nodes/{node}/tasks/{upid}/status")
            if s.get("status") == "stopped":
                return s
            time.sleep(2)
        raise TimeoutError(f"Task {upid} timed out")

    def clone_vm(self, src, new_vmid, name, full=True):
        return self.post_json(
            f"/nodes/{self.node}/qemu/{src}/clone",
            newid=new_vmid, name=name,
            full=1 if full else 0, storage=self.ds,
        )

    def vm_config(self, vmid):
        return self.get(f"/nodes/{self.node}/qemu/{vmid}/config")

    def set_vm_config(self, vmid, **kwargs):
        self.put(f"/nodes/{self.node}/qemu/{vmid}/config", **kwargs)

    def start_vm(self, vmid):
        return self.post_form(f"/nodes/{self.node}/qemu/{vmid}/status/start")

    def stop_vm(self, vmid):
        return self.post_form(f"/nodes/{self.node}/qemu/{vmid}/status/stop")

    def delete_vm(self, vmid):
        return self.delete(
            f"/nodes/{self.node}/qemu/{vmid}"
            "?purge=1&destroy-unreferenced-disks=1"
        )

    def vm_status(self, vmid):
        return self.get(f"/nodes/{self.node}/qemu/{vmid}/status/current")

    def agent_interfaces(self, vmid):
        return self.get(
            f"/nodes/{self.node}/qemu/{vmid}/agent/network-get-interfaces"
        )

    # ── Cloud-init helpers ────────────────────────────────────────────────────

    def ensure_cloudinit_drive(self, vmid: int) -> Optional[str]:
        """
        Check whether a cloud-init drive already exists.
        If not, add one on the first available ide/scsi slot.

        WHY: cloud-init configuration (ciuser, cipassword, sshkeys) is written
        into an ISO that Proxmox mounts as a virtual CD-ROM.  If the template
        was created without a cloud-init drive, Proxmox stores the config in
        the VM's metadata but never writes the ISO — so the guest OS never sees
        the credentials.  Adding the drive here makes cloud-init work even for
        templates that were created manually without it.
        """
        cfg = self.vm_config(vmid)
        # If any drive already references 'cloudinit' we're good
        for key, val in cfg.items():
            if isinstance(val, str) and "cloudinit" in val.lower():
                return key
        # Add one on the first free slot
        for slot in ["ide2", "ide1", "ide3", "scsi30", "scsi31"]:
            if slot not in cfg:
                self.set_vm_config(vmid, **{slot: f"{self.ds}:cloudinit"})
                return slot
        return None

    # ── QEMU agent password ───────────────────────────────────────────────────

    def wait_for_agent(self, vmid: int, timeout: int = 120) -> bool:
        """Poll until the QEMU guest agent responds or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.get(f"/nodes/{self.node}/qemu/{vmid}/agent/info")
                return True
            except Exception:
                time.sleep(3)
        return False

    def set_user_password_via_agent(
        self, vmid: int, username: str, password: str
    ) -> bool:
        """
        Set a user's password inside the running VM using the QEMU guest agent.

        WHY: Cloud-init only runs on first boot and only if the template has a
        cloud-init drive and cloud-init installed.  Many Proxmox templates skip
        one or both.  The QEMU agent call goes directly into the running OS
        (equivalent to 'passwd username') and works regardless of cloud-init.
        The password here is plaintext; Proxmox hashes it before passing it to
        the guest OS's shadow file.
        """
        try:
            self.post_form(
                f"/nodes/{self.node}/qemu/{vmid}/agent/set-user-password",
                username=username,
                password=password,
                crypted=0,
            )
            return True
        except Exception:
            return False

    # ── VNC ───────────────────────────────────────────────────────────────────

    def vncproxy(self, vmid: int) -> dict:
        """
        Create VNC proxy session.  MUST use form-data — the 'websocket' flag
        is only read from the form body, not from a JSON body.
        """
        return self.post_form(
            f"/nodes/{self.node}/qemu/{vmid}/vncproxy",
            websocket=1,
        )

    def vnc_ws_url(self, vmid: int, port: int, ticket: str) -> str:
        encoded = urllib.parse.quote(ticket, safe="")
        return (
            f"wss://{self.pve_host()}:{self.pve_port()}/api2/json"
            f"/nodes/{self.node}/qemu/{vmid}/vncwebsocket"
            f"?port={port}&vncticket={encoded}"
        )

    # ── SPICE ─────────────────────────────────────────────────────────────────

    def spice_proxy(self, vmid: int) -> dict:
        """
        Request SPICE ticket.  MUST use form-data so 'proxy' is read.
        Without 'proxy' the returned 'host' is 'localhost' or a cluster IP
        that the client cannot reach.
        """
        return self.post_form(
            f"/nodes/{self.node}/qemu/{vmid}/spiceproxy",
            proxy=self.pve_host(),
        )

    def build_vv(self, ticket: dict) -> str:
        """Build .vv file content for remote-viewer/virt-viewer."""
        host = ticket.get("host") or ""
        if not host or host in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "::"):
            host = self.pve_host()

        lines = [
            "[virt-viewer]",
            f"type={ticket.get('type','spice')}",
            f"host={host}",
            f"password={ticket.get('password','')}",
            "delete-this-file=1",
            "fullscreen=0",
            "enable-smartcard=0",
            "enable-usb-autoshare=0",
        ]
        tls  = ticket.get("tls-port") or ticket.get("tls_port")
        port = ticket.get("port")
        if tls and int(tls) > 0:
            lines.append(f"tls-port={tls}")
        elif port and int(port) > 0:
            lines.append(f"port={port}")
        if ticket.get("ca"):
            lines.append(f"ca={ticket['ca'].replace(chr(10),'\\n')}")
        if ticket.get("host-subject"):
            lines.append(f"host-subject={ticket['host-subject']}")
        return "\n".join(lines) + "\n"


# =============================================================================
# Helpers
# =============================================================================

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try: return json.loads(CONFIG_FILE.read_text())
        except Exception: pass
    return VpsConfig().model_dump()

def get_proxmox() -> ProxmoxClient:
    return ProxmoxClient(load_config())

def load_ondemand() -> list[dict]:
    if ONDEMAND_FILE.exists():
        try: return json.loads(ONDEMAND_FILE.read_text()).get("vms", [])
        except Exception: pass
    return []

def save_ondemand(vms):
    ONDEMAND_FILE.write_text(json.dumps({"vms": vms}, indent=2))

def add_ondemand(entry: dict):
    vms = load_ondemand()
    vms.append(entry)
    save_ondemand(vms)

def remove_ondemand(vmid: int):
    save_ondemand([v for v in load_ondemand() if v.get("vm_id") != vmid])

def resolve_vm_ips(px: ProxmoxClient, vmid: int) -> list[str]:
    try:
        data   = px.agent_interfaces(vmid)
        ifaces = data if isinstance(data, list) else data.get("result", [])
        ips = []
        for iface in ifaces:
            if iface.get("name","").startswith("lo"): continue
            for addr in iface.get("ip-addresses", []):
                if addr.get("ip-address-type") == "ipv4":
                    ip = addr.get("ip-address", "")
                    if ip and not ip.startswith("127."): ips.append(ip)
        if ips: return ips
    except Exception: pass
    try:
        cfg = px.vm_config(vmid)
        for part in cfg.get("ipconfig0","").split(","):
            if part.startswith("ip="):
                ip = part[3:].split("/")[0]
                if ip and ip != "dhcp": return [ip]
    except Exception: pass
    return []

def _hcl(v) -> str:
    if v is None: return "null"
    if isinstance(v, bool): return "true" if v else "false"
    if isinstance(v, (int,float)): return str(v)
    if isinstance(v, str): return f'"{v}"'
    if isinstance(v, list): return "[" + ", ".join(_hcl(i) for i in v) + "]"
    return f'"{v}"'

def write_tfvars(cfg: VpsConfig):
    lines = ['# Generated by DC Fabric VPS Manager',
             'ssot_config_path = "../../sots/config.py"']
    for k, v in cfg.model_dump().items():
        lines.append(f'{k} = {_hcl(v)}')
    TFVARS_FILE.write_text("\n".join(lines) + "\n")

def parse_tf_state() -> list[dict]:
    r = subprocess.run(["terraform","show","-json"],
                       cwd=str(TF_DIR), capture_output=True, text=True, timeout=30)
    if r.returncode != 0 or not r.stdout.strip(): return []
    try: state = json.loads(r.stdout)
    except Exception: return []
    resources = (state.get("values",{}).get("root_module",{}).get("resources",[]))
    vms = []
    for res in resources:
        if res.get("type") != "proxmox_virtual_environment_vm": continue
        v    = res.get("values",{})
        addr = res.get("address","")
        key  = addr.split('"')[1] if '"' in addr else addr
        cpu  = (v.get("cpu")            or [{}])[0]
        mem  = (v.get("memory")         or [{}])[0]
        disk = (v.get("disk")           or [{}])[0]
        init = (v.get("initialization") or [{}])[0]
        ip4  = ((init.get("ip_config") or [{}])[0].get("ipv4") or [{}])[0]
        vms.append({
            "key":       key,
            "name":      v.get("name","—"),
            "vm_id":     v.get("vm_id"),
            "node":      v.get("node_name","—"),
            "started":   v.get("started", False),
            "cpu":       cpu.get("cores","?"),
            "memory_mb": mem.get("dedicated","?"),
            "disk_gb":   disk.get("size","?"),
            "ip":        ip4.get("address","dhcp"),
            "source":    "terraform",
        })
    return sorted(vms, key=lambda x: x["key"])


# =============================================================================
# FastAPI
# =============================================================================

app = FastAPI(title="DC Fabric VPS Manager")


@app.get("/api/state")
def api_state():
    try:
        vms = parse_tf_state()
        return {"vms": vms, "count": len(vms)}
    except Exception as exc:
        return {"vms": [], "count": 0, "error": str(exc)}

@app.get("/api/config")
def api_get_config():
    return load_config()

@app.post("/api/config")
def api_save_config(cfg: VpsConfig):
    CONFIG_FILE.write_text(cfg.model_dump_json(indent=2))
    try: write_tfvars(cfg)
    except Exception as exc:
        raise HTTPException(500, f"Failed to write tfvars: {exc}")
    return {"ok": True}

@app.get("/api/stream/{operation}")
async def api_stream(operation: str, target: Optional[str] = None):
    allowed = {"init","plan","apply","destroy","destroy_target"}
    if operation not in allowed: raise HTTPException(400)
    if operation == "destroy_target":
        if not target: raise HTTPException(400, "target required")
        cmd = ["terraform","destroy","-auto-approve","-no-color",f"-target={target}"]
    else:
        cmd = {
            "init":    ["terraform","init"],
            "plan":    ["terraform","plan","-no-color"],
            "apply":   ["terraform","apply","-auto-approve","-no-color"],
            "destroy": ["terraform","destroy","-auto-approve","-no-color"],
        }[operation]

    async def generate():
        if _tf_lock.locked():
            yield "data: ERROR: Another operation is running.\n\n"
            yield "event: done\ndata: 1\n\n"
            return
        async with _tf_lock:
            yield f"data: $ {' '.join(cmd)}\n\n"
            env = {**os.environ,"TF_IN_AUTOMATION":"1"}
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(TF_DIR), env=env)
            except FileNotFoundError:
                yield "data: ERROR: terraform not found.\n\n"
                yield "event: done\ndata: 1\n\n"; return
            async for raw in proc.stdout:
                yield f"data: {raw.decode('utf-8',errors='replace').rstrip()}\n\n"
            rc = await proc.wait()
            yield f"data: ── exited {rc} ──\n\n"
            yield f"event: done\ndata: {rc}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


@app.get("/api/ondemand")
def api_ondemand():
    return {"vms": load_ondemand()}


@app.get("/api/vm/create-stream")
async def api_create_vm_stream(
    name: str,
    cpu_cores: int = 2,
    memory_mb: int = 2048,
    disk_gb: int = 20,
    ip: str = "dhcp",
    gateway: Optional[str] = None,
    start: bool = True,
):
    cfg = load_config()
    ci_user  = cfg.get("vm_ci_username") or "ubuntu"
    ci_pass  = cfg.get("vm_ci_password") or "changeme"

    async def generate():
        loop = asyncio.get_event_loop()

        yield f"data: Creating VM '{name}' …\n\n"
        yield f"data: Credentials: user={ci_user}  pass={ci_pass}\n\n"

        # 1. Connect to Proxmox
        try:
            px = await loop.run_in_executor(None, lambda: ProxmoxClient(cfg))
        except Exception as exc:
            yield f"data: [ERR] Connection failed — {exc}\n\n"
            yield "event: done\ndata: 1\n\n"; return

        # 2. Allocate VM ID
        try:
            new_vmid = await loop.run_in_executor(None, px.next_vmid)
            yield f"data: [OK] VM ID: {new_vmid}\n\n"
        except Exception as exc:
            yield f"data: [ERR] VM ID allocation failed — {exc}\n\n"
            yield "event: done\ndata: 1\n\n"; return

        # 3. Clone template
        tmpl = cfg.get("proxmox_template_vm_id", 9000)
        yield f"data: Cloning template {tmpl} → {new_vmid} …\n\n"
        try:
            upid = await loop.run_in_executor(
                None, lambda: px.clone_vm(tmpl, new_vmid, name,
                                          full=cfg.get("vm_full_clone", True)))
            res  = await loop.run_in_executor(None, lambda: px.wait_task(upid))
            if res.get("exitstatus") != "OK":
                raise RuntimeError(res.get("exitstatus","error"))
            yield "data: [OK] Clone complete.\n\n"
        except Exception as exc:
            yield f"data: [ERR] Clone failed — {exc}\n\n"
            yield "event: done\ndata: 1\n\n"; return

        # 4. Ensure cloud-init drive exists, then set cloud-init config
        yield "data: Configuring cloud-init …\n\n"
        try:
            slot = await loop.run_in_executor(
                None, lambda: px.ensure_cloudinit_drive(new_vmid))
            if slot:
                yield f"data:   cloud-init drive on {slot}\n\n"
            else:
                yield "data:   cloud-init drive already present.\n\n"

            ip_config = ("ip=dhcp" if ip == "dhcp"
                         else f"ip={ip}" + (f",gw={gateway}" if gateway else ""))
            ci_kwargs = dict(
                cores    = cpu_cores,
                sockets  = 1,
                memory   = memory_mb,
                agent    = "enabled=1",
                ipconfig0= ip_config,
                ciuser   = ci_user,
                cipassword = ci_pass,
            )
            if cfg.get("vm_ssh_public_keys"):
                ci_kwargs["sshkeys"] = "\n".join(cfg["vm_ssh_public_keys"])

            await loop.run_in_executor(
                None, lambda: px.set_vm_config(new_vmid, **ci_kwargs))
            yield "data: [OK] Cloud-init config applied.\n\n"
        except Exception as exc:
            yield f"data: [WARN] Cloud-init config issue — {exc}\n\n"

        # 5. Start VM
        if start:
            yield "data: Starting VM …\n\n"
            try:
                upid = await loop.run_in_executor(None, lambda: px.start_vm(new_vmid))
                if upid:
                    await loop.run_in_executor(None, lambda: px.wait_task(upid))
                yield "data: [OK] VM started.\n\n"
            except Exception as exc:
                yield f"data: [WARN] Start issue — {exc}\n\n"

            # 6. Wait for QEMU agent, then set password directly
            #    This is the most reliable method: it works even when cloud-init
            #    is not installed or the template has no default credentials.
            yield "data: Waiting for QEMU guest agent (up to 90 s) …\n\n"
            agent_ok = await loop.run_in_executor(
                None, lambda: px.wait_for_agent(new_vmid, timeout=90))

            if agent_ok:
                yield "data: [OK] QEMU agent ready. Setting password via agent …\n\n"
                ok = await loop.run_in_executor(
                    None, lambda: px.set_user_password_via_agent(
                        new_vmid, ci_user, ci_pass))
                if ok:
                    yield f"data: [OK] Password set for user '{ci_user}'\n\n"
                else:
                    yield "data: [WARN] Agent password set failed (user may not exist in VM).\n\n"
                    yield "data:   The cloud-init password will be used on next boot.\n\n"
            else:
                yield "data: [WARN] QEMU agent not responding — cloud-init password used.\n\n"
                yield "data:   Make sure qemu-guest-agent is installed in your template.\n\n"

        # 7. Persist on-demand record
        # FIX: key is vm_id (not vmid) so vmCard can read it uniformly
        add_ondemand({
            "vm_id":     new_vmid,
            "name":      name,
            "cpu":       cpu_cores,
            "memory_mb": memory_mb,
            "disk_gb":   disk_gb,
            "ip":        ip,
            "started":   start,
            "source":    "api",
            "ci_user":   ci_user,
            "created":   time.strftime("%Y-%m-%dT%H:%M:%S"),
        })

        yield f"data: \n\n"
        yield f"data: ════════════════════════════════\n\n"
        yield f"data:  VM '{name}'  ID {new_vmid}  is ready\n\n"
        yield f"data:  User:     {ci_user}\n\n"
        yield f"data:  Password: {ci_pass}\n\n"
        yield f"data: ════════════════════════════════\n\n"
        yield f"event: vmid\ndata: {new_vmid}\n\n"
        yield "event: done\ndata: 0\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


@app.get("/api/vm/{vmid}/ip")
def api_vm_ip(vmid: int):
    try:
        px  = get_proxmox()
        ips = resolve_vm_ips(px, vmid)
        return {"vmid": vmid, "ips": ips}
    except Exception as exc:
        raise HTTPException(502, str(exc))


@app.delete("/api/vm/{vmid}")
def api_vm_delete(vmid: int):
    try:
        px = get_proxmox()
        try:
            if px.vm_status(vmid).get("status") == "running":
                upid = px.stop_vm(vmid)
                if upid: px.wait_task(upid, timeout=60)
                time.sleep(2)
        except Exception: pass
        px.delete_vm(vmid)
    except Exception as exc:
        raise HTTPException(502, str(exc))
    remove_ondemand(vmid)
    _vnc_sessions.pop(vmid, None)
    return {"ok": True, "vmid": vmid}


@app.get("/api/vm/{vmid}/spice")
def api_vm_spice(vmid: int):
    try:
        px  = get_proxmox()
        t   = px.spice_proxy(vmid)
        vv  = px.build_vv(t)
    except Exception as exc:
        raise HTTPException(502, f"SPICE failed: {exc}")
    return Response(content=vv, media_type="application/x-virt-viewer",
                    headers={"Content-Disposition":
                             f'attachment; filename="vm-{vmid}.vv"'})


@app.get("/api/vm/{vmid}/vnc-ticket")
def api_vnc_ticket(vmid: int):
    try:
        px  = get_proxmox()
        vnc = px.vncproxy(vmid)
    except Exception as exc:
        raise HTTPException(502, f"VNC proxy failed: {exc}")
    port   = vnc.get("port") or vnc.get("vnc-port")
    ticket = vnc.get("ticket")
    if not port or not ticket:
        raise HTTPException(502, f"Bad vncproxy response: {vnc}")
    _vnc_sessions[vmid] = {"port": int(port), "ticket": ticket, "px": px}
    return {"ticket": ticket}


@app.websocket("/api/vm/{vmid}/vnc-ws")
async def vnc_ws_proxy(ws: WebSocket, vmid: int):
    """
    Proxies browser noVNC ↔ Proxmox VNC WebSocket.

    WHY proxy server-side:
    Proxmox's noVNC URL requires a valid PVEAuthCookie in the browser.
    By proxying here we use our server-side credentials; the browser only
    needs to connect to us and passes the VNC ticket as the RFB password.
    Also works when Proxmox is on a private network the browser can't reach.
    """
    import ssl as _ssl
    try:
        import websockets as ws_lib
    except ImportError:
        await ws.accept()
        await ws.send_bytes(b"ERROR: pip install websockets")
        await ws.close(); return

    await ws.accept(subprotocol="binary")

    session = _vnc_sessions.get(vmid)
    if not session:
        await ws.send_bytes(b"ERROR: call /api/vm/<id>/vnc-ticket first")
        await ws.close(); return

    px     = session["px"]
    port   = session["port"]
    ticket = session["ticket"]
    url    = px.vnc_ws_url(vmid, port, ticket)

    ssl_ctx = _ssl.create_default_context()
    if px.insecure:
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode    = _ssl.CERT_NONE

    try:
        async with ws_lib.connect(
            url,
            ssl=ssl_ctx,
            additional_headers=px.ws_headers(),
            subprotocols=["binary"],
            ping_interval=None,
            max_size=16*1024*1024,
            open_timeout=15,
        ) as pve_ws:
            stop = asyncio.Event()

            async def b2p():
                try:
                    while not stop.is_set():
                        data = await ws.receive_bytes()
                        await pve_ws.send(data)
                except Exception: pass
                finally: stop.set()

            async def p2b():
                try:
                    async for msg in pve_ws:
                        if isinstance(msg, bytes): await ws.send_bytes(msg)
                        else:                      await ws.send_text(msg)
                except Exception: pass
                finally: stop.set()

            await asyncio.gather(b2p(), p2b())
    except Exception as exc:
        try: await ws.send_bytes(f"ERROR: {exc}".encode()); await ws.close()
        except Exception: pass
    finally:
        _vnc_sessions.pop(vmid, None)


@app.websocket("/api/vm/{vmid}/terminal")
async def vm_terminal(ws: WebSocket, vmid: int, ip: str, port: int = 22):
    await ws.accept()
    try:
        import paramiko
    except ImportError:
        await ws.send_text(json.dumps({"t":"e","d":"pip install paramiko"}))
        await ws.close(); return

    cfg      = load_config()
    ssh_user = cfg.get("vm_ci_username") or "ubuntu"
    ssh_pass = cfg.get("vm_ci_password") or ""
    loop     = asyncio.get_event_loop()
    out_q: asyncio.Queue = asyncio.Queue()
    ssh_obj  = [None]; chan_obj = [None]; stop = threading.Event()

    def _connect():
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(ip, port=port, username=ssh_user, password=ssh_pass,
                    timeout=15, banner_timeout=15)
        chan = ssh.invoke_shell(term="xterm-256color", width=220, height=50)
        chan.settimeout(0.05)
        ssh_obj[0]=ssh; chan_obj[0]=chan

    try:
        await loop.run_in_executor(None, _connect)
    except Exception as exc:
        await ws.send_text(json.dumps({"t":"e","d":f"SSH failed: {exc}"}))
        await ws.close(); return

    await ws.send_text(json.dumps({"t":"o","d":"\r\n\x1b[32mConnected.\x1b[0m\r\n"}))

    def _read():
        chan = chan_obj[0]
        while not stop.is_set():
            try:
                data = chan.recv(8192)
                if not data: break
                asyncio.run_coroutine_threadsafe(
                    out_q.put(data.decode("utf-8",errors="replace")), loop)
            except Exception: pass
            if chan.exit_status_ready():
                asyncio.run_coroutine_threadsafe(out_q.put(None), loop); break

    threading.Thread(target=_read, daemon=True).start()

    async def _out():
        while True:
            chunk = await out_q.get()
            if chunk is None:
                await ws.send_text(json.dumps({"t":"x","c":0})); break
            await ws.send_text(json.dumps({"t":"o","d":chunk}))

    async def _inp():
        try:
            async for msg in ws.iter_text():
                m=json.loads(msg); chan=chan_obj[0]
                if not chan: continue
                if m.get("t")=="i":
                    await loop.run_in_executor(None, chan.send, m.get("d",""))
                elif m.get("t")=="r":
                    await loop.run_in_executor(
                        None, lambda: chan.resize_pty(
                            width=int(m.get("cols",80)),
                            height=int(m.get("rows",24))))
        except WebSocketDisconnect: pass
        finally:
            stop.set()
            if chan_obj[0]: chan_obj[0].close()
            if ssh_obj[0]:  ssh_obj[0].close()

    await asyncio.gather(_out(), _inp())


# =============================================================================
# HTML
# =============================================================================

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DC Fabric — VPS Manager</title>
<link  rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.3.0/css/xterm.css">
<script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.3.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.8.0/lib/addon-fit.js"></script>
<style>
:root{--bg:#0f172a;--sur:#1e293b;--bdr:#334155;--acc:#3b82f6;--ok:#22c55e;
      --err:#ef4444;--warn:#f59e0b;--txt:#f1f5f9;--mut:#94a3b8;
      --r:8px;--f:'Segoe UI',system-ui,sans-serif;--m:'Cascadia Code','Fira Code',monospace}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:var(--f);font-size:14px;
     min-height:100vh;display:flex;flex-direction:column}
.hdr{display:flex;align-items:center;justify-content:space-between;
     padding:0 24px;height:56px;background:var(--sur);border-bottom:1px solid var(--bdr);
     position:sticky;top:0;z-index:100}
.brand{display:flex;align-items:center;gap:10px;font-size:16px;font-weight:600}
.hex{font-size:22px;color:var(--acc)}
.btn{display:inline-flex;align-items:center;gap:6px;padding:7px 14px;border:none;
     border-radius:var(--r);font-size:13px;font-weight:500;cursor:pointer;
     transition:opacity .15s,transform .1s;white-space:nowrap}
.btn:hover{opacity:.85}.btn:active{transform:scale(.97)}
.bp{background:var(--acc);color:#fff}.bs{background:var(--ok);color:#000}
.bd{background:var(--err);color:#fff}.bw{background:var(--warn);color:#000}
.bg{background:transparent;color:var(--txt);border:1px solid var(--bdr)}
.bg:hover{background:var(--sur)}.sm{padding:4px 10px;font-size:12px}
.btn:disabled{opacity:.4;cursor:not-allowed;pointer-events:none}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;
       padding:16px 24px;border-bottom:1px solid var(--bdr)}
.sc{background:var(--sur);border:1px solid var(--bdr);border-radius:var(--r);padding:14px 18px}
.sl{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}
.sv{font-size:24px;font-weight:700;margin-top:4px}.sv.ok{color:var(--ok)}
.abar{display:flex;align-items:center;gap:10px;padding:12px 24px;
      border-bottom:1px solid var(--bdr);flex-wrap:wrap}
.sep{width:1px;height:24px;background:var(--bdr);margin:0 4px}
#opi{margin-left:auto;font-size:13px;color:var(--mut);display:flex;align-items:center;gap:8px}
.spin{width:14px;height:14px;border:2px solid var(--bdr);border-top-color:var(--acc);
      border-radius:50%;animation:sp .7s linear infinite;display:none}
@keyframes sp{to{transform:rotate(360deg)}}
.sec{padding:20px 24px}
.sech{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
.stl{font-size:13px;font-weight:600;color:var(--mut);text-transform:uppercase;letter-spacing:.06em}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:16px}
.empty{grid-column:1/-1;text-align:center;padding:48px;color:var(--mut)}
.empty h3{font-size:16px;margin-bottom:6px}
.vmc{background:var(--sur);border:1px solid var(--bdr);border-radius:var(--r);
     overflow:hidden;transition:border-color .2s}
.vmc:hover{border-color:var(--acc)}
.vmh{display:flex;justify-content:space-between;align-items:center;
     padding:12px 16px 8px;border-bottom:1px solid var(--bdr)}
.vmn{font-weight:600;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:160px}
.badge{font-size:11px;font-weight:600;padding:3px 8px;border-radius:99px;text-transform:uppercase}
.badge.on{background:rgba(34,197,94,.15);color:var(--ok)}
.badge.off{background:rgba(148,163,184,.1);color:var(--mut)}
.vmb{padding:14px 16px}
.specs{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px}
.spec{background:rgba(255,255,255,.04);border-radius:6px;padding:8px;text-align:center}
.sv2{font-size:15px;font-weight:700}
.sl2{font-size:10px;color:var(--mut);margin-top:2px;text-transform:uppercase}
.det{display:flex;justify-content:space-between;font-size:12px;color:var(--mut);margin-top:5px}
.det span:last-child{color:var(--txt);font-family:var(--m)}
.ipr{display:flex;align-items:center;gap:6px;margin-top:6px;min-height:20px}
.ipl{font-family:var(--m);font-size:12px;color:var(--txt)}
.ipb{font-size:10px;padding:2px 6px;border-radius:4px;background:rgba(34,197,94,.15);color:var(--ok)}
.ips{width:10px;height:10px;border:2px solid var(--bdr);border-top-color:var(--ok);
     border-radius:50%;animation:sp .7s linear infinite;flex-shrink:0}
.vmf{display:flex;align-items:center;gap:5px;padding:10px 16px;
     border-top:1px solid var(--bdr);background:rgba(0,0,0,.15);flex-wrap:wrap}
.tag{background:rgba(59,130,246,.15);color:var(--acc);font-size:11px;
     padding:2px 7px;border-radius:4px;font-weight:500}
/* log terminal */
.lterm{border-top:1px solid var(--bdr);background:#0a0e1a;
       display:flex;flex-direction:column;height:220px;flex-shrink:0}
.lhdr{display:flex;justify-content:space-between;align-items:center;
      padding:8px 16px;border-bottom:1px solid var(--bdr);background:var(--sur)}
.lhdr span{font-size:12px;font-weight:600;color:var(--mut)}
.dots{display:flex;gap:6px}
.dot{width:10px;height:10px;border-radius:50%}
.dr{background:#ff5f57}.dy{background:#ffbd2e}.dg{background:#28ca41}
.lbody{flex:1;overflow-y:auto;padding:10px 16px;font-family:var(--m);font-size:12.5px;line-height:1.6}
.ll{white-space:pre-wrap;word-break:break-all}
.ll.p{color:var(--acc)}.ll.e{color:var(--err)}.ll.k{color:var(--ok)}.ll.m{color:var(--mut)}
/* overlays */
.ov{position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:200;
    display:none;justify-content:center;align-items:flex-start;padding:40px 16px;overflow-y:auto}
.ov.open{display:flex}
.modal{background:var(--sur);border:1px solid var(--bdr);border-radius:12px;width:100%;overflow:hidden}
.sm2{max-width:440px}.md{max-width:640px}.xl{max-width:980px}
.mh{display:flex;justify-content:space-between;align-items:center;
    padding:16px 24px;border-bottom:1px solid var(--bdr)}
.mh h2{font-size:16px;font-weight:600}
.mc{background:none;border:none;color:var(--mut);font-size:20px;
    cursor:pointer;padding:4px 8px;border-radius:4px;line-height:1}
.mc:hover{background:rgba(255,255,255,.08);color:var(--txt)}
.mb{padding:22px}
.mf{display:flex;justify-content:flex-end;gap:10px;
    padding:14px 24px;border-top:1px solid var(--bdr);background:rgba(0,0,0,.15)}
.tabs{display:flex;border-bottom:1px solid var(--bdr);padding:0 24px;gap:4px}
.tab{background:none;border:none;color:var(--mut);font-size:13px;font-weight:500;
     padding:12px 14px;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px}
.tab.a{color:var(--acc);border-bottom-color:var(--acc)}.tab:hover{color:var(--txt)}
.tp{display:none;padding:24px}.tp.a{display:block}
.fg{display:flex;flex-direction:column;gap:6px}
.fg2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.fg2.w1{grid-template-columns:1fr}.full{grid-column:1/-1}
label{font-size:11px;font-weight:500;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
input,select,textarea{background:var(--bg);border:1px solid var(--bdr);border-radius:6px;
  color:var(--txt);font-family:inherit;font-size:13px;padding:9px 12px;width:100%;transition:border-color .15s}
input:focus,select:focus{outline:none;border-color:var(--acc)}
textarea{resize:vertical;min-height:80px;font-family:var(--m);font-size:12px}
.sh{font-size:11px;font-weight:600;color:var(--mut);text-transform:uppercase;letter-spacing:.06em;margin:4px 0 12px}
.tr{display:flex;align-items:center;justify-content:space-between;
    padding:10px 0;border-bottom:1px solid var(--bdr)}.tr:last-child{border-bottom:none}
.trl{font-size:13px}.trl small{display:block;color:var(--mut);font-size:11px;margin-top:2px}
.sw{position:relative;display:inline-block;width:40px;height:22px}
.sw input{opacity:0;width:0;height:0}
.sl3{position:absolute;inset:0;cursor:pointer;background:var(--bdr);border-radius:22px;transition:.2s}
.sl3::before{content:"";position:absolute;width:16px;height:16px;left:3px;top:3px;
             background:#fff;border-radius:50%;transition:.2s}
.sw input:checked+.sl3{background:var(--acc)}
.sw input:checked+.sl3::before{transform:translateX(18px)}
.anote{background:rgba(59,130,246,.1);border:1px solid rgba(59,130,246,.25);
       border-radius:6px;padding:10px 14px;font-size:12px;color:var(--mut);margin-bottom:14px}
/* SSH creds box */
.ssh-box{background:var(--bg);border:1px solid var(--bdr);border-radius:6px;
         padding:14px 16px;font-family:var(--m);font-size:13px;
         display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:10px}
.ssh-box.ok{border-color:rgba(34,197,94,.4);color:var(--ok)}
/* noVNC */
#vncc{width:100%;height:520px;background:#000;position:relative;overflow:hidden;border-radius:6px}
#vncc canvas{width:100%!important;height:100%!important}
.vov{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
     flex-direction:column;gap:12px;background:rgba(0,0,0,.7)}
.vov p{color:var(--mut);font-size:13px;text-align:center;max-width:340px}
.vov .espin{width:28px;height:28px;border:3px solid var(--bdr);border-top-color:var(--acc);
            border-radius:50%;animation:sp .7s linear infinite}
/* xterm */
#xtc{width:100%;height:420px;background:#000;border-radius:6px;overflow:hidden}
@media(max-width:700px){.stats{grid-template-columns:repeat(2,1fr)}.fg2{grid-template-columns:1fr}}
</style>
</head>
<body>

<header class="hdr">
  <div class="brand"><span class="hex">⬡</span> DC Fabric <span style="color:var(--mut);font-weight:400;margin:0 6px">/</span> VPS Manager</div>
  <div style="display:flex;gap:10px">
    <button class="btn bg sm" onclick="refreshAll()">↻ Refresh</button>
    <button class="btn bp sm" onclick="openOv('cfg-ov')">⚙ Configure</button>
  </div>
</header>

<div class="stats">
  <div class="sc"><div class="sl">Terraform VMs</div><div class="sv" id="s-tf">—</div></div>
  <div class="sc"><div class="sl">On-Demand VMs</div><div class="sv ok" id="s-od">—</div></div>
  <div class="sc"><div class="sl">Proxmox Node</div><div class="sv" id="s-nd" style="font-size:15px;margin-top:8px">—</div></div>
  <div class="sc"><div class="sl">Template VM ID</div><div class="sv" id="s-tm">—</div></div>
</div>

<div class="abar">
  <button class="btn bg" id="bi" onclick="runTF('init')">⬇ Init</button>
  <button class="btn bg" id="bp2" onclick="runTF('plan')">📋 Plan</button>
  <div class="sep"></div>
  <button class="btn bp" id="ba" onclick="runTF('apply')">▶ Apply Fleet</button>
  <button class="btn bd" id="bde" onclick="confirmDestroyAll()">✕ Destroy Fleet</button>
  <div class="sep"></div>
  <button class="btn bs" onclick="openOv('new-ov')">＋ New VM</button>
  <span id="opi"><span class="spin" id="spn"></span><span id="opl">Idle</span></span>
</div>

<div class="sec">
  <div class="sech"><span class="stl">Terraform Fleet</span><span id="tfc" style="font-size:12px;color:var(--mut)"></span></div>
  <div class="grid" id="tf-g"><div class="empty"><h3>No state</h3><p>Run Init → Apply.</p></div></div>
</div>

<div class="sec" style="border-top:1px solid var(--bdr)">
  <div class="sech"><span class="stl">On-Demand VMs</span><span id="odc" style="font-size:12px;color:var(--mut)"></span></div>
  <div class="grid" id="od-g"><div class="empty"><h3>No on-demand VMs</h3><p>Click <strong>＋ New VM</strong>.</p></div></div>
</div>

<div class="lterm">
  <div class="lhdr">
    <div class="dots"><div class="dot dr"></div><div class="dot dy"></div><div class="dot dg"></div></div>
    <span>Log</span>
    <button class="btn bg sm" onclick="clearLog()">Clear</button>
  </div>
  <div class="lbody" id="log"><div class="ll m">— Waiting —</div></div>
</div>

<!-- Configure modal -->
<div class="ov" id="cfg-ov" onclick="bgClose(event,'cfg-ov')">
 <div class="modal md" onclick="event.stopPropagation()">
  <div class="mh"><h2>Proxmox Configuration</h2><button class="mc" onclick="closeOv('cfg-ov')">✕</button></div>
  <div class="tabs">
    <button class="tab a" onclick="swT('cfg','conn',this)">Connection</button>
    <button class="tab"   onclick="swT('cfg','comp',this)">Compute</button>
    <button class="tab"   onclick="swT('cfg','net',this)">Network</button>
    <button class="tab"   onclick="swT('cfg','ci',this)">Cloud-Init</button>
  </div>
  <div class="tp a" id="cfg-conn">
    <div class="sh">Proxmox API</div>
    <div class="fg2">
      <div class="fg full"><label>Endpoint</label><input id="c-ep" type="url" placeholder="https://pve.example.com:8006/"></div>
      <div class="fg"><label>Node</label><input id="c-nd" placeholder="pve"></div>
      <div class="fg"><label>Template VM ID</label><input id="c-tm" type="number" placeholder="9000"></div>
      <div class="fg"><label>Datastore</label><input id="c-ds" placeholder="local-lvm"></div>
      <div class="fg"><label>Bridge</label><input id="c-br" placeholder="vmbr0"></div>
    </div>
    <div style="height:14px"></div>
    <div class="sh">Authentication (choose one)</div>
    <div class="anote">API token <em>or</em> username+password. Leave the other empty.</div>
    <div class="fg2">
      <div class="fg full"><label>API Token</label><input id="c-tk" type="password" placeholder="user@realm!tokenid=secret"></div>
      <div class="fg"><label>Username</label><input id="c-us" placeholder="terraform@pve"></div>
      <div class="fg"><label>Password</label><input id="c-pw" type="password"></div>
    </div>
    <div style="height:14px"></div>
    <div class="sh">Lifecycle</div>
    <div class="tr"><div class="trl">Skip TLS verification<small>Allow self-signed cert</small></div><label class="sw"><input type="checkbox" id="c-ins" checked><span class="sl3"></span></label></div>
    <div class="tr"><div class="trl">Start VMs on creation</div><label class="sw"><input type="checkbox" id="c-st" checked><span class="sl3"></span></label></div>
    <div class="tr"><div class="trl">Start on host boot</div><label class="sw"><input type="checkbox" id="c-bt" checked><span class="sl3"></span></label></div>
    <div class="tr"><div class="trl">Full clone</div><label class="sw"><input type="checkbox" id="c-fc" checked><span class="sl3"></span></label></div>
  </div>
  <div class="tp" id="cfg-comp">
    <div class="fg2">
      <div class="fg"><label>Cores</label><input id="c-co" type="number" min="1" value="2"></div>
      <div class="fg"><label>Sockets</label><input id="c-so" type="number" min="1" value="1"></div>
      <div class="fg full"><label>CPU Type</label><select id="c-ct"><option value="x86-64-v2-AES">x86-64-v2-AES</option><option value="host">host</option><option value="kvm64">kvm64</option></select></div>
      <div class="fg"><label>RAM (MB)</label><input id="c-me" type="number" step="512" value="2048"></div>
      <div class="fg"><label>Floating RAM (MB)</label><input id="c-mf" type="number" step="512" placeholder="null"></div>
    </div>
    <div style="height:12px"></div>
    <div class="fg2">
      <div class="fg"><label>Disk Interface</label><select id="c-di"><option value="scsi0">scsi0</option><option value="virtio0">virtio0</option><option value="sata0">sata0</option></select></div>
      <div class="fg"><label>Disk (GB)</label><input id="c-dg" type="number" min="8" value="20"></div>
    </div>
    <div class="tr" style="margin-top:12px"><div class="trl">IO thread</div><label class="sw"><input type="checkbox" id="c-it" checked><span class="sl3"></span></label></div>
    <div class="tr"><div class="trl">QEMU guest agent</div><label class="sw"><input type="checkbox" id="c-ag" checked><span class="sl3"></span></label></div>
  </div>
  <div class="tp" id="cfg-net">
    <div class="fg2">
      <div class="fg"><label>NIC Model</label><select id="c-ni"><option value="virtio">virtio</option><option value="e1000">e1000</option></select></div>
      <div class="fg"><label>VLAN ID</label><input id="c-vl" type="number" placeholder="null"></div>
      <div class="fg full"><label>IPv4</label><input id="c-ip" placeholder="dhcp"></div>
      <div class="fg full"><label>Gateway</label><input id="c-gw" placeholder="null"></div>
    </div>
  </div>
  <div class="tp" id="cfg-ci">
    <div class="anote" style="border-color:rgba(34,197,94,.3);background:rgba(34,197,94,.07)">
      These credentials are applied via <strong>QEMU guest agent</strong> after boot — they work
      regardless of whether the template has cloud-init configured.
    </div>
    <div class="fg2">
      <div class="fg"><label>Username</label><input id="c-cu" placeholder="ubuntu"></div>
      <div class="fg"><label>Password</label><input id="c-cp" type="password" placeholder="changeme"></div>
    </div>
    <div style="height:12px"></div>
    <div class="fg"><label>SSH Public Keys (one per line)</label><textarea id="c-ky" placeholder="ssh-ed25519 AAAA..."></textarea></div>
    <div style="height:12px"></div>
    <div class="fg2">
      <div class="fg"><label>Name Prefix</label><input id="c-pf" placeholder="auto from SSOT"></div>
      <div class="fg"><label>Tags (comma-sep)</label><input id="c-tg" value="vps,terraform"></div>
    </div>
  </div>
  <div class="mf">
    <button class="btn bg" onclick="closeOv('cfg-ov')">Cancel</button>
    <button class="btn bp" onclick="saveCfg()">Save & Write tfvars</button>
  </div>
 </div>
</div>

<!-- New VM modal -->
<div class="ov" id="new-ov" onclick="bgClose(event,'new-ov')">
 <div class="modal sm2" onclick="event.stopPropagation()">
  <div class="mh"><h2>Create On-Demand VM</h2><button class="mc" onclick="closeOv('new-ov')">✕</button></div>
  <div class="mb">
    <div class="anote">Clones the configured template directly via Proxmox API.</div>
    <div class="fg2 w1" style="gap:12px">
      <div class="fg"><label>VM Name</label><input id="nv-n" placeholder="my-vm-01"></div>
      <div class="fg2">
        <div class="fg"><label>CPU Cores</label><input id="nv-c" type="number" min="1" value="2"></div>
        <div class="fg"><label>RAM (MB)</label><input id="nv-m" type="number" step="512" value="2048"></div>
      </div>
      <div class="fg"><label>Disk (GB)</label><input id="nv-d" type="number" min="8" value="20"></div>
      <div class="fg2">
        <div class="fg"><label>IPv4</label><input id="nv-i" value="dhcp"></div>
        <div class="fg"><label>Gateway</label><input id="nv-g" placeholder="null"></div>
      </div>
    </div>
    <div class="tr" style="margin-top:14px"><div class="trl">Start immediately</div><label class="sw"><input type="checkbox" id="nv-st" checked><span class="sl3"></span></label></div>
  </div>
  <div class="mf">
    <button class="btn bg" onclick="closeOv('new-ov')">Cancel</button>
    <button class="btn bs" onclick="createVM()">＋ Create VM</button>
  </div>
 </div>
</div>

<!-- SSH modal -->
<div class="ov" id="ssh-ov" onclick="bgClose(event,'ssh-ov')">
 <div class="modal sm2" onclick="event.stopPropagation()">
  <div class="mh"><h2>SSH Access</h2><button class="mc" onclick="closeSsh()">✕</button></div>
  <div class="mb">
    <div id="ssh-ip-status" style="font-size:12px;color:var(--mut);margin-bottom:8px;min-height:18px"></div>
    <div class="ssh-box" id="ssh-box">
      <span id="ssh-cmd">ssh ubuntu@…</span>
      <button class="btn bg sm" onclick="copySSH()">Copy</button>
    </div>
    <div style="margin-top:12px;font-size:12px;color:var(--mut)" id="ssh-creds"></div>
  </div>
  <div class="mf">
    <button class="btn bg" onclick="closeSsh()">Close</button>
    <button class="btn bp" id="ssh-term-btn" onclick="openTerm()">Open Terminal</button>
  </div>
 </div>
</div>

<!-- noVNC Console modal -->
<div class="ov" id="vnc-ov" onclick="bgClose(event,'vnc-ov')">
 <div class="modal xl" onclick="event.stopPropagation()">
  <div class="mh">
    <h2>Console — <span id="vnc-ttl">—</span></h2>
    <div style="display:flex;gap:8px;align-items:center">
      <span id="vnc-st" style="font-size:12px;color:var(--mut)"></span>
      <button class="btn bg sm" onclick="sendCAD()">Ctrl+Alt+Del</button>
      <button class="mc" onclick="closeVnc()">✕</button>
    </div>
  </div>
  <div style="background:#000;padding:0">
    <div id="vncc">
      <div class="vov" id="vov">
        <div class="espin"></div>
        <p id="vnc-msg">Initialising…</p>
      </div>
    </div>
  </div>
  <div class="mf" style="background:rgba(0,0,0,.4)">
    <span style="font-size:12px;color:var(--mut)">
      For desktop VMs: <strong>SPICE</strong> gives better performance.
      Install: <code>sudo apt install virt-viewer</code>
    </span>
    <button class="btn bg sm" onclick="closeOv('vnc-ov')">Close</button>
  </div>
 </div>
</div>

<!-- SSH Terminal modal -->
<div class="ov" id="term-ov" onclick="bgClose(event,'term-ov')">
 <div class="modal xl" onclick="event.stopPropagation()">
  <div class="mh"><h2>Terminal — <span id="term-ttl">—</span></h2><button class="mc" onclick="closeTerm()">✕</button></div>
  <div class="mb" style="padding:16px"><div id="xtc"></div></div>
 </div>
</div>

<!-- noVNC via esm.sh (bundles all relative imports automatically) -->
<script type="module">
(async () => {
  try {
    const { default: RFB } = await import(
      'https://esm.sh/@novnc/novnc@1.4.0/core/rfb.js'
    );
    window._RFB = RFB;
  } catch (e) {
    window._RFB_ERR = String(e);
    console.error('[noVNC]', e);
  }
})();
</script>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let tfVMs=[],odVMs=[],cfg={};
let activeStream=null;
let sshCtx={vmid:0,ip:'',name:''};
let sshPollTimer=null;
let vncRFB=null, xtTerm=null, xtWS=null, fitAddon=null;

(async()=>{ await Promise.all([refreshAll(),loadCfg()]); })();

async function refreshAll(){ await Promise.all([refreshTF(),refreshOD()]); }

async function refreshTF(){
  try{
    const d=await fetch('/api/state').then(r=>r.json());
    tfVMs=d.vms||[];
    renderGrid('tf-g',tfVMs);
    document.getElementById('s-tf').textContent=tfVMs.length;
    document.getElementById('tfc').textContent=tfVMs.length+' VM'+(tfVMs.length!==1?'s':'');
  }catch(e){console.error(e)}
}

async function refreshOD(){
  try{
    const d=await fetch('/api/ondemand').then(r=>r.json());
    odVMs=d.vms||[];
    renderGrid('od-g',odVMs);
    document.getElementById('s-od').textContent=odVMs.length;
    document.getElementById('odc').textContent=odVMs.length+' VM'+(odVMs.length!==1?'s':'');
  }catch(e){console.error(e)}
}

async function loadCfg(){
  cfg=await fetch('/api/config').then(r=>r.json());
  populateForm(cfg);
  if(cfg.proxmox_node_name)      document.getElementById('s-nd').textContent=cfg.proxmox_node_name;
  if(cfg.proxmox_template_vm_id) document.getElementById('s-tm').textContent=cfg.proxmox_template_vm_id;
}

// ── VM card ────────────────────────────────────────────────────────────────
function renderGrid(gid,vms){
  const g=document.getElementById(gid);
  const isOD=gid==='od-g';
  if(!vms.length){
    g.innerHTML=isOD?'<div class="empty"><h3>No on-demand VMs</h3><p>Click <strong>＋ New VM</strong>.</p></div>'
                    :'<div class="empty"><h3>No state</h3><p>Run <strong>Apply Fleet</strong>.</p></div>';
    return;
  }
  g.innerHTML=vms.map(vmCard).join('');
  // FIX: normalise vm_id — on-demand records always use vm_id now, but be safe
  vms.forEach(vm=>{ const id=vm.vm_id||vm.vmid; if(id) fetchIP(id); });
}

function vmCard(vm){
  // FIX: normalise vm_id — on-demand VMs used 'vmid' in earlier versions
  const id=vm.vm_id||vm.vmid;
  const on=vm.started||vm.status==='running';
  const memGB=vm.memory_mb?Math.round(vm.memory_mb/1024):'-';
  return`<div class="vmc" id="vmc-${id}">
  <div class="vmh">
    <span class="vmn" title="${esc(vm.name||'')}">${esc(vm.name||'—')}</span>
    <span class="badge ${on?'on':'off'}">${on?'● Running':'○ Stopped'}</span>
  </div>
  <div class="vmb">
    <div class="specs">
      <div class="spec"><div class="sv2">${vm.cpu||vm.cpu_cores||'—'}</div><div class="sl2">vCPU</div></div>
      <div class="spec"><div class="sv2">${memGB}</div><div class="sl2">GB RAM</div></div>
      <div class="spec"><div class="sv2">${vm.disk_gb||'—'}</div><div class="sl2">GB Disk</div></div>
    </div>
    <div class="det"><span>VM ID</span><span>${id||'—'}</span></div>
    <div class="det"><span>Node</span><span>${vm.node||cfg.proxmox_node_name||'—'}</span></div>
    <div class="ipr">
      <span style="font-size:12px;color:var(--mut)">IP</span>
      <span class="ipl" id="ip-${id}"><span class="ips"></span></span>
    </div>
  </div>
  <div class="vmf">
    <span class="tag">${vm.source||'tf'}</span>
    <button class="btn bg sm" onclick="showSSH(${id},'${esc(vm.name)}','${esc(vm.ci_user||'')}')">SSH</button>
    <button class="btn bg sm" onclick="openVNC(${id},'${esc(vm.name)}')">Console</button>
    <button class="btn bw sm" onclick="dlSPICE(${id})">SPICE</button>
    <button class="btn bd sm" style="margin-left:auto" onclick="deleteVM(${id},'${esc(vm.name)}')">✕</button>
  </div>
</div>`;
}

// ── IP fetching ─────────────────────────────────────────────────────────────
// FIX: fetchIP now also updates the SSH modal if it is open for this vmid.
async function fetchIP(vmid){
  const el=document.getElementById(`ip-${vmid}`);
  try{
    const d=await fetch(`/api/vm/${vmid}/ip`).then(r=>r.json());
    if(d.ips&&d.ips.length){
      const ipStr=d.ips.join(', ');
      if(el) el.innerHTML=`<span class="ipb">LIVE</span> ${ipStr}`;
      // Update SSH modal if open for this vmid
      if(sshCtx.vmid===vmid&&!sshCtx.ip){
        sshCtx.ip=d.ips[0];
        refreshSSHModal();
      }
      return d.ips[0];
    }else{
      if(el) el.innerHTML='<span style="color:var(--mut)">pending/no agent</span>';
    }
  }catch{
    if(el) el.innerHTML='<span style="color:var(--mut)">unavailable</span>';
  }
  return null;
}

// ── SSH Modal ──────────────────────────────────────────────────────────────
// FIX: shows <resolving…> with a live poll, updates when agent responds.
function showSSH(vmid, name, ciUser){
  sshCtx={vmid, name, ip:'', user: ciUser||cfg.vm_ci_username||'ubuntu'};
  clearSshPoll();

  // Try to read IP already in the card
  const ipEl=document.getElementById(`ip-${vmid}`);
  const ipText=(ipEl?.textContent||'').replace(/LIVE|STATIC|pending|no agent|unavailable/g,'').trim();
  // ipText is empty if only the spinner was showing
  const knownIP = ipText && !ipText.includes('…') ? ipText.split(',')[0].trim() : '';
  if(knownIP){ sshCtx.ip=knownIP; }

  refreshSSHModal();
  openOv('ssh-ov');

  // Poll until we have an IP (max 20 × 3 s = 60 s)
  if(!sshCtx.ip){
    let attempts=0;
    sshPollTimer=setInterval(async()=>{
      attempts++;
      const ip=await fetchIP(vmid);
      if(ip){ sshCtx.ip=ip; refreshSSHModal(); clearSshPoll(); }
      else if(attempts>=20){ clearSshPoll(); }
    },3000);
  }
}

function refreshSSHModal(){
  const u=sshCtx.user||cfg.vm_ci_username||'ubuntu';
  const ip=sshCtx.ip;
  const box=document.getElementById('ssh-box');
  const status=document.getElementById('ssh-ip-status');
  const creds=document.getElementById('ssh-creds');
  const termBtn=document.getElementById('ssh-term-btn');
  document.getElementById('term-ttl').textContent=sshCtx.name;

  if(ip){
    document.getElementById('ssh-cmd').textContent=`ssh ${u}@${ip}`;
    box.classList.add('ok');
    status.textContent='';
    termBtn.disabled=false;
  }else{
    document.getElementById('ssh-cmd').textContent=`ssh ${u}@<resolving…>`;
    box.classList.remove('ok');
    status.innerHTML='<span class="ips" style="display:inline-block;margin-right:6px"></span>Waiting for QEMU agent to report IP…';
    termBtn.disabled=true;
  }
  creds.textContent=`User: ${u}  •  Password: set via QEMU agent at creation`;
}

function clearSshPoll(){
  if(sshPollTimer){ clearInterval(sshPollTimer); sshPollTimer=null; }
}

function closeSsh(){ clearSshPoll(); closeOv('ssh-ov'); }

function copySSH(){
  const t=document.getElementById('ssh-cmd').textContent;
  if(t.includes('<')) return;
  navigator.clipboard.writeText(t).then(()=>logLine('Copied.','k'));
}

// ── SPICE ──────────────────────────────────────────────────────────────────
// FIX: was dlSPICE(vm.vm_id) where vm_id could be undefined for on-demand VMs.
// vmCard now passes the already-normalised 'id' variable.
function dlSPICE(vmid){
  if(!vmid||vmid==='undefined'){
    alert('VM ID not available yet. Refresh and try again.');
    return;
  }
  logLine(`Requesting SPICE ticket for VM ${vmid} …`,'p');
  const a=document.createElement('a');
  a.href=`/api/vm/${vmid}/spice`;
  a.download=`vm-${vmid}.vv`;
  a.click();
  logLine('  .vv downloaded. Open with: remote-viewer vm-'+vmid+'.vv','k');
  logLine('  Install:  sudo apt install virt-viewer  |  brew install virt-viewer','m');
}

// ── noVNC console ──────────────────────────────────────────────────────────
// FIX: noVNC was loaded via jsdelivr which has broken relative module imports.
// Now loaded via esm.sh (bundles all dependencies).
// FIX: was checking novncReady flag but window._RFB was never checked before
// the async module loaded; now uses a proper poll with error display.
function openVNC(vmid,name){
  document.getElementById('vnc-ttl').textContent=name;
  document.getElementById('vnc-st').textContent='';
  document.getElementById('vnc-msg').textContent='Loading noVNC library…';
  document.getElementById('vov').style.display='flex';
  if(vncRFB){ try{vncRFB.disconnect()}catch(e){} vncRFB=null; }
  openOv('vnc-ov');

  let polls=0;
  const wait=setInterval(()=>{
    polls++;
    if(window._RFB_ERR){
      clearInterval(wait);
      document.getElementById('vnc-msg').textContent=
        'noVNC failed to load: '+window._RFB_ERR+
        '\nCheck browser console. Try refreshing the page.';
      return;
    }
    if(window._RFB){
      clearInterval(wait);
      doVNC(vmid);
    }else if(polls>40){
      clearInterval(wait);
      document.getElementById('vnc-msg').textContent=
        'noVNC library did not load (timeout). Check your network / browser console.';
    }
  },250);
}

async function doVNC(vmid){
  document.getElementById('vnc-msg').textContent='Creating VNC session…';
  let ticket;
  try{
    const d=await fetch(`/api/vm/${vmid}/vnc-ticket`).then(r=>{
      if(!r.ok) return r.json().then(e=>{throw new Error(e.detail||r.statusText)});
      return r.json();
    });
    ticket=d.ticket;
  }catch(e){
    document.getElementById('vnc-msg').textContent='VNC session failed: '+e.message;
    return;
  }
  document.getElementById('vnc-msg').textContent='Connecting…';
  const proto=location.protocol==='https:'?'wss':'ws';
  const url=`${proto}://${location.host}/api/vm/${vmid}/vnc-ws`;
  try{
    vncRFB=new window._RFB(document.getElementById('vncc'), url, {
      credentials:{password: ticket},
      wsProtocols:['binary'],
    });
    vncRFB.scaleViewport=true;
    vncRFB.resizeSession=true;
    vncRFB.addEventListener('connect',()=>{
      document.getElementById('vov').style.display='none';
      document.getElementById('vnc-st').textContent='Connected';
    });
    vncRFB.addEventListener('disconnect',e=>{
      document.getElementById('vnc-st').textContent='Disconnected';
      document.getElementById('vov').style.display='flex';
      document.getElementById('vnc-msg').textContent=e.detail?.clean?'Session ended.':'Connection lost.';
      vncRFB=null;
    });
    vncRFB.addEventListener('credentialsrequired',()=>vncRFB.sendCredentials({password:ticket}));
    vncRFB.addEventListener('securityfailure',e=>{
      document.getElementById('vnc-msg').textContent=`Auth failed: ${e.detail?.reason||'unknown'}`;
    });
  }catch(e){
    document.getElementById('vnc-msg').textContent='noVNC error: '+e;
  }
}

function closeVnc(){ if(vncRFB){try{vncRFB.disconnect()}catch(e){} vncRFB=null;} closeOv('vnc-ov'); }
function sendCAD(){ if(vncRFB) vncRFB.sendCtrlAltDel(); }

// ── SSH terminal ──────────────────────────────────────────────────────────
function openTerm(){
  if(!sshCtx.ip){ alert('IP not resolved yet.'); return; }
  closeOv('ssh-ov');
  openOv('term-ov');
  if(xtWS){xtWS.close();xtWS=null}
  document.getElementById('xtc').innerHTML='';
  const term=new Terminal({
    cursorBlink:true,
    theme:{background:'#000',foreground:'#f0f0f0',cursor:'#22c55e'},
    fontFamily:"'Cascadia Code','Fira Code',monospace",
    fontSize:13, scrollback:2000,
  });
  const fit=new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(document.getElementById('xtc'));
  fit.fit(); xtTerm=term; fitAddon=fit;
  const prot=location.protocol==='https:'?'wss':'ws';
  const ws=new WebSocket(`${prot}://${location.host}/api/vm/${sshCtx.vmid}/terminal?ip=${encodeURIComponent(sshCtx.ip)}`);
  xtWS=ws;
  ws.onopen=()=>{ term.writeln('\x1b[90mConnecting…\x1b[0m'); fit.fit(); ws.send(JSON.stringify({t:'r',cols:term.cols,rows:term.rows})); };
  ws.onmessage=e=>{ const m=JSON.parse(e.data); if(m.t==='o')term.write(m.d); else if(m.t==='e')term.writeln('\x1b[31m'+m.d+'\x1b[0m'); else if(m.t==='x')term.writeln('\r\n\x1b[90m[ended]\x1b[0m'); };
  ws.onclose=()=>term.writeln('\r\n\x1b[90m[closed]\x1b[0m');
  ws.onerror=()=>term.writeln('\r\n\x1b[31m[error]\x1b[0m');
  term.onData(d=>{ if(ws.readyState===1) ws.send(JSON.stringify({t:'i',d})); });
  term.onResize(({cols,rows})=>{ if(ws.readyState===1) ws.send(JSON.stringify({t:'r',cols,rows})); });
}

function closeTerm(){
  if(xtWS){xtWS.close();xtWS=null}
  if(xtTerm){xtTerm.dispose();xtTerm=null}
  closeOv('term-ov');
}

// ── Terraform ──────────────────────────────────────────────────────────────
function setBusy(op){
  const b=!!op;
  document.getElementById('spn').style.display=b?'block':'none';
  document.getElementById('opl').textContent=b?`terraform ${op}`:'Idle';
  ['bi','bp2','ba','bde'].forEach(id=>{ document.getElementById(id).disabled=b; });
}

function runTF(op,target){
  if(activeStream){activeStream.close();activeStream=null}
  clearLog();setBusy(op);logLine(`$ terraform ${op}`,'p');
  const url=target?`/api/stream/destroy_target?target=${encodeURIComponent(target)}`:`/api/stream/${op}`;
  const es=new EventSource(url);
  activeStream=es;
  es.onmessage=e=>{ const t=e.data; logLine(t,t.includes('[ERR]')||t.includes('Error')?'e':t.includes('complete!')?'k':''); };
  es.addEventListener('done',e=>{
    const rc=parseInt(e.data);
    logLine(rc===0?'✓ Done':'✗ Failed ('+rc+')',rc===0?'k':'e');
    es.close();activeStream=null;setBusy(null);
    if(rc===0) setTimeout(refreshTF,1500);
  });
  es.onerror=()=>{ logLine('Stream error.','e'); es.close();activeStream=null;setBusy(null); };
}

function confirmDestroyAll(){ if(confirm('Destroy ALL Terraform VMs?\nCannot be undone.')) runTF('destroy'); }

// ── New on-demand VM ───────────────────────────────────────────────────────
function createVM(){
  const name=document.getElementById('nv-n').value.trim();
  if(!name){alert('Enter a VM name.');return}
  closeOv('new-ov');clearLog();setBusy('create');
  logLine(`Creating on-demand VM '${name}' …`,'p');
  const p=new URLSearchParams({
    name,
    cpu_cores: document.getElementById('nv-c').value||2,
    memory_mb: document.getElementById('nv-m').value||2048,
    disk_gb:   document.getElementById('nv-d').value||20,
    ip:        document.getElementById('nv-i').value.trim()||'dhcp',
    gateway:   document.getElementById('nv-g').value.trim()||'',
    start:     document.getElementById('nv-st').checked,
  });
  const es=new EventSource(`/api/vm/create-stream?${p}`);
  activeStream=es;
  es.onmessage=e=>{ const t=e.data; logLine(t,t.includes('[ERR]')?'e':t.includes('[OK]')||t.includes('[DONE]')||t.includes('═')||t.includes('is ready')?'k':t.includes('[WARN]')?'m':''); };
  es.addEventListener('done',e=>{ const rc=parseInt(e.data); logLine(rc===0?'✓ VM ready.':'✗ Failed.',rc===0?'k':'e'); es.close();activeStream=null;setBusy(null); if(rc===0) setTimeout(refreshOD,1500); });
  es.onerror=()=>{ es.close();activeStream=null;setBusy(null); };
}

// ── Delete ─────────────────────────────────────────────────────────────────
async function deleteVM(vmid,name){
  if(!confirm(`Delete VM "${name}" (${vmid})?\nPermanently stops and removes it.`)) return;
  try{
    const d=await fetch(`/api/vm/${vmid}`,{method:'DELETE'}).then(r=>r.json());
    if(d.ok){ logLine(`✓ Deleted VM ${name}.`,'k'); refreshAll(); }
    else alert('Delete failed: '+JSON.stringify(d));
  }catch(e){alert('Error: '+e)}
}

// ── Config form ─────────────────────────────────────────────────────────────
function populateForm(c){
  const s=(id,v)=>{const e=document.getElementById(id);if(e&&v!=null)e.value=v};
  const b=(id,v)=>{const e=document.getElementById(id);if(e)e.checked=!!v};
  s('c-ep',c.proxmox_endpoint);s('c-nd',c.proxmox_node_name);
  s('c-tm',c.proxmox_template_vm_id);s('c-ds',c.proxmox_datastore_id);
  s('c-br',c.proxmox_bridge);s('c-tk',c.proxmox_api_token);
  s('c-us',c.proxmox_username);s('c-pw',c.proxmox_password);
  b('c-ins',c.proxmox_insecure);b('c-st',c.vm_started);
  b('c-bt',c.vm_on_boot);b('c-fc',c.vm_full_clone);
  s('c-co',c.vm_cpu_cores);s('c-so',c.vm_cpu_sockets);
  s('c-ct',c.vm_cpu_type);s('c-me',c.vm_memory_mb);
  s('c-mf',c.vm_memory_floating_mb);s('c-di',c.vm_disk_interface);
  s('c-dg',c.vm_disk_size_gb);b('c-it',c.vm_disk_iothread);
  b('c-ag',c.vm_qemu_agent_enabled);s('c-ni',c.vm_network_model);
  s('c-vl',c.vm_network_vlan_id);s('c-ip',c.vm_ipv4_address);
  s('c-gw',c.vm_ipv4_gateway);s('c-cu',c.vm_ci_username);
  s('c-cp',c.vm_ci_password);
  if(c.vm_ssh_public_keys?.length) s('c-ky',c.vm_ssh_public_keys.join('\n'));
  s('c-pf',c.vm_name_prefix);
  if(c.vm_tags?.length) s('c-tg',c.vm_tags.join(','));
}

function readForm(){
  const g=id=>{const v=document.getElementById(id)?.value?.trim();return v||null};
  const gi=id=>{const v=parseInt(document.getElementById(id)?.value);return isNaN(v)?null:v};
  const gb=id=>document.getElementById(id)?.checked??false;
  return{
    proxmox_endpoint:g('c-ep')||'',proxmox_insecure:gb('c-ins'),
    proxmox_api_token:g('c-tk'),proxmox_username:g('c-us'),proxmox_password:g('c-pw'),
    proxmox_node_name:g('c-nd')||'pve',proxmox_template_vm_id:gi('c-tm')||9000,
    proxmox_datastore_id:g('c-ds')||'',proxmox_bridge:g('c-br')||'',
    vm_name_prefix:g('c-pf'),
    vm_tags:(g('c-tg')||'vps,terraform').split(',').map(t=>t.trim()).filter(Boolean),
    vm_started:gb('c-st'),vm_on_boot:gb('c-bt'),vm_full_clone:gb('c-fc'),
    vm_cpu_cores:gi('c-co')||2,vm_cpu_sockets:gi('c-so')||1,
    vm_cpu_type:g('c-ct')||'x86-64-v2-AES',vm_memory_mb:gi('c-me')||2048,
    vm_memory_floating_mb:gi('c-mf'),vm_disk_interface:g('c-di')||'scsi0',
    vm_disk_size_gb:gi('c-dg')||20,vm_disk_iothread:gb('c-it'),
    vm_qemu_agent_enabled:gb('c-ag'),vm_network_model:g('c-ni')||'virtio',
    vm_network_vlan_id:gi('c-vl'),vm_ipv4_address:g('c-ip')||'dhcp',
    vm_ipv4_gateway:g('c-gw'),vm_ci_username:g('c-cu'),vm_ci_password:g('c-cp'),
    vm_ssh_public_keys:(document.getElementById('c-ky')?.value||'').split('\n').map(k=>k.trim()).filter(Boolean),
  };
}

async function saveCfg(){
  const c=readForm();
  const d=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(c)}).then(r=>r.json());
  if(d.ok){
    cfg=c;closeOv('cfg-ov');
    document.getElementById('s-nd').textContent=c.proxmox_node_name;
    document.getElementById('s-tm').textContent=c.proxmox_template_vm_id;
    logLine('✓ Config saved.','k');
  }else alert('Save failed: '+JSON.stringify(d));
}

// ── Utilities ──────────────────────────────────────────────────────────────
function logLine(t,cls=''){
  const b=document.getElementById('log');
  const d=document.createElement('div');
  d.className='ll '+(cls==='k'?'k':cls==='e'?'e':cls==='p'?'p':cls==='m'?'m':'');
  d.textContent=t; b.appendChild(d); b.scrollTop=b.scrollHeight;
}
function clearLog(){document.getElementById('log').innerHTML=''}
function openOv(id){document.getElementById(id).classList.add('open')}
function closeOv(id){document.getElementById(id).classList.remove('open')}
function bgClose(e,id){if(e.target===document.getElementById(id))closeOv(id)}
function swT(pfx,name,btn){
  document.querySelectorAll('.tp').forEach(p=>p.classList.remove('a'));
  document.querySelectorAll('.tab').forEach(b=>b.classList.remove('a'));
  document.getElementById(`${pfx}-${name}`).classList.add('a');
  btn.classList.add('a');
}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def root():
    return HTML

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8080, reload=False)
