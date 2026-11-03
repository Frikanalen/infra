# CloudNativePG: backups, cutover, restore

pgcluster (`roles/cnpg_cluster`) replaces the Kubegres `django-postgres`
database. Backups are physical: continuous WAL archiving plus a daily base
backup at 03:00, through the Barman Cloud plugin (`roles/cnpg`) to Garage on
file01 (`roles/garage`), bucket `cnpg-prod` or `cnpg-staging`. 30 days of
point-in-time recovery.

`kubectl` below is `microk8s kubectl` on the admin node (dev-kube-1 or
prod-kube-1), namespace default.

## Vault entries

Add these to `data/vault.yml` (`ansible-vault edit data/vault.yml`) before
the first run:

```sh
openssl rand -hex 32                 # vault_garage_rpc_secret
echo "GK$(openssl rand -hex 12)"     # vault_prod_cnpg_s3_key_id, vault_dev_cnpg_s3_key_id
openssl rand -hex 32                 # vault_prod_cnpg_s3_secret_key, vault_dev_cnpg_s3_secret_key
```

## First install, per environment

```sh
ansible-playbook playbooks/storage.yml --tags garage                # once, file01
ansible-playbook playbooks/k8s_platform_dev.yml --tags cnpg         # cert-manager, operator, plugin
```

## Cutover from Kubegres

Django has to be stopped while the data comes across, or writes made during
the import are lost. With 3 instances the import takes a few minutes.

```sh
# 1. Stop writes. Argo would scale it back up, so pause its sync first.
kubectl -n argocd patch application django --type merge -p '{"spec":{"syncPolicy":{"automated":null}}}'
kubectl scale deploy/django deploy/django-events --replicas=0

# 2. Recreate pgcluster with a one-time import from Kubegres. Bootstrap only
#    runs on creation, so an existing (empty) pgcluster must go first.
kubectl delete cluster pgcluster --ignore-not-found
ansible-playbook playbooks/k8s_apps_staging.yml --tags cnpg_cluster -e cnpg_cluster_import_host=django-postgres
kubectl get cluster pgcluster -w           # until "Cluster in healthy state"

# 3. Compare row counts between the two, e.g. fk_video and fk_scheduleitem.

# 4. Point Django at pgcluster: set django_db_host: pgcluster-rw in
#    group_vars, then
ansible-playbook playbooks/k8s_apps_staging.yml --tags django
# This re-enables Argo's automated sync, which scales Django back up.
kubectl rollout restart deploy/django deploy/django-events
```

Rollback: set `django_db_host` back to `django-postgres` and run the django
tag again. Kubegres is untouched by all of the above. Anything written to
pgcluster after the cutover is not in Kubegres.

## Checking backups

```sh
kubectl get backups                          # one per day, phase completed
kubectl get cluster pgcluster -o jsonpath='{.status.conditions[?(@.type=="ContinuousArchiving")]}'
```

## Restore test

Bootstrap a throwaway cluster from the object store; it must reach a healthy
state and hold the data. Delete it afterwards.

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: pgcluster-restore-test
spec:
  instances: 1
  storage:
    size: 2Gi
    storageClass: local-ssd
  bootstrap:
    recovery:
      source: origin
  externalClusters:
    - name: origin
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters:
          barmanObjectName: pgcluster-backup
          serverName: pgcluster
```

Leave `spec.plugins` out of it, so the test cluster never writes WAL into
pgcluster's archive.
