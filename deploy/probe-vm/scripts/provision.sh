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
# systemd-resolved: DHCP DNS under networkd. kbd: console keymaps + loadkeys (configurable keyboard
# layout). sudo + openssh-server: break-glass admin access (per-VM user, console + SSH over the VPN).
apt-get install -y --no-install-recommends ca-certificates curl python3 systemd-resolved kbd sudo openssh-server unattended-upgrades needrestart

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
install -D -m 0644 "$FILES/argus-hostkeys.service"     /etc/systemd/system/argus-hostkeys.service
install -D -m 0755 "$FILES/argus-firstboot.py"         /usr/local/bin/argus-firstboot.py
install -D -m 0755 "$FILES/argus-os-report.py"         /usr/local/bin/argus-os-report.py
install -D -m 0644 "$FILES/argus-os-report.service"    /etc/systemd/system/argus-os-report.service
install -D -m 0644 "$FILES/argus-os-report.timer"      /etc/systemd/system/argus-os-report.timer
install -D -m 0600 "$FILES/probe.env.example"          /etc/argus-probe/probe.env.example
install -d -m 0755 /var/lib/argus-probe

# Networking: systemd-networkd DHCPs the primary NIC. cloud-init is purged below, so networkd is the
# sole network manager - no datasource dependency, no fight over the interface.
install -D -m 0644 "$FILES/10-argus-dhcp.network"      /etc/systemd/network/10-argus-dhcp.network
systemctl enable systemd-networkd.service systemd-resolved.service

# The probe unit is installed but NOT enabled - enrollment (the seed disk or the first-boot page)
# enables it once probe.env carries a token, so an un-enrolled VM never crash-loops. The first-boot
# service IS enabled (it drives enrollment + break-glass), as is the SSH host-key regen oneshot.
systemctl enable argus-firstboot.service
systemctl enable argus-hostkeys.service
# OS patch reporter (DESIGN §14c): reports the VM's security-update count + reboot-required to Argus.
# Enabled at build; it no-ops until enrollment writes the probe token to proxy.env.
systemctl enable argus-os-report.timer

echo "==> configuring OS auto-patching (unattended-upgrades, security only)"
# A probe is "cattle": patch + reboot are fully hands-off. Security suite only, auto-reboot in a weekly
# ~03:00 window (probes buffer 7 days offline, so a ~60s reboot is invisible). needrestart auto-restarts
# services after a libc/openssl bump so most updates need no reboot at all.
cat > /etc/apt/apt.conf.d/52argus-unattended <<'UAU'
Unattended-Upgrade::Origins-Pattern {
        "origin=Debian,codename=${distro_codename}-security,label=Debian-Security";
};
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "03:00";
Unattended-Upgrade::MinimalSteps "true";
UAU
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'AU'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
AU
mkdir -p /etc/needrestart/conf.d
printf '$nrconf{restart} = %s;\n' "'a'" > /etc/needrestart/conf.d/99argus.conf

echo "==> pre-pulling the probe image (best-effort, so first boot doesn't wait on a big pull)"
docker pull ghcr.io/g-guglielmi/argus-probe:latest || true

# Point resolv.conf at resolved's stub (done last: before this, the build's own DNS must keep working
# for apt/docker; on the deployed VM systemd-resolved runs and populates the stub from DHCP).
ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf

# Drop cloud-init entirely. It has finished its build-time job (it created the packer user and grew
# the root filesystem to fill the disk on the build's first boot); the deployed appliance uses
# systemd-networkd for networking and the first-boot service (seed disk / setup page) for enrollment,
# so cloud-init is only a flaky, confusing extra on the no-datasource path. Purge it and its state so
# no clone ever runs it. (Because it's gone, the Packer shutdown step no longer runs `cloud-init clean`.)
echo "==> removing cloud-init (the appliance self-configures without it)"
apt-get purge -y cloud-init || true
rm -rf /etc/cloud /var/lib/cloud

echo "==> trimming build artifacts"
apt-get autoremove -y || true
apt-get clean
rm -rf "$FILES" /var/lib/apt/lists/*
echo "==> provision complete"
