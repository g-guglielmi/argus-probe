#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi

# argus_xcpng.py - Argus XCP-NG collector (Zabbix external check).
#
# MIRRORED, keep byte-identical:
#   argus-probe: deploy/probe-image/externalscripts/argus_xcpng.py  (baked into the proxy image)
#   argus-core:  deploy/core/externalscripts/argus_xcpng.py         (installed on the core by setup-core.sh)
# Runs on whichever Zabbix instance monitors the XCP-NG host - a proxy, or the core server directly
# (no-proxy deployments / "Monitored by: Core server").
#
# Speaks XAPI (XML-RPC over HTTPS) to the pool master and prints everything the "Argus XCP-NG by
# XAPI" template reads as ONE JSON object: pool state, every hypervisor in the pool (CPU/memory/
# uptime/version, optional temperature), and - opt-in via the vmmode argument - the resident VMs.
# Host and per-VM CPU/IO rates come from each hypervisor's rrd_updates feed (XAPI dropped the
# host_cpus utilisation fields long ago; the RRDs are the canonical live source). Temperature is
# read through the OPTIONAL "argus-temp" XAPI plugin on dom0 (docs/hosts/xcpng-temp) - hosts
# without it simply omit the temp field. Keeping the whole poll in one master item means dependent
# items parse it with JSONPath - one XAPI session per poll, nothing else.
#
# Usage (as Zabbix runs it): argus_xcpng.py <host> <user> <pass> [vmmode] [ignore]
#   host   - pool master address (the template passes {HOST.CONN}); a slave answers HOST_IS_SLAVE
#            and the script follows the redirect to the master automatically
#   user   - XAPI username (root; XCP-NG local XAPI accounts are root-only)
#   pass   - XAPI password
#   vmmode - off | state | full (default off). "state" adds per-VM power state; "full" adds
#            per-VM CPU / memory / disk I/O / network I/O from the RRDs.
#   ignore - comma-separated VM names to leave out entirely (parked templates, scratch VMs):
#            they disappear from the per-VM lists AND the running/defined counts. Zabbix parses
#            key parameters before expanding macros, so the commas inside the one macro are safe.
#
# A connection failure is NOT an error: it prints reachable=0 so the template's down trigger fires
# instead of the item going unsupported. Rejected credentials print reachable=1, authed=0 (their
# own trigger). Only bad arguments exit non-zero.
import sys
import json
import re
import ssl
import time
import calendar
import urllib.request
import xml.etree.ElementTree as ET
import xmlrpc.client

CALL_TIMEOUT = 8   # per XML-RPC call
RRD_TIMEOUT = 5    # per rrd_updates fetch (one per live host)

OUT = {
    "reachable": 0,
    "authed": 0,
    "pool": {"name": "", "ha": 0, "master": "", "hosts_total": 0, "hosts_live": 0},
    "vm_total": 0,
    "vm_running": 0,
    "hosts": [],
    "vms": [],
    "vms_perf": [],
}


def emit():
    print(json.dumps(OUT, separators=(",", ":")))


class TimeoutTransport(xmlrpc.client.SafeTransport):
    def __init__(self, context):
        super().__init__(context=context)

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = CALL_TIMEOUT
        return conn


class XapiError(Exception):
    def __init__(self, desc):
        super().__init__(":".join(desc) if desc else "unknown")
        self.desc = desc or ["UNKNOWN"]


def call(fn, *args):
    """XAPI result envelopes are {Status, Value|ErrorDescription} - unwrap or raise."""
    r = fn(*args)
    if r.get("Status") != "Success":
        raise XapiError(r.get("ErrorDescription"))
    return r.get("Value")


def connect(addr, user, passwd):
    """Login against addr, following one HOST_IS_SLAVE redirect to the master."""
    ctx = ssl._create_unverified_context()  # XCP-NG hosts run self-signed XAPI certs
    for _ in range(2):
        proxy = xmlrpc.client.ServerProxy("https://" + addr, transport=TimeoutTransport(ctx), allow_none=True)
        r = proxy.session.login_with_password(user, passwd, "1.0", "argus")
        if r.get("Status") == "Success":
            return proxy, r["Value"], addr
        desc = r.get("ErrorDescription") or ["UNKNOWN"]
        if desc[0] == "HOST_IS_SLAVE" and len(desc) > 1:
            addr = desc[1]
            continue
        raise XapiError(desc)
    raise XapiError(["HOST_IS_SLAVE", addr])


def xapi_time(v):
    """xmlrpc DateTime ('20260918T10:20:30Z') -> unix seconds, or None."""
    s = getattr(v, "value", v)
    try:
        return calendar.timegm(time.strptime(str(s).rstrip("Z"), "%Y%m%dT%H:%M:%S"))
    except Exception:
        return None


