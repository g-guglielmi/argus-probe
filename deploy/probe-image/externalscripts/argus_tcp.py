#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi

# argus_tcp.py - Argus TCP port check (Zabbix external check).
#
# MIRRORED, keep byte-identical:
#   argus-probe: deploy/probe-image/externalscripts/argus_tcp.py  (baked into the proxy image)
#   argus-core:  deploy/core/externalscripts/argus_tcp.py         (installed on the core by setup-core.sh)
# Runs on whichever Zabbix instance monitors the host - a proxy, or the core server directly.
#
# Opens a plain TCP connection to each listed port of the host, all at once, and prints one JSON
# object the "Argus TCP ports" template reads: per port whether it answered, how long the connection
# took, and when it didn't, why ("connection refused: nothing listens on 3389", "no answer within 3 s",
# "no route to host"). The list also drives the template's discovery, so each port becomes its own
# sensor. Nothing is sent over the connection; it is closed as soon as it opens. Stdlib only.
#
# Usage (as Zabbix runs it):
#   argus_tcp.py <host> <ports> [<timeout>]
#   host     - the target (the template passes {HOST.CONN})
#   ports    - comma or space separated, each "port" or "name:port" ("22, RDP:3389, 443"); at most
#              MAX_PORTS. A port without a name gets the usual service name when it has one.
#   timeout  - seconds to wait for each connection (default 3, at most 10)
#
# A port that doesn't answer is a reading (up 0, with its reason), not an error. A bad ports list is
# the one error: it is printed in "error" and every port is left out.
import sys
import re
import json
import socket
import time
import errno
import threading

MAX_PORTS = 32
ENTRY_RE = re.compile(r"^(?:([A-Za-z][A-Za-z0-9._-]{0,31}):)?([0-9]{1,5})$")
HOST_RE = re.compile(r"^[A-Za-z0-9.:_\[\]-]{1,253}$")

# The usual service on a port, for a port listed without a name.
KNOWN = {
    21: "FTP", 22: "SSH", 23: "Telnet", 25: "SMTP", 53: "DNS", 80: "HTTP", 110: "POP3",
    143: "IMAP", 389: "LDAP", 443: "HTTPS", 445: "SMB", 465: "SMTPS", 587: "Submission",
    636: "LDAPS", 993: "IMAPS", 995: "POP3S", 1433: "SQL Server", 1521: "Oracle", 1883: "MQTT",
    2049: "NFS", 3306: "MySQL", 3389: "RDP", 5432: "PostgreSQL", 5900: "VNC", 5985: "WinRM",
    5986: "WinRM HTTPS", 6379: "Redis", 8080: "HTTP alt", 8443: "HTTPS alt", 9100: "Printer",
    10050: "Zabbix agent", 10051: "Zabbix server", 27017: "MongoDB",
}


def parse_ports(arg):
    """The ports list -> [(port, name)] in the order given, duplicates dropped, or raise ValueError
    with what is wrong."""
    out, seen = [], set()
    for entry in re.split(r"[,\s]+", arg.strip()):
        if not entry:
            continue
        m = ENTRY_RE.match(entry)
        if not m:
            raise ValueError('"%s" is not a port or name:port' % entry[:40])
        port = int(m.group(2))
        if not 1 <= port <= 65535:
            raise ValueError("port %d is outside 1-65535" % port)
        if port in seen:
            continue
        seen.add(port)
        out.append((port, m.group(1) or KNOWN.get(port, "")))
        if len(out) > MAX_PORTS:
            raise ValueError("at most %d ports per host" % MAX_PORTS)
    return out


def why(e, port, timeout):
    """A failed connection in words."""
    if isinstance(e, socket.timeout):
        return "no answer within %g s (a firewall drops it, or the host is down)" % timeout
    if isinstance(e, socket.gaierror):
        return "the host name does not resolve"
    if isinstance(e, ConnectionRefusedError):
        return "connection refused: nothing listens on %d" % port
    code = getattr(e, "errno", None)
    if code == errno.EHOSTUNREACH:
        return "no route to host"
    if code == errno.ENETUNREACH:
        return "network unreachable"
    if code == errno.ECONNRESET:
        return "the connection was reset"
    s = re.sub(r"^\[Errno -?\d+\]\s*", "", str(e).strip()) or e.__class__.__name__
    return " ".join(s.split())[:200]


def check(host, port, timeout):
    """Connect once: {up 1|0, time seconds (None when down), error}."""
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"up": 1, "time": round(time.monotonic() - start, 6), "error": ""}
    except Exception as e:
        return {"up": 0, "time": None, "error": why(e, port, timeout)}


def run(host, ports, timeout):
    results = [None] * len(ports)

    def one(i, port):
        results[i] = check(host, port, timeout)

    threads = [threading.Thread(target=one, args=(i, p), daemon=True) for i, (p, _) in enumerate(ports)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout + 2)
    out = []
    for (port, name), r in zip(ports, results):
        r = r or {"up": 0, "time": None, "error": "the check did not finish"}
        out.append(dict(port=port, name=name, **r))
    return out


def main():
    if len(sys.argv) < 3:
        sys.stderr.write("usage: argus_tcp.py <host> <ports> [<timeout>]\n")
        sys.exit(1)
    host = sys.argv[1].strip()
    if not HOST_RE.match(host):
        sys.stderr.write("argus_tcp.py: refusing an unexpected host value\n")
        sys.exit(1)
    host = host.strip("[]")
    timeout = 3.0
    if len(sys.argv) > 3 and sys.argv[3].strip():
        try:
            timeout = min(10.0, max(0.5, float(sys.argv[3])))
        except ValueError:
            pass
    out = {"error": "", "ports": [], "port_discovery": []}
    try:
        ports = parse_ports(sys.argv[2])
    except ValueError as e:
        out["error"] = "the port list is not valid: %s" % e
        print(json.dumps(out))
        return
    out["ports"] = run(host, ports, timeout)
    out["port_discovery"] = [
        {"{#PORT}": str(p), "{#PORTNAME}": ("%s (%d)" % (n, p)) if n else str(p)} for p, n in ports
    ]
    print(json.dumps(out))


if __name__ == "__main__":
    main()
