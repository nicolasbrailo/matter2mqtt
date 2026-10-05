#!/usr/bin/env bash
set -uo pipefail

echo "Services:"
for s in /run/service/*; do
  printf "%-22s " "$(basename "$s")"; /command/s6-svstat "$s";
done

echo "Thread network status:"
ot-ctl state

