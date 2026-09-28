#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi

# argus-probe entrypoint: self-enroll on first boot, then hand off to the stock Zabbix proxy.
#
# On first boot (no certs on the data volume yet) it generates a keypair + CSR locally, redeems
# the single-use ARGUS_ENROLL_TOKEN against Argus (/api/enroll), and writes the signed cert +
# ca.crt. The private key never leaves this container. On later boots the certs already exist, so
# enrollment is skipped. Certs live under the mounted /var/lib/zabbix volume - keep it persistent,
# because the enrollment token is single-use.
set -eu

# Self-update helper roles (poll sidecar + one-shot recreate) no longer live in this image - they're
# provided by the shared argus-updater image (ghcr.io/g-guglielmi/argus-updater), driven below.

CERTS=/var/lib/zabbix/enroll
CA="$CERTS/ca.crt"
CRT="$CERTS/proxy.crt"
KEY="$CERTS/proxy.key"
META="$CERTS/proxy.env"

# proxy.env is data, never code. Its values come from Argus over the network (the enroll and
# check-in responses), so they are read one key at a time and checked against the shape each one
# must have before they are used or written back. A value that doesn't fit is dropped with a note,
# not executed and not fatal: the proxy keeps the last good value and keeps running.
read_kv() { [ -f "$META" ] && sed -n "s/^$1=//p" "$META" | head -n1; }
valid_name()  { case "$1" in ''|*[!A-Za-z0-9._-]*) return 1;; esac; }
valid_host()  { case "$1" in ''|*[!]A-Za-z0-9.:_[-]*) return 1;; esac; }
valid_token() { case "$1" in ''|*[!A-Za-z0-9._-]*) return 1;; esac; }
# The check-in carries the probe's long-lived token and brings back the core host it will dial, so
# it goes over https; plain http only when ARGUS_ALLOW_INSECURE_CHECKIN=true says so (a lab).
valid_url() {
  [ -n "$1" ] || return 1
  printf '%s' "$1" | grep -q '[^A-Za-z0-9.:/_%?=&-]' && return 1
  case "$1" in
    https://*) return 0;;
    http://*) [ "${ARGUS_ALLOW_INSECURE_CHECKIN:-}" = "true" ] && return 0
              echo "argus-probe: refusing the plain-http check-in URL $1 (set ARGUS_ALLOW_INSECURE_CHECKIN=true to allow it)" >&2; return 1;;
    *) return 1;;
  esac
}
# drop VAR LABEL - clear a value that failed its check, saying so.
drop() { echo "argus-probe: ignoring an unexpected $2 value from Argus" >&2; eval "$1=''"; }
write_meta() {
  printf 'PROXY_NAME=%s\nCORE_HOST=%s\nPROBE_TOKEN=%s\nCHECKIN_URL=%s\n' \
    "${PROXY_NAME:-}" "${CORE_HOST:-}" "${PROBE_TOKEN:-}" "${CHECKIN_URL:-}" > "$META"
  chmod 600 "$META" 2>/dev/null || true
}

mkdir -p "$CERTS"
if [ ! -f "$CRT" ] || [ ! -f "$KEY" ] || [ ! -f "$CA" ]; then
  : "${ARGUS_ENROLL_URL:?set ARGUS_ENROLL_URL to https://<argus-host>/api/enroll}"
  : "${ARGUS_ENROLL_TOKEN:?set ARGUS_ENROLL_TOKEN to the token from the Argus Probes page}"
  echo "argus-probe: enrolling against $ARGUS_ENROLL_URL"

  openssl req -newkey rsa:2048 -nodes -keyout "$KEY" -out /tmp/probe.csr -subj "/CN=proxy" >/dev/null 2>&1
  BODY=$(jq -n --arg t "$ARGUS_ENROLL_TOKEN" --arg c "$(cat /tmp/probe.csr)" '{token:$t, csr:$c}')
  # Capture body + HTTP status separately so a non-200 surfaces Argus's actual error message
  # (curl -f would hide it).
  HTTP=$(curl -sS -o /tmp/enroll.out -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d "$BODY" "$ARGUS_ENROLL_URL" || echo "000")
  RESP=$(cat /tmp/enroll.out 2>/dev/null || true)
  rm -f /tmp/probe.csr /tmp/enroll.out
  if [ "$HTTP" != "200" ]; then
    echo "argus-probe: enrollment failed (HTTP $HTTP): ${RESP:-<no response - Argus unreachable?>}" >&2
    rm -f "$KEY"
    exit 1
  fi

  echo "$RESP" | jq -er '.certificate' > "$CRT"
  echo "$RESP" | jq -er '.ca' > "$CA"
  PROXY_NAME=$(echo "$RESP" | jq -er '.proxy_name')
  if ! valid_name "$PROXY_NAME"; then
    echo "argus-probe: enrollment returned an unusable proxy name" >&2
    rm -f "$KEY" "$CRT" "$CA"
    exit 1
  fi
  CORE_HOST=$(echo "$RESP" | jq -r '.core_host // ""')
  # Long-lived check-in credential (fleet updates): report our version + read the fleet target.
  # Absent on older Argus servers - the probe simply won't participate in fleet updates then.
  PROBE_TOKEN=$(echo "$RESP" | jq -r '.probe_token // ""')
  CHECKIN_URL=$(echo "$RESP" | jq -r '.checkin_url // ""')
  [ -z "$CORE_HOST" ] || valid_host "$CORE_HOST" || drop CORE_HOST "core host"
  [ -z "$PROBE_TOKEN" ] || valid_token "$PROBE_TOKEN" || drop PROBE_TOKEN "token"
  [ -z "$CHECKIN_URL" ] || valid_url "$CHECKIN_URL" || CHECKIN_URL=""
  write_meta
  chmod 600 "$KEY"
  echo "argus-probe: enrolled as $PROXY_NAME (core: ${CORE_HOST:-<from ZBX_SERVER_HOST>})"
