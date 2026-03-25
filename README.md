# Micro-Air EasyTouch — Home Assistant Integration

A full-featured Home Assistant custom integration for **Micro-Air EasyTouch RV thermostats**, supporting both **WiFi (local HTTP)** and **Bluetooth (BLE)** connections with no cloud dependency.

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

---

## Features

- **WiFi and Bluetooth** — choose the connection method that works best for your setup
- **Multi-zone support** — up to 3 independently controlled zones
- **Full HVAC mode control** — Off, Cool, Heat, Auto, Dry, Fan Only
- **Fan speed control** — Off, Low, High, Auto
- **HVAC action reporting** — know when the unit is actually Heating, Cooling, or Idle
- **Temperature sensors** — per-zone current temperature
- **Reboot button** — (Bluetooth only) remotely restart the thermostat
- **Fully local** — no Micro-Air cloud account required for WiFi; no internet dependency
- **HA automations** — all entities are first-class automation targets
- **HACS compatible**

---

## Requirements

| Requirement | WiFi | Bluetooth |
|---|---|---|
| Thermostat on local network with known IP | Required | — |
| Home Assistant with Bluetooth adapter | — | Required |
| EasyTouch thermostat in Bluetooth range | — | Required |
| Micro-Air account credentials | — | Optional (some firmware versions) |

---

## Installation

### Via HACS (recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations** → click the three-dot menu → **Custom repositories**
3. Add this repository URL, category **Integration**
4. Search for **Micro-Air EasyTouch** and install
5. Restart Home Assistant

### Manual

1. Copy the `custom_components/microair_easytouch/` folder into your HA `config/custom_components/` directory
2. Restart Home Assistant

---

## Setup

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Micro-Air EasyTouch**
3. Choose your connection method:

### WiFi Setup

- Enter a name for the device
- Enter the thermostat's **local IP address** (assign a static DHCP lease in your router for best results)
- Enter the number of zones (1–3)

### Bluetooth Setup

- HA will scan for nearby EasyTouch devices and show them in a list
- Select your thermostat
- Enter your Micro-Air account email and password if prompted — **leave both blank** if your thermostat does not require credentials
- Enter the number of zones (1–3)

> **Important for Bluetooth:** The EasyTouch thermostat only allows one BLE connection at a time. Close the EasyTouch mobile app completely before and during setup, and avoid using the app while HA is connected.

---

## Entities

For each zone the integration creates:

| Entity | Type | Description |
|---|---|---|
| Zone N Thermostat | `climate` | Full HVAC control — mode, fan speed, setpoint |
| Zone N Temperature | `sensor` | Current temperature reading |
| Reboot | `button` | Restart the thermostat *(Bluetooth only)* |

Zones are named **Zone 1**, **Zone 2**, **Zone 3** — you can rename them in HA to match your RV layout (e.g. "Living Area", "Bedroom").

---

## Connection Notes

### WiFi

- Polling interval: **30 seconds**
- Commands are sent instantly; setpoint changes are debounced by 2 seconds to handle rapid +/− presses
- The thermostat must be on the same network as your HA instance

### Bluetooth

- Polling interval: **60 seconds**
- Uses a single BLE connection per poll to query all zones at once
- If you see `BLE busy (InProgress)` in the logs, another device (e.g. the phone app) has an open connection — it will resolve automatically on the next poll cycle
- BLE range is typically 10–30 ft depending on walls and interference

---

## Troubleshooting

**Entities show "Unavailable"**
- WiFi: verify the IP address is reachable from HA (`ping <ip>` from your HA host)
- Bluetooth: ensure the thermostat is powered on and within range; check that the phone app is closed

**No temperature or mode data (Bluetooth)**
- Enable debug logging and check for JSON parse errors:
  ```yaml
  logger:
    logs:
      custom_components.microair_easytouch: debug
  ```
- Look for the raw response line starting with `← ` in the logs

**"BLE busy / InProgress" errors**
- The EasyTouch thermostat only supports one BLE connection at a time
- Force-close the MicroAir EasyTouch app on all phones/tablets
- HA will reconnect automatically on the next poll

**Auth failures (Bluetooth)**
- Try leaving the email and password fields blank during setup — many thermostat firmware versions do not enforce authentication
- If credentials are required, use the same password as your MicroAir mobile app account

---

## Protocol Details

### WiFi

- `POST http://{ip}/ShortStatus` returns XML with hex-encoded status bytes
- `POST http://{ip}/Transmission` accepts hex command strings; responds with `<X>OK</X>` on success
- Completely local — no cloud calls

### Bluetooth

- GATT service UUID: `000000FF-0000-1000-8000-00805F9B34FB`
- Auth write: `0000DD01` characteristic
- Command write: `0000EE01` characteristic (JSON)
- Response read/notify: `0000FF01` characteristic (JSON)
- Response format: `{"Z_sts": {"0": [...], "1": [...], "2": [...]}, "PRM": [...], "SN": "..."}`

---

## Acknowledgements

Protocol research and BLE command structure informed by the [k3vmcd/ha-micro-air-easytouch](https://github.com/k3vmcd/ha-micro-air-easytouch) and [HRFrazier/hass_microair_climate](https://github.com/HRFrazier/hass_microair_climate) projects.

---

## License

MIT
