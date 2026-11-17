#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi
"""dbcheck.py - the proxy's local database after a power cut.

The Zabbix proxy keeps its configuration copy and the values it has not sent yet in one SQLite file
(/var/lib/zabbix/db_data/<proxy name>.sqlite), and opens it with synchronous=0: nothing it writes is
forced to the disk, so a power cut in the middle of a write can leave the file damaged. A proxy with
a damaged file does not come back by itself: it stops at start, at every start, until someone
removes the file by hand on the probe's host.

Nothing in that file is worth that: the configuration comes from the core again within seconds, and
the only loss is the values not yet sent. So the entrypoint runs this before it starts the proxy:

    dbcheck.py DBFILE

  - no file yet (a first start): nothing to do;
  - the file checks out (PRAGMA quick_check, and it holds tables): nothing to do;
  - the file is damaged (SQLite says so, it is not a database at all, or it is empty): it is set
    aside as DBFILE.damaged (one copy, the newest) with its -wal/-shm/-journal files removed, and
    the proxy starts with a new one;
  - anything else (it can't be opened, the disk gives errors, the check takes too long): it is left
    as it is and said so. Only damage is acted on.

The check runs as the file's owner, so the files SQLite writes beside it while it reads stay the
proxy's; the move is done as whoever runs this (root, in the entrypoint). It always exits 0: a
failed check must never be the reason a proxy doesn't start.

    dbcheck.py --verdict DBFILE     the check alone: exit 0 fine, 3 damaged, 4 can't tell (the
                                    reason on stdout). What the call above runs as the owner.
"""
import os
import sqlite3
import stat
import subprocess
import sys

DAMAGED = 3
UNKNOWN = 4
# SQLite's primary result codes for "the file's content is wrong": SQLITE_CORRUPT, SQLITE_NOTADB.
CORRUPT_CODES = (11, 26)
TIMEOUT = int(os.environ.get("ARGUS_DB_CHECK_TIMEOUT", "120") or 120)


def say(msg):
    sys.stderr.write("argus-probe: %s\n" % msg)
    sys.stderr.flush()


def verdict(path):
    """('ok' | 'damaged' | 'unknown', why) for the SQLite file at path."""
    try:
        con = sqlite3.connect(path, timeout=10)
    except sqlite3.Error as e:
        return "unknown", str(e)
    try:
        rows = con.execute("PRAGMA quick_check(1)").fetchall()
        if [tuple(r) for r in rows] != [("ok",)]:
            return "damaged", "; ".join(str(r[0]) for r in rows)[:200] or "the check gave no answer"
        if con.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0] == 0:
            return "damaged", "it holds no tables"
        return "ok", ""
    except sqlite3.DatabaseError as e:
        msg = str(e)
        code = getattr(e, "sqlite_errorcode", None)
        if (code is not None and (code & 0xFF) in CORRUPT_CODES) or "malformed" in msg or "not a database" in msg:
            return "damaged", msg
        return "unknown", msg
    finally:
        con.close()


def check(path):
    """Check the file as its owner; set a damaged one aside. Returns what happened, for the tests:
    'none' (no file), 'ok', 'aside', 'left' (can't tell, or the move failed)."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return "none"
    except OSError as e:
        say("could not look at the proxy's local database %s (%s); left as it is" % (path, e))
        return "left"
    if not stat.S_ISREG(st.st_mode):
        say("the proxy's local database %s is not a file; left as it is" % path)
        return "left"
    as_owner = {}
    if getattr(os, "geteuid", lambda: 1)() == 0 and st.st_uid != 0:
        as_owner = {"user": st.st_uid, "group": st.st_gid, "extra_groups": []}
    try:
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "--verdict", path],
                           capture_output=True, text=True, timeout=TIMEOUT, **as_owner)
    except subprocess.TimeoutExpired:
        say("checking the proxy's local database took more than %d s; left as it is" % TIMEOUT)
        return "left"
    except Exception as e:  # the check must never stop the proxy from starting
        say("could not check the proxy's local database (%s); left as it is" % e)
        return "left"
    why = " ".join((r.stdout or "").split())[:200]
    if r.returncode == 0:
        return "ok"
    if r.returncode != DAMAGED:
        tail = why or " ".join((r.stderr or "").split())[-200:] or "exit code %d" % r.returncode
        say("could not check the proxy's local database (%s); left as it is" % tail)
        return "left"
    aside = path + ".damaged"
    try:
        os.replace(path, aside)
    except OSError as e:
        say("the proxy's local database is damaged (%s) and could not be set aside (%s): "
            "the proxy may not start until %s is removed" % (why, e, path))
        return "left"
    for ext in ("-wal", "-shm", "-journal"):
        try:
            os.remove(path + ext)
        except OSError:
            pass
    say("the proxy's local database was damaged (%s), most likely by a power cut. It was set aside as %s "
        "and the proxy starts with a new one: the values it had not sent to the core yet are lost, "
        "nothing else" % (why, aside))
    return "aside"


def main(argv):
    if len(argv) == 3 and argv[1] == "--verdict":
        state, why = verdict(argv[2])
        if why:
            print(why)
        return {"ok": 0, "damaged": DAMAGED}.get(state, UNKNOWN)
    if len(argv) != 2:
        say("usage: dbcheck.py DBFILE")
        return 0
    check(argv[1])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
