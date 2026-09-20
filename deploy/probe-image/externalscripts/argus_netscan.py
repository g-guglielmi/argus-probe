#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi

# argus_netscan.py - Argus network discovery scanner.
#
# MIRRORED, keep byte-identical:
#   argus-probe: deploy/probe-image/externalscripts/argus_netscan.py  (baked into the proxy image)
#   argus-core:  deploy/core/externalscripts/argus_netscan.py         (installed on the core by setup-core.sh)
#
# Sweeps a subnet and fingerprints what answers: ICMP ping, a small TCP port set, an SNMP GET of
# sysDescr/sysObjectID/sysName (hand-rolled v1/v2c, the same build-the-wire-protocol style as the
# other collectors), an HTTP(S) banner grab, a real DNS query, reverse DNS and the ARP cache. It
# reports raw facts only - Argus core maps them to a suggested device class, so classification
# improves without touching the fleet. Stdlib only.
#
# Run by the probe entrypoint when a check-in hands out a scan job (NOT a Zabbix external check;
# it only lives in externalscripts so the image build ships it automatically):
#   argus_netscan.py --job <job.json>            job: {"id":12,"cidr":"10.0.0.0/24","snmp":{...}}
#     env: ARGUS_PROBE_TOKEN + ARGUS_CHECKIN_URL - results are POSTed with the probe's Bearer
#     token to <checkin base>/scan-results; env keeps the token out of argv/ps.
#   argus_netscan.py --print <cidr> [community]  standalone: print the results JSON to stdout
#     (lab validation on any box with python3 - no Argus needed).
import concurrent.futures
import ipaddress
import json
import os
import random
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.request

TCP_PORTS = [22, 53, 80, 443, 445, 3493, 8080, 8443, 10050]
MAX_HOSTS = 1024      # hard cap; core validates the CIDR to the same limit
WORKERS = 64
DEADLINE_SECS = 480   # overall budget - past it the job is reported partial

_deadline = 0.0


def past_deadline():
    return time.monotonic() > _deadline


def ping(ip):
    try:
        return subprocess.run(["ping", "-c", "1", "-W", "1", ip],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=3).returncode == 0
    except Exception:
        return False


def tcp_open(ip, port):
    try:
        socket.create_connection((ip, port), timeout=1.0).close()
        return True
    except Exception:
        return False


# --- minimal BER encode/decode, enough for an SNMP v1/v2c GET of the system group ---

def ber_tlv(tag, payload):
    n = len(payload)
    if n < 128:
        return bytes([tag, n]) + payload
    lb = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(lb)]) + lb + payload


