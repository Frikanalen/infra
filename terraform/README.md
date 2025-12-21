# VM guest orchestration

## Applying configuration

Obtain an API token from the Proxmox VE dashboard. Set environment variables:

```shell
TF_VAR_proxmox_url=https://vm1:8006/api2/json
TF_VAR_proxmox_token_id=(token id)
TF_VAR_proxmox_token_secret=(token secret)
```

Then, run `terraform init`, `terraform plan`, `terraform apply`.
