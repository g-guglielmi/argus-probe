# Changelog

All notable changes to argus-probe are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/). The repo ships two independently versioned things,
listed together newest first, each section headed by its exact release tag:

- **Probe image** `probe/v<zabbix>-r<n>` - the self-enrolling Zabbix proxy image. The version is the
  Zabbix version it ships plus a revision for our own changes (`-r1` again after each Zabbix bump).
  CI cuts these by itself: every push to `deploy/probe-image/` on `main` is a release, and so is a
  new upstream Zabbix version. To write the notes by hand, add a section for the **next** tag in the
  same commit: the current revision is in `deploy/probe-image/.zabbix-base`, so the next one is
  `revision + 1`. With no matching section, the Release lists the commit subjects since the previous
  probe tag instead.
- **Probe VM** `probe-vm/vX.Y.Z` - the self-configuring golden image. Released by pushing the tag;
  the build fails early if its section below is missing.

---

## [Unreleased]

## [probe-vm/v0.3.5] - 2026-09-30

Refresh of the probe appliance golden image: the new folder layout. It is what a new deployment gets.
The probe image is pre-pulled at build time from `:latest` (`probe/v7.0.31-r14` today: failed systemd
units, CPU iowait and steal, TCP port checks).

### Changed
- The probe container's files are in `/docker/argus-probe` (`/docker/<container name>`, like the core
  VM and the Add probe wizard's `docker run`): the proxy's database and buffer, its certificates, the
  enrollment state, and `snmptraps/`. The folders are listed in the probe VM README.

## [probe/v7.0.31-r14] - 2026-09-30

### Added
- The Linux-over-SSH collector reports the systemd units that are failed (any unit, from
  `systemctl list-units --state=failed`, no privileges needed) with their names, and the CPU time
  spent waiting on storage (iowait) and taken by the hypervisor for other guests (steal).
- A TCP port collector, `argus_tcp.py`: connects to every listed port of a host at once and reports,
  per port, whether it answered, the connect time and, when it didn't, why (refused, no answer, no
  route to host). Nothing is sent over the connection.

### Fixed
- The Linux-over-SSH collector no longer counts guest CPU time twice in the utilization of a host that
  runs VMs itself.

## [probe-vm/v0.3.4] - 2026-09-30

Refresh of the probe appliance golden image. Existing VMs are not changed by this; it is what a new
deployment gets. The probe image is pre-pulled at build time from `:latest` (`probe/v7.0.31-r12`
today: collector failure reasons, the UniFi controller MAC for scanned devices, the container
healthcheck).

### Security
- Docker is installed from Docker's apt repository with the signing key's fingerprint pinned, instead
  of the `get.docker.com` script.

### Changed
- The Release is published beside the probe image releases and no longer takes the repository's
  Latest mark from them.

## [probe/v7.0.31-r13] - 2026-09-30

### Added
- The Linux-over-SSH collector can report systemd units and Docker containers in the same session:
  the units listed in the host's options (`systemctl show`, no privileges needed) and the containers
  whose name matches its filter (`docker ps -a`, which needs docker rights). Each is reported running
  or not with its state (`failed (failed), result exit-code`, `Exited (1) 2 hours ago`); a unit name
  is checked before it reaches the remote command line, and the container filter is applied by the
  collector, never on the target. A host without either option is polled exactly as before.

## [probe/v7.0.31-r12] - 2026-09-29

### Fixed
- A network scan that matches a device to a saved UniFi controller now passes on the controller's
  own MAC for it. A gateway answers on its LAN with a derived address, so a gateway matched by IP
  used to be added with a MAC the controller doesn't know, and none of its sensors could read.

## [probe/v7.0.31-r11] - 2026-09-29

