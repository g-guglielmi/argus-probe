#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi

# argus_action.py - runs an Argus action (DESIGN 7r) on the Zabbix instance that monitors the host.
#
# MIRRORED, keep byte-identical:
#   argus-probe: deploy/probe-image/externalscripts/argus_action.py  (baked into the proxy image)
#   argus-core:  deploy/core/externalscripts/argus_action.py         (synced to the core by the updater)
#
# Each Argus action is a Zabbix global script Argus owns; for the actions that don't go through the
# host's Zabbix agent, the script runs this, on the site's probe (its proxy, EnableRemoteCommands=1)
# or on the core's Zabbix server (EnableGlobalScripts=1), as the zabbix user:
#
#   argus_action.py ssh-service   <host> <user> <port> <auth> <password> <keyfile> <unit>
#   argus_action.py ssh-container <host> <user> <port> <auth> <password> <keyfile> <container>
#   argus_action.py ssh-command   <host> <user> <port> <auth> <password> <keyfile> <command>
#   argus_action.py netscaler     <host> <user> <password>
#   argus_action.py webhook       <url> <method> <header> <secret> <body>
#
# Every argument arrives base64-encoded (Zabbix's {{$MACRO}.btoa()}, or Argus's own encoding), so no
# value a person typed is read by the shell Zabbix starts this from; an argument that is still a
# macro ("{{$NS.USER}.btoa()}") is a macro the host doesn't have. The SSH login is the host's own
# monitoring login, the same as argus_linux_ssh.py's (its code does the login here too).
#
# Prints ONE JSON object, always, and exits 0: {ok, why, hint, output, state}. why says in words why
# it failed; hint names what the host must allow (sudo, docker); output is what the host printed.
# It finishes within 45 s: Zabbix's frontend waits 60 s for a script.
import base64
import binascii
import importlib.util
import json
import os
import re
import shlex
import ssl
import subprocess
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
SSH_TIMEOUT = 40
HTTP_TIMEOUT = 30
OUTPUT_MAX = 8000

