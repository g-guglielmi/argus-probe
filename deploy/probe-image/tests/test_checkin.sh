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

if [ "$FAILS" -gt 0 ]; then
  echo "$FAILS failed"
  exit 1
fi
echo "all passed"
