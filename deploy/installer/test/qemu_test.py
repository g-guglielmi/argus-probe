#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi
"""Boots the Argus probe installer stick in QEMU (UEFI, OVMF) against a blank NVMe disk and checks
what it wrote: a stand-in DietPi image byte for byte (its setup partition aside), the kit's lines in
dietpi.txt, the kit's script beside it, the kit marked used on the stick.

    python3 qemu_test.py output/argus-probe-installer.img

Needs (CI installs them): qemu-system-x86, ovmf, mtools, dosfstools, fdisk (sfdisk), xz-utils.
The stand-in image is DietPi's layout: p1 EFI, p2 root, p3 a 1 MiB FAT labelled DIETPISETUP with a
dietpi.txt. Enter is pressed through the QEMU monitor (the emulated keyboard), as a person would.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

MiB = 1024 * 1024
os.environ["MTOOLS_SKIP_CHECK"] = "1"  # image files have no real disk geometry to check against
DIETPI_TXT = (
    "# DietPi-Automation settings\n"
    "AUTO_SETUP_AUTOMATED=0\n"
    "AUTO_SETUP_GLOBAL_PASSWORD=dietpi\n"
    "AUTO_SETUP_NET_HOSTNAME=DietPi\n"
    "AUTO_SETUP_TIMEZONE=Europe/London\n"
    "#AUTO_SETUP_INSTALL_SOFTWARE_ID=17\n"
    "AUTO_SETUP_CUSTOM_SCRIPT_EXEC=0\n"
    "CONFIG_CHECK_APT_UPDATES=1\n"
)
KIT_LINES = (
    "AUTO_SETUP_AUTOMATED=1\n"
    "AUTO_SETUP_GLOBAL_PASSWORD=TestPassword23\n"
    "AUTO_SETUP_NET_HOSTNAME=proxy-test\n"
    "AUTO_SETUP_TIMEZONE=Europe/Rome\n"
    "AUTO_SETUP_INSTALL_SOFTWARE_ID=docker\n"
    "SURVEY_OPTED_IN=0\n"
    "CONFIG_CHECK_APT_UPDATES=1\n"
)
WANT_TXT = (
    "# DietPi-Automation settings\n"
    "AUTO_SETUP_AUTOMATED=1\n"
    "AUTO_SETUP_GLOBAL_PASSWORD=TestPassword23\n"
    "AUTO_SETUP_NET_HOSTNAME=proxy-test\n"
    "AUTO_SETUP_TIMEZONE=Europe/Rome\n"
    "#AUTO_SETUP_INSTALL_SOFTWARE_ID=17\n"
    "AUTO_SETUP_INSTALL_SOFTWARE_ID=docker\n"
    "AUTO_SETUP_CUSTOM_SCRIPT_EXEC=0\n"
    "CONFIG_CHECK_APT_UPDATES=1\n"
    "SURVEY_OPTED_IN=0\n"
)
KIT_SCRIPT = "#!/bin/bash\necho argus test kit\n"


def run(*cmd, **kw):
    kw.setdefault("check", True)
    return subprocess.run(list(cmd), **kw)


def parts(img):
    out = run("sfdisk", "--json", img, capture_output=True, text=True).stdout
    return json.loads(out)["partitiontable"]["partitions"]


def fake_dietpi(tmp):
    """The stand-in DietPi image (raw), its xz, and the setup partition's (start, size) in bytes."""
    raw = os.path.join(tmp, "fake.img")
    with open(raw, "wb") as fh:
        fh.truncate(128 * MiB)
    run("sfdisk", "--quiet", raw, input=(
        "label: gpt\n"
        "start=2048, size=65536, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B\n"
        "size=131072, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4\n"
        "size=2048, type=EBD0A0A2-B9E5-4433-87C0-68B6B72699C7\n"), text=True)
    p = parts(raw)
    with open(raw, "r+b") as fh:  # a root partition full of noise, so the hash check means something
        fh.seek(p[1]["start"] * 512)
        fh.write(os.urandom(p[1]["size"] * 512))
    setup = os.path.join(tmp, "setup.img")
    with open(setup, "wb") as fh:
        fh.truncate(p[2]["size"] * 512)
    run("mkfs.vfat", "-n", "DIETPISETUP", setup, capture_output=True)
    txt = os.path.join(tmp, "dietpi.txt")
    with open(txt, "w", newline="\n") as fh:
        fh.write(DIETPI_TXT)
    run("mcopy", "-i", setup, txt, "::dietpi.txt")
    run("dd", "if=" + setup, "of=" + raw, "bs=512", "seek=%d" % p[2]["start"], "conv=notrunc", "status=none")
    xz = os.path.join(tmp, "DietPi_NativePC-UEFI-x86_64-Trixie.img.xz")
    with open(xz, "wb") as out:
        run("xz", "-T0", "-0", "-c", raw, stdout=out)
    return raw, xz, (p[2]["start"] * 512, p[2]["size"] * 512)


