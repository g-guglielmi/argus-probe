#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi
#
# The probe image, started for real. The unit tests check our scripts one by one; none of them
# starts the Zabbix proxy, so none can tell whether the settings the entrypoint hands it are ones
# Zabbix accepts (it refuses to start on a pair it doesn't like), or whether a proxy really comes
# back after its local database was damaged. This runs the image a pull request has just built, as
# an already enrolled probe whose core does not answer, and looks:
#   - the proxy starts and stays up on the hybrid buffer (values in memory, not on the disk),
#   - after its database file was overwritten, as a power cut can leave it, the next start sets the
#     file aside and the proxy runs on a new one,
#   - a probe that sets ZBX_PROXYLOCALBUFFER, which Zabbix allows only with the disk buffer, stays
#     on disk and starts.
#
# It needs a Docker Engine and openssl. It lives outside deploy/probe-image/ so that changing it
# never releases an image.
#
# Usage: test/probe_live_test.sh PROBE_IMAGE     (an image already in the local Docker)
set -u
IMG="${1:?usage: probe_live_test.sh PROBE_IMAGE}"
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "SKIP: no Docker Engine to talk to"; exit 0
fi

C=probetest
NAME=site1-proxy
CONF=/etc/zabbix/zabbix_proxy.conf
DB="/var/lib/zabbix/db_data/$NAME.sqlite"

fail=0
check() { # desc got want
  if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1: got <$2> want <$3>"; fail=1; fi
}
has() { # desc text part
  case "$2" in *"$3"*) echo "  ok   $1" ;; *) echo "  FAIL $1: the output lacks <$3>"; fail=1 ;; esac
}
lacks() { # desc text part
  case "$2" in *"$3"*) echo "  FAIL $1: the output has <$3>"; fail=1 ;; *) echo "  ok   $1" ;; esac
}

D=$(mktemp -d)
K=$(mktemp -d)
cleanup() {
  docker rm -f "$C" >/dev/null 2>&1
  # What the container wrote is root's or the proxy user's: let a container remove it.
  docker run --rm -v "$D:/d" --entrypoint sh "$IMG" -c 'rm -rf /d/* /d/.[!.]* 2>/dev/null' >/dev/null 2>&1
  rm -rf "$D" "$K" 2>/dev/null
}
trap cleanup EXIT
docker rm -f "$C" >/dev/null 2>&1

# --- an enrolled probe: a certificate from a CA of its own, and the core at an address that does
# --- not answer. With no check-in address there is no check-in loop either: only the proxy runs.
echo "setting up: a data folder as enrollment leaves it"
mkdir -p "$D/enroll"
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$K/ca.key" -out "$D/enroll/ca.crt" -subj "/CN=Monitoring Core CA" -days 2 >/dev/null 2>&1 \
  && openssl req -newkey rsa:2048 -nodes -keyout "$D/enroll/proxy.key" -out "$K/proxy.csr" -subj "/CN=proxy" >/dev/null 2>&1 \
  && openssl x509 -req -in "$K/proxy.csr" -CA "$D/enroll/ca.crt" -CAkey "$K/ca.key" -CAcreateserial -out "$D/enroll/proxy.crt" -days 2 >/dev/null 2>&1 \
  || { echo "FAILED: could not make the test certificates"; exit 1; }
printf 'PROXY_NAME=%s\nCORE_HOST=127.0.0.1\nPROBE_TOKEN=\nCHECKIN_URL=\n' "$NAME" > "$D/enroll/proxy.env"

# started - wait for this start's Zabbix banner (or for the container to end), give the proxy a
# while to fall over if it is going to, and leave this start's log in LOG.
started() {
  for _ in $(seq 1 45); do
    LOG=$(docker logs --since "$SINCE" "$C" 2>&1)
    case "$LOG" in *"Starting Zabbix Proxy"*) break ;; esac
    [ "$(docker inspect -f '{{.State.Running}}' "$C" 2>/dev/null)" = true ] || break
    sleep 2
  done
  sleep 15
  LOG=$(docker logs --since "$SINCE" "$C" 2>&1)
  printf '%s\n' "$LOG" | grep -v "cannot connect to\|Unable to connect\|unable to connect\|connection refused\|Connection refused" | tail -n 25 | cut -c1-220 | sed 's/^/    | /'
}
now()     { date -u +%Y-%m-%dT%H:%M:%SZ; }
running() { docker inspect -f '{{.State.Running}}' "$C" 2>/dev/null; }
conf()    { docker exec "$C" grep -E "^$1=" "$CONF" 2>/dev/null | head -n1; }
dbstate() { docker exec "$C" python3 /usr/local/lib/argus/dbcheck.py --verdict "$DB" >/dev/null 2>&1; echo $?; }

