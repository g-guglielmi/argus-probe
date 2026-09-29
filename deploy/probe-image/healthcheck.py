#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi

"""Container health for the argus-probe image (Docker HEALTHCHECK, shown by the Unraid GUI and
Dockhand): healthy while the Zabbix proxy runs and accepts connections on its listen port.

Exit 0 = healthy, 1 = unhealthy, with one line saying why. Whether the proxy can reach the core is
not part of it: that is the network's or the core's fault, not this container's, and Argus already
alerts on it ("Probe unreachable"); marking the container unhealthy for it would only invite a
restart that can't help. Standard library only.
"""

import os
import socket
import sys


def proxy_running():
    """True when a zabbix_proxy process exists (the entrypoint execs into it as PID 1)."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                argv0 = f.read().split(b"\0", 1)[0]
        except OSError:
            continue
        if argv0.endswith(b"zabbix_proxy"):
            return True
    return False


def listen_address():
    """Where the proxy's trapper listens: ZBX_LISTENIP (first entry) or the loopback, and
    ZBX_LISTENPORT or 10051. Only the container's own environment is visible here."""
    host = (os.environ.get("ZBX_LISTENIP") or "").split(",")[0].strip()
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    port = (os.environ.get("ZBX_LISTENPORT") or "10051").strip()
    if not port.isdigit() or not 0 < int(port) < 65536:
        port = "10051"
    return host, int(port)


def main():
    if not proxy_running():
        print("unhealthy: the Zabbix proxy process is not running")
        return 1
    host, port = listen_address()
    try:
        with socket.create_connection((host, port), timeout=3):
            pass
    except OSError as e:
        print("unhealthy: the Zabbix proxy does not accept connections on %s:%d (%s)" % (host, port, e))
        return 1
    print("healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
