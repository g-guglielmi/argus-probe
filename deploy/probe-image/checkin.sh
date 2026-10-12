# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 g-guglielmi

# argus-probe check-in report: sourced by entrypoint.sh (and by tests/test_checkin.sh, which runs it
# under dash in CI). Builds the JSON the probe reports to Argus every minute: its version, what it
# can do, the Zabbix process counts it started with and its CPU. Those last parts are best-effort, so
# a part that can't be read is sent as null (Argus keeps what it had) and never empties the whole
# report: a report Argus can't read loses the version, the capabilities and the queued jobs with it.

# --- CPU report (for the autoscaler) ---
# Argus raises a process count only while more processes can help: a machine whose load average stays
# at its CPU count is short on CPU, and more forks only queue for it. Every check-in reports the CPUs
# this container sees, a container CPU limit (cgroup v2 cpu.max or v1 cfs quota; 0 = none) and the
# load average. Inside a container the load average is the whole host's.
CGROUP_ROOT=/sys/fs/cgroup
valid_num() { case "$1" in ''|*[!0-9.]*|*.*.*|.*) return 1;; esac; }
cpu_json() {
  _cpus=$(nproc 2>/dev/null || grep -c '^processor' /proc/cpuinfo 2>/dev/null || echo 0)
  valid_num "$_cpus" || _cpus=0
  _quota=0
  if [ -r "$CGROUP_ROOT/cpu.max" ]; then
    _quota=$(awk '$1 != "max" && $2 > 0 { printf "%.2f", $1 / $2 }' "$CGROUP_ROOT/cpu.max" 2>/dev/null || true)
  elif [ -r "$CGROUP_ROOT/cpu/cpu.cfs_quota_us" ] && [ -r "$CGROUP_ROOT/cpu/cpu.cfs_period_us" ]; then
    _quota=$(awk 'NR == FNR { q = $1; next } q > 0 && $1 > 0 { printf "%.2f", q / $1 }' \
      "$CGROUP_ROOT/cpu/cpu.cfs_quota_us" "$CGROUP_ROOT/cpu/cpu.cfs_period_us" 2>/dev/null || true)
  fi
  valid_num "$_quota" || _quota=0
  _l1=0; _l5=0; _l15=0
  if [ -r /proc/loadavg ]; then read -r _l1 _l5 _l15 _rest < /proc/loadavg || true; fi
  valid_num "$_l1" || _l1=0; valid_num "$_l5" || _l5=0; valid_num "$_l15" || _l15=0
  printf '{"count":%s,"quota":%s,"load":[%s,%s,%s]}' "$_cpus" "$_quota" "$_l1" "$_l5" "$_l15"
}

# json_ok TEXT - TEXT is one JSON value jq can read.
json_ok() { [ -n "$1" ] && printf '%s' "$1" | jq -e 'true' >/dev/null 2>&1; }

# pinned_json "StartPollers StartPingers" - the names as a JSON list; [] for none. (Built with -n, not
# by feeding the names to jq -R: with nothing set on the container that reads no line and prints
# nothing at all, which is what emptied every report from r8 to r28.)
pinned_json() { jq -nc --arg p "$1" '$p | split(" ") | map(select(length > 0))'; }

# checkin_body VERSION PROCS_JSON PINNED_NAMES - the minute report, always a JSON object.
checkin_body() {
  _procs=$2; json_ok "$_procs" || _procs=null
  _pinned=$(pinned_json "$3" 2>/dev/null || true); json_ok "$_pinned" || _pinned=null
  _cpu=$(cpu_json 2>/dev/null || true); json_ok "$_cpu" || _cpu=null
  jq -nc --arg v "$1" --argjson procs "$_procs" --argjson pinned "$_pinned" --argjson cpu "$_cpu" \
    '{version:$v, scans:true, sweeps:true, procs:$procs, procs_pinned:$pinned, cpu:$cpu}' 2>/dev/null \
    || jq -nc --arg v "$1" '{version:$v, scans:true, sweeps:true}'
}
