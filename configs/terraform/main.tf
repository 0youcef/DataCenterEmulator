data "external" "ssot" {
  program = [
    "python3",
    "${path.module}/scripts/read_ssot.py",
    abspath("${path.module}/${var.ssot_config_path}")
  ]
}

locals {
  ssot_num_servers      = tonumber(data.external.ssot.result.num_servers)
  vps_num_servers       = max(local.ssot_num_servers - 1, 0)
  ssot_project_name     = data.external.ssot.result.project_name
  vm_name_prefix        = coalesce(var.vm_name_prefix, replace(lower(local.ssot_project_name), " ", "-"))
  vm_ci_username        = coalesce(var.vm_ci_username, data.external.ssot.result.ssh_user)
  vm_ci_password        = coalesce(var.vm_ci_password, data.external.ssot.result.ssh_pass)
  vm_floating_memory_mb = coalesce(var.vm_memory_floating_mb, var.vm_memory_mb)
  vm_instances = {
    for idx in range(local.vps_num_servers) : tostring(idx + 1) => idx + 1
  }
}

check "proxmox_auth_method" {
  assert {
    condition = (
      var.proxmox_api_token != null && var.proxmox_api_token != ""
    ) || (
      var.proxmox_username != null &&
      var.proxmox_username != "" &&
      var.proxmox_password != null &&
      var.proxmox_password != ""
    )
    error_message = "Set either proxmox_api_token OR proxmox_username/proxmox_password."
  }
}

check "dhcp_gateway_guard" {
  assert {
    condition     = var.vm_ipv4_address != "dhcp" || var.vm_ipv4_gateway == null
    error_message = "vm_ipv4_gateway must be null when vm_ipv4_address is set to dhcp."
  }
}

resource "proxmox_virtual_environment_vm" "vps" {
  for_each = local.vm_instances

  name      = format("%s-%02d", local.vm_name_prefix, each.value)
  node_name = var.proxmox_node_name
  tags      = var.vm_tags
  started   = var.vm_started
  on_boot   = var.vm_on_boot

  clone {
    vm_id        = var.proxmox_template_vm_id
    datastore_id = var.proxmox_datastore_id
    full         = var.vm_full_clone
  }

  cpu {
    cores   = var.vm_cpu_cores
    sockets = var.vm_cpu_sockets
    type    = var.vm_cpu_type
  }

  memory {
    dedicated = var.vm_memory_mb
    floating  = local.vm_floating_memory_mb
  }

  disk {
    datastore_id = var.proxmox_datastore_id
    interface    = var.vm_disk_interface
    size         = var.vm_disk_size_gb
    iothread     = var.vm_disk_iothread
  }

  network_device {
    bridge  = var.proxmox_bridge
    model   = var.vm_network_model
    vlan_id = var.vm_network_vlan_id
  }

  initialization {
    datastore_id = var.proxmox_datastore_id

    ip_config {
      ipv4 {
        address = var.vm_ipv4_address
        gateway = var.vm_ipv4_gateway
      }
    }

    user_account {
      username = local.vm_ci_username
      password = local.vm_ci_password
      keys     = var.vm_ssh_public_keys
    }
  }

  agent {
    enabled = var.vm_qemu_agent_enabled
  }
}
