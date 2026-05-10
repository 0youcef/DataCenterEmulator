#!/bin/bash

while true;do
  usage=$(mpstat 1 1 | awk '/Average/ {print 100 - $NF}')
  usage=${usage%.*} 
  [ "$usage" -lt 15 ] && break
done
python3 configs/config_switches.py
python3 configs/config_firewalls.py
python3 configs/config_frr.py
python3 configs/config_proxmox.py
cd configs/ansible
ansible-playbook -i inventory.py underlay.yml
ansible-playbook -i inventory.py overlay.yml
ansible-playbook -i inventory.py mlag.yml
ansible-playbook -i inventory.py border_leaf.yml
