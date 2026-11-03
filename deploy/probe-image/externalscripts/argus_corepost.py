#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi

# argus_corepost.py - how the probe's jobs (argus_diag.py, argus_netscan.py, argus_unifi_sweep.py)
# post their results to Argus: a JSON POST to <checkin base>/<what>-results with the probe token.
#
# The core's name is looked up IPv4 first, each family on its own. A plain lookup asks for both at
# once, and musl (this image is Alpine) reads the whole lookup as "no such name" when the AAAA half
# comes back NXDOMAIN, which some DNS setups answer for a name that only has an A record (chained
# rewrites, split horizons). The check-in itself goes through curl and was never hit, so a probe
# could check in fine and still lose every result it posted. A failed post says why on stderr,
# which lands in the probe's container log.
import http.client
import json
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request

USER_AGENT = "argus-probe"


def addresses(host, port):
    """Where host:port can be reached: its IPv4 addresses, then its IPv6 ones."""
    out, last = [], None
    for fam in (socket.AF_INET, socket.AF_INET6):
        try:
            out += [ai[4] for ai in socket.getaddrinfo(host, port, fam, socket.SOCK_STREAM)]
        except socket.gaierror as e:
            last = e
    if not out:
        raise last or OSError("%s has no address" % host)
    return out


def dial(host, port, timeout):
    last = None
    for addr in addresses(host, port):
        try:
            return socket.create_connection(addr[:2], timeout=timeout)
        except OSError as e:
            last = e
    raise last


class _HTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = dial(self.host, self.port, self.timeout)


class _HTTPS(http.client.HTTPSConnection):
    def connect(self):
        self.sock = self._context.wrap_socket(dial(self.host, self.port, self.timeout), server_hostname=self.host)


def post_once(url, token, data, timeout):
    """One POST. None when Argus took it, else why not."""
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json", "User-Agent": USER_AGENT}
    if urllib.request.getproxies().get(urllib.parse.urlsplit(url).scheme):
        # An HTTP(S)_PROXY in the environment: the proxy resolves the name, urllib knows the way.
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST", headers=headers), timeout=timeout):
                return None
        except Exception as e:
            return str(e)
    u = urllib.parse.urlsplit(url)
    try:
        if u.scheme == "https":
            conn = _HTTPS(u.hostname, u.port or 443, timeout=timeout, context=ssl.create_default_context())
        else:
            conn = _HTTP(u.hostname, u.port or 80, timeout=timeout)
        try:
            conn.request("POST", (u.path or "/") + ("?" + u.query if u.query else ""), body=data, headers=headers)
            resp = conn.getresponse()
            try:
                body = resp.read(300)
            finally:
                resp.close()  # it holds the socket open past conn.close() otherwise
        finally:
            conn.close()
    except Exception as e:
        return "%s: %s" % (type(e).__name__, e)
    if 200 <= resp.status < 300:
        return None
    return "HTTP %d %s" % (resp.status, body.decode("utf-8", "replace").strip())


def post(url, token, body, tries=1, wait=3, timeout=10, who="argus-probe", log=True):
    """POST body as JSON, up to tries times wait seconds apart. True once Argus took it; each failed
    try is logged to stderr with why (log=False for the partial results a run sends as it goes)."""
    data = json.dumps(body).encode("utf-8")
    for attempt in range(tries):
        why = post_once(url, token, data, timeout)
        if why is None:
            return True
        if log:
            sys.stderr.write("%s: posting to %s failed (try %d of %d): %s\n" % (who, url, attempt + 1, tries, why))
            sys.stderr.flush()
        if attempt < tries - 1:
            time.sleep(wait)
    return False