echo "a probe starts on the hybrid buffer"
SINCE=$(now)
docker run -d --name "$C" -v "$D:/var/lib/zabbix" "$IMG" >/dev/null || { echo "FAILED: could not start the image"; exit 1; }
started
has   "Zabbix starts"                    "$LOG" "Starting Zabbix Proxy"
check "and stays up"                     "$(running)" true
check "the buffer is hybrid"             "$(conf ProxyBufferMode)" "ProxyBufferMode=hybrid"
check "with memory to keep values in"    "$(conf ProxyMemoryBufferSize)" "ProxyMemoryBufferSize=16M"
check "written out when ten minutes old" "$(conf ProxyMemoryBufferAge)" "ProxyMemoryBufferAge=600"
check "seven days are still kept"        "$(conf ProxyOfflineBuffer)" "ProxyOfflineBuffer=168"
check "its database checks out"          "$(dbstate)" 0
lacks "nothing was set aside"            "$LOG" "was damaged"

echo "the database file is damaged while the probe is off: the next start sets it aside"
docker stop "$C" >/dev/null
# In place, past the first page: what the file's owner and name are stays as the proxy left it.
docker run --rm -v "$D:/var/lib/zabbix" --entrypoint sh "$IMG" -c "dd if=/dev/urandom of='$DB' bs=4096 seek=1 count=40 conv=notrunc 2>/dev/null"
check "the file is damaged now" "$(docker run --rm -v "$D:/var/lib/zabbix" --entrypoint sh "$IMG" -c "python3 /usr/local/lib/argus/dbcheck.py --verdict '$DB' >/dev/null 2>&1; echo \$?")" 3
SINCE=$(now)
docker start "$C" >/dev/null
started
has   "it says what it found"        "$LOG" "the proxy's local database was damaged"
has   "and what it did"              "$LOG" "set aside as $DB.damaged"
has   "Zabbix starts"                "$LOG" "Starting Zabbix Proxy"
check "and stays up"                 "$(running)" true
check "the damaged file is kept"     "$(docker exec "$C" sh -c "test -s '$DB.damaged' && echo yes")" yes
check "the new database checks out"  "$(dbstate)" 0

echo "a probe with ZBX_PROXYLOCALBUFFER set stays on the disk buffer, and starts"
docker rm -f "$C" >/dev/null
SINCE=$(now)
docker run -d --name "$C" -v "$D:/var/lib/zabbix" -e ZBX_PROXYLOCALBUFFER=1 "$IMG" >/dev/null
started
has   "it says why"                  "$LOG" "staying on the disk buffer"
has   "Zabbix starts"                "$LOG" "Starting Zabbix Proxy"
check "and stays up"                 "$(running)" true
check "the buffer is disk"           "$(conf ProxyBufferMode)" "ProxyBufferMode=disk"
check "no memory buffer is set"      "$(conf ProxyMemoryBufferSize)" ""
check "nor its age"                  "$(conf ProxyMemoryBufferAge)" ""

echo "ZBX_PROXYBUFFERMODE=disk on the container brings the old way back"
docker rm -f "$C" >/dev/null
SINCE=$(now)
docker run -d --name "$C" -v "$D:/var/lib/zabbix" -e ZBX_PROXYBUFFERMODE=disk "$IMG" >/dev/null
started
has   "Zabbix starts"                "$LOG" "Starting Zabbix Proxy"
check "and stays up"                 "$(running)" true
check "the buffer is disk"           "$(conf ProxyBufferMode)" "ProxyBufferMode=disk"
check "no memory buffer is set"      "$(conf ProxyMemoryBufferSize)" ""

if [ "$fail" = 0 ]; then echo "PASS"; else echo "FAILED"; fi
exit $fail
