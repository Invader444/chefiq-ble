"""Parser for Chef iQ BLE advertisements.

Decodes the manufacturer-specific advertisement data (company id 0x05CD) that
Chef iQ probes broadcast, for interoperability with Home Assistant. The wire
format was determined by observing the probe's BLE broadcasts and verified
against a live CQ60; it builds on the earlier Chef iQ work in the ``ble_monitor``
project (https://github.com/custom-components/ble_monitor). The byte layouts
documented below describe the radio broadcast itself.

A Chef iQ probe advertises three rotating packet types. The low nibble of
``msg[0]`` selects the packet type; the advertisement protocol version is
encoded in the remaining nibbles of ``msg[0:2]`` as ``major.minor.patch``:

  type 0x1  temperature payload
  type 0x3  status payload (MAC address, and battery/soc on newer protocols)
  type 0x0  friendly name (e.g. "Probe 1"), 12 bytes at [2:14], NUL padded

The temperature payload layout is selected by the protocol major version:

  * V3, major 5 (verified against a CQ60, protocol version 5.0.0):
        ambient [2:4], food [4:6], tip1 [6:8], tip2 [8:10],
        tip3 [10:12], tip4 [12:14], (not decoded) [14:16]
    battery/soc arrive in the separate type 0x3 status packet.
  * V2, major 4 (not yet verified on hardware):
        battery [2], soc [3], ambient [4:6], food [6:8],
        tip1 [8:10], tip2 [10:12], tip3 [12:14], (not decoded) [14:16]
    the status packet carries only the MAC address.
  * legacy, majors 2 and 3 (not yet verified on hardware):
        MAC [2:8], battery [8] (major 3 only), soc [9], food [10:12],
        ambient [12:14], (not decoded) [14:16]
    legacy probes have no status packet.

Any other major version is not a known probe protocol, and its packets are
ignored.

All 16-bit temperatures are signed, little-endian, divided by 10 (Celsius).

Identifying a probe
-------------------

The manufacturer id alone does not identify a probe. Some vendors put a sensor
reading where the Bluetooth company id belongs (an Inkbird IBS-TH broadcasts
its temperature x100 there), so any 16-bit value, 0x05CD included, can turn up
as the "manufacturer id" of an unrelated device. An advertisement is therefore
only accepted, and ``supported()`` only reports a probe, once its protocol
major version is a known one and its payload matches the documented layout for
its packet type:

  * a name packet carries a 12-byte, NUL-padded, printable name at [2:14], or
    the device's local name is a known probe model (probes advertise their
    model, e.g. "CQ60", as the BLE local name);
  * a temperature packet carries every reading of its layout, each either a
    plausible Celsius value or the firmware's "no reading" sentinel;
  * a status packet (and a legacy temperature packet) carries the probe's own
    MAC address at [2:8], which must equal the advertising address.

Any single real packet passes, so discovery works whichever of the three
packet types Home Assistant happens to see first.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, replace
from enum import Enum, auto

from bluetooth_data_tools import short_address
from bluetooth_sensor_state_data import BluetoothData
from home_assistant_bluetooth import BluetoothServiceInfo
from sensor_state_data import SensorDeviceClass, Units
from sensor_state_data.enum import StrEnum

_LOGGER = logging.getLogger(__name__)

CHEFIQ_MANUFACTURER_ID = 0x05CD

# Packet types (low nibble of msg[0]).
PACKET_TYPE_NAME = 0x0
PACKET_TYPE_TEMPERATURE = 0x1
PACKET_TYPE_STATUS = 0x3

# Probe advertisements are at most 18 bytes. The iQ Sense base/hub advertises
# under the same manufacturer id but with a longer (>= 20 byte) appliance
# payload, so a length ceiling cleanly excludes it.
MAX_PROBE_PAYLOAD_LEN = 18

# Plausible Celsius window. A failed or disconnected sensor reports a large
# out-of-range sentinel value; any reading outside this window is dropped
# rather than surfaced as a bogus value.
TEMP_MIN_C = -40.0
TEMP_MAX_C = 600.0

# A ring with nothing to read (probe not inserted, tip disconnected) reports a
# large raw int16: any signed value at or above this threshold is the
# firmware's "no reading" sentinel rather than a temperature; 0x7FFF has been
# observed on a CQ60. The sentinel counts as a well-formed field when
# identifying a packet, but it is never emitted as a temperature.
TEMP_SENTINEL_MIN_RAW = 32000


class ChefIqSensor(StrEnum):
    """Sensors exposed by a Chef iQ probe."""

    FOOD_TEMPERATURE = "food_temperature"
    AMBIENT_TEMPERATURE = "ambient_temperature"
    PROBE_TIP_1_TEMPERATURE = "probe_tip_1_temperature"
    PROBE_TIP_2_TEMPERATURE = "probe_tip_2_temperature"
    PROBE_TIP_3_TEMPERATURE = "probe_tip_3_temperature"
    PROBE_TIP_4_TEMPERATURE = "probe_tip_4_temperature"
    SOC_TEMPERATURE = "soc_temperature"
    SIGNAL_STRENGTH = "signal_strength"
    BATTERY_PERCENT = "battery_percent"


_TIP_SENSORS = (
    ChefIqSensor.PROBE_TIP_1_TEMPERATURE,
    ChefIqSensor.PROBE_TIP_2_TEMPERATURE,
    ChefIqSensor.PROBE_TIP_3_TEMPERATURE,
    ChefIqSensor.PROBE_TIP_4_TEMPERATURE,
)


class Models(Enum):
    """Known Chef iQ probe models."""

    CQ60 = auto()


@dataclass(frozen=True, slots=True)
class _TemperatureLayout:
    """Byte offsets of the fields in a temperature packet (type 0x1).

    Used both to decide whether a payload *is* a temperature packet and to
    decode it, so the two can never disagree.
    """

    ambient: int
    food: int
    tips: tuple[int, ...] = ()
    battery: int | None = None
    soc: int | None = None
    has_mac: bool = False  # probe's own MAC address at [2:8]

    @property
    def temperatures(self) -> tuple[int, ...]:
        """Offsets of every signed int16 temperature field."""
        return (self.ambient, self.food, *self.tips)

    @property
    def min_len(self) -> int:
        """Shortest payload that holds every field of this layout."""
        return max(offset + 2 for offset in self.temperatures)


# Temperature payload layout per protocol major version: legacy for 2.x and
# 3.x (battery only from 3.x), V2 for 4.x, V3 for 5.x. Any other major is not
# a known probe protocol.
_LEGACY_LAYOUT = _TemperatureLayout(ambient=12, food=10, soc=9, has_mac=True)
_TEMPERATURE_LAYOUTS: dict[int, _TemperatureLayout] = {
    2: _LEGACY_LAYOUT,
    3: replace(_LEGACY_LAYOUT, battery=8),
    4: _TemperatureLayout(ambient=4, food=6, tips=(8, 10, 12), battery=2, soc=3),
    5: _TemperatureLayout(ambient=2, food=4, tips=(6, 8, 10, 12)),
}

# Status packets (type 0x3) exist from major 4 and carry the probe's own MAC
# at [2:8]; major 5 adds battery at [8] and soc at [9]. Legacy probes put the
# MAC and battery in the temperature packet instead.
STATUS_MIN_MAJOR = 4
STATUS_SENSORS_MIN_MAJOR = 5
_MAC_SLICE = slice(2, 8)
_STATUS_BATTERY = 8
_STATUS_SOC = 9
_STATUS_MIN_LEN = _MAC_SLICE.stop
_STATUS_SENSORS_MIN_LEN = _STATUS_SOC + 1

# Name packet (type 0x0): 12-byte NUL-padded name at [2:14].
_NAME_SLICE = slice(2, 14)
_NAME_MIN_LEN = _NAME_SLICE.stop

_INT16 = struct.Struct("<h")


def _advert_version(msg: bytes) -> tuple[int, int, int]:
    """Decode the major.minor.patch advertisement protocol version."""
    return (msg[1] >> 4, msg[1] & 0x0F, msg[0] >> 4)


def _packet_type(msg: bytes) -> int:
    """Return the packet type carried in the low nibble of msg[0]."""
    return msg[0] & 0x0F


def _raw_temp(msg: bytes, offset: int) -> int:
    """Read the signed int16 LE temperature field at offset (tenths of a degree)."""
    raw: int = _INT16.unpack_from(msg, offset)[0]
    return raw


def _signed_temp(msg: bytes, offset: int) -> float:
    """Decode a signed int16 LE at offset as Celsius (value / 10)."""
    return _raw_temp(msg, offset) / 10


def _known_model(advertised_name: str | None) -> str | None:
    """Return the probe model named in the advertised local name, if any."""
    name = (advertised_name or "").upper()
    for model in Models:
        if model.name in name:
            return model.name
    return None


def _address_bytes(address: str) -> bytes | None:
    """Return the 6-byte MAC behind a colon-separated address, or None.

    Linux adapters and Bluetooth proxies report MAC addresses. CoreBluetooth
    (macOS) reports opaque UUIDs instead, which cannot be compared with the
    MAC a probe embeds in its packets.
    """
    try:
        raw = bytes.fromhex(address.replace(":", ""))
    except ValueError:
        return None
    return raw if len(raw) == 6 else None


def _carries_own_address(msg: bytes, address: str) -> bool:
    """Return True if msg[2:8] is the advertiser's MAC (or it cannot be checked)."""
    mac = _address_bytes(address)
    return mac is None or msg[_MAC_SLICE] == mac


