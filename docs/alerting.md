# Alerting

Prometheus and the Loki ruler on each cluster send alerts to that cluster's
Alertmanager (roles/kube_prometheus_stack). Alertmanager sends them on to
Telegram. The goal is that a normal day leaves the alarm chat silent, so
anything that does turn up there gets read.

## Tiers

| severity   | means                                                        | goes to (prod)                        |
|------------|--------------------------------------------------------------|---------------------------------------|
| `critical` | The channel or the web stream is broken **now**              | alarm chat, with sound, repeats 2 h   |
| `high`     | Not on air yet, but lost a safety net: handle **today**      | alarm chat, with sound, repeats 8 h   |
| `warning`  | Worth a look when convenient                                 | quiet chat, silent, daily, no resolve |
| `info`     | Context only                                                 | nowhere (Alertmanager UI, Grafana)    |

Everything from **staging** goes to the quiet chat, silently, whatever its
severity.

Examples of `high`: a Postgres replica or WAL archiving gone, no recent
database backup, an API server whose watch feed is stuck, a node down, a ZFS
pool degraded, a failing backup CronJob.

**Only alerts named in `kube_prometheus_stack_critical_alerts` can reach
Alertmanager as `critical`** (roles/kube_prometheus_stack/defaults). Any
other rule marked critical (the upstream mixins have many) arrives as `high`.
Adding a name to that list means it will make someone's phone ring: it must
be about air or the public stream, and it must have a clear action.

The quiet chat is the alarm chat unless `vault_alertmanager_telegram_quiet_chat_id`
is set. Its messages arrive without a notification sound either way.

## Writing a rule

- Pick the tier by the table above, not by how bad it sounds.
- Give it a `for:` long enough to ride out normal blips, and a description
  that says what to do. An alert with nothing to do should not exist.
- Something that fires every day and is accepted is not an alert: fix the
  cause in Ansible (e.g. masking a unit), narrow the rule, or delete it.
- If one failure always sets off several alerts, add an inhibit rule in
  roles/kube_prometheus_stack/tasks/main.yml so only the cause is sent.

## Silencing alerts for planned work

**If you are about to do something that you know will set off alerts, silence
exactly those alerts first, for about as long as the work takes, and lift the
silence when you are done.** That applies to people and to agents alike.
Typical cases: draining or rebooting a node, restarting CasparCG or playout,
restarting the packager, rolling out an app on purpose, rebooting file01 or a
Proxmox host.

From a playbook, use the `alert_silence` role (see its defaults;
playbooks/upgrade_microk8s_nodes_ubuntu.yml does it around every drain):

```yaml
- ansible.builtin.include_role:
    name: alert_silence
  vars:
    alert_silence_preset: drain
    alert_silence_minutes: 90
    alert_silence_comment: "What is being done, and by whom"
# ... the work ...
- ansible.builtin.include_role:
    name: alert_silence
    tasks_from: expire
```

By hand (from a machine with the playbooks' SSH access):

```sh
ALERT_SILENCE_BY="claude (thread: Upgrade X)" \
  scripts/alert-silence add staging 45m "Reboot dev-kube-3 for kernel" drain:dev-kube-3
scripts/alert-silence add prod 20m "Restart CasparCG on caspar1 (OK'd by user)" onair
scripts/alert-silence add prod 30m "Roll out django 3.12.0" rollout:default
scripts/alert-silence add prod 30m "Reboot file01" host:file01
scripts/alert-silence list prod
scripts/alert-silence expire prod <id>
```

Rules:

- **Narrow.** Use the preset that fits, or matchers for the alert names you
  expect. Never silence a whole environment; the script refuses to.
- **Short.** About the expected length of the work plus a margin, at most
  6 hours. Lift it when the work is done, so problems the work caused show.
- **Say why.** The comment names the work (and the thread or PR), and
  `ALERT_SILENCE_BY` says who set it.
- **On-air alerts** (`onair`) only when the on-air impact is the accepted,
  planned part of the work. Work that touches prod air needs the user's OK
  anyway; the silence is part of that same work, not a separate decision.
- A silence never replaces checking the result: after the work, look at what
  is firing (`scripts/alert-silence list` shows the silence, Alertmanager
  shows what it hid).

Silences live in Alertmanager's volume, so they survive a restart.
