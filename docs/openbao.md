# OpenBao: secrets for the clusters (and, later, for Ansible)

OpenBao is the open-source fork of HashiCorp Vault. It runs as one node on
**bao1** (192.168.3.111, `data/hosts.yml`), outside both clusters. External
Secrets Operator (ESO) in each cluster copies that cluster's secrets out of
it into ordinary Kubernetes Secrets, and Reloader restarts the workloads that
use a Secret when it changes.

This is the first stage of moving from "Ansible renders every Secret from
`data/vault.yml`" to "OpenBao holds every secret value". For the whole plan,
see the infra and gitops review doc.

## Layout

| Path in OpenBao | Holds | Who can read it |
|---|---|---|
| `kv/staging/<namespace>/<name>` | Staging's Kubernetes Secrets, one path per Secret, keys unchanged | Staging's ESO (policy `eso-staging`, auth mount `kubernetes-staging`) |
| `kv/prod/<namespace>/<name>` | Prod's, same shape | Prod's ESO (`eso-prod`, `kubernetes-prod`) |
| `sys/storage/raft/snapshot` | Nightly snapshot | The snapshot token on bao1 only |

Each cluster's ESO logs in with its own service account (Kubernetes auth),
so a cluster can only ever read its own environment's prefix. An
ExternalSecret for a whole Secret looks like this:

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: ingest-api
  namespace: default
spec:
  refreshInterval: 1m
  secretStoreRef:
    kind: ClusterSecretStore
    name: openbao
  target:
    name: ingest-api
  dataFrom:
    - extract:
        key: staging/default/ingest-api
```

## Unsealing

bao1 unseals itself at boot (seal `"static"`): the 32-byte key is in the
Ansible vault as `vault_openbao_seal_key` and on bao1 in `/etc/openbao/seal/`.
That is a deliberate trade-off, chosen to start with: a reboot needs nobody,
but anyone holding bao1's disk holds both the data and the key. So **bao1 is
not in the Proxmox backup jobs**, and must not be added. It is backed up by
the nightly raft snapshot instead, which is encrypted by the seal key and
useless without the vault.

Moving later to unseal keys held by people, or to unsealing by a second
OpenBao, is a seal migration and needs no rebuild.

## First-time setup

1. Add to `data/vault.yml` (`ansible-vault edit data/vault.yml`):

   ```yaml
   vault_openbao_seal_key_id: "20261111-1"           # any label; date-N works
   vault_openbao_seal_key: "<openssl rand -hex 32>"
   vault_openbao_s3_key_id: "GK<openssl rand -hex 12>"
   vault_openbao_s3_secret_key: "<openssl rand -hex 32>"
   ```

2. Create the VM and give it the common setup, its DNS name and the Garage
   bucket for snapshots:

   ```sh
   cd terraform && terraform apply      # creates bao1 (vmid 502 on vm1)
   ansible-playbook playbooks/site.yml --limit bao1
   ansible-playbook playbooks/site.yml --tags dns       # dc1 zone gets bao1
   ansible-playbook playbooks/storage.yml --tags garage # "openbao" bucket
   ```

3. Install and initialise OpenBao:

   ```sh
   ansible-playbook playbooks/openbao.yml
   ```

   The first run stops after init and leaves `~/openbao-init-bao1.json`
   on the machine you ran it from. Put its `root_token` in the vault as
   `vault_openbao_root_token`. Store the three `recovery_keys_b64` offline
   with two different people; any two of them can mint a new root token.
   Then delete the file and run the playbook again to finish.

4. Connect staging, then copy its existing Secrets in:

   ```sh
   ansible-playbook playbooks/k8s_platform_dev.yml --tags openbao,reloader
   ansible-playbook playbooks/openbao_seed.yml --limit kubedev_admin
   ```

   Check that the ClusterSecretStore is ready:
   `microk8s kubectl get clustersecretstore openbao` should show
   `Valid / Ready True`.

5. Hand staging over to the clusters repo. This creates the `staging` root
   Application, which syncs `clusters/staging/` (every app's Application,
   and an ExternalSecret for each app Secret). Argo CD adopts the existing
   Applications by name, and ESO adopts the existing Secrets, whose contents
   OpenBao now holds unchanged:

   ```sh
   ansible-playbook playbooks/k8s_platform_dev.yml --tags clusters_root
   ```

   Then check that every ExternalSecret reads `SecretSynced`
   (`microk8s kubectl get externalsecrets -A`) and every app is Synced and
   Healthy in Argo CD. If ESO refuses to adopt a Secret, delete that Secret
   and ESO recreates it within a minute.

## Prod

The same handover, on kubeprod, in an order that restarts nothing on air:

```sh
ansible-playbook playbooks/k8s_platform_prod.yml --tags openbao     # ESO + auth mount kubernetes-prod
ansible-playbook playbooks/openbao_seed.yml --limit kubeprod_admin  # kv/prod/default/*
ansible-playbook playbooks/k8s_platform_prod.yml --tags clusters_root
ansible-playbook playbooks/k8s_platform_prod.yml --tags reloader
```

Check the store is `Valid / Ready` before the root, and after it that every
ExternalSecret is `SecretSynced`, every Application Synced and Healthy, and
that no app Secret's data changed. Reloader goes in last, once ESO has taken
the Secrets over, so that an unexpected byte of difference cannot restart
playout. `clusters/prod/apps` was written from the live Applications, so the
root adopts them without a sync that changes anything.

After this, `k8s_apps_prod.yml` creates no Applications: release to prod by
changing the version in `clusters/prod/apps/<app>.yaml` in the clusters repo.

## After the handover

- Staging's app versions and values live in the clusters repo,
  `clusters/staging/apps/`. `k8s_apps_staging.yml` no longer creates
  Applications.
- The app Secrets come from OpenBao. Change a value with
  `bao kv put kv/staging/default/<name> ...` (or the UI on
  https://bao1.dc1.frikanalen.tv:8200), not in the vault. Reloader restarts
  whatever uses it.
- `app_secrets_from_openbao: true` in `group_vars/staging.yml` makes the
  roles that issue credentials (`playout_live_secret`,
  `ingest_archive_account`) write them to OpenBao instead of to the
  cluster, and `cnpg_cluster` stops writing its Secrets.
- `django-secret` (Django's SECRET_KEY) is copied into OpenBao as a backup
  but is still rendered by the django-api chart. Moving it to ESO needs a
  chart change, so that the chart stops creating it.

## Things to check on the first apply

These could not be tested from where this was written:

- The `.deb` asset name in `openbao_deb_url`. If GitHub has renamed it, fix
  the pattern in `roles/openbao/defaults/main.yml`. The package only supplies
  the binary; the role checks that it lands in `/usr/bin/bao`.
- The file audit device, now declared in `openbao.hcl.j2` because the API
  refused it (HTTP 400). Check that `/var/log/openbao/audit.log` fills up
  after the config change restarts OpenBao.
- The chart versions `external_secrets` and `reloader` in
  `group_vars/staging.yml`.

## Restoring from a snapshot

On a fresh bao1 with the same seal key in the vault, run the playbook as far
as init, then restore with the root token from the old cluster:

```sh
rclone --config /etc/openbao/rclone.conf copyto garage:openbao/<file> /tmp/raft.snap
curl --cacert /etc/openbao/tls/ca.crt -H "X-Vault-Token: $TOKEN" \
  -X POST --data-binary @/tmp/raft.snap \
  https://127.0.0.1:8200/v1/sys/storage/raft/snapshot-force
```
