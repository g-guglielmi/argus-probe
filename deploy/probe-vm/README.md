# Argus probe golden image (`deploy/probe-vm/`)

A self-configuring **Debian 13 VM** that runs the `argus-probe` container and self-enrolls on first
boot — the VM equivalent of the "Add probe" Docker command, for sites where a full VM is easier to
drop in than a Docker host. Implements DESIGN **§14a** (delivery vs. enrollment, decoupled).

The VM is a thin wrapper: it boots, reads an **enrollment URL + token** (from cloud-init or the
first-boot setup page), and starts the stock `ghcr.io/g-guglielmi/argus-probe` container, which
generates its key + CSR locally and redeems the single-use token against `/api/enroll`. The private
key never leaves the VM; the token is single-use, so keep the disk persistent.

## What's here

| File | Purpose |
|------|---------|
| `argus-probe-vm.pkr.hcl` | Packer template — builds the golden image (qcow2). |
| `build-seed/` | Build-only cloud-init seed so Packer can SSH into the base cloud image. |
| `scripts/provision.sh` | Installs Docker + the probe units into the image. |
| `files/argus-probe.service` | systemd unit that runs the probe container from `/etc/argus-probe/probe.env`. |
| `files/argus-firstboot.service` + `argus-firstboot.py` | First-boot fallback: serves a one-field setup page when cloud-init supplied no token. |
| `files/90-argus-datasources.cfg` | Enables the NoCloud + ConfigDrive cloud-init datasources on the deployed VM. |
| `files/probe.env.example` | Reference for the `probe.env` contract. |

## Design note — built from the Debian cloud image, not a preseed install

DESIGN §14a sketched a from-ISO `debian-installer` preseed. We build on the official **Debian 13
`genericcloud` qcow2** instead: GitHub runners have no nested-KVM acceleration, so a from-ISO install
under TCG would be painfully slow and flaky, whereas the cloud image boots in seconds, already ships
cloud-init, and yields the same appliance. Packer attaches a throwaway NoCloud seed to get an SSH
login, provisions, then **strips machine-id, SSH host keys, and the build user** in the shutdown step
so every deployed clone is unique and carries no shared credential.

## Building

CI (`.github/workflows/probe-vm.yml`) builds it — `packer validate` on every change under this
directory, and a full build on **workflow_dispatch** or a **`probe-vm/v*`** tag (which also publishes
a GitHub Release with the image assets). Outputs: `argus-probe-vm.qcow2` and `argus-probe-vm.vhd`.

Locally (needs Packer + QEMU):

```bash
cd deploy/probe-vm
packer init argus-probe-vm.pkr.hcl
packer build argus-probe-vm.pkr.hcl        # -> output/argus-probe-vm.qcow2
```

## Deploying on XCP-NG

1. Import the disk: upload `argus-probe-vm.vhd` as a VDI (Xen Orchestra → Import, or
   `xe vdi-import`). Create a VM (1–2 vCPU, 2 GB RAM) and attach the VDI as its boot disk.
2. Give it the enrollment inputs — either of:
   - **cloud-init (zero-touch):** attach the seed ISO from Argus **Add probe → VM** as a CD/config
     drive. The VM enrolls on first boot with no interaction. *(Add-probe VM output ships in the next
     slice; until then, use the first-boot page.)*
   - **first-boot page:** boot without a seed, browse to `http://<vm-ip>/`, and paste the enrollment
     URL + token from the Add-probe wizard. The page disappears once the probe enrolls.
3. The probe registers with Argus and appears on the **Probes** page.

`.qcow2` imports directly on KVM/libvirt; the `.vhd` also imports on Hyper-V. OVA for VMware/Nutanix
is a later slice.

## Scope / not yet

- **Add-probe "VM" output** (cloud-init user-data + downloadable NoCloud seed ISO) — next slice; for
  now the first-boot page covers enrollment.
- **Fleet self-update for VM probes.** VM probes report their version and enroll like any probe, but
  the golden image doesn't yet self-update on the Argus fleet target (the container is systemd-managed
  here, not Docker-restart-managed, so the sister-container recreate path is bypassed). A
  systemd-timer updater that honors the fleet target is a follow-up. Updating today = `systemctl
  restart argus-probe` (re-pulls the tag) or redeploy.
- **OVA (VMware/Nutanix)** and the **bare-metal Clonezilla SKU** — later §A slices.
