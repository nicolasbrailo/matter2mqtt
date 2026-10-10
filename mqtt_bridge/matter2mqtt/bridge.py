#!/usr/bin/env python3
"""Matter -> MQTT bridge, zigbee2mqtt style.

  mt2m/ping                 {}                   -> mt2m {"pong":{}}
  mt2m/provision            {"code":"12345678901"} or {"code":"MT:..."} -- commission a new
                            device over BLE+Thread; progress on mt2m/bridge/event
  mt2m/discover             {}                   -> mt2m/bridge/devices (retained) + every device's state
  mt2m/bridge/request/<subject>                  -> mt2m/bridge/response/<subject>, z2m style:
                            {"status":"ok","data":{...}} or {"status":"error","error":"..."},
                            echoing "transaction" from the request if it had one.
                            subjects:
                              device/dump   {"device":"<device>"} -- every attribute the node
                                            has, named (what matter-discover.py printed)
                              device/rename {"from":"<device>","to":"KitchenLight"} -- writes
                                            the name onto the device (Matter's NodeLabel)
  mt2m/<device>/set         {"state":"ON", "brightness":100, "transition":1.5}
                            options: transition (s), with_on_off (false = don't switch on
                            when setting brightness)
  mt2m/<device>/get         {"state":""}  ({} reads everything) -> mt2m/<device>
  mt2m/<device>                                  <- state, published on every change, and
                            {"action":"double"} on a button press (an event: never retained,
                            never merged into the state message)
  mt2m/<device>/availability                     <- {"state":"online"|"offline"} (retained)
  mt2m/bridge/state                              <- the bridge itself, offline via the mqtt will

mt2m is the default base topic; mqtt_topic in the config file changes it. <device> is a
friendly_name, unique_id or node_id (tried in that order). Errors go to mt2m as
{"error": {...}}.

Run it with `python3 main.py <config.json>` (or `python3 -m matter2mqtt <config.json>`). This module is the wiring: it
owns the pieces and decides what gets published where.
  config.py               the json config: base topic, broker socket, matter-server url
  matter/connection.py    the matter-server connection, on its own loop in a background thread
  matter/data_model.py    what Matter calls things: ids, names, raw values
  matter/registry.py      the device table, kept current from that connection
  matter/provisioning.py  commissioning a new device
  z2m/caps.py             one z2m property and everything it knows about itself
  z2m/devices.py          Matter clusters -> z2m devices, properties and schema
  z2m/control.py          z2m set/get/rename semantics
  z2m/mqtt_link.py        paho, topic names, topic -> route

Threads: paho runs the connection on the main thread; all Matter work (and the device table)
lives on the matter loop, so a route is parsed on the paho thread and handed over.
"""
import sys

from . import config
from .errors import RequestError, describe
from .matter.connection import Matter
from .matter.data_model import dump_node
from .matter.provisioning import Provisioner
from .matter.registry import DeviceRegistry
from .z2m import control
from .z2m.devices import Device
from .z2m.mqtt_link import MqttLink


def log(msg):
    print(f"[bridge] {msg}", flush=True)


