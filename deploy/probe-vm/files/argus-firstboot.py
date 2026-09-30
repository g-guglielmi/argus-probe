#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi

"""Argus probe first-boot enrollment fallback.

Runs on first boot. If an enrollment token is already present (cloud-init seed), it just starts the
probe and exits. Otherwise it serves a small setup page on http://<vm>/ asking for the enroll URL +
token; on submit it writes probe.env, starts argus-probe.service, and then shows a LIVE status page
that polls the probe's real enrollment progress (reading the container's log + cert output) so you
see whether it actually enrolled or why it failed - not an optimistic "enrolling..." with no outcome.

Stdlib only. The setup page serves only until the probe is enrolled, then this service disables
itself so it never runs again.
"""
import hmac
import html
import json
import os
import re
import secrets
import socket
import string
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ENV_PATH = "/etc/argus-probe/probe.env"
ENROLL_DIR = "/docker/argus-probe/enroll"
CERT = os.path.join(ENROLL_DIR, "proxy.crt")
META = os.path.join(ENROLL_DIR, "proxy.env")
PROBE_SERVICE = "argus-probe.service"
UPDATER_SERVICE = "argus-updater.service"
FIRSTBOOT_SERVICE = "argus-firstboot.service"
LISTEN = ("0.0.0.0", 80)

# Argus seed CD (the "Download seed ISO" from Add probe): a plain ISO9660 with volume label ARGUSSEED
# holding a single KEY=VALUE file ARGUS.ENV. It is deliberately NOT a cloud-init NoCloud seed (that
# would need Joliet/Rock-Ridge to keep the user-data/meta-data names) - reading it ourselves sidesteps
# cloud-init's NoCloud datasource, which is fiddly on XCP-NG. Names are matched case-insensitively
# because plain ISO9660 may surface them uppercased and with a ";1" version suffix.
SEED_LABEL = "ARGUSSEED"
SEED_ENV = "argus.env"
SEED_MOUNT = "/run/argus-seed"

# Break-glass console access: a per-VM admin user with a generated password, reported to Argus at
# enrollment (stored encrypted there, revealed to admins) so an operator can reach the VM through the
# hypervisor console (or SSH over the VPN) if something goes wrong. The password is set on the local
# user and cached root-only so a failed report can retry without changing it.
BG_USER = "argus"
BG_SECRET_FILE = "/docker/argus-probe/break-glass.secret"
BG_DONE = "/docker/argus-probe/break-glass.reported"

# The setup page is reachable by anyone on the VM's network until the probe is enrolled, and what it
# collects decides which server this VM trusts. So a submission must carry a one-time SETUP CODE that
# only someone at the hypervisor console can read: it is printed there (and on the console login
# banner) when the page starts serving. Ten wrong codes replace it with a fresh one.
ISSUE_FILE = "/etc/issue.d/argus-setup.issue"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L: it's typed from a console
CODE_MAX_FAILURES = 10


def gen_setup_code():
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return raw[:4] + "-" + raw[4:]


def announce_code(code):
    """Show the setup code where only the console can see it: the login banner (/etc/issue.d, read
    by agetty at each prompt) and the console itself, right now."""
    ips = [ip for ip in sh("hostname", "-I").split() if not ip.startswith("127.")]
    where = ips[0] if ips else "<this VM's address>"
    text = ("\n  Argus probe setup: open http://%s/ in a browser on this network\n"
            "  and enter the setup code  %s\n\n" % (where, code))
    try:
        os.makedirs(os.path.dirname(ISSUE_FILE), exist_ok=True)
        with open(ISSUE_FILE, "w", encoding="utf-8") as fh:
            fh.write(text)
    except Exception:
        pass
    for dev in ("/dev/console", "/dev/tty1"):
        try:
            with open(dev, "w") as fh:
                fh.write(text)
        except Exception:
            pass
    print("argus-firstboot: setup code %s (shown on the console)" % code, flush=True)


def clear_code_banner():
    try:
        os.remove(ISSUE_FILE)
    except FileNotFoundError:
        pass
    except Exception:
        pass


# What the form (or a seed disk) may hand to the probe container. These values end up in an env file
# the container reads and in the address the proxy dials, so each has a shape it must fit.
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.:_\[\]-]{1,253}$")