UNIT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._:-]{0,127}")
CONTAINER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def load(name):
    """A sibling collector as a module (argus_linux_ssh.py for the SSH login, argus_http.py for the
    IPv4-first connection)."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Missing(Exception):
    """An argument that is a macro the host doesn't have, or isn't base64."""


def arg(value, what):
    """One argument decoded. An unresolved macro or a value that isn't base64 raises Missing, naming it."""
    v = value.strip()
    if v.startswith("{"):
        raise Missing("the host has no %s: set it in the host's settings" % what)
    try:
        return base64.b64decode(v, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        raise Missing("Argus passed %s in a form this script can't read: update it" % what)


def emit(ok, why="", hint="", output="", state=""):
    out = output if len(output) <= OUTPUT_MAX else output[:OUTPUT_MAX] + "\n[cut at %d characters]" % OUTPUT_MAX
    print(json.dumps({"ok": bool(ok), "why": why, "hint": hint, "output": out, "state": state}))


def sections(text):
    """The remote output split at its @@RC marker: what the command printed, its exit code, and what
    came after (the state read back)."""
    m = re.search(r"^@@RC (\d+)\s*$", text, re.M)
    if not m:
        return text, None, ""
    return text[:m.start()].rstrip("\n"), int(m.group(1)), text[m.end():].strip("\n")


def remote_why(out, rc, what):
    """Why a command failed on the host, in words, and what the host must allow."""
    low = out.lower()
    if "a password is required" in low or "a terminal is required" in low:
        return "sudo asks for a password: the login may not run this command. Add the line below on the host", "sudo"
    if "is not in the sudoers file" in low or "not allowed to execute" in low:
        return "sudo refused: the login may not run this command. Add the line below on the host", "sudo"
    if "permission denied" in low and "docker" in low:
        return "the login may not use Docker (permission denied on the Docker socket)", "docker"
    if "interactive authentication required" in low or "access denied" in low:
        return "systemd refused: the login may not restart it without sudo", "sudo"
    if re.search(r"unit \S+ not found|could not be found", low):
        return "%s does not exist on the host" % what, ""
    if "no such container" in low:
        return "%s does not exist on the host" % what, ""
    if rc == 127 or "command not found" in low:
        return "the command was not found on the host", ""
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    return ("it ended with code %d" % rc) + (": " + lines[-1][:200] if lines else ""), ""


def run_ssh(argv, kind):
    """The SSH actions: one session that runs the command and reads the state back."""
    ssh = load("argus_linux_ssh")
    try:
        host = arg(argv[0], "address")
        user = arg(argv[1], "{$SSH.USER}") or "root"
        port = arg(argv[2], "{$SSH.PORT}") or "22"
        auth = (arg(argv[3], "{$SSH.AUTH}") or "key").lower()
        try:
            passwd = arg(argv[4], "{$SSH.PASSWORD}")
        except Missing:
            passwd = ""
        keyfile = arg(argv[5], "{$SSH.KEYFILE}") or "/var/lib/zabbix/ssh/argus_id"
        value = arg(argv[6], "the value to act on")
    except Missing as e:
        emit(False, str(e))
        return
    ssh.scrub_cmdline("argus_action.py")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}", user) or not re.fullmatch(r"[A-Za-z0-9.:_\[\]-]{1,253}", host):
        emit(False, "the host's address or SSH login has characters an action can't use")
        return
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        emit(False, "{$SSH.PORT} is not a port (1-65535)")
        return
    if auth == "key" and not os.path.realpath(keyfile).startswith("/var/lib/zabbix/ssh/"):
        emit(False, "the SSH key must live under /var/lib/zabbix/ssh/")
        return

    if kind == "service":
        if not UNIT_RE.fullmatch(value):
            emit(False, "%r is not a systemd unit name" % value)
            return
        u = shlex.quote(value)
        snippet = ("sudo -n systemctl restart %s 2>&1; rc=$?; echo; echo \"@@RC $rc\"; sleep 2; "
                   "systemctl show --no-pager --property=ActiveState,SubState,Result %s 2>&1" % (u, u))
        what = value
    elif kind == "container":
        if not CONTAINER_RE.fullmatch(value):
            emit(False, "%r is not a container name" % value)
            return
        c = shlex.quote(value)
        snippet = ("docker restart %s 2>&1; rc=$?; echo; echo \"@@RC $rc\"; sleep 2; "
                   "docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' %s 2>&1" % (c, c))
        what = "the container " + value
    else:
        if not value.strip() or "\n" in value or len(value) > 500:
            emit(False, "the command is empty or not one line")
            return
        snippet = "( %s ) 2>&1; rc=$?; echo; echo \"@@RC $rc\"" % value
        what = "the command"

    cmd = ssh.ssh_command(host, user, port, auth, keyfile, snippet)
    env = dict(os.environ)
    if auth == "password":
        env["SSHPASS"] = passwd
    try:
        proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=SSH_TIMEOUT)
    except subprocess.TimeoutExpired:
        emit(False, "the SSH session did not finish within %d s" % SSH_TIMEOUT)
        return
    except FileNotFoundError:
        emit(False, "ssh or sshpass is not installed here")
        return
    except Exception as e:
        emit(False, ssh.reason(e))
        return
    text = proc.stdout.decode("utf-8", "replace")
    out, rc, after = sections(text)
    if rc is None:
        why = ssh.ssh_reason(proc.stderr, proc.returncode, auth) or "the SSH session ended before the command did"
        emit(False, "could not log in over SSH: " + why if proc.returncode == 255 or "denied" in why.lower() else why, output=text)
        return
    shown = out + ("\n" + after if after else "")
    if kind == "service":
        props = dict(l.split("=", 1) for l in after.splitlines() if "=" in l)
        state = props.get("ActiveState", "")
        if props.get("SubState"):
            state += " (" + props["SubState"] + ")"
        if rc != 0:
            why, hint = remote_why(out, rc, value)
            emit(False, why, hint, shown, state)
        elif props.get("ActiveState") != "active":
            emit(False, "%s is %s after the restart" % (value, state or "not running"), "", shown, state)
        else:
            emit(True, "", "", shown, state)
        return
    if kind == "container":
        state = after.strip().splitlines()[-1].strip() if after.strip() else ""
        if rc != 0:
            why, hint = remote_why(out, rc, "the container " + value)
            emit(False, why, hint, shown, state)
        elif not state.startswith("running") or "unhealthy" in state:
            emit(False, "%s is %s after the restart" % (value, state or "not running"), "", shown, state)
        else:
            emit(True, "", "", shown, state)
        return
    if rc != 0:
        why, hint = remote_why(out, rc, what)
        emit(False, why, hint, out)
        return
    emit(True, "", "", out)


