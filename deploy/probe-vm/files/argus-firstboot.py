#!/usr/bin/env python3
"""Argus probe first-boot enrollment fallback.

Runs on first boot. If an enrollment token is already present (cloud-init seed), it just starts the
probe and exits. Otherwise it serves a small setup page on http://<vm>/ asking for the enroll URL +
token; on submit it writes probe.env, starts argus-probe.service, and then shows a LIVE status page
that polls the probe's real enrollment progress (reading the container's log + cert output) so you
see whether it actually enrolled or why it failed - not an optimistic "enrolling..." with no outcome.

Stdlib only. The setup page serves only until the probe is enrolled, then this service disables
itself so it never runs again.
"""
import html
import json
import os
import re
import secrets
import string
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

ENV_PATH = "/etc/argus-probe/probe.env"
ENROLL_DIR = "/var/lib/argus-probe/enroll"
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
BG_SECRET_FILE = "/var/lib/argus-probe/break-glass.secret"
BG_DONE = "/var/lib/argus-probe/break-glass.reported"


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
    name = "argus-" + name  # e.g. proxy-office -> argus-proxy-office
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
        subprocess.run(["useradd", "-m", "-s", "/bin/bash", "-G", "sudo", BG_USER], check=False)
        # Also add it to the docker group so break-glass can run docker without sudo (best-effort - the
        # group exists once Docker is installed). This grants no privilege it doesn't already have via sudo.
        subprocess.run(["usermod", "-aG", "docker", BG_USER], check=False)
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


def write_env(enroll_url, enroll_token, core_host):
    lines = [f"ARGUS_ENROLL_URL={enroll_url}", f"ARGUS_ENROLL_TOKEN={enroll_token}"]
    if core_host:
        lines.append(f"ZBX_SERVER_HOST={core_host}")
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


STYLE = """
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: #0e1217; color: #e6e9ef; font: 15px/1.5 system-ui, -apple-system, Segoe UI, sans-serif; }
  .card { width: min(30rem, 92vw); background: #161b22; border: 1px solid #232a33; border-radius: 14px;
    padding: 1.75rem 1.75rem 2rem; box-shadow: 0 12px 40px rgba(0,0,0,.4); }
  .brand { display: flex; align-items: center; gap: .55rem; font-weight: 700; letter-spacing: -.01em;
    font-size: 1.15rem; margin-bottom: 1.1rem; }
  .brand .dot { width: 12px; height: 12px; border-radius: 50%; background: #2ea8c9;
    box-shadow: 0 0 0 4px rgba(46,168,201,.18); }
  h1 { font-size: 1.15rem; margin: 0 0 .4rem; }
  p.hint { color: #9aa4b2; font-size: .88rem; margin: 0 0 1.2rem; }
  label { display: block; font-weight: 600; font-size: .82rem; margin: 1rem 0 .3rem; color: #c4ccd6; }
  input { width: 100%; padding: .6rem .7rem; font-size: .95rem; color: #e6e9ef; background: #0e1217;
    border: 1px solid #2b333d; border-radius: 8px; }
  input:focus { outline: none; border-color: #2ea8c9; box-shadow: 0 0 0 3px rgba(46,168,201,.2); }
  select { width: 100%; padding: .6rem .7rem; font-size: .95rem; color: #e6e9ef; background: #0e1217;
    border: 1px solid #2b333d; border-radius: 8px; }
  .sub { color: #6b7482; font-size: .78rem; margin-top: .3rem; }
  button { margin-top: 1.5rem; width: 100%; padding: .7rem 1rem; font-size: .95rem; font-weight: 600;
    color: #04121a; background: #2ea8c9; border: none; border-radius: 8px; cursor: pointer; }
  button:hover { background: #35b7da; }
  .err { color: #e2564d; font-size: .85rem; margin: .8rem 0 0; }
  /* status page */
  .steps { list-style: none; padding: 0; margin: 1.2rem 0 0; }
  .steps li { display: flex; align-items: center; gap: .6rem; padding: .35rem 0; color: #6b7482; font-size: .92rem; }
  .steps li.done { color: #3fa66a; }
  .steps li.active { color: #e6e9ef; }
  .steps li.fail { color: #e2564d; }
  .ic { width: 18px; text-align: center; flex: none; }
  .result { margin-top: 1.2rem; font-weight: 600; }
  .result.ok { color: #3fa66a; }
  .result.bad { color: #e2564d; }
  a.retry { color: #2ea8c9; }
"""


def page(body):
    return f"<!doctype html><html lang=en><head><meta charset=utf-8>" \
           f"<meta name=viewport content='width=device-width, initial-scale=1'>" \
           f"<title>Argus probe setup</title><style>{STYLE}</style></head><body>" \
           f"<div class=card><div class=brand><span class=dot></span>Argus probe</div>{body}</div></body></html>"


