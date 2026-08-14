#!/bin/sh
# argus-probe entrypoint: self-enroll on first boot, then hand off to the stock Zabbix proxy.
#
# On first boot (no certs on the data volume yet) it generates a keypair + CSR locally, redeems
# the single-use ARGUS_ENROLL_TOKEN against Argus (/api/enroll), and writes the signed cert +
# ca.crt. The private key never leaves this container. On later boots the certs already exist, so
# enrollment is skipped. Certs live under the mounted /var/lib/zabbix volume — keep it persistent,
# because the enrollment token is single-use.
set -eu

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
  if ! RESP=$(curl -fsS -X POST -H 'Content-Type: application/json' -d "$BODY" "$ARGUS_ENROLL_URL"); then
    echo "argus-probe: enrollment request failed (token used/expired, or Argus unreachable)" >&2
    rm -f "$KEY" /tmp/probe.csr
    exit 1
  fi
  rm -f /tmp/probe.csr

  echo "$RESP" | jq -er '.certificate' > "$CRT"
  echo "$RESP" | jq -er '.ca' > "$CA"
  PROXY_NAME=$(echo "$RESP" | jq -er '.proxy_name')
  CORE_HOST=$(echo "$RESP" | jq -r '.core_host // ""')
  printf 'PROXY_NAME=%s\nCORE_HOST=%s\n' "$PROXY_NAME" "$CORE_HOST" > "$META"
  chmod 600 "$KEY"
  echo "argus-probe: enrolled as $PROXY_NAME (core: ${CORE_HOST:-<from ZBX_SERVER_HOST>})"
fi

# The stock entrypoint runs as root then drops to the zabbix user (and fixes the spool ownership
# itself); make sure the enrolled certs we wrote are readable by it too. Best-effort.
chown -R zabbix:zabbix "$CERTS" 2>/dev/null || chown -R 1997:1997 "$CERTS" 2>/dev/null || true

# shellcheck disable=SC1090
. "$META"

# The server may not know the reachable core address; allow ZBX_SERVER_HOST to supply/override it.
CORE_HOST="${CORE_HOST:-${ZBX_SERVER_HOST:-}}"
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

echo "argus-probe: starting Zabbix proxy '$ZBX_HOSTNAME' -> $ZBX_SERVER_HOST:10051"
exec /usr/bin/docker-entrypoint.sh "$@"
