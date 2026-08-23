# node_exporter

Installs Prometheus's `node_exporter` on the hosts that are not Kubernetes
nodes — the Proxmox hypervisors (`vm1`–`vm4`, `vmtx1`) and `file01`.

The kube nodes already have one: the `kube-prometheus-stack` chart runs a
`node-exporter` DaemonSet on every node it can schedule a pod on (see
`roles/kube_prometheus_stack`). That is exactly the set of hosts this role does
*not* cover, and why the inventory group `node_exporter_hosts` is defined the
way it is.

This role only installs and configures the exporter. Nothing here talks to
Prometheus — the scrape side is `roles/node_exporter_scrape`, which runs
against the dev cluster from `playbooks/k8s_cluster_dev.yml`. Run one without
the other and you get either an exporter nobody reads or a Prometheus target
that refuses connections.

## Flags

Debian's package puts its flags in the packaged unit's `ExecStart` and ships an
empty `ARGS` in `/etc/default/prometheus-node-exporter`. That makes the flag set
depend on the packaging, and turns anything this role adds into a possible
duplicate — `node_exporter` rejects a repeated
`--collector.textfile.directory` outright rather than letting the last one win.

So there is a systemd drop-in that clears `ExecStart` and re-forms it as
`prometheus-node-exporter $ARGS`, and everything the exporter is told lives in
`/etc/default/prometheus-node-exporter`, rendered from this role's defaults.

## The ZFS textfile collector

`file01` holds the media archive on ZFS, which makes it the one host here where
"the disks are fine" is worth more than a guess.

`node_exporter` has a built-in `zfs` collector and it is enabled — but all it
can see is `/proc/spl/kstat/zfs`, which carries the ARC counters and per-pool
I/O and nothing else. **Pool capacity, fragmentation, scrub history, per-disk
error counters and per-dataset usage are not in kstat at all.** They exist only
in the output of `zpool(8)` and `zfs(8)`. There is no collector flag that turns
them on, because there is nothing there to turn on.

Filling that gap means either a second daemon (`zfs_exporter`, not packaged in
Debian) or `node_exporter`'s textfile collector, which is the mechanism
upstream ships for exactly this: it reads `*.prom` files out of a directory and
serves their contents as if it had gathered them itself.

`files/zfs-textfile-collector.sh` is that script. A systemd timer runs it every
minute; it writes `zfs.prom` into `/var/lib/prometheus/node-exporter` via a
temporary file and `rename(2)`, because the textfile collector will happily read
a half-written file and report the truncated last line as a parse error.

What it adds, none of which the built-in collector can produce:

| Metric | From |
| --- | --- |
| `zfs_pool_health`, `zfs_pool_{size,allocated,free}_bytes`, `zfs_pool_capacity_ratio`, `zfs_pool_fragmentation_ratio`, `zfs_pool_dedup_ratio` | `zpool list` |
| `zfs_pool_last_scrub_timestamp_seconds`, `zfs_pool_last_scrub_errors`, `zfs_pool_{scrub,resilver}_in_progress`, `zfs_pool_data_errors`, `zfs_pool_device_errors_total`, `zfs_pool_device_state` | `zpool status` |
| `zfs_dataset_{used,available,referenced,logical_used,used_by_snapshots,quota}_bytes` | `zfs list` |
| `zfs_scrape_success`, `zfs_scrape_duration_seconds` | the script itself |

`zfs_pool_last_scrub_timestamp_seconds` is `0` for a pool that has never
completed a scrub. That is deliberate: it makes the "overdue scrub" alert in
`roles/node_exporter_scrape` fire for a pool nobody has ever scrubbed, which is
the same problem as one scrubbed far too long ago.

Installation is conditional on `zpool(8)` being present, not on the host being
`file01`. The Proxmox hosts can have local pools too, and a host that grows one
later starts being watched on the next Ansible run without an inventory edit.

### The other textfile collectors

Debian's `prometheus-node-exporter` recommends
`prometheus-node-exporter-collectors`, so installing the exporter also pulls in
that package's own timers — `smartmon`, `nvme`, `ipmitool-sensor` and `apt` —
which write into the same textfile directory and are picked up automatically.
Nothing in this role asks for them.

That is mostly a gift: on `file01`, `smartmon.prom` carries SMART attributes for
every disk under the `archive` pool, which is the one thing that can warn you
about a disk *before* ZFS starts logging checksum errors against it. It is also
by far the biggest thing in the scrape — about 1700 series, roughly a quarter of
`file01`'s total. Neither the dashboards nor the alerting rules in
`roles/node_exporter_scrape` use it yet.

### Editing the script

It is deliberately defensive: it never uses `set -e`, it reports
`zfs_scrape_success 0` rather than writing a partial file when `zpool` fails,
and it skips the `-` that ZFS prints for an unset value instead of emitting a
line that will not parse. If you change it, check the output before trusting it:

```sh
/usr/local/bin/zfs-textfile-collector /tmp && promtool check metrics < /tmp/zfs.prom
```

`zpool` blocks indefinitely on a suspended pool — precisely the situation this
is meant to report on — so the unit sets `TimeoutStartSec`. When that bites, the
metrics go stale rather than wrong, and `ZfsMetricsStale` is what tells you.
