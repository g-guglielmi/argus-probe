#!/usr/bin/env bash
# Provision the Argus probe golden image on top of the stock Debian cloud image: install Docker + the
# probe container, install the probe systemd unit + first-boot enrollment fallback, then leave the VM
# un-enrolled (enrollment happens at deploy time via cloud-init or the first-boot page). Identity is
# stripped in the Packer shutdown_command, not here, so the live build SSH session isn't cut off.
set -euo pipefail

echo "==> waiting for cloud-init to finish its build-seed run"
cloud-init status --wait || true

export DEBIAN_FRONTEND=noninteractive
echo "==> installing base packages"
apt-get update
# systemd-resolved is a separate package on Debian 13; needed so DHCP DNS works under networkd.
apt-get install -y --no-install-recommends ca-certificates curl python3 systemd-resolved

echo "==> installing Docker Engine"
# Official convenience script: adds Docker's apt repo and installs docker-ce. Pinned enough for an
# appliance; the container itself is what carries the monitoring logic.
curl -fsSL https://get.docker.com | sh
systemctl enable docker

echo "==> installing argus-probe units and files"
FILES=/tmp/files
install -D -m 0644 "$FILES/argus-probe.service"        /etc/systemd/system/argus-probe.service
install -D -m 0644 "$FILES/argus-updater.service"      /etc/systemd/system/argus-updater.service
install -D -m 0644 "$FILES/argus-firstboot.service"    /etc/systemd/system/argus-firstboot.service
install -D -m 0755 "$FILES/argus-firstboot.py"         /usr/local/bin/argus-firstboot.py
install -D -m 0644 "$FILES/90-argus-datasources.cfg"   /etc/cloud/cloud.cfg.d/90-argus-datasources.cfg
install -D -m 0600 "$FILES/probe.env.example"          /etc/argus-probe/probe.env.example
install -d -m 0755 /var/lib/argus-probe

# Networking: systemd-networkd DHCPs the primary NIC regardless of cloud-init (which Debian disables
# when no datasource is attached). cloud-init is told not to manage networking so they never fight.
install -D -m 0644 "$FILES/10-argus-dhcp.network"      /etc/systemd/network/10-argus-dhcp.network
install -D -m 0644 "$FILES/91-argus-network.cfg"       /etc/cloud/cloud.cfg.d/91-argus-network.cfg
systemctl enable systemd-networkd.service systemd-resolved.service

# The probe unit is installed but NOT enabled - enrollment (cloud-init runcmd or the first-boot page)
# enables it once probe.env carries a token, so an un-enrolled VM never crash-loops. The first-boot
# fallback IS enabled: it no-ops when cloud-init already supplied a token.
systemctl enable argus-firstboot.service

echo "==> pre-pulling the probe image (best-effort, so first boot doesn't wait on a big pull)"
docker pull ghcr.io/g-guglielmi/argus-probe:latest || true

# Point resolv.conf at resolved's stub (done last: before this, the build's own DNS must keep working
# for apt/docker; on the deployed VM systemd-resolved runs and populates the stub from DHCP).
ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf

echo "==> trimming build artifacts"
apt-get clean
rm -rf "$FILES" /var/lib/apt/lists/*
echo "==> provision complete"
