#!/usr/bin/env python3
"""The z2m set/get rules, against the fixture devices:

    python3 tests/test_control.py      (from the mqtt_bridge directory)

Every case says what the device should end up being told (the Matter commands, in order), or
which RequestError the caller should get back. The fixture bulb (node 1) has state, brightness,
color_temp, colour in both modes, and a read-only color_mode.
"""
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the matter2mqtt package

from matter_server.client.models.node import MatterNode
from matter_server.common.models import MatterNodeData

from matter2mqtt.errors import RequestError
from matter2mqtt.z2m import control
from matter2mqtt.z2m.devices import Device

FIXTURES = Path(__file__).parent / "fixtures"


class FakeClient:
    """Records what would have gone to the device."""

    def __init__(self):
        self.commands = []
        self.reads = []
        self.writes = []

    async def send_device_command(self, node_id, endpoint_id, command):
        self.commands.append(f"{type(command).__name__}({format_fields(command)})")

    async def refresh_attribute(self, node_id, path):
        self.reads.append(path)

    async def write_attribute(self, node_id, path, value):
        self.writes.append(f"{path}={value!r}")


# The SDK puts its own plumbing in the command dataclasses, and our options are always 0
SKIP_FIELDS = ("cluster_id", "command_id", "is_client", "response_type",
               "optionsMask", "optionsOverride")


def format_fields(command):
    return ", ".join(f"{f}={getattr(command, f)}" for f in command.__dataclass_fields__
                     if f not in SKIP_FIELDS)


def bulb():
    n = json.loads((FIXTURES / "nodes.json").read_text())[0]
    when = datetime.fromisoformat(n["last_interview"])
    return Device(MatterNode(MatterNodeData(
        node_id=n["node_id"], date_commissioned=when, last_interview=when,
        interview_version=n["interview_version"], available=True, attributes=n["attributes"])))


SET_CASES = [
    # payload, expected commands (or the RequestError text)
    ({"state": "ON"}, ["On()"]),
    ({"state": "off"}, ["Off()"]),
    ({"state": True}, ["On()"]),
    ({"state": 0}, ["Off()"]),
    # OFF wins: MoveToLevelWithOnOff would switch the light straight back on
    ({"state": "OFF", "brightness": 10}, ["Off()"]),
    # ON first: colour commands are ignored by a device that is still off
    ({"color_temp": 300, "state": "ON"},
     ["On()", "MoveToColorTemperature(colorTemperatureMireds=300, transitionTime=0)"]),
    ({"brightness": 50}, ["MoveToLevelWithOnOff(level=50, transitionTime=0)"]),
    ({"brightness": "123"}, ["MoveToLevelWithOnOff(level=123, transitionTime=0)"]),
    ({"brightness": 50, "with_on_off": False}, ["MoveToLevel(level=50, transitionTime=0)"]),
    ({"brightness": 50, "transition": "1.5"}, ["MoveToLevelWithOnOff(level=50, transitionTime=15)"]),
    ({"color": {"hue": "180", "saturation": 50}},
     ["MoveToHueAndSaturation(hue=127, saturation=127, transitionTime=0)"]),
    ({"color": {"x": 0.3, "y": 0.3}},
     ["MoveToColor(colorX=19661, colorY=19661, transitionTime=0)"]),
    # Rejections: nothing reaches the device
    ({"state": "TOGGLE", "brightness": 10}, RequestError),
    ({"color_temp": 300, "color": {"hue": 1, "saturation": 2}}, RequestError),
    ({"brightness": 300}, RequestError),
    ({"brightness": "abc"}, RequestError),
    ({"nope": 1}, RequestError),
    ({"color_mode": "hs"}, RequestError),
    ({"state": 2}, RequestError),
    ({"transition": -1, "brightness": 10}, RequestError),
    ({"with_on_off": "no", "brightness": 10}, RequestError),
]

# (new name, names the other devices already use) -> the attribute write, or the error
RENAME_CASES = [
    ("KitchenLight", set(), ["0/40/5='KitchenLight'"]),
    ("  Hallway  ", set(), ["0/40/5='Hallway'"]),          # trimmed
    ("kitchen/lamp", set(), ["0/40/5='kitchen/lamp'"]),    # a slash is fine, topics nest
    ("KitchenLight", {"KitchenLight"}, RequestError),      # already taken
    ("bridge", set(), RequestError),                       # reserved
    ("lamp/set", set(), RequestError),                     # would look like a request
    ("lamp+", set(), RequestError),                        # mqtt wildcard
    ("", set(), RequestError),
    (None, set(), RequestError),
    ("x" * 33, set(), RequestError),                       # Matter allows 32
]

GET_CASES = [
    ({"state": ""}, ["1/6/0"]),
    ({"brightness": "", "color_temp": ""}, ["1/8/0", "1/768/7"]),
    ({}, ["1/6/0", "1/8/0", "1/768/7", "1/768/0", "1/768/1", "1/768/3", "1/768/4", "1/768/8"]),
    ({"nope": ""}, RequestError),
]


async def run():
    dev = bulb()
    failures = []

    for payload, expected in SET_CASES:
        client = FakeClient()
        try:
            await control.apply_set(client, dev, payload)
            got = client.commands
        except RequestError as e:
            got = RequestError
            note = str(e)
        else:
            note = ", ".join(got)
        if got != expected:
            failures.append(f"set {payload}: expected {expected}, got {got} ({note})")
        else:
            print(f"  ok  set {json.dumps(payload)} -> {note}")

    for payload, expected in GET_CASES:
        client = FakeClient()
        try:
            await control.refresh(client, dev, payload)
            got = client.reads
        except RequestError as e:
            got, note = RequestError, str(e)
        else:
            note = ", ".join(got)
        if got != expected:
            failures.append(f"get {payload}: expected {expected}, got {got} ({note})")
        else:
            print(f"  ok  get {json.dumps(payload)} -> {note}")

    for new_name, taken, expected in RENAME_CASES:
        client = FakeClient()
        try:
            await control.rename(client, dev, new_name, taken)
            got, note = client.writes, ", ".join(client.writes)
        except RequestError as e:
            got, note = RequestError, str(e)
        if got != expected:
            failures.append(f"rename {new_name!r}: expected {expected}, got {got} ({note})")
        else:
            print(f"  ok  rename {new_name!r} -> {note}")

    for line in failures:
        print(f"FAIL {line}")
    total = len(SET_CASES) + len(GET_CASES) + len(RENAME_CASES)
    print(f"{total - len(failures)}/{total} cases pass")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
