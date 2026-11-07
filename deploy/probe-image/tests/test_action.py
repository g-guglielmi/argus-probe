#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi

# Tests for argus_action.py (DESIGN 7r): arguments arrive base64 and an unresolved macro is named,
# the SSH actions read the exit code and the state back, refusals say what the host must allow, and
# whatever happens the script prints one JSON object. Stdlib only: the SSH session is faked.
import base64
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "externalscripts", "argus_action.py")
spec = importlib.util.spec_from_file_location("argus_action", SCRIPT)
act = importlib.util.module_from_spec(spec)
spec.loader.exec_module(act)


def b(s):
    return base64.b64encode(s.encode()).decode()


SSH_ARGS = [b("10.0.1.20"), b("argus"), b("22"), b("key"), b(""), b("/var/lib/zabbix/ssh/argus_id")]


def run(argv, stdout=b"", stderr=b"", rc=0):
    """main() with the SSH session faked: what it printed, as JSON, and the commands it ran."""
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)

    real, realpath = act.subprocess.run, act.os.path.realpath
    act.subprocess.run = fake
    if os.name != "posix":  # the key path check is a POSIX path; CI runs on Linux
        act.os.path.realpath = lambda p: p
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            act.main(argv)
    finally:
        act.subprocess.run, act.os.path.realpath = real, realpath
    lines = out.getvalue().strip().splitlines()
    assert len(lines) == 1, out.getvalue()
    return json.loads(lines[0]), calls


class ArgTest(unittest.TestCase):
    def test_base64(self):
        self.assertEqual(act.arg(b("nginx.service"), "x"), "nginx.service")
        self.assertEqual(act.arg("", "x"), "")

    def test_unresolved_macro_is_named(self):
        with self.assertRaises(act.Missing) as e:
            act.arg("{{$NS.USER}.btoa()}", "{$NS.USER}")
        self.assertIn("{$NS.USER}", str(e.exception))

    def test_not_base64(self):
        with self.assertRaises(act.Missing):
            act.arg("nginx;id", "x")


class ServiceTest(unittest.TestCase):
    def test_worked(self):
        res, calls = run(["ssh-service"] + SSH_ARGS + [b("nginx.service")],
                         stdout=b"\n@@RC 0\nActiveState=active\nSubState=running\nResult=success\n")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["state"], "active (running)")
        snippet = calls[0][-1]
        self.assertIn("sudo -n systemctl restart nginx.service", snippet)
        self.assertEqual(calls[0][calls[0].index("--") + 1], "argus@10.0.1.20")

    def test_sudo_password(self):
        res, _ = run(["ssh-service"] + SSH_ARGS + [b("nginx.service")],
                     stdout=b"sudo: a password is required\n\n@@RC 1\nActiveState=failed\nSubState=failed\n")
        self.assertFalse(res["ok"])
        self.assertEqual(res["hint"], "sudo")
        self.assertIn("password", res["why"])

    def test_not_active_after(self):
        res, _ = run(["ssh-service"] + SSH_ARGS + [b("nginx.service")],
                     stdout=b"\n@@RC 0\nActiveState=activating\nSubState=auto-restart\n")
        self.assertFalse(res["ok"])
        self.assertIn("activating", res["why"])

    def test_bad_unit_never_reaches_ssh(self):
        res, calls = run(["ssh-service"] + SSH_ARGS + [b("a;id")])
        self.assertFalse(res["ok"])
        self.assertEqual(calls, [])

    def test_login_failed(self):
        res, _ = run(["ssh-service"] + SSH_ARGS + [b("nginx.service")],
                     stderr=b"argus@10.0.1.20: Permission denied (publickey).\n", rc=255)
        self.assertFalse(res["ok"])
        self.assertIn("Permission denied", res["why"])


class ContainerTest(unittest.TestCase):
    def test_worked(self):
        res, calls = run(["ssh-container"] + SSH_ARGS + [b("jellyfin")], stdout=b"jellyfin\n\n@@RC 0\nrunning healthy\n")
        self.assertTrue(res["ok"], res)
        self.assertIn("docker restart jellyfin", calls[0][-1])

    def test_docker_denied(self):
        res, _ = run(["ssh-container"] + SSH_ARGS + [b("jellyfin")],
                     stdout=b"permission denied while trying to connect to the Docker daemon socket\n\n@@RC 1\n")
        self.assertFalse(res["ok"])
        self.assertEqual(res["hint"], "docker")


class CommandTest(unittest.TestCase):
    def test_exit_code(self):
        res, calls = run(["ssh-command"] + SSH_ARGS + [b("sudo -n journalctl --vacuum-size=500M")],
                         stdout=b"Vacuuming done, freed 1.2G\n\n@@RC 0\n")
        self.assertTrue(res["ok"], res)
        self.assertIn("Vacuuming done", res["output"])
        self.assertIn("( sudo -n journalctl --vacuum-size=500M )", calls[0][-1])
        res, _ = run(["ssh-command"] + SSH_ARGS + [b("false")], stdout=b"\n@@RC 1\n")
        self.assertFalse(res["ok"])
        self.assertIn("code 1", res["why"])


class NetScalerTest(unittest.TestCase):
    def test_missing_login(self):
        res, _ = run(["netscaler", b("10.0.1.30"), "{{$NS.USER}.btoa()}", "{{$NS.PASSWORD}.btoa()}"])
        self.assertFalse(res["ok"])
        self.assertIn("{$NS.USER}", res["why"])


class MainTest(unittest.TestCase):
    def test_unknown_kind(self):
        res, _ = run(["format-disk"])
        self.assertFalse(res["ok"])
        self.assertIn("update it", res["why"])

    def test_too_few(self):
        res, _ = run(["ssh-service", b("x")])
        self.assertFalse(res["ok"])

    def test_webhook_bad_method(self):
        res, _ = run(["webhook", b("https://hooks.example.com/"), b("DELETE"), b(""), "", ""])
        self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main()
