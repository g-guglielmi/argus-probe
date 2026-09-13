#!/usr/bin/env python3
# dns-resolver.py - Argus DNS resolution collector (Zabbix external check).
#
# MIRRORED, keep byte-identical:
#   argus-probe: deploy/probe-image/externalscripts/dns-resolver.py  (baked into the proxy image)
#   argus-core:  deploy/core/externalscripts/dns-resolver.py         (installed on the core by setup-core.sh)
# Runs on whichever Zabbix instance monitors the DNS host - a proxy, or the core server directly
# (no-proxy deployments / "Monitored by: Core server").
#
# Sends a real DNS A-record query to a specific server and reports whether it answered - a genuine
# resolution test, not just a port check. Stdlib only (builds/parses the DNS packet itself), so no
# dnspython dependency. Design follows the Zabbix community DNS name-resolution template.
#
# Usage (as Zabbix runs it):
#   dns-resolver.py discover "<comma,separated,names>"  -> LLD {"data":[{"{#DNSNAME}":"..."}]}
#   dns-resolver.py check <name> <server> [port]        -> {"success":1/0,"time":<seconds>,"ip":..,"rcode":..,"error":..}
import sys
import json
import socket
import struct
import time
import random

RCODES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED"}


def build_query(name):
    tid = random.randint(0, 0xFFFF)
    header = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)  # RD set, QDCOUNT=1
    qname = b""
    for label in name.rstrip(".").split("."):
        b = label.encode("ascii", "ignore")
        qname += bytes([len(b)]) + b
    qname += b"\x00" + struct.pack(">HH", 1, 1)  # QTYPE=A, QCLASS=IN
    return tid, header + qname


def first_a(data, ancount):
    # Best-effort walk of the answer section to pull the first A record; "" on any trouble.
    try:
        i = 12
        while data[i] != 0:               # skip QNAME
            if data[i] & 0xC0 == 0xC0:
                i += 1
                break
            i += 1 + data[i]
        i += 1 + 4                          # null label (+already-consumed ptr byte) + QTYPE/QCLASS
        for _ in range(ancount):
            if data[i] & 0xC0 == 0xC0:      # compressed name pointer
                i += 2
            else:
                while data[i] != 0:
                    i += 1 + data[i]
                i += 1
            rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", data[i:i + 10])
            i += 10
            rdata = data[i:i + rdlen]
            i += rdlen
            if rtype == 1 and rdlen == 4:
                return ".".join(str(b) for b in rdata)
        return ""
    except Exception:
        return ""


def check(name, server, port):
    out = {"success": 0, "time": 0, "ip": "", "rcode": "", "error": ""}
    try:
        tid, pkt = build_query(name)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(3)
        start = time.perf_counter()
        sock.sendto(pkt, (server, port))
        data, _ = sock.recvfrom(2048)
        out["time"] = round(time.perf_counter() - start, 4)
        sock.close()
        if len(data) < 12 or struct.unpack(">H", data[0:2])[0] != tid:
            out["error"] = "unexpected response"
            return out
        flags, _qd, an = struct.unpack(">HHH", data[2:8])
        rcode = flags & 0x0F
        out["rcode"] = RCODES.get(rcode, str(rcode))
        if rcode == 0 and an > 0:
            out["success"] = 1
            out["ip"] = first_a(data, an)
        else:
            out["error"] = out["rcode"]
    except Exception as e:
        out["error"] = str(e)
    return out


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "discover":
        names = sys.argv[2] if len(sys.argv) > 2 else ""
        data = [{"{#DNSNAME}": n.strip()} for n in names.split(",") if n.strip()]
        print(json.dumps({"data": data}))
        return
    if mode == "check":
        if len(sys.argv) < 4:
            sys.stderr.write("usage: dns-resolver.py check <name> <server> [port]\n")
            sys.exit(1)
        name = sys.argv[2]
        server = sys.argv[3]
        port = int(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else 53
        print(json.dumps(check(name, server, port)))
        return
    sys.stderr.write("usage: dns-resolver.py discover <names> | check <name> <server> [port]\n")
    sys.exit(1)


if __name__ == "__main__":
    main()