FORM = """
  <h1>Set up this probe</h1>
  <p class="hint">Paste the enrollment URL and token from the Argus <strong>Add probe</strong> wizard
  (the token is shown once). The probe enrols itself and starts monitoring.</p>
  {error}
  <form method="post">
    <label for="u">Enrollment URL</label>
    <input id="u" name="enroll_url" placeholder="https://monitoring.example.com/api/enroll" value="{url}" required>
    <label for="t">Enrollment token</label>
    <input id="t" name="enroll_token" placeholder="the single-use token" value="{token}" required>
    <label for="c">Core host <span style="color:#6b7482;font-weight:400">(optional)</span></label>
    <input id="c" name="core_host" placeholder="usually leave blank" value="{core}">
    <div class="sub">Leave blank — Argus fills this in. Only set it if the probe can't reach the server after enrolling.</div>
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

PROGRESS = """
  <h1>Enrolling this probe…</h1>
  <p class="hint">Registering with Argus. This usually takes a few seconds.</p>
  <ul class="steps" id="steps">
    <li data-k="starting"><span class="ic">•</span> Starting the probe</li>
    <li data-k="enrolling"><span class="ic">•</span> Generating key &amp; redeeming the token</li>
    <li data-k="enrolled"><span class="ic">•</span> Registered with Argus</li>
  </ul>
  <div class="result" id="result"></div>
  <script>
    const ORDER = ["starting", "enrolling", "enrolled"];
    async function poll() {
      let s;
      try { s = await (await fetch("/status")).json(); } catch (e) { setTimeout(poll, 2000); return; }
      const steps = [...document.querySelectorAll("#steps li")];
      const res = document.getElementById("result");
      if (s.state === "failed") {
        steps.forEach(li => { if (!li.classList.contains("done")) { li.classList.add("fail"); li.querySelector(".ic").textContent = "✕"; } });
        res.className = "result bad";
        res.innerHTML = "Enrollment failed: " + (s.detail || "unknown error") + '<br><a class="retry" href="/?edit=1">Change the URL or token and try again</a>';
        return;
      }
      const idx = ORDER.indexOf(s.state);
      steps.forEach((li, i) => {
        li.classList.remove("active");
        if (i < idx || s.state === "enrolled") { li.classList.add("done"); li.querySelector(".ic").textContent = "✓"; }
        else if (i === idx) { li.classList.add("active"); li.querySelector(".ic").textContent = "…"; }
      });
      if (s.state === "enrolled") {
        res.className = "result ok";
        res.textContent = "✓ Enrolled" + (s.name ? " as " + s.name : "") + " — it will appear on the Probes page shortly. You can close this page.";
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
        self.end_headers()
        self.wfile.write(data)

    def _form(self, error="", env=None):
        env = env or {}
        return page(FORM.format(error=error,
                                url=html.escape(env.get("ARGUS_ENROLL_URL", "")),
                                token=html.escape(env.get("ARGUS_ENROLL_TOKEN", "")),
                                core=html.escape(env.get("ZBX_SERVER_HOST", ""))))

    def do_GET(self):
        if self.path.startswith("/status"):
            self._send(json.dumps(enroll_status(self.server.attempt_since)), ctype="application/json")
            return
        if self.path.startswith("/?edit"):
            # After a failure: stop the probe (and its updater) retrying the bad values and reopen the
            # form, prefilled with what was entered so only the wrong field needs fixing.
            subprocess.run(["systemctl", "stop", UPDATER_SERVICE], check=False)
            subprocess.run(["systemctl", "stop", PROBE_SERVICE], check=False)
            self.server.submitted = False
            self._send(self._form(env=read_kv(ENV_PATH)))
            return
        if self.server.submitted or already_enrolled():
            self._send(page(PROGRESS))
            return
        self._send(self._form())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
        url = form.get("enroll_url", [""])[0].strip()
        token = form.get("enroll_token", [""])[0].strip()
        core = form.get("core_host", [""])[0].strip()
        if not url or not token:
            err = '<p class="err">Enrollment URL and token are both required.</p>'
            self._send(self._form(error=err, env={"ARGUS_ENROLL_URL": url, "ARGUS_ENROLL_TOKEN": token,
                                                  "ZBX_SERVER_HOST": core}), status=400)
            return
        write_env(url, token, core)
        apply_keymap(form.get("keymap", ["us"])[0])
        self.server.attempt_since = time.time()  # scope status polling to this attempt
        start_probe()
        self.server.submitted = True
        self._send(page(PROGRESS))

    def log_message(self, *args):
        pass


def monitor(httpd):
    """Once the probe is enrolled: generate + report the break-glass credential, keep serving briefly
    (so the page shows success), then stop. The service is only disabled once the credential has been
    reported (BG_DONE) - otherwise it stays enabled so a later boot retries the report."""
    while True:
        time.sleep(3)
        if os.path.exists(CERT):
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
        if seed and seed.get("ARGUS_ENROLL_URL") and seed.get("ARGUS_ENROLL_TOKEN"):
            write_env(seed["ARGUS_ENROLL_URL"], seed["ARGUS_ENROLL_TOKEN"], seed.get("ZBX_SERVER_HOST", ""))
            apply_keymap(seed.get("ARGUS_KEYMAP", ""))
            apply_static_net(seed)  # no-op unless the seed carried a static IP (no-DHCP sites)
            print("argus-firstboot: adopted enrollment inputs from the attached seed disk")
    httpd = ThreadingHTTPServer(LISTEN, Handler)
    httpd.attempt_since = time.time()
    httpd.submitted = already_enrolled()  # a seed may have written the token already
    if httpd.submitted:
        start_probe()
    threading.Thread(target=monitor, args=(httpd,), daemon=True).start()
    print(f"argus-firstboot: serving setup page on http://{LISTEN[0]}:{LISTEN[1]}/")
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
