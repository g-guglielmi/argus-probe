#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi

# argus_unifi_sweep.py - Argus UniFi controller sweep.
#
# MIRRORED, keep byte-identical:
#   argus-probe: deploy/probe-image/externalscripts/argus_unifi_sweep.py  (baked into the proxy image)
#   argus-core:  deploy/core/externalscripts/argus_unifi_sweep.py         (installed on the core by setup-core.sh)
#
# Asks a UniFi Network controller for its adopted devices (X-API-KEY header, the same API the
# UniFi class templates poll) and reports them as discovery candidates: exact model, type, MAC,
# IP, firmware and site per device. Tries the UniFi OS path (/proxy/network/...) first and falls
# back to the bare Network-application path on 404. TLS is not verified - consoles ship
# self-signed certificates (same posture as the templates). Raw facts only - Argus core maps
# them to device classes. Stdlib only.
#
# Run by the probe entrypoint when a check-in hands out a sweep job (NOT a Zabbix external check;
# it only lives in externalscripts so the image build ships it automatically):
#   argus_unifi_sweep.py --job <job.json>          job: {"id":12,"url":"https://...","key":"..."}
#     env: ARGUS_PROBE_TOKEN + ARGUS_CHECKIN_URL - results are POSTed with the probe's Bearer
#     token to <checkin base>/scan-results; env keeps the token out of argv/ps.
#   argus_unifi_sweep.py --print <url> <api-key>   standalone: print the results JSON to stdout
#     (lab validation on any box with python3 - no Argus needed).
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

TIMEOUT = 30


def get_json(base, prefix, path, key):
    req = urllib.request.Request(base + prefix + path,
                                 headers={"X-API-KEY": key, "Accept": "application/json"})
    ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ValueError("the controller rejected the API key (HTTP %d)" % e.code)
        raise


def sweep(base, key):
    base = (base or "").strip().rstrip("/")
    if not base:
        raise ValueError("controller URL is empty")
    prefix = "/proxy/network"
    try:
        sites = get_json(base, prefix, "/api/self/sites", key)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        prefix = ""  # plain self-hosted controller: no UniFi OS proxy prefix
        sites = get_json(base, prefix, "/api/self/sites", key)
    if not (sites.get("data") or []):
        raise ValueError("the controller reported no sites")
    hosts = []
    for site in sites["data"]:
        name = site.get("name") or "default"
        desc = site.get("desc") or ""
        devs = get_json(base, prefix, "/api/s/%s/stat/device" % name, key)
        for d in devs.get("data") or []:
            ip = (d.get("ip") or "").strip()
            if not d.get("adopted") or not ip:
                continue  # a device without an IP can't be monitored
            hosts.append({
                "ip": ip,
                "mac": (d.get("mac") or "").strip().lower(),
                "unifi": {
                    "name": (d.get("name") or "").strip(),
                    "model": d.get("model") or "",
                    "type": (d.get("type") or "").lower(),
                    "state": int(d.get("state") or 0),
                    "version": d.get("version") or "",
                    "site": name,
                    "site_desc": desc,
                },
            })
    try:
        hosts.sort(key=lambda h: socket.inet_aton(h["ip"]))
    except OSError:
        hosts.sort(key=lambda h: h["ip"])
    return {"hosts": hosts}


def post_results(url, token, body):
    data = json.dumps(body).encode("utf-8")
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data, method="POST",
                                         headers={"Authorization": "Bearer " + token,
                                                  "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30):
                return True
        except Exception:
            if attempt < 2:
                time.sleep(10)
    return False


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "--print":
        if len(sys.argv) < 4:
            sys.stderr.write("usage: argus_unifi_sweep.py --print <url> <api-key>\n")
            sys.exit(1)
        print(json.dumps(sweep(sys.argv[2], sys.argv[3]), indent=2))
        return
    if mode == "--job":
        token = os.environ.get("ARGUS_PROBE_TOKEN", "")
        checkin = os.environ.get("ARGUS_CHECKIN_URL", "")
        if len(sys.argv) < 3 or not token or not checkin:
            sys.stderr.write("--job needs a job file plus ARGUS_PROBE_TOKEN and ARGUS_CHECKIN_URL\n")
            sys.exit(1)
        with open(sys.argv[2]) as f:
            job = json.load(f)
        url = checkin.rsplit("/", 1)[0] + "/scan-results"  # .../api/probes/checkin -> .../scan-results
        body = {"job_id": int(job.get("id") or 0), "error": "", "hosts": []}
        try:
            out = sweep(job.get("url") or "", job.get("key") or "")
            body["hosts"] = out["hosts"]
        except Exception as e:  # an unreachable controller etc. must still complete the job on core
            body["error"] = str(e)[:200]
        if not post_results(url, token, body):
            sys.exit(1)
        return
    sys.stderr.write("usage: argus_unifi_sweep.py --job <job.json> | --print <url> <api-key>\n")
    sys.exit(1)


if __name__ == "__main__":
    main()
