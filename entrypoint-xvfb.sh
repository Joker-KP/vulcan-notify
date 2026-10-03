#!/bin/bash
set -euo pipefail

export TZ="${TZ:-${QUIET_HOURS_TZ:-Europe/Warsaw}}"
export DISPLAY="${DISPLAY:-:99}"

echo "[xvfb] Starting Xvfb on ${DISPLAY}"

# Clean stale X11 lock/socket left after an unclean container shutdown.
DISPLAY_NUM="${DISPLAY#:}"

rm -f "/tmp/.X${DISPLAY_NUM}-lock" || true
rm -f "/tmp/.X11-unix/X${DISPLAY_NUM}" || true

Xvfb "$DISPLAY" \
    -screen 0 1440x900x24 \
    -ac \
    +extension RANDR \
    >/tmp/xvfb.log 2>&1 &

XVFB_PID=$!

echo "[xvfb] Xvfb PID=${XVFB_PID}"

# Wait until the X11 socket is really available.
for i in $(seq 1 20); do
    if [ -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; then
        echo "[xvfb] X server ready on ${DISPLAY}"
        break
    fi

    # Detect an Xvfb crash instead of silently waiting.
    if ! kill -0 "$XVFB_PID" 2>/dev/null; then
        echo "[xvfb] ERROR: Xvfb terminated during startup"
        cat /tmp/xvfb.log || true
        exit 1
    fi

    sleep 0.25
done

if [ ! -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; then
    echo "[xvfb] ERROR: X server did not become ready"
    cat /tmp/xvfb.log || true
    exit 1
fi

if [ "$#" -eq 0 ]; then
    set -- ./sync-loop.sh
fi

echo "[xvfb] Starting supplied command"
exec "$@"
