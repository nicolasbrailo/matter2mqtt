"""Capabilities: one z2m property, and everything it knows about itself.

A Cap generates its own `exposes` entry, reads its value from matter-server's attribute
cache, validates and coerces an incoming value, and executes the write as Matter commands.
That keeps the published schema and the dispatcher from drifting apart: they're the same
object. `endpoint_caps` decides which of them an endpoint gets.

Values are in z2m units, not Matter's raw ones: brightness 0..254, color_temp in mireds,
color as hue 0..360 / saturation 0..100 and/or CIE x/y 0..1, temperature in °C, etc.
"""
from chip.clusters import Objects as Clusters

from ..matter.data_model import (
    BAT_OK, COLOR, COLOR_CAP, COLOR_CAPABILITIES, COLOR_MODE, COLOR_MODES, COLOR_TEMP_MAX,
    COLOR_TEMP_MIN, COLOR_TEMP_MIREDS, CURRENT_HUE, CURRENT_LEVEL, CURRENT_SATURATION,
    CURRENT_X, CURRENT_Y, FEATURE_MAP, LEVEL, LIGHT_TYPES, MAX_LEVEL, MIN_LEVEL,
    MULTI_PRESS_MAX, NUMBER_OF_POSITIONS, OCCUPIED, ON_OFF, ONOFF, SWITCH, SWITCH_EVENTS,
    SWITCH_FEATURE, live_value, struct_field,
)

# z2m access bits
PUBLISHED = 1
SETTABLE = 2
GETTABLE = 4

# z2m's names for a press count
PRESS_NAMES = {1: "single", 2: "double", 3: "triple", 4: "quadruple", 5: "quintuple"}


def press_name(n):
    return PRESS_NAMES.get(n, f"{n}x")


def as_number(value):
    """Numbers that arrived as strings -- some clients publish {"brightness": "123"}. Anything
    that doesn't look like a number is handed back untouched, for validate() to reject."""
    if isinstance(value, str):
        for parse in (int, float):
            try:
                return parse(value.strip())
            except ValueError:
                pass
    return value


def check_number(value, vmin=None, vmax=None):
    """Error string, or None if value is a number within [vmin, vmax]."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"expected a number, got {value!r}"
    if (vmin is not None and value < vmin) or (vmax is not None and value > vmax):
        return f"{value} out of range [{vmin}, {vmax}]"
    return None



class Cap:
    """One z2m property on one endpoint. Subclasses define how it maps onto Matter."""
    name = None
    access = PUBLISHED | GETTABLE
    attrs = ()  # (cluster, attribute) ids backing the value; re-read from the device on /get
    momentary = False  # true for a property that is an event, so it has no steady value

    def __init__(self, ep, cl):
        self.ep = ep
        self.prop = self.name  # Device suffixes it with _<ep> if the name repeats across endpoints

    def paths(self):
        return [f"{self.ep}/{cid}/{aid}" for cid, aid in self.attrs]

    def attr(self, node, cid, aid):
        return live_value(node, self.ep, cid, aid)

    def base(self, type_):
        return {"type": type_, "name": self.name, "property": self.prop,
                "access": self.access, "endpoint": self.ep}

    def expose(self):
        """List of z2m expose entries for this property."""
        raise NotImplementedError

    def read(self, node):
        """z2m value from matter-server's attribute cache."""
        raise NotImplementedError

    def coerce(self, value):
        """Normalise an incoming json value before validate() sees it."""
        return value

    def validate(self, value):
        """Error string, or None if value can be written."""
        return "read-only"

    async def write(self, client, node_id, value, opts):
        """opts carries the request's options: `transition` (in 1/10 s, as Matter wants it)
        and `with_on_off` (see LevelCap)."""
        raise NotImplementedError


class OnOffCap(Cap):
    name = "state"
    access = PUBLISHED | SETTABLE | GETTABLE
    attrs = [(ONOFF, ON_OFF)]
    COMMANDS = {"ON": Clusters.OnOff.Commands.On,
                "OFF": Clusters.OnOff.Commands.Off,
                "TOGGLE": Clusters.OnOff.Commands.Toggle}

    # Clients send booleans and 1/0 (sometimes as strings) as well as z2m's "ON"/"OFF"
    ALIASES = {True: "ON", False: "OFF", 1: "ON", 0: "OFF", "TRUE": "ON", "FALSE": "OFF"}

    def expose(self):
        return [{**self.base("binary"), "value_on": "ON", "value_off": "OFF", "value_toggle": "TOGGLE"}]

    def coerce(self, value):
        key = as_number(value)
        if isinstance(key, str):
            key = key.strip().upper()
        # bool is an int in python, so True/1 and False/0 land on the same entries
        return self.ALIASES.get(key, key) if not isinstance(key, float) else key

    def read(self, node):
        v = self.attr(node, ONOFF, ON_OFF)
        return None if v is None else ("ON" if v else "OFF")

    def validate(self, value):
        if not isinstance(value, str) or value.upper() not in self.COMMANDS:
            return f"expected ON, OFF or TOGGLE, got {value!r}"
        return None

    async def write(self, client, node_id, value, opts):
        await client.send_device_command(node_id, self.ep, self.COMMANDS[value.upper()]())