fi

# The stock entrypoint runs as root then drops to the zabbix user (and fixes the spool ownership
# itself); make sure the enrolled certs we wrote are readable by it too. Only the certificates and
# the key: the directory and proxy.env stay root's, so the proxy process (which never needs them)
# can't rewrite what this script and the updater sidecar read at their next start. Best-effort.
chown root:root "$CERTS" 2>/dev/null || true
chmod 755 "$CERTS" 2>/dev/null || true
chown zabbix:zabbix "$CA" "$CRT" "$KEY" 2>/dev/null || chown 1997:1997 "$CA" "$CRT" "$KEY" 2>/dev/null || true
chown root:root "$META" 2>/dev/null || true
chmod 600 "$META" 2>/dev/null || true

PROXY_NAME=$(read_kv PROXY_NAME)
CORE_HOST=$(read_kv CORE_HOST)
PROBE_TOKEN=$(read_kv PROBE_TOKEN)
CHECKIN_URL=$(read_kv CHECKIN_URL)
if ! valid_name "$PROXY_NAME"; then
  echo "argus-probe: $META has no usable PROXY_NAME - re-enrol this probe (clear the enroll dir and run with a new token)" >&2
  exit 1
fi
[ -z "$CORE_HOST" ] || valid_host "$CORE_HOST" || drop CORE_HOST "core host"
[ -z "$PROBE_TOKEN" ] || valid_token "$PROBE_TOKEN" || drop PROBE_TOKEN "token"
[ -z "$CHECKIN_URL" ] || valid_url "$CHECKIN_URL" || CHECKIN_URL=""

# --- fleet check-in credential (resolved here, while CORE_HOST still holds the enrolled value) ---
# A probe enrolled before fleet updates has no token in proxy.env; supply it once as
# ARGUS_PROBE_TOKEN (Argus "Enable reporting" mints it). An env-supplied token WINS (so you can
# rotate it) and is SAVED to proxy.env - so you can remove the env var on later runs and reporting
# keeps working. The check-in URL is derived from the enroll URL when not given.
PRIOR_TOKEN="${PROBE_TOKEN:-}"
if [ -n "${ARGUS_PROBE_TOKEN:-}" ]; then
  if valid_token "$ARGUS_PROBE_TOKEN"; then PROBE_TOKEN="$ARGUS_PROBE_TOKEN"; else echo "argus-probe: ARGUS_PROBE_TOKEN doesn't look like a token - ignored" >&2; fi
fi
if [ -n "${ARGUS_CHECKIN_URL:-}" ]; then
  if valid_url "$ARGUS_CHECKIN_URL"; then CHECKIN_URL="$ARGUS_CHECKIN_URL"; else echo "argus-probe: ARGUS_CHECKIN_URL ignored" >&2; fi
fi
if [ -z "${CHECKIN_URL:-}" ] && [ -n "${ARGUS_ENROLL_URL:-}" ]; then
  _derived=$(printf '%s' "$ARGUS_ENROLL_URL" | sed 's#/api/enroll#/api/probes/checkin#')
  if valid_url "$_derived"; then CHECKIN_URL="$_derived"; fi
fi
if [ -n "${PROBE_TOKEN:-}" ] && [ "${PROBE_TOKEN:-}" != "$PRIOR_TOKEN" ]; then
  write_meta
  echo "argus-probe: check-in credential saved to the data volume - you can remove ARGUS_PROBE_TOKEN now"
