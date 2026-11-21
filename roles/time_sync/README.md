# time_sync

Keeps every host on one clock. Run by `playbooks/site.yml` (tag `time_sync`).

```
 Meinberg stratum 1 (77.40.226.121) ─┐
 simula 192.168.3.1 (stratum 2) ─────┼─ NTP ─> vm4  (PTP grandmaster: chrony + phc2sys
 no.pool.ntp.org ────────────────────┘               drives the I350's hardware clock)
                                                    │
                          PTP, layer 2 through fksw │ (no transparent clock)
                                                    ▼
                    vm1, vm2, vm3, vmtx1, file01  (ptp4l slave-only; chrony reads the
                                                    NIC clock as refclock "PTP")
                                                    │
                                ptp_kvm, per host   ▼
             every Linux VM on that host  (chrony refclock "HOST" = /dev/ptp_kvm,
                                           NTP to the Proxmox hosts as fallback)
             util1 (FreeBSD)              (ntpd to the Proxmox hosts by address)
             tx3                          (chrony to the Proxmox hosts; no PHC on its NIC)
```

## Inventory groups

- `time_servers` (the Proxmox hosts): NTP upstream by IP address, and serve NTP
  to `time_sync_allow`.
- `ptp_hosts` (Proxmox hosts + file01): physical hosts whose NIC has a hardware
  clock (Intel I350/I210, Broadcom BCM57800). `time_sync_ptp_grandmaster`
  picks the master; the rest are `slaveOnly`.
- `qemu_guest`: Linux VMs load `ptp_kvm` and prefer their host's clock.

## Expected accuracy

- Physical hosts to each other: a few µs (PTP with hardware timestamps, but fksw
  is an EX4200-class switch with no PTP support, so its queueing is not
  corrected).
- A VM to its host: about 1 µs or better (ptp_kvm).
- The fleet to UTC: about 0.1–1 ms, limited by NTP from the Meinberg over the
  internet. A PPS source on the grandmaster (the cesium clock, via
  `time_sync_pps_refclock`) would take that to the µs range. Note that a PPS
  only marks the second; NTP still supplies which second it is, so keep the
  upstream servers alongside it (`lock`).

## Checking

```
chronyc -n sources -v     # PTP / HOST refclock should carry the '*'
chronyc tracking
pmc -u -b 0 'GET CURRENT_DATA_SET'   # on ptp_hosts: offsetFromMaster in ns
journalctl -u ptp4l-lan -f
```