def _is_temperature_field(msg: bytes, offset: int) -> bool:
    """Return True if the int16 at offset is a plausible reading or the sentinel."""
    raw = _raw_temp(msg, offset)
    return raw >= TEMP_SENTINEL_MIN_RAW or TEMP_MIN_C <= raw / 10 <= TEMP_MAX_C


def _is_probe_name(field: bytes) -> bool:
    """Return True if field is a NUL-padded, printable (UTF-8) probe name."""
    try:
        return field.rstrip(b"\x00").decode().isprintable()
    except UnicodeDecodeError:
        return False


def _matches_probe_layout(
    msg: bytes, packet_type: int, major: int, address: str, name: str | None
) -> bool:
    """Return True if msg matches the documented layout for its packet type.

    This is what separates a probe from an unrelated device whose manufacturer
    data merely starts with 0x05CD (see "Identifying a probe" in the module
    docstring). Every field the layout defines must be present, so decoding
    an accepted packet never reads past the end of the payload.
    """
    if major not in _TEMPERATURE_LAYOUTS:
        return False
    if packet_type == PACKET_TYPE_NAME:
        return _known_model(name) is not None or (
            len(msg) >= _NAME_MIN_LEN and _is_probe_name(msg[_NAME_SLICE])
        )
    if packet_type == PACKET_TYPE_TEMPERATURE:
        layout = _TEMPERATURE_LAYOUTS[major]
        return (
            len(msg) >= layout.min_len
            and (not layout.has_mac or _carries_own_address(msg, address))
            and all(_is_temperature_field(msg, off) for off in layout.temperatures)
        )
    if packet_type == PACKET_TYPE_STATUS and major >= STATUS_MIN_MAJOR:
        min_len = (
            _STATUS_SENSORS_MIN_LEN
            if major >= STATUS_SENSORS_MIN_MAJOR
            else _STATUS_MIN_LEN
        )
        return len(msg) >= min_len and _carries_own_address(msg, address)
    return False


