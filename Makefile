.PHONY: rebuild install_systemd

# Production entry point: build the image and install it as systemd services. Everything
# for development (running the container by hand, the foreground proxies, syncing the
# bridge, shells, logs) lives in docker/Makefile.

rebuild:
	$(MAKE) -C docker rebuild

# Install the two proxies and the container as systemd units (templates in systemd/).
# Units are rendered with the values below and written to /etc/systemd/system, then enabled
# but NOT started -- the container unit `docker rm -f`s anything named matter2mqtt, so
# starting it on a dev box would kill a `make -C docker start` container. Start with
#   sudo systemctl start matter2mqtt.service
# (pulls in both proxies). SYSTEMD_RUNDIR is the host state dir (Thread network, logs, MQTT
# socket, mqtt-bridge.json); it's mounted at /matter2mqtt-run in the container. Re-run after
# editing a template.
# Needs xdg-dbus-proxy on the host.
SYSTEMD_RUNDIR ?= $(HOME)/run/matter2mqtt
IMAGE          ?= matter2mqtt:dev
BROKER         ?= 10.0.0.10
BROKER_PORT    ?= 1883
SYSTEMD_UNITS  := matter2mqtt-mqtt-proxy.socket matter2mqtt-mqtt-proxy.service \
                  matter2mqtt-bluez-proxy.service matter2mqtt.service

install_systemd:
	mkdir -p '$(SYSTEMD_RUNDIR)/logs'
	# Bridge config, seeded with the defaults; never overwrites one that's been edited
	test -f '$(SYSTEMD_RUNDIR)/mqtt-bridge.json' || \
		cp '$(CURDIR)/mqtt_bridge/mqtt-bridge.json' '$(SYSTEMD_RUNDIR)/mqtt-bridge.json'
	for u in $(SYSTEMD_UNITS); do \
		sed -e 's|@RUNDIR@|$(SYSTEMD_RUNDIR)|g' \
		    -e 's|@BROKER@|$(BROKER)|g' \
		    -e 's|@BROKER_PORT@|$(BROKER_PORT)|g' \
		    -e 's|@IMAGE@|$(IMAGE)|g' \
		    "$(CURDIR)/systemd/$$u" | sudo tee "/etc/systemd/system/$$u" >/dev/null || exit 1; \
	done
	sudo systemctl daemon-reload
	# mqtt-proxy.service has no [Install]: its socket activates it.
	sudo systemctl enable $(filter-out matter2mqtt-mqtt-proxy.service,$(SYSTEMD_UNITS))
