# Matter/Thread 2 MQTT — ESP32-C6 firmware

Expose one ESP32-C6 to a Linux host over a **single USB-Serial-JTAG link** as both
a **BLE controller** (Linux/BlueZ is the host) and a **Thread/Spinel RCP**. A mux
frames several logical channels onto the one link; the Linux driver
(`host_driver/host.c`) demuxes and presents real OS interfaces (an `hciN` via
`/dev/vhci`; a Spinel PTY, planned).

## Layout

- `main/mux.c` / `mux.h` — the multiplexer: USB link, framing, TX/RX, `ESP_LOG` capture.
- `main/app_bt.c` / `bt.h` — BLE controller + VHCI↔mux bridge; holds `app_main()`.
- `host_driver/host.c` — Linux driver: demux, `/dev/vhci` BT bridge, log printer
  (built with `host_driver/Makefile`).

## Build / run

Firmware:
```
source ~/.espressif/v6.0/esp-idf/export.sh
idf.py set-target esp32c6
idf.py build flash monitor
```
Host driver:
```
make -C host_driver
sudo host_driver/host /dev/ttyACM0   # native-USB CDC; root needed for /dev/vhci
```
The maintainer builds/flashes/tests on hardware — make code changes and hand off;
don't run build/flash yourself unless asked. IDF console/logs go to **UART0** (a
separate physical port), so they never pollute the mux on the USB link.

## Bring up / test the BT adapter

Flash the firmware, then on the Linux host:

```
sudo hciconfig list                       # See available devices
sudo hciconfig hci0 down                  # take the onboard/built-in adapter out of the way
                                          #   (or: rfkill block <onboard id>)
sudo host_driver/host /dev/ttyACM0        # note the "vhci: created hciN" line -> our index
sudo btmon                                # confirm the Espressif controller (CC:8D:A2:…) comes up
bluetoothctl                              # onboard is down -> ours is the only/default adapter (no select needed)
  power on
  scan on                                 # nearby BLE devices appear
```

Identify ours vs the onboard: the host driver prints the index; `hciconfig -a` shows
`Bus: Virtual` and the Espressif address for ours; in `btmon` ours is the
`= Open Index: CC:8D:A2:…` block.

To keep **both** adapters up instead of downing the onboard, target ours by index
(no MAC needed) — `btmgmt` is index-native:
```
sudo btmgmt --index hci1 power on
sudo btmgmt --index hci1 find -l          # LE scan
```
(or, in bluetoothctl: `select $(hciconfig hci1 | grep -oE '([0-9A-F]{2}:){5}[0-9A-F]{2}')`).

To pair a stress-test device, put a **BLE mouse** in pairing mode and pair it from
`bluetoothctl` (or the GNOME Bluetooth panel, which uses the only present adapter
once the onboard is down).

Sanity check it's really flowing through us: with a scan or the mouse active,
`Ctrl-C` the host driver → the adapter (`hciN`) tears down, the scan/mouse dies.
Audio (A2DP) won't work — the C6 is BLE-only.

## Design (the decisions that matter)

**Frame:** `[type u8][len u16 LE][payload][tail "/mt2mqtt\0" (9B)]`.

| type | name | dir | notes |
|---|---|---|---|
| 4 | SYNC | both | handshake — not implemented (deferred) |
| 5 | PING | host↔fw | liveness |
| 6 | LOG | fw→host | `ESP_LOG` lines, droppable |
| 7 | BT | both | opaque H4 HCI |
| 8 | SPINEL | both | opaque HDLC-Spinel — planned |

**Two TX policies, one serializer.** All USB writes go through `mux_write_locked()`
under a priority-inheriting mutex (frames never interleave):
- **Droppable / async** (logs, ping): `mux_tx()` → queue → low-prio `mux_tx_task`
  → bounded 100 ms write, drop on timeout. Bounded on purpose — avoids hoarding a
  since-boot backlog while no host drains (`cdc_acm` only drains once the port is
  opened, else a blocking write dumps seconds of stale history at connect time).
- **Reliable / sync** (BT, Spinel): `mux_tx_bt()` / `mux_tx_spinel()` write
  straight to USB under the mutex, blocking the (high-priority) caller as
  backpressure, up to a **1 s finite timeout**, then degrade to drop. Priority
  ordering means a flood of logs can't starve them.