def fetch_rrds(addr, session_id):
    """One host's rrd_updates feed -> {'host:<uuid>:<metric>': latest, 'vm:<uuid>:<metric>': latest}.

    The feed is an <xport> document: a legend of 'AVERAGE:host:<uuid>:cpu_avg'-style column names
    plus timestamped rows. Rows are not guaranteed oldest-first and idle columns read NaN, so keep,
    per column, the value from the newest row that has a real number.
    """
    ctx = ssl._create_unverified_context()
    url = "https://%s/rrd_updates?session_id=%s&start=%d&cf=AVERAGE&interval=60&host=true" % (
        addr, session_id, int(time.time()) - 300)
    with urllib.request.urlopen(url, timeout=RRD_TIMEOUT, context=ctx) as resp:
        root = ET.fromstring(resp.read())
    legend = [e.text or "" for e in root.findall("./meta/legend/entry")]
    best = {}   # column index -> (t, value)
    for row in root.findall("./data/row"):
        t = int(row.findtext("t") or 0)
        for i, v in enumerate(row.findall("v")):
            try:
                val = float(v.text)
            except (TypeError, ValueError):
                continue
            if val != val:  # NaN
                continue
            if i not in best or t > best[i][0]:
                best[i] = (t, val)
    out = {}
    for i, (_, val) in best.items():
        if i < len(legend):
            parts = legend[i].split(":", 1)  # drop the leading 'AVERAGE'
            if len(parts) == 2:
                out[parts[1]] = val
    return out


def rrd_sum(rrds, prefix, pattern):
    """Sum of every metric matching pattern under prefix ('vm:<uuid>'), or None if none exist."""
    total, found = 0.0, False
    plen = len(prefix) + 1
    for k, v in rrds.items():
        if k.startswith(prefix + ":") and re.fullmatch(pattern, k[plen:]):
            total += v
            found = True
    return total if found else None


