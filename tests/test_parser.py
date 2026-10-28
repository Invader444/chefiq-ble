"""Tests for the Chef iQ BLE advertisement parser.

V3 fixtures are real advertisements captured from a CQ60 (firmware 5.0.0).
V2 and legacy fixtures are synthetic, constructed from the byte layout
for those advertisement formats, and are marked as such.
"""

from __future__ import annotations

import struct

import pytest
from home_assistant_bluetooth import BluetoothServiceInfo

from chefiq_ble import ChefIqBluetoothDeviceData

CHEFIQ = 0x05CD
ADDRESS = "C9:14:65:CA:07:9A"

# Real CQ60 captures (protocol 5.0.0). The status packet carries the probe's
# own MAC address (c9:14:65:ca:07:9a) at [2:8].
V3_TEMPERATURE = "015024012b0135012e012b0130012401315a"
V3_STATUS = "0350c91465ca079a64201e010301007ac4"
V3_NAME = "005050726f6265203100000000000a2b"  # "Probe 1"

# Synthetic: header 01 40 -> type 1, version 4.0.0 -> V2
# battery=0x55, soc=0x1e, ambient=25.0, food=60.0, tip1=40.0, tip2=41.0, tip3=42.0
V2_TEMPERATURE = "0140551efa00580290019a01a401fa00"

# Synthetic: header 01 30 -> type 1, version 3.0.0 -> legacy (with battery)
# MAC c9:14:65:ca:07:9a, battery=0x5a, soc=0x21, food=55.0, ambient=22.0
LEGACY_TEMPERATURE = "0130c91465ca079a5a212602dc00dc00"


def _service_info(
    payload: str, *, name: str = "CQ60", address: str = ADDRESS
) -> BluetoothServiceInfo:
    return BluetoothServiceInfo(
        name=name,
        address=address,
        rssi=-60,
        manufacturer_data={CHEFIQ: bytes.fromhex(payload)},
        service_data={},
        service_uuids=[],
        source="local",
    )


def _values(
    device: ChefIqBluetoothDeviceData, payload: str, **kw: str
) -> dict[str, object]:
    update = device.update(_service_info(payload, **kw))
    return {key.key: value.native_value for key, value in update.entity_values.items()}


def _supported(payload: str, **kw: str) -> bool:
    return ChefIqBluetoothDeviceData().supported(_service_info(payload, **kw))


def _name_packet(name_field_hex: str) -> str:
    """Build a V3 name packet around a 12-byte name field."""
    assert len(name_field_hex) == 24
    return "0050" + name_field_hex + "0a2b"


def _inkbird_ibs_th(temp_c: float, humidity: float) -> BluetoothServiceInfo:
    """Build an Inkbird IBS-TH ("sps") advertisement as Home Assistant sees it.

    The sensor has no company id: its manufacturer data starts with the
    temperature (int16, x100) followed by the humidity (uint16, x100), so
    Home Assistant files the remaining seven bytes under whatever
    "manufacturer id" the current temperature spells. The trailing bytes are
    the capture from home-assistant/core#188557.
    """
    raw = struct.pack("<hH", round(temp_c * 100), round(humidity * 100))
    raw += bytes.fromhex("0003b94008")
    return BluetoothServiceInfo(
        name="sps",
        address="49:42:08:00:3F:7C",
        rssi=-64,
        manufacturer_data={int.from_bytes(raw[:2], "little"): raw[2:]},
        service_data={},
        service_uuids=["0000fff0-0000-1000-8000-00805f9b34fb"],
        source="local",
    )


# --- V3 (real CQ60 captures) ------------------------------------------------


def test_v3_temperature_packet_real_capture() -> None:
    device = ChefIqBluetoothDeviceData()
    values = _values(device, V3_TEMPERATURE)
    assert values["ambient_temperature"] == 29.2
    assert values["food_temperature"] == 29.9
    assert values["probe_tip_1_temperature"] == 30.9
    assert values["probe_tip_2_temperature"] == 30.2
    assert values["probe_tip_3_temperature"] == 29.9
    assert values["probe_tip_4_temperature"] == 30.4
    # battery/soc are not in the temperature packet on V3
    assert "battery_percent" not in values


def test_v3_status_packet_real_capture() -> None:
    device = ChefIqBluetoothDeviceData()
    values = _values(device, V3_STATUS)
    assert values["battery_percent"] == 100
    assert values["soc_temperature"] == 32


def test_v3_name_packet_emits_no_sensors() -> None:
    device = ChefIqBluetoothDeviceData()
    update = device.update(_service_info(V3_NAME))
    assert {k.key for k in update.entity_values} == {"signal_strength"}


def test_v3_accumulates_across_packet_types() -> None:
    device = ChefIqBluetoothDeviceData()
    _values(device, V3_TEMPERATURE)
    values = _values(device, V3_STATUS)
    # temperatures persist while the status packet adds battery/soc
    assert values["food_temperature"] == 29.9
    assert values["battery_percent"] == 100
    assert values["soc_temperature"] == 32


