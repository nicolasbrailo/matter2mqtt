# Matter/Thread → MQTT bridge — setup notes

Working notes for building a custom Matter/Thread → MQTT bridge that sits next
to an existing Zigbee2MQTT-based home automation system. Hardware: ESP32-C6
acting as a USB-attached Thread radio; Matter/Thread button as the first device
to commission.

## Goal

- ESP32-C6 acts as a **USB-attached Thread radio** for a Linux host.
- The host runs the Thread border router stack and a Matter controller.
- A custom bridge maps Matter operations to MQTT (parallel to how Z2M maps
  Zigbee to MQTT).
- **No bridging between LAN and the Thread network.** The only crossing point
  is the MQTT bridge. No IPv6 forwarding, no mDNS bridging onto Wi-Fi, no
  NAT64.

## Why

I quite like Zigbee's architecture: stupid simple. I run my own zigbee controller, so I know exactly what it's doing. I know it can't phone the mothership, and I know when a device is working (or not). Matter+Thread are too magical: I don't want an IoT device to interface with my LAN. At all. I prefer the security domains of my IoT and LAN boundaries to be well defined, in different vlans or ideally even in different radios (that's to reduce interference though, not security).

When I setup Zigbee, I made the "mistake" of relying on 3rd party vendor dongles without understanding well enough how they work. Much better than relying on a 3rd party off-the-shelf solution (*), but if my dongle dies, I'll have to invest a lot of effort in finding a new dongle that works.

My Zigbee devices have had a very good run, some going strong after 10 years. Now that a few of them are starting to die, I decided to jump to Matter/Thread (but keep both networks, with a layer of interop). This time I'm going a bit deeper in the stack, and building my own Matter/Thread dongle, based on ESP32. Yes, I fully expect to build the next IoT network based on software-defined-radio.

(*) Nitpick: Much better in terms of control, a lot worse in terms of time investment and possibly money

## Architectural decision: which ESP-IDF example to start from

There are two relevant examples in the ESP-IDF tree:

| | `ot_br` (Border Router) | `ot_rcp` (Radio Co-Processor) |
|---|---|---|
| OpenThread stack | runs on the ESP32 | runs on the host PC |
| Thread ↔ IP routing | on the ESP32 | on the host PC |
| Why Wi-Fi exists | it's the IP-side uplink | not used at all |
| USB carries | console / flashing only | the entire radio (Spinel protocol) |

`ot_br` is designed to be the *complete* border router on the chip, with Wi-Fi
as its uplink to the LAN. Stripping Wi-Fi out of `ot_br` would mean
reimplementing the IP uplink over USB (CDC-NCM/RNDIS) — basically
reinventing what RCP gives for free.

`ot_rcp` is the natural fit: the C6 is a dumb radio over USB, the host runs the
real stack. This is the same architecture as Nordic nRF52840 dongle + RPi.

**Decision: use `ot_rcp` unchanged. Do not modify `ot_br`.**

## Step 1 — Flash the RCP firmware

The stock `ot_rcp` may or may not have USB Serial/JTAG enabled in
`sdkconfig.defaults` (verify that `CONFIG_OPENTHREAD_RCP_USB_SERIAL_JTAG=y`). Enable if not, then `rm sdkconfig` to regen a new config the next run.

```
cd ot_rcp
idf.py -p /dev/ttyACM0 build flash monitor
```

### Expected behavior: only boot logs, then silence

This is **correct**. After boot the USB CDC endpoint stops being a log pipe
and becomes a binary Spinel protocol pipe. The C6 has one USB Serial/JTAG
endpoint and the RCP firmware reserves it for Spinel framing. Anything sent
afterward looks like garbage in `idf.py monitor`.

Also note these defaults in `ot_rcp/sdkconfig.defaults`:
- `CONFIG_OPENTHREAD_LOG_LEVEL_NONE=y` — OpenThread logs are compiled out for
  firmware size. They're not just silenced, they're gone.
- `CONFIG_OPENTHREAD_RCP_SPINEL_CONSOLE=y` — any logs that *do* get emitted
  are encapsulated inside Spinel frames; only a Spinel-aware host decodes them.

To get real OpenThread logs you'd need to rebuild with
`CONFIG_OPENTHREAD_LOG_LEVEL_INFO=y` (or `_DEBG`), and read them via a Spinel
host stack (e.g. `otbr-agent -v -d 7`). Don't bother yet — by the time
anything interesting is happening you'll be running `otbr-agent`, which logs
naturally.

### Host gotcha: disable ModemManager

The ESP32-C6's USB Serial/JTAG looks like a USB CDC-ACM device, which
Linux's `ModemManager` will eagerly probe with AT commands to see if it's
a cellular modem. Those probes corrupt the Spinel handshake and otbr-agent
fails at startup with:

```
otbr-agent[…]: [C] Platform------: Init() at spinel_driver.cpp:87: Failure
```

(Or pyspinel hangs, or `idf.py flash` randomly fails, depending on
timing.) Permanently disable ModemManager — it's not useful on a
non-cellular dev box:

```
sudo systemctl stop ModemManager
sudo systemctl mask ModemManager
```

Then re-plug the dongle (or `sudo udevadm trigger`). One-time fix per
host.

### Host gotcha: use a stable device path

`/dev/ttyACM0` shifts to `ttyACM1` etc. if other USB serial devices appear
or the dongle is re-plugged. For anything long-lived (otbr-agent unit
files, scripts) prefer the by-id path:

```
ls -l /dev/serial/by-id/
# e.g. /dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_…-if00
```

## Step 2 — Verify the dongle works from the host with pyspinel

`pyspinel` is a low-level Spinel client. Useful **only** to confirm the radio
responds. It cannot bring up a Thread network and cannot commission Matter
devices (see "Why pyspinel can't pair a button" below).

```
pip install pyspinel
spinel-cli.py -u /dev/ttyACM0 -b 460800
> version          # returns OPENTHREAD/...   → layers 1+2 OK
> ifconfig up      # returns "Error"           → expected, see below
```

### Why `ifconfig up` returns "Error"

Spinel has two historical roles:
- **NCP** (Network Co-Processor): Thread stack on device. `ifconfig up`,
  `thread start` etc. work because the stack exists on the chip.
- **RCP** (Radio Co-Processor): Thread stack on host. The chip is only a
  radio; commands like `ifconfig up` target a stack that isn't there.

`ot_rcp` is firmly RCP (`CONFIG_OPENTHREAD_RADIO=y`, BR/CLI/commissioner all
disabled). `spinel-cli.py`'s higher-level commands are NCP-era leftovers; they
hit a "property not found" path on the RCP and surface as just "Error." It's
not a misconfiguration, it's the wrong layer for that tool.

`version` working is sufficient proof — move on.

### Why pyspinel can't pair a Matter button

Matter commissioning requires four stacked layers all present:

```
4. Matter commissioner          chip-tool / python-matter-server (host)
                                also needs host BLE for initial pairing
3. Thread stack + IP routing    otbr-agent on the host → wpan0 netif
2. Spinel protocol              pyspinel reaches this layer
1. 802.15.4 radio               the ESP32-C6
```

pyspinel sits at layer 2 looking down. Pairing happens at layer 4. Also:
**Matter commissioning starts over BLE**, not Thread — the commissioner uses
the host's Bluetooth radio to find the unprovisioned device, authenticates
with the QR/setup code, then hands the device Thread credentials so it can
join via the OTBR.

## Step 3 — Install `otbr-agent` but skip the setup script

`ot-br-posix`'s `script/setup` configures the host as a *consumer-grade*
border router (the Apple TV / Google Nest / Home Assistant pattern). It
assumes you want Thread devices to be full citizens of the LAN: IPv6
forwarding both ways, RAs on Wi-Fi, mDNS bridging across the boundary,
NAT64 for IPv4 internet.

For an isolated Thread island whose only outside link is the MQTT bridge,
**none of that applies**. What the daemon itself needs is tiny:

1. Open `/dev/ttyACM0`, run the Thread stack against the RCP.
2. Create `wpan0` (TUN/TAP) so the host's IPv6 stack has a handle on Thread.
3. Listen on its own REST/D-Bus socket for `ot-ctl`.

That's it — none of that touches the LAN.

### What the install script adds, and whether you need it

| Change | Purpose | Needed here? |
|---|---|---|
| `net.ipv6.conf.all.forwarding=1` | LAN ↔ wpan0 packet forwarding | No |
| Router Advertisements on Wi-Fi/eth | LAN devices learn Thread prefix | No |
| mDNS bridging (Avahi reconfig) | `_matter._udp` visible across boundary | **See nuance** |
| NAT64 (tayga / openthread-nat64) | Thread-only → IPv4 internet | No |
| systemd-networkd/NM exclusion for `wpan0` | OS doesn't fight over netif | Maybe, harmless |
| Firewall rules | scope inbound traffic to Thread | No |
| `otbr-web` (port 80 admin UI) | optional UI | No |

### The mDNS nuance

Matter operational discovery uses mDNS to find already-commissioned devices.
OTBR's mDNS bridging copies these announcements between Thread and Wi-Fi.
You don't want them on Wi-Fi, but you **do** want the Matter controller on
the same host to see them. Two options:

- **Bind mDNS publisher to loopback only.** Controller (also on host) sees
  Thread services via local Avahi; nothing leaks. Cleanest path.
- **Skip mDNS bridging entirely** and have the controller address devices
  by IPv6 directly. Workable for small static fleets, fragile when devices
  rotate.

### Recommended minimal build of `otbr-agent`

Don't run `script/setup`. Build the daemon only with isolation-friendly
flags. From the `ot-br-posix` checkout:

```
./script/bootstrap        # installs build deps only
mkdir build && cd build
cmake -GNinja \
  -DOTBR_INFRA_IF_NAME=lo \
  -DOTBR_BORDER_ROUTING=ON \
  -DOTBR_BACKBONE_ROUTER=OFF \
  -DOTBR_NAT64=OFF \
  -DOTBR_WEB=OFF \
  -DOTBR_REST=ON \
  -DOTBR_DBUS=ON \
  -DOTBR_VENDOR_NAME="NicoMT2M" \
  -DOTBR_PRODUCT_NAME="NicoMT2M-otbr" \
  ..
ninja
```

`OTBR_VENDOR_NAME` and `OTBR_PRODUCT_NAME` are required — otbr-agent
refuses to start with "Vendor name must be set" otherwise. The strings
populate the `_meshcop._udp` mDNS TXT record and Border Agent state; the
content doesn't matter as long as they're non-empty. Optional siblings
(`OTBR_VENDOR_OUI`, `OTBR_VENDOR_SW_VERSION`) can stay at defaults until
a later error complains.

### Finding `ot-ctl`

`ot-ctl` is the CLI client used to talk to `otbr-agent`. ot-br-posix doesn't
ship its own — it reuses the upstream OpenThread one via the submodule. It
lives in the build tree at:

```
build/third_party/openthread/repo/src/posix/ot-ctl
```

(That's the binary, not a directory.) Install it on PATH for convenience:

```
sudo install -m 755 \
  build/third_party/openthread/repo/src/posix/ot-ctl \
  /usr/local/bin/ot-ctl
```

Use it only while `otbr-agent` is running — it's a thin D-Bus client, not
a self-contained stack. (`ot-cli` is the self-contained one, which would
conflict with the daemon for RCP ownership.)

First bring-up:

```
sudo ot-ctl state                       # "disabled"
sudo ot-ctl dataset init new
sudo ot-ctl dataset commit active
sudo ot-ctl ifconfig up
sudo ot-ctl thread start
sudo ot-ctl state                       # eventually "leader"
sudo ot-ctl dataset active -x           # SAVE THIS — needed for commissioner
```

0e080000000000010000000300001a4a0300000c35060004001fffe00208ce1b1e6984e805000708fd6d3415bd5ced9e0510294a58aa6b7baabd2149a4faa6f8db55030f4f70656e5468726561642d31366465010216de0410e175d202043a12a875d6045e814ccdb10c0402a0f7f8

The hex dataset is what the Matter commissioner will feed to the button so
it can join. Lose the dataset and any commissioned devices can't rejoin.

### D-Bus policy

`script/setup` would have installed otbr-agent's D-Bus policy file; since
we skipped it, the system bus refuses to let the daemon own
`io.openthread.BorderRouter.wpan0` and you'll see:

```
Failed to request DBus name: org.freedesktop.DBus.Error.AccessDenied:
Connection ":1.X" is not allowed to own the service ...
```

The policy file is in the build tree (rendered from `.conf.in`):

```
find . -name 'otbr-agent.conf'
sudo install -m 644 src/agent/otbr-agent.conf /etc/dbus-1/system.d/otbr-agent.conf
sudo systemctl reload dbus
```

Only needs to be done once per host.

`OTBR_INFRA_IF_NAME=lo` is the key isolation trick: it tells the BR
"upstream LAN is loopback," so code paths that would bridge have nothing
real to bridge to. RAs go to loopback, route programming is a no-op,
the Discovery Proxy has no infra mDNS to query against.

**Do not set `OTBR_BORDER_ROUTING=OFF`.** Despite the name, that subsystem
does more than LAN bridging — it also manages the OMR (Off-Mesh Routable)
prefix that gives Thread devices their routable global IPv6 addresses. With
it off, Matter comms inside the mesh can break because devices may only
have link-local addresses. It also triggers a build failure:
`OPENTHREAD_CONFIG_DNSSD_DISCOVERY_PROXY_ENABLE requires
OPENTHREAD_CONFIG_BORDER_ROUTING_ENABLE` — the two are co-dependent in
the OpenThread config. Leave it on and rely on `INFRA_IF_NAME=lo` for
isolation.

The Discovery Proxy stays on (default). It's harmless when infra is `lo`
and isn't required by Matter (Matter operational discovery uses SRP on the
Thread side). If you want it explicitly off:
`-DOTBR_DNSSD_DISCOVERY_PROXY=OFF`.

Some missing deps I found in my system, even after running the bootstrap script:

sudo apt-get install libgtest-dev libgmock-dev ninja-build


### First run (no systemd yet)

```
sudo ./src/agent/otbr-agent -I wpan0 -B lo -v -d 7 \
  'spinel+hdlc+uart:///dev/ttyACM0?uart-baudrate=460800'
```

In another shell:
```
sudo ot-ctl state                                 # daemon responds
sudo ot-ctl dataset init new
sudo ot-ctl dataset commit active
sudo ot-ctl ifconfig up
sudo ot-ctl thread start
# wait, then:
sudo ot-ctl state                                 # eventually says "leader"
```

### Verify isolation

- `ip -6 route show` — should show a route via `wpan0` but **no** Thread ULA
  prefix announced on the LAN-facing interface.
- `ss -tunlp | grep otbr` — listeners should be on `lo` only.

## Step 4 — Commission a Matter device with chip-tool

### Why commission from the host, not the phone

It's tempting to use a phone app since BLE is easier on a phone. Don't, for
this project. Three reasons:

1. **Phone apps commission onto their own Thread network.** Apple Home,
   Google Home, etc. hand the device *their* ecosystem's Thread dataset —
   they have no UI to paste in your custom OTBR's hex dataset. The button
   would join Apple's/Google's Thread network and your OTBR would never see
   it. Developer-focused apps (CHIPTool for Android, Nordic's Matter tools)
   *can* accept a custom dataset, but then you're in case (2).

2. **The commissioner becomes a fabric admin — cryptographically and
   permanently.** "Admin" in Matter isn't about who keeps talking to the
   device; it's about whose **Root CA** is installed on the device's trust
   store during commissioning. After PASE, the commissioner installs:
   - Its own fabric Root CA cert (trust anchor)
   - A NOC (per-device cert) signed by that Root CA

   BLE then drops, but the device only authenticates entities holding a
   valid NOC signed by that Root CA. The Root CA private key never leaves
   the commissioner. So if a phone commissions, *the phone is forever a
   privileged party for that device*, and the host has no way to
   authenticate without going through multi-admin (an additional BLE PASE
   round to install a second independent Root CA on the device). The host
   can route IPv6 packets to the button on Thread but CASE rejects it:
   "your cert isn't from a CA I trust."

3. **You need a host Matter controller anyway** for the MQTT bridge. So
   commission from there directly and skip the phone hop.

The phone is still useful for one thing: **scanning the device QR code** to
read the setup payload. That's a one-shot info transfer, not a control
relationship.

### Reading the device QR code

Any QR reader works. The raw payload is a string starting with `MT:` (e.g.
`MT:EX7A042C00NC0V2G710`). Inside it encodes vendor/product IDs,
discovery capabilities, the **discriminator** (12-bit, identifies the
device on BLE), and the **passcode** (8-digit setup PIN).

Decode the payload locally without touching BLE or commissioning:

```
chip-tool payload parse-setup-payload 'MT:EX7A042C00NC0V2G710'
chip-tool payload parse-manual-code 12345678901    # for the 11-digit form
```

Example output for the Aqara button:
```
VendorID: 4476    (0x117C — Aqara)
ProductID: 32769
Long discriminator: 1336
Passcode: 56569036
```

Save the discriminator and passcode — those are what `chip-tool pairing
ble-thread` needs as positional args.

### chip-tool install (snap is fine for now)

`sudo snap install chip-tool` is the path of least resistance. It's a
slightly older build with a few quirks (see below) but works for
commissioning. Long-term, if the MQTT bridge ends up needing chip-tool's
Python bindings or a custom controller, switch to building from
`connectedhomeip` source.

Storage lives in `~/snap/chip-tool/common/chip_tool_kvs` — fabric state
persists across runs.

### Running the pairing command

The QR-payload variant (`pairing code-thread`) has a known quirk in some
snap builds where it routes the `MT:...` string to the manual code parser
and fails with `ManualSetupPayloadParser.cpp:46 Integrity check failed`,
even though `payload parse-setup-payload` accepts the same string. Use
`pairing ble-thread` with explicit args instead — it sidesteps payload
parsing entirely:

```
sudo chip-tool pairing ble-thread \
  <node-id>                  \   # you choose, e.g. 100 — unique in YOUR fabric
  hex:<dataset>              \   # the hex from `ot-ctl dataset active -x`
  <passcode>                 \   # 8-digit, from parse-setup-payload
  <discriminator>            \   # 12-bit, from parse-setup-payload
  --bypass-attestation-verifier true
```

Concrete example:
```
sudo chip-tool pairing ble-thread 100 \
  hex:0e0800...your-dataset...f8 \
  56569036 1336 \
  --bypass-attestation-verifier true
```

The button must be in commissioning mode (BLE advertising with UUID
0xFFF6) when you run this. Long-press / button combo varies per device.

Expected flow (60–90s on success):
BLE scan → connect → PASE → fabric setup → cert exchange → attestation →
dataset transfer → BLE drop → device joins Thread → CASE over Thread →
`Device commissioning completed with success`.

After success: `sudo ot-ctl neighbor table` shows the device on the mesh.

### Why `--bypass-attestation-verifier`

Matter's device attestation chain is:
```
PAA  (Product Attestation Authority)   ← root, held by CSA/vendor;
                                          must be in commissioner's trust store
 └─ PAI  (intermediate)                ← sent by device
     └─ DAC  (device cert)             ← sent by device
```

The commissioner needs the **PAA root** to walk the chain to a trusted
anchor. snap's chip-tool ships with an empty (or dev-only) PAA trust store
and fails with:

```
Unable to find PAA, err: CA certificate not found,
PAI's AKID: 6B:31:8C:FC:...
Failed Device Attestation
```

Two ways out:

- **Install the production PAA store** (proper fix). Sparse-clone the
  certs:
  ```
  git clone --depth 1 --filter=blob:none --sparse \
    https://github.com/project-chip/connectedhomeip ~/src/mt2m/credentials
  cd ~/src/mt2m/credentials
  git sparse-checkout set credentials/production/paa-root-certs
  ```
  Then add `--paa-trust-store-path
  ~/src/mt2m/credentials/credentials/production/paa-root-certs` to every
  pairing invocation.

- **Bypass verification** with `--bypass-attestation-verifier true`. Skips
  the chain walk entirely.

For this project, **bypass is the right trade-off**: the threat model is
"a human bought a device and plugged it in," not "an untrusted device is
trying to join a multi-tenant fabric." Device attestation defends against
the latter, which doesn't apply here. The trust anchor for "is this the
right device" is the physical act of buying and pairing it.

If you ever expose this fabric to less trusted environments, switch to the
PAA store approach.

### After commissioning

The device is in your fabric. `chip-tool` can now talk to it by node-id:

```
sudo chip-tool descriptor read parts-list 100 0          # list endpoints
sudo chip-tool onoff read on-off 100 1                   # read on-off attr
sudo chip-tool onoff toggle 100 1                        # toggle (for on/off devices)
```

For a button specifically, you'll want to subscribe to events / attribute
changes — that's the wire-up the MQTT bridge will eventually drive.

## Step 5 — Current state: device pairs to Thread, not yet to Matter

After running the commissioning command, the device joins the Thread mesh
but the Matter commissioning rolls back at the final step. Commissioning is
not yet working end-to-end — we know exactly why, and Step 6 is the fix.

### What works

The button **does join the Thread network** as a child of the OTBR. BLE
PASE, cert exchange, attestation (bypassed), and Thread dataset transfer
all succeed. Verify with:

```
sudo ot-ctl child table       # button appears with its MAC, RLOC16, RSSI
sudo ot-ctl neighbor table    # same device shown as a Child (Role = C)
```

The button also (briefly) registers its operational Matter service in the
OTBR's SRP server:

```
sudo ot-ctl srp server service
# you'll see e.g.:
#   90A107364EC59C3D-0000000000000064._matter._tcp.default.service.arpa.
#   (compressed-fabric-id - node-id)
```

### What doesn't work, and why

After the device joins Thread, chip-tool needs to find its operational
mDNS advertisement (`_matter._tcp`) to start CASE — the final mTLS-like
handshake that completes commissioning. **That mDNS lookup times out**:

```
[DIS] Lookup started for 90A107364EC59C3D-0000000000000064
[DIS] Timeout waiting for mDNS resolution.
[CTL] Session establishment failed ... CHIP Error 0x00000032: Timeout
[CTL] Cancelling CASE setup for step 'FindOperationalForStayActive'
Run command failure: ... CHIP Error 0x00000032: Timeout
```

When CASE never completes, the fail-safe timer expires and the **device
rolls back the entire commissioning** — deletes its NOC, deletes the SRP
registration, re-enters commissioning mode. That's why ot-ctl shows the
operational entry but with `deleted: true`:

```
90A107364EC59C3D-0000000000000064._matter._tcp.…    deleted: true
```

The root cause is the **mDNS publish path**. We chose
`OTBR_INFRA_IF_NAME=lo` to keep otbr-agent's mDNS announcements off the
LAN. But otbr-agent then publishes only on the loopback interface, and
neither Avahi (the host-wide mDNS resolver) nor chip-tool's internal
resolver listens to loopback for mDNS by convention. The publisher and the
consumer are on the same host, but on different mDNS link domains.

Confirm with avahi-browse — empty result is the diagnostic:

```
avahi-browse -r -t _matter._tcp     # expect: nothing (the bug)
avahi-browse -r -t _matterc._udp    # also nothing
```

So the SRP server holds the service, but no one outside otbr-agent's own
process tree sees it. Hence the failure chain:

```
device joins Thread                       ✓
device registers _matter._tcp via SRP     ✓  (ot-ctl saw it)
otbr-agent republishes to mDNS on lo      ✓  (its job, did happen)
Avahi picks up the announcement           ✗  (browse empty)
chip-tool resolver gets an answer         ✗  (timeout)
failsafe expires → device deletes SRP     ← what you see in the aftermath
```

## Step 6 — Fix mDNS by switching infra to a dummy interface (DONE)

Replaced `OTBR_INFRA_IF_NAME=lo` with a Linux **dummy interface**
(`ot-infra`). A dummy interface looks like a real netif to otbr-agent's
mDNS publisher (so it behaves the way Avahi and chip-tool expect), but
has no external connectivity — preserving the no-LAN-bridging invariant
from Step 3.

### What we did

1. **Create the dummy interface:**
   ```
   sudo ip link add ot-infra type dummy
   sudo ip link set ot-infra up
   sudo ip -6 addr add fe80::1/64 dev ot-infra
   ```
   (Add to a systemd-networkd or /etc/network/interfaces stanza later for
   persistence — for now, ad-hoc is fine.)

2. **Reconfigure and rebuild otbr-agent:**
   ```
   cd ~/src/mt2m/host/ot-br-posix/build
   cmake -DOTBR_INFRA_IF_NAME=ot-infra .
   ninja
   ```

3. **Restart otbr-agent with the new infra arg:**
   ```
   sudo ./src/agent/otbr-agent -I wpan0 -B ot-infra -v -d 7 \
     'spinel+hdlc+uart:///dev/ttyACM0?uart-baudrate=460800'
   ```
   Bring the Thread network back up after restart (dataset persists, so
   it's quick):
   ```
   sudo ot-ctl ifconfig up
   sudo ot-ctl thread start
   sudo ot-ctl srp server enable
   ```

4. **Verify mDNS publication is now visible:**
   ```
   sudo ot-ctl child table             # button rejoins as a child
   avahi-browse -r -t _matterc._udp    # expect: device shows up now
   ```

5. **Recommission over Thread (no BLE this time):** the button is already
   on the mesh and re-advertising commissionable, so use the on-network
   variant:
   ```
   sudo chip-tool pairing onnetwork-long 100 56569036 1336 \
     --bypass-attestation-verifier true
   ```

6. **Confirm success:**
   ```
   sudo ot-ctl srp server service      # should now show _matter._tcp
                                       # WITHOUT "deleted: true"
   sudo chip-tool descriptor read parts-list 100 0
   ```

### Verified working

End-to-end test against the Aqara button:

- mDNS lookup resolves in ~1ms, with the address tagged
  `%ot-infra` confirming the route through the dummy interface:
  ```
  UDP:[fdd8:8db0:6ae3:1:a81a:a479:6824:3405%ot-infra]:5540
  ```
- CASE Sigma1/Sigma2/Sigma3 complete, secure session activated.
- `chip-tool descriptor read parts-list 100 0` returns the device's
  endpoint topology:
  ```
  PartsList: 2 entries
    [1]: 1
    [2]: 2
  ```
  i.e. the button exposes endpoints 1 and 2 (plus root endpoint 0).

If a subsequent CASE attempt returns `CHIP Error 0x000000DB: The Resource
is busy` that's normal for sleepy/ICD devices (the Aqara button is one):
press the button to wake it and retry within a second, or use a
subscription instead of one-shot reads. Not a regression of Step 6.

## Step 7 — TODO: operational resilience (USB wedge recovery)

Observed problem: every so often otbr-agent fails to start with

```
[C] Platform------: Init() at spinel_driver.cpp:87: Failure
```

and the only fix is physically unplugging and replugging the C6. For a
24/7 home automation service that's unacceptable — needs both diagnosis
(why does the C6 / kernel get into this state?) and recovery (how to
self-heal without touching the cable).

This is not ModemManager (already masked in Step 1) — that one's fixed.
Something else is wedging the USB-CDC pipe occasionally.

### Diagnosis to capture next time

Right now there are no logs from the failure moment. Before the next
wedge, set up persistent capture:

1. **Stream kernel USB events to a logfile** so resets / disconnects /
   `error -110` timeouts get recorded:
   ```
   dmesg -wT > /var/log/usb-events.log &
   # or: journalctl -kf | grep -i 'usb\|cdc_acm\|tty'
   ```

2. **Run otbr-agent under systemd** so its dying breath is in the
   journal even when started by something other than an interactive
   shell. Minimal unit:
   ```ini
   # /etc/systemd/system/otbr-agent.service
   [Unit]
   Description=OpenThread Border Router agent (mt2m)
   After=dbus.service network.target

   [Service]
   ExecStart=/usr/local/sbin/otbr-agent -I wpan0 -B ot-infra -v -d 7 \
     'spinel+hdlc+uart:///dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_…-if00?uart-baudrate=460800'
   Restart=on-failure
   RestartSec=2s
   StandardOutput=journal
   StandardError=journal

   [Install]
   WantedBy=multi-user.target
   ```
   Then `journalctl -u otbr-agent -e` after every failure.

3. **Build the RCP firmware with OpenThread logs enabled**
   (`CONFIG_OPENTHREAD_LOG_LEVEL_INFO=y` in ot_rcp menuconfig) so the
   radio-side state at the moment of failure comes through Spinel.

### Likely culprits to investigate

- **USB autosuspend** — kernel may be putting the device to sleep on
  idle. Check (and disable per-device if needed):
  ```
  DEV=$(udevadm info -q path -n /dev/ttyACM0 | awk -F/ '{print $(NF-2)}')
  cat /sys/bus/usb/devices/$DEV/power/control          # want "on", not "auto"
  echo on | sudo tee /sys/bus/usb/devices/$DEV/power/control
  ```
  Make persistent via a udev rule.

- **Brown-out under 802.15.4 TX** — TX bursts are current-hungry; an
  underpowered USB port can reboot the C6 and leave TinyUSB in a half-
  reset state the kernel doesn't notice. A powered hub usually fixes
  this.

- **TinyUSB endpoint stall** in the C6 firmware under sustained traffic.
  Less common but observed.

- **otbr-agent's spinel reset sequence racing the firmware** — usually
  self-recovers, sometimes doesn't.

### Soft-recovery options (avoid physical replug)

Three levels, each more aggressive:

1. **Driver unbind/rebind** — fastest, no extra hardware:
   ```
   DEV=$(udevadm info -q path -n /dev/ttyACM0 | awk -F/ '{print $(NF-2)}')
   echo "$DEV" | sudo tee /sys/bus/usb/drivers/usb/unbind
   sleep 1
   echo "$DEV" | sudo tee /sys/bus/usb/drivers/usb/bind
   ```

2. **`authorized` toggle** — slightly more thorough:
   ```
   echo 0 | sudo tee /sys/bus/usb/devices/$DEV/authorized
   sleep 1
   echo 1 | sudo tee /sys/bus/usb/devices/$DEV/authorized
   ```

3. **Physical VBUS power cycle via `uhubctl`** — only works with hubs
   that support per-port power switching (~$20 hubs exist):
   ```
   sudo uhubctl -l <hub> -p <port> -a cycle
   ```
   This is the closest equivalent to actually unplugging the dongle.

### Target resilience pattern

```
otbr-agent.service
  ExecStartPre = /usr/local/bin/otbr-preflight.sh
    └─ check /dev/serial/by-id/... exists
    └─ if Spinel handshake fails: unbind/rebind USB
    └─ if still failing: uhubctl power cycle
    └─ exit non-zero if device unreachable (systemd retries)
  Restart = on-failure
  RestartSec = 2s
```

Plus an independent higher-level watchdog (e.g. the MQTT bridge polls
`ot-ctl state` and triggers `systemctl restart otbr-agent` if the Thread
state is unhealthy for >30s).

Defer this work until the basic MQTT bridge is up — the reliability
investment only matters once the system is doing useful work to be
unreliable about.

## Matter controller

We'll run python-matter-server as a controller. This service is not suited for "bare metal" running, it has too many assumptions
baked in about running in a docker container. Since we're using docker, we can isolate both python-matter-server and
ot-br-posix into the same container, and let them share the network as much as they'd like, knowing they can never
escape it.

### BLE commissioning needs a host Bluetooth adapter

Matter commissioning starts over BLE (the commissioner scans for the device's
0xFFF6 service, does PASE with the setup passcode, installs the fabric, then
hands over the Thread dataset). The C6 running `ot_rcp` is an 802.15.4-only
radio — it does NOT give the host any BLE — so commissioning needs a separate
Bluetooth adapter (host built-in or a USB BT dongle).

Annoyingly, Bluetooth adapters are bound to a **network namespace**. A
container on the default bridge sees no BT adapters at all; you can't
`--device` an HCI adapter the way you can the C6's USB serial. So the only
reliable way to give the container BLE is to share the host's netns
(`--network host`). That breaks the isolation the project is built on (wpan0
and avahi land in the host netns, and avahi can then leak Matter mDNS onto the
real LAN).

Mitigations if running host-net: lock avahi down to `ot-infra`
(`allow-interfaces=ot-infra` in `avahi-daemon.conf`), mask the host's own
`bluetoothd` so it doesn't fight the container's for the adapter, run
`bluetoothd` inside the container on its private system bus, and pass
`--bluetooth-adapter 0` to matter-server (it defaults to the `999` "no adapter"
sentinel otherwise).

**BLE is only needed at commission time** — once a device is on Thread,
matter-server reaches it over IPv6 via wpan0 forever after. So the host-net
exposure should be a transient "commissioning mode" run, not the 24/7 posture:
commission with host-net, `make stop`, then go back to the isolated bridge-mode
run. Fabric state persists in `/mt2mqtt-run`, so the device stays commissioned
across the switch.

### TODO: use the C6 itself as the BLE commissioning interface (no host adapter, no host-net)

The C6 has a single 2.4 GHz radio that can do BLE *and* 802.15.4 via
coexistence (it's the chip Espressif sells for Matter). Since we own the
firmware, we could make the one dongle provide both — eliminating the separate
BT adapter *and* the `--network host` isolation break.

The key design point: **don't move the commissioner onto the ESP.** The
commissioner is the fabric admin — whoever holds the fabric Root CA controls
the device operationally over Thread (see Step 4). Our long-lived controller
(matter-server → MQTT bridge) is on the host and must own the fabric, so the
commissioner brain (PASE, SPAKE2+, attestation, NOC signing — all the chip
code we already have) stays host-side. Moving it to the ESP would just create a
fabric-key-syncing problem.

Instead, cut the stack at **HCI**: the ESP does the BLE radio + link layer, the
host runs everything above HCI (BlueZ + chip, unchanged). "Exposing a BT
device" really just means *speaking HCI to BlueZ* — it does not require a USB
btusb device. Two realizations:

1. **HCI over a second CDC**: firmware exposes a second USB CDC speaking H4
   HCI; host does `btattach -B /dev/ttyACMy -P h4` → `hci0`.
2. **HCI tunneled over a Spinel vendor extension** → host shim pumps the HCI
   frames into the kernel's `/dev/vhci` (`hci_vhci` virtual controller) → a
   real `hci0`. matter-server commissions over it with zero changes to chip or
   BlueZ.

```
ESP: 802.15.4 link  ──Spinel─────────────► otbr-agent
ESP: BLE link layer ──Spinel vendor frames─► host shim → /dev/vhci → hci0 → BlueZ → matter-server
```

The vhci route is the attractive one: one cable does everything, and because
vhci is created by our own daemon in its own netns, `hci0` *may* land in the
container's namespace — which would kill the host-net isolation break too.

Two unknowns to verify before committing to this:
- Does ESP-IDF support BLE-controller + 802.15.4-RCP coexistence in one
  firmware build? (Radio is shared; coex is mandatory. And note the BR is
  *routing* the mesh while BLE scans — more contention than an end-device,
  which interacts with the Step 7 USB/stability work.)
- Does a `vhci`-created `hci0` actually register in the caller's network
  namespace (container-local) or in init_net? If init_net, we still need
  host-net and only save the separate dongle, not the isolation.

Defer until basic commissioning works end-to-end via the host-adapter route —
debug coexistence + composite USB + Matter commissioning separately, not all at
once, and always keep the host-adapter path as a working fallback.

### RESOLVED (2026-06-26): the HCI/vhci cut does NOT escape init_net

The second unknown above is answered, and it kills the vhci route's isolation
benefit. The Linux Bluetooth subsystem is hard-wired to the **initial network
namespace**: `bt_sock_create()` in `net/bluetooth/af_bluetooth.c` returns
`-EAFNOSUPPORT` for any netns that isn't `init_net`, and `hci_vhci` always
registers its adapter in `init_net`. Proven empirically — in a bridge-mode
container (even with `/dev/vhci` mapped in and `hci_vhci` loaded on the host):

```
hciconfig -a   → Can't open HCI socket.: Address family not supported by protocol
bluetoothd -n  → adapter.c:adapter_init() Failed to access management interface
```

Both are `socket(AF_BLUETOOTH, …)` failing because the container's netns isn't
`init_net`. Consequence: **anything that cuts the stack at HCI or below — vhci,
H4-over-CDC, userspace GATT over L2CAP — still needs a host BLE *host stack*
(bluetoothd) opening `AF_BLUETOOTH` sockets, so it's pinned to host-net.** The
vhci idea saves the separate dongle but not the isolation break. To keep the
container isolated you must either (a) not put any BT socket in the container,
or (b) cut the stack *above* GATT so no `AF_BLUETOOTH` socket exists on the host
at all.

That leaves two real paths.

### Path A — share host bluetoothd into the isolated container over D-Bus (IMPLEMENTED + VERIFIED 2026-07-02, see Step 9)

`bluetoothd` runs on the **host** (in `init_net`, where BT works). The container
— matter-server + otbr-agent, in its **isolated** internal network — reaches
BlueZ only through a **bind-mounted system D-Bus socket**
(`/run/dbus/system_bus_socket`). D-Bus is a unix socket, so it is *not*
netns-bound; it crosses the boundary fine. The GATT/BTP traffic flows
bluetoothd↔device in `init_net`; the container just issues D-Bus method calls.
wpan0 and avahi stay in the container's netns, so the LAN-isolation invariant
holds — only the D-Bus socket crosses, nothing routable.

```
host (init_net):  hci0 ── bluetoothd ──┐ /run/dbus/system_bus_socket (bind-mounted)
                                       ▼
container (isolated netns):  matter-server (chip BlueZ delegate, unchanged) → otbr → MQTT
```

Why this is the quick win: **zero firmware work, zero custom chip build.** Stock
matter-server wheels, stock BlueZ, just a bind mount and a host-side bluetoothd.
This is the simplest/fastest path to "BT commissioning without the container in
host-net."

The caveat that had to be verified before relying on it: chip's Linux BLE layer
must be **purely BlueZ-over-D-Bus** and never open a raw `AF_BLUETOOTH`/L2CAP
socket from the matter-server process itself (which would hit the same
`init_net` wall). Matter commissioning is BTP-over-GATT (not L2CAP CoC), so it
was *probably* D-Bus-only — and Step 9 **confirmed it**: an isolated container
completed a full commission (BLE scan → PASE → CASE) over nothing but the
bind-mounted host bus. Note that because the *host's* bluetoothd (not the
container's) now owns the adapter, the WirePlumber-masking dance from Step 8 is
**no longer needed** — the container drives the host daemon over D-Bus instead
of fighting it for the adapter. Still requires a host BT adapter, and the rfkill
gotcha (desktop BT toggle soft-blocks the radio) still applies host-side.

### Path B — run the full BLE commissioning GATT client on the C6 (cleanest end state)

Cut the stack at **GATT**, higher than the HCI cut above. The C6 runs the full
BLE stack (controller *and* host) and performs the Matter GATT-client role
itself; the host link carries only "write these bytes to C1 / here's a C2
indication" over a char device. **No `AF_BLUETOOTH` socket exists anywhere on
the host**, so the whole thing runs inside the isolated netns — no bluetoothd,
no host adapter, no host-net, one cable for Thread + BLE.

Matter's BLE usage is narrow enough to make this tractable: the commissioner is
just a GATT central doing scan(`0xFFF6`+discriminator) → connect → discover C1
(`…9D11`, write) / C2 (`…9D12`, indicate) → subscribe C2 → relay opaque **BTP**
frames. The C6 firmware (NimBLE central) does exactly that and nothing more — it
runs no Matter logic, it's a dumb GATT pipe for one service.

```
C6:  802.15.4 RCP        ──Spinel──────────────► otbr-agent
C6:  BLE central + GATT  ──custom C1/C2 frames over 2nd CDC/Spinel-vendor──┐
                                                                          ▼
container (isolated):  driver shim → chip BlePlatformDelegate → matter-server
```

The commissioner brain (PASE/SPAKE2+, attestation, NOC signing, fabric
ownership) stays host-side — only the radio + GATT *transport* moves to the C6,
so the Step 4 fabric-authority invariant is preserved.

Cost: a **custom chip build** with a `BlePlatformDelegate` / `BleConnectionDelegate`
(`src/ble/BleLayer.h`) wired to the serial driver instead of BlueZ — well-defined
interface, but real C++/build work and divergence from the stock wheels. Plus
the firmware coexistence question above (RCP + BLE central on one radio), bounded
to commissioning windows.

### Recommendation

Do **Path A** first — it's the fastest way to get isolated commissioning working
and de-risks the rest of the bridge. Keep **Path B** as the eventual one-cable,
no-host-adapter end state, and pursue it only once Path A proves the isolated
posture and the MQTT bridge is doing useful work. De-risk Path B with two
independent spikes (firmware NimBLE central that dumps C1/C2; trivial custom chip
`BlePlatformDelegate` that logs) before committing.

## Step 8 — First successful commission via matter-server in the container (DONE)

> **Posture superseded by Step 9 (Path A).** The matter-server usage below is
> still exactly how commissioning works; what changed is the *isolation posture*:
> Step 8 used `--network host` + a container-local `bluetoothd` + masking the
> host's `bluetooth.service`. Step 9 replaced all of that with a single isolated
> `make start` that reaches the host's bluetoothd over a bind-mounted D-Bus
> socket. The `start-commission`/`stop-commission` targets and `MT2M_COMMISSION`
> described here no longer exist. Read Step 8 for the matter-server flow and the
> WirePlumber/rfkill gotchas; ignore its host-net/masking mechanics.

Commissioned the Aqara button end-to-end from inside the Docker container using
`python-matter-server` (not chip-tool), over BLE + Thread. This is the first
time CASE completed and the device did **not** roll back — the Step 6
dummy-interface mDNS fix works the same inside the container.

### The flow

1. `make start-commission` — host-net container with `MT2M_COMMISSION=1`. The
   entrypoint creates `ot-infra`, starts dbus/avahi, and starts the container's
   own `bluetoothd`.
2. `make shell`, then start the stack by hand (the entrypoint only stages it):
   ```
   mt2mqtt-otbr-agent &                    # foreground/-v -d 7, give it a shell
   mt2mqtt-otbr-net-start                  # ifconfig up + thread start
   mt2mqtt-otbr-net-state                  # poll until "leader"
   mt2mqtt-matter-server-commission.sh &   # matter-server --bluetooth-adapter 0
   ```
3. Edit `/mt2mqtt-run/matter-ws-check.py`: uncomment **Route A** only
   (`commission_with_code(code, network_only=False)` — BLE + Thread for a fresh
   device). Route B (`network_only=True`) is the no-BLE on-network recommission
   path and is wrong for first-time pairing.
4. Wake the button into BLE commissioning mode (advertising 0xFFF6), then
   `python /mt2mqtt-run/matter-ws-check.py`.

Success markers in the matter-server log:
```
Established secure session with Device       # PASE -> CASE
Commissioning complete
Commissioned Node ID: 1 ... successful
Subscription succeeded with report interval [1, 60]
```

### Gotcha: host BlueZ keeps respawning, and WirePlumber is why

`--network host` shares the host netns, so the container's `bluetoothd` and the
host's fight over the single Intel adapter (hci0). `systemctl stop bluetooth`
does **not** hold: BlueZ is D-Bus-activated, and **WirePlumber's bluetooth
monitor** pokes `org.bluez` constantly, respawning the daemon immediately.

Identify the reactivator with the system bus (journalctl won't name it):
```
sudo dbus-monitor --system "destination='org.bluez'"   # then stop bluetooth
sudo busctl status :1.NNN                               # map sender to PID
```

Fix is to **mask** (not just stop) for the commissioning window — the Makefile
now does `mask`+`stop` in `start-commission` and `unmask`+`start` in
`stop-commission`. Only cost is no host BT audio while commissioning.

### Gotcha: the OS-UI Bluetooth toggle is rfkill, and it blocks power-on

Toggling Bluetooth off in the desktop UI issues an `rfkill` soft-block on the
radio. With it blocked, the container's `bluetoothd` cannot power the adapter
on — `bluetoothctl power on` fails and the daemon log shows:
```
src/shared/mgmt.c:can_read_data() [0x0000] command 0x05 status: 0x03   # Set Powered -> Failed
src/rfkill.c:rfkill_init() Failed to open RFKILL control device
```
(The container has no `/dev/rfkill`, so its bluetoothd can't clear the block
itself.) rfkill is global kernel state (not netns-scoped), so fix it on the
host:
```
rfkill list bluetooth
sudo rfkill unblock bluetooth
```
Correct commissioning posture: **radio unblocked (rfkill off) + host
`bluetooth.service` masked.** The two are independent — mask stops the daemon,
rfkill controls the radio.

### Gotcha: ot-ctl "connect session failed: No such file or directory"

Means `otbr-agent` isn't running — `ot-ctl` is a thin client for the daemon's
control socket `/run/openthread-wpan0.sock` (named from `-I wpan0`). No daemon,
no socket. Usually the C6 isn't talking (USB wedge, Step 7) or the `--device`
mapping didn't resolve because the dongle wasn't plugged in at `docker run`
time (the `$(RCP)` readlink was empty). Check `pgrep -af otbr-agent` and
`ls -l /dev/ttyACM0` inside the container.

### The 11-digit manual code works directly (no `MT:` prefix)

`commission_with_code` accepts either the QR payload (`MT:...`) or the 11-digit
**manual** pairing code, and auto-detects. Pass the manual code as-is —
prefixing `MT:` makes it a malformed QR payload (`Integrity check failed`).

Two benign `CHIP_ERROR` lines appear and can be ignored:
```
Long discriminator is required
Failed to start commissionable node discovery over Wi-Fi PAF ... Invalid argument
```
matter-server probes several discovery transports in parallel. Wi-Fi PAF needs
the long (12-bit) discriminator, which the manual code only carries as the
short (4-bit) form, so that one transport bails instantly. BLE discovery runs
alongside and is what actually carries the commissioning. Only matters if two
unprovisioned devices advertise at once (short discriminator could be
ambiguous) — a non-issue with a single button.

### After commissioning: drop back to isolated mode

> **Obsolete under Step 9.** There is no mode to drop back to anymore — Path A's
> `make start` is *already* isolated while commissioning, so you just leave it
> running. The `make stop-commission` step below is gone. Fabric state still
> persists in `/mt2mqtt-run/matter-server-data`, and the verification commands
> still apply.

BLE is only needed at pair time. Fabric state persists in
`/mt2mqtt-run/matter-server-data`, so:
```
make stop-commission     # tears down, unmasks + restarts host bluetooth
make start               # isolated bridge-mode container, no host-net, no BLE
```
matter-server reaches the device over wpan0 from here on. Verify it stuck:
```
ot-ctl child table          # button present as a child
ot-ctl srp server service   # _matter._tcp WITHOUT "deleted: true"
```

## Step 9 — Isolated BLE commissioning via host bluetoothd over D-Bus (Path A, DONE + VERIFIED)

Implemented and verified NOTES "Path A" (2026-07-02): the container commissions
over BLE **without** `--network host` and **without** its own `bluetoothd`. It
stays in its isolated netns and reaches the **host's** BlueZ purely over the
host system D-Bus socket, bind-mounted in. This retires the Step 8 host-net
posture entirely — commissioning and steady-state operation are now **one
container mode**.

### Why it works (the assumption, now confirmed)

D-Bus is a unix socket, not netns-bound, so it crosses into the isolated
container while nothing routable does. chip's Linux BLE layer is a **pure
BlueZ-over-D-Bus client** (BTP-over-GATT — WriteValue method calls, indication
`PropertiesChanged` signals — no raw `AF_BLUETOOTH`/L2CAP socket from the
matter-server process), so pointing only that process's system bus at the host
bus is enough. All radio work happens in the host's bluetoothd in `init_net`;
only D-Bus messages cross. wpan0, avahi, and the container's *own* system dbus
(for otbr-agent) stay isolated. mDNS is chip minimal-mdns over raw sockets on
`ot-infra`, not D-Bus, so redirecting matter-server's bus doesn't disturb
operational discovery.

### As built

> **Mount mechanism superseded by Step 10.** Step 9 first mounted the *raw* host
> system bus. Step 10 replaced that with a `org.bluez`-only `xdg-dbus-proxy`
> (`make bluez-proxy`) whose socket lives in RUNDIR, so the container never sees
> the raw host bus. The `matter-server/run` `org.bluez` probe below is unchanged
> (just repointed + extracted to `scripts/bt-host-check.sh`).

- **`Makefile` `start`**: single mode, adds one mount and nothing else (no sudo,
  no host-bluetooth juggling):
  ```
  -v /run/dbus/system_bus_socket:/run/host_dbus/system_bus_socket
  ```
  `start-commission`, `stop-commission`, `MT2M_COMMISSION`, `--network host`,
  the `mask`/`unmask` dance, and the `-commission` container-name plumbing are
  all removed.
- **`s6-rc.d/matter-server/run`**: enables BLE (`--bluetooth-adapter 0` +
  `DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/host_dbus/system_bus_socket`) **only
  when `org.bluez` is actually owned on that bus** — probed with a `dbus-send`
  `NameHasOwner` call, not just "is the socket mounted." The socket is mounted
  unconditionally, but the host's dbus-daemon owns it even when bluetoothd/the
  adapter are down; keying on `org.bluez` ownership means BLE lights up
  automatically when the host has a working BlueZ and the bridge degrades
  cleanly to operational-only (no crash) when it doesn't. A host-net fallback
  branch (local `hci*` adapter → container bluetoothd) remains but is unused.
- **`s6-rc.d/bluetoothd`**: now a dormant fallback. In the isolated netns it
  finds no adapter and self-downs; the primary path never uses it.

### Verified working

An isolated `make start` (no host-net) commissioned a device end-to-end:
BLE scan → PASE → CASE all completed over the bind-mounted host bus, proving the
no-`AF_BLUETOOTH` assumption. Host prep is just "Bluetooth on" (host bluetoothd
running, radio not rfkill-blocked) — no masking. If a commission fails with the
adapter down: `sudo rfkill unblock bluetooth` on the host (desktop BT toggle
soft-blocks the radio; NOTES Step 8 rfkill gotcha still applies host-side).

### Gotcha: edit the pairing code in the CONTAINER, not the host repo

The commissioning client that actually runs is the container's copy of
`matter-ws-check.py` (under the `/mt2mqtt-run` bind mount), **not** the host repo
copy `host/docker/mqtt_bridge/matter-ws-check.py`. Editing the setup code on the
host alone does nothing. The failure symptom when the code is stale/placeholder:
```
[chip.native.BLE] Skip connection: Device discriminator does not match: 2835 != 2
... Discovery timed out ... Secure Pairing Failed
```
i.e. chip is BLE-scanning fine (Path A working!) but the code encodes a
discriminator that no advertising device matches. Fix the code in the
container's copy and it pairs.

### Left to do

1. ~~**Scope the D-Bus exposure (step 2b).**~~ **DONE in Step 10** — raw mount
   replaced by an `org.bluez`-only `xdg-dbus-proxy`.
2. **Remove the vestigial container `bluetoothd`.** Delete the `bluetoothd` +
   `bluetoothd-log` s6 services, `matter-server/dependencies.d/bluetoothd`, and
   optionally the `bluez` apt install (chip needs none of bluez's userspace
   tools; the `NameHasOwner` probe uses `dbus-send` from the `dbus` package).

## Step 10 — D-Bus proxy hardening, log relocation, RCP watchdog, and the road to non-root

Everything below landed on top of Step 9. Net effect: commissioning still works
the same, but the container no longer touches the raw host system bus, logs live
with the rest of the run state, a wedged radio is detected from inside the
container, and we have a clear (deferred) plan for running unprivileged.

### D-Bus exposure scoped to `org.bluez` (step 2b, DONE) — and it must run as root

The raw host-bus mount is gone. A filtered `xdg-dbus-proxy` now forwards **only**
`org.bluez` from the host system bus; the container reaches BlueZ through that
and nothing else. Mechanics:

- **`make bluez-proxy`** runs the proxy in the foreground (its own terminal;
  Ctrl-C to stop). Its socket is created inside RUNDIR
  (`mt2mqtt-run/bluez-proxy.sock`), so it rides the existing `/mt2mqtt-run` mount
  — no separate `-v`, and the raw `/run/dbus/system_bus_socket` is never bind-
  mounted. `make start` mounts nothing BT-specific; commissioning = run
  `make bluez-proxy` in another terminal, otherwise the container is
  operational-only.
- **`matter-server/run`** `HOST_BUS=/mt2mqtt-run/bluez-proxy.sock`, and the
  `org.bluez` `NameHasOwner` probe was extracted to **`scripts/bt-host-check.sh`**
  (copied to `/mt2mqtt-bin` in the Dockerfile, on PATH). BLE enables only when
  that probe passes, so a missing/idle proxy degrades cleanly to operational.

**KEY finding — the proxy MUST run as root (`sudo xdg-dbus-proxy`).** This
reverses the earlier "no sudo, the at_console user is enough" hope. That
reasoning was about the *upstream* leg (proxy → bluez). The blocker is the
*downstream* leg (container → proxy): D-Bus `EXTERNAL` auth verifies the client
via `SO_PEERCRED`. The container runs as root (host uid 0), so the proxy must
also be uid 0 or the auth handshake never completes and the client sees:
```
Failed to open connection to "system" message bus: Did not receive a reply.
```
A proxy run as your normal user accepts the connection but can't authenticate a
uid-0 peer → no reply. Fix: run the proxy under `sudo`. (`make bluez-proxy` still
shows the non-sudo form; invoke it with sudo, or we bake sudo in.) The **whole
host system bus exposure motivation** stands: raw bus into a root container ≈
host root (systemd `StartTransientUnit`, UDisks2 mounting host disks,
NetworkManager dispatcher scripts) — see the threat-model discussion under
"Matter controller". An in-*container* proxy would not help a compromised
container (raw socket still reachable); the host-side proxy does.

### Logs moved into the run dir

All in-container logs now write under **`/mt2mqtt-run/logs`** (per-service s6-log
dirs plus the `ot-infra` / `otbr-agent-net` oneshot `*.log` files) instead of a
separate `/var/log` bind mount. The `LOGDIR` / `mt2mqtt-log` mount is removed;
logs now ride the single RUNDIR mount and survive `make stop` with the rest of
the state. Each log run script `mkdir -p /mt2mqtt-run/logs` first (s6-log creates
only the leaf, not the parent). Note the old `/var/log/s6-overlay` "catch-all" in
earlier comments was never actually active (`S6_LOGGING` unset → uncaught output
goes to container stdout / `make logs`), so nothing was lost.

### `rcp-watchdog` — detect a wedged Spinel RCP from inside the container

New s6 longrun (`s6-rc.d/rcp-watchdog`) that partially closes the Step 7 TODO:
**detection** is now automatic (recovery stays host-side — the container can't
unbind/rebind USB). It does **not** touch `/dev/ttyACM0` (that would fight
otbr-agent for the exclusive port). Instead it pings the daemon's control socket
(`timeout 5 ot-ctl state`) every 10s; on repeated failure it greps the otbr-agent
log for the Spinel-init signature (matched loosely as `Platform.*Init.*Failure`,
not a fixed file:line) and, if found, logs loudly and drops a marker
`/mt2mqtt-run/rcp-wedged` (host-visible, for a host recovery step to act on and
clear). No dedicated logger — output goes to container stdout so it shows in
`make logs`. Depends on `otbr-agent` only, deliberately **not** `otbr-agent-net`.
Its unique value is the *mid-run* wedge (daemon up but radio stopped answering),
which nothing else notices.

Why the container doesn't just die on a wedge: **otbr-agent is a longrun**, and a
longrun counts as "up" the instant it's *spawned*, not when healthy — so a
crash-loop doesn't fail s6 bring-up; s6-supervise just restarts it. The container
only dies indirectly when the **`otbr-agent-net` oneshot** times out (~150s,
`timeout-up 150000`) and `S6_BEHAVIOUR_IF_STAGE2_FAILS=2` tears it down. And even
then, restarting the container can't fix a *host* USB wedge — hence recovery must
be host-side (unbind/rebind by sysfs path, escalating to `uhubctl`), gated before
`docker run`. The reliable health check for a wedge is a **Spinel round-trip**
(e.g. `spinel-cli.py version`), because every cheap check (device node exists,
port opens, baud) passes while wedged — only the round-trip times out.

### Running the container as non-root — DEFERRED until after Path B

Goal: the container's uid should be **unprivileged on the host** (a container
escape lands as a subuid, not host root), while staying uid 0 *inside* (s6,
otbr-agent netns setup, dbus all need in-container root). The tool is Docker
**`userns-remap`** (`/etc/docker/daemon.json: {"userns-remap":"default"}`), NOT
`--user` (which would break in-container root). It's daemon-global (all
containers remap; opt out per-container with `--userns=host`).

Consequences for this container:
- **`/dev/ttyACM0`** (`root:dialout 0660`) becomes unreadable by the remapped
  container-root → needs a host udev rule to `0666` (or chown to the subuid).
  `/dev/net/tun` is already `0666`. `NET_ADMIN` is fine (caps are userns-relative;
  it only manages its own netns).
- **RUNDIR + existing persisted state** must be `chown`ed to the subuid or the
  container gets EACCES on Thread/fabric state.
- **The proxy socket auth breaks again (the decider).** Post-remap the container
  still thinks it's uid 0 but `SO_PEERCRED` reports the host subuid → the same
  `EXTERNAL` mismatch → "Did not receive a reply", plus the root-created socket is
  `0600` so the subuid can't even connect. So **userns-remap and the host-BlueZ
  proxy fight over exactly this D-Bus auth.**

Decision (2026-07-07): **do Path B first, then non-root.** Path B (C6 runs the
BLE GATT client itself; no host `AF_BLUETOOTH`/D-Bus socket at all) is the
*userns-clean* end state — with no host socket to authenticate against, the
remap/auth conflict disappears. Chasing non-root before Path B would mean solving
the proxy-under-userns auth only to throw it away. So the operational-hardening
via userns-remap waits until commissioning no longer needs a host BT socket.

