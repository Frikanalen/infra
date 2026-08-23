# junos_exporter

Scrapes `fksw` — the Juniper switch every host in `192.168.3.0/24` hangs off —
and feeds it to the Prometheus that `roles/kube_prometheus_stack` installs.

Everything in this repository is downstream of that switch, and until now
nothing watched it. An optic degrading, a fan failing or a power supply
dropping out was something you found out about from the symptoms.

`junos_exporter` is not SNMP. It logs in over SSH and runs the same
operational commands you would type by hand (`show interfaces`, `show chassis
environment`, …) with `| display xml`, and parses the result. That is why it
needs a real login user on the switch rather than a community string.

## The credential

Two halves, in two places:

- The **private** half lives in `data/vault.yml` as
  `vault_junos_exporter_ssh_private_key`. This role renders it into
  `Secret/junos-exporter-ssh` and mounts it into the pod.
- The **public** half is in `fksw`'s configuration, and is kept in the vault
  alongside the private one as `vault_junos_exporter_ssh_public_key` so it can
  be re-read without going to the switch for it.

This is deliberately *not* how `roles/ingest_archive_account` does it. That
role generates its key straight into a Kubernetes secret and never writes it
down, because it can authorise the key itself — the far end is `file01`, which
Ansible already has a login on. Here the far end is a switch that Ansible has
no login on at all, so authorising the key means a human pasting it into the
configuration. A key that only existed in the cluster would, if the cluster
were rebuilt, leave the switch silently unscraped until somebody noticed and
re-pasted the public half. The vault is the durable copy.

## Generating the key

Once, by hand:

```sh
ansible-playbook playbooks/junos_exporter_key.yml
```

It generates the keypair, appends both halves to `data/vault.yml`, and prints
the two `set` commands to paste into `fksw`. It refuses to overwrite an
existing key, and verifies the re-encrypted vault round-trips before it
replaces the real one — a half-written `data/vault.yml` would take every other
secret in the repository with it.

RSA rather than ed25519: `set system login user … authentication ssh-ed25519`
only exists from Junos 15.1, and `fksw`'s `ge-0/0/32` puts it on EX-class
hardware old enough that assuming otherwise is unwise. If it turns out to be
newer, change `junos_exporter_key_type` in the playbook and
`junos_exporter_ssh_key_filename` in this role's defaults together.

## The switch side

Not automated, and cannot be from here. Paste this into `fksw` before running
the role, or the exporter comes up and fails every scrape:

```
set system login class exporter-ro permissions view
set system login class exporter-ro permissions view-configuration
set system login user junos_exporter class exporter-ro
set system login user junos_exporter authentication ssh-rsa "<the key the playbook printed>"
```

The class is `exporter-ro` and not `junos-exporter-ro`: Junos reserves the
`junos-` prefix for its own system-defined classes and rejects anything else
using it with "cannot use reserved identifier". The *user* keeps its name —
the reservation is on the hyphen form, and `junos_exporter` is not that.

That `ssh-rsa "ssh-rsa AAAA…"` doubling is not a typo — the Junos statement
names the key type, and the quoted argument is the whole public key line,
type prefix and trailing comment included.

`view` alone is not enough: some of what the exporter asks for is read out of
the configuration rather than out of operational state, which is
`view-configuration`. Those two together are already read-only, which is why
there is no `allow-commands`/`deny-commands` pair here. Pinning the exporter to
a hand-written command regex reads like hardening, but what it mostly buys is a
way for some later exporter version to fail every scrape over an auxiliary
command the regex did not anticipate — `set cli screen-length 0` and its
relatives — in exchange for no privilege that `view`/`view-configuration` had
not already withheld.

## Rotating the key

In this order, because the switch keeps working on the old key until you
replace it:

1. Remove both `vault_junos_exporter_ssh_*` vars with
   `ansible-vault edit data/vault.yml`.
2. `ansible-playbook playbooks/junos_exporter_key.yml`.
3. Paste the new `authentication ssh-rsa` line into `fksw`, replacing the old
   one.
4. Re-run the cluster playbook. The Deployment hashes the key into a pod
   annotation, so the pod rolls onto it by itself.

