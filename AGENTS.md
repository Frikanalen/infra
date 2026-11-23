# Notes for agents working in this repository

## Alerts

Before you do anything you know will set off alerts (drain or reboot a node,
restart CasparCG/playout/packager, roll out or restart an app on purpose,
reboot file01, a Proxmox host, bao1, mail1), silence exactly those alerts
first, for about as long as the work takes, and lift the silence afterwards.
Use `scripts/alert-silence` by hand, or the `alert_silence` role in a
playbook. Narrow matchers, at most 6 h, a comment naming the work, and
`ALERT_SILENCE_BY` naming you and the thread. Details, presets and the
severity tiers: [docs/alerting.md](docs/alerting.md).

When adding or changing an alert rule, pick its severity by the tiers in
docs/alerting.md. Only names listed in `kube_prometheus_stack_critical_alerts`
can be `critical`.
