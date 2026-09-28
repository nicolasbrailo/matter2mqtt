"""zigbee2mqtt semantics over a Device: set, get and rename.

`set` coerces and validates every property before touching the device, then sorts out the
properties that would fight each other. `get` live-reads from the device rather than the
cache. `rename` writes the name onto the device itself. All raise RequestError for anything
the caller got wrong; nothing here publishes.
"""
from ..errors import RequestError
from ..matter.data_model import NODE_LABEL_PATH
from .caps import GETTABLE, SETTABLE, as_number
from .devices import valid_name

MAX_LABEL = 32  # Matter caps NodeLabel at 32 characters


def log(msg):
    print(f"[control] {msg}", flush=True)


def request_options(payload):
    """Pop the per-request options out of a set payload: what's left are properties.

    - transition: seconds, like z2m (Matter wants 1/10 s)
    - with_on_off: false picks MoveToLevel over MoveToLevelWithOnOff, so setting brightness
      doesn't switch a light on
    """
    transition = as_number(payload.pop("transition", 0))
    with_on_off = payload.pop("with_on_off", True)
    # TODO: transition doesn't apply to `state`: Matter's On()/Off() take no transition time,
    # so {"state":"OFF","transition":3} switches off instantly while brightness/colour fade.
    # z2m fades the level to 0 and then switches off; we could do the same.
    if isinstance(transition, bool) or not isinstance(transition, (int, float)) or transition < 0:
        raise RequestError(f"transition: expected seconds >= 0, got {transition!r}")
    if not isinstance(with_on_off, bool):
        raise RequestError(f"with_on_off: expected true or false, got {with_on_off!r}")
    return {"transition": round(transition * 10), "with_on_off": with_on_off}


def capability(dev, prop, access, what):
    cap = dev.props.get(prop)
    if cap is None:
        raise RequestError(f"unknown property {prop!r}; have {sorted(dev.props)}")
    if not cap.access & access:
        raise RequestError(f"{prop}: {what}")
    return cap


def order_writes(dev, writes):
    """Sort out properties that fight each other, per endpoint.

    - state OFF wins: brightness goes out as MoveToLevelWithOnOff, which would switch the
      light straight back on. {"state":"OFF","brightness":10} must just turn it off.
    - state ON goes first: ColorControl/LevelControl commands are dropped by the device while
      it's off (we don't set ExecuteIfOff), so colour set before ON would be lost.
    - state TOGGLE with anything else is ambiguous -- we can't know what it'll toggle into.
    - color_temp and color are two different colour modes; whichever ran last would win.
    """
    by_ep = {}
    for cap, value in writes:
        by_ep.setdefault(cap.ep, []).append((cap, value))

    ordered = []
    for ep, group in by_ep.items():
        names = [c.name for c, _ in group]
        if "color_temp" in names and "color" in names:
            raise RequestError(
                f"endpoint {ep}: color_temp and color are different colour modes, set one")
        state = next((v.upper() for c, v in group if c.name == "state"), None)
        others = [c.prop for c, _ in group if c.name != "state"]
        if state and others:
            if state == "TOGGLE":
                raise RequestError(f"endpoint {ep}: can't combine state TOGGLE with {others}")
            if state == "OFF":
                log(f"{dev.friendly_name}: endpoint {ep} turning off, ignoring {others}")
                group = [(c, v) for c, v in group if c.name == "state"]
            else:
                group.sort(key=lambda cv: cv[0].name != "state")  # ON first
        ordered.extend(group)
    return ordered


async def apply_set(client, dev, payload):
    """Write the properties in a set payload to the device."""
    payload = dict(payload)
    opts = request_options(payload)
    writes = []
    for prop, value in payload.items():  # validate everything before touching the device
        cap = capability(dev, prop, SETTABLE, "read-only")
        value = cap.coerce(value)
        if err := cap.validate(value):
            raise RequestError(f"{prop}: {err}")
        writes.append((cap, value))
    for cap, value in order_writes(dev, writes):
        await cap.write(client, dev.node_id, value, opts)
    # New state gets published when the device reports the attribute changes


async def refresh(client, dev, payload):
    """Re-read the properties in a get payload from the device itself (an empty payload reads
    everything). refresh_attribute also updates matter-server's cache, so the caller can just
    publish the device's state afterwards."""
    paths = []
    for prop in list(payload) or list(dev.props):
        cap = capability(dev, prop, GETTABLE, "not readable")
        paths.extend(p for p in cap.paths() if p not in paths)
    for path in paths:
        await client.refresh_attribute(dev.node_id, path)


async def rename(client, dev, new_name, taken):
    """Rename a device by writing its NodeLabel, which is where friendly_name comes from.

    Unlike z2m -- which keeps friendly names in its own config -- the name lives on the device,
    so it survives a reinstall of this bridge and any other controller sharing the device sees
    it too. `taken` is the set of names the other devices already use.
    """
    if not isinstance(new_name, str) or not new_name.strip():
        raise RequestError('expected {"from": "<device>", "to": "<new name>"}')
    new_name = new_name.strip()
    if len(new_name) > MAX_LABEL:
        raise RequestError(f"{new_name!r} is {len(new_name)} characters, Matter allows {MAX_LABEL}")
    if not valid_name(new_name):
        raise RequestError(f"{new_name!r} can't be a device name: it would collide with a bridge "
                           f"topic, a set/get request, or an mqtt wildcard")
    if new_name in taken:
        raise RequestError(f"{new_name!r} is already used by another device")
    log(f"{dev.friendly_name}: writing NodeLabel {new_name!r}")
    await client.write_attribute(dev.node_id, NODE_LABEL_PATH, new_name)
    # A write doesn't update matter-server's cache, so re-read it before anyone rebuilds
    await client.refresh_attribute(dev.node_id, NODE_LABEL_PATH)
    return new_name
