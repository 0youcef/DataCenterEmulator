import requests
import sys


class GNS3Client:

    def __init__(self, server, user, password):
        self.server = server
        self.session = requests.Session()
        self._authenticate(user, password)

    def _authenticate(self, user, password):
        resp = self.session.post(f"{self.server}/access/users/login", data={
            "username": user, "password": password
        })
        if resp.status_code != 200:
            print(f"Authentication failed: {resp.text}")
            sys.exit(1)
        token = resp.json().get("access_token")
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def _request(self, method, path, **kwargs):
        resp = self.session.request(method, f"{self.server}{path}", **kwargs)
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(f"API error [{method} {path}] {resp.status_code}: {resp.text}")
        try:
            return resp.json()
        except Exception:
            return {}

    # --- Projects ---
    def get_projects(self):
        return self._request("GET", "/projects")

    def create_project(self, name):
        resp = self.session.post(f"{self.server}/projects", json={"name": name})
        if resp.status_code == 409:
            return None  # already exists
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Failed to create project: {resp.text}")
        return resp.json()

    def open_project(self, project_id):
        self._request("POST", f"/projects/{project_id}/open")

    def close_project(self, project_id):
        self._request("POST", f"/projects/{project_id}/close")

    def delete_project(self, project_id):
        self._request("DELETE", f"/projects/{project_id}")

    # --- Computes ---
    def get_computes(self):
        return self._request("GET", "/computes")

    # --- Templates ---
    def get_templates(self):
        return self._request("GET", "/templates")

    # --- Nodes ---
    def get_nodes(self, project_id):
        return self._request("GET", f"/projects/{project_id}/nodes")

    def get_links(self, project_id):
        return self._request("GET", f"/projects/{project_id}/links")

    def create_node_from_template(self, project_id, template_id, compute_id, x, y):
        return self._request("POST", f"/projects/{project_id}/templates/{template_id}", json={
            "compute_id": compute_id, "x": x, "y": y
        })

    def create_node(self, project_id, name, node_type, compute_id, x, y, properties=None):
        payload = {"name": name, "node_type": node_type, "compute_id": compute_id, "x": x, "y": y}
        if properties:
            payload["properties"] = properties
        return self._request("POST", f"/projects/{project_id}/nodes", json=payload)

    def rename_node(self, project_id, node_id, name):
        result = self._request("PUT", f"/projects/{project_id}/nodes/{node_id}", json={"name": name})
        result['name'] = name  # in case PUT returns empty
        return result

    def start_nodes(self, project_id):
        self._request("POST", f"/projects/{project_id}/nodes/start")
    def start_node(self, project_id, node_id):
        self._request("POST", f"/projects/{project_id}/nodes/{node_id}/start", json={})


    # --- Links ---
    def create_link(self, project_id, node_a, adapter_a, node_b, adapter_b, port_a=0, port_b=0):
        return self._request("POST", f"/projects/{project_id}/links", json={
            "nodes": [
                {"node_id": node_a, "adapter_number": adapter_a, "port_number": port_a},
                {"node_id": node_b, "adapter_number": adapter_b, "port_number": port_b},
            ]
        })
    def start_capture(self, project_id, link_id):
        """Starts a packet capture on a specific link."""
        return self._request("POST", f"/projects/{project_id}/links/{link_id}/capture/start", json={})

    def set_switch_ports(self, project_id, node_id, num_ports):
        ports = [{"name": f"Ethernet{i}", "port_number": i, "type": "access", "vlan": 1}
                 for i in range(num_ports)]
        return self._request("PUT", f"/projects/{project_id}/nodes/{node_id}", json={
            "properties": {"ports_mapping": ports}
        })
