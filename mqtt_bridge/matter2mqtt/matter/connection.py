"""Long-lived matter-server connection, on its own asyncio loop in a background thread.

matter-server keeps a live cache of every node's attributes and streams updates while we
stay connected, so we connect once and keep the connection (reconnecting forever) instead
of connecting per request.
"""
import asyncio
import re
import threading
from pathlib import Path

import aiohttp
from matter_server.client import MatterClient

MATTER_URL = "ws://127.0.0.1:5580/ws"
RECONNECT_DELAY = 5
OT_CTL_DATASET = ("ot-ctl", "dataset", "active", "-x")
# Must match --paa-root-cert-dir in s6-overlay/s6-rc.d/matter-server/run
PAA_ROOT_CERT_DIR = Path("/src/connectedhomeip/credentials/production/paa-root-certs")


def log(msg):
    print(f"[matter] {msg}", flush=True)


class Matter:
    def __init__(self, on_connect, on_disconnect):
        """on_connect(client) runs on the matter loop after every (re)connect, once the node cache
        is populated; on_disconnect() when that connection drops (nothing is known any more)."""
        self.client = None
        self.loop = asyncio.new_event_loop()
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        threading.Thread(target=self.loop.run_until_complete, args=(self._run(),), daemon=True).start()

    async def _run(self):
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    client = MatterClient(MATTER_URL, session)
                    await client.connect()
                    init = asyncio.Event()
                    listen = asyncio.create_task(client.start_listening(init))
                    ready = asyncio.create_task(init.wait())
                    # start_listening may die before it ever gets the node dump
                    await asyncio.wait({listen, ready}, return_when=asyncio.FIRST_COMPLETED)
                    ready.cancel()
                    if init.is_set():
                        log(f"connected, {len(client.get_nodes())} node(s)")
                        info = client.server_info
                        if info is not None:
                            # bluetooth_enabled / thread_credentials_set decide whether
                            # commissioning can work at all, so say so up front
                            log(f"server: sdk={info.sdk_version} fabric={info.fabric_id} "
                                f"ble={info.bluetooth_enabled} "
                                f"thread_credentials={info.thread_credentials_set}")
                        self.client = client
                        try:
                            self._on_connect(client)
                        except Exception as e:
                            log(f"on_connect failed: {e!r}")
                    await listen  # returns (or raises) when the connection drops
                    log("disconnected")
            except Exception as e:
                log(f"connection to {MATTER_URL} failed: {e!r}")
            self.client = None
            try:
                self._on_disconnect()
            except Exception as e:
                log(f"on_disconnect failed: {e!r}")
            await asyncio.sleep(RECONNECT_DELAY)

    def submit(self, coro):
        """Run a coroutine on the matter loop from another thread; don't wait for it."""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        fut.add_done_callback(_log_failure)


def _log_failure(fut):
    if not fut.cancelled() and fut.exception() is not None:
        log(f"task failed: {fut.exception()!r}")


async def thread_dataset():
    """The active Thread dataset as hex, from otbr-agent via ot-ctl.

    ot-ctl is a thin D-Bus client, so this only works while otbr-agent is running with the mesh
    up. Its output is the hex blob on one line followed by "Done". matter-server needs this to
    push Thread credentials into a device at commission time.
    """
    proc = await asyncio.create_subprocess_exec(
        *OT_CTL_DATASET, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    text = out.decode(errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ot-ctl exited {proc.returncode}: {text.strip()!r}")
    for line in text.splitlines():
        line = line.strip()
        if re.fullmatch(r"[0-9a-fA-F]{20,}", line):
            return line
    raise RuntimeError(f"no dataset hex in ot-ctl output: {text.strip()!r}")


def paa_cert_problem():
    """Why device attestation is going to fail, or None if the trust store looks usable.

    Commissioning verifies the device's cert chain against the PAA roots in this directory, and
    the SDK's file trust store reads the .der files. matter-server treats the directory as its
    own cache: without a .version marker it deletes everything there and refetches from the DCL,
    which a --network none container can't reach. The result is an empty store and every
    commissioning dying in 'AttestationVerification', long after BLE and Thread worked -- so
    check it up front instead.
    """
    if not PAA_ROOT_CERT_DIR.is_dir():
        return f"{PAA_ROOT_CERT_DIR} does not exist: matter-server has no PAA root certificates"
    ders = sum(1 for _ in PAA_ROOT_CERT_DIR.glob("*.der"))
    if ders:
        return None
    marker = "" if (PAA_ROOT_CERT_DIR / ".version").exists() else " (and no .version marker)"
    return (f"no PAA root certificates (*.der) in {PAA_ROOT_CERT_DIR}{marker}: matter-server "
            f"wiped them and could not refetch from the DCL with --network none, so device "
            f"attestation will fail. Fix: `make rebuild` (the image ships the certs plus the "
            f".version marker that stops the wipe), then `make stop && make start`")
