#!/usr/bin/env bash
# bt-host-check.sh — is the host's BlueZ usable over the filtered D-Bus proxy?
#
# For BLE commissioning the container drives the HOST's bluetoothd over an
# xdg-dbus-proxy socket (see `make bluez-proxy`) that exposes only org.bluez.
# This predicate checks that path is actually usable right now: the proxy socket
# must exist AND org.bluez must be owned on that bus. A present socket alone is
# not enough — the proxy can be up while bluetoothd / the adapter are down.
#
# Usage:  bt-host-check.sh [SOCKET_PATH]
#           SOCKET_PATH defaults to /mt2mqtt-run/bluez/bluez-proxy.sock
# Exit:   0 = host BlueZ usable (enable BLE);  1 = not (socket missing / org.bluez unowned)
set -uo pipefail

HOST_BUS="${1:-/mt2mqtt-run/bluez/bluez-proxy.sock}"

[ -S "$HOST_BUS" ] || exit 1

# NameHasOwner(org.bluez) on the proxied bus. The proxy answers driver calls for
# names it's allowed to --talk to, so this round-trips even though only org.bluez
# is exposed. grep's exit code becomes the script's (pipefail catches dbus-send).
DBUS_SYSTEM_BUS_ADDRESS="unix:path=$HOST_BUS" \
    dbus-send --system --print-reply --dest=org.freedesktop.DBus \
      /org/freedesktop/DBus org.freedesktop.DBus.NameHasOwner \
      string:org.bluez 2>/dev/null | grep -q 'boolean true'