def main():
    if len(sys.argv) < 4 or not sys.argv[1]:
        sys.stderr.write("usage: argus_xcpng.py <host> <user> <pass> [vmmode]\n")
        sys.exit(1)
    addr, user, passwd = sys.argv[1], sys.argv[2], sys.argv[3]
    vmmode = (sys.argv[4] if len(sys.argv) > 4 else "off").strip().lower() or "off"
    ignore = set()
    if len(sys.argv) > 5:
        ignore = {n.strip() for n in sys.argv[5].split(",") if n.strip()}

    try:
        proxy, sid, addr = connect(addr, user, passwd)
    except XapiError as e:
        if e.desc[0] == "SESSION_AUTHENTICATION_FAILED":
            OUT["reachable"] = 1   # XAPI answered - the credentials are the problem
        emit()
        return
    except Exception:
        emit()
        return

    try:
        pools = call(proxy.pool.get_all_records, sid)
        hosts = call(proxy.host.get_all_records, sid)
        hmetrics = call(proxy.host_metrics.get_all_records, sid)
        vms = call(proxy.VM.get_all_records, sid)
        vmetrics = call(proxy.VM_metrics.get_all_records, sid)
    except Exception:
        try:
            proxy.session.logout(sid)
        except Exception:
            pass
        OUT["reachable"] = 1
        emit()
        return

    OUT["reachable"] = 1
    OUT["authed"] = 1

    pool = next(iter(pools.values()), {})
    master_ref = pool.get("master", "")

    # Control domains carry the host boot time (dom0 starts with the host); real VMs are everything
    # that is not a template / snapshot / control domain - minus the user's ignore list, which drops
    # a VM from the per-VM lists and the counts alike.
    dom0_by_host = {}
    real_vms = []
    for ref, vm in vms.items():
        if vm.get("is_a_template") or vm.get("is_a_snapshot"):
            continue
        if vm.get("is_control_domain"):
            dom0_by_host[vm.get("resident_on", "")] = vm
            continue
        if vm.get("name_label", "") in ignore:
            continue
        real_vms.append(vm)

    OUT["vm_total"] = len(real_vms)
    OUT["vm_running"] = sum(1 for vm in real_vms if vm.get("power_state") == "Running")

    # One rrd_updates fetch per live host: host cpu_avg always; resident-VM rates only in full mode
    # (each host's feed only covers the VMs resident on it).
    rrds_by_host = {}
    for ref, h in hosts.items():
        hm = hmetrics.get(h.get("metrics", ""), {})
        if not hm.get("live"):
            continue
        try:
            rrds_by_host[ref] = fetch_rrds(h.get("address", ""), sid)
        except Exception:
            rrds_by_host[ref] = {}

    out_hosts = []
    live_count = 0
    for ref, h in hosts.items():
        hm = hmetrics.get(h.get("metrics", ""), {})
        live = 1 if hm.get("live") else 0
        live_count += live
        mem_total = int(hm.get("memory_total", 0) or 0)
        mem_free = int(hm.get("memory_free", 0) or 0)
        mem_used = mem_total - mem_free if mem_total else 0
        rrds = rrds_by_host.get(ref, {})
        cpu = rrds.get("host:%s:cpu_avg" % h.get("uuid", ""))
        # Host boot time lives in other_config.boot_time (epoch seconds - what Xen Orchestra uses);
        # the control domain's VM_metrics.start_time is the fallback, but reads epoch-0 on some
        # releases (XCP-NG 8.3), so it can't be the primary source.
        uptime = None
        try:
            bt = float((h.get("other_config") or {}).get("boot_time"))
            if bt > 0:
                uptime = max(0, int(time.time() - bt))
        except (TypeError, ValueError):
            pass
        if uptime is None:
            dom0 = dom0_by_host.get(ref)
            if dom0:
                started = xapi_time(vmetrics.get(dom0.get("metrics", ""), {}).get("start_time"))
                if started:
                    uptime = max(0, int(time.time()) - started)
        entry = {
            "uuid": h.get("uuid", ""),
            "name": h.get("name_label", ""),
            "live": live,
            "enabled": 1 if h.get("enabled") else 0,
            "cpus": int((h.get("cpu_info") or {}).get("cpu_count", 0) or 0),
            "cpu_pct": round(cpu * 100, 2) if cpu is not None else None,
            "mem_total": mem_total,
            "mem_used": mem_used,
            "mem_pct": round(mem_used * 100.0 / mem_total, 2) if mem_total else None,
            "uptime": uptime,
            "version": (h.get("software_version") or {}).get("product_version", ""),
            "vms": sum(1 for vm in real_vms
                       if vm.get("resident_on") == ref and vm.get("power_state") == "Running"),
        }
        if live:
            # Optional dom0 plugin (docs/hosts/xcpng-temp): absent plugin -> no temp field at all,
            # and the template's temperature item discards, so the sensor row never appears.
            try:
                t = float(call(proxy.host.call_plugin, sid, ref, "argus-temp", "get", {}))
                entry["temp"] = round(t, 1)
            except Exception:
                pass
        out_hosts.append(entry)
    out_hosts.sort(key=lambda e: e["name"])
    OUT["hosts"] = out_hosts

    OUT["pool"] = {
        "name": pool.get("name_label", ""),
        "ha": 1 if pool.get("ha_enabled") else 0,
        "master": (hosts.get(master_ref) or {}).get("name_label", ""),
        "hosts_total": len(hosts),
        "hosts_live": live_count,
    }

    if vmmode in ("state", "full"):
        out_vms = []
        for vm in real_vms:
            out_vms.append({
                "uuid": vm.get("uuid", ""),
                "name": vm.get("name_label", ""),
                "state": vm.get("power_state", ""),
                "host": (hosts.get(vm.get("resident_on", "")) or {}).get("name_label", ""),
            })
        out_vms.sort(key=lambda e: e["name"])
        OUT["vms"] = out_vms

    if vmmode == "full":
        out_perf = []
        for vm in real_vms:
            if vm.get("power_state") != "Running":
                continue
            uuid = vm.get("uuid", "")
            rrds = rrds_by_host.get(vm.get("resident_on", ""), {})
            prefix = "vm:" + uuid
            # Per-vCPU fractions -> mean % (RRDs expose cpu0..cpuN, each 0..1).
            cpus = [v for k, v in rrds.items()
                    if k.startswith(prefix + ":") and re.fullmatch(r"cpu\d+", k[len(prefix) + 1:])]
            mem = rrds.get(prefix + ":memory")                    # bytes, from the hypervisor
            mem_free = rrds.get(prefix + ":memory_internal_free")  # KiB, needs guest tools
            out_perf.append({
                "uuid": uuid,
                "name": vm.get("name_label", ""),
                "cpu_pct": round(sum(cpus) * 100 / len(cpus), 2) if cpus else None,
                "mem_total": int(mem) if mem is not None else None,
                "mem_used": int(mem - mem_free * 1024) if mem is not None and mem_free is not None else None,
                "disk_read": rrd_sum(rrds, prefix, r"vbd_[a-z0-9]+_read"),    # bytes/s
                "disk_write": rrd_sum(rrds, prefix, r"vbd_[a-z0-9]+_write"),  # bytes/s
                "net_rx": rrd_sum(rrds, prefix, r"vif_\d+_rx"),               # bytes/s
                "net_tx": rrd_sum(rrds, prefix, r"vif_\d+_tx"),               # bytes/s
            })
        out_perf.sort(key=lambda e: e["name"])
        OUT["vms_perf"] = out_perf

    try:
        proxy.session.logout(sid)
    except Exception:
        pass
    emit()


if __name__ == "__main__":
    main()
