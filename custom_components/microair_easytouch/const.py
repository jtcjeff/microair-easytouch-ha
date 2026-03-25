"""Constants for the Micro-Air EasyTouch integration."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "microair_easytouch"

# Platforms
PLATFORMS_WIFI = ["climate", "sensor"]
PLATFORMS_BLE = ["climate", "sensor", "button"]

# Config entry keys
CONF_CONNECTION_TYPE = "connection_type"
CONF_IP_ADDRESS = "ip_address"
CONF_DEVICE_NAME = "device_name"
CONF_MAC_ADDRESS = "mac_address"
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_ZONE_COUNT = "zone_count"

DEFAULT_ZONE_COUNT = 1
MAX_ZONE_COUNT = 3

# Connection types
CONN_TYPE_WIFI = "wifi"
CONN_TYPE_BLE = "bluetooth"

# Update intervals
WIFI_UPDATE_INTERVAL = timedelta(seconds=30)
BLE_UPDATE_INTERVAL = timedelta(seconds=30)

# Setpoint debounce delay (seconds) — wait before pushing to device
SETPOINT_DEBOUNCE_SECONDS = 2.0

# ── BLE GATT UUIDs ──────────────────────────────────────────────────────────
BLE_SERVICE_UUID = "000000FF-0000-1000-8000-00805F9B34FB"
BLE_PASSWORD_CMD_UUID = "0000DD01-0000-1000-8000-00805F9B34FB"
BLE_JSON_CMD_UUID = "0000EE01-0000-1000-8000-00805F9B34FB"
BLE_JSON_RETURN_UUID = "0000FF01-0000-1000-8000-00805F9B34FB"

# BLE retry settings
BLE_MAX_AUTH_ATTEMPTS = 3
BLE_CONNECT_TIMEOUT = 20  # seconds
BLE_READ_TIMEOUT = 10  # seconds

# ── BLE HVAC Mode mappings ───────────────────────────────────────────────────
# mode_num (info[10]) → HA HVACMode
BLE_HVAC_HA_TO_DEVICE = {
    "off":       0,
    "fan_only":  1,
    "cool":      2,
    "heat":      4,
    "dry":       6,
    "auto":      11,
    "heat_cool": 11,
}

# current_mode_num (info[15]) — includes transitional states
BLE_CURRENT_MODE_MAP = {
    0:  "off",
    1:  "fan",
    2:  "cool",
    3:  "cool_on",   # compressor starting
    4:  "heat",
    5:  "heat_on",   # heat starting
    11: "auto",
}

# mode_num → HA HVACMode (set mode, not current action)
BLE_MODE_NUM_TO_HA = {
    0:  "off",
    1:  "fan_only",
    2:  "cool",
    4:  "heat",
    6:  "dry",
    11: "auto",
}

# ── BLE response array indices (info = Z_sts["zone_key"]) ──────────────────
BLE_IDX_AUTO_HEAT_SP   = 0
BLE_IDX_AUTO_COOL_SP   = 1
BLE_IDX_COOL_SP        = 2
BLE_IDX_HEAT_SP        = 3
BLE_IDX_DRY_SP         = 4
BLE_IDX_FAN_ONLY_MODE  = 6
BLE_IDX_COOL_FAN_MODE  = 7
BLE_IDX_AUTO_FAN_MODE  = 9
BLE_IDX_MODE_NUM       = 10
BLE_IDX_HEAT_FAN_MODE  = 11
BLE_IDX_TEMPERATURE    = 12
BLE_IDX_CURRENT_MODE   = 15

# BLE Fan mode mappings
BLE_FAN_MODES_FULL = {
    "off":       0,
    "low":       1,    # manualL
    "high":      2,    # manualH
    "cycled_low":  65,
    "cycled_high": 66,
    "auto":      128,
}
BLE_FAN_MODES_FAN_ONLY = {
    "off":  0,
    "low":  1,
    "high": 2,
}
BLE_FAN_MODE_DEVICE_TO_HA = {
    0:   "off",
    1:   "low",
    2:   "high",
    65:  "low",
    66:  "high",
    128: "auto",
}

# ── WiFi HVAC Mode mappings ──────────────────────────────────────────────────
# Status byte values at data0[11:13] (hex → HA mode)
WIFI_CONTROL_MODE_MAP = {
    0x00: "off",
    0x20: "dry",   # 32
    0x30: "auto",  # 48
    0x40: "heat",  # 64
    0x50: "cool",  # 80
}

# WiFi Fan mode nibble at data0[15:16]
WIFI_FAN_STATE_MAP = {
    0:  ("off",    "off"),    # (fan_mode, fan_state)
    1:  ("on",     "low"),
    2:  ("on",     "medium"),
    3:  ("auto",   "high"),
    8:  ("auto",   "low"),
    9:  ("auto",   "medium"),
    10: ("auto",   "high"),
}

# WiFi command hex strings — 'xx' is replaced with the device's 2-char network ID
WIFI_CMD_HVAC_OFF   = "17F0xx000401000000"
WIFI_CMD_HVAC_DRY   = "17F0xx000402000000"
WIFI_CMD_HVAC_AUTO  = "17F0xx000403000000"
WIFI_CMD_HVAC_HEAT  = "17F0xx000404000000"
WIFI_CMD_HVAC_COOL  = "17F0xx000405000000"

# Fan mode commands: append fan value nibble (0-3) as single hex char
WIFI_CMD_FAN_PREFIX  = "17F0xx00040{fan}0000"

# Setpoint command: append setpoint as 2-char hex at end
WIFI_CMD_SETPOINT_PREFIX = "17F0xx0004000000"

WIFI_HVAC_HA_TO_CMD = {
    "off":  WIFI_CMD_HVAC_OFF,
    "dry":  WIFI_CMD_HVAC_DRY,
    "auto": WIFI_CMD_HVAC_AUTO,
    "heat_cool": WIFI_CMD_HVAC_AUTO,
    "heat": WIFI_CMD_HVAC_HEAT,
    "cool": WIFI_CMD_HVAC_COOL,
}

# WiFi fan nibble values for set-fan commands
WIFI_FAN_MODE_TO_NIBBLE = {
    "off":    0,
    "low":    1,
    "medium": 2,
    "high":   3,
    "auto":   8,
}

# ── Shared HA mode string constants ─────────────────────────────────────────
# These mirror homeassistant.components.climate.HVACMode string values
# so we can use them in mappings before importing HA constants.
HA_OFF       = "off"
HA_HEAT      = "heat"
HA_COOL      = "cool"
HA_AUTO      = "auto"
HA_DRY       = "dry"
HA_FAN_ONLY  = "fan_only"
HA_HEAT_COOL = "heat_cool"