### Added
- The collectors say why a poll failed. The SSH, XCP-NG and NUT collectors print an `error` field
  next to `reachable` (the line ssh printed, such as `Permission denied (publickey)` or `Host key
  verification failed`; the XAPI failure; upsd's `ERR` answer and what it means, such as no UPS by
  that name), and the DNS check words its failures (`the server answered NXDOMAIN`, no answer within
  3 s). Argus shows the reason next to the down reading and puts it in the alert; the core's templates
  keep it in a Collection error item. Reasons carry no secret: only what the other side said and
  which setting to check.

## [probe/v7.0.31-r10] - 2026-09-29

### Added
- A Docker `HEALTHCHECK` (`/app/healthcheck.py`, standard library only): healthy while the Zabbix
  proxy process runs and accepts connections on its listen port (`ZBX_LISTENIP` / `ZBX_LISTENPORT`,
  default the loopback and 10051). Every 30 s, a 120 s start period for a first boot's enrollment,
  unhealthy after 3 failures. Whether the proxy reaches the core stays out of it: Argus alerts on
  that, and a restart can't fix the network.

## [probe/v7.0.31-r9] - 2026-09-29

### Added
- The check-in reports the CPU (`cpu`): the CPUs the container sees, a container CPU limit (cgroup
  v2 `cpu.max` or the v1 CFS quota; 0 = none) and the load average. Argus uses it to add no
  processes to a probe short on CPU, and to tell the admin so. Every value is checked for shape
  before it is sent.

### Changed
- The release notes no longer mention Watchtower: probes update from the Argus Probes page (with the
  argus-updater sidecar), the Unraid GUI or Dockhand.

## [probe/v7.0.31-r8] - 2026-09-29

