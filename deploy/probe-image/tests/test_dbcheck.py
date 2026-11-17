#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi
"""Tests for dbcheck.py: the proxy's local database after a power cut.

A healthy file is never touched; a damaged one (SQLite says so, it is no database, it is empty) is
set aside so the proxy can start; anything the check can't tell is left alone.
"""
import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import dbcheck  # noqa: E402


def make_db(path, rows=2000):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE proxy_history (id INTEGER PRIMARY KEY, itemid INTEGER, value TEXT)")
    con.execute("CREATE INDEX proxy_history_1 ON proxy_history (itemid)")
    con.executemany("INSERT INTO proxy_history (itemid, value) VALUES (?, ?)",
                    [(i % 50, "value %d " % i * 8) for i in range(rows)])
    con.commit()
    con.close()


def content(path):
    with open(path, "rb") as f:
        return f.read()


def quiet(fn, *a):
    """Run fn, return (its result, what it wrote to stderr)."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        out = fn(*a)
    return out, err.getvalue()


class DbCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "site1-proxy.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_file_yet_is_a_first_start(self):
        out, err = quiet(dbcheck.check, self.db)
        self.assertEqual(out, "none")
        self.assertEqual(err, "")
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_a_healthy_database_is_not_touched(self):
        make_db(self.db)
        before = content(self.db)
        out, err = quiet(dbcheck.check, self.db)
        self.assertEqual(out, "ok")
        self.assertEqual(err, "")
        self.assertEqual(content(self.db), before)
        self.assertFalse(os.path.exists(self.db + ".damaged"))

    def test_a_healthy_wal_database_is_not_touched(self):
        make_db(self.db)
        con = sqlite3.connect(self.db)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("INSERT INTO proxy_history (itemid, value) VALUES (1, 'x')")
        con.commit()
        con.close()
        out, _ = quiet(dbcheck.check, self.db)
        self.assertEqual(out, "ok")
        self.assertTrue(os.path.exists(self.db))

    def test_a_file_that_is_no_database_is_set_aside(self):
        with open(self.db, "wb") as f:
            f.write(b"this was a database before the power went" * 400)
        out, err = quiet(dbcheck.check, self.db)
        self.assertEqual(out, "aside")
        self.assertFalse(os.path.exists(self.db))
        self.assertTrue(os.path.exists(self.db + ".damaged"))
        self.assertIn("was damaged", err)
        self.assertIn("starts with a new one", err)

    def test_a_database_with_overwritten_pages_is_set_aside(self):
        make_db(self.db)
        size = os.path.getsize(self.db)
        with open(self.db, "r+b") as f:      # the header stays, the middle is lost
            f.seek(4096)
            f.write(os.urandom(size - 8192))
        state, why = dbcheck.verdict(self.db)
        self.assertEqual(state, "damaged", why)
        out, _ = quiet(dbcheck.check, self.db)
        self.assertEqual(out, "aside")
        self.assertFalse(os.path.exists(self.db))

    def test_a_cut_short_database_is_set_aside(self):
        make_db(self.db)
        with open(self.db, "r+b") as f:
            f.truncate(os.path.getsize(self.db) // 2)
        out, _ = quiet(dbcheck.check, self.db)
        self.assertEqual(out, "aside")

    def test_an_empty_file_is_set_aside(self):
        open(self.db, "wb").close()
        state, why = dbcheck.verdict(self.db)
        self.assertEqual((state, why), ("damaged", "it holds no tables"))
        out, _ = quiet(dbcheck.check, self.db)
        self.assertEqual(out, "aside")

    def test_the_side_files_go_with_it(self):
        with open(self.db, "wb") as f:
            f.write(b"\0" * 5000)
        for ext in ("-wal", "-shm", "-journal"):
            open(self.db + ext, "wb").close()
        out, _ = quiet(dbcheck.check, self.db)
        self.assertEqual(out, "aside")
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["site1-proxy.sqlite.damaged"])

    def test_only_the_newest_damaged_copy_is_kept(self):
        with open(self.db + ".damaged", "wb") as f:
            f.write(b"an older one")
        with open(self.db, "wb") as f:
            f.write(b"the newer one, not a database either" * 200)
        out, _ = quiet(dbcheck.check, self.db)
        self.assertEqual(out, "aside")
        self.assertTrue(content(self.db + ".damaged").startswith(b"the newer one"))
        self.assertEqual(os.listdir(self.tmp.name), ["site1-proxy.sqlite.damaged"])

    def test_a_folder_in_its_place_is_left_alone(self):
        os.mkdir(self.db)
        out, err = quiet(dbcheck.check, self.db)
        self.assertEqual(out, "left")
        self.assertTrue(os.path.isdir(self.db))
        self.assertIn("left as it is", err)

    def test_a_check_that_cannot_tell_leaves_the_file(self):
        make_db(self.db)
        real = dbcheck.subprocess.run

        def cannot_tell(*a, **kw):
            class R:
                returncode = dbcheck.UNKNOWN
                stdout = "disk I/O error"
                stderr = ""
            return R()
        dbcheck.subprocess.run = cannot_tell
        try:
            out, err = quiet(dbcheck.check, self.db)
        finally:
            dbcheck.subprocess.run = real
        self.assertEqual(out, "left")
        self.assertTrue(os.path.exists(self.db))
        self.assertIn("disk I/O error", err)

    def test_a_check_that_takes_too_long_leaves_the_file(self):
        make_db(self.db)
        real = dbcheck.subprocess.run

        def too_long(*a, **kw):
            raise dbcheck.subprocess.TimeoutExpired(cmd="dbcheck", timeout=1)
        dbcheck.subprocess.run = too_long
        try:
            out, err = quiet(dbcheck.check, self.db)
        finally:
            dbcheck.subprocess.run = real
        self.assertEqual(out, "left")
        self.assertTrue(os.path.exists(self.db))
        self.assertIn("took more than", err)

    def test_it_always_exits_zero(self):
        with open(self.db, "wb") as f:
            f.write(b"junk" * 2000)
        code, _ = quiet(dbcheck.main, ["dbcheck.py", self.db])
        self.assertEqual(code, 0)
        code, _ = quiet(dbcheck.main, ["dbcheck.py"])
        self.assertEqual(code, 0)

    def test_the_verdict_call_says_damaged_with_exit_3(self):
        with open(self.db, "wb") as f:
            f.write(b"junk" * 2000)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dbcheck.main(["dbcheck.py", "--verdict", self.db])
        self.assertEqual(code, dbcheck.DAMAGED)
        self.assertTrue(out.getvalue().strip())


if __name__ == "__main__":
    unittest.main()
