.PHONY: rebuild start start-bare bluez-proxy mqtt-proxy sync-bridge redeploy-bridge undeploy-bridge bridge-logs test-bridge stop shell logs

NAME := matter2mqtt

# shell/logs attach to the running container (only `matter2mqtt` now -- commissioning
# is no longer a separate mode; see `start`).
CONTAINER = $(shell docker ps --format '{{.Names}}' | grep -E '^$(NAME)$$' | head -1)

# Host directory bind-mounted at /mt2mqtt-run so Thread network state, the
# per-service logs under /mt2mqtt-run/logs, and any future editable configs
# survive `make stop` (which does `docker rm -f`) and can be backed up directly
# from the host. Override with `make start RUNDIR=...`.
RUNDIR := $(HOME)/run/matter2mqtt

rebuild:
	docker build -t matter2mqtt:dev -f ./Dockerfile \
		--build-context host_driver=../fw_btmux/host_driver \
		--build-context mqtt_bridge=../mqtt_bridge \
		.

# Pick the dongle by its stable by-id name, but resolve it to the real
# /dev/ttyACMx node — that path has no colons, so --device can parse it
# (--device splits its arg on ':', and the by-id name embeds a MAC with colons).
RCP_BYID := '/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_*'
RCP      := $(shell readlink -f '$(RCP_BYID)')

# The container runs fully isolated (its own netns: wpan0, avahi and its own
# system dbus never touch the LAN) AND can commission over BLE. It reaches the
# HOST's BlueZ over a FILTERED D-Bus proxy (see `bluez-proxy` below): the proxy
# socket lives in RUNDIR, so it rides the existing /mt2mqtt-run mount -- no
# separate bind, and the container never sees the raw host system bus. The host's
# bluetoothd does all radio work in init_net; only BTP-over-GATT D-Bus traffic
# crosses. matter-server/run detects a usable BlueZ on that proxy (org.bluez
# owned) and enables BLE; if the proxy isn't running (or host BT is off) it
# degrades cleanly to an operational-only bridge. So: run `make bluez-proxy` in
# another terminal only when you want to commission; otherwise just `make start`.
start:
	mkdir -p '$(RUNDIR)/logs'
	docker run -d --name $(NAME) \
		--network none \
		-v '$(RUNDIR)':/mt2mqtt-run \
		--device $(RCP):/dev/ttyACM0 \
		--device /dev/net/tun --cap-add NET_ADMIN \
		matter2mqtt:dev

# Bare debug shell: same image and device/volume/cap setup as `make start`, but
# overrides the /init entrypoint so s6-overlay never runs — nothing is started
# automatically. Use this to bring the stack up one service at a time (e.g. when
# testing the C6 mux) without the otbr-agent-net oneshot spinning on a net that
# isn't ready and timing out the whole container (S6_BEHAVIOUR_IF_STAGE2_FAILS=2).
#
# With no s6 supervisor there's no s6-rc; start dependencies by hand, e.g.:
#   dbus-daemon --system & avahi-daemon & ...   (see the s6 run scripts)
# Runs interactively as `matter2mqtt` and is removed on exit (--rm). `make shell`
# from another terminal opens a second shell into it.
start-bare:
	mkdir -p '$(RUNDIR)/logs'
	docker run -it --rm --name $(NAME) \
		--security-opt apparmor=unconfined \
		--entrypoint /bin/bash \
		-v '$(RUNDIR)':/mt2mqtt-run \
		--device $(RCP):/dev/ttyACM0 \
		--device /dev/net/tun --cap-add NET_ADMIN \
		matter2mqtt:dev

# Filtered D-Bus proxy exposing ONLY org.bluez to the container, so commissioning
# can drive the host's bluetoothd without handing the container the whole host
# system bus -- which, for a root container, is ~host root (systemd
# StartTransientUnit, UDisks2 mounting host disks, NetworkManager dispatcher
# scripts). --filter default-denies; --talk=org.bluez is the only grant.
#
# Runs in the FOREGROUND as your normal user (no sudo: the logged-in user is
# at_console, the same grant the desktop BT applet uses to scan/connect). Leave
# it running in its own terminal while commissioning; Ctrl-C when done. The
# socket lives in RUNDIR, so it appears in the container at
# /mt2mqtt-run/bluez/bluez-proxy.sock via the existing mount -- matter-server/run picks
# it up automatically. When it's not running, `make start` is operational-only.
bluez-proxy:
	mkdir -p '$(RUNDIR)/bluez'
	rm -f '$(RUNDIR)/bluez/bluez-proxy.sock'
	sudo xdg-dbus-proxy \
		unix:path=/run/dbus/system_bus_socket \
		'$(RUNDIR)/bluez/bluez-proxy.sock' \
		--filter --talk=org.bluez

