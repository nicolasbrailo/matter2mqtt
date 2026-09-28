"""What Matter calls things: ids, names, and raw attribute values.

Pure lookup, no behaviour. The ids come from the data model generated from the Matter spec
(chip.clusters), so a typo here is an AttributeError at import instead of a silent None at
runtime. Only the device type names are hand-written -- those live in the spec's device
library, which chip.clusters doesn't generate.
"""
import dataclasses

from chip.clusters import ClusterObjects
from chip.clusters import Objects as Clusters
from chip.clusters.Types import Nullable

# Cluster / attribute / bitmap ids, from the data model generated from the Matter spec
# (chip.clusters), so a typo is an AttributeError at import instead of a silent None.
DESCRIPTOR = Clusters.Descriptor.id
BASIC_INFO = Clusters.BasicInformation.id
ONOFF = Clusters.OnOff.id
LEVEL = Clusters.LevelControl.id
COLOR = Clusters.ColorControl.id
THREAD_DIAG = Clusters.ThreadNetworkDiagnostics.id
WIFI_DIAG = Clusters.WiFiNetworkDiagnostics.id
ETHERNET_DIAG = Clusters.EthernetNetworkDiagnostics.id
OTA_REQUESTOR = Clusters.OtaSoftwareUpdateRequestor.id
POWER_SOURCE = Clusters.PowerSource.id
SWITCH = Clusters.Switch.id

DEVICE_TYPE_LIST = Clusters.Descriptor.Attributes.DeviceTypeList.attribute_id
NODE_LABEL = Clusters.BasicInformation.Attributes.NodeLabel.attribute_id
UNIQUE_ID = Clusters.BasicInformation.Attributes.UniqueID.attribute_id
ON_OFF = Clusters.OnOff.Attributes.OnOff.attribute_id
CURRENT_LEVEL = Clusters.LevelControl.Attributes.CurrentLevel.attribute_id
MIN_LEVEL = Clusters.LevelControl.Attributes.MinLevel.attribute_id
MAX_LEVEL = Clusters.LevelControl.Attributes.MaxLevel.attribute_id
COLOR_CAPABILITIES = Clusters.ColorControl.Attributes.ColorCapabilities.attribute_id
COLOR_TEMP_MIREDS = Clusters.ColorControl.Attributes.ColorTemperatureMireds.attribute_id
COLOR_TEMP_MIN = Clusters.ColorControl.Attributes.ColorTempPhysicalMinMireds.attribute_id
COLOR_TEMP_MAX = Clusters.ColorControl.Attributes.ColorTempPhysicalMaxMireds.attribute_id
CURRENT_HUE = Clusters.ColorControl.Attributes.CurrentHue.attribute_id
CURRENT_SATURATION = Clusters.ColorControl.Attributes.CurrentSaturation.attribute_id
CURRENT_X = Clusters.ColorControl.Attributes.CurrentX.attribute_id
CURRENT_Y = Clusters.ColorControl.Attributes.CurrentY.attribute_id
COLOR_MODE = Clusters.ColorControl.Attributes.ColorMode.attribute_id

COLOR_CAP = Clusters.ColorControl.Bitmaps.ColorCapabilitiesBitmap
NUMBER_OF_POSITIONS = Clusters.Switch.Attributes.NumberOfPositions.attribute_id
MULTI_PRESS_MAX = Clusters.Switch.Attributes.MultiPressMax.attribute_id
FEATURE_MAP = Clusters.Switch.Attributes.FeatureMap.attribute_id
SWITCH_EVENTS = Clusters.Switch.Events
SWITCH_FEATURE = Clusters.Switch.Bitmaps.Feature

BAT_OK = Clusters.PowerSource.Enums.BatChargeLevelEnum.kOk
COLOR_MODE_ENUM = Clusters.ColorControl.Enums.ColorModeEnum
# z2m names for Matter's ColorMode
COLOR_MODES = {int(COLOR_MODE_ENUM.kCurrentHueAndCurrentSaturation): "hs",
               int(COLOR_MODE_ENUM.kCurrentXAndCurrentY): "xy",
               int(COLOR_MODE_ENUM.kColorTemperatureMireds): "color_temp"}
OCCUPIED = Clusters.OccupancySensing.Bitmaps.OccupancyBitmap.kOccupied