### Added
- Zabbix process counts sized by Argus: the check-in reports the counts the proxy started with
  (`procs`) and which ones are set on the container (`procs_pinned`); at start the probe saves the
  counts Argus hands out to `procs.env` on its data volume (read as data, each value checked) and
  starts Zabbix with them. A `ZBX_START*` variable on the container still wins, and the image
  defaults stay (5 ICMP pingers, Zabbix's own for the rest). An Argus without the feature changes
  nothing.

## [probe/v7.0.31-r7] - 2026-09-29

### Security
- The XCP-NG and Linux SSH collectors (and the sweep script's manual mode) wipe their own command
  line as soon as they have read their arguments, so a password Zabbix hands them as an argument
  shows in `ps`, `top`, `docker top` or a support bundle only during interpreter start-up, not for
  the whole check. Zabbix can pass a value to an external check no other way; the proxy's own
  configuration database holds the same macros, so this closes the accidental capture, not access
  by whoever already runs inside the container.

### CI
- Actions pinned to commits, permissions granted per job, provenance and SBOM attestations on the
  probe image; `:latest` and a Release are only ever produced from `main`.

## [probe/v7.0.31-r6] - 2026-09-29

### Added
- The sweep script answers a **certificate check** job (`cert_only`) from Argus: it connects to the
  controller URL, reports the certificate it presents (SHA-256, subject, issuer, expiry) and
  nothing else, so a controller only the probe can reach can be pinned from the Argus dialog. A
  sweep refused by the certificate check reports the certificate it saw the same way.

## [probe/v7.0.31-r5] - 2026-09-29

### Security
- **Controller certificates are checked** in scans and sweeps as the controller's saved policy
  says (Argus `v0.5.14`+ hands it out with the job): verify against the CA store, pin a SHA-256
  fingerprint (compared on the very connection each request uses), or ignore. An older Argus sends
  no policy and the scripts verify against the CA store, so a self-signed console then needs its
  certificate pinned or ignored in Argus.
- **XCP-NG collector:** a sixth argument (`{$XCP.TLS}`: pin / verify / ignore, default pin) decides
  how the XAPI certificate is checked. *pin* trusts it on first contact and remembers its SHA-256
  under `/var/lib/zabbix/argus-pins/`; a change is refused and reported as `tls_error` with
  `reachable=0`.
- **Linux-by-SSH collector:** refuses a login name that would read as an ssh option, a port outside
  1-65535 and a key outside `/var/lib/zabbix/ssh/`; the target follows `--`. The comment about
  passwords now says what is true: ssh's argv carries none, the script's own does (a Zabbix macro).

## [probe-vm/v0.3.3] - 2026-09-29

### Security
- **A setup code guards the first-boot page.** The page is reachable by anyone on the VM's network
  until the probe enrols, and what it collects decides which server the VM trusts. It now prints an
  8-character code on the hypervisor console (and the console login banner) and refuses a submission
  without it; ten wrong codes replace it. The stored token is never shown again on the "change and
  retry" form, and opening that form no longer stops the running services.
- **Every value is checked** before it reaches the probe's env file: the enrollment URL must be
  `https://<argus>/api/enroll` (a "no HTTPS, lab only" switch allows http and passes
  `ARGUS_ALLOW_INSECURE_CHECKIN=true` to the container), the token and core host must look like a
  token and a host. A seed disk goes through the same checks.
- **SSH password login is limited to the break-glass account**: off for every other account and for
  root (keys work for all), so the password Argus reveals still works over the site VPN when the
  console isn't at hand, and nothing else on the VM can be guessed at over the network.
- No console to read the setup code from? The seed ISO is the zero-touch path and needs no code.
- The break-glass user is no longer in the `docker` group (sudo already covers it, and is logged).
- The page answers with `Cache-Control: no-store` and `X-Frame-Options: DENY`.

### Changed
- The first-boot setup page links to its source code (AGPL-3.0 section 13); small wording fixes.

## [probe/v7.0.31-r4] - 2026-09-28

### Security
- `proxy.env` (the probe's saved enrollment: proxy name, core host, check-in token and URL) is
  read as data, never sourced as shell. Its values come from Argus over the network, so each is
  checked against the shape it must have (a host, a token, a URL) before it is used or written
  back; a value that doesn't fit is dropped with a note and the last good one kept. Before, a
  crafted core host in a check-in response was executed by the entrypoint as root.
- The enrollment directory and `proxy.env` stay owned by root; only the certificates and the key
  are handed to the zabbix user, which never needs the rest. So the proxy process can't rewrite
  what this entrypoint (and the updater sidecar, which reads the same file) run with.
- The check-in URL must be https. A plain-http URL is refused, with a message, unless
  `ARGUS_ALLOW_INSECURE_CHECKIN=true` is set for a lab; the proxy itself keeps running either way,
  only the check-in (fleet updates, scans) is off.
- The scan and sweep job files (an SNMP community, a controller key) are created readable by their
  owner only.

## [probe/v7.0.31-r3] - 2026-09-28

### Changed
- The proxy starts **5 ICMP pingers** instead of Zabbix's default of 1. A single pinger queues every
  ping check behind it while `fping` waits for slow or silent devices, and a mid-size site already
  kept it about 60% busy. Idle pingers cost a few MB. Set `ZBX_STARTPINGERS` to override.

## [probe/v7.0.31-r2] - 2026-09-22

### Fixed
- The network scanner fills in a host's MAC address from the UniFi controller's record when the
  scan runs across a routed network, so UniFi device classes get it automatically.

## [probe/v7.0.31-r1] - 2026-09-22

### Changed
- Zabbix proxy base 7.0.30 -> 7.0.31.
- The scanner queries the saved UniFi controllers locally from the probe.

## [probe/v7.0.30-r15] - 2026-09-22

### Added
- UniFi controller sweep: the probe runs discovery jobs against a site's controller.

### Fixed
- The sweep uses the gateway's LAN address, not its WAN address.

## [probe/v7.0.30-r14] - 2026-09-21

### Changed
- Richer scan fingerprints: page titles after redirects, the Location header and the SSH banner.

## [probe/v7.0.30-r13] - 2026-09-21

### Added
- Network-discovery scanner; the probe picks up scan jobs from Argus at check-in.

## [probe/v7.0.30-r12] - 2026-09-18

### Added
- Agentless Linux collector (SSH, one login per poll).

## [probe/v7.0.30-r11] - 2026-09-18

### Added
- XCP-NG pool collector.

## [probe/v7.0.30-r10] - 2026-09-16

### Fixed
- The probe pre-creates its TLS directories on start, so a rebuilt Zabbix base image can no longer
  crash-loop it.

## [probe/v7.0.30-r9] - 2026-09-15

### Added
- The probe re-fetches the core host from Argus at startup, so the whole fleet can be re-pointed
  centrally.

## [probe/v7.0.30-r8] - 2026-09-14

### Changed
- Relicensed to AGPL-3.0; SPDX headers on all source files.

## [probe/v7.0.30-r7] - 2026-09-13

### Added
- UPS (NUT) and DNS-resolution external-check collectors baked into the image.

## [probe-vm/v0.3.2] - 2026-09-05

### Changed
- The first-boot page follows the Argus look: logo, light/dark theme, VM identity, and a no-JS
  fallback.

## [probe-vm/v0.3.1] - 2026-09-05

### Added
- The VM patches its OS automatically (security updates) and reports patch status and its OS name
  to Argus.

## [probe-vm/v0.3.0] - 2026-09-02

### Added
- OVA delivery and zero-touch enrollment from a seed CD.
- Static networking from the seed for sites without DHCP.
- A break-glass console user, a keyboard-layout choice and host-key regeneration on first boot.

### Changed
- The VM names itself `argus-probe-<site>` on enrollment.
- cloud-init is removed after first boot; larger default disk.

### Fixed
- The OVA reported the wrong disk capacity.

## [probe-vm/v0.2.0] - 2026-09-02

### Changed
- The VM runs the proxy plus an `argus-updater` sidecar; the proxy itself holds no Docker socket.

## [probe/v7.0.30-r6] - 2026-09-01

### Changed
- The probe is a pure reporter; self-updates are done by the `argus-updater` sidecar.

## [probe/v7.0.30-r5] - 2026-09-01

### Changed
- The probe doesn't advertise self-update when it has no Docker socket.

## [probe/v7.0.30-r4] - 2026-09-01

### Changed
- Uses the shared `argus-updater` image instead of bundled update scripts.

## [probe/v7.0.30-r3] - 2026-09-01

### Changed
- Rebuild with no functional change.

## [probe/v7.0.30-r2] - 2026-09-01

### Changed
- The image is labelled with its new source repository after the repo split.

## [probe-vm/v0.1.3] - 2026-09-01

### Added
- First release of the self-configuring probe golden image (qcow2, VHD), enrolled from the
  Add-probe wizard's cloud-init output.
- A styled first-boot page with live enrollment feedback; a failed enrollment can be corrected and
  retried.

### Fixed
- DHCP works without cloud-init.

## [probe/v7.0.30-r1] - 2026-08-26

### Changed
- Zabbix proxy base 7.0.29 -> 7.0.30.

## [probe/v7.0.29-r7] - 2026-08-19

### Added
- Probe self-updates verify health and roll back on failure.

### Fixed
- The SNMP traps volume is bound, so no anonymous volume is created.

## [probe/v7.0.29-r6] - 2026-08-19

### Changed
- The check-in token is persisted, so its environment variable can be removed after first boot.

## [probe/v7.0.29-r5] - 2026-08-19

### Added
- Version reporting for probes deployed before it existed (check-in token via environment).

## [probe/v7.0.29-r4] - 2026-08-19

### Added
- Self-update recreate helper and reporter trigger.

## [probe/v7.0.29-r3] - 2026-08-19

### Changed
- Text style fixes; no functional change.

## [probe/v7.0.29-r2] - 2026-08-18

### Added
- Self-updater sidecar and a baked-in probe version.

## [probe/v7.0.29] - 2026-08-17

### Added
- First release: the self-enrolling probe image (Zabbix proxy 7.0.29), with an optional core host
  override and clearer enrollment errors.
