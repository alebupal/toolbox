#!/bin/sh
set -eu

PUID="${PUID:-0}"
PGID="${PGID:-0}"
CONFIG_DIR="${CONFIG_DIR:-/config}"

mkdir -p "$CONFIG_DIR" "$CONFIG_DIR/cli/mover" "$CONFIG_DIR/cli/ftp"

if [ "$(id -u)" = "0" ] && [ "${PUID}:${PGID}" != "0:0" ]; then
    chown -R "${PUID}:${PGID}" "$CONFIG_DIR" 2>/dev/null || true
    exec gosu "${PUID}:${PGID}" python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8090}"
else
    exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8090}"
fi