class LevelCap(Cap):
    name = "brightness"
    access = PUBLISHED | SETTABLE | GETTABLE
    attrs = [(LEVEL, CURRENT_LEVEL)]

    def __init__(self, ep, cl):
        super().__init__(ep, cl)
        self.vmin = cl[LEVEL].get(MIN_LEVEL) or 1
        self.vmax = cl[LEVEL].get(MAX_LEVEL) or 254

    def expose(self):
        return [{**self.base("numeric"), "value_min": self.vmin, "value_max": self.vmax}]

    def read(self, node):
        return self.attr(node, LEVEL, CURRENT_LEVEL)

    def coerce(self, value):
        return as_number(value)

    def validate(self, value):
        return check_number(value, self.vmin, self.vmax)

    async def write(self, client, node_id, value, opts):
        # WithOnOff: raising the level from 0 also switches the light on. `with_on_off: false`
        # in the request picks plain MoveToLevel, which leaves a switched-off light off (and is
        # then ignored by the device, see below).
        # TODO: a device that reports MinLevel 0 accepts brightness 0, which switches it off
        # through this command. Most report 1, so validate() rejects it there.
        # TODO: optionsMask/optionsOverride 0 means the device ignores this while it's off, so
        # brightness can't be pre-set on an off light. Bit 0 is ExecuteIfOff if we want that.
        cmd = (Clusters.LevelControl.Commands.MoveToLevelWithOnOff if opts.get("with_on_off", True)
               else Clusters.LevelControl.Commands.MoveToLevel)
        await client.send_device_command(node_id, self.ep, cmd(
            level=round(value), transitionTime=opts["transition"], optionsMask=0, optionsOverride=0))


class ColorTempCap(Cap):
    name = "color_temp"
    access = PUBLISHED | SETTABLE | GETTABLE
    attrs = [(COLOR, COLOR_TEMP_MIREDS)]

    def __init__(self, ep, cl):
        super().__init__(ep, cl)
        self.vmin = cl[COLOR].get(COLOR_TEMP_MIN)
        self.vmax = cl[COLOR].get(COLOR_TEMP_MAX)

    def expose(self):
        return [{**self.base("numeric"), "unit": "mired", "value_min": self.vmin, "value_max": self.vmax}]

    def read(self, node):
        return self.attr(node, COLOR, COLOR_TEMP_MIREDS)

    def coerce(self, value):
        return as_number(value)

    def validate(self, value):
        return check_number(value, self.vmin, self.vmax)

    async def write(self, client, node_id, value, opts):
        # TODO: like LevelCap, optionsMask 0 means this is ignored while the light is off
        await client.send_device_command(node_id, self.ep, Clusters.ColorControl.Commands.MoveToColorTemperature(
            colorTemperatureMireds=round(value), transitionTime=opts["transition"],
            optionsMask=0, optionsOverride=0))


