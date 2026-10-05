#!/usr/bin/env python3

"""Hard-reset the ESP32-C6 RCP over its USB-Serial-JTAG CDC port.

Use this to reset the ESP if it's detected down. This tends to happen (for example) when the host is suspended (which
shouldn't happen in a real deployment, as an hibernating host will have no thread leader, but is frequent during development)

Detection can be based on otbr-agent logs, which should look like

    [C] P-RadioSpinel-: Failed to communicate with RCP - no response from RCP
    [C] Platform------: HandleRcpTimeout() at radio_spinel.cpp:2013

The C6's USB-Serial-JTAG block is *in silicon* and keeps decoding the CDC
line-control signals even when the app is hung, wired like the classic auto-reset
circuit:

    RTS asserted -> EN low  -> chip held in reset
    DTR asserted -> IO9 low -> strapped into the ROM serial download bootloader

So a hard reset into the app = pulse RTS with DTR deasserted the whole time.
Needs no privileges beyond the tty itself, and does NOT re-enumerate USB, so the
container's --device node (fixed major:minor at `docker run`) stays valid.

Gotcha: a reset with pins in the wrong order may enter download mode, which behaves like a hung device. This script
will read the boot up banner to verify we entered the app running mode, and not the download mode.

Exit status:
    0 = reset, app banner seen
    1 = still in download mode / no banner
    2 = port unusable.
"""

import argparse
import sys
import time

try:
    import serial
except ImportError:
    sys.exit(
        "pyserial not installed. Host: apt install python3-serial. "
        "Container: add python3-serial (or pip install pyserial) to the image."
    )

# The ROM prints these at every boot, before any app output. "ESP-ROM:" alone is
# NOT a failure signal -- a healthy app boot prints it too. Only the download
# strings mean we strapped it wrong.
DOWNLOAD_MARKERS = (b"waiting for download", b"DOWNLOAD(USB/UART0)", b"DOWNLOAD_BOOT")
BOOT_MARKERS = (b"ESP-ROM:", b"boot:", b"rst:")


def open_port(port):
    """Open with both handshake lines deasserted from the very first moment.

    Assigning .dtr/.rts on a closed port only records the state; pySerial applies
    it inside open(). Setting them after open() would be too late -- the glitch
    has already happened.
    """
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = 115200  # ROM console rate; irrelevant to the reset itself
    ser.timeout = 0.1
    ser.dtr = False  # keep IO9 released: normal boot, never download mode
    ser.rts = False  # EN released
    ser.exclusive = True  # refuse to fight otbr-agent over the port
    ser.open()
    return ser


def pulse_reset(ser, hold):
    """Assert EN for `hold` seconds, then release with the strap still free."""
    ser.dtr = False  # re-assert the invariant; never let IO9 go low
    ser.rts = True  # EN low -> in reset
    time.sleep(hold)
    ser.rts = False  # EN released -> ROM boots the app
    ser.dtr = False


def read_banner(ser, window):
    """Collect whatever the chip prints for `window` seconds after reset.

    The RCP build has CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG=y, so ROM and
    app console output share this pipe with Spinel and we get a banner to judge.
    """
    deadline = time.monotonic() + window
    out = bytearray()
    while time.monotonic() < deadline:
        chunk = ser.read(256)
        if chunk:
            out += chunk
    return bytes(out)


def classify(banner):
    """-> ('download'|'booted'|'quiet', human readable reason)"""
    for marker in DOWNLOAD_MARKERS:
        if marker in banner:
            return "download", f"ROM download mode ({marker.decode()} seen)"
    for marker in BOOT_MARKERS:
        if marker in banner:
            return "booted", "ROM boot banner seen, app boot"
    return "quiet", f"no banner ({len(banner)} bytes read)"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-p", "--port", required=True, help="EG: /dev/ttyACM0")
    ap.add_argument("--hold", type=float, default=0.1, help="EN low, seconds (default %(default)s)")
    ap.add_argument("--window", type=float, default=1.5, help="banner read, seconds (default %(default)s)")
    ap.add_argument("--attempts", type=int, default=3, help="retries when it lands in download mode")
    args = ap.parse_args()

    try:
        ser = open_port(args.port)
    except serial.SerialException as err:
        sys.exit(
            f"cannot open {args.port}: {err}\n"
            "If it's busy, otbr-agent still holds it: s6-svc -d /run/service/otbr-agent"
        )

    with ser:
        for attempt in range(1, args.attempts + 1):
            print(f"[esp-reset] pulsing EN on {args.port} ({attempt}/{args.attempts})")
            ser.reset_input_buffer()
            pulse_reset(ser, args.hold)

            state, reason = classify(read_banner(ser, args.window))
            print(f"[esp-reset] {state}: {reason}")

            if state == "booted":
                return 0
            if state == "quiet":
                # Either the chip is genuinely mute (console disabled, or it's
                # already back to pure Spinel framing) or the USB-Serial-JTAG
                # never saw the pulse. Can't tell from here -- let otbr-agent
                # decide, it's the real test.
                print("[esp-reset] inconclusive; restart otbr-agent and watch its log")
                return 1
            # download mode: pulse again, the strap is released this time too
            time.sleep(0.2)

        print("[esp-reset] still in download mode after all attempts; replug needed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
