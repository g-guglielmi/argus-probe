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
#   ip, isp, colo, city     - the public address, its network's owner, the Cloudflare site and city
#   error                   - why it could not measure ("" when it did); the numbers measured so far
#                             are still printed
# Stdlib only. A run takes about 2 x <seconds> + 5 seconds and moves, on a gigabit line, about
# <seconds> x 125 MB each way: schedule it accordingly.
#
# Usage (as Zabbix runs it):
#   argus_speedtest.py [<seconds> [<streams>]]
#   seconds - how long each direction is measured (default 8, 3..15)
#   streams - parallel connections per direction (default 4, 1..8)
import sys
import json
import time
import socket
import ssl
import threading
import statistics
import http.client

HOST = "speed.cloudflare.com"
USER_AGENT = "Argus speedtest"
DOWN_CHUNK = 25_000_000  # bytes per download request
UP_CHUNK = 8_000_000  # bytes per upload request
WARMUP = 1.0  # seconds of each phase left out (TCP slow start)
SOCK_TIMEOUT = 10
LATENCY_SAMPLES = 20


def connect():
    ctx = ssl.create_default_context()
    return http.client.HTTPSConnection(HOST, 443, timeout=SOCK_TIMEOUT, context=ctx)


def headers(extra=None):
    h = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if extra:
        h.update(extra)
    return h


# The Server-Timing entries that are Cloudflare's own time on a request (its edge and the speed test
# worker; older answers carry one cfRequestDuration), left out of a round trip.
SERVER_TIMES = ("cfSpeedEdge", "cfSpeedWorker", "cfRequestDuration")


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
    return max(ms - server_ms(resp), 0.1)


def why(e):
    """A network error, as a reason."""
    if isinstance(e, socket.timeout):
        return "no answer within %d s" % SOCK_TIMEOUT
    if isinstance(e, socket.gaierror):
        return "%s does not resolve (no DNS or no internet)" % HOST
    if isinstance(e, ssl.SSLError):
        return "TLS failed: %s" % (e.reason or e)
    if isinstance(e, ConnectionRefusedError):
        return "connection refused"
    if isinstance(e, OSError) and e.strerror:
        return e.strerror.lower()
    return str(e) or e.__class__.__name__


def meta(out):
    """The public address and its network, as Cloudflare sees them (its speed test page's /meta,
    which answers the page's own requests); without it, the address and site from /cdn-cgi/trace."""
    conn = connect()
    try:
        conn.request("GET", "/meta", headers=headers({"Referer": "https://%s/" % HOST}))
        resp = conn.getresponse()
        body = resp.read()
        if resp.status == 200:
            m = json.loads(body.decode("utf-8", "replace"))
            out["ip"] = str(m.get("clientIp") or "")[:64]
            out["isp"] = str(m.get("asOrganization") or "")[:120]
            colo = m.get("colo") or ""
            if isinstance(colo, dict):  # newer answers: {"iata": "MXP", "city": ..., ...}
                colo = colo.get("iata") or ""
            out["colo"] = str(colo)[:16]
            out["city"] = str(m.get("city") or "")[:64]
            return
        conn.request("GET", "/cdn-cgi/trace", headers=headers())
        resp = conn.getresponse()
        body = resp.read()
        if resp.status == 200:
            kv = dict(line.split("=", 1) for line in body.decode("utf-8", "replace").splitlines() if "=" in line)
            out["ip"] = kv.get("ip", "")[:64]
            out["colo"] = kv.get("colo", "")[:16]
    finally:
        conn.close()


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
    """One direction: streams moving data until the deadline, a side connection timing round trips,
    and the bytes counted after the warm-up."""

    def __init__(self, seconds):
        self.lock = threading.Lock()
        self.bytes = 0
        self.start = time.perf_counter()
        self.warm = self.start + WARMUP
        self.deadline = self.warm + seconds
        self.at_warm = None
        self.loaded = []
        self.errors = []

    def add(self, n):
        now = time.perf_counter()
        with self.lock:
            if self.at_warm is None and now >= self.warm:
                self.at_warm = (self.bytes, now)
            self.bytes += n

    def bps(self):
        end = time.perf_counter()
        with self.lock:
            if self.at_warm is None:
                return None
            b0, t0 = self.at_warm
            moved = self.bytes - b0
        span = min(end, self.deadline) - t0
        return moved * 8 / span if span > 0 else None

    def fail(self, e):
        with self.lock:
            self.errors.append(why(e))

    def pinger(self):
        try:
            conn = connect()
            ping(conn)
            while time.perf_counter() < self.deadline:
                time.sleep(0.4)
                if time.perf_counter() >= self.warm:
                    self.loaded.append(ping(conn))
            conn.close()
        except Exception:  # a lost side measurement isn't a failed test
            pass


