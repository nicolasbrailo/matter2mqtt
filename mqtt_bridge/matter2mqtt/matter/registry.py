"""The device table: Devices built from matter-server's node cache, kept current.

Owns the matter-side subscriptions that keep it current, and tells the bridge what changed
through callbacks. It never publishes anything itself.
"""
import asyncio

from matter_server.common.models import EventType

from .data_model import NODE_LABEL_PATH

STATE_DEBOUNCE = 0.2  # s; a transition fires a burst of attribute updates, publish once


def log(msg):
    print(f"[registry] {msg}", flush=True)


class DeviceRegistry:
    def __init__(self, matter, device_cls, on_devices, on_state, on_action, on_renamed):
        """on_devices(devices) after every rebuild; on_state(device) when a node's attributes
        changed (debounced); on_action(device, prop, action) for a button press;
        on_renamed(old_name, device) when a device's published name changed."""
        self.matter = matter
        self.device_cls = device_cls
        self.on_devices = on_devices
        self.on_state = on_state
        self.on_action = on_action
        self.on_renamed = on_renamed
        self.devices = {}       # node_id -> Device
        self._attr_unsubs = {}  # node_id -> unsubscribe fns for its attribute updates
        self._pending_state = set()

    def attach(self, client):
        """Subscribe to a (re)connected matter client and build the table."""
        self._attr_unsubs = {}  # the old client's subscriptions died with it
        for ev in (EventType.NODE_ADDED, EventType.NODE_UPDATED, EventType.NODE_REMOVED):
            client.subscribe_events(lambda *_: self.rebuild(), event_filter=ev)
        # Node events carry their own node_id, so unlike attribute updates one subscription does
        client.subscribe_events(self._on_node_event, event_filter=EventType.NODE_EVENT)
        self.rebuild()

    def rebuild(self):
        """Rebuild the table from matter-server's cache and hand it to the bridge."""
        client = self.matter.client
        was_named = {nid: d.friendly_name for nid, d in self.devices.items()}
        self.devices = {n.node_id: self.device_cls(n) for n in client.get_nodes()}
        # Attribute-update callbacks don't say which node changed, so subscribe per node
        for node_id in list(self._attr_unsubs):
            if node_id not in self.devices:
                for unsub in self._attr_unsubs.pop(node_id):
                    unsub()
        for node_id in self.devices:
            if node_id not in self._attr_unsubs:
                self._attr_unsubs[node_id] = [
                    client.subscribe_events(
                        lambda *_, nid=node_id: self._schedule_state(nid),
                        event_filter=EventType.ATTRIBUTE_UPDATED, node_filter=node_id),
                    # A rename from anywhere else -- another controller, the vendor's app --
                    # arrives only as this one attribute changing, and the name we publish
                    # devices under has to follow it
                    client.subscribe_events(
                        lambda *_, nid=node_id: self._on_label_change(nid),
                        event_filter=EventType.ATTRIBUTE_UPDATED, node_filter=node_id,
                        attr_path_filter=NODE_LABEL_PATH),
                ]
        log(f"{len(self.devices)} device(s): "
            f"{', '.join(d.friendly_name for d in self.devices.values())}")
        self.on_devices(list(self.devices.values()))
        for node_id, dev in self.devices.items():
            old = was_named.get(node_id)
            if old is not None and old != dev.friendly_name:
                self.on_renamed(old, dev)

    def resolve(self, name):
        """A device by friendly_name, unique_id or node_id, in that order."""
        # TODO: two nodes can carry the same NodeLabel; first match wins, silently
        for key in (lambda d: d.friendly_name, lambda d: d.unique_id, lambda d: str(d.node_id)):
            for dev in self.devices.values():
                if key(dev) == name:
                    return dev
        return None

    def _on_node_event(self, event, data):
        """Buttons: Matter reports presses as Switch cluster events, not attribute updates."""
        dev = self.devices.get(data.node_id)
        if dev is None:
            return
        hit = dev.action(data.endpoint_id, data.cluster_id, data.event_id, data.data)
        if hit is not None:
            self.on_action(dev, *hit)

    def _on_label_change(self, node_id):
        dev = self.devices.get(node_id)
        if dev is None or dev.current_name() == dev.friendly_name:
            return  # our own rename: rebuild() already published under the new name
        log(f"{dev.friendly_name}: renamed to {dev.current_name()!r} elsewhere")
        self.rebuild()

    def _schedule_state(self, node_id):
        if node_id not in self._pending_state:
            self._pending_state.add(node_id)
            asyncio.get_running_loop().call_later(STATE_DEBOUNCE, self._flush_state, node_id)

    def _flush_state(self, node_id):
        self._pending_state.discard(node_id)
        if dev := self.devices.get(node_id):
            self.on_state(dev)
