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
  picks the master; the rest are `slaveOnly`. Only with
  `time_sync_ptp_enabled: true`, which is off: these hosts sit on LACP bonds
  with VLANs on top, and Linux gives no hardware timestamps through an
  802.3ad bond (see defaults/main.yml). Until then they run chrony on NTP.
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

## NTP hardware timestamps (off: see time_sync_ntp_hwtimestamp)

While PTP is off, the `ptp_hosts` (Proxmox hosts and file01) run chrony with
`hwtimestamp *` and `xleave` to each other. Since Linux 4.13 chrony maps a
packet on a bond, VLAN or bridge to the physical port it used, so the LACP
bonds don't get in the way. igb (I350/I210) gives hardware RX and TX
timestamps; vmtx1's bnx2x only timestamps PTP frames on RX, so it gets
hardware TX and kernel RX. Expect single-digit µs between hosts across fksw.

### Incident 2026-11-22: why it is off

`hwtimestamp *` sets the I350 (igb) ports on vm1-4 to rx_filter ALL. With
MTU 9000 on the LACP bond, every full-size received frame then arrived 32
bytes short (`total length 9000 > length 8968`) and was dropped. Small
packets (pings, heartbeats, handshakes) still passed, so Ceph's OSD-to-OSD
traffic stalled silently: degraded from 09:58Z, fully down 10:20-11:32Z.
Removing the line and restarting chrony does not reset the NIC; it takes a
SIOCSHWTSTAMP ioctl (rx_filter NONE, tx OFF) or a driver reload. Don't turn
`time_sync_ntp_hwtimestamp` on for jumbo-frame NICs without first testing a
sender MTU below ~8960 on a non-Ceph host.