class ChefIqBluetoothDeviceData(BluetoothData):
    """Data for Chef iQ BLE sensors."""

    def _start_update(self, data: BluetoothServiceInfo) -> None:
        """Update from BLE advertisement data.

        Device info is only recorded, and so ``supported()`` only returns
        True, once the payload has been identified as a probe packet.
        """
        manufacturer_data = data.manufacturer_data
        if CHEFIQ_MANUFACTURER_ID not in manufacturer_data:
            return

        msg = manufacturer_data[CHEFIQ_MANUFACTURER_ID]
        # Probe packets are short; reject the longer iQ Sense appliance payload.
        if not 2 <= len(msg) <= MAX_PROBE_PAYLOAD_LEN:
            return

        packet_type = _packet_type(msg)
        version = _advert_version(msg)
        major = version[0]
        if not _matches_probe_layout(msg, packet_type, major, data.address, data.name):
            _LOGGER.debug(
                "Ignoring advertisement from %s (%r): manufacturer data %s "
                "(type=%s version=%s) does not match a Chef iQ probe packet",
                data.address,
                data.name,
                msg.hex(),
                packet_type,
                version,
            )
            return

        _LOGGER.debug(
            "Parsing Chef iQ advertisement: type=%s version=%s data=%s",
            packet_type,
            version,
            msg.hex(),
        )

        self.set_device_manufacturer("Chef iQ")
        model = _known_model(data.name) or Models.CQ60.name
        self.set_device_type(model)
        name = f"{model} {short_address(data.address)}"
        self.set_device_name(name)
        self.set_title(name)

        if packet_type == PACKET_TYPE_TEMPERATURE:
            self._update_temperatures(msg, _TEMPERATURE_LAYOUTS[major])
        elif packet_type == PACKET_TYPE_STATUS:
            self._update_status(msg, major)
        # PACKET_TYPE_NAME carries only the probe name; no sensor data.

    def _update_temperatures(self, msg: bytes, layout: _TemperatureLayout) -> None:
        """Parse a temperature payload (type 0x1) with the given layout."""
        if layout.battery is not None:
            self._emit_battery(msg[layout.battery])
        if layout.soc is not None:
            self._emit_soc(msg[layout.soc])
        self._emit_temperature(
            ChefIqSensor.AMBIENT_TEMPERATURE, msg, layout.ambient, "Ambient temperature"
        )
        self._emit_temperature(
            ChefIqSensor.FOOD_TEMPERATURE, msg, layout.food, "Food temperature"
        )
        for index, (sensor, offset) in enumerate(
            zip(_TIP_SENSORS, layout.tips, strict=False), start=1
        ):
            self._emit_temperature(
                sensor, msg, offset, f"Probe tip {index} temperature"
            )

    def _update_status(self, msg: bytes, major: int) -> None:
        """Parse a status payload (type 0x3): MAC, and battery/soc from major 5."""
        if major >= STATUS_SENSORS_MIN_MAJOR:
            self._emit_battery(msg[_STATUS_BATTERY])
            self._emit_soc(msg[_STATUS_SOC])
        # Older status packets carry only the MAC address (no sensor data).

    def _emit_temperature(
        self, sensor: ChefIqSensor, msg: bytes, offset: int, friendly_name: str
    ) -> None:
        """Emit one temperature sensor unless it holds the no-reading sentinel."""
        value = _signed_temp(msg, offset)
        if TEMP_MIN_C <= value <= TEMP_MAX_C:
            self.update_sensor(
                str(sensor),
                Units.TEMP_CELSIUS,
                value,
                SensorDeviceClass.TEMPERATURE,
                friendly_name,
            )

    def _emit_battery(self, percent: int) -> None:
        """Emit the battery sensor."""
        self.update_sensor(
            str(ChefIqSensor.BATTERY_PERCENT),
            Units.PERCENTAGE,
            percent,
            SensorDeviceClass.BATTERY,
            "Battery",
        )

    def _emit_soc(self, celsius: int) -> None:
        """Emit the system-on-chip temperature (raw whole-degree byte)."""
        if TEMP_MIN_C <= celsius <= TEMP_MAX_C:
            self.update_sensor(
                str(ChefIqSensor.SOC_TEMPERATURE),
                Units.TEMP_CELSIUS,
                celsius,
                SensorDeviceClass.TEMPERATURE,
                "SoC temperature",
            )
