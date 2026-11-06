#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi

# Builds the Argus probe installer stick (argus-core DESIGN 14a): argus-probe-installer.img(.xz), a
# 2 GiB disk image with one FAT32 partition labelled ARGUS (MBR, type 0x0c: Windows opens it as a
# drive) that boots UEFI through EFI/BOOT/BOOTX64.EFI, one unified kernel image: Alpine's LTS kernel
# and an initramfs with busybox, xz, pv, blkid, kmod, the storage/USB/input drivers and our /init.
#
#   bash build.sh [VERSION]        (on Linux with Docker; CI installs the rest: see installer.yml)
#
# Writes output/argus-probe-installer.img and output/argus-probe-installer.img.xz.
set -euo pipefail
cd "$(dirname "$0")"
VERSION="${1:-dev}"
export MTOOLS_SKIP_CHECK=1  # image files have no real disk geometry to check against
ALPINE="${ALPINE_IMAGE:-alpine:3.22}"
WORK="$PWD/work"
OUT="$PWD/output"
rm -rf "$WORK" "$OUT"
mkdir -p "$WORK" "$OUT"

echo "==> the live system's files (Alpine $ALPINE)"
docker run --rm -v "$WORK:/work" "$ALPINE" sh -euc '
  # The kernel and its modules, in a staging root (what the initramfs takes is picked below).
  apk add --quiet --root /work/kernel --initdb --keys-dir /etc/apk/keys --repositories-file /etc/apk/repositories \
    --no-scripts linux-lts linux-firmware-none
  # The initramfs root: a shell and the tools /init uses.
  apk add --quiet --root /work/root --initdb --keys-dir /etc/apk/keys --repositories-file /etc/apk/repositories \
    --no-scripts busybox busybox-binsh kmod xz pv blkid
  # --no-scripts skipped the links of the busybox applets and the kmod tools: made here (busybox
  # leaves the real xz, blkid and kmod alone).
  chroot /work/root /bin/busybox --install -s
  KMOD=$(cd /work/root && ls bin/kmod sbin/kmod usr/bin/kmod 2>/dev/null | head -n1)
  [ -n "$KMOD" ] || { echo "no kmod in the initramfs root" >&2; exit 1; }
  for t in modprobe depmod insmod lsmod rmmod modinfo; do ln -sf "/$KMOD" "/work/root/sbin/$t"; done
  apk add --quiet kmod
  KV=$(ls /work/kernel/lib/modules)
  echo "$KV" > /work/kver
  mkdir -p /work/root/lib/modules/$KV
  # Storage (SATA, NVMe, eMMC, USB, virtio for the CI test), the keyboard and screen, FAT. Not the
  # network, sound, media or the big GPU drivers: the installer needs none of them.
  tar -C /work/kernel/lib/modules/$KV -cf - \
      --exclude=kernel/net --exclude=kernel/sound \
      --exclude=kernel/drivers/net --exclude=kernel/drivers/media --exclude=kernel/drivers/staging \
      --exclude=kernel/drivers/infiniband --exclude=kernel/drivers/isdn --exclude=kernel/drivers/bluetooth \
      --exclude=kernel/drivers/iio --exclude=kernel/drivers/comedi --exclude=kernel/drivers/gpu/drm/amd \
      --exclude=kernel/drivers/gpu/drm/radeon --exclude=kernel/drivers/gpu/drm/nouveau \
      --exclude=kernel/drivers/gpu/drm/i915 --exclude=kernel/drivers/gpu/drm/xe \
      --exclude=kernel/fs/btrfs --exclude=kernel/fs/xfs --exclude=kernel/fs/bcachefs --exclude=kernel/fs/ocfs2 \
      --exclude=kernel/fs/gfs2 --exclude=kernel/fs/nfs --exclude=kernel/fs/nfsd --exclude=kernel/fs/ceph \
      --exclude=kernel/fs/smb --exclude=kernel/fs/f2fs --exclude=kernel/fs/jfs --exclude=kernel/fs/ntfs3 \
      kernel modules.order modules.builtin modules.builtin.modinfo \
    | tar -C /work/root/lib/modules/$KV -xf -
  depmod -b /work/root "$KV"
  cp /work/kernel/boot/vmlinuz-lts /work/vmlinuz
  du -sh /work/root/lib/modules
'
KV="$(cat "$WORK/kver")"
echo "    kernel $KV"
# docker ran as root: hand the tree back before writing into it (cpio reads it too).
sudo chown -R "$(id -u):$(id -g)" "$WORK"

echo "==> /init and its files"
R="$WORK/root"
install -m 0755 init "$R/init"
install -D -m 0644 apply-kit-lines.awk "$R/usr/share/argus/apply-kit-lines.awk"
printf '%s\n' "$VERSION" > "$R/etc/argus-installer.version"
mkdir -p "$R/proc" "$R/sys" "$R/dev" "$R/run" "$R/tmp" "$R/mnt"

echo "==> initramfs"
( cd "$R" && find . -print0 | sort -z | cpio --null -o -H newc --owner 0:0 --quiet ) \
  | xz -T0 -9 --check=crc32 > "$WORK/initramfs.img"
ls -lh "$WORK/initramfs.img"

echo "==> BOOTX64.EFI (unified kernel image)"
# Kernel messages on the serial port and the screen; /dev/console is the screen (the last console=).
ukify build --linux="$WORK/vmlinuz" --initrd="$WORK/initramfs.img" \
  --cmdline="console=ttyS0,115200 console=tty0 quiet loglevel=3 rdinit=/init" \
  --uname="$KV" --os-release="NAME=\"Argus probe installer\"
VERSION=\"$VERSION\"" \
  --output="$WORK/BOOTX64.EFI"
ls -lh "$WORK/BOOTX64.EFI"

echo "==> the stick image"
IMG="$OUT/argus-probe-installer.img"
PART="$WORK/part.img"
truncate -s 2G "$IMG"
printf 'label: dos\nstart=2048, type=c, bootable\n' | sfdisk --quiet "$IMG"
start=$(sfdisk --json "$IMG" | python3 -c 'import json,sys; print(json.load(sys.stdin)["partitiontable"]["partitions"][0]["start"])')
sectors=$(sfdisk --json "$IMG" | python3 -c 'import json,sys; print(json.load(sys.stdin)["partitiontable"]["partitions"][0]["size"])')
truncate -s $((sectors * 512)) "$PART"
mkfs.vfat -F 32 -n ARGUS -h "$start" "$PART" >/dev/null
mmd -i "$PART" ::EFI ::EFI/BOOT ::argus ::argus/kits
mcopy -i "$PART" "$WORK/BOOTX64.EFI" ::EFI/BOOT/BOOTX64.EFI
sed "s/@VERSION@/$VERSION/" README-stick.txt | sed 's/$/\r/' > "$WORK/README.txt"
mcopy -i "$PART" "$WORK/README.txt" ::README.txt
dd if="$PART" of="$IMG" bs=512 seek="$start" conv=notrunc status=none
mdir -i "$IMG@@$((start * 512))" -/ :: | tail -n +4
xz -T0 -6 -k "$IMG"
ls -lh "$OUT"