class Bridge:
    """Runs on the matter loop, except for the constructor and the routes handed to it."""

    def __init__(self, link, matter_url):
        self.link = link
        self.matter = Matter(matter_url, on_connect=self.on_matter_connect,
                             on_disconnect=self.on_matter_disconnect)
        self.registry = DeviceRegistry(self.matter, Device, on_devices=self.publish_devices,
                                       on_state=self.publish_state, on_action=self.publish_action,
                                       on_renamed=self.forget_name)
        self.provisioner = Provisioner(self.matter, self.on_provision_progress)
        self.routes = {
            "ping": self.ping,
            "discover": self.discover,
            "provision": self.provision,
            "request": self.request,
            "device": self.device_request,
        }

    # --- publishing -----------------------------------------------------------

    def error(self, request, msg):
        log(f"{request}: {msg}")
        self.link.publish(self.link.topic, {"error": {"request": request, "msg": msg}})

    def event(self, type_, **data):
        """z2m-style bridge/event: progress a client can follow without reading the logs."""
        self.link.publish(self.link.event_topic, {"type": type_, "data": data})

    def publish_devices(self, devices):
        self.link.publish(self.link.devices_topic, [d.info for d in devices], retain=True)
        for dev in devices:
            self.publish_availability(dev)
            self.publish_state(dev)

    def publish_state(self, dev):
        self.link.publish(self.link.device_topic(dev.friendly_name), dev.state())

    def publish_availability(self, dev, online=None):
        """z2m-style availability. A node is `available` while matter-server can reach it."""
        if online is None:
            online = dev.node.available
        self.link.publish(self.link.availability_topic(dev.friendly_name),
                          {"state": "online" if online else "offline"}, retain=True)

    def forget_name(self, old_name, dev):
        """A device is published under a new name: drop the retained messages under the old one,
        or the broker keeps serving them to every new subscriber."""
        log(f"renamed {old_name!r} -> {dev.friendly_name!r}")
        self.link.clear_retained(self.link.device_topic(old_name))
        self.link.clear_retained(self.link.availability_topic(old_name))

    def publish_action(self, dev, prop, action):
        log(f"{dev.friendly_name}: {prop}={action}")
        # Its own message, not retained: a retained press would replay to every new subscriber
        self.link.publish(self.link.device_topic(dev.friendly_name), {prop: action})

    def respond(self, subject, transaction, data=None, error=None):
        msg = {"status": "error" if error else "ok"}
        if error:
            msg["error"] = error
        else:
            msg["data"] = {} if data is None else data
        if transaction is not None:
            msg["transaction"] = transaction
        self.link.publish(self.link.response_topic(subject), msg)

    # --- matter events --------------------------------------------------------

    def on_matter_connect(self, client):
        self.registry.attach(client)

    def on_matter_disconnect(self):
        """matter-server is gone, so we no longer know anything about any device."""
        for dev in self.registry.devices.values():
            self.publish_availability(dev, online=False)

    def on_provision_progress(self, step, message, **data):
        self.event("provision", step=step, message=message, **data)

    # --- routes ---------------------------------------------------------------

    def dispatch(self, route, payload):
        """Called on the paho thread: hand the request to the matter loop."""
        name, *args = route
        self.matter.submit(self.routes[name](*args, payload))

    async def ping(self, payload):
        self.link.publish(self.link.topic, {"pong": {}})

    async def discover(self, payload):
        """Rebuild the device table and republish it, even if nothing changed."""
        try:
            self.require_client()
            self.registry.rebuild()
        except Exception as e:
            self.error("discover", describe(e))

    async def provision(self, payload):
        """Commission a device, then republish the device list so it shows up right away."""
        try:
            node_data = await self.provisioner.provision(payload)
        except Exception as e:
            return self.error("provision", describe(e))  # failed step already reported
        # The node arrives through NODE_ADDED too, but rebuild now so mt2m/bridge/devices is
        # already correct when the "done" step goes out
        self.registry.rebuild()
        dev = self.registry.devices.get(node_data.node_id)
        name = dev.friendly_name if dev else f"matter_{node_data.node_id}"
        self.on_provision_progress("done", f"{name} is commissioned; set it with {self.link.device_topic(name)}/set",
                                   node_id=node_data.node_id, friendly_name=name)

    async def device_request(self, name, op, payload):
        """mt2m/<device>/set and /get."""
        try:
            client = self.require_client()
            if not isinstance(payload, dict):
                raise RequestError("payload must be a json object")
            dev = self.require_device(name)
            if op == "set":
                await control.apply_set(client, dev, payload)
            else:
                await control.refresh(client, dev, payload)
                self.publish_state(dev)
        except Exception as e:
            self.error(f"{name}/{op}", describe(e))

    async def request(self, subject, payload):
        """The z2m-style bridge/request -> bridge/response envelope."""
        transaction = payload.get("transaction") if isinstance(payload, dict) else None
        try:
            if not isinstance(payload, dict):
                raise RequestError("payload must be a json object")
            handler = REQUESTS.get(subject)
            if handler is None:
                raise RequestError(f"unknown request {subject!r}; have {sorted(REQUESTS)}")
            self.respond(subject, transaction, data=await handler(self, payload))
        except Exception as e:
            log(f"bridge/request/{subject}: {e!r}")
            self.respond(subject, transaction, error=describe(e))

    async def req_device_rename(self, payload):
        """z2m's device/rename. The name goes onto the device, so its topics move with it."""
        client = self.require_client()
        dev = self.require_device(payload.get("from"))
        old = dev.friendly_name
        taken = {d.friendly_name for d in self.registry.devices.values() if d is not dev}
        await control.rename(client, dev, payload.get("to"), taken)
        # rebuild republishes the list and the device's topics under the new name, and reports
        # the rename back through on_renamed, which clears the old topics
        self.registry.rebuild()
        renamed = self.registry.devices.get(dev.node_id)
        return {"from": old, "to": renamed.friendly_name if renamed else old}

    async def req_device_dump(self, payload):
        """Every attribute of one node, for debugging a device the schema doesn't cover."""
        self.require_client()
        dev = self.require_device(payload.get("device"))
        return {"node_id": dev.node_id, "friendly_name": dev.friendly_name,
                "endpoints": dump_node(dev.node)}

    def require_client(self):
        if self.matter.client is None:
            raise RequestError("matter-server not connected")
        return self.matter.client

    def require_device(self, name):
        dev = self.registry.resolve(name) if name is not None else None
        if dev is None:
            raise RequestError(f"unknown device {name!r}")
        return dev


REQUESTS = {
    "device/dump": Bridge.req_device_dump,
    "device/rename": Bridge.req_device_rename,
}


def main():
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <config.json>")
    cfg = config.load(sys.argv[1])
    log(f"config from {sys.argv[1]}: {cfg}")
    bridge = None

    def on_ready(link):
        nonlocal bridge
        bridge = Bridge(link, cfg["matter_server_url"])

    def on_route(route, payload):
        bridge.dispatch(route, payload)

    MqttLink(cfg["mqtt_topic"], cfg["mqtt_socket"],
             on_ready=on_ready, on_route=on_route).run_forever()


if __name__ == "__main__":
    main()
