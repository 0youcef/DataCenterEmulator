provider "proxmox" {
  endpoint = var.proxmox_endpoint
  insecure = var.proxmox_insecure

  api_token = var.proxmox_api_token
  username  = var.proxmox_username
  password  = var.proxmox_password
}
