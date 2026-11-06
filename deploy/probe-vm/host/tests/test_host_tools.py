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
        for name in ("argus-host-tools-apply", "argus-console-login", "argus-reboot-check", "argus-patch"):
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
            self.assertEqual(mod.os_name(rel, deb, os.path.join(tmp, "no-dietpi")), want, release)

    def load(self):
        loader = importlib.machinery.SourceFileLoader("argus_os_report", os.path.join(HOST, "argus-os-report"))
        spec = importlib.util.spec_from_loader("argus_os_report", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def test_dietpi_names_itself_and_its_debian(self):
        mod = self.load()
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        rel, ver = os.path.join(tmp, "os-release"), os.path.join(tmp, ".version")
        self.write(rel, DEBIAN_13)
        self.write(ver, "G_DIETPI_VERSION_CORE=10\nG_DIETPI_VERSION_SUB=8\nG_DIETPI_VERSION_RC=1\nG_GITBRANCH='master'\n")
        self.assertEqual(mod.os_name(rel, os.path.join(tmp, "none"), ver), "DietPi 10.8 (Debian 13.7)")
        self.write(ver, "G_DIETPI_VERSION_CORE=x\n")
        self.assertEqual(mod.os_name(rel, os.path.join(tmp, "none"), ver), "Debian 13.7")

    def test_a_waiting_dietpi_release(self):
        mod = self.load()
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        flag = os.path.join(tmp, ".update_available")
        self.assertEqual(mod.dietpi_update(flag), "")
        for text, want in (("10.9.0\n", "10.9.0"), ("-1\n", ""), ("rm -rf /\n", "")):
            self.write(flag, text)
            self.assertEqual(mod.dietpi_update(flag), want, text)

    def test_the_count_takes_security_and_raspberry_pi(self):
        mod = self.load()
        out = ("Inst libc6 [2.41-12] (2.41-12+deb13u1 Debian-Security:13/stable-security [arm64])\n"
               "Inst linux-image-rpi-v8 [1:6.12.47-1+rpt1] (1:6.12.62-1+rpt1 Raspberry Pi Foundation:stable [arm64])\n"
               "Inst curl [8.14.1-2] (8.14.1-2+deb13u2 Debian:13.8/stable [arm64])\n"
               "Conf libc6 (2.41-12+deb13u1 Debian-Security:13/stable-security [arm64])\n")
        self.assertEqual(mod.count_updates(out), 2)

    def test_a_reboot_is_needed_when(self):
        mod = self.load()
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        flag, modules, boot = os.path.join(tmp, "reboot-required"), os.path.join(tmp, "modules"), os.path.join(tmp, "boot")
        for d in (modules, boot):
            os.makedirs(d)

        def kernels(*names):
            for d in (modules, boot):
                for n in os.listdir(d):
                    p = os.path.join(d, n)
                    shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
            for n in names:
                os.makedirs(os.path.join(modules, n))
                open(os.path.join(boot, "vmlinuz-" + n), "w").close()

        def why(running):
            return mod.reboot_reason(flag, modules, boot, running)

        kernels("6.12.47+rpt-rpi-v8", "6.12.47+rpt-rpi-2712")
        self.assertEqual(why("6.12.47+rpt-rpi-v8"), "", "up to date")
        open(flag, "w").close()
        self.assertEqual(why("6.12.47+rpt-rpi-v8"), "an update asks for it")
        os.remove(flag)
        kernels("6.12.62+rpt-rpi-v8", "6.12.62+rpt-rpi-2712")
        self.assertEqual(why("6.12.47+rpt-rpi-v8"), "the running kernel was removed")
        kernels("6.12.47+rpt-rpi-v8", "6.12.62+rpt-rpi-v8")
        self.assertEqual(why("6.12.47+rpt-rpi-v8"), "a newer kernel is installed")
        kernels("6.12.47+rpt-rpi-v8", "6.12.62+rpt-rpi-2712")
        self.assertEqual(why("6.12.47+rpt-rpi-v8"), "", "another flavour's newer kernel is not this one's")
        kernels("6.12.48+deb13-amd64", "6.12.57+deb13-amd64")
        self.assertEqual(why("6.12.57+deb13-amd64"), "", "an older kernel kept beside the running one")
        self.assertEqual(why("6.12.48+deb13-amd64"), "a newer kernel is installed")
        kernels("6.1.0-25-amd64", "6.1.0-26-amd64")
        self.assertEqual(why("6.1.0-25-amd64"), "a newer kernel is installed", "Debian 12's ABI numbers")
        kernels()
        self.assertEqual(why("6.12.48+deb13-amd64"), "", "no modules folder at all (a container) says nothing")


class LANTest(unittest.TestCase):
    """The VM's LAN address and gateway come from ip -j route get; anything else reads as none."""

    def setUp(self):
        loader = importlib.machinery.SourceFileLoader("argus_os_report", os.path.join(HOST, "argus-os-report"))
        spec = importlib.util.spec_from_loader("argus_os_report", loader)
        self.mod = importlib.util.module_from_spec(spec)
        loader.exec_module(self.mod)

    def lan(self, stdout=None, exc=None):
        class Out:
            pass

        def run(*a, **kw):
            if exc:
                raise exc
            o = Out()
            o.stdout = stdout
            return o
        return self.mod.lan(run)

    def test_the_route_gives_address_and_gateway(self):
        out = '[{"dst":"1.1.1.1","gateway":"10.0.1.1","dev":"ens18","prefsrc":"10.0.1.20","flags":[],"uid":0,"cache":[]}]'
        self.assertEqual(self.lan(out), {"ip": "10.0.1.20", "gw": "10.0.1.1"})

    def test_a_direct_route_has_no_gateway(self):
        self.assertEqual(self.lan('[{"dst":"1.1.1.1","dev":"ens18","prefsrc":"10.0.1.20"}]'), {"ip": "10.0.1.20", "gw": ""})

    def test_no_route_or_no_ip_reads_as_none(self):
        for out in ("", "[]", "{}", "not json", '[{"dst":"1.1.1.1"}]', '[{"prefsrc":5}]', '["x"]'):
            self.assertEqual(self.lan(out), {}, out)
        self.assertEqual(self.lan(exc=FileNotFoundError("ip")), {})


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


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class RebootCheckTest(unittest.TestCase):
    """argus-reboot-check against a stub clock: it reboots only when the OS asks for it, in its window."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.state = os.path.join(self.tmp, "state")
        self.bin = os.path.join(self.tmp, "bin")
        self.zones = os.path.join(self.tmp, "zoneinfo")
        for d in (self.state, self.bin, os.path.join(self.zones, "Europe")):
            os.makedirs(d)
        open(os.path.join(self.zones, "Europe", "Rome"), "w").close()
        self.flag = os.path.join(self.tmp, "reboot-required")
        self.log = os.path.join(self.tmp, "calls")
        # The clock reads Sunday 03:02 (and records the zone it was asked in); systemctl, logger and
        # the reporter only record that they ran.
        self.stub("date", 'echo "TZ=${TZ:-}" >> "%s"; case "$1" in +%%w) echo 0;; +%%-H) echo 3;; +%%-M) echo 2;; *) echo 1700000000;; esac' % self.log)
        self.stub("systemctl", 'echo "systemctl $*" >> "%s"' % self.log)
        self.stub("logger", ':')
        # The reporter's broader test (--reboot-needed): says no unless a test says otherwise.
        self.report_says = os.path.join(self.tmp, "report-says-yes")
        self.stub("argus-os-report", '[ "$1" = --reboot-needed ] && [ -f "%s" ] && { echo "a newer kernel is installed"; exit 0; }; [ "$1" = --reboot-needed ] && exit 1; echo report >> "%s"' % (self.report_says.replace("\\", "/"), self.log.replace("\\", "/")))

    def stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w", newline="\n") as fh:
            fh.write("#!/usr/bin/env bash\n" + body + "\n")
        os.chmod(path, 0o755)

    def run_check(self, window=None, needs_reboot=True):
        if window is not None:
            with open(os.path.join(self.state, "reboot-window.json"), "w") as fh:
                fh.write(json.dumps(window))
        if needs_reboot:
            open(self.flag, "w").close()
        stubs = self.bin
        if os.name == "nt":  # Git Bash: a drive letter's colon would split its PATH
            stubs = subprocess.run(["cygpath", "-u", stubs], capture_output=True, text=True).stdout.strip()
        env = dict(os.environ, ARGUS_STUBS=stubs, ARGUS_STATE_DIR=self.state, ARGUS_OS_REPORT="argus-os-report",
                   ARGUS_REBOOT_FLAG=self.flag, ARGUS_UU_REBOOT_CONF=os.path.join(self.tmp, "53argus"), ARGUS_ZONEINFO=self.zones)
        r = subprocess.run([shutil.which("bash"), "-c", 'PATH="$ARGUS_STUBS:$PATH" exec bash "$0"', os.path.join(HOST, "argus-reboot-check")],
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        try:
            with open(self.log) as fh:
                return fh.read()
        except FileNotFoundError:
            return ""

    def test_without_a_window_it_keeps_every_day_at_three(self):
        self.assertIn("systemctl reboot", self.run_check())

    def test_every_day_in_the_cores_zone(self):
        calls = self.run_check({"mode": "auto", "weekday": -1, "hour": 3, "minute": 0, "tz": "Europe/Rome"})
        self.assertIn("systemctl reboot", calls)
        self.assertIn("TZ=Europe/Rome", calls)
        with open(os.path.join(self.state, "reboot-result.json")) as fh:
            self.assertEqual(json.load(fh)["why"], "window")

    def test_another_day_or_hour_waits(self):
        self.assertNotIn("systemctl", self.run_check({"mode": "auto", "weekday": 1, "hour": 3, "minute": 0}))
        self.assertNotIn("systemctl", self.run_check({"mode": "auto", "weekday": 0, "hour": 4, "minute": 0}))

    def test_notify_only_never_reboots(self):
        self.assertNotIn("systemctl", self.run_check({"mode": "notify", "weekday": 0, "hour": 0, "minute": 0}))

    def test_no_reboot_without_the_os_asking(self):
        self.assertNotIn("systemctl", self.run_check({"mode": "auto", "weekday": -1, "hour": 3, "minute": 0}, needs_reboot=False))

    def test_an_unknown_zone_falls_back_to_the_vms_clock(self):
        calls = self.run_check({"mode": "auto", "weekday": -1, "hour": 3, "minute": 0, "tz": "Mars/Base"})
        self.assertIn("systemctl reboot", calls)
        self.assertNotIn("TZ=Mars", calls)

    def test_it_switches_off_unattended_upgrades_own_reboot(self):
        self.run_check(needs_reboot=False)
        with open(os.path.join(self.tmp, "53argus")) as fh:
            self.assertIn('Automatic-Reboot "false"', fh.read())

    def test_reboot_now_only_when_needed(self):
        stub_report = os.path.join(self.tmp, "report")
        open(os.path.join(self.state, "reboot-request.json"), "w").close()
        calls = self.run_check({"mode": "notify", "weekday": 0, "hour": 0, "minute": 0})
        self.assertIn("systemctl reboot", calls)
        self.assertFalse(os.path.exists(os.path.join(self.state, "reboot-request.json")))
        with open(os.path.join(self.state, "reboot-result.json")) as fh:
            self.assertEqual(json.load(fh)["state"], "rebooting")

    def test_reboot_now_without_a_need_reports_again(self):
        open(os.path.join(self.state, "reboot-request.json"), "w").close()
        calls = self.run_check({"mode": "notify", "weekday": 0, "hour": 0, "minute": 0}, needs_reboot=False)
        self.assertNotIn("systemctl", calls)
        self.assertIn("report", calls)
        with open(os.path.join(self.state, "reboot-result.json")) as fh:
            self.assertEqual(json.load(fh)["state"], "done")

    def test_a_newer_kernel_without_the_flag_reboots_in_the_window(self):
        open(self.report_says, "w").close()
        self.assertIn("systemctl reboot", self.run_check({"mode": "auto", "weekday": -1, "hour": 3, "minute": 0}, needs_reboot=False))
        os.remove(self.log)
        self.assertNotIn("systemctl", self.run_check({"mode": "auto", "weekday": 1, "hour": 3, "minute": 0}, needs_reboot=False))


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class PatchTest(unittest.TestCase):
    """argus-patch: the security updates, then a waiting DietPi release on a DietPi host."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.state = os.path.join(self.tmp, "state")
        self.bin = os.path.join(self.tmp, "bin")
        for d in (self.state, self.bin):
            os.makedirs(d)
        self.log = os.path.join(self.tmp, "calls").replace("\\", "/")
        self.flag = os.path.join(self.tmp, "update_available")
        self.stub("unattended-upgrade", 'echo uu >> "%s"; exit "${UU_EXIT:-0}"' % self.log)
        self.stub("dietpi-update", 'echo "dietpi-update $*" >> "%s"; exit "${DP_EXIT:-0}"' % self.log)
        self.stub("argus-os-report", ':')
        self.stub("logger", 'cat >/dev/null 2>&1; :')

    def stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w", newline="\n") as fh:
            fh.write("#!/usr/bin/env bash\n" + body + "\n")
        os.chmod(path, 0o755)

    def run_patch(self, dietpi=None, **env_extra):
        open(os.path.join(self.state, "patch-request.json"), "w").close()
        if dietpi is not None:
            with open(self.flag, "w", newline="\n") as fh:
                fh.write(dietpi)
        stubs = self.bin
        if os.name == "nt":
            stubs = subprocess.run(["cygpath", "-u", stubs], capture_output=True, text=True).stdout.strip()
        env = dict(os.environ, ARGUS_STUBS=stubs, ARGUS_STATE_DIR=self.state, ARGUS_DIETPI_FLAG=self.flag.replace("\\", "/"),
                   ARGUS_DIETPI_UPDATE=stubs + "/dietpi-update", ARGUS_OS_REPORT="argus-os-report", **env_extra)
        r = subprocess.run([shutil.which("bash"), "-c", 'PATH="$ARGUS_STUBS:$PATH" exec bash "$0"', os.path.join(HOST, "argus-patch")],
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.state, "patch-result.json")) as fh:
            res = json.load(fh)
        try:
            with open(self.log) as fh:
                calls = fh.read()
        except FileNotFoundError:
            calls = ""
        return res, calls

    def test_a_vm_runs_the_security_updates_only(self):
        res, calls = self.run_patch()
        self.assertEqual((res["state"], res["detail"]), ("done", "security updates applied"))
        self.assertNotIn("dietpi-update", calls)

    def test_a_dietpi_host_takes_the_waiting_release(self):
        res, calls = self.run_patch("10.9.0\n")
        self.assertIn("dietpi-update 1", calls)
        self.assertEqual((res["state"], res["detail"]), ("done", "security updates and DietPi 10.9.0 applied"))

    def test_a_failed_dietpi_update_is_a_failure(self):
        res, _ = self.run_patch("10.9.0\n", DP_EXIT="1")
        self.assertEqual(res["state"], "failed")
        self.assertIn("DietPi 10.9.0 was not", res["detail"])

    def test_a_failed_apt_run_is_a_failure(self):
        res, _ = self.run_patch(UU_EXIT="2")
        self.assertEqual(res["state"], "failed")


if __name__ == "__main__":
    unittest.main(verbosity=1)
