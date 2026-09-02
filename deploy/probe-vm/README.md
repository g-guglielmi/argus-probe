# Argus probe golden image (`deploy/probe-vm/`)

A self-configuring **Debian 13 VM** that runs the `argus-probe` container and self-enrolls on first
boot — the VM equivalent of the "Add probe" Docker command, for sites where a full VM is easier to
drop in than a Docker host. Implements DESIGN **§14a** (delivery vs. enrollment, decoupled).

The VM is a thin wrapper: it boots, reads an **enrollment URL + token** (from cloud-init, an attached
**seed CD**, or the first-boot setup page), and starts the stock `ghcr.io/g-guglielmi/argus-probe`
container, which generates its key + CSR locally and redeems the single-use token against `/api/enroll`.
The private key never leaves the VM; the token is single-use, so keep the disk persistent.

## What's here

| File | Purpose |
|------|---------|
| `argus-probe-vm.pkr.hcl` | Packer template — builds the golden image (qcow2). |
| `build-seed/` | Build-only cloud-init seed so Packer can SSH into the base cloud image. |
| `scripts/provision.sh` | Installs Docker + the probe units into the image. |
| `scripts/make-ova.sh` | Packages the built qcow2 into an OVA (stream-optimized VMDK + OVF + manifest). |
| `files/argus-probe.service` | systemd unit that runs the probe container from `/etc/argus-probe/probe.env`. |
| `files/argus-firstboot.service` + `argus-firstboot.py` | First-boot enrollment: adopts an attached **seed CD** (label `ARGUSSEED`) if present, else serves a setup page; also sets the console keyboard layout and generates + reports the **break-glass** credential. No-ops once enrolled + reported. |
| `files/argus-hostkeys.service` | Regenerates SSH host keys on first boot (they're stripped from the golden image; cloud-init used to do this). |
| `files/probe.env.example` | Reference for the `probe.env` / seed-disk `ARGUS.ENV` contract. |

## Design note — built from the Debian cloud image, not a preseed install

DESIGN §14a sketched a from-ISO `debian-installer` preseed. We build on the official **Debian 13
`generic` qcow2** instead: GitHub runners have no nested-KVM acceleration, so a from-ISO install
under TCG would be painfully slow and flaky, whereas the cloud image boots in seconds and yields the
same appliance. Packer attaches a throwaway NoCloud seed to get an SSH login, provisions, then
**strips machine-id, SSH host keys, and the build user** in the shutdown step so every deployed clone
is unique and carries no shared credential.

We use the **`generic`** variant (full driver set), not `genericcloud` (virtio-only): the **OVA** has
to boot on non-virtio hypervisors (VMware/VirtualBox SCSI/SATA) and the first-boot **seed CD** needs
isofs + a CD-ROM driver, both trimmed out of the cloud kernel. `generic` is a superset, so XCP-NG/KVM
are unaffected; it's only marginally larger.

**cloud-init is removed from the deployed image.** It does its build-time job (create the build user,
grow the root filesystem to fill the disk) and is then purged in `provision.sh`, so the appliance
self-configures entirely through systemd-networkd (DHCP) + the first-boot service (enrollment). This
drops the flaky, confusing dependency on cloud-init's datasource detection (fiddly on XCP-NG). The
first-boot service regenerates SSH host keys (`argus-hostkeys.service`) and systemd regenerates the
machine-id, so clones stay unique.

## Break-glass console access

The golden image ships with **no login**. On first boot the service generates a per-VM admin user
**`argus`** (in the `sudo` and `docker` groups, so it can run `docker` without sudo) with a random
password and reports it to Argus over the probe check-in channel;
Argus stores it **encrypted** and reveals it to admins on the **Probes** page (the **Console** button).
Use it at the hypervisor console, or over SSH once you're on the site VPN (remote sites are
outbound-only, so SSH isn't internet-reachable). The password is cached root-only on the VM and the
report is retried until it lands, so a brief core outage during enrollment doesn't lose it.

The **console keyboard layout** is configurable per-VM (it matters for typing that password at the
console): pick it in Add-probe → **VM**, or on the first-boot setup page. It's written to
`/etc/vconsole.conf` on first boot; the default is `us`.

## Building

CI (`.github/workflows/probe-vm.yml`) builds it — `packer validate` on every change under this
directory, and a full build on **workflow_dispatch** or a **`probe-vm/v*`** tag (which also publishes
a GitHub Release with the image assets). Outputs: `argus-probe-vm.qcow2` (~800 MB), `argus-probe-vm.vhd`
(~2 GB), and `argus-probe-vm.ova` (~800 MB — a stream-optimized VMDK + OVF). The Release ships the OVA
and qcow2 as-is and the VHD **gzipped** (`argus-probe-vm.vhd.gz`, GitHub's 2 GiB asset cap); the raw
VHD is available from the run's workflow artifacts.

Locally (needs Packer + QEMU):

```bash
cd deploy/probe-vm
packer init argus-probe-vm.pkr.hcl
packer build argus-probe-vm.pkr.hcl        # -> output/argus-probe-vm.qcow2
```

## Deploying

**Import the disk.** Pick the format for your hypervisor:

- **VMware / Nutanix / VirtualBox:** import `argus-probe-vm.ova` directly.
- **XCP-NG / Xen Orchestra:** either import `argus-probe-vm.ova` (**Import → OVA** — creates a
  ready-to-run VM), or `gunzip argus-probe-vm.vhd.gz` and upload the `.vhd` as a VDI (Xen Orchestra →
  Import, or `xe vdi-import`), then create a VM (1–2 vCPU, 2 GB RAM) and attach it as the boot disk.
- **KVM / libvirt:** `argus-probe-vm.qcow2` imports directly; the `.vhd` also imports on Hyper-V.

**Give it the enrollment inputs** — either way (in Argus **Add probe → VM**, which also has the
keyboard-layout picker):

- **seed CD (zero-touch):** **Download seed ISO** and attach it as a CD/DVD when creating the VM. It's
  an Argus-owned ISO (label `ARGUSSEED`) read by the first-boot service, so it works on any hypervisor
  — no cloud-init needed. The VM enrols on first boot with no interaction.
- **first-boot page:** boot with no seed (needs DHCP to be reachable), browse to `http://<vm-ip>/`,
  and paste the enrollment URL + token from the wizard (and pick the keyboard layout). The page
  disappears once the probe enrols.

The probe registers with Argus and appears on the **Probes** page; its break-glass console credential
is revealed there (the **Console** button). On enrollment the VM also sets its **hostname** to
`argus-probe-<site>` (e.g. `argus-probe-office`) — the VM is the probe appliance; the container it runs
is the Zabbix proxy (`proxy-<site>`).

**No DHCP?** Turn on **Static IP** in Add probe → VM and fill in the address / prefix / gateway / DNS
before you download the seed ISO — they're baked into it, and the first-boot service applies them
before enrollment, so the VM comes up on its fixed address with no interaction. (This is seed-only: the
first-boot page can't collect it, since you'd need an IP to reach the page.) A VM stuck without network
isn't enrolled yet, so it's recoverable the same way — attach a corrected seed and reboot.

## Scope / not yet

- **Fleet self-update for VM probes.** The golden image runs **two** containers, installed as two
  systemd units: `argus-probe` (the proxy, detached + Docker-restart-managed) and `argus-updater`
  (the shared updater in `probe-watch` mode, holding the socket). Both come up together at enrollment.
  Argus drives updates like any other probe - the updater recreates the proxy (and itself) via the
  Engine API. A `systemctl restart argus-probe` (or a reboot) re-pulls `ARGUS_PROBE_TAG`, so pin it in
  `/etc/argus-probe/probe.env` if you don't want a reboot to converge the VM back on latest.
- **Bare-metal Clonezilla SKU** — the same golden image wrapped in a Clonezilla restore ISO for
  appliance installs with no hypervisor. Reuses the first-boot enrollment path. A later §A slice.
