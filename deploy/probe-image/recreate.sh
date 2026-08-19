#!/bin/sh
# argus-probe self-update helper (role: ARGUS_PROBE_ROLE=recreate).
#
# A container can't cleanly `docker rm -f` itself mid-update, so the proxy spawns THIS short-lived
# sister container (Docker socket mounted, run with --rm) to recreate the proxy on a new image.
# It clones the proxy's own config via the Docker Engine API - Binds/Mounts, env, restart policy,
# network, labels - swapping only the image, so whatever flags the operator deployed with survive.
#
# Safety: the old container is stopped and renamed (not removed) first, and restored if the new
# container fails to create, start, OR stay up (a crash-looping bad image is rolled back too) - a
# bad pull/create/build never leaves the site without a probe.
#
# Inputs (env): ARGUS_RECREATE_TARGET = the proxy container id/name; ARGUS_RECREATE_TAG = the image
# tag to converge on (e.g. "7.0.29-r2" or "latest").
set -eu

SOCK=/var/run/docker.sock
TARGET="${ARGUS_RECREATE_TARGET:?set ARGUS_RECREATE_TARGET}"
TAG="${ARGUS_RECREATE_TAG:?set ARGUS_RECREATE_TAG}"
HEALTH_STABLE="${ARGUS_HEALTH_STABLE:-20}"   # seconds the new proxy must stay up to pass
VERIFY_TIMEOUT="${ARGUS_VERIFY_TIMEOUT:-90}" # give up (and roll back) after this long

# api METHOD PATH [BODY] - talk to the Docker Engine API over the unix socket.
api() {
  if [ "$#" -ge 3 ]; then
    curl -sS --unix-socket "$SOCK" -X "$1" -H 'Content-Type: application/json' -d "$3" "http://localhost$2"
  else
    curl -sS --unix-socket "$SOCK" -X "$1" "http://localhost$2"
  fi
}

# verify NAME - return 0 once the new container has stayed Running and not Restarting for
# HEALTH_STABLE seconds; return 1 on timeout. A crash-loop guard: a bad image that starts then exits
# (or reboots under a restart policy) never reaches a stable window, so it gets rolled back. The
# argus-probe runs an ACTIVE Zabbix proxy (dials out, no listening HTTP endpoint), so this is
# container-stability only - unlike the core's updater there's no /healthz to probe.
verify() {
  _name="$1"
  sleep 5   # brief grace for startup
  _need=$(( HEALTH_STABLE / 3 )); [ "$_need" -lt 1 ] && _need=1
  _stable=0
  _end=$(( $(date +%s) + VERIFY_TIMEOUT ))
  while [ "$(date +%s)" -lt "$_end" ]; do
    _json=$(api GET "/containers/$_name/json")
    _running=$(printf '%s' "$_json" | jq -r '.State.Running // false')
    _restarting=$(printf '%s' "$_json" | jq -r '.State.Restarting // false')
    if [ "$_running" != "true" ] || [ "$_restarting" = "true" ]; then
      _stable=0; sleep 3; continue
    fi
    _stable=$(( _stable + 1 ))
    [ "$_stable" -ge "$_need" ] && return 0
    sleep 3
  done
  return 1
}

INSPECT=$(api GET "/containers/$TARGET/json")
NAME=$(printf '%s' "$INSPECT" | jq -r '.Name // empty' | sed 's#^/##')
CUR_IMAGE=$(printf '%s' "$INSPECT" | jq -r '.Config.Image // empty')
if [ -z "$NAME" ] || [ -z "$CUR_IMAGE" ]; then
  echo "argus-recreate: could not inspect target '$TARGET' - aborting (proxy untouched)" >&2
  exit 1
fi
REPO=$(printf '%s' "$CUR_IMAGE" | sed 's/:[^:/]*$//')   # strip the tag, keep the repo
NEW_IMAGE="$REPO:$TAG"
echo "argus-recreate: $NAME  $CUR_IMAGE -> $NEW_IMAGE"

# Pull the target image up front (docker CLI handles registry auth for public images). Abort
# before touching the running proxy if the image can't be fetched.
if ! docker pull "$NEW_IMAGE"; then
  echo "argus-recreate: pull of $NEW_IMAGE failed - leaving the proxy untouched" >&2
  exit 1
fi

# Build the create body from the proxy's own config, swapping the image. Preserve the operator-set
# bits (Env, Labels, ExposedPorts, and the whole HostConfig: binds/mounts, restart policy, network
# mode, port bindings). Deliberately drop Cmd/Entrypoint/Hostname so the NEW image's defaults apply
# and the recreated container gets a fresh hostname (= its own id, which the reporter relies on).
CREATE_BODY=$(printf '%s' "$INSPECT" | jq --arg img "$NEW_IMAGE" '{
  Image: $img,
  Env: .Config.Env,
  Labels: (.Config.Labels // {}),
  ExposedPorts: .Config.ExposedPorts,
  HostConfig: .HostConfig
}')

rollback() {
  echo "argus-recreate: rolling back to the previous container" >&2
  api POST "/containers/${NAME}_old/rename?name=$NAME" >/dev/null 2>&1 || true
  api POST "/containers/$NAME/start" >/dev/null 2>&1 || true
}

# Stop + rename the old container (kept as a rollback), then create + start the new one under the
# original name.
api POST "/containers/$TARGET/stop?t=15" >/dev/null 2>&1 || true
if ! api POST "/containers/$TARGET/rename?name=${NAME}_old" >/dev/null 2>&1; then
  echo "argus-recreate: could not rename the old container - aborting, proxy left stopped" >&2
  api POST "/containers/$TARGET/start" >/dev/null 2>&1 || true
  exit 1
fi

NEWID=$(api POST "/containers/create?name=$NAME" "$CREATE_BODY" | jq -r '.Id // empty')
if [ -z "$NEWID" ]; then
  echo "argus-recreate: create failed" >&2
  rollback
  exit 1
fi
if ! api POST "/containers/$NEWID/start" >/dev/null 2>&1; then
  echo "argus-recreate: start failed" >&2
  api DELETE "/containers/$NEWID?force=true" >/dev/null 2>&1 || true
  rollback
  exit 1
fi

# Verify the new proxy stays up (crash-loop guard); roll back to the previous container if it doesn't.
if ! verify "$NAME"; then
  echo "argus-recreate: new proxy did not stay healthy - rolling back to the previous version" >&2
  api POST "/containers/$NAME/stop?t=10" >/dev/null 2>&1 || true
  api DELETE "/containers/$NAME?force=true" >/dev/null 2>&1 || true
  rollback
  exit 1
fi

# Success - drop the old container.
api DELETE "/containers/${NAME}_old?force=true" >/dev/null 2>&1 || true
echo "argus-recreate: $NAME updated to $NEW_IMAGE"
