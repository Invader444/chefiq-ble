"""Tests for the Chef iQ BLE advertisement parser.

V3 fixtures are real advertisements captured from a CQ60 (firmware 5.0.0).
V2 and legacy fixtures are synthetic, constructed from the byte layout
for those advertisement formats, and are marked as such.
"""

from __future__ import annotations

import pytest
from home_assistant_bluetooth import BluetoothServiceInfo

from chefiq_ble import ChefIqBluetoothDeviceData

CHEFIQ = 0x05CD


def _service_info(payload: str, *, name: str = "CQ60") -> BluetoothServiceInfo:
    return BluetoothServiceInfo(
        name=name,
        address="C9:14:65:CA:07:9A",
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


# --- V3 (real CQ60 captures) ------------------------------------------------


def test_v3_temperature_packet_real_capture() -> None:
    device = ChefIqBluetoothDeviceData()
    values = _values(device, "015024012b0135012e012b0130012401315a")
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
    values = _values(device, "0350c91465ca079a64201e010301007ac4")
    assert values["battery_percent"] == 100
    assert values["soc_temperature"] == 32


def test_v3_name_packet_emits_no_sensors() -> None:
    device = ChefIqBluetoothDeviceData()
    update = device.update(_service_info("005050726f6265203100000000000a2b"))
    assert all(k.key == "signal_strength" for k in update.entity_values)


def test_v3_accumulates_across_packet_types() -> None:
    device = ChefIqBluetoothDeviceData()
    _values(device, "015024012b0135012e012b0130012401315a")  # temps
    values = _values(device, "0350c91465ca079a64201e010301007ac4")  # status
    # temperatures persist while the status packet adds battery/soc
    assert values["food_temperature"] == 29.9
    assert values["battery_percent"] == 100
    assert values["soc_temperature"] == 32


# --- V2 (synthetic, from spec) ----------------------------------------------


def test_v2_temperature_packet_synthetic() -> None:
    # header 01 20 -> type 1, version 2.0.0 -> V2
    # battery=0x55, soc=0x1e, ambient=25.0, food=60.0, tip1=40.0, tip2=41.0, tip3=42.0
    device = ChefIqBluetoothDeviceData()
    values = _values(device, "0120551efa00580290019a01a401fa00")
    assert values["battery_percent"] == 85
    assert values["soc_temperature"] == 30
    assert values["ambient_temperature"] == 25.0
    assert values["food_temperature"] == 60.0
    assert values["probe_tip_1_temperature"] == 40.0
    assert values["probe_tip_2_temperature"] == 41.0
    assert values["probe_tip_3_temperature"] == 42.0
    # V2 has only 3 tips
    assert "probe_tip_4_temperature" not in values


# --- legacy (synthetic, from spec) ------------------------------------------


def test_legacy_temperature_packet_synthetic() -> None:
    # header 01 10 -> type 1, version 1.0.0 -> legacy
    # MAC aa..ff, battery=0x5a, soc=0x21, food=55.0, ambient=22.0
    device = ChefIqBluetoothDeviceData()
    values = _values(device, "0110aabbccddeeff5a212602dc00dc00")
    assert values["battery_percent"] == 90
    assert values["soc_temperature"] == 33
    assert values["food_temperature"] == 55.0
    assert values["ambient_temperature"] == 22.0
    # legacy has no tip sensors
    assert "probe_tip_1_temperature" not in values


# --- guards / robustness ----------------------------------------------------


def test_iq_sense_hub_is_rejected() -> None:
    # Real iQ Sense 540 appliance advert (21 bytes) under the same manufacturer.
    device = ChefIqBluetoothDeviceData()
    update = device.update(
        _service_info("50754e427588e5133f8d4bb3a403113f437a871e05", name="iQ Sense 540")
    )
    assert not device.supported(
        _service_info("50754e427588e5133f8d4bb3a403113f437a871e05", name="iQ Sense 540")
    )
    assert not update.entity_values


def test_probe_is_supported() -> None:
    device = ChefIqBluetoothDeviceData()
    assert device.supported(_service_info("015024012b0135012e012b0130012401315a"))


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


def test_out_of_range_temperature_is_dropped() -> None:
    # ambient slot set to 0x7fff LE (= ff 7f -> 3276.7 C sentinel); must be dropped.
    device = ChefIqBluetoothDeviceData()
    values = _values(device, "0150ff7f2b0135012e012b0130012401315a")
    assert "ambient_temperature" not in values
    # the other sensors still parse fine
    assert values["food_temperature"] == 29.9


def test_model_from_name() -> None:
    device = ChefIqBluetoothDeviceData()
    device.update(_service_info("015024012b0135012e012b0130012401315a", name="CQ50"))
    assert device.get_device_name() is not None
    assert "CQ50" in (device.title or "")


@pytest.mark.parametrize("short", ["", "01"])
def test_too_short_payload_ignored(short: str) -> None:
    device = ChefIqBluetoothDeviceData()
    update = device.update(_service_info(short))
    assert not any(k.key != "signal_strength" for k in update.entity_values)
