#!/usr/bin/env python3
"""Start the Matter -> MQTT bridge.

    python3 main.py <config.json>          (or: python3 -m matter2mqtt <config.json>)

See mqtt-bridge.json next to this file for the config keys and their defaults.

Everything lives in the matter2mqtt package; this is just the entry point, so the thing you
run is obvious from a directory listing.
"""
from matter2mqtt.bridge import main

if __name__ == "__main__":
    main()
