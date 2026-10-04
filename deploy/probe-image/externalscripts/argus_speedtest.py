#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi

# argus_speedtest.py - Argus internet speed test (Zabbix external check).
#
# MIRRORED, keep byte-identical:
#   argus-probe: deploy/probe-image/externalscripts/argus_speedtest.py  (baked into the proxy image)
#   argus-core:  deploy/core/externalscripts/argus_speedtest.py         (installed on the core by setup-core.sh)
# Runs on whichever Zabbix instance monitors the host - a proxy, or the core server directly - so the
# result is that site's internet as its probe sees it.
#
# Measures the connection against Cloudflare's speed test (speed.cloudflare.com, the endpoints its
# MIT-licensed speedtest library uses) and prints one JSON object the "Argus Speedtest" template reads:
#   down_bps, up_bps        - download and upload throughput, bits per second, over several streams
#   latency_ms, jitter_ms   - idle round trip (median) and its jitter (mean change between samples)
#   loaded_down_ms,
#   loaded_up_ms            - the round trip while downloading / uploading (bufferbloat)
#   ip, colo                - the public address and the Cloudflare site reached (its /cdn-cgi/trace)
#   isp                     - the network that announces the address (RIPEstat's public data API)
#   city                    - kept for older templates, always ""
#   error                   - why it could not measure ("" when it did); the numbers measured so far
#                             are still printed
# It asks for what Cloudflare serves any client, with no headers posing as its own page, and a run moves
# at most about what one test on that page moves on a fast line: DOWN_BUDGET down and UP_BUDGET up, shared
# among the streams (downloads of at most DOWN_REQUEST a request: it refuses 100 MB), so a fast line is
# measured on a bounded amount of data and a slower one stops at the deadline. The rate is read from the
# warm-up to the first stream done, while all of them fill the line. Cloudflare still limits how much one
# address tests in an hour: a direction any of whose streams was refused (HTTP 429) reports no speed,
# with why, since the streams left would read low. Stdlib only. A run takes at most about
# 2 x (<seconds> + 1) + 5 seconds.
#
# Usage (as Zabbix runs it):
#   argus_speedtest.py [<seconds> [<streams>]]
#   seconds - the longest each direction is measured, on a line too slow to move its share sooner
#             (default 8, 3..15)
#   streams - parallel connections per direction (default 8, 1..16)
import sys
import json
import time
import socket
import select
import ssl
import threading
import statistics
import http.client

HOST = "speed.cloudflare.com"
USER_AGENT = "Argus speedtest"
# What a run moves at most, all streams together: no more than one test on Cloudflare's own page moves
# on a fast line (its library's steps add up to about 970 MB down and 300 MB up), so a fast line doesn't
# use up the address's hourly allowance in one run.
DOWN_BUDGET = 720_000_000
UP_BUDGET = 300_000_000  # one request a stream (Cloudflare refuses 1 GB: 413)
DOWN_REQUEST = 90_000_000  # the most a download request asks for (100 MB is refused without its page's headers)
WARMUP = 1.0  # seconds of each phase left out (TCP slow start) ...
WARM_SHARE = 0.25  # ... or until a quarter of its data moved, when a fast line gets there sooner
MIN_WINDOW = 0.2  # seconds: a shorter measured span counts from the start instead
SOCK_TIMEOUT = 10
LATENCY_SAMPLES = 12
LOADED_EVERY = 0.25  # seconds between round trips while a direction is busy
# The Server-Timing entries that are Cloudflare's own time on a request (its edge and the speed test
# worker; older answers carry one cfRequestDuration), left out of a round trip.
SERVER_TIMES = ("cfSpeedEdge", "cfSpeedWorker", "cfRequestDuration")


class Refused(Exception):
    """Cloudflare answered with an error status instead of the test."""

    def __init__(self, status, reason="", retry_after=""):
        super().__init__(status)
        self.status, self.reason, self.retry_after = status, reason, retry_after

    def __str__(self):
        if self.status == 429:
            wait = ""
            try:
                mins = max(1, round(int(self.retry_after) / 60))
                wait = ", for about a minute" if mins == 1 else ", for about %d minutes" % mins
            except (TypeError, ValueError):
                pass
            return ("Cloudflare limits how much one address tests in an hour and refuses more%s (HTTP 429); "
                    "other speed tests from this address count too" % wait)
        return "speed.cloudflare.com answered HTTP %d%s" % (self.status, (" " + self.reason) if self.reason else "")


def refused(resp):
    return Refused(resp.status, resp.reason, resp.getheader("Retry-After") or "")


def connect():
    ctx = ssl.create_default_context()
    return http.client.HTTPSConnection(HOST, 443, timeout=SOCK_TIMEOUT, context=ctx)


def headers(extra=None):
    h = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if extra:
        h.update(extra)
    return h


def server_ms(resp):
    """The time Cloudflare spent on the request itself, from its Server-Timing header."""
    total = 0.0
    for part in (resp.getheader("Server-Timing") or "").split(","):
        name, _, rest = part.strip().partition(";")
        if name not in SERVER_TIMES:
            continue
        for kv in rest.split(";"):
            kv = kv.strip()
            if kv.startswith("dur="):
                try:
                    total += float(kv[4:])
                except ValueError:
                    pass
    return total