## What it collects

`junos_exporter_enabled_features` in the defaults, chosen against what fksw
actually is: a two-member EX4200-class virtual chassis doing layer-2
switching, PoE and LACP, with a static default route and no routing protocols
at all.

| Enabled | Why |
| --- | --- |
| `alarm`, `environment` | Chassis alarms, temperatures, fans, PSUs |
| `interfaces`, `interface_diagnostic` | Per-port counters, and optics on the `xe-` uplinks |
| `lldp` | `protocols lldp interface all` is configured |
| `lacp` | `ae1` and `ae10`-`ae14` are LACP bundles |
| `poe` | `poe interface all`, on PoE-capable hardware |
| `virtual_chassis`, `fpc` | Two preprovisioned members; per-slot state and CPU |
| `routing_engine`, `storage`, `system` | RE health, flash usage, model/serial |

Nothing for BGP, OSPF, ISIS, LDP, MPLS, EVPN, firewall filters, MACsec or
security policies, because the switch does none of those.

### Omitting a feature does not disable it

This is the trap, and it is worth stating plainly. The exporter loads its own
defaults first and then merges the config file over them, so a key the file
does not mention keeps upstream's default — and upstream defaults `alarm`,
`bgp`, `environment`, `firewall`, `interfaces`, `interface_diagnostic`,
`interface_queue`, `isis`, `ldp`, `macsec`, `ospf`, `routes`,
`routing_engine` and `system_statistics` all to **true**.

A config listing only the features you want therefore leaves nine unwanted
collectors running. On fksw that showed up as

```
level=error msg="MACsec: failed to parse 'show security macsec connections'
output: xml.unmarshal failed: XML syntax error on line 3: unexpected EOF"
```

every scrape — the exporter asking a layer-2 switch about MACsec connections
and getting an empty response back.

So `configmap.yml.j2` renders **every** key in
`junos_exporter_known_features` explicitly, `true` or `false`, and a task
asserts that everything in `junos_exporter_enabled_features` is a key the
exporter actually defines — a typo there would otherwise be silent, the
feature simply never rendering as true.

`power` is among the disabled: it runs `show chassis power`, an MX/PTX-oriented
command EX hardware does not implement, and `environment` already reports PEM
readings where they exist.

A failing collector only logs. It does not zero `junos_up` or abort the rest
of the scrape, so an empty panel means "read the pod log", not "the scrape is
broken".

Scrape interval is 60s rather than the usual 30s: a scrape is a series of
synchronous CLI commands against a decade-old control plane, and interface
counters do not move fast enough to care.

## The dashboard

`files/junos-exporter-dashboard.json`, published as a ConfigMap labelled
`grafana_dashboard: "1"`. kube-prometheus-stack runs the kiwigrid k8s-sidecar
beside Grafana watching for that label, so the dashboard appears by itself
with nothing in this role ever talking to Grafana. The chart sets
`searchNamespace: ALL`, so the ConfigMap could live anywhere; it sits in
`monitoring` because that is where the exporter is. `labelValue` is `"1"`, so
the label's value is matched as well as its presence -- the same convention
`roles/traefik`, `roles/argocd` and `roles/metallb` already use.

It is provisioned, so **edits made in the Grafana UI are overwritten on the
next Ansible run**. Change the JSON here instead.

Upstream does ship one (`example/dashboards/grafana_dashboard.json`), and it
is not worth using: five panels, two of which hard-code the author's own
device IP, one querying `junos_storage_used_percent` from the `storage`
feature, and a schema from the Grafana 6 era whose `singlestat` and `graph`
panels now survive only on Grafana's angular auto-migration. The dashboard on
grafana.com that comes up for "Junos" is SNMP-exporter-based, so its metric
names have nothing to do with this exporter.

This one is built around the features actually enabled above, and every metric
name in it was checked against the v0.16.2 collector source rather than
guessed:

| Section | What it answers |
| --- | --- |
| Health | Is it up, is anything alarming, how many ports are live |
| Routing engine | CPU, memory, temperature, uptime |
| Interfaces | Throughput per port, errors and drops, a state table |
| Optics | Rx/Tx power and transceiver temperature per port |
| Chassis and neighbours | Chassis temperatures, fan/PSU state, LLDP table |
| Aggregates and PoE | LACP member state, virtual-chassis members and VCPs, PoE draw |
| Members and storage | Per-FPC CPU and memory, filesystem usage |

