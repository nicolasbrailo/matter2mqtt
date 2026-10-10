"""Bridge config: a json file whose path comes from argv (the s6 run script points it at the
run dir, so it can be edited on the host). Keys missing from the file keep their default.
"""
import json

DEFAULTS = {
    # Base mqtt topic: everything the bridge publishes or listens to lives under <mqtt_topic>/
    "mqtt_topic": "mt2m",
    # Unix socket to the broker; the host relays it to the LAN broker
    "mqtt_socket": "/matter2mqtt-run/mqtt.sock",
    "matter_server_url": "ws://127.0.0.1:5580/ws",
}


def load(path):
    with open(path) as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ValueError(f"{path}: expected a json object")
    unknown = set(cfg) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"{path}: unknown keys {sorted(unknown)}; have {sorted(DEFAULTS)}")
    cfg = {**DEFAULTS, **cfg}
    topic = cfg["mqtt_topic"]
    if not isinstance(topic, str) or not topic.strip("/") or topic != topic.strip("/") \
            or any(c in topic for c in "+#"):
        raise ValueError(f"{path}: bad mqtt_topic {topic!r}")
    return cfg
