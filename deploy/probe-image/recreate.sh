#!/bin/sh
# argus-probe self-update helper (role: ARGUS_PROBE_ROLE=recreate).
#
# A container can't cleanly `docker rm -f` itself mid-update, so the proxy spawns THIS short-lived
# sister container (Docker socket mounted, run with --rm) to recreate the proxy on a new image.
# It clones the proxy's own config via the Docker Engine API - Binds/Mounts, env, restart policy,
# network, labels - swapping only the image, so whatever flags the operator deployed with survive.
#
# Safety: the old container is stopped and renamed (not removed) first, and restored if the new
# container fails to create or start - a bad pull/create never leaves the site without a probe.
#
# Inputs (env): ARGUS_RECREATE_TARGET = the proxy container id/name; ARGUS_RECREATE_TAG = the image
# tag to converge on (e.g. "7.0.29-r2" or "latest").
set -eu

SOCK=/var/run/docker.sock
TARGET="${ARGUS_RECREATE_TARGET:?set ARGUS_RECREATE_TARGET}"
TAG="${ARGUS_RECREATE_TAG:?set ARGUS_RECREATE_TAG}"

# api METHOD PATH [BODY] - talk to the Docker Engine API over the unix socket.
api() {
  if [ "$#" -ge 3 ]; then
    curl -sS --unix-socket "$SOCK" -X "$1" -H 'Content-Type: application/json' -d "$3" "http://localhost$2"
  else
    curl -sS --unix-socket "$SOCK" -X "$1" "http://localhost$2"
  fi
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

# Success - drop the old container.
api DELETE "/containers/${NAME}_old?force=true" >/dev/null 2>&1 || true
echo "argus-recreate: $NAME updated to $NEW_IMAGE"
