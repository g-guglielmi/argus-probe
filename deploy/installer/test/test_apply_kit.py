#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi
"""apply-kit-lines.awk sets a kit's lines in dietpi.txt by the same rule as Argus's patchDietpiTxt:
set keys are replaced (their first line), commented examples get the line after them, missing keys
go at the end, everything else and the line endings stay. Runs every awk on the PATH it finds
(busybox's is the one on the stick)."""
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AWK = os.path.join(HERE, "apply-kit-lines.awk")


def awks():
    out = []
    for name in ("awk", "gawk", "mawk"):
        p = shutil.which(name)
        if p and p not in [x[1] for x in out]:
            out.append((name, [p]))
    if shutil.which("busybox"):
        out.append(("busybox awk", [shutil.which("busybox"), "awk"]))
    return out


class ApplyKitTest(unittest.TestCase):
    def apply(self, awk, text, lines):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        txt, kit = os.path.join(tmp, "dietpi.txt"), os.path.join(tmp, "kit.txt")
        with open(txt, "w", newline="") as fh:
            fh.write(text)
        with open(kit, "w", newline="") as fh:
            fh.write(lines)
        # awk reads escapes in -v values: a Windows path goes with forward slashes.
        r = subprocess.run(awk + ["-v", "kit=" + kit.replace("\\", "/"), "-f", AWK, txt, txt], capture_output=True)
        return r.returncode, r.stdout.decode()

    def test_the_rule(self):
        text = ("# comment\nAUTO_SETUP_AUTOMATED=0\n#AUTO_SETUP_INSTALL_SOFTWARE_ID=17\n"
                "AUTO_SETUP_TIMEZONE=Europe/London\nAUTO_SETUP_TIMEZONE=again\nOTHER=1\n")
        lines = "AUTO_SETUP_AUTOMATED=1\nAUTO_SETUP_INSTALL_SOFTWARE_ID=docker\nAUTO_SETUP_TIMEZONE=Europe/Rome\nSURVEY_OPTED_IN=0\n"
        want = ("# comment\nAUTO_SETUP_AUTOMATED=1\n#AUTO_SETUP_INSTALL_SOFTWARE_ID=17\nAUTO_SETUP_INSTALL_SOFTWARE_ID=docker\n"
                "AUTO_SETUP_TIMEZONE=Europe/Rome\nAUTO_SETUP_TIMEZONE=again\nOTHER=1\nSURVEY_OPTED_IN=0\n")
        for name, awk in awks():
            with self.subTest(awk=name):
                self.assertEqual(self.apply(awk, text, lines), (0, want))

    @unittest.skipIf(os.name == "nt", "Git Bash's awk drops the CR on reading; the stick's busybox doesn't")
    def test_crlf_stays_crlf(self):
        for name, awk in awks():
            with self.subTest(awk=name):
                rc, out = self.apply(awk, "A=0\r\n#B=1\r\n", "A=1\r\nB=2\r\nC=3\n")
                self.assertEqual((rc, out), (0, "A=1\r\n#B=1\r\nB=2\r\nC=3\r\n"))

    def test_a_set_key_beats_its_example(self):
        for name, awk in awks():
            with self.subTest(awk=name):
                self.assertEqual(self.apply(awk, "#K=example\nK=old\n", "K=new\n"), (0, "#K=example\nK=new\n"))

    def test_an_empty_kit_is_refused(self):
        for name, awk in awks():
            with self.subTest(awk=name):
                self.assertNotEqual(self.apply(awk, "A=0\n", "# nothing\n")[0], 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
