"""The bridge's own record of each device's name, kept in a json file in the run dir.

A rename writes the name onto the device (Matter's NodeLabel), but some devices -- IKEA's
KAJPLATS bulbs, at least -- drop it on a power cycle and come back unnamed. This file is what
lets the bridge notice and put the name back. The device stays the source of truth while it
has a name: whatever usable name it reports, from us or another controller, is recorded here.

    {"6": "BatiOficinaWall", "8": "Kitchen"}      node_id -> name

Entries are never dropped, even for nodes matter-server no longer knows: a node_id is not
reused, and losing names because matter-server's data went missing would be worse.
"""
import json
import os


def log(msg):
    print(f"[names] {msg}", flush=True)


class NameStore:
    def __init__(self, path):
        self.path = path
        try:
            with open(path) as f:
                names = json.load(f)
        except FileNotFoundError:
            names = {}
        # A broken file stops the bridge rather than being treated as empty and overwritten
        if not isinstance(names, dict) or not all(isinstance(v, str) for v in names.values()):
            raise ValueError(f"{path}: expected a json object of node_id -> name")
        self.names = {int(k): v for k, v in names.items()}
        log(f"{len(self.names)} name(s) from {path}")

    def get(self, node_id):
        return self.names.get(node_id)

    def set(self, node_id, name):
        if self.names.get(node_id) == name:
            return
        log(f"node {node_id}: recording name {name!r}")
        self.names[node_id] = name
        # Write a temp file and swap it in, so a crash mid-write can't leave a truncated file
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as f:
            json.dump({str(k): v for k, v in sorted(self.names.items())}, f, indent=4)
            f.write("\n")
        os.replace(tmp, self.path)
