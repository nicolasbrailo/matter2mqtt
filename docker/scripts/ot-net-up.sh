#!/usr/bin/env bash
# Bring the Thread network up and block until the node attaches to the mesh.
#
# Exit codes:
#   0  attached (leader/router/child), or no Thread network exists (see below)
#   1  timed out waiting to attach
#   2  otbr-agent never answered on its control socket
#
# Output goes to stdout and the caller decides where that lands (a logfile for
# otbr-agent-net, the s6 logger for rcp-watchdog), so don't redirect here.
#
# Tunables, as env vars so callers can be less patient than the boot path:
#   ATTEMPTS      how many times to poll for attachment (default 12)
#   INTERVAL      seconds between polls (default 10)
#   SOCKET_TRIES  seconds to wait for otbr-agent's control socket (default 30)
#
# Checks ot-cli's output rather than its exit status: "Done" means the command
# worked, "Error N: ..." that it didn't.
set -uo pipefail

ATTEMPTS=${ATTEMPTS:-12}
INTERVAL=${INTERVAL:-10}
SOCKET_TRIES=${SOCKET_TRIES:-30}

# otbr-agent is a longrun with no readiness notification, so s6 starts us as soon as
# it has been exec'd -- typically before it has created its control socket, and every
# ot-cli call fails with "connect session failed". Wait until it answers, probing with
# 'dataset active' since we need its answer anyway: "Done" (there's a dataset) or
# "Error 23: NotFound" (there isn't) both mean the agent is up; anything else doesn't.
for i in $(seq 1 "$SOCKET_TRIES"); do
    dataset=$(ot-cli dataset active 2>&1)
    if grep -qE '^Done|NotFound' <<<"$dataset"; then
        break
    fi
    if [ "$i" -eq "$SOCKET_TRIES" ]; then
        echo "[ot-net-up] otbr-agent did not answer on its control socket after ${SOCKET_TRIES}s, check logs in /matter2mqtt-run/logs/otbr-agent" >&2
        exit 2
    fi
    sleep 1
done

# Without an active dataset there is no network to start. Creating one is never automatic
# (devices commissioned on an old network couldn't join a new one), so say how and carry on:
# everything else runs fine without Thread, and ot-net-new.sh brings the network up once it
# has created it. Exit 0 so the rest of the container keeps booting.
if grep -q 'NotFound' <<<"$dataset"; then
    echo "[ot-net-up] NO THREAD NETWORK: no Thread network exists, not starting Thread."
    echo "[ot-net-up] NO THREAD NETWORK: to create one, run 'make -C docker new-network' on the host"
    echo "[ot-net-up] NO THREAD NETWORK: (runs ot-net-new.sh in the container)."
    exit 0
fi

# Run an ot-cli command and log it if it doesn't say Done. Not fatal: 'thread start'
# can error harmlessly if the network is already up (e.g. on the rcp-watchdog path), and
# the attach poll below is what decides success either way.
ot() {
    local out
    out=$(ot-cli "$@" 2>&1)
    if ! grep -q '^Done' <<<"$out"; then
        echo "[ot-net-up] warning: 'ot-cli $*' said: $(tr '\n' ' ' <<<"$out")" >&2
    fi
}

echo "[ot-net-up] bringing Thread interface up, starting network..."
ot ifconfig up
ot thread start

for _ in $(seq 1 "$ATTEMPTS"); do
    if ot-cli state | grep -E '^(leader|router|child)' >/dev/null; then
        echo "[ot-net-up] attached to mesh; network ready."
        exit 0
    fi
    echo "[ot-net-up] network not ready, waiting $INTERVAL seconds..."
    sleep "$INTERVAL"
done

echo "[ot-net-up] timed out waiting to attach to mesh, check logs in /matter2mqtt-run/logs/otbr-agent" >&2
exit 1
