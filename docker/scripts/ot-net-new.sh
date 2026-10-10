#!/usr/bin/env bash
# Create a brand new Thread network: random network key, PAN ID, channel and name,
# committed as the active dataset (persisted under /matter2mqtt-run/otbr), then brought up.
#
# Run by hand when ot-net-up.sh reports there is no network: `make -C docker new-network`.
# Refuses to run if a network already exists, so it can't replace one by accident --
# devices commissioned on the old network would be cut off.
set -uo pipefail

dataset=$(timeout 5 ot-ctl dataset active 2>&1)
if grep -q '^Done' <<<"$dataset"; then
    echo "[ot-net-new] a Thread network is already configured; refusing to replace it" >&2
    exit 1
elif ! grep -q 'NotFound' <<<"$dataset"; then
    echo "[ot-net-new] otbr-agent isn't answering: $(tr '\n' ' ' <<<"$dataset")" >&2
    exit 1
fi

for cmd in "dataset init new" "dataset commit active"; do
    # shellcheck disable=SC2086  # word-split on purpose: "dataset init new" is 3 args
    out=$(timeout 5 ot-ctl $cmd 2>&1)
    if ! grep -q '^Done' <<<"$out"; then
        echo "[ot-net-new] 'ot-ctl $cmd' failed: $(tr '\n' ' ' <<<"$out")" >&2
        exit 1
    fi
done

name=$(timeout 5 ot-ctl networkname 2>/dev/null | head -1)
echo "[ot-net-new] created new Thread network '$name'"
exec ot-net-up.sh
