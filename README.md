<p align="center"><img src="argus-logo.png" alt="Argus" width="110"></p>

# argus-probe

The monitoring **probe** for [Argus](https://github.com/g-guglielmi/argus-core) - a self-enrolling
Zabbix active proxy, in two delivery formats that share one enrollment flow:

- **`deploy/probe-image/`** - the **Docker image** (`ghcr.io/g-guglielmi/argus-probe`). On first boot it
  generates a key + CSR, redeems a single-use enrollment token against Argus (`/api/enroll`), and runs
  the stock Zabbix proxy. Includes the opt-in self-update roles (`updater`, `recreate`).
- **`deploy/probe-vm/`** - the **self-configuring golden VM** (Packer). A Debian image that runs the
  `argus-probe` container and self-enrolls on first boot (cloud-init or a first-boot setup page).
  Published as `probe-vm/vX.Y.Z` releases (qcow2 + VHD).

The VM is a delivery wrapper around the same container - that's why they live together here.

## First-boot enrollment

On first boot the golden VM serves a setup page at its IP: paste the enrollment URL and single-use
token from the Argus **Add probe** wizard, pick the console keyboard layout, and the probe signs its
own certificate and registers itself - the private key never leaves the probe. Zero-touch enrollment
via cloud-init (a seed ISO) is also supported, in which case this page is skipped.

<p align="center"><img src="docs/screenshots/first-boot.png" alt="Probe first-boot setup page" width="440"></p>

<p align="center"><sub>Screenshot uses generic placeholder data.</sub></p>

## Relationship to the rest of Argus

- **[argus-core](https://github.com/g-guglielmi/argus-core)** - the app (backend + UI). Mints the
  enrollment tokens, signs CSRs, and drives the probe fleet. The probe's enrollment/check-in protocol
  is a contract shared with the core.
- **[argus-updater](https://github.com/g-guglielmi/argus-updater)** - the socket-holding self-update
  sidecar for the core.

## Check-in & network discovery

The running probe reports to Argus every **60 seconds** (`POST /api/probes/checkin`, Bearer probe
token minted at enrollment): `{"version": "<zabbix>-r<n>", "scans": true, "sweeps": true}` - its
image version plus the **network-scan** and **UniFi-sweep capability** adverts. The response
carries the fleet target, the current core host (fleet re-point), and, when an Argus admin has
queued a discovery job for this probe, ONE one-shot job:

- `scan: {id, cidr, snmp, controllers}` - the entrypoint backgrounds `argus_netscan.py`
  (stdlib-only Python, baked into the image at `/usr/lib/zabbix/externalscripts/`), which sweeps
  the subnet - ICMP, a small TCP port set, SNMP v1/v2c system OIDs, an HTTP(S) banner, a real DNS
  query, reverse DNS, ARP. An 8-minute budget, at most 1024 addresses. Since r16 the optional
  `controllers` list (Argus's saved UniFi controllers) is queried locally after the scan,
  best-effort: matching hosts gain controller device facts (`unifi` + `unifi_ctl`) or a
  client-table naming hint (`unifi_client`) in the posted results.
- `sweep: {id, url, key}` - the entrypoint backgrounds `argus_unifi_sweep.py`, which asks that
  UniFi Network controller for its adopted devices (`X-API-KEY`, all sites, TLS unverified - a
  handful of HTTPS calls).

Both POST their raw facts back to `POST /api/probes/scan-results`, run one at a time per kind
(lock files), and keep the probe a pure reporter with no listening port; an older Argus that never
sends `scan`/`sweep` leaves the branches inert.

## Zabbix process counts

Zabbix reads its process counts (`StartPingers`, `StartPollers`, `StartTrappers`, ...) only when the
proxy starts, and Argus sizes them from the probe's own load (argus-core DESIGN 18c). Each check-in
reports the counts this start runs with (`procs`) and which of them were set on the container
(`procs_pinned`). At start the entrypoint asks Argus for the counts it wants, saves them to
`enroll/procs.env` on the data volume (root-owned; read as data, every value checked to be 1-1000)
and exports `ZBX_START*` from it, so a start without Argus keeps the last ones. Precedence:

1. a `ZBX_START*` variable set on the container (Argus is told and leaves that kind alone);
2. the count Argus handed out;
3. the image default: Zabbix's, except **5** ICMP pingers (one pinger queues every ping behind
   `fping` waiting out slow or silent devices).

To apply a new count Argus asks the `argus-updater` sidecar to restart the proxy (a few seconds; the
proxy's buffer keeps unsent data); without the sidecar it applies at the next start.

Each check-in also reports the CPU (`cpu`): the CPUs the container sees (`nproc`), a container CPU
limit (cgroup v2 `cpu.max`, or the v1 CFS quota; 0 = none) and `/proc/loadavg`. Argus uses it to tell
a probe short on processes from one short on CPU, where more processes would only queue. Inside a
container the load average is the whole host's.

## Images & releases

- Container: `ghcr.io/g-guglielmi/argus-probe` (built by `.github/workflows/probe-image.yml`).
- Container **revisions** are also cut as GitHub Releases `probe/v<zabbix>-r<n>`, tracking the Zabbix base version (decoupled from the app's semver).
- Golden VM: GitHub Releases tagged `probe-vm/vX.Y.Z` (built by `.github/workflows/probe-vm.yml` on a
  `probe-vm/v*` tag or manual dispatch).
- What changed in each release is in [CHANGELOG.md](CHANGELOG.md), which also feeds the Release
  notes. A `probe-vm/v*` tag needs its section first (the build fails without it); a probe-image
  release uses its section when present and otherwise lists the commits since the previous probe
  tag. The changelog header explains how to name the next probe-image section.

See `deploy/probe-vm/README.md` for building and deploying the VM.

## License

Argus is free software licensed under the **GNU Affero General Public License v3.0**
(see [`LICENSE`](LICENSE)). Source: <https://github.com/g-guglielmi/argus-probe>

Copyright (C) 2026 g-guglielmi
