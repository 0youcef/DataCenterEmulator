output "ssot_num_servers" {
  description = "Server count loaded from SSOT NUM_SERVERS."
  value       = local.ssot_num_servers
}

output "vps_num_servers" {
  description = "Effective VPS count (SSOT NUM_SERVERS minus first reserved server)."
  value       = local.vps_num_servers
}

output "vps_vm_names" {
  description = "Created VM names."
  value       = [for vm in proxmox_virtual_environment_vm.vps : vm.name]
}

output "vps_vm_ids" {
  description = "Created VM IDs by instance index key."
  value = {
    for key, vm in proxmox_virtual_environment_vm.vps : key => vm.vm_id
  }
}
