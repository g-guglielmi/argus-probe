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
| `files/argus-firstboot.service` + `argus-firstboot.py` | First-boot enrollment: adopts an attached **seed CD** (label `ARGUSSEED`) if present, else serves a one-field setup page. No-ops once enrolled. |
| `files/90-argus-datasources.cfg` | Enables the NoCloud + ConfigDrive cloud-init datasources on the deployed VM. |
| `files/probe.env.example` | Reference for the `probe.env` contract. |

## Design note — built from the Debian cloud image, not a preseed install

DESIGN §14a sketched a from-ISO `debian-installer` preseed. We build on the official **Debian 13
`generic` qcow2** instead: GitHub runners have no nested-KVM acceleration, so a from-ISO install
under TCG would be painfully slow and flaky, whereas the cloud image boots in seconds, already ships
cloud-init, and yields the same appliance. Packer attaches a throwaway NoCloud seed to get an SSH
login, provisions, then **strips machine-id, SSH host keys, and the build user** in the shutdown step
so every deployed clone is unique and carries no shared credential.

We use the **`generic`** variant (full driver set), not `genericcloud` (virtio-only): the **OVA** has
to boot on non-virtio hypervisors (VMware/VirtualBox SCSI/SATA) and the first-boot **seed CD** needs
isofs + a CD-ROM driver, both trimmed out of the cloud kernel. `generic` is a superset, so XCP-NG/KVM
are unaffected; it's only marginally larger.

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

**Give it the enrollment inputs** — any one of three, all zero-touch except the last:

- **cloud-init:** in Argus **Add probe → VM (cloud-init)**, copy the user-data and paste it into the
  hypervisor's **Cloud Config** field (Xen Orchestra, libvirt, VMware guestinfo).
- **seed CD:** on the same tab, **Download seed ISO** and attach it as a CD/DVD when creating the VM.
  This is an Argus-owned ISO (label `ARGUSSEED`), read by the first-boot service — no cloud-init
  datasource needed, so it works on hypervisors that lack a cloud-init field.
- **first-boot page:** boot with no cloud-init and no seed, browse to `http://<vm-ip>/`, and paste the
  enrollment URL + token from the wizard. The page disappears once the probe enrolls.

The probe registers with Argus and appears on the **Probes** page.

## Scope / not yet

- **Fleet self-update for VM probes.** The golden image runs **two** containers, installed as two
  systemd units: `argus-probe` (the proxy, detached + Docker-restart-managed) and `argus-updater`
  (the shared updater in `probe-watch` mode, holding the socket). Both come up together at enrollment.
  Argus drives updates like any other probe - the updater recreates the proxy (and itself) via the
  Engine API. A `systemctl restart argus-probe` (or a reboot) re-pulls `ARGUS_PROBE_TAG`, so pin it in
  `/etc/argus-probe/probe.env` if you don't want a reboot to converge the VM back on latest.
- **Bare-metal Clonezilla SKU** — the same golden image wrapped in a Clonezilla restore ISO for
  appliance installs with no hypervisor. Reuses the first-boot enrollment path. A later §A slice.
