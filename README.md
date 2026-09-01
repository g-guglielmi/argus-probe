<p align="center"><img src="argus-logo.png" alt="Argus" width="110"></p>

# argus-probe

The monitoring **probe** for [Argus](https://github.com/g-guglielmi/argus-core) — a self-enrolling
Zabbix active proxy, in two delivery formats that share one enrollment flow:

- **`deploy/probe-image/`** — the **Docker image** (`ghcr.io/g-guglielmi/argus-probe`). On first boot it
  generates a key + CSR, redeems a single-use enrollment token against Argus (`/api/enroll`), and runs
  the stock Zabbix proxy. Includes the opt-in self-update roles (`updater`, `recreate`).
- **`deploy/probe-vm/`** — the **self-configuring golden VM** (Packer). A Debian image that runs the
  `argus-probe` container and self-enrolls on first boot (cloud-init or a first-boot setup page).
  Published as `probe-vm/vX.Y.Z` releases (qcow2 + VHD).

The VM is a delivery wrapper around the same container — that's why they live together here.

## Relationship to the rest of Argus

- **[argus-core](https://github.com/g-guglielmi/argus-core)** — the app (backend + UI). Mints the
  enrollment tokens, signs CSRs, and drives the probe fleet. The probe's enrollment/check-in protocol
  is a contract shared with the core.
- **[argus-updater](https://github.com/g-guglielmi/argus-updater)** — the socket-holding self-update
  sidecar for the core.

## Images & releases

- Container: `ghcr.io/g-guglielmi/argus-probe` (built by `.github/workflows/probe-image.yml`).
- Container **revisions** are also cut as GitHub Releases `probe/v<zabbix>-r<n>`, tracking the Zabbix base version (decoupled from the app's semver).
- Golden VM: GitHub Releases tagged `probe-vm/vX.Y.Z` (built by `.github/workflows/probe-vm.yml` on a
  `probe-vm/v*` tag or manual dispatch).

See `deploy/probe-vm/README.md` for building and deploying the VM.
