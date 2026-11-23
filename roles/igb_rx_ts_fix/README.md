# igb_rx_ts_fix

Builds a patched `igb` driver with DKMS so NIC hardware timestamping (chrony
`hwtimestamp`, later ptp4l) can be used on the I350/I210 ports without
breaking jumbo frames. Delete this role once every host runs a kernel whose
own igb has the fix; the role tells you when that is.

## The bug

Since commit 5379260852b0 ("igb: Fix XDP with PTP enabled", v5.13),
`igb_clean_rx_irq()` checks for the 16-byte timestamp header (TSIP) on every
receive buffer, not only the first buffer of a frame. With MTU 9000 a frame
spans three 3072-byte buffers; when timestamping is on (any filter becomes
"ALL" on 82580/I350/I210) and a later buffer happens to start with eight zero
bytes, 16 bytes of payload are cut out. The kernel then drops the frame
("total length 9000 > length 8968"). Small packets and ping patterns pass, so
links look healthy while bulk traffic of zero-filled data stalls. This took
Proxmox Ceph down on 2026-11-22.

Fix: "igb: only strip Rx timestamp header on the first buffer of a frame"
(Tjerk Kusters, v7, patch 2/3, intel-wired-lan). Not in mainline when this
role was written. Patches 1/3 and 3/3 (XDP/AF_XDP) ride along where they apply.

## What it does

- On the controller, once per run: reads mainline `igb_main.c` from
  torvalds/linux and reports whether the fix has landed. It looks at the code
  (the `!skb &&` guard on the TSIP check), so a reworded commit still counts.
- Skips hosts without igb ports.
- For each installed kernel with headers: downloads igb from the matching
  stable tag (gregkh/linux `v<x.y.z>`), checks whether that kernel's own igb
  already has the fix, and if not applies the patches and installs a DKMS
  module (`igb-fk`, `/updates/dkms/igb.ko`) for that kernel.
- Writes `/etc/igb-rx-ts-fix/<uname -r>` = `dkms` or `intree-fixed`.
- Says per host whether the patched module is loaded or a reboot is needed.

## How it stays safe

`roles/time_sync` never writes `hwtimestamp` into chrony.conf. A guard
(`chrony-hwts-guard`, chrony ExecStartPre) turns it on at each chrony start
only if the loaded igb is the DKMS build for the running kernel (same
srcversion) or the kernel's own igb is fixed. Otherwise it leaves
timestamping off and clears any NIC filter left from before. The guard
runs on every `ptp_hosts` member without ptp4l, also with
`time_sync_ntp_hwtimestamp` off, so switching it off clears the NIC too. So a new kernel
that has no patched build yet just runs without NIC timestamps.

## Maintenance

- The role installs headers per installed kernel and never the headers meta
  packages, which would drag in a new kernel series. A kernel installed by
  apt gets its headers and build at the next run of this role; boot it before
  that and the guard simply keeps NIC timestamping off.
- A new stable version (6.14.8 → 6.14.11, or a new PVE series): rerun the
  role. Until then the guard keeps timestamping off; nothing breaks.
- Patch no longer applies to a new stable version: the run fails for that
  kernel with the patch output. The guard keeps timestamping off there.
- Fix in mainline and backported: the role reports `intree-fixed` and builds
  nothing. Remove the role and the `updates/dkms/igb.ko` files then.

Loading the module needs a reboot or `modprobe -r igb && modprobe igb`; both
drop the host's bond (both ports are igb), so drain/`noout` as for a reboot.

```
ansible-playbook playbooks/site.yml --tags igb_rx_ts_fix --limit vm1
```