fi

# --- central core-host sync (fleet re-point) ---
# Re-fetch the core host from Argus at every start, so changing ARGUS_PROBE_CORE_HOST centrally
# re-points the whole fleet on the next restart - no re-enrollment. The check-in response carries
# the current core_host; we apply it to the baked value (an explicit ZBX_SERVER_HOST below still
# wins) and persist it so it survives a later Argus outage. Best-effort and fail-safe: any failure
# (older Argus, transient network, unset value) keeps the last known CORE_HOST, so nothing can
# strand the probe. Skipped when ZBX_SERVER_HOST already pins the host.
PROBE_VERSION="$(cat /etc/argus-probe.version 2>/dev/null || echo dev)"
if [ -z "${ZBX_SERVER_HOST:-}" ] && [ -n "${PROBE_TOKEN:-}" ] && [ -n "${CHECKIN_URL:-}" ]; then
  SYNC=$(curl -sS -m 15 \
    -H "Authorization: Bearer $PROBE_TOKEN" -H 'Content-Type: application/json' \
    -d "$(jq -nc --arg v "$PROBE_VERSION" '{version:$v}')" \
    "$CHECKIN_URL" 2>/dev/null || true)
  NEW_HOST=$(printf '%s' "$SYNC" | jq -r '.core_host // ""' 2>/dev/null || true)
  if [ -n "$NEW_HOST" ] && ! valid_host "$NEW_HOST"; then
    echo "argus-probe: ignoring an unexpected core host value from Argus (keeping ${CORE_HOST:-<unset>})" >&2
    NEW_HOST=""
  fi
  if [ -n "$NEW_HOST" ] && [ "$NEW_HOST" != "${CORE_HOST:-}" ]; then
    echo "argus-probe: core host updated by Argus: ${CORE_HOST:-<unset>} -> $NEW_HOST"
    CORE_HOST="$NEW_HOST"
    write_meta
  fi
fi

# An explicit ZBX_SERVER_HOST always wins (lets you re-point a probe without re-enrolling); else
# use the core host baked in at enrollment (or just refreshed from Argus above).
CORE_HOST="${ZBX_SERVER_HOST:-$CORE_HOST}"
if [ -z "$CORE_HOST" ]; then
  echo "argus-probe: no core host known - set ARGUS_PROBE_CORE_HOST in Argus or ZBX_SERVER_HOST here" >&2
  exit 1
fi

export ZBX_HOSTNAME="$PROXY_NAME"
export ZBX_SERVER_HOST="$CORE_HOST"
export ZBX_PROXYMODE=0
export ZBX_PROXYOFFLINEBUFFER="${ZBX_PROXYOFFLINEBUFFER:-168}"
export ZBX_PROXYLOCALBUFFER="${ZBX_PROXYLOCALBUFFER:-0}"
# ICMP pingers: Zabbix's default is one, and every Base Ping host's checks queue behind it while fping
# waits out slow or silent devices - a mid-size site already kept it ~60% busy. Five idle pingers cost
# a few MB and only fork fping when there's work. Zabbix can't scale them at runtime (StartPingers is
# read at start); an explicit ZBX_STARTPINGERS still wins.
export ZBX_STARTPINGERS="${ZBX_STARTPINGERS:-5}"
export ZBX_TLSCONNECT=cert
export ZBX_TLSACCEPT=cert
export ZBX_TLSCAFILE="$CA"
export ZBX_TLSCERTFILE="$CRT"
export ZBX_TLSKEYFILE="$KEY"
export ZBX_TLSSERVERCERTISSUER="${ZBX_TLSSERVERCERTISSUER:-CN=Monitoring Core CA}"
export ZBX_TLSSERVERCERTSUBJECT="${ZBX_TLSSERVERCERTSUBJECT:-CN=zabbix-core}"

# Fleet check-in reporter: every minute, report our running version (+ the network-scan capability)
# to Argus and receive the fleet target. Report-only (no Docker socket); the opt-in self-updater is
# a separate sidecar. Runs as a background child so the Zabbix proxy stays PID 1. Best-effort: any
# failure (older Argus, transient network) is ignored and retried next tick. The check-in
# credential (PROBE_TOKEN / CHECKIN_URL) and PROBE_VERSION were resolved above.

