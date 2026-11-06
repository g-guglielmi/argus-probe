# Argus probe installer

A USB stick that puts an Argus probe on a mini PC: it writes DietPi's x86 image to the mini PC's
internal disk and applies the probe's kit to it, so the first boot is DietPi's automated one with
the probe, its updater sidecar, the OS patching and the host tools on top (argus-core DESIGN 14a).
A Raspberry Pi doesn't need it: its SD card is written from your PC.

## The stick

`argus-probe-installer.img.xz` from the `installer/v*` releases, written to a USB stick of 4 GB or
more with Rufus or balenaEtcher (as it is). It is one 2 GiB FAT32 partition labelled `ARGUS`, so
Windows opens it as a drive:

```
EFI/BOOT/BOOTX64.EFI      the installer (UEFI only; Secure Boot off)
README.txt
argus/kits/               the probes' kits
```

Add:

- **DietPi's image**, `DietPi_NativePC-UEFI-x86_64-Trixie.img.xz` from dietpi.com, at the top level,
  as downloaded. It is not bundled: Argus doesn't redistribute DietPi.
- **A probe's kit**: in Argus, Add probe, DietPi, Mini PC gives `argus-kit-<probe>.zip`; copy its
  `argus` folder onto the stick. A stick can carry several kits.

## On the mini PC

Boot it from the stick (network cable in, Secure Boot off). The installer:

1. finds the stick, DietPi's image and the kits (several: asks which probe this is);
2. lists the disks it may write: internal, not removable, not USB, at least 8 GB (several: asks);
3. shows the probe, the image and the disk with what is on it now, and writes **only after Enter**;
4. streams the image onto the disk, reads it back and compares the SHA-256;
5. sets the kit's lines in the `dietpi.txt` on DietPi's `DIETPISETUP` partition
   (`apply-kit-lines.awk`, the same rule as Argus's browser-side edit for a Pi) and copies
   `Automation_Custom_Script.sh` beside it; DietPi moves both into `/boot` at its first boot;
6. renames the kit `used-<probe>` on the stick (its enrollment token is single use);
7. waits for the stick to be pulled out and restarts into DietPi.

Anything it can't do stops it with the reason; Enter then powers off (`s` opens a shell).

## Build and test

`build.sh [VERSION]` on Linux with Docker (CI: `.github/workflows/installer.yml`): Alpine's LTS
kernel and an initramfs (busybox, kmod, xz, pv, blkid, the storage/USB/input drivers, `init`) made
into one unified kernel image with `ukify`, put on the FAT32 image with mtools.

`test/test_apply_kit.py` checks the kit rule with every awk around (busybox's among them).
`test/qemu_test.py` boots the built stick in QEMU with OVMF against a blank NVMe disk and a stand-in
DietPi image (DietPi's partition layout), presses Enter through the QEMU monitor, pulls the stick,
and checks the disk byte for byte, the patched `dietpi.txt`, the script and the used kit.

Releases: tag `installer/vX.Y.Z` with a `## [installer/vX.Y.Z]` section in `CHANGELOG.md`. Not
under `deploy/probe-image/`, so never a probe (fleet) release.
