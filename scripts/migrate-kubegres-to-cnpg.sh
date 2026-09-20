#!/bin/bash
#
# This script to migrate from Kubegres to CloudNativePG has been written
# and run against a throwaway database, but it has not been tested.
# The rest of this is from an LLM:
#
# One-time data migration from the Kubegres "django-postgres" database to
# the CloudNativePG "pgcluster" Cluster that roles/cnpg_cluster stands up
# alongside it (see README.md and roles/cnpg_cluster/defaults/main.yml).
#
# Goes via manage.py dumpdata / migrate / loaddata rather than pg_dump, on
# purpose: a fresh `migrate` only creates tables for models the current
# django-api codebase actually has, so years of orphaned tables (an old
# South migration-history table, a stray duplicate of django_migrations, and
# tables for apps that were deleted from the codebase without ever dropping
# their tables) are left behind rather than carried across.
#
# Does NOT touch DATABASE_URL -- the app keeps talking to Kubegres until you
# cut it over yourself (group_vars/{prod,staging}.yml: django_db_host, then
# `ansible-playbook playbooks/k8s_apps_{prod,staging}.yml --tags django` and
# a rollout restart of django + schedule-service). That is a separate,
# deliberate step; this script only makes CNPG's copy of the data current.
#
# Safe to re-run: manage.py migrate is idempotent, and loaddata assigns
# explicit primary keys, so Django's save() updates existing rows instead of
# duplicating them.
#
# Run this on the cluster's admin node (dev-kube-1 for staging, prod-kube-1
# for prod), where `microk8s kubectl` already has a working config -- same
# pattern as roles/staging_db_sync/templates/staging-db-sync.sh.j2.
#
# Usage:
#   ./migrate-kubegres-to-cnpg.sh            # asks for confirmation
#   ./migrate-kubegres-to-cnpg.sh -y         # no prompt, for scripting
#
# Override any of these if an environment ever diverges from the shared
# defaults (both prod and staging currently use identical names -- see
# roles/cnpg_cluster/defaults/main.yml and data/apps.yml):
#   NAMESPACE, DJANGO_DEPLOYMENT, DJANGO_CONTAINER, CNPG_SERVICE, DB_USER,
#   DB_NAME, DB_PORT, CNPG_APP_SECRET, KUBECTL
set -euo pipefail

NAMESPACE="${NAMESPACE:-default}"
DJANGO_DEPLOYMENT="${DJANGO_DEPLOYMENT:-django}"
DJANGO_CONTAINER="${DJANGO_CONTAINER:-django}"
CNPG_CLUSTER="${CNPG_CLUSTER:-pgcluster}"
CNPG_SERVICE="${CNPG_SERVICE:-${CNPG_CLUSTER}-rw}"
CNPG_APP_SECRET="${CNPG_APP_SECRET:-${CNPG_CLUSTER}-app}"
DB_USER="${DB_USER:-fkweb}"
DB_NAME="${DB_NAME:-fkweb}"
DB_PORT="${DB_PORT:-5432}"
KUBECTL="${KUBECTL:-microk8s kubectl}"

ASSUME_YES=0
if [ "${1:-}" = "-y" ]; then
  ASSUME_YES=1
fi

DUMP_FILE="$(mktemp /var/tmp/migrate-kubegres-to-cnpg.XXXXXX.json)"
chmod 600 "$DUMP_FILE"
cleanup() {
  rm -f "$DUMP_FILE"
}
trap cleanup EXIT

exec_django() {
  # -i so stdin (the loaddata step pipes the dump in) actually reaches the
  # container; kubectl exec drops stdin without it.
  # shellcheck disable=SC2086
  $KUBECTL exec -i -n "$NAMESPACE" "deploy/$DJANGO_DEPLOYMENT" -c "$DJANGO_CONTAINER" -- "$@"
}

echo "==> Checking pgcluster is healthy"
phase="$($KUBECTL get cluster.postgresql.cnpg.io "$CNPG_CLUSTER" -n "$NAMESPACE" -o jsonpath='{.status.phase}')"
if [ "$phase" != "Cluster in healthy state" ]; then
  echo "pgcluster is not healthy (status: $phase) -- aborting." >&2
  exit 1
fi

echo "==> Reading CNPG app password from secret/$CNPG_APP_SECRET"
CNPG_PASSWORD="$($KUBECTL get secret "$CNPG_APP_SECRET" -n "$NAMESPACE" -o jsonpath='{.data.password}' | base64 -d)"
if [ -z "$CNPG_PASSWORD" ]; then
  echo "No password found in secret/$CNPG_APP_SECRET" >&2
  exit 1
fi
CNPG_DATABASE_URL="postgresql://${DB_USER}:${CNPG_PASSWORD}@${CNPG_SERVICE}:${DB_PORT}/${DB_NAME}"

if [ "$ASSUME_YES" -ne 1 ]; then
  echo
  echo "About to migrate data from Kubegres (via the running django pod's own"
  echo "DATABASE_URL) into CNPG at ${CNPG_SERVICE}:${DB_PORT}/${DB_NAME}."
  echo "This will run 'manage.py migrate' against that CNPG database and load"
  echo "the dumped data into it. Kubegres itself is not modified."
  read -r -p "Continue? [y/N] " reply
  case "$reply" in
    [yY]*) ;;
    *) echo "Aborted."; exit 1 ;;
  esac
fi

