locals {
  # Load the hosts from the infra repo's host database
  hosts_all = yamldecode(file("${path.module}/../data/hosts.yml")).host_config

  # Hosts without a vm stanza are ignored; those with are flattened
  hosts = {
    for name, h in local.hosts_all :
    name => merge(
      {
        ip_cidr = h.ip_cidr
        # The correct value is almost always "none". Use "unsafe" *only* for ephemeral hosts on HDDs.
        # "unsafe" will "lie" to the guest OS about whether writes to disk have been committed.
        # It speeds disk I/O at the cost of almost certain data loss on host power failure.
        # We should probably remove this option as soon as vm1..vm4 are on SSDs.
        disk_cache = try(h.vm.disk_cache, "none")
        cores      = try(h.vm.cores, 8)
        memory_max = try(h.vm.memory_max, 16384)
        memory_min = try(h.vm.memory_min, 8192)
      },
      h.vm
    )
    if try(h.vm, null) != null
  }
}

resource "proxmox_vm_qemu" "kube" {
  for_each = local.hosts

  machine     = "q35"
  name        = each.key # "prod-kube-1" .. "prod-kube-4"
  vmid        = each.value.vmid
  tags        = "ubuntu"
  target_node = each.value.node # "vm1" .. "vm4"

  clone = "ubuntu-24.04-cloud"

  cpu {
    # note this creates portability problems if CPU cores in cluster have different features
    type    = "host"
    sockets = 1
    cores   = each.value.cores
    numa    = true
  }

  serial {
    id = 0
  }

  memory  = each.value.memory_max
  balloon = each.value.memory_min

  # Disks
  scsihw = "virtio-scsi-single"

  disks {
    scsi {
      scsi0 {
        disk {
          size     = "40G"
          storage  = "localssd-lvm"
          cache    = each.value.disk_cache
          iothread = true
          discard  = true
        }
      }
    }
    ide {
      ide2 {
        cloudinit {
          storage = "localssd-lvm"
        }
      }
    }
  }

  # Network

  network {
    id     = 0
    model  = "virtio"
    bridge = "vmbr0"
    queues = 4 # consider 8 in production
  }

  # Cloud-init
  os_type   = "cloud-init"
  ciuser    = "ansible"
  sshkeys   = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGQNn2dIm6s2ybuJXphkIRYxlubNrohoMlhW9XSNpvSw frikanalen ansible init"
  ipconfig0 = "ip=${each.value.ip_cidr},gw=192.168.3.2"

  nameserver   = "192.168.3.2"
  searchdomain = "dc1.frikanalen.no frikanalen.no"

  agent  = 1
  onboot = true
}

output "kube_nodes_ips" {
  description = "Kubernetes node IPs by name"
  value = {
    for name, vm in proxmox_vm_qemu.kube :
    name => vm.default_ipv4_address
  }
}