def ping(conn):
    """One round trip in ms on a kept-alive connection: a zero-byte download, less Cloudflare's own
    time."""
    t = time.perf_counter()
    conn.request("GET", "/__down?bytes=0", headers=headers())
    resp = conn.getresponse()
    resp.read()
    ms = (time.perf_counter() - t) * 1000
    if resp.status != 200:
        raise refused(resp)
    return max(ms - server_ms(resp), 0.1)


def why(e):
    """A network error, as a reason."""
    if isinstance(e, Refused):
        return str(e)
    if isinstance(e, socket.timeout):
        return "no answer within %d s" % SOCK_TIMEOUT
    if isinstance(e, socket.gaierror):
        return "%s does not resolve (no DNS or no internet)" % HOST
    if isinstance(e, ssl.SSLError):
        return "TLS failed: %s" % (e.reason or e)
    if isinstance(e, ConnectionRefusedError):
        return "connection refused"
    if isinstance(e, ConnectionResetError):
        return "the connection was reset"
    if isinstance(e, OSError) and e.strerror:
        return e.strerror.lower()
    return str(e) or e.__class__.__name__


RIPESTAT = "stat.ripe.net"


def holder_name(holder):
    """A network's readable name from its registry holder ("ASN-EXAMPLENET Example Telecom S.p.A." or
    "CLOUDFLARENET - Cloudflare, Inc." -> the name after the handle)."""
    holder = " ".join(str(holder or "").split())
    if " - " in holder:
        return holder.split(" - ", 1)[1]
    first, _, rest = holder.partition(" ")
    if rest and first.upper() == first and any(c.isalpha() for c in first):
        return rest
    return holder


def meta(out):
    """The public address and the Cloudflare site, from Cloudflare's /cdn-cgi/trace; the network that
    announces the address from RIPEstat (best effort: it only names the provider)."""
    conn = connect()
    try:
        conn.request("GET", "/cdn-cgi/trace", headers=headers())
        resp = conn.getresponse()
        body = resp.read()
        if resp.status == 200:
            kv = dict(line.split("=", 1) for line in body.decode("utf-8", "replace").splitlines() if "=" in line)
            out["ip"] = kv.get("ip", "").strip()[:64]
            out["colo"] = kv.get("colo", "").strip()[:16]
    finally:
        conn.close()
    if not out["ip"]:
        return
    rs = http.client.HTTPSConnection(RIPESTAT, 443, timeout=5, context=ssl.create_default_context())
    try:
        rs.request("GET", "/data/prefix-overview/data.json?resource=%s&sourceapp=argus" % out["ip"], headers=headers())
        resp = rs.getresponse()
        body = resp.read()
        if resp.status == 200:
            asns = json.loads(body.decode("utf-8", "replace")).get("data", {}).get("asns") or []
            if asns:
                out["isp"] = holder_name(asns[0].get("holder"))[:120]
    finally:
        rs.close()


def latency(out):
    conn = connect()
    try:
        ping(conn)  # the first one pays for TCP and TLS
        samples = [ping(conn) for _ in range(LATENCY_SAMPLES)]
    finally:
        conn.close()
    out["latency_ms"] = round(statistics.median(samples), 2)
    out["jitter_ms"] = round(statistics.mean(abs(a - b) for a, b in zip(samples, samples[1:])), 2)


class Phase:
    """One direction: streams each moving their share of the budget until it's moved or the deadline,
    a side connection timing round trips while they're all busy, and the rate from the warm-up to the
    first stream done (after it, fewer streams are left to fill the line)."""

    def __init__(self, seconds, budget):
        self.lock = threading.Lock()
        self.bytes = 0
        self.start = time.perf_counter()
        self.warm = self.start + WARMUP
        self.warm_bytes = budget * WARM_SHARE
        self.deadline = self.warm + seconds
        self.at_warm = None  # (bytes, time) when the warm-up ended
        self.at_done = None  # (bytes, time) when the first stream moved its share
        self.over = threading.Event()  # the line is no longer full: stop timing round trips
        self.loaded = []
        self.errors = []

    def add(self, n):
        now = time.perf_counter()
        with self.lock:
            if self.at_warm is None and (now >= self.warm or self.bytes >= self.warm_bytes):
                self.at_warm = (self.bytes, now)
            self.bytes += n

    def done(self):
        now = time.perf_counter()
        with self.lock:
            if self.at_done is None:
                self.at_done = (self.bytes, now)
        self.over.set()

    def bps(self):
        end = min(time.perf_counter(), self.deadline)
        with self.lock:
            if not self.bytes:
                return None
            b0, t0 = self.at_warm or (0, self.start)
            b1, t1 = self.at_done or (self.bytes, end)
        if t1 - t0 < MIN_WINDOW:  # a stream finished within the warm-up: count from the start
            b0, t0 = 0, self.start
        span = t1 - t0
        return (b1 - b0) * 8 / span if span > 0 else None

    def fail(self, e):
        with self.lock:
            self.errors.append(why(e))

    def pinger(self):
        try:
            conn = connect()
            ping(conn)
            while not self.over.wait(LOADED_EVERY):
                if self.at_warm is not None:
                    self.loaded.append(ping(conn))
            conn.close()
        except Exception:  # a lost side measurement isn't a failed test
            pass


