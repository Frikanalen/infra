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
- `data/` - the shared source of truth, and **also `group_vars/all`**, which is
  a symlink to it. Everything in this directory is therefore loaded as
  variables for every host in the inventory, at group_vars precedence — adding
  a file here adds variables everywhere. It is a symlink rather than a copy so
  that Terraform can read the same files: `terraform/main.tf` builds its VM
  definitions straight out of `../data/hosts.yml`, so a host is defined once
  and both tools agree.
  - `data/users.yml` - user database and SSH keys. Submit changes here to
    request or update developer/admin accounts.
  - `data/hosts.yml` - host/IP definitions, and the VM specs Terraform clones
    from. Exposed to playbooks as `host_config`.
  - `data/argocd.yml` - ArgoCD and other cluster-wide application settings.
  - `data/cluster.yml` - cluster capability flags, currently
    `k8s_monitoring_enabled`.
  - `data/vault.yml` - encrypted or sensitive values consumed by playbooks.
- `group_vars/prod.yml` and `group_vars/staging.yml` - environment-specific
  application versions, domains, ingress settings, and Helm chart versions.
- `playbooks/` - entry points for applying infrastructure state.
- `roles/` - local Ansible roles for common setup, users, DNS, firewall,
  MicroK8s, ArgoCD, Traefik, MetalLB, Kubegres, CloudNativePG, CasparCG, NFS,
  and app deployment.
- `terraform/` - Proxmox VM guest orchestration.

## Development

Install the collections this repo depends on:

```sh
ansible-galaxy collection install -r requirements.yml
```

Lint before pushing — CI runs the same two checks on every pull request:

```sh
ansible-lint
for pb in playbooks/*.yml playbooks/*/*.yml; do ansible-playbook --syntax-check "$pb"; done
```

Roles that talk to the cluster all do it the same way: `become: true` plus an
explicit `kubeconfig: "{{ k8s_kubeconfig }}"`, which is MicroK8s's own
credential file and readable only by root. The alternative — running
unprivileged against the connecting user's `~/.kube/config` — works, but only
once `microk8s_kubectl` has written that file for the `ansible` user, which
made the platform playbooks quietly depend on the cluster playbook having run
first. Helm tasks additionally carry `run_once: true`, since a Helm release
should be installed once per cluster and not once per node.

`.ansible-lint` sets the `production` profile and skips three rules, each
with its reasoning in the file — chiefly `var-naming[no-role-prefix]`, which
would fight the cross-role variable sharing this repo relies on.

CI needs the vault password to parse anything at all, since `group_vars/all`
is a symlink to `data/` and `data/vault.yml` is loaded for every host. Store
it as a repository secret named `ANSIBLE_VAULT_PASSWORD` (Settings → Secrets
and variables → Actions), matching `~/.vault_pass_frikanalen`.

## Common Playbooks

```sh
ansible-playbook playbooks/site.yml
```

Applies broad host setup: users, common packages, QEMU guest tools, DNS, utility
host networking, and MicroK8s installation.

It also installs `node_exporter` on the hosts outside the Kubernetes clusters
-- `file01` and the Proxmox hypervisors, i.e. the inventory group
`node_exporter_hosts` (see `roles/node_exporter`). The kube nodes are excluded
on purpose: kube-prometheus-stack already runs a node-exporter DaemonSet on
those. On any host with ZFS -- `file01` above all, which holds the media
archive -- a systemd timer additionally feeds pool capacity, scrub history,
per-disk error counters and per-dataset usage into node_exporter's textfile
collector, none of which node_exporter can see by itself. The Prometheus that
reads all of this, its two Grafana dashboards and its alerting rules are set up
by `playbooks/k8s_cluster_dev.yml` via `roles/node_exporter_scrape`; installing
the exporter without running that leaves it scraped by nobody.

```sh
ansible-playbook playbooks/firewall.yml
```

Configures firewall and ingress forwarding on `util1`.

```sh
ansible-playbook playbooks/k8s_cluster_prod.yml
ansible-playbook playbooks/k8s_cluster_dev.yml
```

Forms the MicroK8s clusters: seeds, joins the remaining nodes, and installs
the host-level prerequisites (kubectl config, Helm, `nfs-common`). This is the
once-in-a-cluster's-life part. It is now a no-op against a cluster that is
already formed — the seed play lists the nodes and skips minting a join token
when every expected member is present.

```sh
ansible-playbook playbooks/k8s_platform_prod.yml
ansible-playbook playbooks/k8s_platform_dev.yml
```

Installs the platform services: MetalLB, Traefik, external-dns, Kubegres, the
CloudNativePG operator, ArgoCD, and ArgoCD Image Updater. The dev cluster
additionally runs kube-prometheus-stack and, alongside it, `junos_exporter`
pointed at the `fksw` switch (see `roles/junos_exporter` and
`playbooks/junos_exporter_key.yml` below). Two Grafana dashboards ship with
it: a generic per-metric one, and `fksw — Frikanalen network`, laid out by
what is actually plugged into the switch (WAN uplink, LACP bundles by peer,
broadcast chain, management ports).

The dev cluster also carries the scrape configuration, dashboards and alerting
rules for the hosts that are not Kubernetes nodes -- see
`roles/node_exporter_scrape` and the `node_exporter` note below.

Only the dev cluster has a Prometheus Operator, so `k8s_monitoring_enabled`
(see `data/cluster.yml`) is false on prod and the ServiceMonitors, PodMonitors
and Grafana dashboard ConfigMaps these roles would otherwise create are
skipped there. Without that gate, installing Traefik on prod would fail:
the chart renders a kind the API server does not know.

### Tags

Three axes, applied consistently across the Kubernetes playbooks:

| Axis | Tags |
| --- | --- |
| Layer, one per play | `bootstrap`, `platform`, `apps` |
| Component, one per role | `traefik`, `metallb`, `external_dns`, `kubegres`, `cnpg`, `argocd`, `argocd_gitops`, `argocd_image_updater`, `junos_exporter`, `kube_prometheus_stack`, `django`, `frontend`, `graphics`, `schedule`, `playout`, `stream`, `ingest`, `media_server`, `cnpg_cluster`, `kubegres_backup` |
| Slice, cross-cutting | `ingress`, `dns`, `internal_dns`, `database`, `monitoring` |

So a single component can be advanced on its own, which is the usual way to
work:

```sh
ansible-playbook playbooks/k8s_platform_dev.yml --tags traefik
ansible-playbook playbooks/k8s_platform_prod.yml --tags dns
ansible-playbook playbooks/k8s_apps_prod.yml --tags frontend
ansible-playbook playbooks/k8s_platform_dev.yml --skip-tags monitoring
```

Two things to know. Roles with a `meta/argument_specs.yml` (`traefik`,
`metallb`, `external_dns`) get an automatic "Validating arguments against arg
spec" task that Ansible tags `always`, so you will see those role names go
past even under an unrelated tag — they validate variables and change
nothing. And `--tags join` on its own will refuse to run: the join play reads
cluster membership from the seed play, so select `bootstrap` instead.

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
