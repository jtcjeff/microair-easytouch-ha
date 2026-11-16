"""Zone off, heat source detection and selection."""
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from homeassistant.components.climate import HVACMode

from custom_components.microair_easytouch.climate import MicroAirClimate
from custom_components.microair_easytouch.const import CONN_TYPE_BLE
from custom_components.microair_easytouch.coordinator import (
    MicroAirBLECoordinator, parse_zone_config,
)
from custom_components.microair_easytouch.select import MicroAirHeatSourceSelect


def info(mode, cur=0, heat_sp=70):
    return [68, 76, 74, heat_sp, 72, 45, 0, 128, 128, 128, mode, 128, 75, 255, 0, cur]


def ble(hass, zones=3):
    c = MicroAirBLECoordinator(hass, "AA:BB", "", "", "t", zone_count=zones)
    c._send_command = AsyncMock(return_value=True)
    c.async_request_refresh = AsyncMock()
    return c


def sent(c):
    return c._send_command.await_args.args[0]["Changes"]


def with_status(c, *modes):
    c.data = c._parse_all_zones({"Z_sts": {str(i): info(m) for i, m in enumerate(modes)}, "PRM": []})


# ── Zone off vs system power ────────────────────────────────────────────────

async def test_zone_off_does_not_send_power(hass):
    c = ble(hass); with_status(c, 2, 2, 2)
    await c.async_set_hvac_mode(HVACMode.OFF, zone=2)
    assert sent(c) == {"zone": 2, "mode": 0}


async def test_cool_still_powers_on(hass):
    c = ble(hass); with_status(c, 0, 0, 0)
    await c.async_set_hvac_mode(HVACMode.COOL, zone=1)
    assert sent(c) == {"zone": 1, "power": 1, "mode": 2}


async def test_setpoint_does_not_send_power(hass):
    c = ble(hass); with_status(c, 2, 0, 0)
    await c.async_set_temperature(zone=0, setpoint=73)
    assert sent(c) == {"zone": 0, "cool_sp": 73}


async def test_system_power(hass):
    c = ble(hass)
    await c.async_set_system_power(False)
    assert sent(c) == {"zone": 0, "power": 0}


# ── Config parsing ──────────────────────────────────────────────────────────

def test_parse_config_mode_list_and_limits():
    zc = parse_zone_config(0, {"RT": "Config", "CFG": {"Zone": 0, "MA": [1, 2, 4, 7, 11], "SPL": [60, 90, 50, 80]}})
    assert zc.modes == {0, 1, 2, 4, 7, 11}
    assert zc.heat_modes == [4, 7]
    assert (zc.min_cool, zc.max_cool, zc.min_heat, zc.max_heat) == (60, 90, 50, 80)


def test_parse_config_bitmask():
    zc = parse_zone_config(1, {"CFG": {"Zone": 1, "MAV": (1 << 2) | (1 << 5)}})
    assert zc.modes == {0, 2, 5} and zc.heat_modes == [5]


def test_parse_config_garbage_is_unknown():
    zc = parse_zone_config(0, {"CFG": "nope"})
    assert not zc.known


async def test_load_configs_from_device(hass):
    c = ble(hass, zones=2)
    replies = {0: {"RT": "Config", "CFG": {"Zone": 0, "MA": [2, 4]}},
               1: {"RT": "Config", "CFG": {"Zone": 1, "MA": [2, 4, 7]}}}
    c._request = AsyncMock(side_effect=lambda client, cmd: replies[cmd["Zone"]])
    await c._load_zone_configs(MagicMock())
    assert [cmd.args[1] for cmd in c._request.await_args_list] == [
        {"Type": "Get Config", "Zone": 0}, {"Type": "Get Config", "Zone": 1}]
    assert c.available_heat_modes(0) == [4]
    assert c.available_heat_modes(1) == [4, 7]
    await c._load_zone_configs(MagicMock())  # already loaded → no new requests
    assert c._request.await_count == 2


# ── Heat with the right source ──────────────────────────────────────────────

async def test_heat_uses_preferred_source(hass):
    c = ble(hass); with_status(c, 0, 0, 0)
    c.zone_configs[0] = parse_zone_config(0, {"CFG": {"MA": [2, 4, 7]}})
    c.heat_mode_preference[0] = 7
    await c.async_set_hvac_mode(HVACMode.HEAT, zone=0)
    assert sent(c) == {"zone": 0, "power": 1, "mode": 7}