# The proxy is a pure reporter: it reports its running version to Argus and reads the fleet target
# for drift visibility, but it never touches Docker. Self-update is done by the separate
# argus-updater sidecar (probe-watch mode), which holds the socket - so the proxy never does, and it
# needs no docker-cli. The sidecar is what advertises self-update capability to Argus; we only report
# our version (Argus keeps the stored capability flag when a check-in omits it).
#
# Network discovery piggybacks on the same channel: "scans":true / "sweeps":true advertise the
# capabilities, and a response may carry ONE one-shot job queued by an Argus admin - either a
# .scan ({id, cidr, snmp}, subnet scan) or a .sweep ({id, url, key}, UniFi controller sweep).
# Each runs in a backgrounded python3 so this tick is never blocked; a lock file per kind keeps
# runs serial, and both scripts POST their results straight back to Argus
# (<checkin base>/scan-results). An older Argus simply never sends .scan/.sweep - inert.
if [ -n "${PROBE_TOKEN:-}" ] && [ -n "${CHECKIN_URL:-}" ]; then
  echo "argus-probe: fleet check-in enabled -> $CHECKIN_URL (version $PROBE_VERSION)"
  (
    # A short initial delay lets the proxy come up before the first report.
    sleep 20
    LOCK="$CERTS/netscan.lock"
    JOBFILE="$CERTS/scanjob.json"
    SWLOCK="$CERTS/unifisweep.lock"
    SWJOBFILE="$CERTS/sweepjob.json"
    umask 077   # a job file carries an SNMP community or a controller key
    while true; do
      RESP=$(curl -sS -m 15 \
        -H "Authorization: Bearer $PROBE_TOKEN" -H 'Content-Type: application/json' \
        -d "$(jq -nc --arg v "$PROBE_VERSION" '{version:$v, scans:true, sweeps:true}')" \
        "$CHECKIN_URL" 2>/dev/null || true)
      JOB=$(printf '%s' "$RESP" | jq -c '.scan // empty' 2>/dev/null || true)
      if [ -n "$JOB" ]; then
        # A lock older than the scanner's own 8-minute budget is a crashed run - clear it.
        if [ -f "$LOCK" ] && [ -n "$(find "$LOCK" -mmin +15 2>/dev/null)" ]; then rm -f "$LOCK"; fi
        if [ ! -f "$LOCK" ]; then
          touch "$LOCK"
          printf '%s' "$JOB" > "$JOBFILE"
          echo "argus-probe: network scan requested by Argus: $(printf '%s' "$JOB" | jq -r '.cidr // "?"' 2>/dev/null || echo '?')"
          (
            ARGUS_PROBE_TOKEN="$PROBE_TOKEN" ARGUS_CHECKIN_URL="$CHECKIN_URL" \
              python3 /usr/lib/zabbix/externalscripts/argus_netscan.py --job "$JOBFILE" || true
            rm -f "$LOCK" "$JOBFILE"
          ) &
        fi
      fi
      SWEEP=$(printf '%s' "$RESP" | jq -c '.sweep // empty' 2>/dev/null || true)
      if [ -n "$SWEEP" ]; then
        # A sweep is a handful of HTTPS calls; a lock this old is a crashed run - clear it.
        if [ -f "$SWLOCK" ] && [ -n "$(find "$SWLOCK" -mmin +15 2>/dev/null)" ]; then rm -f "$SWLOCK"; fi
        if [ ! -f "$SWLOCK" ]; then
          touch "$SWLOCK"
          printf '%s' "$SWEEP" > "$SWJOBFILE"
          echo "argus-probe: UniFi sweep requested by Argus: $(printf '%s' "$SWEEP" | jq -r '.url // "?"' 2>/dev/null || echo '?')"
          (
            ARGUS_PROBE_TOKEN="$PROBE_TOKEN" ARGUS_CHECKIN_URL="$CHECKIN_URL" \
              python3 /usr/lib/zabbix/externalscripts/argus_unifi_sweep.py --job "$SWJOBFILE" || true
            rm -f "$SWLOCK" "$SWJOBFILE"
          ) &
        fi
      fi
      sleep 60
    done
  ) &
fi

# The base Zabbix entrypoint unconditionally copies /var/lib/zabbix/ssl/ssl_ca into ssl_ca_internal
# for HTTPS-based checks and aborts if the source dir is missing (`cp: can't stat ...ssl_ca/.`) - which
# crash-loops the proxy on any data volume created before that dir existed. Create the SSL dirs
# (empty is fine; the copy of an empty dir is a no-op) so the handoff never fails.
mkdir -p /var/lib/zabbix/ssl/ssl_ca /var/lib/zabbix/ssl/ssl_ca_internal \
         /var/lib/zabbix/ssl/certs /var/lib/zabbix/ssl/keys 2>/dev/null || true

echo "argus-probe: starting Zabbix proxy '$ZBX_HOSTNAME' -> $ZBX_SERVER_HOST:10051"
exec /usr/bin/docker-entrypoint.sh "$@"
