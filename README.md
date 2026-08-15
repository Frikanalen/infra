# infra

Frikanalen infrastructure governance repository.

This repository contains the Ansible and Terraform definitions used to manage
Frikanalen infrastructure: users, host inventory, firewall and DNS services,
MicroK8s clusters, ArgoCD applications, CasparCG nodes, and Proxmox VM
definitions.

## Repository Layout

- `inventory.yaml` - Ansible inventory groups for utility hosts, Proxmox hosts,
  Kubernetes nodes, staging, and production.
- `ansible.cfg` - local Ansible defaults, including inventory, role path,
  vault-password file, remote user, and SSH key.
- `data/users.yml` - user database and SSH keys. Submit changes here to request
  or update developer/admin accounts.
- `data/hosts.yml` - host/IP definitions used by infrastructure automation.
- `data/argocd.yml` - ArgoCD application configuration data.
- `data/vault.yml` - encrypted or sensitive values consumed by playbooks.
- `group_vars/prod.yml` and `group_vars/staging.yml` - environment-specific
  application versions, domains, ingress settings, and Helm chart versions.
- `playbooks/` - entry points for applying infrastructure state.
- `roles/` - local Ansible roles for common setup, users, DNS, firewall,
  MicroK8s, ArgoCD, Traefik, MetalLB, Kubegres, CasparCG, NFS, and app
  deployment.
- `terraform/` - Proxmox VM guest orchestration.

## Common Playbooks

```sh
ansible-playbook playbooks/site.yml
```

Applies broad host setup: users, common packages, QEMU guest tools, DNS, utility
host networking, and MicroK8s installation.

```sh
ansible-playbook playbooks/firewall.yml
```

Configures firewall and ingress forwarding on `util1`.

```sh
ansible-playbook playbooks/k8s_cluster_prod.yml
ansible-playbook playbooks/k8s_cluster_dev.yml
```

Forms MicroK8s clusters and installs base Kubernetes services such as MetalLB,
Traefik, Kubegres, and ArgoCD.

```sh
ansible-playbook playbooks/k8s_apps_prod.yml
ansible-playbook playbooks/k8s_apps_staging.yml
```

Deploys ArgoCD application definitions for Django, frontend, graphics, schedule,
playout, and stream components.

```sh
ansible-playbook playbooks/caspar.yml
```

Configures CasparCG nodes, including mounting the media archive from file01
over NFS.

```sh
ansible-playbook playbooks/storage.yml
```

Configures the NFS export of the ZFS media archive on file01.

## Requirements

The local Ansible configuration assumes:

- Ansible is installed locally.
- The vault password is available at `~/.vault_pass_frikanalen`.
- The SSH key for automation is available at `~/.ssh/frikanalen_ansible`.
- Remote hosts are reachable as the `ansible` user.

The repository intentionally keeps infrastructure state in reviewable YAML and
Terraform files. Treat changes as governance changes: review diffs carefully,
especially user access, firewall rules, DNS zones, and production app versions.

## Terraform

The `terraform/` directory manages Proxmox VM guest orchestration. See
`terraform/README.md` for required Proxmox token environment variables and the
usual `terraform init`, `terraform plan`, and `terraform apply` flow.
