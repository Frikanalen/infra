mediamtx_preview_secret

Owns the credential MediaMTX's on-demand Opus converter uses. Django issues
it, and `Secret/mediamtx-preview` holds the only copy of its password.

When a member opens a source's preview, MediaMTX starts FFmpeg. FFmpeg reads
`contrib/live/<UUID>` and publishes `contrib/preview/<UUID>`. A Django
**Preview converter (all enabled sources)** credential may do exactly that for
any enabled source over RTSP, so this one Secret never changes as sources are
added.

It works like `roles/playout_live_secret`. It runs
`manage.py ensure_preview_credential --keep <key UUID>` in a Django pod and
rewrites the Secret only when Django issues a new credential. It also sets
`mediamtx_preview_key`, which the mediamtx app definition hashes into a pod
annotation.

It runs only where `mediamtx_preview_enabled` is true. Run it after the django
app (it needs django-api's `ensure_preview_credential`) and before mediamtx:

    ansible-playbook playbooks/k8s_apps_staging.yml --tags mediamtx

After `staging_db_sync`, re-run this on staging.
