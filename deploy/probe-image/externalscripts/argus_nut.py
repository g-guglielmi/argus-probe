#!/usr/bin/env python3
# argus_nut.py - Argus NUT collector (Zabbix external check).
#
# MIRRORED, keep byte-identical:
#   argus-probe: deploy/probe-image/externalscripts/argus_nut.py  (baked into the proxy image)
#   argus-core:  deploy/core/externalscripts/argus_nut.py         (installed on the core by setup-core.sh)
# Runs on whichever Zabbix instance monitors the UPS host - a proxy, or the core server directly
# (no-proxy deployments / "Monitored by: Core server").
#
# Speaks the NUT (Network UPS Tools) network protocol to upsd directly and prints the UPS variables
# Argus's "Argus UPS by NUT" template reads, as one JSON object. Keeping the whole poll in one master
# item means dependent items parse it with JSONPath - no per-variable connections.
#
# Usage (as Zabbix runs it): argus_nut.py <host> [port] [ups] [user] [pass]
#   host  - upsd host (the template passes {HOST.CONN})
#   port  - upsd port (default 3493)
#   ups   - UPS name in upsd (default "ups")
#   user/pass - only if this upsd requires a login to read (most allow anonymous LIST VAR)
#
# A connection/protocol failure is NOT an error: it prints reachable=0 so the template's down trigger
# fires (max(nut.reachable,#3)=0) instead of the item going unsupported. Only bad arguments do.
import sys
import json
import socket

OUT = {
    "reachable": 0, "status": "", "charge": None, "runtime": None,
    "load": None, "input_voltage": None, "output_voltage": None, "realpower": 0,
    "on_battery": 0, "low_battery": 0,
}


def emit():
    print(json.dumps(OUT))


def to_num(vars_, key):
    try:
        return float(vars_[key])
    except (KeyError, ValueError, TypeError):
        return None


def main():
    if len(sys.argv) < 2 or not sys.argv[1]:
        sys.stderr.write("usage: argus_nut.py <host> [port] [ups] [user] [pass]\n")
        sys.exit(1)
    host = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else 3493
    ups = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else "ups"
    user = sys.argv[4] if len(sys.argv) > 4 else ""
    passwd = sys.argv[5] if len(sys.argv) > 5 else ""

    vars_ = {}
    try:
        sock = socket.create_connection((host, port), timeout=5)
        sock.settimeout(5)
        conn = sock.makefile("rw", encoding="utf-8", newline="\n")

        def send(cmd):
            conn.write(cmd + "\n")
            conn.flush()

        # Optional login for the rare upsd that restricts read access.
        if user:
            send("USERNAME " + user)
            conn.readline()
            send("PASSWORD " + passwd)
            conn.readline()

        send("LIST VAR " + ups)
        first = conn.readline()
        if not first.startswith("BEGIN LIST VAR"):
            # Reached upsd but it refused (e.g. unknown UPS, access denied): reachable, no vars.
            OUT["reachable"] = 1
            try:
                send("LOGOUT")
            except Exception:
                pass
            sock.close()
            emit()
            return
        while True:
            line = conn.readline()
            if not line:
                break
            line = line.rstrip("\n")
            if line.startswith("END LIST VAR"):
                break
            if line.startswith("VAR "):
                # VAR <ups> <name> "<value>"
                rest = line[4:]
                sp = rest.find(" ")                 # drop the ups name
                if sp < 0:
                    continue
                rest = rest[sp + 1:]
                sp = rest.find(" ")                 # split name / value
                if sp < 0:
                    continue
                name = rest[:sp]
                val = rest[sp + 1:].strip()
                if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                    val = val[1:-1]
                vars_[name] = val
        try:
            send("LOGOUT")
            conn.readline()
        except Exception:
            pass
        sock.close()
    except Exception:
        emit()   # unreachable -> reachable stays 0
        return

    OUT["reachable"] = 1
    OUT["charge"] = to_num(vars_, "battery.charge")
    OUT["runtime"] = to_num(vars_, "battery.runtime")
    OUT["load"] = to_num(vars_, "ups.load")
    OUT["input_voltage"] = to_num(vars_, "input.voltage")
    OUT["output_voltage"] = to_num(vars_, "output.voltage")
    rp = to_num(vars_, "ups.realpower")
    OUT["realpower"] = rp if rp is not None else 0
    status = vars_.get("ups.status", "") or ""
    OUT["status"] = status
    toks = status.upper().split()
    OUT["on_battery"] = 1 if "OB" in toks else 0
    OUT["low_battery"] = 1 if "LB" in toks else 0
    emit()


if __name__ == "__main__":
    main()
