# matter2mqtt

Bridges a Matter-over-Thread network to MQTT (no Matter-over-WiFi).

## Design: a "dumb", jailed Matter bridge

- Everything (otbr-agent, matter-server, bluetoothd, avahi, the MQTT bridge) runs in one container with
  `--network none`: no internet, no LAN. Devices can't reach anything and nothing can reach them.
- Devices are isolated by physical radio (ESP32-C6 Thread RCP over USB). The only way in or out is MQTT,
  over a unix socket proxied by the host (`matter2mqtt-mqtt-proxy`). BLE for commissioning goes through
  `matter2mqtt-bluez-proxy` (xdg-dbus-proxy).
- Never add anything that bridges Thread/IPv6/mDNS onto the LAN or gives the container network access.
  Expected consequence: no DCL cert updates (see README "Normal errors").
- The bridge speaks a Z2M-compatible MQTT dialect on `matter2mqtt/...`, so ZMW consumes it like a second
  zigbee2mqtt network.

## Layout

- `mqtt_bridge/matter2mqtt/`: the bridge (Python, runs in the container). `matter/` talks to matter-server,
  `z2m/` emulates the Z2M MQTT API, `names.py` keeps persistent renames. Config: `mqtt-bridge.json`.
- `docker/`: Dockerfile, s6-overlay services, dev Makefile (`start`, `sync-bridge`, `redeploy-bridge`,
  `test-bridge`, `new-network`, `shell`, `logs`).
- `systemd/`: unit templates, installed by root `make install_systemd`.
- `fw_simple/`: ESP32-C6 ot_rcp firmware (what's used). `fw_btmux/`: WIP BLE+spinel mux firmware.
- `notes/NOTES.md`: design notes and rationale. `README.md`: setup / network migration.

## Workflow

- Tests run inside the container: `make -C docker test-bridge` (syncs the bridge first). Don't run them on the
  dev machine.
- Iterate on bridge code with `make -C docker redeploy-bridge`; no image rebuild needed.
