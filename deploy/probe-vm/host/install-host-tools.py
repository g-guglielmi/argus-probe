#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi
"""install-host-tools: write the probe VM's host scripts and units from the manifest beside this file.

The probe image carries this folder as /host-tools (labelled io.argus.host-tools); after each probe
update the argus-updater runs the image once, as root with the host's /usr/local/sbin and
/etc/systemd/system bound under --root and the probe's data folder at --update-dir, and this writes
the files that changed. The same script runs at image build on the VM itself (--root /). A container
cannot reload systemd: when a systemd file changed this drops host-tools-apply.json in the update
dir and argus-host-tools.path on the host runs argus-host-tools-apply (reload, enable, re-report).

Manifest lines: SOURCE DEST MODE [render] [enable]. render substitutes @STATE_DIR@ (the host path of
the probe's data folder) in the file; enable lists the unit for the host to `systemctl enable --now`.
Prints one JSON line: {"version", "installed", "unchanged", "units_changed", "apply"}.
"""
import argparse
import json
import os
import sys
import time

APPLY_FILE = "host-tools-apply.json"


class ManifestError(Exception):
    pass


def parse_manifest(text):
    entries = []
    for n, raw in enumerate(text.split("\n"), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        f = line.split()
        if len(f) < 3:
            raise ManifestError("manifest line %d: want source, destination and mode" % n)
        try:
            mode = int(f[2], 8)
        except ValueError:
            mode = -1
        if mode < 0 or not f[1].startswith("/") or "/" in f[0] or ".." in f[1]:
            raise ManifestError("manifest line %d: bad entry %r" % (n, line))
        e = {"source": f[0], "dest": f[1], "mode": mode, "render": False, "enable": False}
        for flag in f[3:]:
            if flag in ("render", "enable"):
                e[flag] = True
            else:
                raise ManifestError("manifest line %d: unknown flag %r" % (n, flag))
        entries.append(e)
    if not entries:
        raise ManifestError("the manifest lists nothing")
    return entries


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def install(src, root, state_dir, update_dir, version, now=None):
    """Write every manifest entry under root, a file only when its content or mode differs (to a
    temporary name, then renamed over, so nothing on the host ever reads half a script), and drop the
    reload marker in update_dir when a systemd file changed. Returns the summary."""
    try:
        entries = parse_manifest(read_bytes(os.path.join(src, "manifest")).decode("utf-8"))
    except OSError:
        raise ManifestError("this image carries no host tools")
    summary = {"version": version, "installed": [], "unchanged": [], "units_changed": [], "apply": False}
    enable = []
    for e in entries:
        try:
            data = read_bytes(os.path.join(src, e["source"]))
        except OSError:
            raise ManifestError("the image has no %s" % e["source"])
        if e["render"]:
            data = data.replace(b"@STATE_DIR@", state_dir.encode("utf-8"))
        if e["enable"]:
            enable.append(os.path.basename(e["dest"]))
        target = os.path.join(root, e["dest"].lstrip("/"))
        try:
            same = read_bytes(target) == data and (os.name == "nt" or (os.stat(target).st_mode & 0o777) == e["mode"])
        except OSError:
            same = False
        if same:
            summary["unchanged"].append(e["dest"])
            continue
        folder = os.path.dirname(target)
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as err:
            raise ManifestError("could not create the folder for %s: %s" % (e["dest"], err))
        tmp = os.path.join(folder, "." + os.path.basename(target) + ".argus-new")
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.chmod(tmp, e["mode"])
            if os.name == "nt" and os.path.exists(target):
                os.chmod(target, 0o644)
            os.replace(tmp, target)
        except OSError as err:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise ManifestError("could not write %s: %s" % (e["dest"], err))
        summary["installed"].append(e["dest"])
        if e["dest"].startswith("/etc/systemd/"):
            summary["units_changed"].append(os.path.basename(e["dest"]))
    for k in ("installed", "unchanged", "units_changed"):
        summary[k].sort()
    if summary["units_changed"]:
        marker = {"at": int(now if now is not None else time.time()), "version": version,
                  "enable": " ".join(enable), "changed": " ".join(summary["units_changed"])}
        final = os.path.join(update_dir, APPLY_FILE)
        tmp = final + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(marker))
            os.chmod(tmp, 0o644)
            os.replace(tmp, final)
        except OSError as err:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise ManifestError("could not drop the reload marker in %s: %s" % (update_dir, err))
        summary["apply"] = True
    return summary


def image_version():
    try:
        with open("/etc/argus-probe.version", encoding="utf-8") as fh:
            return fh.read().strip() or "dev"
    except OSError:
        return "dev"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Install the probe VM's host scripts and units from the manifest.")
    ap.add_argument("--src", default=os.path.dirname(os.path.abspath(__file__)), help="the folder with the manifest (default: this script's)")
    ap.add_argument("--root", default="/host", help="where the host's filesystem is bound (default /host; / on the host itself)")
    ap.add_argument("--state-dir", default="/docker/argus-probe", help="host path of the probe's data folder, for the units")
    ap.add_argument("--update-dir", default="/update", help="where to drop the reload marker (the data folder, as bound here)")
    ap.add_argument("--version", default=None, help="the probe version these tools belong to (default: the image's)")
    args = ap.parse_args(argv)
    try:
        summary = install(args.src, args.root, args.state_dir, args.update_dir, args.version or image_version())
    except ManifestError as e:
        print("install-host-tools: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
