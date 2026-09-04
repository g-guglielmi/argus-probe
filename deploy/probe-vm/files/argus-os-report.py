#!/usr/bin/env python3
"""Report this probe VM's OS patch status to Argus (DESIGN §14c).

The VM patches its own Debian OS locally via unattended-upgrades (security suite only) and reboots
itself in a weekly ~03:00 window. This reporter only *reports* — it posts the pending security-update
count and the reboot-required flag to Argus so the fleet view shows which sites carry CVEs / need a
reboot. Run hourly by argus-os-report.timer.

Stdlib only. Authenticated by the same long-lived probe token the enrollment wrote to proxy.env — so
until the VM has enrolled there's nothing to report and this exits quietly.
"""
import json
import os
import re
import subprocess
import urllib.request

META = "/var/lib/argus-probe/enroll/proxy.env"  # written by the probe container at enrollment


def read_kv(path):
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return out


def sec_updates():
    """Count pending SECURITY updates (best-effort). A simulated upgrade lists the packages apt would
    install; the ones whose source archive is a *-security suite are the security updates. -1 = unknown."""
    try:
        out = subprocess.run(
            ["apt-get", "-s", "-o", "Debug::NoLocking=true", "upgrade"],
            capture_output=True, text=True, timeout=90,
        ).stdout
    except Exception:
        return -1
    n = 0
    for line in out.splitlines():
        if line.startswith("Inst") and re.search(r"[Ss]ecurity", line):
            n += 1
    return n


def main():
    env = read_kv(META)
    token, checkin = env.get("PROBE_TOKEN", ""), env.get("CHECKIN_URL", "")
    if not token or not checkin:
        return 0  # not enrolled yet — nothing to report
    url = checkin.rsplit("/", 1)[0] + "/os-status"  # .../api/probes/checkin -> .../api/probes/os-status
    body = json.dumps({
        "sec_updates": sec_updates(),
        "reboot_required": os.path.exists("/var/run/reboot-required"),
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Authorization": "Bearer " + token,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20):
            pass
    except Exception:
        pass  # best-effort; the next hourly run retries
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
