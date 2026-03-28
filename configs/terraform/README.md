# Proxmox VPS Terraform

This Terraform config clones an Ubuntu template on Proxmox and creates a variable number of VPS instances.

The VM count is read directly from SSOT: `NUM_SERVERS` in `sots/config.py`.
The first server slot is reserved (border leaf), so effective VPS count is `max(NUM_SERVERS - 1, 0)`.

## Files

- `scripts/read_ssot.py`: parses SSOT and returns JSON for Terraform external data source.
- `main.tf`: reads SSOT, validates inputs, and creates VMs.
- `variables.tf`: all Proxmox and VM settings are input variables.
- `terraform.tfvars.example`: copy to `terraform.tfvars` and fill your values.

## Quick Guide

### 1) Create VMs

```bash
cd configs/terraform
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` and set your Proxmox values:

- `proxmox_endpoint`
- Auth (`proxmox_api_token` OR `proxmox_username` + `proxmox_password`)
- `proxmox_node_name`
- `proxmox_template_vm_id`
- `proxmox_datastore_id`
- `proxmox_bridge`

Then create VMs:

```bash
terraform init
terraform plan
terraform apply
```

### 2) Configure/Change VMs

You can reconfigure in two ways:

- Scale VM count from SSOT by editing `../../sots/config.py` (`NUM_SERVERS`)
- Change VM profile by editing `terraform.tfvars` (CPU, memory, disk, VLAN, cloud-init)

Apply changes:

```bash
terraform plan
terraform apply
```

### 3) Delete VMs

Delete all Terraform-managed VPS VMs:

```bash
terraform destroy
```

Delete some VMs (scale down):

1. Decrease `NUM_SERVERS` in `../../sots/config.py`
2. Run:

```bash
terraform plan
terraform apply
```

## SSOT-driven scaling

Edit `NUM_SERVERS` in `../../sots/config.py`, then re-run:

```bash
terraform plan
terraform apply
```

Terraform will scale VM resources up/down to match the effective VPS count.

## Credentials

Use either:

- `proxmox_api_token`, or
- `proxmox_username` + `proxmox_password`

Do not commit real secrets.
