#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi

# argus_diag.py - Argus diagnostics on a probe: a ping, a traceroute or a DNS lookup an admin or
# helpdesk user started from the Argus UI (core DESIGN 7q). The twin of the core's internal/diag:
# the same requests, the same result shape, the same words.
#
# Run by the probe entrypoint when a check-in hands out a diagnostic (NOT a Zabbix external check; it
# only lives in externalscripts so the image build ships it automatically):
#   argus_diag.py --job <job.json>     job: {"id":..., "tool":"ping|traceroute|dns", "target":"...",
#                                            "count":5, "type":"both|A|AAAA|CNAME|MX|TXT|PTR", "server":""}
#     env: ARGUS_PROBE_TOKEN + ARGUS_CHECKIN_URL - results are POSTed with the probe's Bearer token to
#     <checkin base>/diag-results as they come (the last with done set); env keeps the token out of ps.
#   argus_diag.py --print '<request json>'   standalone: print the final result JSON (lab validation).
#
# Ping and traceroute run busybox's own tools (the probe runs as root) against the target's IPv4
# address, the way the checks resolve names first. A DNS lookup asks the server directly (a small
# client here, so the answer carries its TTL and its rcode) and the system resolver too (musl, what
# the checks use), so an answer the checks never see shows up as the two differing. Stdlib only.
import concurrent.futures
import json
import os
import random
import re
import socket
import struct
import subprocess
import sys
import time

import argus_corepost

MAX_HOPS = 30
TOOLS = ("ping", "traceroute", "dns")
DNS_TYPES = {"A": 1, "AAAA": 28, "CNAME": 5, "MX": 15, "TXT": 16, "PTR": 12}
QTYPES = {"both": ["A", "AAAA"], "A": ["A"], "AAAA": ["AAAA"], "CNAME": ["CNAME"], "MX": ["MX"], "TXT": ["TXT"], "PTR": ["PTR"]}
# A host name or an address, starting and ending with a letter or digit, so nothing reads as an option.
TARGET_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,251}[A-Za-z0-9])?$")
RCODES = {1: ("FORMERR", "FORMERR"), 2: ("SERVFAIL", "SERVFAIL: the server could not get an answer"),
          3: ("NXDOMAIN", "NXDOMAIN: the name does not exist"), 5: ("REFUSED", "REFUSED: the server would not answer")}


def is_addr(s):
    for fam in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(fam, s)
            return True
        except (OSError, ValueError):
            pass
    return False


def clean(req):
    """The request checked again here (Argus checked it already); the error says what is wrong."""
    tool = str(req.get("tool") or "").lower()
    target = str(req.get("target") or "").strip().rstrip(".")
    if tool not in TOOLS:
        raise ValueError("pick ping, traceroute or DNS lookup")
    if not TARGET_RE.match(target):
        raise ValueError("the target must be a host name or an address")
    out = {"tool": tool, "target": target}
    if tool == "ping":
        out["count"] = max(1, min(int(req.get("count") or 5), 20))
    if tool == "dns":
        t = str(req.get("type") or "both")
        t = t if t == "both" else t.upper()
        if t not in QTYPES:
            raise ValueError("pick a record type")
        if t == "PTR" and not is_addr(target):
            raise ValueError("a PTR lookup needs an address as its target")
        server = str(req.get("server") or "").strip()
        if server and not is_addr(server):
            raise ValueError("the DNS server must be an address")
        out["type"], out["server"] = t, server
    return out


def gai_why(e):
    """A resolver error in plain words."""
    if isinstance(e, socket.gaierror):
        if e.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", -5)):
            return "no such name"
        if e.errno == socket.EAI_AGAIN:
            return "the DNS server did not answer"
    if isinstance(e, socket.herror):
        return "no such name"
    return str(e)


