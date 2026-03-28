variable "ssot_config_path" {
  description = "Path to the Python SSOT file (relative to this Terraform module)."
  type        = string
  default     = "../../sots/config.py"
}

variable "proxmox_endpoint" {
  description = "Proxmox API endpoint (for example: https://pve.example.com:8006/)."
  type        = string
}

variable "proxmox_insecure" {
  description = "Skip TLS certificate verification for Proxmox API."
  type        = bool
  default     = false
}

variable "proxmox_api_token" {
  description = "API token in the format user@realm!tokenid=secret."
  type        = string
  default     = null
  nullable    = true
  sensitive   = true
}

variable "proxmox_username" {
  description = "Proxmox username with realm (used when API token is not provided)."
  type        = string
  default     = null
  nullable    = true
}

variable "proxmox_password" {
  description = "Proxmox password (used with proxmox_username)."
  type        = string
  default     = null
  nullable    = true
  sensitive   = true
}

variable "proxmox_node_name" {
  description = "Target Proxmox node name where VMs will be created."
  type        = string
}

variable "proxmox_template_vm_id" {
  description = "VM ID of the Ubuntu template to clone."
  type        = number
}

variable "proxmox_datastore_id" {
  description = "Datastore used for clone target disks and cloud-init disk."
  type        = string
}

variable "proxmox_bridge" {
  description = "Proxmox bridge name for VM NICs (for example vmbr0)."
  type        = string
}

variable "vm_name_prefix" {
  description = "VM name prefix. If null, the SSOT PROJECT_NAME is used."
  type        = string
  default     = null
  nullable    = true
}

variable "vm_tags" {
  description = "List of tags to apply to each VM."
  type        = list(string)
}

variable "vm_started" {
  description = "Whether each VM should be started after creation."
  type        = bool
}

variable "vm_on_boot" {
  description = "Whether each VM should start automatically with Proxmox host boot."
  type        = bool
}

variable "vm_full_clone" {
  description = "Use full clone (true) or linked clone (false)."
  type        = bool
}

variable "vm_cpu_cores" {
  description = "CPU cores per VM."
  type        = number
}

variable "vm_cpu_sockets" {
  description = "CPU sockets per VM."
  type        = number
}

variable "vm_cpu_type" {
  description = "CPU type exposed to guest (for example host, x86-64-v2-AES)."
  type        = string
}

variable "vm_memory_mb" {
  description = "Dedicated memory in MB per VM."
  type        = number
}

variable "vm_memory_floating_mb" {
  description = "Floating memory in MB per VM; set null to match vm_memory_mb."
  type        = number
  default     = null
  nullable    = true
}

variable "vm_disk_interface" {
  description = "Disk interface name (for example scsi0, virtio0, sata0)."
  type        = string
}

variable "vm_disk_size_gb" {
  description = "Disk size in GB per VM."
  type        = number
}

variable "vm_disk_iothread" {
  description = "Enable iothread on VM disk."
  type        = bool
}

variable "vm_network_model" {
  description = "NIC model (for example virtio, e1000)."
  type        = string
}

variable "vm_network_vlan_id" {
  description = "Optional VLAN ID for NIC. Set null for untagged."
  type        = number
  default     = null
  nullable    = true
}

variable "vm_qemu_agent_enabled" {
  description = "Enable QEMU guest agent integration in Proxmox settings."
  type        = bool
}

variable "vm_ipv4_address" {
  description = "Cloud-init IPv4 address in CIDR notation or dhcp."
  type        = string
}

variable "vm_ipv4_gateway" {
  description = "Cloud-init IPv4 gateway. Must be null when vm_ipv4_address is dhcp."
  type        = string
  default     = null
  nullable    = true
}

variable "vm_ci_username" {
  description = "Cloud-init username. If null, SSH_USER from SSOT is used."
  type        = string
  default     = null
  nullable    = true
}

variable "vm_ci_password" {
  description = "Cloud-init password. If null, SSH_PASS from SSOT is used."
  type        = string
  default     = null
  nullable    = true
  sensitive   = true
}

variable "vm_ssh_public_keys" {
  description = "SSH public keys injected via cloud-init."
  type        = list(string)
  default     = []
}