def download_stream(ph):
    buf = bytearray(256 * 1024)
    view = memoryview(buf)
    try:
        conn = connect()
        while time.perf_counter() < ph.deadline:
            conn.request("GET", "/__down?bytes=%d" % DOWN_CHUNK, headers=headers())
            resp = conn.getresponse()
            if resp.status != 200:
                raise OSError(0, "speed.cloudflare.com answered HTTP %d" % resp.status)
            while time.perf_counter() < ph.deadline:
                n = resp.readinto(view)
                if not n:
                    break
                ph.add(n)
            if time.perf_counter() >= ph.deadline:
                break
        conn.close()
    except Exception as e:
        ph.fail(e)


def upload_stream(ph):
    piece = b"0" * (64 * 1024)
    try:
        conn = connect()
        while time.perf_counter() < ph.deadline:
            conn.putrequest("POST", "/__up")
            for k, v in headers({"Content-Type": "application/octet-stream", "Content-Length": str(UP_CHUNK)}).items():
                conn.putheader(k, v)
            conn.endheaders()
            sent = 0
            while sent < UP_CHUNK:
                n = min(len(piece), UP_CHUNK - sent)
                conn.send(piece[:n])
                sent += n
                ph.add(n)
                if time.perf_counter() >= ph.deadline:
                    break
            if sent < UP_CHUNK:
                break  # stopped mid-request: the connection is dropped below
            resp = conn.getresponse()
            resp.read()
            if resp.status != 200:
                raise OSError(0, "speed.cloudflare.com answered HTTP %d" % resp.status)
        conn.close()
    except Exception as e:
        ph.fail(e)


def run_phase(target, seconds, streams):
    ph = Phase(seconds)
    threads = [threading.Thread(target=target, args=(ph,), daemon=True) for _ in range(streams)]
    threads.append(threading.Thread(target=ph.pinger, daemon=True))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=seconds + WARMUP + SOCK_TIMEOUT + 2)
    rate = ph.bps()
    loaded = round(statistics.median(ph.loaded), 2) if ph.loaded else None
    err = ph.errors[0] if ph.errors and (rate is None or len(ph.errors) == streams) else ""
    return rate, loaded, err


def arg_int(i, default, lo, hi):
    try:
        v = int(sys.argv[i]) if len(sys.argv) > i and sys.argv[i].strip() != "" else default
    except ValueError:
        v = default
    return max(lo, min(hi, v))


def main():
    seconds = arg_int(1, 8, 3, 15)
    streams = arg_int(2, 4, 1, 8)
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
    down, out["loaded_down_ms"], err = run_phase(download_stream, seconds, streams)
    if down is not None:
        out["down_bps"] = round(down)
    elif err:
        out["error"] = "the download failed: " + err
    up, out["loaded_up_ms"], err = run_phase(upload_stream, seconds, streams)
    if up is not None:
        out["up_bps"] = round(up)
    elif err and not out["error"]:
        out["error"] = "the upload failed: " + err
    print(json.dumps(out))


if __name__ == "__main__":
    main()
