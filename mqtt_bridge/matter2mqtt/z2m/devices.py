"""Matter nodes -> zigbee2mqtt-style devices.

A Device holds both the published schema (`info`, what goes to mt2m/bridge/devices) and the
adapter that maps z2m properties onto Matter (`props`, the Caps from caps.py).

Published entry, one per commissioned node:
{
  "node_id": 1, "friendly_name": "...", "available": true,
  "network": "thread",               # thread | wifi | ethernet | unknown
  "manufacturer": "...", "vendor_id": 4107, "model": "...", "product_id": 1,
  "serial_number": "...", "unique_id": "...", "software_version": "...", "hardware_version": "...",
  "interview_completed": true, "interviewing": false, "last_interview": "...", "interview_version": 6,
  "definition": {"vendor": "...", "model": "...", "description": "Extended Color Light",
                 "supports_ota": true,
                 "exposes": [ ... z2m-style exposes, each tagged with its "endpoint" ... ]},
  "endpoints": {"1": {"device_types": ["Extended Color Light"], "clusters": ["OnOff", ...]}}
}
"""
from collections import Counter

from ..matter.data_model import (
    BASIC_INFO, BASIC_INFO_FIELDS, DESCRIPTOR, DEVICE_TYPE_FIELD, DEVICE_TYPE_LIST,
    ETHERNET_DIAG, NODE_LABEL, OTA_REQUESTOR, SWITCH, THREAD_DIAG, UNIQUE_ID, WIFI_DIAG,
    attr_tree, cluster_name, device_type_name, live_value, struct_field,
)
from .caps import ActionCap, BATTERY, endpoint_caps, sensor_caps

# A device's state goes to mt2m/<friendly_name>, so it can't be named like a bridge topic
RESERVED_NAMES = {"bridge", "ping", "discover", "provision"}


def friendly_name(label, node_id):
    """The name we publish a device under: its NodeLabel, or matter_<node_id> if it hasn't got
    a usable one."""
    return label if valid_name(label) else f"matter_{node_id}"


def valid_name(label):
    if not isinstance(label, str) or not label.strip() or label in RESERVED_NAMES:
        return False
    if "+" in label or "#" in label:  # mqtt wildcards, not allowed in a publish topic
        return False
    return label.split("/")[-1] not in ("set", "get")  # mt2m/<name> would look like a request


class Device:
    def __init__(self, node):
        self.node = node
        self.node_id = node.node_id
        tree = attr_tree(node.node_data.attributes)
        root = tree.get(0, {})
        basic = root.get(BASIC_INFO, {})

        # From the live cache, not the interview dump: a rename writes NodeLabel on the device
        self.friendly_name = self.current_name()
        self.unique_id = live_value(node, 0, BASIC_INFO, UNIQUE_ID)

        endpoints = {}
        groups = []  # (ep, kind, actuators, sensors)
        device_types = []  # names, non-root endpoints, for definition.description
        for ep in sorted(tree):
            cl = tree[ep]
            dts = [struct_field(d, DEVICE_TYPE_FIELD, "deviceType")
                   for d in cl.get(DESCRIPTOR, {}).get(DEVICE_TYPE_LIST) or []]
            dts = [dt for dt in dts if dt is not None]
            endpoints[str(ep)] = {
                "device_types": [device_type_name(dt) for dt in dts],
                "clusters": [cluster_name(cid) for cid in sorted(cl)],
            }
            if ep != 0:
                groups.append((ep, *endpoint_caps(ep, cl, dts)))
                device_types.extend(endpoints[str(ep)]["device_types"])
            else:
                # PowerSource usually sits on the root endpoint, and battery is a device-level
                # property in z2m, so take just that from endpoint 0
                groups.append((ep, None, [], sensor_caps(ep, cl, only=BATTERY)))

        # Like z2m, a property name repeated across endpoints gets suffixed: state_1, state_2
        caps = [c for _, _, act, sens in groups for c in act + sens]
        counts = Counter(c.name for c in caps)
        for c in caps:
            if counts[c.name] > 1:
                c.prop = f"{c.name}_{c.ep}"
        self.props = {c.prop: c for c in caps}

        exposes = []
        for ep, kind, act, sens in groups:
            if act:
                exposes.append({"type": kind, "endpoint": ep,
                                "features": [e for c in act for e in c.expose()]})
            exposes.extend(e for c in sens for e in c.expose())

        if THREAD_DIAG in root:
            network = "thread"
        elif WIFI_DIAG in root:
            network = "wifi"
        elif ETHERNET_DIAG in root:
            network = "ethernet"
        else:
            network = "unknown"

        # Matter interviews a node (reads its whole data model) right after commissioning, but
        # matter-server only hands a node to clients once that's done, so from here it's always
        # finished -- there's no "interviewing" state to report. Hardcoded for z2m compatibility;
        # last_interview / interview_version below are the real data.
        self.info = {"node_id": self.node_id, "friendly_name": self.friendly_name,
                     "available": node.available, "network": network,
                     "interview_completed": True, "interviewing": False,
                     "last_interview": node.node_data.last_interview,
                     "interview_version": node.node_data.interview_version}
        for aid, key in BASIC_INFO_FIELDS.items():
            if aid in basic:
                self.info[key] = basic[aid]
        # z2m keeps what a device *is* (and can do) under `definition`, separate from the
        # per-node facts above. vendor/model repeat manufacturer/model, as they do in z2m.
        self.info["definition"] = {
            "vendor": self.info.get("manufacturer"),
            "model": self.info.get("model"),
            "description": ", ".join(dict.fromkeys(device_types)) or None,
            "supports_ota": OTA_REQUESTOR in root,
            "exposes": exposes,
        }
        self.info["endpoints"] = endpoints

    def current_name(self):
        """The name the device reports right now. It differs from friendly_name when something
        renamed it after this Device was built."""
        return friendly_name(live_value(self.node, 0, BASIC_INFO, NODE_LABEL), self.node_id)

    def state(self):
        """Current z2m state, from matter-server's (live-updated) attribute cache. Momentary
        properties (button actions) are events, so they're published separately, not here."""
        return {prop: cap.read(self.node) for prop, cap in self.props.items() if not cap.momentary}

    def action(self, endpoint_id, cluster_id, event_id, data):
        """(property, action) for a Switch event on this node, or None if we don't publish it."""
        if cluster_id != SWITCH:
            return None
        for prop, cap in self.props.items():
            if isinstance(cap, ActionCap) and cap.ep == endpoint_id:
                action = cap.event(event_id, data)
                return None if action is None else (prop, action)
        return None
