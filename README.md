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
  MicroK8s, ArgoCD, Traefik, MetalLB, Kubegres, CloudNativePG, CasparCG, NFS,
  and app deployment.
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
Traefik, Kubegres, the CloudNativePG operator, ArgoCD, and ArgoCD Image
Updater. The dev cluster additionally runs kube-prometheus-stack and, alongside
it, `junos_exporter` pointed at the `fksw` switch (see `roles/junos_exporter`
and `playbooks/junos_exporter_key.yml` below). Two Grafana dashboards ship
with it: a generic per-metric one, and `fksw — Frikanalen network`, laid out
by what is actually plugged into the switch (WAN uplink, LACP bundles by peer,
broadcast chain, management ports).

```sh
ansible-playbook playbooks/k8s_apps_prod.yml
ansible-playbook playbooks/k8s_apps_staging.yml
```

Deploys ArgoCD application definitions for Django, frontend, graphics, schedule,
playout, and stream components. Also deploys a CloudNativePG `Cluster` named
`pgcluster` (see `roles/cnpg_cluster`) as a migration target alongside the
existing Kubegres database: postgres 16, 3 instances on the `local-ssd`
storage class, `fkweb` db/user, and the same credentials as Kubegres's
`django-postgres` secret, so the eventual Django cutover is just a
`DATABASE_URL` hostname change. Data migration into it is manual and
Kubegres stays in place until that migration is done. Also enables
Kubegres's built-in daily backup CronJob for prod's `django-postgres`
(`pg_dumpall`, gzipped, 03:00 -- see `roles/kubegres_backup`), writing into
a PVC backed by an NFS PV pointed at file01's `/util/k8s` export (see
`playbooks/storage.yml`).

```sh
ansible-playbook playbooks/junos_exporter_key.yml
```

Generates the SSH credential `junos_exporter` uses to log into `fksw`, the
Juniper switch the whole internal network hangs off, and stores the private
half in `data/vault.yml`. Run once, by hand: it prints a public key that has
to be pasted into the switch's configuration, which nothing here can do for
you. The exporter itself is installed by `playbooks/k8s_cluster_dev.yml` and
scraped by the Prometheus there -- see `roles/junos_exporter/README.md` for
the login class the switch needs and how to rotate the key.

```sh
ansible-playbook playbooks/caspar.yml
```

Configures CasparCG nodes, including mounting the media archive from file01
over NFS.

```sh
ansible-playbook playbooks/storage.yml
```

Configures the NFS export of the ZFS media archive on file01, and installs a
weekly cron job there that resets `archive/media-staging` to a fresh clone of
`archive/media` (Monday 00:00, see `roles/media_staging_reset`) -- this is
the dataset `caspar-sw1` (staging playout) mounts, so staging always starts
its week from a clean copy of prod's archive. Also exports `/util/k8s` (rw,
owned by uid/gid 999) as the destination for the prod database backup
CronJob above, and installs a daily cron job there that prunes dumps older
than 14 days (see `roles/db_backup_retention`), since Kubegres's own backup
CronJob never deletes old ones itself.

```sh
ansible-playbook playbooks/staging_db_sync.yml
```

Installs a weekly cron job on the staging cluster node (`dev-kube-1`) that
refreshes the staging Django database from production: `pg_dump`/`pg_restore`
over a `kubectl port-forward` into each cluster's `django-postgres`, followed
by `manage.py migrate` to bring the restored schema forward to whatever
staging's tracked branch expects (Monday 00:15, see `roles/staging_db_sync`).
This gives the staging node a cluster-admin kubeconfig for prod, fetched at
provisioning time -- treat it as sensitive.

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
