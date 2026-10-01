#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi

# argus_http.py - Argus HTTP check (Zabbix external check).
#
# MIRRORED, keep byte-identical:
#   argus-probe: deploy/probe-image/externalscripts/argus_http.py  (baked into the proxy image)
#   argus-core:  deploy/core/externalscripts/argus_http.py         (installed on the core by setup-core.sh)
# Runs on whichever Zabbix instance monitors the host - a proxy, or the core server directly.
#
# Fetches each listed URL of a host, all at once, and prints one JSON object the "Argus HTTP Endpoint"
# template reads: per URL whether it answered as expected, the status code, how long it took, the
# days its certificate has left and, when it didn't answer as expected, why ("returned 502 Bad
# Gateway", "the page does not contain \"Welcome\"", "the certificate is not trusted: self-signed
# certificate", "no answer within 10 s"). The list also drives the template's discovery, so each URL
# becomes its own sensor. Stdlib only.
#
# Usage (as Zabbix runs it):
#   argus_http.py <host> <urls> [<scheme> <port> <expect> <verify> <timeout>]
#   host     - the host's address (the template passes {HOST.CONN})
#   urls     - comma or space separated, at most MAX_URLS; each a full http(s) URL
#              ("https://portal.example.com/app"), or a path on the host's own address ("/login"). Blank
#              checks the host's own address once. A URL can end in "#text": the page must contain text
#              (case-insensitive; %20 for a space), or "#!text": it must not.
#   scheme   - http or https, for paths and a blank list (default https)
#   port     - the port for those (default 443 for https, 80 for http)
#   expect   - the accepted status codes of the final answer, like "200-299" or "200,204,401" (default
#              200-299); redirects are followed, up to MAX_REDIRECTS
#   verify   - how the certificate is checked: "verify" (default) wants one a known CA issued, for the
#              URL's name and valid now; "self-signed" also takes one no CA vouches for (a device's own,
#              a private CA), still for the URL's name and valid now; "ignore" takes any certificate.
#              Its expiry is reported in every mode.
#   timeout  - seconds to wait for each answer (default 10, at most 15); the whole run stays within
#              RUN_BUDGET seconds, under the template's item timeout
#
# A URL that doesn't answer as expected is a reading (up 0, with its reason), not an error. A bad URL
# list is the one error: it is printed in "error" and every URL is left out.
import sys
import re
import json
import ssl
import time
import errno
import socket
import hashlib
import threading
import http.client
import urllib.parse
from datetime import datetime, timezone

MAX_URLS = 16
MAX_REDIRECTS = 5
MAX_BODY = 1 << 20  # bytes of a page read to look for its text
RUN_BUDGET = 50  # seconds the whole run may take (the template's item timeout is 60 s)
USER_AGENT = "Argus HTTP check"
HOST_RE = re.compile(r"^[A-Za-z0-9.:_\[\]-]{1,253}$")
# Characters a URL entry may hold: no spaces, quotes, backslashes, backticks or "$" (the values reach a
# command line), nothing a URL doesn't use.
ENTRY_RE = re.compile(r"^[A-Za-z0-9._~:/?#\[\]@!&'()*+,;=%-]{1,2048}$")
CODES_RE = re.compile(r"^\s*[1-5][0-9]{2}(\s*-\s*[1-5][0-9]{2})?(\s*,\s*[1-5][0-9]{2}(\s*-\s*[1-5][0-9]{2})?)*\s*$")


class Entry:
    """One URL to check: where to fetch, what to call it, and the text the page must (not) contain."""

    def __init__(self, url, name, text="", absent=False, raw=""):
        self.url, self.name, self.text, self.absent = url, name, text, absent
        self.id = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
        u = urllib.parse.urlsplit(url)
        self.tls = u.scheme == "https"


def display_name(u):
    """How a URL reads in Argus: host[:port] and path, without the scheme ("portal.example.com/app")."""
    netloc = u.hostname or ""
    if ":" in netloc:  # an IPv6 address
        netloc = "[%s]" % netloc
    default = {"https": 443, "http": 80}[u.scheme]
    if u.port and u.port != default:
        netloc += ":%d" % u.port
    path = u.path if u.path not in ("", "/") else ""
    if u.query:
        path += "?" + u.query
    return (netloc + path)[:120]