class ColorCap(Cap):
    """z2m's `color`: {hue, saturation} and/or {x, y}, whichever the device supports."""
    name = "color"
    access = PUBLISHED | SETTABLE | GETTABLE
    XY_MAX = 0xFEFF  # Matter CurrentX/CurrentY are x * 65536, capped here

    def __init__(self, ep, cl):
        super().__init__(ep, cl)
        caps = cl[COLOR].get(COLOR_CAPABILITIES) or 0
        self.hs = bool(caps & COLOR_CAP.kHueSaturation)
        self.xy = bool(caps & COLOR_CAP.kXy)
        self.attrs = ([(COLOR, CURRENT_HUE), (COLOR, CURRENT_SATURATION)] if self.hs else []) + \
                     ([(COLOR, CURRENT_X), (COLOR, CURRENT_Y)] if self.xy else [])

    def feature(self, name, vmin, vmax):
        return {"type": "numeric", "name": name, "property": name, "access": self.access,
                "value_min": vmin, "value_max": vmax}

    def expose(self):
        out = []
        if self.hs:
            out.append({**self.base("composite"), "name": "color_hs",
                        "features": [self.feature("hue", 0, 360), self.feature("saturation", 0, 100)]})
        if self.xy:
            out.append({**self.base("composite"), "name": "color_xy",
                        "features": [self.feature("x", 0, 1), self.feature("y", 0, 1)]})
        return out

    def read(self, node):
        out = {}
        if self.hs:
            h, s = self.attr(node, COLOR, CURRENT_HUE), self.attr(node, COLOR, CURRENT_SATURATION)
            if h is not None:
                out["hue"] = round(h * 360 / 254)
            if s is not None:
                out["saturation"] = round(s * 100 / 254)
        if self.xy:
            x, y = self.attr(node, COLOR, CURRENT_X), self.attr(node, COLOR, CURRENT_Y)
            if x is not None:
                out["x"] = round(x / 65536, 4)
            if y is not None:
                out["y"] = round(y / 65536, 4)
        return out or None

    def coerce(self, value):
        if isinstance(value, dict):
            return {k: as_number(v) for k, v in value.items()}
        return value

    def validate(self, value):
        if isinstance(value, dict):
            if self.hs and "hue" in value and "saturation" in value:
                return check_number(value["hue"], 0, 360) or check_number(value["saturation"], 0, 100)
            if self.xy and "x" in value and "y" in value:
                return check_number(value["x"], 0, 1) or check_number(value["y"], 0, 1)
        wanted = " or ".join(f for f, ok in (("{hue, saturation}", self.hs), ("{x, y}", self.xy)) if ok)
        return f"expected {wanted}, got {value!r}"

    async def write(self, client, node_id, value, opts):
        # TODO: like LevelCap, optionsMask 0 means this is ignored while the light is off
        if self.hs and "hue" in value and "saturation" in value:
            cmd = Clusters.ColorControl.Commands.MoveToHueAndSaturation(
                hue=round(value["hue"] * 254 / 360), saturation=round(value["saturation"] * 254 / 100),
                transitionTime=opts["transition"], optionsMask=0, optionsOverride=0)
        else:
            cmd = Clusters.ColorControl.Commands.MoveToColor(
                colorX=min(round(value["x"] * 65536), self.XY_MAX),
                colorY=min(round(value["y"] * 65536), self.XY_MAX),
                transitionTime=opts["transition"], optionsMask=0, optionsOverride=0)
        await client.send_device_command(node_id, self.ep, cmd)


class ColorModeEnumCap(Cap):
    """z2m's `color_mode`: which of the colour modes the device is currently in. Read-only --
    it changes as a side effect of setting color_temp or color."""
    name = "color_mode"
    attrs = [(COLOR, COLOR_MODE)]

    def __init__(self, ep, cl):
        super().__init__(ep, cl)
        caps = cl[COLOR].get(COLOR_CAPABILITIES) or 0
        self.values = [name for bit, name in ((COLOR_CAP.kHueSaturation, "hs"),
                                              (COLOR_CAP.kXy, "xy"),
                                              (COLOR_CAP.kColorTemperature, "color_temp"))
                       if caps & bit]

    def expose(self):
        return [{**self.base("enum"), "values": self.values}]

    def read(self, node):
        v = self.attr(node, COLOR, COLOR_MODE)
        return None if v is None else COLOR_MODES.get(int(v))


class ActionCap(Cap):
    """z2m's `action`: a button press. Published only -- it's an event, not state, so there is
    nothing to read back and nothing to set. Matter reports these as Switch cluster events,
    which arrive as EventType.NODE_EVENT rather than as attribute updates.
    """
    name = "action"
    access = PUBLISHED
    momentary = True

    def __init__(self, ep, cl):
        super().__init__(ep, cl)
        sw = cl[SWITCH]
        feat = sw.get(FEATURE_MAP) or 0
        self.latching = bool(feat & SWITCH_FEATURE.kLatchingSwitch)
        self.multi = bool(feat & SWITCH_FEATURE.kMomentarySwitchMultiPress)
        self.long = bool(feat & SWITCH_FEATURE.kMomentarySwitchLongPress)
        self.release = bool(feat & SWITCH_FEATURE.kMomentarySwitchRelease)
        self.positions = sw.get(NUMBER_OF_POSITIONS) or 2
        self.max_presses = sw.get(MULTI_PRESS_MAX) or 2

    def expose(self):
        if self.latching:
            values = [f"position_{n}" for n in range(self.positions)]
        else:
            values = ["single"]
            if self.multi:
                values += [press_name(n) for n in range(2, self.max_presses + 1)]
            if self.long:
                values += ["hold", "release"]
        return [{**self.base("enum"), "values": values}]

    def read(self, node):
        return None  # momentary: never part of the state message

    def event(self, event_id, data):
        """Switch event -> z2m action name, or None for the events we don't publish."""
        data = data or {}
        if event_id == SWITCH_EVENTS.SwitchLatched.event_id:
            pos = struct_field(data, 0, "newPosition")
            return None if pos is None else f"position_{pos}"
        if self.long and event_id == SWITCH_EVENTS.LongPress.event_id:
            return "hold"
        if self.long and event_id == SWITCH_EVENTS.LongRelease.event_id:
            return "release"
        if self.multi:
            # A multi-press device also sends InitialPress/ShortRelease around every press;
            # only MultiPressComplete knows how many presses it turned out to be, so the
            # others are dropped to avoid counting one press twice.
            if event_id == SWITCH_EVENTS.MultiPressComplete.event_id:
                return press_name(struct_field(data, 1, "totalNumberOfPressesCounted") or 1)
            return None
        # No multi-press support: the press ends at ShortRelease, or at InitialPress on a
        # device that doesn't report releases either.
        if self.release and event_id == SWITCH_EVENTS.ShortRelease.event_id:
            return "single"
        if not self.release and event_id == SWITCH_EVENTS.InitialPress.event_id:
            return "single"
        return None


