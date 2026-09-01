// Argus probe golden image (§14a). Builds on top of the official Debian 13 (trixie) genericcloud
// qcow2 rather than a from-ISO preseed install: the cloud image boots in seconds under plain TCG
// (GitHub runners have no KVM acceleration) and already ships cloud-init, so the result is the same
// appliance with a far faster, more reliable CI build. A NoCloud seed CD gives Packer an SSH login;
// provisioning installs Docker + the probe; the shutdown step strips machine identity and the build
// user so every deployed clone is unique and carries no shared credential.

packer {
  required_plugins {
    qemu = {
      source  = "github.com/hashicorp/qemu"
      version = "~> 1.1"
    }
  }
}

variable "debian_image_url" {
  type        = string
  default     = "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2"
  description = "Base Debian cloud image (qcow2)."
}

variable "debian_image_checksum" {
  type        = string
  default     = "file:https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS"
  description = "Checksum of the base image; 'file:<url>' pulls Debian's published SHA512SUMS."
}

variable "output_directory" {
  type    = string
  default = "output"
}

variable "vm_name" {
  type    = string
  default = "argus-probe-vm.qcow2"
}

variable "disk_size" {
  type    = string
  default = "8G"
}

source "qemu" "argus-probe" {
  iso_url      = var.debian_image_url
  iso_checksum = var.debian_image_checksum
  disk_image   = true // the source is a bootable disk, not an install ISO
  disk_size    = var.disk_size
  format       = "qcow2"

  accelerator = "none" // TCG: works without nested KVM (GitHub runners); the cloud image still boots fast
  cpus        = 2
  memory      = 2048
  headless    = true

  // NoCloud seed CD so cloud-init creates the build-only 'packer' user for SSH.
  cd_label = "cidata"
  cd_files = ["./build-seed/user-data", "./build-seed/meta-data"]

  disk_interface = "virtio"
  net_device     = "virtio-net"

  ssh_username = "packer"
  ssh_password = "packer"
  ssh_timeout  = "15m" // first boot + cloud-init user creation + apt can take a while under TCG

  output_directory = var.output_directory
  vm_name          = var.vm_name
  disk_compression = true

  // Strip identity and the build user in a single SSH command, then power off. Doing the ssh-host-key
  // and machine-id removal here (not in provision.sh) keeps the live build session alive - a new SSH
  // connection would fail once the host keys are gone. Order: clean, wipe identity, drop the build
  // user, shut down.
  shutdown_command = "sudo bash -c 'cloud-init clean --logs --seed; rm -f /etc/ssh/ssh_host_* /etc/machine-id /var/lib/dbus/machine-id; touch /etc/machine-id; userdel -f -r packer 2>/dev/null || true; shutdown -P now'"
  shutdown_timeout = "5m"
}

build {
  sources = ["source.qemu.argus-probe"]

  // No trailing slash on source: uploads the directory itself, creating /tmp/files (the destination
  // dir need not pre-exist, unlike the "contents" form).
  provisioner "file" {
    source      = "files"
    destination = "/tmp"
  }

  provisioner "shell" {
    execute_command = "sudo -E bash '{{ .Path }}'"
    script          = "scripts/provision.sh"
  }
}