Two panels earn their place before the rest. The optics graphs, because a
transceiver's receive power slides for weeks before the link actually drops --
the difference between replacing an optic on a Tuesday afternoon and finding
out about it when playout stops. And the LACP table, because a member link
dropping below `Distributing` halves a bundle's bandwidth without taking the
aggregate down, so `ae11` quietly running at half speed is not something any
other panel would show you.

Temperature is deliberately a separate panel from CPU utilisation rather than
a second axis on it, and interface receive and transmit are two panels rather
than one mirrored around zero. Two scales on one plot is the most consistently
misread thing a dashboard can do.

`$interface` is a multi-select defaulting to everything, which on a 48-port
virtual chassis is a lot of series. Narrowing it is usually the first thing to
do.

### fksw — Frikanalen network

`files/fksw-network-dashboard.json`, in the same ConfigMap (the sidecar treats
each key as its own dashboard). Where the generic one is organised by metric,
this one is organised by **what is plugged into the switch**:

| Row | What is in it |
| --- | --- |
| Switch | Up, alarms, both VC members present, chassis temperature |
| WAN (vlan 5) | `ge-0/0/47` throughput and errors, on its own |
| LACP bundles | One tile per host showing distributing members as `n/2`; bundle throughput; member table |
| vmtx1 10G and optics | `xe-0/1/2`, and Rx/Tx optical power on the three `xe-` ports |
| Broadcast chain | `ge-0/0/33`, the D9036 transport stream out -- the only port carrying video |
| Management ports | Link state and throughput for every BMC/appliance mgmt port |
| Port inventory | Every described port on the switch |

The LACP tiles are the point of it. Each is titled with the host on the other
end -- `file01`, `vm1`, `vmtx1` -- and reads
`count(junos_lacp_muxstate{aggregate="ae11"} >= 5) or vector(0)`, rendered
through value mappings as `0/2` red, `1/2` yellow, `2/2` green. So `vm1`
showing `1/2` in yellow means that bundle is up but at half bandwidth, which
nothing else notices.

The `or vector(0)` is load-bearing. `count()` over a match with no results
returns no series at all rather than zero, so a bundle that loses *every*
member would render blank -- exactly the case that matters most. Falling back
to a literal zero makes it red instead. The denominator is hard-coded at two
because every bundle here has two members; a three-member bundle would need
its own mapping.

Member ports in the table are relabelled to the thing on the other end of the
cable (`ge-0/0/18 — vm1 gbit 1`) using value mappings baked in from the
configuration, rather than a `group_left` join at query time that would break
the moment the label sets did not line up.

**This dashboard encodes port numbers.** Recabling fksw means editing the
JSON; the generic dashboard needs no such maintenance, which is why both
exist. The mapping was taken from fksw's configuration on 2026-08-23 and
checked port by port against it, `ae15` included -- so the `ae15 · vmtx1` tile
reads a red zero until that bundle is actually committed on the switch.

Two things fell out of reading that configuration, neither of which the
dashboard can fix:

- **Per-vlan traffic is not available.** The WAN vlan is trunked to tx3,
  vmtx1 and every LACP bundle, but junos_exporter reports per-port counters,
  not per-vlan ones. Only `ge-0/0/47` is pure WAN, so it is the only thing the
  WAN row can honestly show.
- **`vlan management` (id 4, `192.168.4.2`) has no member ports.** Every port
  described as `mgmt` is in `vlan internal` (id 3) along with everything else,
  which is why the management row is a naming convention rather than a
  network boundary. Worth knowing before trusting the row's name.

## Not in production

Only the dev/staging cluster runs a Prometheus (see
`playbooks/k8s_cluster_dev.yml`), so that is where this runs. There is one
switch, so one exporter watching it is the right number regardless of which
cluster hosts it — this does not want duplicating into
`playbooks/k8s_cluster_prod.yml` if that cluster ever grows a Prometheus of
its own.
