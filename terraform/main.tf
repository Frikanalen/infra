locals {
  ansible_inventory_hosts = {
    for name, cfg in var.kube_nodes :
    name => {
      ansible_host = split("/", cfg.ip_cidr)[0]
    }
  }

  kube_ci_user = "ubuntu"

  kube_nodes_merged = {
    for name, cfg in var.kube_nodes : name => cfg
  }
}

resource "proxmox_vm_qemu" "kube" {
  for_each = local.kube_nodes_merged

  name        = each.key # "prod-kube-1" .. "prod-kube-4"
  tags        = ""
  vmid        = each.value.vmid
  target_node = each.value.node # "vm1" .. "vm4"

  clone = "ubuntu-24.04-cloud"

  cpu {
    sockets = 1
    cores   = 30
  }

  serial {
    id = 0
  }

  memory = 32768

  # Disks
  scsihw = "virtio-scsi-pci"

  disks {
    scsi {
      scsi0 {
        disk {
          size    = "40G"
          storage = "local-lvm"
        }
      }
    }
    ide {
      ide2 {
        cloudinit {
          storage = "local-lvm"
        }
      }
    }
  }

  # Network

  network {
    id     = 0
    model  = "virtio"
    bridge = "vmbr0"
  }

  # Cloud-init
  os_type   = "cloud-init"
  ciuser    = "ubuntu"
  sshkeys   = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGQNn2dIm6s2ybuJXphkIRYxlubNrohoMlhW9XSNpvSw frikanalen ansible init"
  ipconfig0 = "ip=${each.value.ip_cidr},gw=192.168.3.1"

  # FIXME: Placeholder!
  nameserver   = "8.8.8.8"
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
