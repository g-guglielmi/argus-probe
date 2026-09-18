#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi

# argus_linux_ssh.py - Argus agentless-Linux collector (Zabbix external check).
#
# MIRRORED, keep byte-identical:
#   argus-probe: deploy/probe-image/externalscripts/argus_linux_ssh.py  (baked into the proxy image)
#   argus-core:  deploy/core/externalscripts/argus_linux_ssh.py         (installed on the core by setup-core.sh)
# Runs on whichever Zabbix instance monitors the host - a proxy, or the core server directly
# (no-proxy deployments / "Monitored by: Core server").
#
# The no-SNMP, no-agent fallback for a Linux box you can only reach over SSH. Opens ONE SSH session
# per poll, runs a single POSIX-sh snippet that dumps /proc + df + /proc/net/dev, and prints the whole
# reading Argus's "Argus Linux by SSH" template reads as one JSON object. Keeping the poll in one
# master item means the dependent items parse it with JSONPath (native item keys, so curation is
# shared with the SNMP/agent Linux classes) and the filesystems / interfaces come through LLD - all
# from a single login instead of one connection per item.
#
# Needs the system `ssh` client on the collector host (openssh-client), and `sshpass` for password
# auth (both baked into the argus-probe image / installed on the core by setup-core.sh). Stdlib only.
#
# Usage (as Zabbix runs it): argus_linux_ssh.py <host> <user> <port> <auth> <password> <keyfile>
#   host      - the Linux box (the template passes {HOST.CONN})
#   user      - SSH login user (a read-only account is enough; default "root")
#   port      - SSH port (default 22)
#   auth      - "key" or "password" (default "key")
#   password  - the SSH password, for auth=password (passed to sshpass via the environment, never argv)
#   keyfile   - path ON THIS COLLECTOR to the private key, for auth=key
#
# A connection/auth/parse failure is NOT an error: it prints reachable=0 so the template's down
# trigger fires (max(linux.ssh.reachable,#3)=0) instead of the items going unsupported. Only bad
# arguments exit non-zero.
import sys
import os
import json
import subprocess

# One remote snippet, POSIX sh so it runs on any Linux (bash not required). Sections are fenced with
# @@TAGs the parser splits on. Two /proc/stat samples one second apart give a real CPU-utilisation
# delta; everything else is a single read. Kept deliberately small and read-only.
REMOTE = (
    "echo @@STAT1; cat /proc/stat 2>/dev/null; "
    "sleep 1; "
    "echo @@STAT2; cat /proc/stat 2>/dev/null; "
    "echo @@CPUN; nproc 2>/dev/null || grep -c '^processor' /proc/cpuinfo 2>/dev/null; "
    "echo @@MEM; cat /proc/meminfo 2>/dev/null; "
    "echo @@LOAD; cat /proc/loadavg 2>/dev/null; "
    "echo @@UP; cat /proc/uptime 2>/dev/null; "
    "echo @@DF; df -P -B1 2>/dev/null; "
    "echo @@NET; cat /proc/net/dev 2>/dev/null; "
    "echo @@END"
)

# Filesystem types / mount roots that are never real storage - filtered here so the LLD only ever
# sees actual mounts (the template's {$FS.NAME.SKIP} macro can trim further per host).
SKIP_FS_SRC = ("tmpfs", "devtmpfs", "overlay", "shm", "none", "udev", "cgroup", "cgroup2")
SKIP_MOUNT_PREFIX = ("/proc", "/sys", "/dev", "/run", "/var/lib/docker/")

OUT = {
    "reachable": 0, "authed": 0,
    "cpu_util": None, "cpu_cores": None,
    "load1": None, "load5": None, "load15": None,
    "mem_total": None, "mem_available": None, "mem_free": None,
    "mem_buffers": None, "mem_cached": None, "mem_util": None,
    "uptime": None,
    "fs": [], "net": [],
    "fs_discovery": [], "net_discovery": [],
}


def emit():
    print(json.dumps(OUT))


def ssh_command(host, user, port, auth, keyfile):
    common = [
        "-o", "ConnectTimeout=8",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=" + known_hosts_path(),
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2",
        "-o", "NumberOfPasswordPrompts=1",
        "-p", str(port),
    ]
    target = "%s@%s" % (user, host)
    if auth == "password":
        # sshpass -e reads $SSHPASS from the environment, so the password never appears in argv or ps.
        # No BatchMode here: it suppresses the password prompt, which is exactly what sshpass feeds -
        # sshpass keeps the login non-interactive instead (NumberOfPasswordPrompts=1 caps a bad login).
        return ["sshpass", "-e", "ssh",
                "-o", "PubkeyAuthentication=no",
                "-o", "PreferredAuthentications=password,keyboard-interactive"] + \
            common + [target, REMOTE]
    # Key auth is genuinely non-interactive, so BatchMode=yes here fails fast instead of ever prompting.
    return ["ssh",
            "-i", keyfile,
            "-o", "BatchMode=yes",
            "-o", "PasswordAuthentication=no",
            "-o", "PreferredAuthentications=publickey"] + \
        common + [target, REMOTE]