def http_request(url, method, headers, body):
    """One HTTP(S) request (IPv4 first, like the HTTP checks): status, the start of the answer."""
    import http.client
    h = load("argus_http")
    u = urllib.parse.urlsplit(url)
    port = u.port or (443 if u.scheme == "https" else 80)
    if u.scheme == "https":
        ctx = ssl.create_default_context()
        if headers.pop("__insecure__", None):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(u.hostname, port, timeout=HTTP_TIMEOUT, context=ctx)
    else:
        conn = http.client.HTTPConnection(u.hostname, port, timeout=HTTP_TIMEOUT)
    conn._create_connection = h.create_connection
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read(OUTPUT_MAX).decode("utf-8", "replace")
        return resp.status, data, ""
    except Exception as e:
        return 0, "", h.why(e, port, HTTP_TIMEOUT)
    finally:
        conn.close()


def run_netscaler(argv):
    """NITRO: save the running config (POST /nitro/v1/config/nsconfig?action=save). NetScalers carry
    their own certificate, so it isn't checked."""
    try:
        host = arg(argv[0], "address")
        user = arg(argv[1], "{$NS.USER}")
        passwd = arg(argv[2], "{$NS.PASSWORD}")
    except Missing as e:
        emit(False, str(e) + " (the action's settings set the NetScaler login)")
        return
    load("argus_linux_ssh").scrub_cmdline("argus_action.py")
    if not re.fullmatch(r"[A-Za-z0-9.:_\[\]-]{1,253}", host):
        emit(False, "the host's address has characters an action can't use")
        return
    hostpart = "[" + host + "]" if ":" in host and not host.startswith("[") else host
    status, data, err = http_request("https://%s/nitro/v1/config/nsconfig?action=save" % hostpart, "POST",
                                     {"X-NITRO-USER": user, "X-NITRO-PASS": passwd, "Content-Type": "application/json", "__insecure__": "1"},
                                     json.dumps({"nsconfig": {}}))
    if err:
        emit(False, "could not reach the NetScaler's API: " + err)
        return
    msg = ""
    try:
        msg = json.loads(data).get("message", "")
    except ValueError:
        pass
    if status == 200:
        emit(True, "", "", "HTTP 200: " + (msg or "Done"), "saved")
    elif status == 401:
        emit(False, "the NetScaler refused the login ({$NS.USER} / {$NS.PASSWORD})", "", data)
    elif status == 403:
        emit(False, "the NetScaler login may not save the config: " + (msg or "forbidden"), "", data)
    else:
        emit(False, "the NetScaler answered HTTP %d: %s" % (status, msg or data[:200]), "", data)


def run_webhook(argv):
    try:
        url = arg(argv[0], "the URL")
        method = arg(argv[1], "the method") or "POST"
        header = arg(argv[2], "the header name")
        secret = arg(argv[3], "the secret header's value") if argv[3].strip() else ""
        body = arg(argv[4], "the body") if argv[4].strip() else ""
    except Missing as e:
        emit(False, str(e))
        return
    load("argus_linux_ssh").scrub_cmdline("argus_action.py")
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname or method not in ("POST", "PUT", "GET"):
        emit(False, "the webhook's URL or method is not one Argus makes")
        return
    headers = {"User-Agent": "Argus-action"}
    if header and secret:
        headers[header] = secret
    data = None
    if method != "GET":
        headers["Content-Type"] = "application/json"
        data = body.encode("utf-8") if body else b""
    status, text, err = http_request(url, method, headers, data)
    if err:
        emit(False, "could not reach the webhook: " + err)
        return
    out = "HTTP %d\n%s" % (status, text)
    if 200 <= status < 300:
        emit(True, "", "", out, "HTTP %d" % status)
    else:
        emit(False, "the webhook answered HTTP %d" % status, "", out, "HTTP %d" % status)


def main(argv):
    if not argv:
        emit(False, "argus_action.py needs a kind of action")
        return
    kind, rest = argv[0], argv[1:]
    need = {"ssh-service": 7, "ssh-container": 7, "ssh-command": 7, "netscaler": 3, "webhook": 5}
    if kind not in need:
        emit(False, "argus_action.py doesn't know the action %r: update it" % kind)
        return
    if len(rest) < need[kind]:
        emit(False, "argus_action.py got %d values for %s, not %d: update it" % (len(rest), kind, need[kind]))
        return
    try:
        if kind.startswith("ssh-"):
            run_ssh(rest, kind[4:])
        elif kind == "netscaler":
            run_netscaler(rest)
        else:
            run_webhook(rest)
    except Exception as e:  # never a traceback for Zabbix to show: the reason, as JSON
        emit(False, "argus_action.py failed: %s" % (" ".join(str(e).split())[:200] or e.__class__.__name__))


if __name__ == "__main__":
    main(sys.argv[1:])