def stick(src, tmp, image_xz):
    """A copy of the built stick with DietPi's stand-in and a kit on it."""
    img = os.path.join(tmp, "stick.img")
    shutil.copyfile(src, img)
    off = parts(img)[0]["start"] * 512
    at = "%s@@%d" % (img, off)
    run("mcopy", "-i", at, image_xz, "::" + os.path.basename(image_xz))
    run("mmd", "-i", at, "::argus/kits/proxy-test")
    for name, text in (("dietpi-lines.txt", KIT_LINES), ("Automation_Custom_Script.sh", KIT_SCRIPT)):
        path = os.path.join(tmp, name)
        with open(path, "w", newline="\n") as fh:
            fh.write(text)
        run("mcopy", "-i", at, path, "::argus/kits/proxy-test/" + name)
    return img, at


def ovmf(tmp):
    for code, vars_ in (("/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd"),
                        ("/usr/share/OVMF/OVMF_CODE.fd", "/usr/share/OVMF/OVMF_VARS.fd")):
        if os.path.exists(code):
            v = os.path.join(tmp, "vars.fd")
            shutil.copyfile(vars_, v)
            return code, v
    raise SystemExit("OVMF not found (apt-get install ovmf)")


class Monitor:
    def __init__(self, path):
        for _ in range(100):
            try:
                self.s = socket.socket(socket.AF_UNIX)
                self.s.connect(path)
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise SystemExit("no QEMU monitor at " + path)
        self.s.settimeout(2)
        self.read()

    def read(self):
        try:
            return self.s.recv(65536).decode(errors="replace")
        except socket.timeout:
            return ""

    def cmd(self, line):
        self.s.sendall((line + "\n").encode())
        time.sleep(0.3)
        return self.read()


def wait_for(log, text, proc, limit):
    end = time.time() + limit
    while time.time() < end:
        try:
            with open(log, errors="replace") as fh:
                if text in fh.read():
                    return
        except FileNotFoundError:
            pass
        if proc.poll() is not None:
            break
        time.sleep(1)
    with open(log, errors="replace") as fh:
        sys.stdout.write(fh.read()[-6000:])
    raise SystemExit("timed out waiting for %r" % text)


def main():
    built = os.path.abspath(sys.argv[1])
    tmp = tempfile.mkdtemp(prefix="argus-installer-test-")
    raw, xz, (setup_off, setup_size) = fake_dietpi(tmp)
    img, stick_at = stick(built, tmp, xz)
    target = os.path.join(tmp, "target.img")
    with open(target, "wb") as fh:
        fh.truncate(10 * 1000 * 1000 * 1000)  # sparse; over the installer's 8 GB minimum
    code, vars_ = ovmf(tmp)
    log, mon = os.path.join(tmp, "serial.log"), os.path.join(tmp, "mon.sock")
    accel = "kvm" if os.access("/dev/kvm", os.W_OK) else "tcg"
    qemu = ["qemu-system-x86_64", "-machine", "q35", "-accel", accel, "-m", "1536", "-smp", "2",
            "-drive", "if=pflash,format=raw,readonly=on,file=" + code,
            "-drive", "if=pflash,format=raw,file=" + vars_,
            "-device", "qemu-xhci,id=xhci",
            "-drive", "if=none,id=stick,format=raw,file=" + img,
            "-device", "usb-storage,bus=xhci.0,drive=stick,id=stickdev,removable=on,bootindex=0",
            "-drive", "if=none,id=disk,format=raw,file=" + target,
            "-device", "nvme,serial=argustest,drive=disk",
            "-nic", "none", "-display", "none", "-no-reboot",
            "-serial", "file:" + log, "-monitor", "unix:%s,server=on,wait=off" % mon]
    print("booting the stick (%s)" % accel, flush=True)
    proc = subprocess.Popen(qemu)
    try:
        m = Monitor(mon)
        wait_for(log, "ASK Press Enter to install", proc, 900 if accel == "tcg" else 300)
        print("the installer asks: pressing Enter", flush=True)
        m.cmd("sendkey ret")
        wait_for(log, "Pull the stick out", proc, 1200 if accel == "tcg" else 300)
        print("done writing: pulling the stick", flush=True)
        m.cmd("device_del stickdev")
        proc.wait(timeout=120)
    finally:
        if proc.poll() is None:
            proc.kill()
    with open(log, errors="replace") as fh:
        print(fh.read()[-3000:])

    # The disk: the image byte for byte outside the setup partition.
    size = os.path.getsize(raw)
    with open(raw, "rb") as a, open(target, "rb") as b:
        want, got = a.read(size), b.read(size)
    for lo, hi in ((0, setup_off), (setup_off + setup_size, size)):
        assert want[lo:hi] == got[lo:hi], "the disk differs from the image in %d..%d" % (lo, hi)
    # The setup partition: the kit's lines in dietpi.txt, its script beside it.
    at = "%s@@%d" % (target, setup_off)
    txt = run("mtype", "-i", at, "::dietpi.txt", capture_output=True, text=True).stdout
    assert txt == WANT_TXT, "dietpi.txt:\n" + txt
    script = run("mtype", "-i", at, "::Automation_Custom_Script.sh", capture_output=True, text=True).stdout
    assert script == KIT_SCRIPT, "the script:\n" + script
    # The stick: the kit is marked used.
    listing = run("mdir", "-i", stick_at, "-b", "::argus/kits", capture_output=True, text=True).stdout
    assert "used-proxy-test" in listing and "/proxy-test" not in listing.replace("used-proxy-test", ""), listing
    print("OK: DietPi written and checked, the kit applied, the kit marked used")
    shutil.rmtree(tmp, True)


if __name__ == "__main__":
    main()
