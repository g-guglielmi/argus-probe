#!/bin/sh
# argus-probe self-updater (opt-in sidecar; role: ARGUS_PROBE_ROLE=updater).
#
# Outbound-only sites can't be pushed to, so updates are pull-based but Argus-coordinated: this
# sidecar asks Argus for the fleet target version and, when it differs from what the proxy runs,
# rewrites ARGUS_PROBE_TAG in the compose .env and recreates the proxy service. Argus is the
# control plane; the probe converges. Off unless you deploy this sidecar (it needs the Docker
# socket), so socket access is opt-in and isolated to this container, not the proxy.
#
# It reads the check-in credential the proxy stored at enrollment from the shared probe volume
# (mounted read-only at /probe), so no token needs to be supplied twice.
set -eu

COMPOSE_DIR="${ARGUS_COMPOSE_DIR:-/compose}"
COMPOSE_FILE="$COMPOSE_DIR/docker-compose.yml"
ENV_FILE="$COMPOSE_DIR/.env"
META="${ARGUS_PROBE_META:-/probe/enroll/proxy.env}"
VERSION="$(cat /etc/argus-probe.version 2>/dev/null || echo dev)"
INTERVAL="${ARGUS_UPDATE_INTERVAL:-300}"

echo "argus-updater: starting (version $VERSION, poll ${INTERVAL}s, compose $COMPOSE_FILE)"

while true; do
  # The proxy writes PROBE_TOKEN + CHECKIN_URL here at enrollment; may be absent early on.
  PROBE_TOKEN=""; CHECKIN_URL=""
  if [ -f "$META" ]; then
    # shellcheck disable=SC1090
    . "$META" 2>/dev/null || true
  fi

  if [ -n "$PROBE_TOKEN" ] && [ -n "$CHECKIN_URL" ]; then
    RESP=$(curl -sS -m 15 \
      -H "Authorization: Bearer $PROBE_TOKEN" -H 'Content-Type: application/json' \
      -d "$(jq -nc --arg v "$VERSION" '{version:$v, selfupdate:true}')" \
      "$CHECKIN_URL" 2>/dev/null || echo '')
    TARGET=$(echo "$RESP" | jq -r '.target // empty' 2>/dev/null || true)

    if [ -n "$TARGET" ]; then
      # "latest" maps to the rolling tag; a pin (e.g. 7.0.29-r1) maps to itself.
      TAG="latest"
      [ "$TARGET" != "latest" ] && TAG="$TARGET"
      CUR=$(sed -n 's/^ARGUS_PROBE_TAG=//p' "$ENV_FILE" 2>/dev/null || true)

      if [ "$TAG" != "$CUR" ]; then
        echo "argus-updater: fleet target=$TARGET (tag $TAG) differs from current '${CUR:-unset}' — updating proxy"
        touch "$ENV_FILE"
        grep -v -E '^ARGUS_PROBE_TAG=' "$ENV_FILE" > "$ENV_FILE.tmp" 2>/dev/null || true
        echo "ARGUS_PROBE_TAG=$TAG" >> "$ENV_FILE.tmp"
        mv "$ENV_FILE.tmp" "$ENV_FILE"
        # Recreate only the proxy service (not this updater, so it can't kill itself mid-update).
        if docker compose -f "$COMPOSE_FILE" pull proxy && docker compose -f "$COMPOSE_FILE" up -d proxy; then
          echo "argus-updater: proxy updated to $TAG"
        else
          echo "argus-updater: update to $TAG failed; will retry next tick" >&2
        fi
      fi
    fi
  fi

  sleep "$INTERVAL"
done
