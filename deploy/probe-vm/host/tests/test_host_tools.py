#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi
"""Tests for the probe VM's host-tools folder: the installer writes what the manifest says, only when
it changed, with the modes it says, renders the data folder into the units, drops the reload marker
when any file changed, and the manifest matches the folder. Standard library; runs anywhere
(CI runs it before the image is built)."""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HOST = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_installer():
    loader = importlib.machinery.SourceFileLoader("install_host_tools", os.path.join(HOST, "install-host-tools.py"))
    spec = importlib.util.spec_from_loader("install_host_tools", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class ManifestTest(unittest.TestCase):
    def test_the_manifest_matches_the_folder(self):
        mod = load_installer()
        with open(os.path.join(HOST, "manifest"), encoding="utf-8") as fh:
            entries = mod.parse_manifest(fh.read())
        listed = {e["source"] for e in entries}
        for e in entries:
            self.assertTrue(os.path.exists(os.path.join(HOST, e["source"])), "manifest lists %s, which is not in the folder" % e["source"])
            self.assertTrue(e["dest"].startswith(("/usr/local/sbin/", "/etc/systemd/system/")), e["dest"])
            if e["dest"].endswith((".timer", ".path")):
                self.assertTrue(e["enable"], "%s must be enabled by the host" % e["dest"])
            if e["dest"].startswith("/usr/local/sbin/"):
                self.assertEqual(e["mode"], 0o755, e["dest"])
        for name in os.listdir(HOST):
            if name.endswith((".service", ".timer", ".path")) or (name.startswith("argus-") and not name.endswith(".py")):
                self.assertIn(name, listed, "%s is in the folder but not in the manifest" % name)
        # The console menu is one file in two repositories: this copy must carry both roles.
        with open(os.path.join(HOST, "argus-console"), encoding="utf-8") as fh:
            console = fh.read()
        self.assertIn('ROLE == "probe"', console)
        self.assertIn("ONE FILE, TWO REPOSITORIES", console)

    def test_bad_manifests_are_refused(self):
        mod = load_installer()
        for text, why in (("a /x\n", "mode"), ("a/b /x 0755\n", "slash in source"), ("a x 0755\n", "relative dest"),
                          ("a /x/../y 0755\n", "dotdot"), ("a /x 0755 frobnicate\n", "flag"), ("# only\n", "empty")):
            with self.assertRaises(mod.ManifestError, msg=why):
                mod.parse_manifest(text)

    @unittest.skipUnless(shutil.which("bash") and shutil.which("sh"), "needs a POSIX shell")
    def test_the_shell_scripts_parse(self):
        for name in ("argus-host-tools-apply", "argus-console-login"):
            r = subprocess.run([shutil.which("bash"), "-n", os.path.join(HOST, name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_python_scripts_compile(self):
        import py_compile
        for name in ("argus-console", "argus-os-report", "install-host-tools.py"):
            py_compile.compile(os.path.join(HOST, name), doraise=True)


# (os-release, /etc/debian_version or None, the name Argus shows). Debian by its point release, from
# DEBIAN_VERSION_FULL (Debian 13 on) or debian_version (before); anything else by its PRETTY_NAME.
DEBIAN_13 = ('PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nNAME="Debian GNU/Linux"\nVERSION_ID="13"\n'
             'VERSION="13 (trixie)"\nVERSION_CODENAME=trixie\nDEBIAN_VERSION_FULL=13.7\nID=debian\n')
OS_NAMES = [
    (DEBIAN_13, "13.7", "Debian 13.7"),
    (DEBIAN_13, None, "Debian 13.7"),
    ('PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nVERSION_ID="12"\nID=debian\n', "12.12\n", "Debian 12.12"),
    ('PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nVERSION_ID="12"\nID=debian\n', None, "Debian GNU/Linux 12 (bookworm)"),
    ('PRETTY_NAME="Debian GNU/Linux forky/sid"\nID=debian\n', "forky/sid\n", "Debian GNU/Linux forky/sid"),
    ('PRETTY_NAME="Ubuntu 24.04.3 LTS"\nVERSION_ID="24.04"\nID=ubuntu\nID_LIKE=debian\n', "trixie/sid\n", "Ubuntu 24.04.3 LTS"),
    (None, None, ""),
]


class OSReportTest(unittest.TestCase):
    def test_debian_is_named_by_its_point_release(self):
        loader = importlib.machinery.SourceFileLoader("argus_os_report", os.path.join(HOST, "argus-os-report"))
        spec = importlib.util.spec_from_loader("argus_os_report", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        for i, (release, debian, want) in enumerate(OS_NAMES):
            rel, deb = os.path.join(tmp, "os-release-%d" % i), os.path.join(tmp, "debian_version-%d" % i)
            for path, text in ((rel, release), (deb, debian)):
                if text is not None:
                    with open(path, "w", encoding="utf-8", newline="\n") as fh:
                        fh.write(text)
            self.assertEqual(mod.os_name(rel, deb), want, release)


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_installer()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = os.path.join(self.tmp, "src")
        self.root = os.path.join(self.tmp, "root")
        self.update = os.path.join(self.tmp, "update")
        os.makedirs(self.src)
        os.makedirs(self.update)
        self.write("manifest", "tool /usr/local/sbin/tool 0755\nunit.service /etc/systemd/system/unit.service 0644 render\n"
                               "unit.timer /etc/systemd/system/unit.timer 0644 enable\n")
        self.write("tool", "#!/bin/sh\necho hi\n")
        self.write("unit.service", "[Service]\nEnvironment=ARGUS_STATE_DIR=@STATE_DIR@\n")
        self.write("unit.timer", "[Timer]\nOnBootSec=1min\n")

    def write(self, name, text):
        with open(os.path.join(self.src, name), "w", newline="\n") as fh:
            fh.write(text)

    def read(self, rel):
        with open(os.path.join(self.root, rel), encoding="utf-8") as fh:
            return fh.read()

    def install(self):
        return self.mod.install(self.src, self.root, "/docker/argus-probe", self.update, "7.0.33-r1", now=1700000000)

    def test_first_install_writes_everything_and_drops_the_marker(self):
        s = self.install()
        self.assertEqual(s["installed"], ["/etc/systemd/system/unit.service", "/etc/systemd/system/unit.timer", "/usr/local/sbin/tool"])
        self.assertEqual(s["unchanged"], [])
        self.assertEqual(s["units_changed"], ["unit.service", "unit.timer"])
        self.assertTrue(s["apply"])
        self.assertEqual(self.read("etc/systemd/system/unit.service"), "[Service]\nEnvironment=ARGUS_STATE_DIR=/docker/argus-probe\n", "rendered")
        self.assertEqual(self.read("usr/local/sbin/tool"), "#!/bin/sh\necho hi\n")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.root, "usr/local/sbin/tool")).st_mode), 0o755)
        with open(os.path.join(self.update, "host-tools-apply.json"), encoding="utf-8") as fh:
            marker = json.load(fh)
        self.assertEqual(marker, {"at": 1700000000, "version": "7.0.33-r1", "enable": "unit.timer", "changed": "unit.service unit.timer"})
        self.assertFalse(os.path.exists(os.path.join(self.root, "usr/local/sbin/.tool.argus-new")), "no temporary file left")

    def test_a_second_run_changes_nothing(self):
        self.install()
        os.remove(os.path.join(self.update, "host-tools-apply.json"))
        s = self.install()
        self.assertEqual(s["installed"], [])
        self.assertEqual(len(s["unchanged"]), 3)
        self.assertFalse(s["apply"])
        self.assertFalse(os.path.exists(os.path.join(self.update, "host-tools-apply.json")))

    def test_a_changed_script_drops_the_marker_so_the_host_re_reports(self):
        self.install()
        os.remove(os.path.join(self.update, "host-tools-apply.json"))
        self.write("tool", "#!/bin/sh\necho bye\n")
        s = self.install()
        self.assertEqual(s["installed"], ["/usr/local/sbin/tool"])
        self.assertEqual(s["units_changed"], [])
        self.assertTrue(s["apply"], "a new reporter must run now, not at its next hourly tick")
        self.assertEqual(self.read("usr/local/sbin/tool"), "#!/bin/sh\necho bye\n")
        with open(os.path.join(self.update, "host-tools-apply.json"), encoding="utf-8") as fh:
            marker = json.load(fh)
        self.assertEqual(marker, {"at": 1700000000, "version": "7.0.33-r1", "enable": "unit.timer", "changed": ""})

    def test_a_missing_source_or_manifest_is_an_error(self):
        self.write("manifest", "ghost /usr/local/sbin/ghost 0755\n")
        with self.assertRaises(self.mod.ManifestError):
            self.install()
        os.remove(os.path.join(self.src, "manifest"))
        with self.assertRaises(self.mod.ManifestError):
            self.install()

    def test_the_command_line_prints_the_summary(self):
        r = subprocess.run([sys.executable, "-I", os.path.join(HOST, "install-host-tools.py"), "--src", self.src, "--root", self.root,
                            "--state-dir", "/docker/argus-probe", "--update-dir", self.update, "--version", "x"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["version"], "x")
        r = subprocess.run([sys.executable, "-I", os.path.join(HOST, "install-host-tools.py"), "--src", os.path.join(self.tmp, "nowhere"),
                            "--root", self.root, "--update-dir", self.update], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("carries no host tools", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