async def test_heat_defaults_to_furnace_without_config(hass):
    c = ble(hass); with_status(c, 0, 0, 0)
    await c.async_set_hvac_mode(HVACMode.HEAT, zone=0)
    assert sent(c)["mode"] == 4


async def test_mode7_reads_as_heat(hass):
    c = ble(hass); with_status(c, 7, 0, 0)
    zd = c.data[0]
    assert zd.hvac_mode == HVACMode.HEAT and zd.mode_num == 7 and zd.setpoint == 70


# ── Entities ────────────────────────────────────────────────────────────────

def test_climate_modes_follow_zone_config(hass):
    c = ble(hass); with_status(c, 2, 2, 2)
    c.zone_configs[0] = parse_zone_config(0, {"CFG": {"MA": [1, 2], "SPL": [62, 85, 55, 80]}})
    e = MicroAirClimate(c, MagicMock(title="t"), "id", CONN_TYPE_BLE, 0)
    assert e.hvac_modes == [HVACMode.OFF, HVACMode.COOL, HVACMode.FAN_ONLY]
    assert (e.min_temp, e.max_temp) == (55, 85)
    e2 = MicroAirClimate(c, MagicMock(title="t"), "id", CONN_TYPE_BLE, 1)
    assert HVACMode.HEAT in e2.hvac_modes  # unknown config → full list


async def test_select_options_and_switch_while_heating(hass):
    c = ble(hass); with_status(c, 4, 0, 0)
    c.zone_configs[0] = parse_zone_config(0, {"CFG": {"MA": [2, 4, 7]}})
    s = MicroAirHeatSourceSelect(c, MagicMock(title="t"), "AA:BB", 0)
    assert s.options == ["Furnace", "Heat pump / heat strip"]
    assert s.current_option == "Furnace"
    await s.async_select_option("Heat pump / heat strip")
    assert sent(c) == {"zone": 0, "power": 1, "mode": 7}


async def test_select_when_not_heating_only_remembers(hass):
    c = ble(hass); with_status(c, 2, 0, 0)
    c.zone_configs[0] = parse_zone_config(0, {"CFG": {"MA": [2, 4, 7]}})
    c.async_update_listeners = MagicMock()
    s = MicroAirHeatSourceSelect(c, MagicMock(title="t"), "AA:BB", 0)
    await s.async_select_option("Heat pump / heat strip")
    c._send_command.assert_not_awaited()
    assert s.current_option == "Heat pump / heat strip"


def test_misread_config_does_not_hide_modes(hass):
    """A config that decodes to no cool/heat/auto mode must not leave only Off."""
    c = ble(hass); with_status(c, 2, 2, 2)
    # e.g. MA as on/off flags, or a bitmask whose bits mean something else
    c.zone_configs[0] = parse_zone_config(0, {"RT": "Config", "CFG": {"MA": [1, 0, 1, 0]}})
    c.zone_configs[1] = parse_zone_config(1, {"RT": "Config", "CFG": {"MAV": 1 << 20}})
    for z in (0, 1):
        assert c.zone_config(z) is None
        e = MicroAirClimate(c, MagicMock(title="t"), "id", CONN_TYPE_BLE, z)
        assert {HVACMode.COOL, HVACMode.HEAT, HVACMode.AUTO} <= set(e.hvac_modes)
        assert c.available_heat_modes(z) == [4, 5, 7]


async def test_heat_source_preset_on_card(hass):
    from homeassistant.components.climate import ClimateEntityFeature
    c = ble(hass); with_status(c, 4, 2, 2)
    e = MicroAirClimate(c, MagicMock(title="t"), "id", CONN_TYPE_BLE, 0)
    # No config read → furnace, heat pump, electric heat all offered
    assert e.supported_features & ClimateEntityFeature.PRESET_MODE
    assert e.preset_modes == ["Furnace", "Heat pump", "Heat pump / heat strip"]
    assert e.preset_mode == "Furnace"
    await e.async_set_preset_mode("Heat pump")  # zone is heating → switches now
    assert sent(c) == {"zone": 0, "power": 1, "mode": 5}
    # Zone with one heat source → no preset
    c.zone_configs[1] = parse_zone_config(1, {"CFG": {"MA": [2, 4]}})
    e1 = MicroAirClimate(c, MagicMock(title="t"), "id", CONN_TYPE_BLE, 1)
    assert e1.preset_modes is None
    assert not e1.supported_features & ClimateEntityFeature.PRESET_MODE
    # WiFi never has one
    w = MicroAirClimate(MagicMock(data={}), MagicMock(title="t"), "ip", "wifi", 0)
    assert w.preset_modes is None
