#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi

# Tests for checkin.sh, the probe's minute report to Argus. Run with a POSIX sh (CI uses dash, like
# the image's busybox sh is) and jq: sh deploy/probe-image/tests/test_checkin.sh
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/../checkin.sh"

FAILS=0
# check NAME BODY JQ-FILTER - the filter must hold for the body.
check() {
  if printf '%s' "$2" | jq -e "$3" >/dev/null 2>&1; then
    echo "ok   $1"
  else
    echo "FAIL $1: $3"
    echo "     body: ${2:-<empty>}"
    FAILS=$((FAILS + 1))
  fi
}
base='.version == "7.0.31-r29" and .scans == true and .sweeps == true'
PROCS='{"StartPingers":5,"StartPollers":5}'

# Nothing set on the container: the case every probe is in (r8 to r28 sent an empty body here).
B=$(checkin_body 7.0.31-r29 "$PROCS" "")
check "no pinned counts" "$B" "$base and .procs_pinned == [] and .procs.StartPingers == 5"
B=$(checkin_body 7.0.31-r29 "$PROCS" " ")
check "only blanks pinned" "$B" "$base and .procs_pinned == []"

B=$(checkin_body 7.0.31-r29 "$PROCS" " StartPollers StartPingers")
check "pinned counts" "$B" "$base and .procs_pinned == [\"StartPollers\",\"StartPingers\"]"

B=$(checkin_body 7.0.31-r29 "" "")
check "no process counts" "$B" "$base and .procs == null"
B=$(checkin_body 7.0.31-r29 "{broken" "")
check "unreadable process counts" "$B" "$base and .procs == null and .procs_pinned == []"

B=$(checkin_body 7.0.31-r29 "$PROCS" "")
check "cpu reported" "$B" '(.cpu.count | type) == "number" and (.cpu.load | length) == 3'

# A container limit through cgroup v2, and none.
T=$(mktemp -d)
printf '150000 100000\n' > "$T/cpu.max"
CGROUP_ROOT=$T
B=$(checkin_body 7.0.31-r29 "$PROCS" "")
check "cpu limit" "$B" '.cpu.quota == 1.5'
printf 'max 100000\n' > "$T/cpu.max"
B=$(checkin_body 7.0.31-r29 "$PROCS" "")
check "no cpu limit" "$B" '.cpu.quota == 0'
rm -rf "$T"
CGROUP_ROOT=/sys/fs/cgroup

# A CPU reading that isn't JSON leaves the rest of the report whole.
cpu_json() { printf 'not json'; }
B=$(checkin_body 7.0.31-r29 "$PROCS" "")
check "unreadable cpu" "$B" "$base and .cpu == null and .procs.StartPollers == 5"

# Whatever the version string holds, the report stays JSON.
B=$(checkin_body 'dev "quoted" \ back' "$PROCS" "")
check "odd version" "$B" '.version == "dev \"quoted\" \\ back"'

# The CA's fingerprint rides along when known, and is left out when not.
B=$(checkin_body 7.0.31-r29 "$PROCS" "" "ab12")
check "ca reported" "$B" "$base and .ca == \"ab12\""
B=$(checkin_body 7.0.31-r29 "$PROCS" "" "")
check "no ca" "$B" "$base and (has(\"ca\") | not)"

# take_ca: only a renewal of the CA the probe trusts replaces its ca.crt.
# expect NAME CONDITION - a shell condition that must hold.
expect() {
  if eval "$2"; then echo "ok   $1"; else echo "FAIL $1: $2"; FAILS=$((FAILS + 1)); fi
}
T=$(mktemp -d)
export MSYS2_ARG_CONV_EXCL="/CN="   # Git Bash on Windows: keep the subject as it is
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$T/ca.key" -out "$T/old.crt" -days 30 -subj "/CN=Monitoring Core CA" >/dev/null 2>&1
openssl x509 -in "$T/old.crt" -signkey "$T/ca.key" -days 3650 -set_serial 0x0a -out "$T/renewed.crt" >/dev/null 2>&1
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$T/other.key" -out "$T/other.crt" -days 3650 -subj "/CN=Monitoring Core CA" >/dev/null 2>&1
openssl req -x509 -key "$T/ca.key" -out "$T/renamed.crt" -days 3650 -subj "/CN=Someone Else CA" >/dev/null 2>&1
cp "$T/old.crt" "$T/ca.crt"
OLD_FP=$(ca_fp "$T/ca.crt")
expect "fingerprint is lowercase hex" '[ "${#OLD_FP}" = 64 ] && [ -z "$(printf %s "$OLD_FP" | tr -d 0-9a-f)" ]'

WHY=$(take_ca "$(cat "$T/other.crt")" "$T/ca.crt") && R=0 || R=$?
expect "another key is refused" '[ "$R" = 1 ] && [ "$(ca_fp "$T/ca.crt")" = "$OLD_FP" ] && [ "$WHY" = "it has another key than the CA this probe trusts" ]'
WHY=$(take_ca "$(cat "$T/renamed.crt")" "$T/ca.crt") && R=0 || R=$?
expect "another name is refused" '[ "$R" = 1 ] && [ "$(ca_fp "$T/ca.crt")" = "$OLD_FP" ] && [ "$WHY" = "it has another name than the CA this probe trusts" ]'
WHY=$(take_ca "not a certificate" "$T/ca.crt") && R=0 || R=$?
expect "garbage is refused" '[ "$R" = 1 ] && [ "$WHY" = "it is not one certificate" ] && [ ! -e "$T/ca.crt.new" ]'
WHY=$(take_ca "$(cat "$T/renewed.crt" "$T/other.crt")" "$T/ca.crt") && R=0 || R=$?
expect "a bundle is refused" '[ "$R" = 1 ] && [ "$WHY" = "it is not one certificate" ]'
WHY=$(take_ca "$(cat "$T/old.crt")" "$T/ca.crt") && R=0 || R=$?
expect "the same one is refused" '[ "$R" = 1 ] && [ "$WHY" = "it doesn'"'"'t end later than the one this probe has" ]'