# --- V2, protocol major 4 (synthetic) ---------------------------------------


def test_v2_temperature_packet_synthetic() -> None:
    device = ChefIqBluetoothDeviceData()
    values = _values(device, V2_TEMPERATURE)
    assert values["battery_percent"] == 85
    assert values["soc_temperature"] == 30
    assert values["ambient_temperature"] == 25.0
    assert values["food_temperature"] == 60.0
    assert values["probe_tip_1_temperature"] == 40.0
    assert values["probe_tip_2_temperature"] == 41.0
    assert values["probe_tip_3_temperature"] == 42.0
    # V2 has only 3 tips
    assert "probe_tip_4_temperature" not in values


def test_v2_status_packet_carries_only_the_mac() -> None:
    # header 03 40 -> type 3, version 4.0.0; MAC only, no battery/soc
    payload = "0340c91465ca079a"
    assert _supported(payload)
    values = _values(ChefIqBluetoothDeviceData(), payload)
    assert "battery_percent" not in values
    assert "soc_temperature" not in values


# --- legacy, protocol majors 2 and 3 (synthetic) ----------------------------


def test_legacy_temperature_packet_synthetic() -> None:
    device = ChefIqBluetoothDeviceData()
    values = _values(device, LEGACY_TEMPERATURE)
    assert values["battery_percent"] == 90
    assert values["soc_temperature"] == 33
    assert values["food_temperature"] == 55.0
    assert values["ambient_temperature"] == 22.0
    # legacy has no tip sensors
    assert "probe_tip_1_temperature" not in values


def test_legacy_major_2_has_no_battery() -> None:
    # header 01 20 -> version 2.0.0: same layout, battery byte not yet defined
    payload = "0120" + LEGACY_TEMPERATURE[4:]
    values = _values(ChefIqBluetoothDeviceData(), payload)
    assert "battery_percent" not in values
    assert values["soc_temperature"] == 33
    assert values["food_temperature"] == 55.0


def test_legacy_has_no_status_packet() -> None:
    # 2.x/3.x firmware has no status packet: MAC and battery ride in the
    # temperature packet.
    assert not _supported("0330" + V3_STATUS[4:])


# --- identification ---------------------------------------------------------


@pytest.mark.parametrize("payload", [V3_TEMPERATURE, V3_STATUS, V3_NAME])
@pytest.mark.parametrize("name", ["CQ60", ""])
def test_every_real_packet_type_is_supported(payload: str, name: str) -> None:
    # Home Assistant runs discovery on whichever of the three rotating packets
    # it sees first and does not retry for the same address, so each one must
    # identify the probe on its own, with or without the local name present.
    assert _supported(payload, name=name)


@pytest.mark.parametrize("major", [0, 1, 6, 15])
def test_unknown_protocol_major_is_rejected(major: int) -> None:
    # Only majors 2-5 are known probe protocols; the version lives in the high
    # nibble of byte 1.
    assert not _supported(f"01{major:x}0" + V3_TEMPERATURE[4:])
    assert not _supported(f"03{major:x}0" + V3_STATUS[4:])
    assert not _supported(f"00{major:x}0" + V3_NAME[4:], name="")


def test_inkbird_ibs_th_at_14_85_c_is_not_a_probe() -> None:
    # home-assistant/core#188557: at 14.85 C the Inkbird's temperature field
    # reads 0x05CD, so HA offers the sensor to the Chef iQ integration.
    service_info = _inkbird_ibs_th(14.85, 66.72)
    assert service_info.manufacturer_data == {CHEFIQ: bytes.fromhex("101a0003b94008")}
    assert not ChefIqBluetoothDeviceData().supported(service_info)
    assert not ChefIqBluetoothDeviceData().update(service_info).entity_values


def test_inkbird_ibs_th_is_never_a_probe_at_any_humidity() -> None:
    # The humidity byte lands where the packet type nibble lives, so over a
    # humidity sweep the payload is offered as every packet type in turn.
    for centi_percent in range(0, 10001):
        service_info = _inkbird_ibs_th(14.85, centi_percent / 100)
        assert not ChefIqBluetoothDeviceData().supported(service_info)


def test_status_packet_must_carry_own_mac() -> None:
    service_info = _service_info(V3_STATUS, address="AA:BB:CC:DD:EE:FF")
    assert not ChefIqBluetoothDeviceData().supported(service_info)
    assert not ChefIqBluetoothDeviceData().update(service_info).entity_values


def test_status_packet_accepted_when_address_is_not_a_mac() -> None:
    # CoreBluetooth (macOS) reports UUIDs, not MACs; the MAC check is skipped.
    uuid = "6F4C2B1A-7E3D-4F00-8000-0123456789AB"
    assert _supported(V3_STATUS, address=uuid)
    values = _values(ChefIqBluetoothDeviceData(), V3_STATUS, address=uuid)
    assert values["battery_percent"] == 100