def known_hosts_path():
    # accept-new needs a writable known_hosts; prefer a persistent per-collector store, fall back to a
    # throwaway so a read-only home never blocks the poll.
    for d in ("/var/lib/zabbix/ssh", "/tmp"):
        try:
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, "argus_known_hosts")
            open(p, "a").close()
            return p
        except OSError:
            continue
    return "/dev/null"


def parse_stat(block):
    # The aggregate "cpu " line: user nice system idle iowait irq softirq steal ...
    for line in block.splitlines():
        if line.startswith("cpu "):
            vals = [int(x) for x in line.split()[1:] if x.isdigit()]
            if len(vals) < 4:
                return None
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
            total = sum(vals)
            return total, idle
    return None


def parse_meminfo(block):
    m = {}
    for line in block.splitlines():
        parts = line.split(":")
        if len(parts) != 2:
            continue
        num = parts[1].strip().split()
        if num and num[0].isdigit():
            m[parts[0].strip()] = int(num[0]) * 1024  # kB -> bytes
    return m


def parse_df(block):
    fs, disc = [], []
    for line in block.splitlines()[1:]:  # drop the header
        f = line.split()
        if len(f) < 6:
            continue
        src, total, used, _avail, _pct, mount = f[0], f[1], f[2], f[3], f[4], f[5]
        if src in SKIP_FS_SRC:
            continue
        if any(mount == p or mount.startswith(p) for p in SKIP_MOUNT_PREFIX):
            continue
        try:
            t, u = int(total), int(used)
        except ValueError:
            continue
        if t <= 0:
            continue
        pused = round(100.0 * u / t, 4)
        fs.append({"name": mount, "total": t, "used": u, "pused": pused})
        disc.append({"{#FSNAME}": mount})
    return fs, disc


def parse_net(block):
    net, disc = [], []
    for line in block.splitlines():
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        name = name.strip()
        if name == "lo" or not name:
            continue
        cols = rest.split()
        if len(cols) < 16:
            continue
        try:
            rx, tx = int(cols[0]), int(cols[8])
        except ValueError:
            continue
        net.append({"name": name, "in": rx, "out": tx})
        disc.append({"{#IFNAME}": name})
    return net, disc


def sections(text):
    out, cur, buf = {}, None, []
    for line in text.splitlines():
        if line.startswith("@@"):
            if cur is not None:
                out[cur] = "\n".join(buf)
            cur, buf = line[2:], []
        else:
            buf.append(line)
    if cur is not None:
        out[cur] = "\n".join(buf)
    return out


def main():
    if len(sys.argv) < 2 or not sys.argv[1]:
        sys.stderr.write("usage: argus_linux_ssh.py <host> <user> <port> <auth> <password> <keyfile>\n")
        sys.exit(1)
    host = sys.argv[1]
    user = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else "root"
    port = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else "22"
    auth = (sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else "key").lower()
    passwd = sys.argv[5] if len(sys.argv) > 5 else ""
    keyfile = sys.argv[6] if len(sys.argv) > 6 and sys.argv[6] else "/var/lib/zabbix/ssh/argus_id"

    cmd = ssh_command(host, user, port, auth, keyfile)
    env = dict(os.environ)
    if auth == "password":
        env["SSHPASS"] = passwd
    try:
        proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=25)
    except Exception:
        emit()          # ssh/sshpass missing or timed out -> unreachable
        return
    text = proc.stdout.decode("utf-8", "replace")
    sec = sections(text)
    if "END" not in sec:
        # No clean end marker: the session did not complete (auth failure, refused, dropped).
        OUT["reachable"] = 1 if proc.returncode in (0, 1) and text.strip() else 0
        emit()
        return

    OUT["reachable"] = 1
    OUT["authed"] = 1

    s1 = parse_stat(sec.get("STAT1", ""))
    s2 = parse_stat(sec.get("STAT2", ""))
    if s1 and s2:
        dt = s2[0] - s1[0]
        di = s2[1] - s1[1]
        if dt > 0:
            OUT["cpu_util"] = round(100.0 * (dt - di) / dt, 2)

    cpun = sec.get("CPUN", "").strip().split()
    if cpun and cpun[0].isdigit():
        OUT["cpu_cores"] = int(cpun[0])

    mem = parse_meminfo(sec.get("MEM", ""))
    total = mem.get("MemTotal")
    avail = mem.get("MemAvailable")
    OUT["mem_total"] = total
    OUT["mem_available"] = avail
    OUT["mem_free"] = mem.get("MemFree")
    OUT["mem_buffers"] = mem.get("Buffers")
    OUT["mem_cached"] = mem.get("Cached")
    if total and avail is not None and total > 0:
        OUT["mem_util"] = round(100.0 * (total - avail) / total, 2)

    load = sec.get("LOAD", "").strip().split()
    if len(load) >= 3:
        try:
            OUT["load1"], OUT["load5"], OUT["load15"] = float(load[0]), float(load[1]), float(load[2])
        except ValueError:
            pass

    up = sec.get("UP", "").strip().split()
    if up:
        try:
            OUT["uptime"] = int(float(up[0]))
        except ValueError:
            pass

    OUT["fs"], OUT["fs_discovery"] = parse_df(sec.get("DF", ""))
    OUT["net"], OUT["net_discovery"] = parse_net(sec.get("NET", ""))
    emit()


if __name__ == "__main__":
    main()