**Reliability = backpressure, not buffering.** Each stage blocks its producer when
full, chaining back to the true source (Linux via USB CDC flow control for
host→fw; the BLE controller's own buffer pool for fw→host). **Invariant:** exactly
one timeout in the chain is finite — the reliable TX write. The host→fw paths block
with `portMAX` and rely on that one to break a wedge; don't make it infinite.

**RX (host→fw):** USB read loop → stream buffer (blocking send) → `mux_rx_task`
reassembles whole frames and dispatches per channel.

**Observability:** `mux.c` keeps per-channel frame counters (BT/Spinel × tx/rx) and a
low-priority `mux_stats_task` logs the per-channel counts every 5 s (deltas + running
totals) via `ESP_LOG` — so the line rides the mux LOG channel to the host as well as
UART0. tx counts successful reliable writes; rx counts dispatched frames.

**Host framing/resync (`host.c`):** raw mode + `tcflush` on open; `drain_on_open()`
swallows the post-open backlog; `process_frames()` resyncs by scanning for the tail
sync-word (never trusts a possibly-bogus `len`).

**BT bridge:** controller-only (no on-device host stack); HCI over VHCI
(`esp_vhci_host_*`), carrying opaque H4. host→ctrl is reassembled to whole packets
and paced by controller flow control; ctrl→host calls `mux_tx_bt()`. Host side:
`/dev/vhci` presents a real `hciN`. Frame cap `MUX_TX_MAX_PAYLOAD`=528 ≥ max H4 ACL
(`5 + CONFIG_BT_LE_ACL_BUF_SIZE` = 522), `_Static_assert`-guarded.

**Spinel bridge:** RCP-only; OT runs on the host. Device side (`spinel.c`) re-inits the
OT NCP onto a custom HDLC send callback that chunks into `mux_tx_spinel()`, and feeds
host bytes to `otNcpHdlcReceive()` under the OT lock (see S2 / the #1-unknown writeup
for why re-init). Unlike BT/VHCI there's no whole-packet-per-frame rule — Spinel is a
byte stream, so it chunks freely across mux frames and the host reassembles by HDLC
framing. Host side (`host.c`): a PTY presents `/dev/pts/N` for `ot-daemon`/`otbr-agent`.

## Current state

**BT works end-to-end, validated on hardware.** BlueZ sees the ESP as an `hciN`
via `/dev/vhci`, LE scan finds devices, and a **BLE HID mouse stayed connected and
stable for minutes** (killing the host driver kills the adapter — proves traffic
really flows through it). No dropped HCI. **The C6 is BLE-only** — no BR/EDR, so
Classic audio (A2DP) is out of scope; stress-test with BLE devices/throughput.

Two open items, both deferred:
- `bt_rx_from_controller` **blocks inside the VHCI `notify_host_recv` callback** to
  backpressure the controller. Fine so far, but untested at high ACL rate; if
  events go missing under load that assumption is wrong and we'd need HCI
  controller-to-host flow control. (Throughput / DLE / 2M-PHY tuning also untested.)
- **SYNC handshake** deferred: drain+resync already gives clean framing. Only edge
  cases would benefit (plugged-but-not-reading 1 s stalls; clean reconnect).

## Host integration: isolating matter-server, split at the D-Bus layer (← NEXT)

The `hciN` this firmware exposes (via `/dev/vhci`) registers in the host's
**initial network namespace**. That's a hard kernel constraint, not a config
choice: the Linux Bluetooth subsystem only allows `AF_BLUETOOTH` sockets in
`init_net` (`bt_sock_create()` → `-EAFNOSUPPORT` elsewhere) and `hci_vhci`
always registers in `init_net`. So a bridge-mode (isolated-netns) container
**cannot** open the adapter directly — `bluetoothd`/`hciconfig` inside it die
with `Address family not supported by protocol`. Cutting the stack at HCI (vhci,
H4-over-CDC) doesn't help: anything below GATT still needs a host BLE host-stack
(`bluetoothd`) holding `AF_BLUETOOTH` sockets, pinning it to host-net.

The fix is to **split at the D-Bus layer**, and it's now **verified** (spike,
2026-06-27): `python-matter-server`/chip's Linux BLE path talks to BlueZ
**purely over D-Bus** — it opens **zero** `AF_BLUETOOTH` sockets (confirmed by
`strace -e socket` during a commission; the adapter-0 scan ran while a bogus
`--bluetooth-adapter 123` failed instantly with "adapter unavailable", proving
the adapter is resolved through `org.bluez`, not a raw HCI open). So:

- `bluetoothd` runs on the **host** (`init_net`), owning this firmware's `hciN`.
- `matter-server` runs in its **isolated** container and reaches BlueZ only
  through a **bind-mounted system D-Bus socket** — a unix socket, *not*
  netns-bound. The Matter commissioner stays host-fabric-owning yet
  namespace-isolated; `wpan0`/avahi never leave the container.
- Proven end to end: a bridge-mode container (own netns, **no** BT `--device`,
  **no** `--network host`) drove a real 20 s BLE scan on the host `hci0` over the
  bind-mounted socket. See NOTES.md "Path A" for the full writeup.

