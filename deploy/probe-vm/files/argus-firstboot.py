#!/usr/bin/env python3
"""Argus probe first-boot enrollment fallback.

Runs once on first boot (after cloud-init). If cloud-init already wrote an enrollment token to
/etc/argus-probe/probe.env, this simply starts the probe and exits. Otherwise - e.g. a hypervisor
where the cloud-init datasource didn't attach - it serves a single-page setup form on http://<vm>/
asking for the enroll URL + token (copy them from the Argus "Add probe" wizard). On submit it writes
probe.env, starts the probe service, disables itself, and exits.

Stdlib only (cloud-init already pulls in python3), no external deps. The setup page is reachable by
anyone on the VM's network, so it serves only until the probe is enrolled - exactly like an
appliance's first-boot setup screen.
"""
import html
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

ENV_PATH = "/etc/argus-probe/probe.env"
PROBE_SERVICE = "argus-probe.service"
FIRSTBOOT_SERVICE = "argus-firstboot.service"
LISTEN = ("0.0.0.0", 80)


def read_env():
    """Parse KEY=VALUE lines from probe.env into a dict (missing file -> empty)."""
    out = {}
    try:
        with open(ENV_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return out


def already_enrolled():
    return bool(read_env().get("ARGUS_ENROLL_TOKEN"))


def write_env(enroll_url, enroll_token, core_host):
    lines = [
        f"ARGUS_ENROLL_URL={enroll_url}",
        f"ARGUS_ENROLL_TOKEN={enroll_token}",
    ]
    if core_host:
        lines.append(f"ZBX_SERVER_HOST={core_host}")
    import os
    os.makedirs("/etc/argus-probe", exist_ok=True)
    tmp = ENV_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, ENV_PATH)


def start_probe():
    """Enable + start the probe, and stop this fallback from running again."""
    subprocess.run(["systemctl", "enable", "--now", PROBE_SERVICE], check=False)
    subprocess.run(["systemctl", "disable", FIRSTBOOT_SERVICE], check=False)


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Argus probe setup</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, sans-serif; max-width: 30rem; margin: 6vh auto; padding: 0 1rem; }}
  h1 {{ font-size: 1.3rem; }}
  label {{ display: block; margin: 1rem 0 0.25rem; font-weight: 600; font-size: 0.9rem; }}
  input {{ width: 100%; padding: 0.5rem; font-size: 1rem; box-sizing: border-box; }}
  button {{ margin-top: 1.5rem; padding: 0.6rem 1.2rem; font-size: 1rem; cursor: pointer; }}
  p.hint {{ color: #666; font-size: 0.85rem; }}
  .err {{ color: #c0392b; }}
</style></head>
<body>
  <h1>Set up this Argus probe</h1>
  <p class="hint">Paste the enrollment URL and token from the Argus <strong>Add probe</strong> wizard
  (the token is shown once). The probe self-enrolls and starts monitoring; this page then disappears.</p>
  {error}
  <form method="post">
    <label for="u">Enrollment URL</label>
    <input id="u" name="enroll_url" placeholder="https://monitoring.example.com/api/enroll" value="{url}" required>
    <label for="t">Enrollment token</label>
    <input id="t" name="enroll_token" placeholder="the single-use token" value="{token}" required>
    <label for="c">Core host (optional)</label>
    <input id="c" name="core_host" placeholder="only if the wizard didn't bake one in" value="{core}">
    <button type="submit">Enroll</button>
  </form>
</body></html>
"""

DONE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Argus probe enrolling</title>
<style>body {{ font-family: system-ui, sans-serif; max-width: 30rem; margin: 6vh auto; padding: 0 1rem; }}</style>
</head><body><h1>Enrolling…</h1>
<p>The probe is starting and will register with Argus shortly. You can close this page.</p>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, status=200):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(PAGE.format(error="", url="", token="", core=""))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
        url = form.get("enroll_url", [""])[0].strip()
        token = form.get("enroll_token", [""])[0].strip()
        core = form.get("core_host", [""])[0].strip()
        if not url or not token:
            err = '<p class="err">Enrollment URL and token are both required.</p>'
            self._send(PAGE.format(error=err, url=html.escape(url), token=html.escape(token),
                                   core=html.escape(core)), status=400)
            return
        write_env(url, token, core)
        self._send(DONE)
        self.server.enrolled = True  # signal the main loop to stop after this request

    def log_message(self, *args):  # keep the journal quiet
        pass


def main():
    if already_enrolled():
        start_probe()
        return 0
    httpd = HTTPServer(LISTEN, Handler)
    httpd.enrolled = False
    print(f"argus-firstboot: no token yet; serving setup page on http://{LISTEN[0]}:{LISTEN[1]}/")
    while not httpd.enrolled:
        httpd.handle_request()
    start_probe()
    print("argus-firstboot: enrolled; probe service started")
    return 0


if __name__ == "__main__":
    sys.exit(main())