def resolve4(target):
    if is_addr(target):
        try:
            socket.inet_pton(socket.AF_INET, target)
            return target
        except OSError:
            raise ValueError("ping and traceroute from a probe are IPv4 only")
    try:
        return socket.getaddrinfo(target, None, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except (OSError, IndexError) as e:
        raise ValueError("could not resolve %s: %s" % (target, gai_why(e)))


def reverse_name(addr, pool):
    """An address's PTR name, or "" (kept short: a missing PTR must not stall a trace)."""
    fut = pool.submit(socket.gethostbyaddr, addr)
    try:
        return fut.result(timeout=1.5)[0].rstrip(".")
    except Exception:
        return ""


def ms_str(v):
    return "%.3f ms" % v


# --- ping ---

# The image's ping is iputils (the Zabbix base image installs it over busybox's): "64 bytes from
# 10.0.0.20: icmp_seq=1 ttl=64 time=0.412 ms". Busybox's "seq=0" reads the same.
PING_TIME = re.compile(r"from (?:\S+ \()?([0-9.]+)\)?: (?:icmp_)?seq=\d+ ttl=\d+ time=([0-9.]+) ms")


def ping_raw(target, addr, replies, done):
    lines = ["PING %s (%s): 56 data bytes" % (target, addr)]
    got = [r["ms"] for r in replies if "ms" in r]
    for r in replies:
        if "ms" in r:
            lines.append("64 bytes from %s: seq=%d time=%s" % (r["from"], r["seq"], ms_str(r["ms"])))
        elif r.get("error"):
            lines.append("ping failed for seq=%d: %s" % (r["seq"], r["error"]))
        else:
            lines.append("no reply for seq=%d" % r["seq"])
    if done and replies:
        lines += ["", "--- %s ping statistics ---" % target,
                  "%d packets transmitted, %d packets received, %d%% packet loss" % (len(replies), len(got), (len(replies) - len(got)) * 100 // len(replies))]
        if got:
            lines.append("round-trip min/avg/max = %.3f/%.3f/%.3f ms" % (min(got), sum(got) / len(got), max(got)))
    return "\n".join(lines) + "\n"


def error_line(text):
    """The last non-empty line of a tool's stderr, kept short."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1][:160] if lines else ""


def run_ping(req, progress):
    res = {"done": False, "raw": "", "ping": {"replies": []}}
    addr = resolve4(req["target"])
    res["addr"] = addr
    for seq in range(req["count"]):
        start = time.monotonic()
        rep = {"seq": seq}
        try:
            p = subprocess.run(["ping", "-c", "1", "-W", "1", addr], capture_output=True, text=True, timeout=5)
            m = PING_TIME.search(p.stdout)
            if m:
                rep["from"], rep["ms"] = m.group(1), float(m.group(2))
            elif p.returncode > 1 or (p.returncode != 0 and p.stderr.strip()):
                # Not a lost reply: ping itself could not run (exit 2, a socket it may not open). Saying
                # so keeps an error from reading like a filter dropping the packets.
                rep["error"] = error_line(p.stderr) or "ping exited with %d" % p.returncode
        except OSError as e:
            rep["error"] = str(e)
        except subprocess.TimeoutExpired:
            pass
        res["ping"]["replies"].append(rep)
        res["raw"] = ping_raw(req["target"], addr, res["ping"]["replies"], False)
        progress(res)
        if seq < req["count"] - 1:
            time.sleep(max(0.0, 1.0 - (time.monotonic() - start)))
    res["raw"] = ping_raw(req["target"], addr, res["ping"]["replies"], True)
    return res


# --- traceroute ---

HOP_LINE = re.compile(r"^\s*(\d+)\s+(.*)$")
UNREACH = {"!N": "network unreachable", "!H": "host unreachable", "!P": "unreachable", "!X": "blocked by a filter",
           "!S": "unreachable", "!F": "unreachable", "!A": "blocked by a filter", "!C": "blocked by a filter"}


def parse_hop(line):
    """One line of busybox's `traceroute -n -q 1`: ' 3  198.51.100.9  11.207 ms', ' 4  *',
    ' 5  192.0.2.1  4.1 ms !H'. None for anything else (the header)."""
    m = HOP_LINE.match(line)
    if not m:
        return None
    hop = {"n": int(m.group(1))}
    rest = m.group(2).split()
    if not rest or rest[0] == "*":
        return hop
    if not is_addr(rest[0]):
        return None
    hop["addr"] = rest[0]
    for i, tok in enumerate(rest):
        if tok == "ms" and i > 0:
            try:
                hop["ms"] = float(rest[i - 1])
            except ValueError:
                pass
        elif tok.startswith("!") and tok[:2] in UNREACH:
            hop["note"] = UNREACH[tok[:2]]
    return hop


def trace_raw(target, addr, hops):
    lines = ["traceroute to %s (%s), %d hops max" % (target, addr, MAX_HOPS)]
    for h in hops:
        if not h.get("addr"):
            lines.append("%2d  *" % h["n"])
            continue
        line = "%2d  %s (%s)" % (h["n"], h.get("name") or h["addr"], h["addr"])
        if "ms" in h:
            line += "  " + ms_str(h["ms"])
        if h.get("note"):
            line += "  !" + h["note"]
        lines.append(line)
    return "\n".join(lines) + "\n"


def traceroute_usage():
    try:
        p = subprocess.run(["traceroute", "--help"], capture_output=True, text=True, timeout=5)
        return p.stdout + p.stderr
    except (OSError, subprocess.TimeoutExpired):
        return ""


def run_trace(req, progress):
    res = {"done": False, "raw": "", "trace": {"hops": [], "reached": False}}
    addr = resolve4(req["target"])
    res["addr"] = addr
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    # ICMP echoes like the core's traceroute, when this busybox has them (FEATURE_TRACEROUTE_USE_ICMP).
    icmp = ["-I"] if "-I" in traceroute_usage() else []
    try:
        proc = subprocess.Popen(["traceroute"] + icmp + ["-n", "-q", "1", "-w", "2", "-m", str(MAX_HOPS), addr],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError as e:
        raise ValueError("could not start traceroute: %s" % e)
    deadline = time.monotonic() + 100
    try:
        for line in proc.stdout:
            hop = parse_hop(line)
            if hop is None:
                continue
            if hop.get("addr"):
                hop["name"] = reverse_name(hop["addr"], pool)
                if hop["addr"] == addr:
                    res["trace"]["reached"] = True
            res["trace"]["hops"].append(hop)
            res["raw"] = trace_raw(req["target"], addr, res["trace"]["hops"])
            progress(res)
            if res["trace"]["reached"] or hop.get("note") or time.monotonic() > deadline:
                break
    finally:
        proc.kill()
        proc.wait()
        pool.shutdown(wait=False)
    return res


# --- DNS ---

def reverse_zone(addr):
    try:
        b = socket.inet_pton(socket.AF_INET, addr)
        return "%d.%d.%d.%d.in-addr.arpa" % (b[3], b[2], b[1], b[0])
    except OSError:
        b = socket.inet_pton(socket.AF_INET6, addr)
        return "".join("%x.%x." % (x & 0xF, x >> 4) for x in reversed(b)) + "ip6.arpa"


def build_query(qid, name, qtype):
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)  # recursion desired, one question
    for label in name.rstrip(".").split("."):
        lb = label.encode("ascii") if label else b""
        if not lb or len(lb) > 63:
            raise ValueError("that name is too long for DNS")
        q += bytes([len(lb)]) + lb
    return q + b"\x00" + struct.pack(">HH", qtype, 1)


def read_name(msg, off, depth=0):
    """A (possibly compressed) name at off: (name, offset after it)."""
    labels, end = [], None
    while True:
        if off >= len(msg) or depth > 20:
            raise ValueError("bad name")
        n = msg[off]
        if n & 0xC0 == 0xC0:
            if end is None:
                end = off + 2
            off = ((n & 0x3F) << 8) | msg[off + 1]
            depth += 1
            continue
        off += 1
        if n == 0:
            break
        labels.append(msg[off:off + n].decode("ascii", "replace"))
        off += n
    return ".".join(labels), (end if end is not None else off)


def parse_answer(msg, qid):
    """(rcode, truncated, [answers]) of a reply to qid."""
    rid, flags, qd, an, _, _ = struct.unpack(">HHHHHH", msg[:12])
    if rid != qid or not flags & 0x8000:
        raise ValueError("the DNS server sent an answer that could not be read")
    off = 12
    for _ in range(qd):
        _, off = read_name(msg, off)
        off += 4
    names = {v: k for k, v in DNS_TYPES.items()}
    out = []
    for _ in range(an):
        _, off = read_name(msg, off)
        rtype, _, ttl, rdlen = struct.unpack(">HHIH", msg[off:off + 10])
        off += 10
        rd = msg[off:off + rdlen]
        t = names.get(rtype)
        val = None
        if t == "A" and rdlen == 4:
            val = socket.inet_ntop(socket.AF_INET, rd)
        elif t == "AAAA" and rdlen == 16:
            val = socket.inet_ntop(socket.AF_INET6, rd)
        elif t in ("CNAME", "PTR"):
            val = read_name(msg, off)[0]
        elif t == "MX":
            val = "%d %s" % (struct.unpack(">H", rd[:2])[0], read_name(msg, off + 2)[0])
        elif t == "TXT":
            parts, i = [], 0
            while i < len(rd):
                parts.append(rd[i + 1:i + 1 + rd[i]].decode("utf-8", "replace"))
                i += 1 + rd[i]
            val = "".join(parts)
        if val is not None:
            out.append({"type": t, "value": val, "ttl": ttl})
        off += rdlen
    return flags & 0x000F, bool(flags & 0x0200), out


def exchange(server, q, qid, tcp=False):
    fam = socket.AF_INET6 if ":" in server else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM)
    s.settimeout(3)
    try:
        s.connect((server, DNS_PORT))
        if tcp:
            s.sendall(struct.pack(">H", len(q)) + q)
            ln = struct.unpack(">H", recv_all(s, 2))[0]
            msg = recv_all(s, ln)
        else:
            s.send(q)
            msg = s.recv(65535)
    except OSError:
        raise ValueError("the DNS server did not answer")
    finally:
        s.close()
    return parse_answer(msg, qid)


def recv_all(s, n):
    b = b""
    while len(b) < n:
        chunk = s.recv(n - len(b))
        if not chunk:
            raise OSError("short read")
        b += chunk
    return b


DNS_PORT = 53


def query_server(server, name, tname):
    qid = random.randint(0, 0xFFFF)
    q = build_query(qid, name, DNS_TYPES[tname])
    err = None
    for _ in range(2):
        try:
            rcode, tc, ans = exchange(server, q, qid)
            if tc:
                rcode, tc, ans = exchange(server, q, qid, tcp=True)
            return rcode, ans
        except ValueError as e:
            err = e
    raise err


def system_lookup(req):
    """What the system resolver (musl, the one the probe's checks use) answers, for the types it can
    be asked: addresses, and names for an address."""
    out = []
    t = req["type"]
    if t in ("both", "A", "AAAA"):
        for tn, fam in (("A", socket.AF_INET), ("AAAA", socket.AF_INET6)):
            if t != "both" and t != tn:
                continue
            try:
                seen = []
                for ai in socket.getaddrinfo(req["target"], None, fam, socket.SOCK_STREAM):
                    if ai[4][0] not in seen:
                        seen.append(ai[4][0])
                out += [{"type": tn, "value": a} for a in seen] or [{"type": tn, "error": "no such name"}]
            except OSError as e:
                out.append({"type": tn, "error": gai_why(e)})
    elif t == "PTR":
        try:
            name, aliases, _ = socket.gethostbyaddr(req["target"])
            out += [{"type": "PTR", "value": n.rstrip(".")} for n in [name] + aliases]
        except OSError as e:
            out.append({"type": "PTR", "error": gai_why(e)})
    return out


def differ(server, system):
    """The system resolver's answers disagree with the server's, type by type."""
    def vals(rows, t):
        return sorted(r["value"] for r in rows if r["type"] == t and not r.get("error"))
    return any(vals(server, t) != vals(system, t) for t in {r["type"] for r in system})


def resolv_conf_server():
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                p = line.split()
                if len(p) >= 2 and p[0] == "nameserver" and is_addr(p[1]):
                    return p[1]
    except OSError:
        pass
    return ""


def run_dns(req, progress):
    server = req["server"] or resolv_conf_server()
    d = {"server": server, "answers": [], "system": [], "differ": False}
    res = {"done": False, "raw": "", "dns": d}
    name = reverse_zone(req["target"]) if req["type"] == "PTR" else req["target"]
    raw = []
    if not server:
        res["error"] = "no DNS server to ask: none was given and the probe has none configured"
    else:
        for tn in QTYPES[req["type"]]:
            try:
                rcode, ans = query_server(server, name, tn)
            except ValueError as e:
                d["answers"].append({"type": tn, "error": str(e)})
                raw.append(";; asked %s for %s %s: %s" % (server, name, tn, e))
                continue
            if rcode:
                short, why = RCODES.get(rcode, ("RCODE %d" % rcode, "RCODE %d" % rcode))
                d["answers"].append({"type": tn, "error": why})
                raw.append(";; asked %s for %s %s: %s" % (server, name, tn, short))
                continue
            raw.append(";; asked %s for %s %s: NOERROR" % (server, name, tn))
            if not ans:
                d["answers"].append({"type": tn, "error": "no %s record" % tn})
            for a in ans:
                d["answers"].append(a)
                raw.append("%s.\t%d\tIN\t%s\t%s" % (name, a["ttl"], a["type"], a["value"]))
    d["system"] = system_lookup(req)
    for a in d["system"]:
        raw.append(";; system resolver, %s: %s" % (a["type"], a.get("error") or a.get("value")))
    d["differ"] = bool(server) and bool(d["system"]) and differ(d["answers"], d["system"])
    res["raw"] = "\n".join(raw) + "\n"
    return res


def run(req, progress=lambda r: None):
    try:
        req = clean(req)
        res = {"ping": run_ping, "traceroute": run_trace, "dns": run_dns}[req["tool"]](req, progress)
    except ValueError as e:
        res = {"raw": "", "error": str(e)[:300]}
    except Exception as e:  # a run must always end on core, with why
        res = {"raw": "", "error": ("the diagnostic failed: %s" % e)[:300]}
    res["done"] = True
    return res


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "--print" and len(sys.argv) > 2:
        print(json.dumps(run(json.loads(sys.argv[2])), indent=2))
        return
    if mode == "--job" and len(sys.argv) > 2:
        token = os.environ.get("ARGUS_PROBE_TOKEN", "")
        checkin = os.environ.get("ARGUS_CHECKIN_URL", "")
        if not token or not checkin:
            sys.stderr.write("--job needs ARGUS_PROBE_TOKEN and ARGUS_CHECKIN_URL\n")
            sys.exit(1)
        with open(sys.argv[2]) as f:
            job = json.load(f)
        url = checkin.rsplit("/", 1)[0] + "/diag-results"  # .../api/probes/checkin -> .../diag-results
        jid = int(job.get("id") or 0)
        last = [0.0]

        def progress(res):
            # Partial results, at most twice a second: the dialog reads them every second.
            if time.monotonic() - last[0] >= 0.5:
                last[0] = time.monotonic()
                argus_corepost.post(url, token, {"id": jid, "result": res}, who="argus_diag", log=False)

        if not argus_corepost.post(url, token, {"id": jid, "result": run(job, progress)}, tries=3, who="argus_diag"):
            sys.exit(1)
        return
    sys.stderr.write("usage: argus_diag.py --job <job.json> | --print '<request json>'\n")
    sys.exit(1)


if __name__ == "__main__":
    main()
