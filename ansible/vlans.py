# --- VLAN Source of Truth ---
# Add a dict to provision a VLAN across the fabric.
# Remove a dict to deprovision it (then run remove_vlans.yml).
# vni must be unique per VLAN.
 
VLANS = [
    {"name": "tenant-1", "vlan_id": 10, "vni": 10010},
]
