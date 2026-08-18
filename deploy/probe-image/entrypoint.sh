#!/bin/sh
# argus-probe entrypoint: self-enroll on first boot, then hand off to the stock Zabbix proxy.
#
# On first boot (no certs on the data volume yet) it generates a keypair + CSR locally, redeems
# the single-use ARGUS_ENROLL_TOKEN against Argus (/api/enroll), and writes the signed cert +
# ca.crt. The private key never leaves this container. On later boots the certs already exist, so
# enrollment is skipped. Certs live under the mounted /var/lib/zabbix volume — keep it persistent,
# because the enrollment token is single-use.
set -eu

# Role switch: an opt-in "updater" sidecar runs the self-update loop instead of the Zabbix proxy.
# It never enrolls — it reads the proxy's check-in credential from the shared volume.
if [ "${ARGUS_PROBE_ROLE:-proxy}" = "updater" ]; then
  exec /usr/local/bin/argus-updater.sh
fi

CERTS=/var/lib/zabbix/enroll
CA="$CERTS/ca.crt"
CRT="$CERTS/proxy.crt"
KEY="$CERTS/proxy.key"
META="$CERTS/proxy.env"

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
    echo "argus-probe: enrollment failed (HTTP $HTTP): ${RESP:-<no response — Argus unreachable?>}" >&2
    rm -f "$KEY"
    exit 1
  fi

  echo "$RESP" | jq -er '.certificate' > "$CRT"
  echo "$RESP" | jq -er '.ca' > "$CA"
  PROXY_NAME=$(echo "$RESP" | jq -er '.proxy_name')
  CORE_HOST=$(echo "$RESP" | jq -r '.core_host // ""')
  # Long-lived check-in credential (fleet updates): report our version + read the fleet target.
  # Absent on older Argus servers — the probe simply won't participate in fleet updates then.
  PROBE_TOKEN=$(echo "$RESP" | jq -r '.probe_token // ""')
  CHECKIN_URL=$(echo "$RESP" | jq -r '.checkin_url // ""')
  printf 'PROXY_NAME=%s\nCORE_HOST=%s\nPROBE_TOKEN=%s\nCHECKIN_URL=%s\n' \
    "$PROXY_NAME" "$CORE_HOST" "$PROBE_TOKEN" "$CHECKIN_URL" > "$META"
  chmod 600 "$KEY" "$META"
  echo "argus-probe: enrolled as $PROXY_NAME (core: ${CORE_HOST:-<from ZBX_SERVER_HOST>})"
fi

# The stock entrypoint runs as root then drops to the zabbix user (and fixes the spool ownership
# itself); make sure the enrolled certs we wrote are readable by it too. Best-effort.
chown -R zabbix:zabbix "$CERTS" 2>/dev/null || chown -R 1997:1997 "$CERTS" 2>/dev/null || true

# shellcheck disable=SC1090
. "$META"

# An explicit ZBX_SERVER_HOST always wins (lets you re-point a probe without re-enrolling); else
# use the core host baked in at enrollment.
CORE_HOST="${ZBX_SERVER_HOST:-$CORE_HOST}"
if [ -z "$CORE_HOST" ]; then
  echo "argus-probe: no core host known — set ARGUS_PROBE_CORE_HOST in Argus or ZBX_SERVER_HOST here" >&2
  exit 1
fi

export ZBX_HOSTNAME="$PROXY_NAME"
export ZBX_SERVER_HOST="$CORE_HOST"
export ZBX_PROXYMODE=0
export ZBX_PROXYOFFLINEBUFFER="${ZBX_PROXYOFFLINEBUFFER:-168}"
export ZBX_PROXYLOCALBUFFER="${ZBX_PROXYLOCALBUFFER:-0}"
export ZBX_TLSCONNECT=cert
export ZBX_TLSACCEPT=cert
export ZBX_TLSCAFILE="$CA"
export ZBX_TLSCERTFILE="$CRT"
export ZBX_TLSKEYFILE="$KEY"
export ZBX_TLSSERVERCERTISSUER="${ZBX_TLSSERVERCERTISSUER:-CN=Monitoring Core CA}"
export ZBX_TLSSERVERCERTSUBJECT="${ZBX_TLSSERVERCERTSUBJECT:-CN=zabbix-core}"

# Fleet check-in reporter: every 5 min, report our running version + self-updater flag to Argus
# and receive the fleet target. Report-only (no Docker socket); the opt-in self-updater is a
# separate sidecar. Runs as a background child so the Zabbix proxy stays PID 1. Best-effort: any
# failure (older Argus, transient network) is ignored and retried next tick.
PROBE_VERSION="$(cat /etc/argus-probe.version 2>/dev/null || echo dev)"
SELFUPDATE="${ARGUS_PROBE_SELFUPDATE:-0}"
if [ -n "${PROBE_TOKEN:-}" ] && [ -n "${CHECKIN_URL:-}" ]; then
  echo "argus-probe: fleet check-in enabled -> $CHECKIN_URL (version $PROBE_VERSION)"
  (
    # A short initial delay lets the proxy come up before the first report.
    sleep 20
    while true; do
      curl -sS -m 15 -o /dev/null \
        -H "Authorization: Bearer $PROBE_TOKEN" -H 'Content-Type: application/json' \
        -d "$(jq -nc --arg v "$PROBE_VERSION" --argjson su "$([ "$SELFUPDATE" = "1" ] && echo true || echo false)" '{version:$v, selfupdate:$su}')" \
        "$CHECKIN_URL" 2>/dev/null || true
      sleep 300
    done
  ) &
fi

echo "argus-probe: starting Zabbix proxy '$ZBX_HOSTNAME' -> $ZBX_SERVER_HOST:10051"
exec /usr/bin/docker-entrypoint.sh "$@"
