playout_live_secret

Owns the credential playout uses to read live sources from MediaMTX: Django
issues it, and `Secret/playout-live` holds the only copy of its password.

Playout puts `contrib/live/<UUID>` on air for whichever source the schedule
books. A Django **Playout reader** credential may read any enabled source's
contribution over RTSP (and nothing else), so this one Secret never changes
as sources are added.

## What it does

1. Reads the key UUID out of `Secret/playout-live`, if the Secret exists.
2. Runs `manage.py ensure_playout_credential --keep <key UUID>` in a running
   Django pod. Only the key UUID goes on the command line, never the password.
3. If Django still accepts that key, nothing changes. Otherwise (first run,
   revoked or expired, or a staging database replaced by a copy of
   production's) Django revokes the old one, issues a new credential and
   returns its password once, and the Secret is rewritten.
4. Sets `playout_live_key`, which the playout app definition hashes into a pod
   annotation, so playout rolls onto a new key. Helm cannot watch a Secret it
   does not own.

Run it after the django app (it needs a running pod with the command, from
django-api #85) and before the playout app:

    ansible-playbook playbooks/k8s_apps_prod.yml --tags playout

After `staging_db_sync`, re-run this on staging. The copied database knows
only production's key, so staging's playout cannot read until it does.