def download_stream(ph, share):
    buf = bytearray(256 * 1024)
    view = memoryview(buf)
    conn = None
    try:
        conn = connect()
        left = share
        while left > 0 and time.perf_counter() < ph.deadline:
            conn.request("GET", "/__down?bytes=%d" % min(left, DOWN_REQUEST), headers=headers())
            resp = conn.getresponse()
            if resp.status != 200:
                raise refused(resp)
            while time.perf_counter() < ph.deadline:
                n = resp.readinto(view)
                if not n:
                    break
                ph.add(n)
                left -= n
        if left <= 0:
            ph.done()
    except Exception as e:
        ph.fail(e)
    finally:
        if conn is not None:
            conn.close()  # a request cut at the deadline: the rest isn't wanted


def early_answer(sock):
    """An answer Cloudflare sent before the upload finished (a refusal), or None."""
    if not select.select([sock], [], [], 0)[0]:
        return None
    head = sock.recv(1024).decode("latin-1", "replace")
    lines = head.split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) >= 2 and parts[0].startswith("HTTP/") and parts[1].isdigit():
        retry = next((ln.split(":", 1)[1].strip() for ln in lines[1:] if ln.lower().startswith("retry-after:")), "")
        return Refused(int(parts[1]), parts[2] if len(parts) > 2 else "", retry)
    return Refused(0, "an unexpected answer")


def upload_stream(ph, share):
    piece = b"0" * (64 * 1024)
    conn = None
    try:
        conn = connect()
        conn.putrequest("POST", "/__up")
        for k, v in headers({"Content-Type": "application/octet-stream", "Content-Length": str(share)}).items():
            conn.putheader(k, v)
        conn.endheaders()
        sent, n = 0, 0
        while sent < share:
            if time.perf_counter() >= ph.deadline:
                return  # cut at the deadline: the connection is dropped below
            k = min(len(piece), share - sent)
            conn.send(piece[:k])
            sent += k
            ph.add(k)
            n += 1
            if n % 32 == 0:  # every 2 MB: a refusal arrives as an early answer
                refused = early_answer(conn.sock)
                if refused:
                    raise refused
        resp = conn.getresponse()
        resp.read()
        if resp.status != 200:
            raise refused(resp)
        ph.done()  # Cloudflare answers once it has the whole upload
    except Exception as e:
        ph.fail(e)
    finally:
        if conn is not None:
            conn.close()


def run_phase(target, seconds, streams, budget):
    ph = Phase(seconds, budget)
    threads = [threading.Thread(target=target, args=(ph, budget // streams), daemon=True) for _ in range(streams)]
    pinger = threading.Thread(target=ph.pinger, daemon=True)
    for t in threads + [pinger]:
        t.start()
    for t in threads:
        t.join(timeout=seconds + WARMUP + SOCK_TIMEOUT + 2)
    ph.over.set()
    pinger.join(timeout=SOCK_TIMEOUT)
    loaded = round(statistics.median(ph.loaded), 2) if ph.loaded else None
    if ph.errors:
        # A stream that failed leaves the others to read low: no speed, but why.
        n = len(ph.errors)
        lead = "all %d connections failed" % streams if n == streams else "%d of %d connections failed" % (n, streams)
        return None, loaded, "%s: %s" % (lead, ph.errors[0])
    return ph.bps(), loaded, ""


def arg_int(i, default, lo, hi):
    try:
        v = int(sys.argv[i]) if len(sys.argv) > i and sys.argv[i].strip() != "" else default
    except ValueError:
        v = default
    return max(lo, min(hi, v))


def main():
    seconds = arg_int(1, 8, 3, 15)
    streams = arg_int(2, 8, 1, 16)
    out = {"down_bps": None, "up_bps": None, "latency_ms": None, "jitter_ms": None, "loaded_down_ms": None,
           "loaded_up_ms": None, "ip": "", "isp": "", "colo": "", "city": "", "error": ""}
    try:
        meta(out)
    except Exception:
        pass  # the address and network are nice to have; the test itself says what fails
    try:
        latency(out)
    except Exception as e:
        out["error"] = "could not reach %s: %s" % (HOST, why(e))
        print(json.dumps(out))
        return
    down, out["loaded_down_ms"], err = run_phase(download_stream, seconds, streams, DOWN_BUDGET)
    if down is not None:
        out["down_bps"] = round(down)
    elif err:
        out["error"] = "the download: " + err
    up, out["loaded_up_ms"], err = run_phase(upload_stream, seconds, streams, UP_BUDGET)
    if up is not None:
        out["up_bps"] = round(up)
    elif err and not out["error"]:
        out["error"] = "the upload: " + err
    print(json.dumps(out))


if __name__ == "__main__":
    main()