class SensorCap(Cap):
    """Read-only value from one attribute of a sensor cluster."""

    def __init__(self, ep, cl, name, attribute, unit, conv, binary):
        self.name = name
        super().__init__(ep, cl)
        self.cid, self.aid = attribute.cluster_id, attribute.attribute_id
        self.unit, self.conv, self.binary = unit, conv, binary
        self.attrs = [(self.cid, self.aid)]

    def expose(self):
        e = self.base("binary" if self.binary else "numeric")
        if self.binary:
            e.update(value_on=True, value_off=False)
        if self.unit:
            e["unit"] = self.unit
        return [e]

    def read(self, node):
        v = self.attr(node, self.cid, self.aid)
        return None if v is None else self.conv(v)


SENSORS = [
    # name, attribute, unit, raw Matter value -> z2m value, binary
    # The units/scaling are spec conventions; chip.clusters doesn't carry them as data.
    ("temperature", Clusters.TemperatureMeasurement.Attributes.MeasuredValue, "°C", lambda v: v / 100, False),
    ("humidity", Clusters.RelativeHumidityMeasurement.Attributes.MeasuredValue, "%", lambda v: v / 100, False),
    # Matter: 0.1 kPa == 1 hPa
    ("pressure", Clusters.PressureMeasurement.Attributes.MeasuredValue, "hPa", lambda v: v, False),
    ("illuminance", Clusters.IlluminanceMeasurement.Attributes.MeasuredValue, "lx",
     lambda v: round(10 ** ((v - 1) / 10000), 1) if v else 0, False),
    ("occupancy", Clusters.OccupancySensing.Attributes.Occupancy, None, lambda v: bool(v & OCCUPIED), True),
    ("contact", Clusters.BooleanState.Attributes.StateValue, None, lambda v: bool(v), True),
    # Matter reports battery charge in half percent; z2m publishes plain percent
    ("battery", Clusters.PowerSource.Attributes.BatPercentRemaining, "%", lambda v: v / 2, False),
    ("battery_low", Clusters.PowerSource.Attributes.BatChargeLevel, None, lambda v: int(v) != BAT_OK, True),
]
BATTERY = {"battery", "battery_low"}


def sensor_caps(ep, cl, only=None):
    """Sensor caps for the attributes this endpoint actually reports. A mains-powered device has
    PowerSource without any Bat* attribute, so presence of the cluster alone isn't enough."""
    return [SensorCap(ep, cl, name, attribute, unit, conv, binary)
            for name, attribute, unit, conv, binary in SENSORS
            if (only is None or name in only)
            and attribute.attribute_id in cl.get(attribute.cluster_id, {})]


def endpoint_caps(ep, cl, device_types):
    """-> (kind, actuator caps, sensor caps). Actuators get grouped as a z2m light/switch."""
    act = []
    if ONOFF in cl:
        act.append(OnOffCap(ep, cl))
        if LEVEL in cl:
            act.append(LevelCap(ep, cl))
        if COLOR in cl:
            caps = cl[COLOR].get(COLOR_CAPABILITIES) or 0
            if caps & COLOR_CAP.kColorTemperature:
                act.append(ColorTempCap(ep, cl))
            if caps & (COLOR_CAP.kHueSaturation | COLOR_CAP.kXy):
                act.append(ColorCap(ep, cl))
            act.append(ColorModeEnumCap(ep, cl))
    is_light = LEVEL in cl or COLOR in cl or LIGHT_TYPES & set(device_types)
    sens = sensor_caps(ep, cl)
    if SWITCH in cl:
        sens.append(ActionCap(ep, cl))
    return ("light" if is_light else "switch"), act, sens


