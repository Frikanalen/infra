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
        disk_cache     = try(h.vm.disk_cache, "none")
        cores          = try(h.vm.cores, 8)
        memory_max     = try(h.vm.memory_max, 16384)
        memory_min     = try(h.vm.memory_min, 8192)
        template       = try(h.vm.template, var.template)
        storage        = try(h.vm.storage, "localssd-lvm")
        ceph_storage   = try(h.vm.ceph_storage, "local-lvm")
        ceph_disk_size = try(h.vm.ceph_disk_size, null)
        pci_mappings   = try(h.vm.pci_mappings, [])
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

  clone = each.value.template

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
          storage  = each.value.storage
          cache    = each.value.disk_cache
          iothread = true
          discard  = true
        }
      }
      dynamic "scsi1" {
        for_each = each.value.ceph_disk_size == null ? [] : [each.value.ceph_disk_size]
        content {
          disk {
            size      = scsi1.value
            storage   = each.value.ceph_storage
            cache     = "none"
            iothread  = true
            discard   = true
            backup    = false
            replicate = false
            serial    = "ceph-osd-${each.value.vmid}"
          }
        }
      }
    }
    ide {
      ide2 {
        cloudinit {
          storage = each.value.storage
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

  # PCIe passthrough (e.g. GPUs, capture cards), referencing cluster PCI
  # Resource Mappings (Datacenter > Resource Mappings) by name rather than
  # raw host PCI addresses: the Proxmox API rejects raw hostpciN assignment
  # from anything but a root@pam ticket session, so API-token-driven
  # Terraform must go through a mapping. Host-side driver binding to
  # vfio-pci is handled by Proxmox at VM start.
  dynamic "pcis" {
    for_each = length(each.value.pci_mappings) > 0 ? [each.value.pci_mappings] : []
    content {
      dynamic "pci0" {
        for_each = length(pcis.value) > 0 ? [pcis.value[0]] : []
        content {
          mapping {
            mapping_id = pci0.value
            pcie       = true
          }
        }
      }
      dynamic "pci1" {
        for_each = length(pcis.value) > 1 ? [pcis.value[1]] : []
        content {
          mapping {
            mapping_id = pci1.value
            pcie       = true
          }
        }
      }
      dynamic "pci2" {
        for_each = length(pcis.value) > 2 ? [pcis.value[2]] : []
        content {
          mapping {
            mapping_id = pci2.value
            pcie       = true
          }
        }
      }
    }
  }

  # Cloud-init
  os_type   = "cloud-init"
  ciuser    = "ansible"
  sshkeys   = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGQNn2dIm6s2ybuJXphkIRYxlubNrohoMlhW9XSNpvSw frikanalen ansible init"
  ipconfig0 = "ip=${each.value.ip_cidr},gw=192.168.3.2"

  nameserver   = "192.168.3.2"
  searchdomain = "dc1.frikanalen.no"

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