def test_legacy_temperature_packet_must_carry_own_mac() -> None:
    assert not _supported(LEGACY_TEMPERATURE, address="AA:BB:CC:DD:EE:FF")


@pytest.mark.parametrize(
    ("name_field", "accepted"),
    [
        ("50726f626520310000000000", True),  # "Probe 1", NUL padded
        ("4272c3b67463686e65000000", True),  # "Brötchen" (UTF-8)
        ("000000000000000000000000", True),  # unnamed probe
        ("50726f626500310000000000", False),  # NUL inside the name
        ("0003b9400801020304050607", False),  # binary, not UTF-8
        ("0150726f6265203100000000", False),  # control character
    ],
)
def test_name_packet_must_hold_a_printable_name(
    name_field: str, accepted: bool
) -> None:
    # No local name here, so only the payload can identify the probe.
    assert _supported(_name_packet(name_field), name="") is accepted


def test_name_packet_accepted_on_known_local_name() -> None:
    # Probes advertise their model as the BLE local name, so a name packet
    # from a device called "CQ60" is a probe whatever the (unverified) name
    # field holds.
    garbage = _name_packet("0003b9400801020304050607")
    assert _supported(garbage, name="CQ60")
    assert not _supported(garbage, name="sps")


def test_name_packet_too_short_is_rejected() -> None:
    # header + 11 bytes: the 12-byte name field is incomplete
    assert not _supported("0050" + "50726f62652031" + "00000000", name="")


@pytest.mark.parametrize(
    "tip4",
    [
        "204e",  # 2000.0 C
        "0cfe",  # -50.0 C
        "ff7c",  # 3199.9 C: just below the "no reading" sentinel range
    ],
)
def test_temperature_packet_with_implausible_reading_is_rejected(tip4: str) -> None:
    payload = V3_TEMPERATURE[:24] + tip4 + V3_TEMPERATURE[28:]
    assert not _supported(payload)
    assert not ChefIqBluetoothDeviceData().update(_service_info(payload)).entity_values


@pytest.mark.parametrize(
    "ambient",
    [
        "ff7f",  # 0x7fff, observed on a CQ60
        "007d",  # 32000, the lowest "no reading" sentinel value
    ],
)
def test_sentinel_reading_is_accepted_but_not_emitted(ambient: str) -> None:
    # The packet still identifies a probe; the field is dropped, the rest
    # parse fine.
    payload = V3_TEMPERATURE[:4] + ambient + V3_TEMPERATURE[8:]
    assert _supported(payload)
    values = _values(ChefIqBluetoothDeviceData(), payload)
    assert "ambient_temperature" not in values
    assert values["food_temperature"] == 29.9


def test_truncated_temperature_packet_is_rejected() -> None:
    # header + ambient/food/tip1/tip2/tip3: tip4 is missing
    assert not _supported(V3_TEMPERATURE[:24])


def test_v2_temperature_packet_with_implausible_reading_is_rejected() -> None:
    # food slot [6:8] = 0x4e20 -> 2000.0 C
    payload = V2_TEMPERATURE[:12] + "204e" + V2_TEMPERATURE[16:]
    assert not _supported(payload)


# --- guards / robustness ----------------------------------------------------


def test_iq_sense_hub_is_rejected() -> None:
    # Real iQ Sense 540 appliance advert (21 bytes) under the same manufacturer.
    payload = "50754e427588e5133f8d4bb3a403113f437a871e05"
    device = ChefIqBluetoothDeviceData()
    update = device.update(_service_info(payload, name="iQ Sense 540"))
    assert not _supported(payload, name="iQ Sense 540")
    assert not update.entity_values


def test_other_manufacturer_ignored() -> None:
    device = ChefIqBluetoothDeviceData()
    si = BluetoothServiceInfo(
        name="something",
        address="00:11:22:33:44:55",
        rssi=-50,
        manufacturer_data={0x004C: b"\x01\x02\x03"},
        service_data={},
        service_uuids=[],
        source="local",
    )
    assert not device.supported(si)


def test_model_from_name() -> None:
    device = ChefIqBluetoothDeviceData()
    device.update(_service_info(V3_TEMPERATURE, name="CQ50"))
    assert device.get_device_name() is not None
    assert "CQ50" in (device.title or "")


def test_unknown_name_falls_back_to_cq60() -> None:
    device = ChefIqBluetoothDeviceData()
    device.update(_service_info(V3_TEMPERATURE, name=""))
    assert device.title == "CQ60 079A"


@pytest.mark.parametrize("short", ["", "01"])
def test_too_short_payload_ignored(short: str) -> None:
    device = ChefIqBluetoothDeviceData()
    update = device.update(_service_info(short))
    assert not update.entity_values
