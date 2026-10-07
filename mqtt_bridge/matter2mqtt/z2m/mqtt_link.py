"""The mqtt side: the paho connection, the topic names, and turning topics into routes.

Nothing here knows about Matter. Incoming topics are parsed into a route -- ("ping",),
("discover",), ("provision",), ("request", subject) or ("device", name, op) -- and handed to
the bridge, which decides what to do. Everything the bridge publishes goes out as json.

paho runs the connection on the main thread; the bridge does its work on the matter loop.
"""
import json

import paho.mqtt.client as mqtt

SOCKET = "/matter2mqtt-run/mqtt.sock"
TOPIC = "mt2m"
DEVICES_TOPIC = f"{TOPIC}/bridge/devices"
BRIDGE_STATE_TOPIC = f"{TOPIC}/bridge/state"
EVENT_TOPIC = f"{TOPIC}/bridge/event"

# Bare topics that are requests rather than something we published ourselves
COMMANDS = ("ping", "discover", "provision")


def device_topic(name):
    return f"{TOPIC}/{name}"


def availability_topic(name):
    return f"{TOPIC}/{name}/availability"


def response_topic(subject):
    return f"{TOPIC}/bridge/response/{subject}"


def log(msg):
    print(f"[mqtt] {msg}", flush=True)


def route_for(topic):
    """Route for an incoming topic, or None if it's one of our own publications."""
    rest = topic.split("/")[1:]
    if len(rest) == 1 and rest[0] in COMMANDS:
        return (rest[0],)
    if len(rest) >= 3 and rest[:2] == ["bridge", "request"]:
        return ("request", "/".join(rest[2:]))
    if len(rest) >= 2 and rest[-1] in ("set", "get"):
        return ("device", "/".join(rest[:-1]), rest[-1])
    return None


class MqttLink:
    def __init__(self, on_ready, on_route):
        """on_ready(link) runs once the broker connection is up and subscribed; on_route(route,
        payload) runs for every incoming request."""
        self.on_ready = on_ready
        self.on_route = on_route
        self.ready = False
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, transport="unix")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        # Last will: the broker publishes this for us if we die or drop off
        self.client.will_set(BRIDGE_STATE_TOPIC, json.dumps({"state": "offline"}), retain=True)

    def run_forever(self, socket=SOCKET):
        log(f"connecting to {socket!r}")
        # paho ignores the port for unix sockets, but rejects port<=0
        self.client.connect(socket, 1883)
        self.client.loop_forever()

    def publish(self, topic, payload, retain=False):
        self.client.publish(topic, json.dumps(payload, default=str), retain=retain)

    def clear_retained(self, topic):
        """Drop a retained message: an empty payload is how mqtt deletes one."""
        self.client.publish(topic, None, retain=True)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        log(f"connected ({reason_code}), subscribing to {TOPIC + '/#'!r}")
        client.subscribe(f"{TOPIC}/#")
        self.publish(BRIDGE_STATE_TOPIC, {"state": "online"}, retain=True)
        if not self.ready:  # only once mqtt is up, so the bridge's first publishes aren't dropped
            self.ready = True
            self.on_ready(self)

    def _on_message(self, client, userdata, msg):
        route = route_for(msg.topic)
        if route is None:
            return  # our own state/devices/response publications
        log(f"rx {msg.topic} {msg.payload!r}")
        raw = msg.payload.decode(errors="replace").strip()
        try:
            payload = json.loads(raw) if raw else {}  # an empty payload is an empty request
        except ValueError:
            log(f"ignoring {msg.topic}: payload is not json")
            return
        self.on_route(route, payload)