# Device type ids live in the spec's device library, which chip.clusters doesn't generate,
# so these stay hand-written.
DEVICE_TYPES = {
    14: "Aggregator",
    17: "Power Source",
    19: "Bridged Node",
    21: "Contact Sensor",
    22: "Root Node",
    256: "On/Off Light",
    257: "Dimmable Light",
    259: "On/Off Light Switch",
    263: "Occupancy Sensor",
    266: "On/Off Plug-in Unit",
    267: "Dimmable Plug-in Unit",
    268: "Color Temperature Light",
    269: "Extended Color Light",
    770: "Temperature Sensor",
    773: "Pressure Sensor",
    775: "Humidity Sensor",
}
LIGHT_TYPES = {256, 257, 268, 269}

# BasicInformation attribute -> output key
BASIC_INFO_FIELDS = {
    Clusters.BasicInformation.Attributes.VendorName.attribute_id: "manufacturer",
    Clusters.BasicInformation.Attributes.VendorID.attribute_id: "vendor_id",
    Clusters.BasicInformation.Attributes.ProductName.attribute_id: "model",
    Clusters.BasicInformation.Attributes.ProductID.attribute_id: "product_id",
    Clusters.BasicInformation.Attributes.HardwareVersionString.attribute_id: "hardware_version",
    Clusters.BasicInformation.Attributes.SoftwareVersionString.attribute_id: "software_version",
    Clusters.BasicInformation.Attributes.SerialNumber.attribute_id: "serial_number",
    UNIQUE_ID: "unique_id",
}


def cluster_name(cid):
    cls = ClusterObjects.ALL_CLUSTERS.get(cid)
    return cls.__name__ if cls is not None else f"cluster_{cid}"


def attr_name(cid, aid):
    cls = ClusterObjects.ALL_ATTRIBUTES.get(cid, {}).get(aid)
    return cls.__name__ if cls is not None else f"attr_{aid}"


def device_type_name(dt):
    return DEVICE_TYPES.get(dt, f"0x{dt:04x}")


def struct_tag(struct_cls, label):
    """Field tag of a struct field, from the generated descriptor."""
    return next(f.Tag for f in struct_cls.descriptor.Fields if f.Label == label)


DEVICE_TYPE_FIELD = struct_tag(Clusters.Descriptor.Structs.DeviceTypeStruct, "deviceType")


def struct_field(s, tag, name):
    """matter-server hands structs over keyed by field tag (as str); accept names too."""
    if not isinstance(s, dict):
        return None
    for k in (str(tag), tag, name):
        if k in s:
            return s[k]
    return None


NODE_LABEL_PATH = f"0/{BASIC_INFO}/{NODE_LABEL}"


def live_value(node, ep, cid, aid, default=None):
    """The current value of one attribute, from the typed cluster cache.

    node_data.attributes is only the dump taken at interview time: writes and subscription
    updates land in the typed cluster objects instead, so anything that can change at runtime
    (a NodeLabel someone renamed, every value a Cap reads) has to come from here.
    """
    try:
        v = node.get_attribute_value(ep, cid, aid)
    except KeyError:
        return default  # endpoint or cluster the node doesn't have
    return default if v is None or isinstance(v, Nullable) else v


def attr_tree(attrs):
    """Flat {"ep/cid/aid": val} -> {ep: {cid: {aid: val}}}"""
    tree = {}
    for key, val in attrs.items():
        try:
            ep, cid, aid = (int(x) for x in key.split("/"))
        except ValueError:
            continue
        tree.setdefault(ep, {}).setdefault(cid, {})[aid] = val
    return tree


def json_safe(v):
    """Attribute values as plain json: structs become objects, enums/bitmaps plain ints,
    octet strings hex. Without this a struct would land in mqtt as its python repr."""
    if v is None or isinstance(v, Nullable):
        return None
    if dataclasses.is_dataclass(v):
        return {f.name: json_safe(getattr(v, f.name)) for f in dataclasses.fields(v)}
    if isinstance(v, (list, tuple)):
        return [json_safe(x) for x in v]
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    if isinstance(v, bool):
        return v
    if isinstance(v, int):  # IntEnum / IntFlag included
        return int(v)
    return v


def dump_node(node):
    """Every attribute the node has, named: {endpoint: {cluster: {attribute: value}}}.

    This is matter-discover.py's output as data, including the clusters the bridge has no
    capability for. Values come from the live cache, names from the generated data model.
    """
    out = {}
    for ep, clusters in sorted(attr_tree(node.node_data.attributes).items()):
        eout = {}
        for cid, attrs in sorted(clusters.items()):
            vals = {}
            for aid in sorted(attrs):
                try:
                    v = node.get_attribute_value(ep, cid, aid)
                except KeyError:
                    v = attrs[aid]  # not in the typed cache (unknown cluster): initial dump value
                vals[attr_name(cid, aid)] = json_safe(v)
            eout[cluster_name(cid)] = vals
        out[str(ep)] = eout
    return out

