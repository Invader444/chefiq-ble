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
  type 0x0  ASCII friendly name (e.g. "Probe 1")

The temperature payload layout depends on the advertisement protocol version.
Three layouts are supported, selected by version:

  * V3 (verified against a CQ60, protocol version 5.0.0):
        ambient [2:4], food [4:6], tip1 [6:8], tip2 [8:10],
        tip3 [10:12], tip4 [12:14], (ambient mirror) [14:16]
    battery/soc arrive in the separate type 0x3 status packet.
  * V2 (not yet verified on hardware):
        battery [2], soc [3], ambient [4:6], food [6:8],
        tip1 [8:10], tip2 [10:12], tip3 [12:14], (ambient mirror) [14:16]
  * legacy (not yet verified on hardware):
        MAC [2:8], battery [8], soc [9], food [10:12], ambient [12:14],
        (ambient mirror) [14:16]

All 16-bit temperatures are signed, little-endian, divided by 10 (Celsius).
"""

from __future__ import annotations

import logging
import struct
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

    CQ50 = auto()
    CQ60 = auto()


class _Format(Enum):
    """Temperature-payload advertisement format."""

    LEGACY = auto()
    V2 = auto()
    V3 = auto()


# Protocol major-version boundaries for selecting the temperature format.
# Verified: CQ60 reports 5.0.0 -> V3. The V2/legacy boundaries are estimated.
PROTOCOL_V3_MIN_MAJOR = 3
PROTOCOL_V2_MAJOR = 2

# Minimum payload lengths to fully parse each layout.
_INLINE_TEMP_MIN_LEN = 14  # V2/legacy temperature packets
_STATUS_MIN_LEN = 10  # V3 status packet (battery at [8], soc at [9])


def _advert_version(msg: bytes) -> tuple[int, int, int]:
    """Decode the major.minor.patch advertisement protocol version."""
    return (msg[1] >> 4, msg[1] & 0x0F, msg[0] >> 4)


def _packet_type(msg: bytes) -> int:
    """Return the packet type carried in the low nibble of msg[0]."""
    return msg[0] & 0x0F


def _temperature_format(version: tuple[int, int, int]) -> _Format:
    """Select the temperature-payload format from the protocol version.

    Verified anchor: the CQ60 reports version 5.0.0 and uses V3. The V2 and
    legacy boundaries are reconstructed from the protocol and are not yet
    verified against real hardware.
    """
    major = version[0]
    if major >= PROTOCOL_V3_MIN_MAJOR:
        return _Format.V3
    if major == PROTOCOL_V2_MAJOR:
        return _Format.V2
    return _Format.LEGACY


def _signed_temp(msg: bytes, offset: int) -> float:
    """Decode a signed int16 LE at offset as Celsius (value / 10)."""
    raw: int = struct.unpack_from("<h", msg, offset)[0]
    return raw / 10


class ChefIqBluetoothDeviceData(BluetoothData):
    """Data for Chef iQ BLE sensors."""

    def _start_update(self, data: BluetoothServiceInfo) -> None:
        """Update from BLE advertisement data."""
        manufacturer_data = data.manufacturer_data
        if CHEFIQ_MANUFACTURER_ID not in manufacturer_data:
            return

        msg = manufacturer_data[CHEFIQ_MANUFACTURER_ID]
        # Probe packets are short; reject the longer iQ Sense appliance payload.
        if not 2 <= len(msg) <= MAX_PROBE_PAYLOAD_LEN:
            return

        packet_type = _packet_type(msg)
        if packet_type not in (
            PACKET_TYPE_NAME,
            PACKET_TYPE_TEMPERATURE,
            PACKET_TYPE_STATUS,
        ):
            return

        version = _advert_version(msg)
        _LOGGER.debug(
            "Parsing Chef iQ advertisement: type=%s version=%s data=%s",
            packet_type,
            version,
            msg.hex(),
        )

        self.set_device_manufacturer("Chef iQ")
        model = self._model_for(data.name)
        self.set_device_type(model)
        name = f"{model} {short_address(data.address)}"
        self.set_device_name(name)
        self.set_title(name)

        if packet_type == PACKET_TYPE_TEMPERATURE:
            self._update_temperatures(msg, version)
        elif packet_type == PACKET_TYPE_STATUS:
            self._update_status(msg, version)
        # PACKET_TYPE_NAME carries only the ASCII probe name; no sensor data.

    @staticmethod
    def _model_for(advertised_name: str | None) -> str:
        """Map the advertised local name to a probe model label."""
        name = (advertised_name or "").upper()
        for model in (Models.CQ60, Models.CQ50):
            if model.name in name:
                return model.name
        return Models.CQ60.name

    def _update_temperatures(self, msg: bytes, version: tuple[int, int, int]) -> None:
        """Parse a temperature payload (type 0x1) for the matching format."""
        fmt = _temperature_format(version)
        if fmt is _Format.V3:
            self._emit_temps(msg, ambient=2, food=4, tip_start=6, tip_count=4)
        elif fmt is _Format.V2:
            if len(msg) < _INLINE_TEMP_MIN_LEN:
                return
            self._emit_battery(msg[2])
            self._emit_soc(msg[3])
            self._emit_temps(msg, ambient=4, food=6, tip_start=8, tip_count=3)
        else:  # legacy
            if len(msg) < _INLINE_TEMP_MIN_LEN:
                return
            self._emit_battery(msg[8])
            self._emit_soc(msg[9])
            self._emit_temps(msg, ambient=12, food=10, tip_start=None, tip_count=0)

    def _update_status(self, msg: bytes, version: tuple[int, int, int]) -> None:
        """Parse a status payload (type 0x3): MAC, and battery/soc on V3."""
        if _temperature_format(version) is _Format.V3 and len(msg) >= _STATUS_MIN_LEN:
            self._emit_battery(msg[8])
            self._emit_soc(msg[9])
        # The V2 status packet carries only the MAC address (no sensor data).

    def _emit_temps(
        self,
        msg: bytes,
        *,
        ambient: int,
        food: int,
        tip_start: int | None,
        tip_count: int,
    ) -> None:
        """Emit the food, ambient and tip temperatures for one packet."""
        self._emit_temperature(
            ChefIqSensor.AMBIENT_TEMPERATURE, msg, ambient, "Ambient temperature"
        )
        self._emit_temperature(
            ChefIqSensor.FOOD_TEMPERATURE, msg, food, "Food temperature"
        )
        if tip_start is None:
            return
        for index in range(tip_count):
            offset = tip_start + index * 2
            if offset + 2 > len(msg):
                break
            self._emit_temperature(
                _TIP_SENSORS[index],
                msg,
                offset,
                f"Probe tip {index + 1} temperature",
            )

    def _emit_temperature(
        self, sensor: ChefIqSensor, msg: bytes, offset: int, friendly_name: str
    ) -> None:
        """Emit one temperature sensor if it is within the plausible range."""
        if offset + 2 > len(msg):
            return
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
