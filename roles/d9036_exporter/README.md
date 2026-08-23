# d9036_exporter

Scrapes the Cisco D9036 Modular Encoding Platform — the chassis that turns the
playout feed into the transport stream — and feeds it to the Prometheus that
`roles/kube_prometheus_stack` installs.

The exporter is [Frikanalen/d9036_exporter][repo], written here rather than
found on a shelf. The D9036 has no SNMP worth the name; what it has is an EMI
XML endpoint for inventory and a web-to-IIOP bridge that answers in omniORB's
Python `repr` syntax. The exporter parses that representation with a strict
data-only parser — it never evaluates device output as Python.

[repo]: https://github.com/Frikanalen/d9036_exporter

## The device

`enc-sec-mgmt` in `data/hosts.yml`, 192.168.3.156, "Secondary D9036 mgmt MAIN"
on `fksw` ge-0/0/32. `data/hosts.yml` also notes a primary D9036 whose address
nobody has written down yet; when it turns up, this role can be included a
second time with `d9036_exporter_target` and the resource names overridden, and
the dashboard's Encoder variable will pick both up.

Plain HTTP, deliberately. The appliance's TLS predates anything a current
client will negotiate, and this is a management VLAN reachable only from
inside.

## The credential

`vault_d9036_exporter_password` in `data/vault.yml`, the password for the
appliance's `Api` automation account. Set it with `ansible-vault edit
data/vault.yml`.

Treat that credential as privileged despite the exporter only reading. The
vendor's `/emi/iiop_api.json` executes its `iiop_args` value as Python before
invoking CORBA, so anyone holding this password has code execution in the
appliance's web process. The exporter never passes client input into that
field — it hardcodes an allowlist of read-only methods — but the account itself
is not a read-only account in any enforced sense.

## The image

`ghcr.io/frikanalen/d9036_exporter`, pinned to a `sha-` build rather than
`latest`: release-please's first release PR (0.1.0) is still open, so no semver
tag exists yet. Move `d9036_exporter_image_tag` to `v0.1.0` once it does.

The repository is private, so GHCR made the package private with it and the
cluster could not pull it; the package has since been made public, like
`frontend`, `django-api` and the rest. If it is ever made private again, put a
`kubernetes.io/dockerconfigjson` Secret holding a PAT with `read:packages` in
the `monitoring` namespace and set `d9036_exporter_image_pull_secret` to its
name — the Deployment template already handles that case.

## The dashboard

`files/d9036-exporter-dashboard.json`, shipped as a ConfigMap carrying the
`grafana_dashboard: "1"` label that kube-prometheus-stack's Grafana sidecar
watches for — the same mechanism `roles/junos_exporter` uses.

Two things on it are worth explaining, because both look like mistakes:

**Hardware pools are aggregated with `max`, never `sum`.** The chassis reports
each pool once per engine class that can draw on it, so `Audio Encode Unit`
appears nine times with identical totals. Summing them would claim the chassis
has 288 audio encode units rather than 32.

**Alarms carry no text.** The exporter deliberately leaves the human-readable
alarm string and subject out of the labels; they are unbounded cardinality and
would put an arbitrary string into Prometheus's index. What you get is
`type_id`, the board and port, severity and class. Look the type up in the
chassis's own message log. Fields the alarm does not use come back as the
all-ones value for their width — 65535, 4294967295 — and the dashboard renders
those as an em dash; board 65534 means the chassis itself rather than a card.

## Scrape pacing

60s interval, 30s timeout. A collection measured about 3 seconds against the
live chassis, so this is slack rather than an estimate. The exporter serialises
its own collections so that overlapping Prometheus scrapes cannot burst
requests at the appliance; if the "Collection duration" panel ever approaches
the interval, that serialisation has started queueing and the interval wants
raising.
