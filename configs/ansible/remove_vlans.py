---
# Deprovision VLANs that were removed from vlans.py.
# To delete a VLAN: remove it from vlans.py and run this playbook.
# Takes a mandatory variable: vlan_id and vni
#
# Usage:
#   ansible-playbook -i inventory.py remove_vlans.yml -e "vlan_id=20 vni=10020"

- name: Remove VLAN from fabric
  hosts: leaves
  gather_facts: no

  tasks:

    - name: Remove BGP VLAN block
      arista.eos.eos_config:
        lines:
          - "no vlan {{ vlan_id }}"
        parents: "router bgp {{ bgp_asn }}"

    - name: Remove VNI mapping
      arista.eos.eos_config:
        lines:
          - "no vxlan vlan {{ vlan_id }} vni {{ vni }}"
        parents: interface Vxlan1

    - name: Remove VLAN
      arista.eos.eos_config:
        lines:
          - "no vlan {{ vlan_id }}"

    - name: Save config
      arista.eos.eos_command:
        commands: write memory

    - name: Confirm VLAN is gone
      arista.eos.eos_command:
        commands: show vlan
      register: vlan_output

    - name: Print VLANs
      debug:
        msg: "{{ vlan_output.stdout_lines[0] }}"