**Next step — wire it up:** point matter-server at the host bus with
`DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/host_dbus/system_bus_socket`
(env in `matter-server/run`; mount in a dedicated Makefile target). Productionize
with **`xdg-dbus-proxy --filter --talk=org.bluez`** in front of the host bus so
the container can reach *only* `org.bluez` (not the whole host system bus) — that
also drops the `--security-opt apparmor=unconfined` the raw-socket debug path
needs. The `make start-bare` target is the throwaway debug version (raw bus +
unconfined); the real target uses the filtered proxy.

## Next: Spinel / OpenThread RCP over the mux

Goal: fold the standalone RCP (`~/src/mt2m/ot_rcp`, `esp_ot_rcp.c`) into this
firmware so the device does BLE **and** Thread over the one mux. Two facts drive it:
- The RCP today uses `HOST_CONNECTION_MODE_RCP_USB` — **the same USB-Serial-JTAG the
  mux owns** — so OT must hand its Spinel stream to the mux instead of owning the link.
- Coexistence is **off** in the RCP build; BLE + 802.15.4 on the one radio needs SW
  coexistence enabled — the biggest risk.

**Architecture** (mirrors BT; Spinel is just another reliable channel):
- **Device transport seam:** OT does its own HDLC framing, so the mux carries an
  opaque HDLC-Spinel byte stream. Hook via OT-native `otNcpHdlcInit(send_cb)` +
  `otNcpHdlcReceive()`: `send_cb` → chunked `mux_tx_spinel()`; `MUX_MSG_SPINEL` RX →
  `otNcpHdlcReceive()` **under the OT lock** (`esp_openthread_lock_acquire/release`
  — OT is single-threaded; don't call it raw from `mux_rx_task`).
- **Host side:** a **PTY** (not `/dev/vhci` — no kernel virtual-spinel device exists;
  OT host tools want a serial device). `host.c` pumps PTY master ↔ `MUX_MSG_SPINEL`;
  point `ot-daemon`/`otbr-agent` at `spinel+hdlc+uart:///dev/pts/N`.
- Reuses the reliable-channel infra. Spinel may **chunk across frames** (it's a
  stream — no whole-packet-per-frame constraint like BT/VHCI).

**#1 unknown — RESOLVED.** esp_openthread exposes only `RCP_UART/SPI/USB` host
modes, no "give bytes to my callback" seam. What actually happens: with
`CONFIG_OPENTHREAD_RADIO` + `CONFIG_OPENTHREAD_RCP_UART` (our build), `ot_task_worker`
**auto-inits an HDLC NCP** (`otAppNcpInit` → `otNcpHdlcInit(inst, NcpSend)`) wired to
`otPlatUartSend` → `write(s_uart_fd)`. With `host_connection_mode = NONE` that fd is
never opened (dead). Both `otAppNcpInit` and `otPlatUartSend` are non-weak and live
inside `libopenthread.a`, so we can't override either, and the RCP transport is a
forced Kconfig `choice` (can't select "none"). **The seam is the OT public API:** after
`esp_openthread_start()`, re-init the singleton NCP with our own send callback —
`otNcpHdlcInit(inst, our_send_cb)` under the OT lock. Re-init re-runs the `NcpBase`
ctor on the same `sNcpRaw` static; its callback registrations are single-pointer
overwrites or dedup'd (`otSetStateChangedCallback` is `IgnoreError`-wrapped), and the
lock stalls the OT task so nothing runs mid-swap. The auto-init NCP never sends/receives
real bytes before the swap (dead fd, no RX feed), so any boot reset frame is lost
harmlessly — the host re-syncs on connect. RX/TX use `otNcpHdlcReceive()` /
`otNcpHdlcSendDone()` (OT core, always compiled; only `otNcpHdlcInit` is vendor-hooked).

**Phases:**
- **S0 — build/config merge: DONE & VERIFIED.** `main/CMakeLists.txt` (explicit
  `SRCS`, requires `openthread esp_event vfs`); `sdkconfig.defaults` enables
  `OPENTHREAD_ENABLED/RADIO`, `IEEE802154_ENABLED`, SW coexistence (already on),
  `FREERTOS_HZ` 100→1000, trims OT features, `OPENTHREAD_LOG_LEVEL_DYNAMIC=n`
  (see gotchas); custom `partitions.csv` gives the app the full 2 MB. Builds & fits.
- **S1 — OT alongside, transport NONE: DONE & VERIFIED.** `main/spinel.c/.h` +
  `main/esp_ot_config.h` (native radio, `HOST_CONNECTION_MODE_NONE`); `spinel_init()`
  registers the eventfd VFS + default event loop and calls `esp_openthread_start()`.
  `app_main` = `mux_init(); bt_init(); spinel_init(); mux_run();`. Confirmed on
  hardware: boots, `SPINEL: OpenThread RCP started` in the muxed logs, **BT mouse
  still works** with the radio + coexistence active. Radio is up but idle (NONE =
  nothing drives Spinel yet).
- **S2 — wire Spinel to mux: CODE COMPLETE, awaiting hardware verification.** In
  `spinel.c`: `spinel_init()` re-inits the NCP after `esp_openthread_start()` with our
  `spinel_ncp_send` callback (see #1 unknown above). `spinel_ncp_send` chunks the
  HDLC buffer into `MUX_TX_MAX_PAYLOAD`-sized `mux_tx_spinel()` frames (Spinel is a
  byte stream — split freely; host reassembles by HDLC framing), always returns the
  full `len` (the encoder asserts it; drop on no-host rather than crash), then calls
  `otNcpHdlcSendDone()` — mirroring `otPlatUartSend`'s synchronous send-then-done.
  `mux.c` `MUX_MSG_SPINEL` case → `spinel_rx_from_host()` → `otNcpHdlcReceive()`
  **under the OT lock** (`esp_openthread_lock_acquire(portMAX)` — host→fw blocks per
  the backpressure invariant). No new `.c` (extends existing `spinel.c`). **To verify:**
  flash, confirm `SPINEL: OpenThread RCP started (Spinel bridged to mux)` + BT mouse
  still works; full Spinel exchange needs the S3 host PTY to actually drive it.
- **S3 — host PTY bridge: CODE COMPLETE, awaiting hardware verification.**
  `host_driver/host.c` opens a PTY (`pty_open()`: `posix_openpt`/`grantpt`/`unlockpt`,
  raw termios so HDLC bytes pass untouched) and pumps it ↔ `MUX_MSG_SPINEL`: PTY
  master is a third poll fd → host→fw `MUX_MSG_SPINEL` (chunked to `SPINEL_CHUNK_MAX`
  512 ≤ fw cap 528, since Spinel is a stream); fw→host `MUX_MSG_SPINEL` → `write()` to
  the master, inside `process_frames()`. On open it prints
  `spinel+hdlc+uart:///dev/pts/N` to point `ot-daemon`/`otbr-agent` at. A held-open
  slave fd (`g_pty_slave_fd`) keeps the master from reporting `POLLHUP` (busy-spin)
  before an OT tool attaches. Bridge is optional like vhci: if `pty_open()` fails,
  SPINEL frames are hex-dumped and the rest still works.
- **S4 — bring-up: ← NEXT.** `ot-ctl`/`ot-daemon` form/join a network, ping a Thread node.
- **S5 — coex stress:** BT (mouse/scan) and Thread traffic at once; both stable.

## Gotchas

- Logs flow over the mux only **after** `mux_init()` installs the `ESP_LOG` hook;
  `app_main()` order is `mux_init(); bt_init(); [spinel_init();] mux_run();`.
  Pre-hook / bootloader logs go to UART0 only.
- The `ESP_LOG` hook, `mux_write_locked`, and the reliable senders must **never log
  on their own error paths** — reachable from the hook / under `g_tx_mutex` → recursion.
- **Head-of-line:** RX is one stream buffer + one `mux_rx_task`, so a backpressured
  reliable flow stalls other channels' RX too. Per-channel RX queues would fix it.
- `mux_rx_task` resync is still crude (flush-on-oversize); port the host's tail-scan
  resync if it matters.

### Build gotchas (ESP-IDF)
- **`sdkconfig.defaults` only seeds a *new* `sdkconfig`.** Editing it does nothing
  to an existing `sdkconfig` (and `idf.py reconfigure` won't re-apply it either).
  After changing defaults: `rm sdkconfig && idf.py set-target esp32c6 && idf.py build`
  (set-target also wipes the build dir, so library configs like OpenThread's actually
  rebuild). Or flip the option in `idf.py menuconfig`.
- **List `SRCS` explicitly in `main/CMakeLists.txt`** — IDF evaluates it in CMake
  script mode (requirement expansion), where `file(GLOB ... CONFIGURE_DEPENDS)` is
  illegal, and a plain `GLOB` silently misses files added since the last reconfigure
  (→ `undefined reference`). Add each new `.c` to the `SRCS` line.
- **`CONFIG_OPENTHREAD_LOG_LEVEL_DYNAMIC=n`** is required: with it on,
  `esp_openthread.cpp` calls `otLoggingSetLevel()`, which the OT lib here doesn't
  provide → link error. Static level still routes OT logs to `ESP_LOG`/the mux.