def parse_urls(arg, host, scheme, port):
    """The URL list -> [Entry] in the order given, duplicates dropped, or raise ValueError with what
    is wrong."""
    base_host = "[%s]" % host if ":" in host else host
    default_port = {"https": 443, "http": 80}[scheme]
    base = "%s://%s%s" % (scheme, base_host, "" if port == default_port else ":%d" % port)
    raw = [e for e in re.split(r"[,\s]+", arg.strip()) if e]
    if not raw:
        raw = ["/"]
    out, seen = [], set()
    for entry in raw:
        if not ENTRY_RE.match(entry):
            raise ValueError('"%s" has characters a URL can\'t have' % entry[:60])
        target, _, frag = entry.partition("#")
        if target.startswith("/"):
            target = base + target
        u = urllib.parse.urlsplit(target)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError('"%s" is not an http(s) URL or a path starting with /' % entry[:60])
        if u.username or u.password:
            raise ValueError('"%s" has a user name or password in it, which is not supported' % u.hostname[:60])
        try:
            u.port
        except ValueError:
            raise ValueError('"%s" has a port outside 1-65535' % entry[:60])
        absent = frag.startswith("!")
        text = urllib.parse.unquote(frag[1:] if absent else frag).strip()
        key = target + ("#" + frag if frag else "")
        if key in seen:
            continue
        seen.add(key)
        name = display_name(u)
        if text:
            name += (' without "%s"' if absent else ' with "%s"') % text[:40]
        out.append(Entry(target, name, text, absent, key))
        if len(out) > MAX_URLS:
            raise ValueError("at most %d URLs per host" % MAX_URLS)
    return out


def parse_codes(spec):
    """"200-399,401" -> [(200, 399), (401, 401)], or raise ValueError."""
    spec = (spec or "").strip() or "200-299"
    if not CODES_RE.match(spec):
        raise ValueError('"%s" is not a list of status codes like 200-399,401' % spec[:40])
    out = []
    for part in spec.split(","):
        lo, _, hi = part.strip().partition("-")
        lo, hi = int(lo), int(hi or lo)
        out.append((min(lo, hi), max(lo, hi)))
    return out


# --- certificates -----------------------------------------------------------------------------------