echo "==> Dumping data from Kubegres (excluding contenttypes/permissions/sessions)"
# contenttypes and auth.permission are excluded because migrate below
# recreates them fresh, and --natural-foreign means every FK pointing at a
# ContentType (including admin.logentry's) is exported as ["app_label",
# "model"] rather than a raw id, so it resolves against those freshly
# created rows on load instead of needing the old ids to line up. sessions
# are pure churn, excluded outright.
#
# authtoken.tokenproxy is DRF's admin-only proxy model for authtoken.Token --
# same table, different app_label.model, so dumpdata otherwise serializes
# every token twice and loaddata's second insert trips the token's unique
# constraint on user_id.
exec_django python manage.py dumpdata \
  -e contenttypes -e auth.permission -e sessions \
  -e authtoken.tokenproxy \
  --natural-foreign --natural-primary \
  > "$DUMP_FILE"
echo "    $(wc -c < "$DUMP_FILE") bytes dumped to a local temp file"

echo "==> Building a clean schema on CNPG (manage.py migrate)"
exec_django sh -c "DATABASE_URL='${CNPG_DATABASE_URL}' python manage.py migrate --noinput"

echo "==> Fetching the current (app_label, model) pairs migrate just created"
VALID_CONTENT_TYPES="$(exec_django sh -c "DATABASE_URL='${CNPG_DATABASE_URL}' python manage.py shell -c \"
from django.contrib.contenttypes.models import ContentType
for c in ContentType.objects.all():
    print(f'{c.app_label}.{c.model}')
\"")"

# Three fixups against the dump, neither touching Kubegres:
#
# 1. fk.Video.upload_token is a 32-char uuid4().hex, but a handful of old
#    rows (from before validation caught this -- see fk/models/video.py)
#    carry a longer value and fail loaddata's max_length check, aborting the
#    whole fixture in one atomic transaction. The field is a transient
#    upload-flow token with no meaning once a video is published, so it's
#    safe to regenerate.
#
# 2 & 3. Both auth.Group.permissions (a natural-key list) and
#    admin.LogEntry.content_type (a natural-key pair) can point at a
#    Permission/ContentType for a model that no longer exists in the
#    codebase -- e.g. fk.FileFormat, dropped in
#    fk/migrations/0025_video_file_variant.py but never cleaned out of
#    whatever group once had it, or out of the admin log entries recording
#    actions once taken on it. migrate does not recreate a ContentType (or
#    Permission) for a model that is gone, so loaddata fails resolving
#    either natural key. A stale group permission is just dropped from the
#    list (it cannot grant anything once the model is gone); a log entry for
#    a vanished model has nothing left to point the frontend at, so the
#    whole entry is dropped rather than left dangling.
python3 - "$DUMP_FILE" <<PYEOF
import json

valid_content_types = set("""$VALID_CONTENT_TYPES""".split())

path = "$DUMP_FILE"
with open(path) as f:
    objects = json.load(f)

fixed_tokens = 0
dropped_perms = 0
kept_objects = []
dropped_logentries = 0
for obj in objects:
    if obj.get("model") == "fk.video":
        token = obj["fields"].get("upload_token", "")
        if len(token) > 32:
            import uuid
            obj["fields"]["upload_token"] = uuid.uuid4().hex
            fixed_tokens += 1
    elif obj.get("model") == "auth.group":
        perms = obj["fields"].get("permissions", [])
        kept = [p for p in perms if f"{p[1]}.{p[2]}" in valid_content_types]
        dropped_perms += len(perms) - len(kept)
        obj["fields"]["permissions"] = kept
    elif obj.get("model") == "admin.logentry":
        ct = obj["fields"].get("content_type")
        if ct and f"{ct[0]}.{ct[1]}" not in valid_content_types:
            dropped_logentries += 1
            continue
    kept_objects.append(obj)

with open(path, "w") as f:
    json.dump(kept_objects, f)
if fixed_tokens:
    print(f"    regenerated upload_token on {fixed_tokens} fk.Video row(s)")
if dropped_perms:
    print(f"    dropped {dropped_perms} stale group permission(s) for removed models")
if dropped_logentries:
    print(f"    dropped {dropped_logentries} admin log entr(y/ies) for removed models")
PYEOF

echo "==> Loading data into CNPG (manage.py loaddata)"
# fkweb/signals.py auto-creates an authtoken.Token on every post_save of a
# new User, with no guard for save(raw=True) -- which is exactly what
# loaddata uses to insert fixture rows. Left connected, it fires for each
# newly-inserted user and races the user's own real Token object later in
# the same fixture, which then fails to insert on the unique user_id
# constraint. Disconnected for the duration of this load only, in-process;
# nothing about the signal itself changes.
exec_django sh -c "DATABASE_URL='${CNPG_DATABASE_URL}' python manage.py shell -c \"
import sys
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db.models.signals import post_save
from fkweb.signals import create_auth_token
post_save.disconnect(create_auth_token, sender=get_user_model())
call_command('loaddata', '-', format='json')
\"" < "$DUMP_FILE"

echo "==> Row counts on CNPG, for a spot check against Kubegres:"
exec_django sh -c "DATABASE_URL='${CNPG_DATABASE_URL}' python manage.py shell -c \"
from django.apps import apps
for label in ['fk.Video', 'fk.VideoFile', 'fk.ScheduleItem', 'fk.User', 'authtoken.Token']:
    model = apps.get_model(label)
    print(f'{label:20s} {model.objects.count()}')
\""

echo "==> Done. Kubegres is untouched; DATABASE_URL has not been changed."
echo "    Compare the counts above against Kubegres, then cut over"
echo "    django_db_host in group_vars and roll the django + schedule-service"
echo "    deployments when ready."
