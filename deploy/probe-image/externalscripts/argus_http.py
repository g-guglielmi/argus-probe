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
#              ("https://portal.example.com/app"), a host without a scheme ("10.0.0.20:8443/admin",
#              which gets <scheme>), or a path on the host's own address ("/login"). Blank checks the
#              host's own address once. Options for one URL go after "#", like a query:
#              "#tls=self-signed&text=Welcome%20back" - tls (verify, self-signed or ignore) checks its
#              certificate unlike the host's <verify>, text is text the page must contain
#              (case-insensitive, %20 for a space), notext text it must not, name is what Argus calls
#              it ("#name=Microsoft%20365", instead of its host and path). The older "#text" and
#              "#!text" still read as text and notext.
#   scheme   - http or https, for paths, hosts without a scheme and a blank list (default https)
#   port     - the port for those (default 443 for https, 80 for http)
#   expect   - the accepted status codes of the final answer, like "200-299" or "200,204,401" (default
#              200-299); redirects are followed, up to MAX_REDIRECTS
#   verify   - how the certificate is checked: "verify" (default) wants one a known CA issued, for the
#              URL's name and valid now; "self-signed" also takes one no CA vouches for (a device's own,
#              a private CA), still valid now and, when the URL uses a name, for that name (a URL by IP
#              address isn't name-checked: a device's own certificate rarely lists its address);
#              "ignore" takes any certificate and doesn't read it, so that URL reports no days left
#              (its expiry is reported in the other two modes).
#   timeout  - seconds to wait for each answer (default 10, at most 15); the whole run stays within
#              RUN_BUDGET seconds, under the template's item timeout
#
# A URL whose certificate isn't accepted is down with that reason, but its page is still asked for
# (over the unchecked connection), so its response time and status code keep coming.
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
import ipaddress
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


MODES = ("verify", "self-signed", "ignore")


class Entry:
    """One URL to check: where to fetch, what to call it, the text the page must (not) contain, and
    its own certificate check ("" = the host's). Its id is the URL's alone, so changing its options
    keeps its sensors and their history."""

    def __init__(self, url, name, text="", absent=False, mode=""):
        self.url, self.name, self.text, self.absent, self.mode = url, name, text, absent, mode
        self.id = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        u = urllib.parse.urlsplit(url)
        self.tls = u.scheme == "https"


NAME_MAX = 60


def parse_options(frag, entry):
    """A URL's options after "#" -> (text, absent, mode, name), or raise ValueError.
    "tls=...&text=...&name=..." is the form; a fragment without "=" is the older "#text" / "#!text"."""
    if not frag:
        return "", False, "", ""
    if "=" not in frag:
        absent = frag.startswith("!")
        return urllib.parse.unquote(frag[1:] if absent else frag).strip(), absent, "", ""
    text, absent, mode, name = "", False, "", ""
    for part in frag.split("&"):
        k, _, v = part.partition("=")
        v = urllib.parse.unquote(v).strip()
        if k == "tls":
            if v not in MODES:
                raise ValueError('"%s": tls must be verify, self-signed or ignore' % entry[:60])
            mode = v
        elif k in ("text", "notext"):
            if not v:
                raise ValueError('"%s": %s needs the text to look for' % (entry[:60], k))
            text, absent = v, k == "notext"
        elif k == "name":
            name = " ".join(v.split())[:NAME_MAX]
        elif k:
            raise ValueError('"%s": "%s" is not an option (tls, text, notext or name)' % (entry[:60], k[:20]))
    return text, absent, mode, name


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
        elif "://" not in target:
            target = "%s://%s" % (scheme, target)  # a host without a scheme
        u = urllib.parse.urlsplit(target)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError('"%s" is not an http(s) URL or a path starting with /' % entry[:60])
        if u.username or u.password:
            raise ValueError('"%s" has a user name or password in it, which is not supported' % u.hostname[:60])
        try:
            u.port
        except ValueError:
            raise ValueError('"%s" has a port outside 1-65535' % entry[:60])
        text, absent, mode, name = parse_options(frag, entry)
        if target in seen:
            continue
        seen.add(target)
        out.append(Entry(target, name or display_name(u), text, absent, mode))
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


