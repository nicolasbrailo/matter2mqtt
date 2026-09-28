"""Commissioning a new device onto the fabric.

The flow commisioning_test.py used to do by hand: check the trust store, hand matter-server
the live Thread dataset, then pair over BLE with the device's setup code. It takes tens of
seconds and fails in a dozen ways, so every stage is reported through `progress` -- the caller
decides where that goes (the bridge logs it and publishes it to mt2m/bridge/event).
"""
import time

from ..errors import RequestError
from .connection import PAA_ROOT_CERT_DIR, paa_cert_problem, thread_dataset

MANUAL_CODE_DIGITS = 11


def log(msg):
    print(f"[provision] {msg}", flush=True)


def parse_code(payload):
    """The setup code from a request payload, normalised."""
    code = payload.get("code")
    if isinstance(code, int):  # {"code": 12345678901} -- fine, but see the digit check below
        code = str(code)
    if not isinstance(code, str) or not code.strip():
        raise RequestError('expected {"code": "<pairing code>"}')
    return code.strip().replace("-", "").replace(" ", "")


class Provisioner:
    """One commissioning at a time, on the matter loop."""

    def __init__(self, matter, progress):
        """progress(step, message, **data) is called for each stage, including "failed"."""
        self.matter = matter
        self.progress = progress
        self.busy = False

    async def provision(self, payload):
        """Commission a device and return its MatterNodeData. Raises on any failure, after
        reporting a "failed" step."""
        code = parse_code(payload)
        client = self.matter.client
        if client is None:
            raise RequestError("matter-server not connected")
        if self.busy:
            raise RequestError("a provision is already running")

        started = time.monotonic()

        def step(name, msg, **data):
            log(f"+{time.monotonic() - started:5.1f}s [{name}] {msg}")
            self.progress(name, msg, code=code, **data)

        self.busy = True
        try:
            step("start", f"pairing code {code!r}")
            self.warn_about(code, client)

            # Cheap, and it fails ~40s earlier than the attestation step would
            step("check_certs", f"checking the PAA trust store in {PAA_ROOT_CERT_DIR}")
            if problem := paa_cert_problem():
                raise RuntimeError(problem)

            step("thread_dataset", "reading the active Thread dataset from otbr-agent (ot-ctl)")
            dataset = await thread_dataset()
            step("thread_dataset", f"got {len(dataset) // 2} bytes: {dataset[:16]}...{dataset[-8:]}")
            await client.set_thread_operational_dataset(dataset)
            step("thread_dataset", "pushed to matter-server")

            step("commissioning", "waiting for the device to advertise over BLE -- put it in "
                                  "pairing mode now; this can take a couple of minutes")
            node_data = await client.commission_with_code(code, network_only=False)
            step("commissioned", f"node_id={node_data.node_id} available={node_data.available}",
                 node_id=node_data.node_id)
            return node_data
        except Exception as e:
            log(f"FAILED after {time.monotonic() - started:.1f}s: {e!r}")
            step("failed", repr(e))
            raise
        finally:
            self.busy = False

    def warn_about(self, code, client):
        """Things that don't stop us trying, but explain the failure that's probably coming."""
        if code.isdigit() and len(code) != MANUAL_CODE_DIGITS:
            # A manual code is 11 digits; as a json number a leading zero is already gone
            log(f"WARNING: {len(code)} digits, expected {MANUAL_CODE_DIGITS} -- if the code "
                f'starts with 0, send it as a string: {{"code": "0{code}"}}')
        info = client.server_info
        if info is not None and not info.bluetooth_enabled:
            log("WARNING: matter-server reports BLE disabled -- start `make bluez-proxy` on the "
                "host and restart the container's matter-server, or this will only find devices "
                "already on the network")