def ber_int(v):
    if v == 0:
        return ber_tlv(0x02, b"\x00")
    b = v.to_bytes(v.bit_length() // 8 + 1, "big")  # extra byte keeps the sign bit clear
    while len(b) > 1 and b[0] == 0 and b[1] < 0x80:
        b = b[1:]
    return ber_tlv(0x02, b)


def ber_oid(oid):
    parts = [int(p) for p in oid.strip(".").split(".")]
    body = bytes([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        chunk = bytes([p & 0x7F])
        p >>= 7
        while p:
            chunk = bytes([0x80 | (p & 0x7F)]) + chunk
            p >>= 7
        body += chunk
    return ber_tlv(0x06, body)


def ber_decode(data, i=0):
    # One TLV -> ((tag, value), next index); constructed tags decode into a list of TLVs.
    tag = data[i]
    ln = data[i + 1]
    i += 2
    if ln & 0x80:
        n = ln & 0x7F
        ln = int.from_bytes(data[i:i + n], "big")
        i += n
    end = i + ln
    if tag & 0x20:
        items = []
        while i < end:
            item, i = ber_decode(data, i)
            items.append(item)
        return (tag, items), end
    return (tag, data[i:end]), end


def oid_to_str(b):
    if not b:
        return ""
    out = [str(b[0] // 40), str(b[0] % 40)]
    val = 0
    for c in b[1:]:
        val = (val << 7) | (c & 0x7F)
        if not c & 0x80:
            out.append(str(val))
            val = 0
    return "." + ".".join(out)


SNMP_OIDS = {"sysdescr": "1.3.6.1.2.1.1.1.0",
             "sysobjectid": "1.3.6.1.2.1.1.2.0",
             "sysname": "1.3.6.1.2.1.1.5.0"}


def snmp_get(ip, community, version, port):
    """One GetRequest for the three system OIDs. Returns the value dict when the agent answered
    (possibly empty), None when it never did - v1/v2c agents stay silent on a wrong community, so
    an answer at all is itself a fingerprint."""
    reqid = random.randint(1, 0x7FFFFFF)
    varbinds = b"".join(ber_tlv(0x30, ber_oid(o) + ber_tlv(0x05, b"")) for o in SNMP_OIDS.values())
    pdu = ber_tlv(0xA0, ber_int(reqid) + ber_int(0) + ber_int(0) + ber_tlv(0x30, varbinds))
    msg = ber_tlv(0x30, ber_int(0 if version == 1 else 1) +
                  ber_tlv(0x04, community.encode()) + pdu)
    for _ in range(2):  # UDP: one retry
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(1.5)
            sock.sendto(msg, (ip, port))
            data, _ = sock.recvfrom(65535)
            sock.close()
            (_tag, seq), _ = ber_decode(data)
            pdu_items = None
            for t, v in seq:
                if t >= 0xA0 and isinstance(v, list):
                    pdu_items = v
            if not pdu_items or len(pdu_items) < 4:
                return {}
            byoid = {v: k for k, v in SNMP_OIDS.items()}
            out = {}
            for _t, vb in pdu_items[3][1]:
                if not isinstance(vb, list) or len(vb) < 2:
                    continue
                oid = oid_to_str(vb[0][1]).lstrip(".")
                vtag, vval = vb[1]
                if vtag == 0x04:
                    val = vval.decode("utf-8", "replace")
                elif vtag == 0x06:
                    val = oid_to_str(vval)
                elif vtag == 0x02:
                    val = str(int.from_bytes(vval, "big", signed=True))
                else:
                    val = ""  # NULL / noSuchObject / unhandled type
                name = byoid.get(oid)
                if name and val:
                    out[name] = val.strip()
            return out
        except Exception:
            continue
    return None


def dns_answers(ip):
    # A real A query; any well-formed reply (even REFUSED) proves a DNS service is listening.
    try:
        tid = random.randint(0, 0xFFFF)
        pkt = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
        for label in (b"example", b"com"):
            pkt += bytes([len(label)]) + label
        pkt += b"\x00" + struct.pack(">HH", 1, 1)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2)
        sock.sendto(pkt, (ip, 53))
        data, _ = sock.recvfrom(2048)
        sock.close()
        return len(data) >= 12 and struct.unpack(">H", data[0:2])[0] == tid
    except Exception:
        return False


def http_fingerprint(ip, ports):
    # Banner-grab the first web port that answers (https first - richer titles than an http
    # redirect stub): status + Server header + <title>.
    import http.client
    import ssl
    candidates = [(p, s) for p, s in ((443, "https"), (80, "http"), (8443, "https"), (8080, "http"))
                  if p in ports]
    for port, scheme in candidates:
        try:
            if scheme == "https":
                conn = http.client.HTTPSConnection(ip, port, timeout=3,
                                                   context=ssl._create_unverified_context())
            else:
                conn = http.client.HTTPConnection(ip, port, timeout=3)
            conn.request("GET", "/", headers={"Host": ip, "User-Agent": "argus-netscan"})
            resp = conn.getresponse()
            body = resp.read(8192).decode("utf-8", "replace")
            conn.close()
            title = ""
            m = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
            if m:
                title = re.sub(r"\s+", " ", m.group(1)).strip()[:120]
            return {"port": port, "scheme": scheme, "status": resp.status,
                    "server": (resp.getheader("Server") or "")[:80], "title": title}
        except Exception:
            continue
    return None


def rdns(ip):
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return ""


def arp_table():
    macs = {}
    try:
        with open("/proc/net/arp") as f:
            next(f, None)
            for line in f:
                cols = line.split()
                if len(cols) >= 4 and cols[2] != "0x0" and cols[3] != "00:00:00:00:00:00":
                    macs[cols[0]] = cols[3].lower()
    except Exception:
        pass
    return macs


def scan_host(ip, snmp_cfg):
    if past_deadline():
        return None
    alive = ping(ip)
    open_ports = [p for p in TCP_PORTS if tcp_open(ip, p)]
    snmp = None
    if snmp_cfg and snmp_cfg.get("community"):
        snmp = snmp_get(ip, snmp_cfg["community"], int(snmp_cfg.get("version") or 2),
                        int(snmp_cfg.get("port") or 161))
    if not alive and not open_ports and snmp is None:
        return None
    host = {"ip": ip, "alive": 1, "mac": "", "rdns": rdns(ip), "tcp": open_ports}
    if snmp:
        host["snmp"] = snmp
    if 53 in open_ports and dns_answers(ip):
        host["dns"] = True
    fp = http_fingerprint(ip, open_ports)
    if fp:
        host["http"] = fp
    return host


def scan(cidr, snmp_cfg):
    global _deadline
    net = ipaddress.ip_network(cidr.strip(), strict=False)
    targets = [str(h) for h in net.hosts()][:MAX_HOSTS]
    _deadline = time.monotonic() + DEADLINE_SECS
    hosts = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for res in pool.map(lambda ip: scan_host(ip, snmp_cfg), targets):
            if res:
                hosts.append(res)
    macs = arp_table()  # read after the sweep so the contact we just made populated it
    for h in hosts:
        h["mac"] = macs.get(h["ip"], "")
    hosts.sort(key=lambda h: socket.inet_aton(h["ip"]))
    return {"hosts": hosts, "partial": past_deadline()}


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
        if len(sys.argv) < 3:
            sys.stderr.write("usage: argus_netscan.py --print <cidr> [community]\n")
            sys.exit(1)
        snmp_cfg = {"community": sys.argv[3]} if len(sys.argv) > 3 and sys.argv[3] else None
        print(json.dumps(scan(sys.argv[2], snmp_cfg), indent=2))
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
            out = scan(job.get("cidr") or "", job.get("snmp"))
            body["hosts"] = out["hosts"]
            if out["partial"]:
                body["error"] = "scan hit the time budget - results are partial"
        except Exception as e:  # a bad CIDR etc. must still complete the job on core
            body["error"] = str(e)[:200]
        if not post_results(url, token, body):
            sys.exit(1)
        return
    sys.stderr.write("usage: argus_netscan.py --job <job.json> | --print <cidr> [community]\n")
    sys.exit(1)


if __name__ == "__main__":
    main()