OID_CN = bytes([0x55, 0x04, 0x03])  # 2.5.4.3, commonName
OID_SAN = bytes([0x55, 0x1D, 0x11])  # 2.5.29.17, subjectAltName


def _time(tag, raw):
    """A UTCTime (YYMMDDHHMMSSZ; RFC 5280: 50-99 are 19xx) or GeneralizedTime as unix s."""
    s = raw.decode("ascii")
    if tag == 0x17:
        s = ("19" if int(s[:2]) >= 50 else "20") + s
    return datetime.strptime(s, "%Y%m%d%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()


def _subject_cn(der, i, end):
    """The common name in a Name (a SEQUENCE of SETs of type-value pairs), or ""."""
    while i < end:
        _, sc, sn, nxt = _der(der, i)  # a SET
        k = sc
        while k < sc + sn:
            _, ac, _, k2 = _der(der, k)  # a type-value pair
            _, oc, on, m = _der(der, ac)
            if der[oc:oc + on] == OID_CN:
                _, vc, vn, _ = _der(der, m)
                return der[vc:vc + vn].decode("utf-8", "replace")
            k = k2
        i = nxt
    return ""


def der_cert(der):
    """What the "self-signed" check needs from a certificate, read from its DER without the ssl
    module (an unverified connection only hands the raw certificate over): its validity, its common
    name, and the DNS names and IP addresses it lists as alternative names."""
    _, c, _, _ = _der(der, 0)  # Certificate
    _, c, n, _ = _der(der, c)  # tbsCertificate
    end, i = c + n, c
    tag, _, _, nxt = _der(der, i)
    if tag == 0xA0:  # [0] version
        i = nxt
    for _ in range(3):  # serialNumber, signature, issuer
        i = _der(der, i)[3]
    _, vc, _, i = _der(der, i)  # validity
    t1, c1, n1, j = _der(der, vc)
    t2, c2, n2, _ = _der(der, j)
    out = {"not_before": _time(t1, der[c1:c1 + n1]), "not_after": _time(t2, der[c2:c2 + n2]), "cn": "", "dns": [], "ips": []}
    _, sc, sn, i = _der(der, i)  # subject
    out["cn"] = _subject_cn(der, sc, sc + sn)
    i = _der(der, i)[3]  # subjectPublicKeyInfo
    while i < end:  # [1] / [2] unique ids, [3] extensions
        tag, ec, _, nxt = _der(der, i)
        if tag == 0xA3:
            _, lc, ln, _ = _der(der, ec)
            k = lc
            while k < lc + ln:
                _, xc, _, k2 = _der(der, k)  # Extension
                _, oc, on, m = _der(der, xc)
                oid = der[oc:oc + on]
                t, vc2, _, m2 = _der(der, m)
                if t == 0x01:  # critical
                    t, vc2, _, _ = _der(der, m2)
                if oid == OID_SAN and t == 0x04:
                    _, gc, gn, _ = _der(der, vc2)  # GeneralNames
                    g = gc
                    while g < gc + gn:
                        gt, nc, nn, g2 = _der(der, g)
                        if gt == 0x82:  # dNSName
                            out["dns"].append(der[nc:nc + nn].decode("ascii", "replace"))
                        elif gt == 0x87:  # iPAddress
                            out["ips"].append(ipaddress.ip_address(der[nc:nc + nn]).compressed)
                        g = g2
                k = k2
        i = nxt
    return out


def der_not_after(der):
    """A certificate's notAfter (unix s) from its DER."""
    return der_cert(der)["not_after"]


def name_matches(host, pattern):
    """Whether a certificate name covers host: exactly, or a "*." wildcard for one leftmost label."""
    host, pattern = host.lower().rstrip("."), pattern.lower().rstrip(".")
    if pattern.startswith("*."):
        first, _, rest = host.partition(".")
        return bool(first) and rest == pattern[2:]
    return host == pattern


def self_signed_problem(info, host, now):
    """Why the "self-signed" mode refuses a certificate, or "": it must be valid now and, for a URL by
    name, list that name (or, listing none, have it as its common name). A device reached by its
    address isn't name-checked: its own certificate names its hostname, rarely its address. Whether a
    CA signed it, and how it is marked (devices often don't mark their own certificate as a CA, which
    a strict TLS library refuses), doesn't matter here."""
    if now < info["not_before"]:
        return "the certificate is not valid yet"
    if now >= info["not_after"]:
        return "the certificate has expired"
    if is_ip(host):
        return ""
    names = info["dns"] or ([info["cn"]] if info["cn"] else [])
    if not any(name_matches(host, n) for n in names):
        return "the certificate is for another name" + (" (%s)" % ", ".join(names[:3]) if names else "")
    return ""


def verify_reason(e):
    """Why a certificate failed verification, in words."""
    msg = (getattr(e, "verify_message", "") or str(e)).lower()
    if "hostname mismatch" in msg or "ip address mismatch" in msg:
        return "the certificate is for another name"
    if "expired" in msg:
        return "the certificate has expired"
    if "self-signed" in msg or "self signed" in msg:
        return "the certificate is not trusted: self-signed certificate"
    if "invalid ca" in msg:
        return "the certificate is not trusted: it isn't from a known CA (a device's own certificate? use self-signed)"
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


def is_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def certificate(u, mode, timeout):
    """The URL's certificate: (days left or None, why it isn't accepted or "", an open connection to ask
    for the page over, or None). An untrusted certificate is read again without checking, for its
    expiry; in the "self-signed" mode it is then accepted if it is valid now and, for a URL by name, for
    that name. A refused one still hands back that unchecked connection, to time the page over."""
    strict, loose = contexts()
    try:
        conn, _ = connect(u, strict, timeout)
        not_after = ssl.cert_time_to_seconds(conn.sock.getpeercert()["notAfter"])
        return round((not_after - time.time()) / 86400, 4), "", conn
    except ssl.SSLCertVerificationError as e:
        reason = verify_reason(e)
    try:
        conn, _ = connect(u, loose, timeout)
        info = der_cert(conn.sock.getpeercert(binary_form=True))
        # Four decimals (about 9 s): the countdown draws as a smooth line, not a step every 2.4 hours.
        days = round((info["not_after"] - time.time()) / 86400, 4)
    except Exception:
        return None, reason, None
    if mode == "ignore":
        return days, reason, conn
    if mode == "self-signed":
        bad = self_signed_problem(info, u.hostname, time.time())
        if not bad:
            return days, "", conn
        reason = bad
    return days, reason, conn


def fetch(entry, codes, mode, timeout):
    """Check one URL: {id, up 1|0, status, time (None when it didn't answer), cert_days, error}. Its
    own certificate check, when it has one, wins over the host's mode. A refused certificate makes it
    down with that reason, but the page is still asked for, so its status and time are read."""
    mode = entry.mode or mode
    out = {"id": entry.id, "up": 0, "status": None, "time": None, "cert_days": None, "error": ""}
    start = time.monotonic()
    url, conn = entry.url, None
    u = urllib.parse.urlsplit(url)
    refused = ""  # why the URL's certificate isn't accepted ("" when it is, or isn't checked)
    try:
        if entry.tls and mode != "ignore":
            out["cert_days"], refused, conn = certificate(u, mode, timeout)
            if refused and conn is None:  # not even an unchecked connection: nothing to time
                out["error"] = refused
                return out
        strict, loose = contexts()
        for _ in range(MAX_REDIRECTS + 1):
            u = urllib.parse.urlsplit(url)
            if conn is None:
                # A later hop (a redirect) is checked like "verify"; "self-signed" and "ignore" take its
                # certificate as it is (the URL's own one was checked above), and so does every hop of
                # a URL whose own certificate was refused (it is down for that already).
                conn, _ = connect(u, strict if mode == "verify" and not refused else loose, timeout)
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
            if refused:
                out["error"] = refused
                return out
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
        out["error"] = refused or why(e, u.port or {"https": 443, "http": 80}[u.scheme], timeout)
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


def tls_discovery(entries, mode):
    """The URLs whose certificate is tracked: the https ones whose check (their own, else the host's
    mode) isn't "ignore"."""
    return [{"{#URLID}": e.id, "{#URLNAME}": e.name} for e in entries if e.tls and (e.mode or mode) != "ignore"]


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
    out["tls_discovery"] = tls_discovery(entries, mode)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
