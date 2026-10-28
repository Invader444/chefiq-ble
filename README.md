# chefiq-ble

Passive Bluetooth Low Energy advertisement parser for **Chef iQ** wireless
cooking probes (CQ60), built for use with the Home Assistant
`bluetooth` passive update framework.

The probe broadcasts everything needed to read temperatures over plain BLE
advertisements - no connection, no cloud, no base/hub required.

## Install

```bash
pip install chefiq-ble
```

## Usage

```python
from chefiq_ble import ChefIqBluetoothDeviceData

device = ChefIqBluetoothDeviceData()
# service_info is a BluetoothServiceInfo (or BluetoothServiceInfoBleak)
update = device.update(service_info)
for key, value in update.entity_values.items():
    print(key.key, value.native_value)
```

## What it parses

A Chef iQ probe rotates three advertisement packet types in its manufacturer
specific data (company id `0x05CD`). The low nibble of the first byte selects
the type; the remaining header nibbles encode a `major.minor.patch`
advertisement protocol version.

| sensor | unit | source |
| --- | --- | --- |
| `food_temperature` | °C | temperature packet (food core) |
| `ambient_temperature` | °C | temperature packet |
| `probe_tip_1..4_temperature` | °C | temperature packet (V2: 3 tips, V3: 4 tips) |
| `battery_percent` | % | status packet (V3) / temperature packet (V2, legacy) |
| `soc_temperature` | °C | status packet (V3) / temperature packet (V2, legacy) |

All 16-bit temperatures are signed, little-endian, divided by 10 (Celsius).

## Identifying a probe

The manufacturer id alone is not proof of a Chef iQ probe. Some vendors put a
sensor reading where the Bluetooth company id belongs: an Inkbird IBS-TH
broadcasts its temperature x100 there, so at exactly 14.85 °C it shows up with
"manufacturer id" `0x05CD` ([home-assistant/core#188557][issue]). The parser
therefore accepts an advertisement, and `supported()` reports a probe, only when
the protocol major version is a known one (2 to 5) and the payload matches the
documented layout for its packet type:

- a **name** packet carries a 12-byte, NUL-padded, printable name, or the
  device's local name is a known probe model (probes advertise their model,
  e.g. `CQ60`, as the BLE local name);
- a **temperature** packet carries every reading of its layout, each either a
  plausible Celsius value or the firmware's "no reading" sentinel (raw values
  of 32000 and above; a CQ60 sends `0x7FFF` for an empty ring);
- a **status** packet (and a legacy temperature packet) carries the probe's own
  MAC address, which must match the advertising address.

Any single real packet passes, so discovery works whichever of the three packet
types is seen first. Rejected advertisements are logged at debug level.

[issue]: https://github.com/home-assistant/core/issues/188557

## Advertisement formats

Three temperature-payload layouts are supported, selected by the protocol
major version carried in the advertisement:

| protocol major | layout | status packet |
| --- | --- | --- |
| 5 | **V3** - verified against a CQ60 (protocol version 5.0.0) | MAC, battery, SoC |
| 4 | **V2** - 3 tips, battery and SoC inline | MAC only |
| 2, 3 | **legacy** - MAC, SoC, food and ambient inline; battery from 3.x | none |

V2 and legacy are supported from the protocol description and are **not yet
verified against real hardware**; captures from an older-firmware probe are
welcome to confirm the layouts and the version boundaries. Other major
versions are not known probe protocols and are ignored.

This library decodes the probe's public BLE advertisements for
interoperability and builds on the earlier Chef iQ work in
[`ble_monitor`](https://github.com/custom-components/ble_monitor). The byte
layouts are documented in [`parser.py`](src/chefiq_ble/parser.py).

## License

MIT
