variable "proxmox_url" {
  type        = string
  description = "Proxmox API URL, e.g. https://192.168.3.70:8006/api2/json"
}

variable "proxmox_token_id" {
  type        = string
  description = "Proxmox API token id in the form user@realm!tokenname"
}

variable "proxmox_token_secret" {
  type        = string
  sensitive   = true
  description = "Proxmox API token secret, obtained from Proxmox cluster dashboard"
}

variable "template" {
  type        = string
  description = "Name of the Ubuntu cloud-init template"
  default     = "ubuntu-24.04-cloud"
}


variable "kube_nodes" {
  type = map(object({
    vmid    = number
    node    = string
    ip_cidr = string
  }))
}
