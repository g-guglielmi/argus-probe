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
expect "its end is read" 'case "$(ca_end "$T/ca.crt")" in 20[0-9][0-9]-[01][0-9]-[0-3][0-9]" "*Z) true;; *) false;; esac'
WHY=$(take_ca "$(cat "$T/old.crt")" "$T/ca.crt") && R=0 || R=$?
expect "an older one is refused" '[ "$R" = 1 ] && [ "$(ca_fp "$T/ca.crt")" = "$(ca_fp "$T/renewed.crt")" ]'
rm -rf "$T"

if [ "$FAILS" -gt 0 ]; then
  echo "$FAILS failed"
  exit 1
fi
echo "all passed"