def _der(b, i):
    """One DER element at i: (tag, content start, content length, next element)."""
    tag, n = b[i], b[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(b[i:i + k], "big")
        i += k
    return tag, i, n, i + n


def der_not_after(der):
    """A certificate's notAfter (unix s) from its DER, read without the ssl module's verification
    (an unverified connection only hands the raw certificate over)."""
    _, c, _, _ = _der(der, 0)  # Certificate
    _, c, _, _ = _der(der, c)  # tbsCertificate
    i = c
    tag, _, _, nxt = _der(der, i)
    if tag == 0xA0:  # [0] version
        i = nxt
    for _ in range(3):  # serialNumber, signature, issuer
        i = _der(der, i)[3]
    _, c, _, _ = _der(der, i)  # validity
    j = _der(der, c)[3]  # past notBefore
    tag, c, n, _ = _der(der, j)  # notAfter
    s = der[c:c + n].decode("ascii")
    if tag == 0x17:  # UTCTime, YYMMDDHHMMSSZ (RFC 5280: 50-99 are 19xx)
        year = int(s[:2])
        s = ("19" if year >= 50 else "20") + s
    return datetime.strptime(s, "%Y%m%d%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()


def verify_reason(e):
    """Why a certificate failed verification, in words."""
    msg = (getattr(e, "verify_message", "") or str(e)).lower()
    if "hostname mismatch" in msg or "ip address mismatch" in msg:
        return "the certificate is for another name"
    if "expired" in msg:
        return "the certificate has expired"
    if "self-signed" in msg or "self signed" in msg:
        return "the certificate is not trusted: self-signed certificate"
    if "unable to get local issuer" in msg or "unknown ca" in msg:
        return "the certificate is not trusted: its issuer is unknown"
    if "not yet valid" in msg:
        return "the certificate is not valid yet"
    return "the certificate is not trusted: " + " ".join(msg.split())[:120]


# --- fetching ---------------------------------------------------------------------------------------

def why(e, port, timeout):
    """A failed connection in words."""
    if isinstance(e, socket.timeout):
        return "no answer within %g s (a firewall drops it, or the host is down)" % timeout
    if isinstance(e, socket.gaierror):
        return "the host name does not resolve"
    if isinstance(e, ConnectionRefusedError):
        return "connection refused: nothing listens on %d" % port
    if isinstance(e, ssl.SSLError):
        return "the TLS handshake failed: " + " ".join((getattr(e, "reason", "") or str(e)).lower().replace("_", " ").split())[:120]
    if isinstance(e, http.client.HTTPException):
        return "the answer is not HTTP: " + (e.__class__.__name__)
    code = getattr(e, "errno", None)
    if code == errno.EHOSTUNREACH:
        return "no route to host"
    if code == errno.ENETUNREACH:
        return "network unreachable"
    if code == errno.ECONNRESET:
        return "the connection was reset"
    s = re.sub(r"^\[Errno -?\d+\]\s*", "", str(e).strip()) or e.__class__.__name__
    return " ".join(s.split())[:200]


def contexts():
    """The certificate-checking TLS context and the accept-anything one."""
    strict = ssl.create_default_context()
    loose = ssl.create_default_context()
    loose.check_hostname = False
    loose.verify_mode = ssl.CERT_NONE
    return strict, loose


def connect(u, ctx, timeout):
    """An open HTTP(S) connection to the URL's host."""
    port = u.port or {"https": 443, "http": 80}[u.scheme]
    if u.scheme == "https":
        conn = http.client.HTTPSConnection(u.hostname, port, timeout=timeout, context=ctx)
    else:
        conn = http.client.HTTPConnection(u.hostname, port, timeout=timeout)
    try:
        conn.connect()
    except Exception:
        conn.close()  # a failed TLS handshake leaves the plain socket open
        raise
    return conn, port


def trusting(der):
    """A context that trusts this one certificate as it is, and still checks its name and dates: how a
    self-signed certificate (or one from a private CA) is verified in the "self-signed" mode."""
    ctx = ssl.create_default_context(cadata=der)
    ctx.verify_flags |= getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)  # a leaf that isn't its own issuer
    return ctx


def certificate(u, mode, timeout):
    """The URL's certificate: (days left or None, why it isn't accepted or "", the open connection when
    it is). An untrusted certificate is read again without checking, for its expiry; in the
    "self-signed" mode it is then accepted if it is for the URL's name and valid now."""
    strict, loose = contexts()
    try:
        conn, _ = connect(u, strict, timeout)
        not_after = ssl.cert_time_to_seconds(conn.sock.getpeercert()["notAfter"])
        return round((not_after - time.time()) / 86400, 1), "", conn
    except ssl.SSLCertVerificationError as e:
        reason = verify_reason(e)
    try:
        conn, _ = connect(u, loose, timeout)
        der = conn.sock.getpeercert(binary_form=True)
        days = round((der_not_after(der) - time.time()) / 86400, 1)
    except Exception:
        return None, reason, None
    if mode == "ignore":
        return days, reason, conn
    conn.close()
    if mode != "self-signed":
        return days, reason, None
    if days < 0:
        return days, "the certificate has expired", None
    try:
        conn, _ = connect(u, trusting(der), timeout)
        return days, "", conn
    except ssl.SSLCertVerificationError as e:
        return days, verify_reason(e), None


def fetch(entry, codes, mode, timeout):
    """Check one URL: {id, up 1|0, status, time (None when it didn't answer), cert_days, error}."""
    out = {"id": entry.id, "up": 0, "status": None, "time": None, "cert_days": None, "error": ""}
    start = time.monotonic()
    url, conn = entry.url, None
    u = urllib.parse.urlsplit(url)
    try:
        if entry.tls:
            out["cert_days"], bad, conn = certificate(u, mode, timeout)
            if bad and mode != "ignore":
                out["error"] = bad
                return out
        strict, loose = contexts()
        for _ in range(MAX_REDIRECTS + 1):
            u = urllib.parse.urlsplit(url)
            if conn is None:
                # A later hop (a redirect) is checked like "verify"; "self-signed" and "ignore" take its
                # certificate as it is (the URL's own one was checked above).
                conn, _ = connect(u, strict if mode == "verify" else loose, timeout)
            path = (u.path or "/") + ("?" + u.query if u.query else "")
            conn.request("GET", path, headers={"Host": u.netloc, "User-Agent": USER_AGENT, "Accept": "*/*", "Connection": "close"})
            resp = conn.getresponse()
            status = resp.status
            loc = resp.getheader("Location")
            if status in (301, 302, 303, 307, 308) and loc:
                resp.read(MAX_BODY)
                conn.close()
                conn = None
                url = urllib.parse.urljoin(url, loc)
                if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
                    out["error"] = "it redirects to %s, which is not an http(s) address" % url[:80]
                    return out
                continue
            body = resp.read(MAX_BODY) if entry.text else resp.read(65536)
            out["time"] = round(time.monotonic() - start, 6)
            out["status"] = status
            if not any(lo <= status <= hi for lo, hi in codes):
                out["error"] = "returned %d %s" % (status, resp.reason or http.client.responses.get(status, ""))
                out["error"] = out["error"].strip()
                return out
            if entry.text:
                found = entry.text.lower() in body.decode("utf-8", "replace").lower()
                if found == entry.absent:
                    out["error"] = ('the page contains "%s"' if entry.absent else 'the page does not contain "%s"') % entry.text[:60]
                    return out
            out["up"] = 1
            return out
        out["error"] = "more than %d redirects" % MAX_REDIRECTS
        return out
    except Exception as e:
        out["error"] = why(e, u.port or {"https": 443, "http": 80}[u.scheme], timeout)
        return out
    finally:
        if conn is not None:
            conn.close()


def run(entries, codes, mode, timeout):
    results = [None] * len(entries)

    def one(i, e):
        results[i] = fetch(e, codes, mode, timeout)

    threads = [threading.Thread(target=one, args=(i, e), daemon=True) for i, e in enumerate(entries)]
    for t in threads:
        t.start()
    budget = time.monotonic() + min(timeout * 3 + 2, RUN_BUDGET)
    for t in threads:
        t.join(max(0, budget - time.monotonic()))
    return [r or {"id": e.id, "up": 0, "status": None, "time": None, "cert_days": None, "error": "the check did not finish"}
            for e, r in zip(entries, results)]


def main():
    if len(sys.argv) < 3:
        sys.stderr.write("usage: argus_http.py <host> <urls> [<scheme> <port> <expect> <verify> <timeout>]\n")
        sys.exit(1)
    host = sys.argv[1].strip()
    if not HOST_RE.match(host):
        sys.stderr.write("argus_http.py: refusing an unexpected host value\n")
        sys.exit(1)
    host = host.strip("[]")
    arg = lambda i: sys.argv[i].strip() if len(sys.argv) > i else ""
    scheme = arg(3).lower() if arg(3).lower() in ("http", "https") else "https"
    port = {"https": 443, "http": 80}[scheme]
    if arg(4).isdigit() and 1 <= int(arg(4)) <= 65535:
        port = int(arg(4))
    mode = arg(6).lower()
    if mode in ("0", "no", "off", "false"):
        mode = "ignore"
    elif mode in ("self", "selfsigned", "self_signed"):
        mode = "self-signed"
    elif mode not in ("ignore", "self-signed"):
        mode = "verify"
    timeout = 10.0
    if arg(7):
        try:
            timeout = min(15.0, max(1.0, float(arg(7))))
        except ValueError:
            pass
    out = {"error": "", "urls": [], "url_discovery": [], "tls_discovery": []}
    try:
        entries = parse_urls(sys.argv[2], host, scheme, port)
        codes = parse_codes(arg(5))
    except ValueError as e:
        out["error"] = "the URL settings are not valid: %s" % e
        print(json.dumps(out))
        return
    out["urls"] = run(entries, codes, mode, timeout)
    out["url_discovery"] = [{"{#URLID}": e.id, "{#URLNAME}": e.name} for e in entries]
    out["tls_discovery"] = [{"{#URLID}": e.id, "{#URLNAME}": e.name} for e in entries if e.tls]
    print(json.dumps(out))


if __name__ == "__main__":
    main()