def valid_enroll_url(url, allow_http=False):
    if not url or len(url) > 512 or re.search(r"[\s\x00-\x1f\x7f'\"\\]", url):
        return False
    u = urlparse(url)
    if u.scheme == "https" or (u.scheme == "http" and allow_http):
        return bool(u.netloc) and u.path.endswith("/api/enroll")
    return False


def valid_token(tok):
    return bool(tok) and bool(TOKEN_RE.fullmatch(tok))


def valid_host(h):
    return not h or bool(HOST_RE.fullmatch(h))


def read_kv(path):
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return out


def already_enrolled():
    return bool(read_kv(ENV_PATH).get("ARGUS_ENROLL_TOKEN"))


def find_seed_device():
    """Return the /dev path of an attached Argus seed disk (FS label ARGUSSEED), or None."""
    for line in sh("blkid").splitlines():
        m = re.match(r"^(\S+?):", line)
        lab = re.search(r'LABEL="([^"]*)"', line)
        if m and lab and lab.group(1).strip().upper() == SEED_LABEL:
            return m.group(1)
    return None


def read_seed_disk():
    """If an Argus seed CD/disk is attached, mount it read-only and read ARGUS.ENV. Returns the parsed
    KEY=VALUE dict (needs at least a token) or None. Best-effort - any failure just falls through to
    the setup page."""
    dev = find_seed_device()
    if not dev:
        return None
    os.makedirs(SEED_MOUNT, exist_ok=True)
    try:
        r = subprocess.run(["mount", "-o", "ro", dev, SEED_MOUNT], capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return None
        envfile = None
        for fn in os.listdir(SEED_MOUNT):
            if fn.split(";", 1)[0].lower() == SEED_ENV:  # tolerate ARGUS.ENV / argus.env / argus.env;1
                envfile = os.path.join(SEED_MOUNT, fn)
                break
        if not envfile:
            return None
        kv = read_kv(envfile)
        return kv if kv.get("ARGUS_ENROLL_TOKEN") else None
    except Exception:
        return None
    finally:
        subprocess.run(["umount", SEED_MOUNT], check=False)


def apply_keymap(km):
    """Set the console keyboard layout (the hypervisor console + break-glass login use it). Writes
    /etc/vconsole.conf and reloads systemd-vconsole-setup. Best-effort + validated - a bad/unknown
    layout just leaves the default (us) in place."""
    km = (km or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,15}", km):
        return
    try:
        with open("/etc/vconsole.conf", "w", encoding="utf-8") as fh:
            fh.write("KEYMAP=%s\n" % km)
        subprocess.run(["systemctl", "restart", "systemd-vconsole-setup.service"],
                       check=False, timeout=15, capture_output=True)
    except Exception:
        pass


def apply_static_net(env):
    """If the seed carried static networking (sites with no DHCP), replace the DHCP networkd file with
    a static one and re-apply, so the VM comes up on its fixed address and can enroll. Values are
    validated by the server that built the seed; omitted -> the VM keeps DHCP."""
    ip = (env.get("ARGUS_IP") or "").strip()  # CIDR, e.g. 10.0.0.50/24
    if not ip:
        return
    lines = ["[Match]", "Name=en* eth*", "", "[Network]", "Address=%s" % ip]
    gw = (env.get("ARGUS_GATEWAY") or "").strip()
    if gw:
        lines.append("Gateway=%s" % gw)
    for d in re.split(r"[,\s]+", (env.get("ARGUS_DNS") or "").strip()):
        if d:
            lines.append("DNS=%s" % d)
    try:
        with open("/etc/systemd/network/10-argus-static.network", "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        # drop the DHCP file so networkd doesn't also DHCP the same NIC
        try:
            os.remove("/etc/systemd/network/10-argus-dhcp.network")
        except FileNotFoundError:
            pass
        subprocess.run(["systemctl", "restart", "systemd-networkd.service"],
                       check=False, timeout=20, capture_output=True)
        print("argus-firstboot: applied static network %s" % ip)
    except Exception:
        pass


def apply_hostname():
    """Set the VM hostname to the enrolled proxy name (matching the container's Zabbix hostname), once
    enrollment has written PROXY_NAME to proxy.env. Idempotent + best-effort."""
    name = re.sub(r"[^a-z0-9-]", "-", read_kv(META).get("PROXY_NAME", "").strip().lower()).strip("-")
    if not name:
        return
    # The VM is the probe appliance (the container it runs is the Zabbix proxy), so name the host
    # argus-probe-<site>: proxy-site5 -> argus-probe-site5.
    site = name[len("proxy-"):] if name.startswith("proxy-") else name
    name = "argus-probe-" + site
    try:
        current = open("/etc/hostname", encoding="utf-8").read().strip()
    except Exception:
        current = ""
    if current == name:
        return
    subprocess.run(["hostnamectl", "set-hostname", name], check=False, timeout=10)
    print("argus-firstboot: hostname set to %s" % name)


def gen_password(n=20):
    alphabet = string.ascii_letters + string.digits  # unambiguous + easy to type at a console
    return "".join(secrets.choice(alphabet) for _ in range(n))


def ensure_break_glass():
    """Once the probe is enrolled: create the break-glass admin user with a generated password and
    report it to Argus using the probe's check-in credential. Idempotent - the password is cached
    root-only and the report is retried on later boots until it succeeds (BG_DONE marks success)."""
    if os.path.exists(BG_DONE):
        return
    pw = ""
    if os.path.exists(BG_SECRET_FILE):
        try:
            pw = open(BG_SECRET_FILE, encoding="utf-8").read().strip()
        except Exception:
            pw = ""
    if not pw:
        pw = gen_password()
        # sudo is the whole privilege; a docker-group membership would be a second, unlogged root.
        subprocess.run(["useradd", "-m", "-s", "/bin/bash", "-G", "sudo", BG_USER], check=False)
        subprocess.run(["chpasswd"], input="%s:%s" % (BG_USER, pw), text=True, check=False)
        old = os.umask(0o077)
        try:
            with open(BG_SECRET_FILE, "w", encoding="utf-8") as fh:
                fh.write(pw + "\n")
        finally:
            os.umask(old)
    # Report it to Argus with the probe token from proxy.env (same credential the sidecar checks in
    # with). checkin URL .../api/probes/checkin -> the break-glass endpoint .../api/probes/break-glass.
    env = read_kv(META)
    token, checkin = env.get("PROBE_TOKEN", ""), env.get("CHECKIN_URL", "")
    if not token or not checkin:
        return  # not reportable yet - retry on the next boot
    url = checkin.rsplit("/", 1)[0] + "/break-glass"
    body = json.dumps({"username": BG_USER, "password": pw}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Authorization": "Bearer " + token,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                open(BG_DONE, "w", encoding="utf-8").close()
                print("argus-firstboot: break-glass credential reported to Argus")
    except Exception:
        pass  # retry next boot


def write_env(enroll_url, enroll_token, core_host, insecure=False):
    lines = [f"ARGUS_ENROLL_URL={enroll_url}", f"ARGUS_ENROLL_TOKEN={enroll_token}"]
    if core_host:
        lines.append(f"ZBX_SERVER_HOST={core_host}")
    if insecure:
        # A plain-http Argus (a lab): the container refuses http check-in unless told so.
        lines.append("ARGUS_ALLOW_INSECURE_CHECKIN=true")
    os.makedirs("/etc/argus-probe", exist_ok=True)
    tmp = ENV_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, ENV_PATH)


def sh(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""


def start_probe():
    # restart (not just start) so a corrected probe.env is picked up on a retry. The two containers -
    # the proxy and the argus-updater sidecar - come up together (the updater keeps the proxy on the
    # Argus fleet target and can update itself).
    for svc in (PROBE_SERVICE, UPDATER_SERVICE):
        subprocess.run(["systemctl", "enable", svc], check=False)
        subprocess.run(["systemctl", "restart", svc], check=False)


def enroll_status(since=None):
    """Derive enrollment progress from the probe container's log + cert output.

    Returns {state, detail?, name?} where state is one of:
      starting | enrolling | enrolled | failed
    `since` (epoch) scopes the log to the current attempt, so a stale failure from a previous try
    doesn't mask a fresh retry.
    """
    # Success is authoritative: the container writes proxy.crt + proxy.env once enrolled.
    if os.path.exists(CERT):
        return {"state": "enrolled", "name": read_kv(META).get("PROXY_NAME", "")}

    args = ["journalctl", "-u", PROBE_SERVICE, "-n", "200", "--no-pager", "-o", "cat"]
    if since:
        args += ["--since", "@%d" % int(since)]
    log = sh(*args)
    m = re.search(r"enrolled as (\S+)", log)
    if m:
        return {"state": "enrolled", "name": m.group(1)}
    m = re.search(r"enrollment failed \(([^)]*)\):?\s*(.*)", log)
    if m:
        detail = (m.group(2) or m.group(1)).strip()
        return {"state": "failed", "detail": detail[:300] or "enrollment was rejected"}

    active = sh("systemctl", "is-active", PROBE_SERVICE).strip()
    if active == "failed":
        return {"state": "failed", "detail": "the probe service failed to start - check the console"}
    if "enrolling against" in log or active in ("active", "activating"):
        return {"state": "enrolling"}
    return {"state": "starting"}


# The same design tokens as the Argus UI (theme.css): light by default, dark when the device asks for it.
STYLE = """
  :root { color-scheme: light dark;
    --bg: #eaeef4; --card: #ffffff; --border: #dbe2ec; --text: #141d28; --muted: #4f5b69; --faint: #647082;
    --field: #f4f7fb; --accent: #2ea8c9; --ok: #3fa66a; --err: #e2564d; --shadow: 0 1px 2px rgba(20,30,45,.06), 0 6px 20px rgba(20,30,45,.06); }
  @media (prefers-color-scheme: dark) { :root {
    --bg: #0e1218; --card: #151b23; --border: #262f3b; --text: #eef2f8; --muted: #b3bfcd; --faint: #8b98a8;
    --field: #0e1218; --shadow: 0 12px 40px rgba(0,0,0,.4); } }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 1rem;
    background: var(--bg); color: var(--text); font: 15px/1.5 system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }
  .card { width: min(30rem, 100%); background: var(--card); border: 1px solid var(--border); border-radius: 14px;
    padding: 1.6rem 1.6rem 1.4rem; box-shadow: var(--shadow); }
  .brand { display: flex; align-items: center; gap: .6rem; font-weight: 700; letter-spacing: .12em; text-transform: uppercase;
    font-size: .95rem; margin-bottom: 1.1rem; }
  .brand svg { width: 26px; height: 26px; color: var(--accent); flex: none; }
  .brand small { display: block; font-size: .62rem; letter-spacing: .14em; color: var(--faint); font-weight: 600; margin-top: 1px; }
  h1 { font-size: 1.15rem; margin: 0 0 .4rem; }
  p.hint { color: var(--muted); font-size: .88rem; margin: 0 0 1.2rem; }
  label { display: block; font-weight: 600; font-size: .82rem; margin: 1rem 0 .3rem; color: var(--muted); }
  input, select { width: 100%; padding: .6rem .7rem; font-size: 1rem; color: var(--text); background: var(--field);
    border: 1px solid var(--border); border-radius: 8px; }
  input:focus, select:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(46,168,201,.2); }
  .sub { color: var(--faint); font-size: .78rem; margin-top: .3rem; }
  button { margin-top: 1.5rem; width: 100%; padding: .75rem 1rem; font-size: .95rem; font-weight: 600;
    color: #fff; background: var(--accent); border: none; border-radius: 8px; cursor: pointer; }
  button:hover { filter: brightness(1.05); }
  .err { color: var(--err); font-size: .85rem; margin: .8rem 0 0; }
  /* status page */
  .steps { list-style: none; padding: 0; margin: 1.2rem 0 0; }
  .steps li { display: flex; align-items: center; gap: .6rem; padding: .35rem 0; color: var(--faint); font-size: .92rem; }
  .steps li.done { color: var(--ok); }
  .steps li.active { color: var(--text); }
  .steps li.fail { color: var(--err); }
  .ic { width: 18px; text-align: center; flex: none; }
  .result { margin-top: 1.2rem; font-weight: 600; }
  .result.ok { color: var(--ok); }
  .result.bad { color: var(--err); }
  a.retry { color: var(--accent); }
  /* which VM this is (hostname · address), so the page is unambiguous with several probes on the bench */
  .vm { margin-top: 1.4rem; padding-top: .8rem; border-top: 1px solid var(--border); font-size: .78rem; color: var(--faint); }
.vm a { color: inherit; text-decoration: underline; }
.vm .src { margin-top: .4rem; }
  .vm b { color: var(--muted); font-weight: 600; }
"""

# The Argus "probe" mark (the same radar glyph as the Probes tab in the UI), inline so the page needs no
# extra request.
LOGO = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">'
        '<circle cx="12" cy="12" r="2"/><path d="M16.2 7.8a6 6 0 0 1 0 8.4M7.8 16.2a6 6 0 0 1 0-8.4M19 5a10 10 0 0 1 0 14M5 19A10 10 0 0 1 5 5"/></svg>')


def vm_identity():
    """'hostname · ip [· ip]' for the page footer - which VM am I looking at. Best-effort."""
    try:
        name = socket.gethostname()
    except Exception:
        name = ""
    ips = [ip for ip in sh("hostname", "-I").split() if not ip.startswith("127.")]
    parts = [html.escape(p) for p in ([name] if name else []) + ips[:2]]
    return " · ".join(parts)


def page(body, head_extra=""):
    ident = vm_identity()
    # AGPL-3.0 §13: this network-served UI must let its users reach the corresponding source.
    vm_line = f"<div>This VM: <b>{ident}</b></div>" if ident else ""
    src_line = "<div class=src>Argus probe - free software under the " \
               "<a href='https://www.gnu.org/licenses/agpl-3.0.html'>AGPL-3.0</a>. " \
               "<a href='https://github.com/g-guglielmi/argus-probe'>Source code</a>.</div>"
    foot = f"<div class=vm>{vm_line}{src_line}</div>"
    return f"<!doctype html><html lang=en><head><meta charset=utf-8>" \
           f"<meta name=viewport content='width=device-width, initial-scale=1'>" \
           f"<meta name=color-scheme content='light dark'>{head_extra}" \
           f"<title>Argus probe setup</title><style>{STYLE}</style></head><body>" \
           f"<div class=card><div class=brand>{LOGO}<div>Argus<small>Probe setup</small></div></div>{body}{foot}</div></body></html>"


FORM = """
  <h1>Set up this probe</h1>
  <p class="hint">Paste the enrollment URL and token from the Argus <strong>Add probe</strong> wizard
  (the token is shown once). The probe enrols itself and starts monitoring.</p>
  {error}
  <form method="post">
    <input type="hidden" name="csrf" value="{csrf}">
    <label for="s">Setup code</label>
    <input id="s" name="setup_code" placeholder="XXXX-XXXX" autocomplete="off" required>
    <div class="sub">Shown on this VM's console (the hypervisor's console window), so only someone who can see it can enrol this probe.</div>
    <label for="u">Enrollment URL</label>
    <input id="u" name="enroll_url" placeholder="https://monitoring.example.com/api/enroll" value="{url}" required>
    <label for="t">Enrollment token</label>
    <input id="t" name="enroll_token" placeholder="the single-use token" autocomplete="off" required>
    <label for="c">Core host <span style="color:var(--faint);font-weight:400">(optional)</span></label>
    <input id="c" name="core_host" placeholder="usually leave blank" value="{core}">
    <div class="sub">Leave blank - Argus fills this in. Only set it if the probe can't reach the server after enrolling.</div>
    <label style="display:flex;gap:.5rem;align-items:center;font-weight:400"><input type="checkbox" name="insecure" value="1" style="width:auto" {insecure}> This Argus has no HTTPS (lab only)</label>
    <div class="sub">Allows an http:// enrollment URL. The probe's token then travels in clear; never for a production core.</div>
    <label for="k">Console keyboard layout</label>
    <select id="k" name="keymap">
      <option value="us">US English</option>
      <option value="uk">UK English</option>
      <option value="it">Italian</option>
      <option value="de">German</option>
      <option value="fr">French</option>
      <option value="es">Spanish</option>
      <option value="pt-latin1">Portuguese</option>
    </select>
    <div class="sub">For this VM's console and the break-glass admin login.</div>
    <button type="submit">Enrol probe</button>
  </form>
"""

# The progress page is rendered with the CURRENT state server-side (so it is right without JavaScript - a
# <noscript> meta-refresh reloads it every 3s), and the script then polls /status for live updates.
STEP_LABELS = [("starting", "Starting the probe"), ("enrolling", "Generating key &amp; redeeming the token"), ("enrolled", "Registered with Argus")]
NOSCRIPT_REFRESH = "<noscript><meta http-equiv=refresh content=3></noscript>"


def render_steps(status):
    """Server-side rendering of the step list + result line for an enroll_status() dict."""
    order = [k for k, _ in STEP_LABELS]
    state = status.get("state", "starting")
    idx = order.index(state) if state in order else -1
    items = []
    for i, (k, label) in enumerate(STEP_LABELS):
        cls, ic = "", "•"
        if state == "enrolled" or i < idx:
            cls, ic = "done", "✓"
        elif state == "failed":
            cls, ic = ("done", "✓") if i == 0 else ("fail", "✕")
        elif i == idx:
            cls, ic = "active", "…"
        items.append(f'<li data-k="{k}" class="{cls}"><span class="ic">{ic}</span> {label}</li>')
    result = ""
    if state == "failed":
        result = ('<div class="result bad" id="result">Enrollment failed: ' + html.escape(status.get("detail") or "unknown error")
                  + '<br><a class="retry" href="/?edit=1">Change the URL or token and try again</a></div>')
    elif state == "enrolled":
        who = (" as " + html.escape(status["name"])) if status.get("name") else ""
        result = f'<div class="result ok" id="result">✓ Enrolled{who} - it will appear on the Probes page shortly. You can close this page.</div>'
    else:
        result = '<div class="result" id="result"></div>'
    return "\n".join(items), result


def progress_page(status):
    steps, result = render_steps(status)
    body = PROGRESS.replace("%%STEPS%%", steps).replace("%%RESULT%%", result)
    return page(body, head_extra=NOSCRIPT_REFRESH)


PROGRESS = """
  <h1>Enrolling this probe…</h1>
  <p class="hint">Registering with Argus. This usually takes a few seconds.</p>
  <ul class="steps" id="steps">
%%STEPS%%
  </ul>
%%RESULT%%
  <script>
    const ORDER = ["starting", "enrolling", "enrolled"];
    async function poll() {
      let s;
      try { s = await (await fetch("/status")).json(); } catch (e) { setTimeout(poll, 2000); return; }
      const steps = [...document.querySelectorAll("#steps li")];
      const res = document.getElementById("result");
      if (s.state === "failed") {
        steps.forEach(li => { li.classList.remove("active"); if (!li.classList.contains("done")) { li.classList.add("fail"); li.querySelector(".ic").textContent = "✕"; } });
        res.className = "result bad";
        res.innerHTML = "Enrollment failed: " + (s.detail || "unknown error") + '<br><a class="retry" href="/?edit=1">Change the URL or token and try again</a>';
        return;
      }
      const idx = ORDER.indexOf(s.state);
      steps.forEach((li, i) => {
        li.classList.remove("active", "fail");
        if (i < idx || s.state === "enrolled") { li.classList.add("done"); li.querySelector(".ic").textContent = "✓"; }
        else if (i === idx) { li.classList.add("active"); li.querySelector(".ic").textContent = "…"; }
      });
      if (s.state === "enrolled") {
        res.className = "result ok";
        res.textContent = "✓ Enrolled" + (s.name ? " as " + s.name : "") + " - it will appear on the Probes page shortly. You can close this page.";
        return;
      }
      setTimeout(poll, 1500);
    }
    poll();
  </script>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, status=200, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def _form(self, error="", env=None):
        # The stored token is never shown again; the URL and core host are, so a typo is easy to fix.
        env = env or {}
        return page(FORM.format(error=error, csrf=html.escape(self.server.csrf),
                                url=html.escape(env.get("ARGUS_ENROLL_URL", "")),
                                core=html.escape(env.get("ZBX_SERVER_HOST", "")),
                                insecure="checked" if env.get("ARGUS_ALLOW_INSECURE_CHECKIN") == "true" else ""))

    def do_GET(self):
        if self.path.startswith("/status"):
            self._send(json.dumps(enroll_status(self.server.attempt_since)), ctype="application/json")
            return
        if self.path.startswith("/?edit"):
            # After a failure: reopen the form prefilled with the URL and core host (not the token) so
            # only the wrong field needs fixing. The running attempt is replaced when a valid
            # submission arrives, not by opening this page.
            self.server.submitted = False
            self._send(self._form(env=read_kv(ENV_PATH)))
            return
        if self.server.submitted or already_enrolled():
            self._send(progress_page(enroll_status(self.server.attempt_since)))
            return
        self._send(self._form())

    def _code_ok(self, form):
        """The setup code check: constant-time, counted, replaced after too many misses."""
        given = (form.get("setup_code", [""])[0] or "").strip().upper().replace(" ", "")
        want = self.server.setup_code
        if hmac.compare_digest(given, want) or hmac.compare_digest(given, want.replace("-", "")):
            self.server.code_failures = 0
            return True
        self.server.code_failures += 1
        time.sleep(1)
        if self.server.code_failures >= CODE_MAX_FAILURES:
            self.server.setup_code = gen_setup_code()
            self.server.code_failures = 0
            announce_code(self.server.setup_code)
        return False

    def do_POST(self):
        length = min(int(self.headers.get("Content-Length", 0) or 0), 16384)
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        url = form.get("enroll_url", [""])[0].strip()
        token = form.get("enroll_token", [""])[0].strip()
        core = form.get("core_host", [""])[0].strip()
        insecure = form.get("insecure", [""])[0] == "1"
        keep = {"ARGUS_ENROLL_URL": url, "ZBX_SERVER_HOST": core, "ARGUS_ALLOW_INSECURE_CHECKIN": "true" if insecure else ""}

        def bad(msg, status=400):
            self._send(self._form(error='<p class="err">%s</p>' % html.escape(msg), env=keep), status=status)

        if not hmac.compare_digest(form.get("csrf", [""])[0], self.server.csrf):
            bad("This form is stale; reload the page and try again.")
            return
        if not self._code_ok(form):
            bad("That setup code isn't right. It's shown on this VM's console.", status=403)
            return
        if not url or not token:
            bad("Enrollment URL and token are both required.")
            return
        if not valid_enroll_url(url, allow_http=insecure):
            bad("The enrollment URL must be https://<argus>/api/enroll (tick the lab option for plain http).")
            return
        if not valid_token(token):
            bad("That doesn't look like an enrollment token.")
            return
        if not valid_host(core):
            bad("Core host must be a host name or address, optionally with :port.")
            return
        write_env(url, token, core, insecure=insecure)
        apply_keymap(form.get("keymap", ["us"])[0])
        self.server.attempt_since = time.time()  # scope status polling to this attempt
        start_probe()
        self.server.submitted = True
        self._send(progress_page({"state": "starting"}))

    def log_message(self, *args):
        pass


def monitor(httpd):
    """Once the probe is enrolled: generate + report the break-glass credential, keep serving briefly
    (so the page shows success), then stop. The service is only disabled once the credential has been
    reported (BG_DONE) - otherwise it stays enabled so a later boot retries the report."""
    while True:
        time.sleep(3)
        if os.path.exists(CERT):
            clear_code_banner()
            apply_hostname()
            ensure_break_glass()
            time.sleep(15)
            if os.path.exists(BG_DONE):
                subprocess.run(["systemctl", "disable", FIRSTBOOT_SERVICE], check=False)
            httpd.shutdown()
            return


def main():
    if already_enrolled() and os.path.exists(CERT):
        start_probe()
        apply_hostname()
        ensure_break_glass()  # retry the report if a prior boot couldn't reach Argus
        if os.path.exists(BG_DONE):
            subprocess.run(["systemctl", "disable", FIRSTBOOT_SERVICE], check=False)
        return 0
    # Zero-touch via an attached seed CD (no cloud-init needed): if one is present and no token has
    # been written yet, adopt its enrollment inputs (and keyboard layout) so this boot enrolls itself.
    if not already_enrolled():
        seed = read_seed_disk()
        if seed and valid_enroll_url(seed.get("ARGUS_ENROLL_URL", ""), allow_http=True) \
                and valid_token(seed.get("ARGUS_ENROLL_TOKEN", "")) and valid_host(seed.get("ZBX_SERVER_HOST", "")):
            write_env(seed["ARGUS_ENROLL_URL"], seed["ARGUS_ENROLL_TOKEN"], seed.get("ZBX_SERVER_HOST", ""),
                      insecure=seed["ARGUS_ENROLL_URL"].startswith("http://"))
            apply_keymap(seed.get("ARGUS_KEYMAP", ""))
            apply_static_net(seed)  # no-op unless the seed carried a static IP (no-DHCP sites)
            print("argus-firstboot: adopted enrollment inputs from the attached seed disk")
    httpd = ThreadingHTTPServer(LISTEN, Handler)
    httpd.attempt_since = time.time()
    httpd.submitted = already_enrolled()  # a seed may have written the token already
    httpd.csrf = secrets.token_urlsafe(24)
    httpd.setup_code = gen_setup_code()
    httpd.code_failures = 0
    if httpd.submitted:
        start_probe()
    else:
        announce_code(httpd.setup_code)
    threading.Thread(target=monitor, args=(httpd,), daemon=True).start()
    print(f"argus-firstboot: serving setup page on http://{LISTEN[0]}:{LISTEN[1]}/")
    httpd.serve_forever()
    clear_code_banner()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
