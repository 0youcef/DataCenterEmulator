---
# Provision VLANs defined in vlans.py across the fabric.
# Idempotent — safe to run multiple times.
# To add a VLAN: add it to vlans.py and rerun this playbook.

- name: Provision VLANs on Leaves
  hosts: leaves
  gather_facts: no

  tasks:

    - name: Create VLANs
      arista.eos.eos_config:
        lines:
          - "name {{ item.name }}"
        parents: "vlan {{ item.vlan_id }}"
      loop: "{{ vlans }}"

    - name: Map VLANs to VNIs
      arista.eos.eos_config:
        lines:
          - "vxlan vlan {{ item.vlan_id }} vni {{ item.vni }}"
        parents: interface Vxlan1
      loop: "{{ vlans }}"

    - name: Advertise VLANs into EVPN
      arista.eos.eos_config:
        lines:
          - "rd auto"
          - "route-target both {{ item.vni }}:{{ item.vni }}"
          - "redistribute learned"
        parents:
          - "router bgp {{ bgp_asn }}"
          - "vlan {{ item.vlan_id }}"
      loop: "{{ vlans }}"

    - name: Save config
      arista.eos.eos_command:
        commands: write memory

- name: Verify VLANs
  hosts: leaves
  gather_facts: no

  tasks:

    - name: Show VLAN summary
      arista.eos.eos_command:
        commands: show vlan
      register: vlan_output

    - name: Print VLANs
      debug:
        msg: "{{ vlan_output.stdout_lines[0] }}"

    - name: Show VNI mappings
      arista.eos.eos_command:
        commands: show vxlan vni
      register: vni_output

    - name: Print VNI mappings
      debug:
        msg: "{{ vni_output.stdout_lines[0] }}"
