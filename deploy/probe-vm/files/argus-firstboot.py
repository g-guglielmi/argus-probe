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
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

ENV_PATH = "/etc/argus-probe/probe.env"
ENROLL_DIR = "/var/lib/argus-probe/enroll"
CERT = os.path.join(ENROLL_DIR, "proxy.crt")
META = os.path.join(ENROLL_DIR, "proxy.env")
PROBE_SERVICE = "argus-probe.service"
FIRSTBOOT_SERVICE = "argus-firstboot.service"
LISTEN = ("0.0.0.0", 80)


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
    # restart (not just start) so a corrected probe.env is picked up on a retry.
    subprocess.run(["systemctl", "enable", PROBE_SERVICE], check=False)
    subprocess.run(["systemctl", "restart", PROBE_SERVICE], check=False)


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
            # After a failure: stop the probe retrying the bad values and reopen the form, prefilled
            # with what was entered so only the wrong field needs fixing.
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
        self.server.attempt_since = time.time()  # scope status polling to this attempt
        start_probe()
        self.server.submitted = True
        self._send(page(PROGRESS))

    def log_message(self, *args):
        pass


def monitor(httpd):
    """Once the probe is enrolled, keep serving briefly (so the page shows success), then disable this
    service for future boots and stop."""
    while True:
        time.sleep(3)
        if os.path.exists(CERT):
            time.sleep(20)
            subprocess.run(["systemctl", "disable", FIRSTBOOT_SERVICE], check=False)
            httpd.shutdown()
            return


def main():
    if already_enrolled() and os.path.exists(CERT):
        start_probe()
        subprocess.run(["systemctl", "disable", FIRSTBOOT_SERVICE], check=False)
        return 0
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
