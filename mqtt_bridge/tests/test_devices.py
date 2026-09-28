#!/usr/bin/env python3
"""Regression test: fixtures/nodes.json -> Device.info == fixtures/devices.json

    python3 tests/test_devices.py      (from the mqtt_bridge directory)

fixtures/devices.json is a real mt2m/bridge/devices capture (two IKEA KAJPLATS bulbs on
Thread). fixtures/nodes.json is the matter-server node data that feeds it -- reconstructed
from that capture, not captured itself, so it pins the schema against accidental change
rather than proving what a real bulb reports.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the matter2mqtt package

from matter_server.client.models.node import MatterNode
from matter_server.common.models import MatterNodeData

from matter2mqtt.z2m.devices import Device

FIXTURES = Path(__file__).parent / "fixtures"


def load_nodes():
    for n in json.loads((FIXTURES / "nodes.json").read_text()):
        yield MatterNode(MatterNodeData(
            node_id=n["node_id"],
            date_commissioned=datetime.fromisoformat(n["last_interview"]),
            last_interview=datetime.fromisoformat(n["last_interview"]),
            interview_version=n["interview_version"],
            available=n["available"],
            attributes=n["attributes"]))


def main():
    expected = json.loads((FIXTURES / "devices.json").read_text())
    # default=str matches how the bridge serialises (last_interview is a datetime)
    got = json.loads(json.dumps([Device(n).info for n in load_nodes()], default=str))

    if got == expected:
        print(f"OK: {len(got)} device(s) match fixtures/devices.json")
        return 0
    for exp, act in zip(expected, got):
        for key in sorted(set(exp) | set(act)):
            if exp.get(key) != act.get(key):
                print(f"node {exp.get('node_id')}: {key}\n  expected {exp.get(key)!r}\n  got      {act.get(key)!r}")
    if len(got) != len(expected):
        print(f"device count: expected {len(expected)}, got {len(got)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
