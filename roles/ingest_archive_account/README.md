# ingest_archive_account

Owns the credential ingest uses to write to the media archive: the account on
the storage host, the SSH keypair, and the Kubernetes secret ingest reads it
from.

Ingest used to run on `file01` because it archived by copying files into the
media archive directly. It now writes over SSH, so it runs in Kubernetes like
everything else, and this role gives it a way in.

## The key lives in exactly one place

The private key is generated on first run and stored **only** in
`Secret/ingest-archive-ssh`. It is not in the vault, not in Git, and not on
anyone's laptop — generation happens in a temporary directory on the Ansible
controller that is removed in the same task block.

Every later run reads the existing key back out of the secret and leaves it
alone, so re-running the playbook does not churn the credential. The public
half is kept in the secret alongside the private one, which is what makes that
reconciliation possible without regenerating anything.

Losing the secret is recoverable: re-run the playbook and it generates a fresh
key and replaces the authorised one. Nothing else needs updating.

## What it does

1. Reads `Secret/ingest-archive-ssh`, if it exists.
2. Generates an ed25519 keypair when there is none (no passphrase — nothing can
   type one in when the pod starts).
3. Creates the account and, if absent, the archive directory on the storage
   host.
4. Installs the public key in `authorized_keys`, `exclusive`, restricted to
   `restrict,command="internal-sftp"` — a stolen key cannot get a shell,
   forward ports, or allocate a PTY. Ingest only ever speaks SFTP, so this
   costs nothing.
5. Checks the account can actually write to the archive directory, so a
   permissions mistake fails the play rather than surfacing as an ingest that
   silently archives nothing.
6. Reads the host's own SSH host key to build `known_hosts`. Taking it from the
   host rather than `ssh-keyscan` means there is no trust-on-first-use window.
7. Writes the secret.

The existing archive directory is never chowned — only created when missing.
`/archive/media` is already there and exported over NFS by `roles/nfs_server`,
and its ownership is not this role's to restyle.

## Environments

Separate accounts and directories, so a staging upload cannot reach the
production archive:

| | Account | Directory |
| --- | --- | --- |
| prod | `ingest` | `/archive/media` |
| staging | `ingest-staging` | `/archive/media-staging` |

Set in `group_vars/prod.yml` and `group_vars/staging.yml`. Each environment
gets its own key, so a staging compromise grants nothing in production.

## Requirements

- `file01` reachable in the `storage` inventory group, as the other storage
  plays already assume.
- `ssh-keygen` on the Ansible controller.
- The archive directory writable by the account. For `/archive/media`, which
  predates this role, that may mean adding the account to the group that owns
  it; step 5 tells you if it is not.

## Rotating the key

Delete the secret and re-run the playbook. A new key is generated,
`authorized_keys` is replaced (it is `exclusive`), and `argocd_ingest` hashes
the key into a pod annotation so ArgoCD rolls ingest onto it.