WHY=$(take_ca "$(cat "$T/renewed.crt")" "$T/ca.crt") && R=0 || R=$?
expect "the renewal is taken" '[ "$R" = 0 ] && [ "$(ca_fp "$T/ca.crt")" = "$(ca_fp "$T/renewed.crt")" ] && [ ! -e "$T/ca.crt.new" ]'
expect "its end is read" 'case "$(cert_end "$T/ca.crt")" in 20[0-9][0-9]-[01][0-9]-[0-3][0-9]" "*Z) true;; *) false;; esac'
WHY=$(take_ca "$(cat "$T/old.crt")" "$T/ca.crt") && R=0 || R=$?
expect "an older one is refused" '[ "$R" = 1 ] && [ "$(ca_fp "$T/ca.crt")" = "$(ca_fp "$T/renewed.crt")" ]'

B=$(checkin_body 7.0.31-r29 "$PROCS" "" "" "2031-08-10 08:23:27Z")
check "certificate end reported" "$B" "$base and .cert_end == \"2031-08-10 08:23:27Z\" and (has(\"ca\") | not)"

# renew_cert: Argus stands in as a curl that checks the proof (the request signed by the current key)
# and signs the request with the CA, or answers for another key, or refuses.
openssl req -newkey rsa:2048 -nodes -keyout "$T/proxy.key" -out "$T/proxy.csr" -subj "/CN=proxy" >/dev/null 2>&1
openssl x509 -req -in "$T/proxy.csr" -CA "$T/ca.crt" -CAkey "$T/ca.key" -set_serial 0x0c -days 10 -subj "/CN=proxy-site1" -out "$T/proxy.crt" >/dev/null 2>&1
MOCK=sign
curl() {
  _o=""; _b=""
  while [ $# -gt 0 ]; do
    case "$1" in -o) _o=$2; shift;; --data-binary) _b=${2#@}; shift;; esac
    shift
  done
  if [ "$MOCK" = deny ]; then printf '{"error":"renewal refused: nope"}' > "$_o"; printf 403; return 0; fi
  jq -j .csr "$_b" > "$T/sent.csr"
  jq -r .sig "$_b" | openssl base64 -d -A > "$T/sent.sig"
  jq -r .cert "$_b" > "$T/sent.crt"
  openssl x509 -in "$T/sent.crt" -noout -pubkey > "$T/sent.pub"
  if ! openssl dgst -sha256 -verify "$T/sent.pub" -signature "$T/sent.sig" "$T/sent.csr" >/dev/null 2>&1; then
    printf '{"error":"renewal refused: bad proof"}' > "$_o"; printf 403; return 0
  fi
  if [ "$MOCK" = other ]; then openssl req -newkey rsa:2048 -nodes -keyout "$T/x.key" -out "$T/sent.csr" -subj "/CN=x" >/dev/null 2>&1; fi
  openssl x509 -req -in "$T/sent.csr" -CA "$T/ca.crt" -CAkey "$T/ca.key" -set_serial 0x0d -days 1825 -subj "/CN=proxy-site1" -out "$T/new.crt" >/dev/null 2>&1
  jq -n --rawfile c "$T/new.crt" '{certificate:$c}' > "$_o"
  printf 200
}
OLD_CRT=$(cat "$T/proxy.crt"); OLD_KEY=$(cat "$T/proxy.key")
MOCK=deny
WHY=$(renew_cert https://argus.example.lan/api/probes/renew-cert tok "$T/proxy.crt" "$T/proxy.key" "$T/ca.crt") && R=0 || R=$?
expect "a refusal is said" '[ "$R" = 1 ] && [ "$WHY" = "Argus answered HTTP 403: renewal refused: nope" ] && [ "$(cat "$T/proxy.crt")" = "$OLD_CRT" ]'
MOCK=other
WHY=$(renew_cert https://argus.example.lan/api/probes/renew-cert tok "$T/proxy.crt" "$T/proxy.key" "$T/ca.crt") && R=0 || R=$?
expect "a certificate for another key is refused" '[ "$R" = 1 ] && [ "$WHY" = "the certificate is for another key" ] && [ "$(cat "$T/proxy.key")" = "$OLD_KEY" ]'
MOCK=sign
WHY=$(renew_cert https://argus.example.lan/api/probes/renew-cert tok "$T/proxy.crt" "$T/proxy.key" "$T/ca.crt") && R=0 || R=$?
expect "the renewal is taken" '[ "$R" = 0 ] && [ "$(cat "$T/proxy.key")" != "$OLD_KEY" ] && [ "$(ca_key "$T/proxy.crt")" = "$(openssl pkey -in "$T/proxy.key" -pubout)" ]'
expect "it ends later, same name" 'openssl x509 -in "$T/proxy.crt" -noout -checkend 31536000 >/dev/null 2>&1 && [ "$(ca_name "$T/proxy.crt")" = "subject=CN=proxy-site1" ]'
expect "the old pair is kept" '[ "$(cat "$T/proxy.crt.old")" = "$OLD_CRT" ] && [ "$(cat "$T/proxy.key.old")" = "$OLD_KEY" ]'
unset -f curl
rm -rf "$T"

if [ "$FAILS" -gt 0 ]; then
  echo "$FAILS failed"
  exit 1
fi
echo "all passed"