BROKER      ?= 10.0.0.10
#BROKER_PORT ?= 4500
BROKER_PORT ?= 1883

# Host-side MQTT relay -- the single sanctioned LAN crossing. The container runs
# --network none, so its only path to the LAN broker is a unix socket bridged by
# a host process. socat listens on a unix socket in RUNDIR (rides the existing
# /mt2mqtt-run mount, so it's /mt2mqtt-run/mqtt.sock inside the container) and
# forwards each connection to the broker. The container's bridge connects with
# paho transport="unix" -- no container-side relay needed.
#
# No sudo (unlike bluez-proxy): MQTT relaying is plain byte-forwarding, no D-Bus
# EXTERNAL/SO_PEERCRED auth, and the socket lives in the user-owned RUNDIR. Runs
# in the FOREGROUND; leave it in its own terminal. It only ever talks to
# $(BROKER):$(BROKER_PORT) -- override with `make mqtt-proxy BROKER=... BROKER_PORT=...`.
# Needs socat on the host: `sudo apt install socat`.
mqtt-proxy:
	mkdir -p '$(RUNDIR)'
	rm -f '$(RUNDIR)/mqtt.sock'
	socat -d -d \
		UNIX-LISTEN:'$(RUNDIR)/mqtt.sock',fork,reuseaddr \
		TCP:$(BROKER):$(BROKER_PORT)

# Matter->MQTT bridge. It runs as the s6 `mqtt-bridge` service, from the copy baked into the
# image -- unless a dev copy exists at /mt2mqtt-run/bridge2, which the service runs instead.
# These targets manage that dev copy, so iterating on the bridge is stop/sync/start rather
# than a full image rebuild. Output goes to $(RUNDIR)/logs/mqtt-bridge; `make bridge-logs`
# follows it. Needs `make mqtt-proxy` in another terminal (the bridge's only path to the
# broker).
#
# The container runs the bridge as root, so Python leaves root-owned __pycache__ dirs in the
# dev copy; sync-bridge deletes the old copy from inside the container for that reason.
BRIDGE_DIR := /mt2mqtt-run/bridge2
BRIDGE_SVC := /run/service/mqtt-bridge

sync-bridge:
	docker exec $(CONTAINER) rm -rf $(BRIDGE_DIR)
	mkdir -p '$(RUNDIR)/bridge2'
	cp -r $(CURDIR)/../mqtt_bridge/matter2mqtt $(CURDIR)/../mqtt_bridge/tests \
	      $(CURDIR)/../mqtt_bridge/main.py '$(RUNDIR)/bridge2/'

# Stop the service, swap in the working tree's bridge, start it again.
redeploy-bridge:
	docker exec $(CONTAINER) /command/s6-svc -wd -d $(BRIDGE_SVC)
	$(MAKE) sync-bridge
	docker exec $(CONTAINER) /command/s6-svc -u $(BRIDGE_SVC)

# Drop the dev copy and go back to running the image's bridge.
undeploy-bridge:
	docker exec $(CONTAINER) /command/s6-svc -wd -d $(BRIDGE_SVC)
	docker exec $(CONTAINER) rm -rf $(BRIDGE_DIR)
	docker exec $(CONTAINER) /command/s6-svc -u $(BRIDGE_SVC)

bridge-logs:
	tail -n 100 -F '$(RUNDIR)/logs/mqtt-bridge/mqtt-bridge.log'

# The offline tests (schema fixtures and the set/get rules), run against the container's own
# matter-server library rather than whatever python the host has. Note this syncs the dev
# copy, so the next service restart runs it.
test-bridge: sync-bridge
	docker exec -it -w $(BRIDGE_DIR) $(CONTAINER) python3 tests/test_devices.py
	docker exec -it -w $(BRIDGE_DIR) $(CONTAINER) python3 tests/test_control.py

stop:
	docker rm -f $(NAME)

# Attach an interactive shell to the already-running container.
shell:
	docker exec -it $(CONTAINER) bash

# Tail every service log under $(RUNDIR)/logs. The s6 loggers each expose a stable
# <svc>.log symlink to their live file, and the oneshots write <name>.log directly;
# tail -F re-opens across s6-log rotation. These are host-side files (RUNDIR is
# bind-mounted), so this works even when the container is stopped.
logs:
	find '$(RUNDIR)/logs' -name '*.log' -print0 2>/dev/null | xargs -0 -r tail -n 100 -F
