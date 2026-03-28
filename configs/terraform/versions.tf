terraform {
  required_version = ">= 1.6.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.99"
    }
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3"
    }
  }
}
