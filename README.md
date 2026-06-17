# chefiq-ble

Passive Bluetooth Low Energy advertisement parser for **Chef iQ** wireless
cooking probes (CQ50 / CQ60), built for use with the Home Assistant
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
update = device.update(service_info)  # a BluetoothServiceInfo / BluetoothServiceInfoBleak
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

## Advertisement formats

Three temperature-payload layouts are supported, selected by the protocol
version carried in the advertisement:

- **V3** - verified against a CQ60 (protocol version 5.0.0).
- **V2** and **legacy** - supported from the protocol description but
  **not yet verified against real hardware**. Captures from a CQ50 or an
  older-firmware probe are welcome to confirm the layouts and the version
  dispatch boundaries.

This library decodes the probe's public BLE advertisements for
interoperability and builds on the earlier Chef iQ work in
[`ble_monitor`](https://github.com/custom-components/ble_monitor). The byte
layouts are documented in [`parser.py`](src/chefiq_ble/parser.py).

## License

MIT
