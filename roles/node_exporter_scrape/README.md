# node_exporter_scrape

The cluster half of node_exporter monitoring: points the Prometheus installed
by `roles/kube_prometheus_stack` at the exporters that `roles/node_exporter`
installs on `file01` and the Proxmox hypervisors, and ships the dashboards and
alerting rules for them.

Runs against the **dev** cluster, because that is where the only Prometheus in
the estate lives. The hosts it scrapes are not dev hosts — `file01` and the
hypervisors are production infrastructure. That asymmetry is a property of
where the monitoring stack happens to be installed, not a statement about the
hosts.

## How the targets get in

The hosts are not in the cluster and never will be, so there are no pods to
discover. Instead:

- a **Service** with `clusterIP: None` and **no selector**, which exists only to
  name the job and give the ServiceMonitor something to match;
- an **Endpoints** object written by hand, rendered from the inventory group
  `node_exporter_hosts` and the `ip_cidr` values in `data/hosts.yml`;
- a **ServiceMonitor** selecting that Service.

This is the Prometheus Operator's supported way of scraping something
Kubernetes does not run. The Service having no selector is load-bearing: give
it one and the endpoints controller takes ownership of the Endpoints object and
deletes every address in it.

Each address carries a `hostname`, which reaches Prometheus as
`__meta_kubernetes_endpoint_hostname` and is relabelled to `instance`. Without
it every target would be an IP address and reading a dashboard would mean
keeping `data/hosts.yml` open next to it.

Adding a host to `node_exporter_hosts` in `inventory.yaml` is the whole change
needed at both ends — this role picks up the target, `roles/node_exporter`
installs the exporter — provided the host has an `ip_cidr` in `data/hosts.yml`.
The role asserts that rather than rendering a broken Endpoints object.

## The job name

`job="node-exporter"` — deliberately the **same** job as the chart's own
node-exporter DaemonSet.

This role originally used a separate `node-exporter-external`, on the reasoning
that mixing the two host sets would let a dashboard average them together. That
was the wrong trade. The upstream node-exporter mixin the chart ships — the
"Node Exporter / Nodes" and "USE Method" dashboards, and ~21 alerting rules —
hardcodes `job="node-exporter"` in every query and in its `instance` dropdown.
Under any other job name these hosts are invisible to all of it, and get none of
those rules. Sharing the job is the only way in.

It is set the way the chart sets it: a `jobLabel: node-exporter` label on the
Service plus `spec.jobLabel: jobLabel` on the ServiceMonitor. Both are
first-class fields, so this does not depend on where the operator happens to
order user relabelings relative to its own.

What separates the two host sets now is `nodeclass="external"`, a label on the
Service copied onto every target by the ServiceMonitor's `targetLabels`. This
role's own alerting rules select on it rather than on a job, so they stay scoped
to these six hosts instead of firing a second time for every kube node.

Note for anything filtering on it: the kube nodes carry no `nodeclass` label at
all, so a Grafana variable over it needs `allValue: ".*"` — Grafana's default
"All" expands to an alternation of known values, which would silently exclude
every target missing the label.

## Alerts

A `PrometheusRule` in two groups: general host health (down, filesystems full
or filling, memory, load, reboots, failed systemd units) and ZFS (pool not
`ONLINE`, data errors, per-disk error counters, capacity, fragmentation,
overdue scrubs).

Because the job is now shared, the mixin's ~21 node rules apply to these hosts
too, and several overlap with the general group here:
`NodeFilesystemAlmostOutOfSpace` against `NodeFilesystemAlmostFull`,
`NodeMemoryHighUtilization` against `NodeMemoryPressure`,
`NodeSystemdServiceFailed` against `NodeSystemdUnitFailed`,
`NodeSystemSaturation` against `NodeLoadHigh`. That duplication is deliberate —
these thresholds are ours to tune — but it does mean two alerts for one
problem. Drop the local rule if the mixin's turns out to say it better. The ZFS
group has no upstream equivalent and duplicates nothing.

Two of the ZFS rules watch the collector rather than the pools.
`ZfsMetricsStale` fires on `node_textfile_mtime_seconds` for `zfs.prom` going
stale, and `ZfsCollectorFailing` on `zfs_scrape_success == 0`. The ZFS metrics
come from a systemd timer on the host, entirely separate from `node_exporter`
itself; if it stops, every other ZFS alert goes quiet on frozen data instead of
firing, which is the worst possible failure mode for the thing this exists to
watch.

The `release: kube-prometheus-stack` label on the rule object is required. The
chart's `ruleSelector` matches on it, and unlike the ServiceMonitor selectors
`roles/kube_prometheus_stack` does not disable that one — without the label the
rules are simply never loaded.

Everything fires into the Alertmanager that `roles/kube_prometheus_stack`
installs, which at the time of writing **has no receiver configured**: alerts
appear in its UI and in Grafana, and nowhere else. That is worth fixing and is
not this role's job.

## Dashboards

Two, in one ConfigMap, picked up by the Grafana sidecar via the
`grafana_dashboard` label — the same mechanism as `roles/junos_exporter`.

- **Hosts (node_exporter)** — the estate at a glance, then CPU, memory,
  filesystems, disk and network per host. Its job/host variables read from
  `node_uname_info`, so it also works against the kube nodes' DaemonSet if you
  switch the job.
- **ZFS** — pools, disks and vdevs with their error counters, capacity and
  fragmentation, datasets, throughput, and the ARC. Panels are annotated with
  what a healthy shape looks like, since most of these are numbers you only ever
  look at when something is already wrong.

Both make heavy use of table panels, whose Prometheus targets must carry
`"format": "table"` (and `instant`). Without it the datasource returns a
time-series frame, the label columns the `joinByField`/`organize` transforms
expect are not there, and the panel renders an unhelpful "No data".

Both are generated files. Edits made in Grafana's UI are overwritten on the next
Ansible run.
