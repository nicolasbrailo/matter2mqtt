#!/usr/bin/env bash
# Bring the Thread network up and block until the node attaches to the mesh.
#
# Exit 0 once the node reports leader/router/child, 1 on timeout. At boot that
# non-zero makes the otbr-agent-net oneshot fail, so anything depending on it
# won't start.
#
# Output goes to stdout and the caller decides where that lands (a logfile for
# otbr-agent-net, the s6 logger for rcp-watchdog), so don't redirect here.
#
# Tunables, as env vars so callers can be less patient than the boot path:
#   ATTEMPTS  how many times to poll for attachment (default 12)
#   INTERVAL  seconds between polls (default 10)
#
# 'set -e' is intentionally omitted: 'thread start' errors harmlessly if the
# network is already up (e.g. on a restart with persisted state).
set -uo pipefail

ATTEMPTS=${ATTEMPTS:-12}
INTERVAL=${INTERVAL:-10}

echo "[ot-net-up] bringing Thread interface up, starting network..."
ot-ctl ifconfig up
ot-ctl thread start

for _ in $(seq 1 "$ATTEMPTS"); do
    if timeout 5 ot-ctl state | grep -E '^(leader|router|child)' >/dev/null; then
        echo "[ot-net-up] attached to mesh; network ready."
        exit 0
    fi
    echo "[ot-net-up] network not ready, waiting $INTERVAL seconds..."
    sleep "$INTERVAL"
done

echo "[ot-net-up] timed out waiting to attach to mesh, check logs in /mt2mqtt-run/logs/otbr-agent" >&2
exit 1
