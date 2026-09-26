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

### Probe VM
- The first-boot setup page links to its source code (AGPL-3.0 section 13); small wording fixes.

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
