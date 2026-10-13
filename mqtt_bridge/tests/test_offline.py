#!/usr/bin/env python3
"""OfflineTracker, with a fake clock:

    python3 tests/test_offline.py      (from the mqtt_bridge directory)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the matter2mqtt package

from matter2mqtt.offline import OfflineTracker


def check(got, want):
    if got != want:
        raise AssertionError(f"expected {want!r}, got {got!r}")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def case_short_outage_isnt_reported(t, clock):
    t.update(6, online=False)
    clock.t += 600
    check(t.overdue(), [])
    check(t.update(6, online=True), None)  # back before the threshold: nothing to say
    clock.t += 3600
    check(t.overdue(), [])


def case_long_outage_is_reported_once(t, clock):
    t.update(6, online=False)
    clock.t += 901
    check(t.overdue(), [(6, 901)])
    clock.t += 600
    check(t.overdue(), [])


def case_recovery_after_report_says_how_long(t, clock):
    t.update(6, online=False)
    clock.t += 901
    t.overdue()
    clock.t += 99
    check(t.update(6, online=True), 1000)
    check(t.update(6, online=True), None)  # repeated online: no second recovery


def case_repeated_offline_keeps_first_time(t, clock):
    # Every rebuild republishes availability, so the same offline arrives many times
    t.update(6, online=False)
    clock.t += 500
    t.update(6, online=False)
    clock.t += 401
    check(t.overdue(), [(6, 901)])


def case_new_outage_is_reported_again(t, clock):
    t.update(6, online=False)
    clock.t += 901
    t.overdue()
    t.update(6, online=True)
    t.update(6, online=False)
    clock.t += 901
    check(t.overdue(), [(6, 901)])


def case_devices_are_independent(t, clock):
    t.update(6, online=False)
    clock.t += 500
    t.update(8, online=False)
    clock.t += 401
    check(t.overdue(), [(6, 901)])
    clock.t += 500
    check(t.overdue(), [(8, 901)])


def case_removed_device_is_forgotten(t, clock):
    t.update(6, online=False)
    t.update(8, online=False)
    t.retain([8])
    clock.t += 901
    check(t.overdue(), [(8, 901)])


def run():
    cases = [(name, fn) for name, fn in globals().items() if name.startswith("case_")]
    failures = []
    for name, fn in cases:
        clock = Clock()
        try:
            fn(OfflineTracker(900, clock=clock), clock)
        except AssertionError as e:
            failures.append(f"{name}: {e!r}")
        else:
            print(f"  ok  {name}")
    for line in failures:
        print(f"FAIL {line}")
    print(f"{len(cases) - len(failures)}/{len(cases)} cases pass")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
