"""Checks for the bug fixes on fix/ble-wifi-bugs."""
import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import pytest
from homeassistant.components.climate import ClimateEntityFeature, HVACMode

from custom_components.microair_easytouch import coordinator as coord_mod
from custom_components.microair_easytouch import config_flow as flow_mod
from custom_components.microair_easytouch.climate import MicroAirClimate
from custom_components.microair_easytouch.const import CONN_TYPE_BLE, CONN_TYPE_WIFI
from custom_components.microair_easytouch.coordinator import (
    MicroAirBLECoordinator,
    MicroAirData,
    MicroAirWiFiCoordinator,
)


def ble(hass, zones=3):
    return MicroAirBLECoordinator(hass, "AA:BB:CC:DD:EE:FF", "", "", "t", zone_count=zones)


def wifi(hass, zones=1):
    return MicroAirWiFiCoordinator(hass, "127.0.0.1", "t", zone_count=zones)


def zone_info(mode=2, cool_fan=0, cur=2):
    info = [70, 76, 74, 68, 72, 0, 1, cool_fan, 0, 128, mode, 128, 75, 0, 0, cur]
    return info


# ── BLE parsing ─────────────────────────────────────────────────────────────

async def test_ble_fan_off_is_off_not_auto(hass):
    c = ble(hass)
    data = c._parse_all_zones({"Z_sts": {"0": zone_info(mode=2, cool_fan=0)}, "PRM": []})
    assert data[0].hvac_mode == HVACMode.COOL
    assert data[0].fan_mode == "off"
    assert "medium" not in data[0].available_fan_modes


async def test_ble_short_zone_is_skipped(hass):
    c = ble(hass)
    data = c._parse_all_zones(
        {"Z_sts": {"0": zone_info(), "1": [70, 76, 74]}, "PRM": []}
    )
    assert 0 in data and 1 not in data


async def test_ble_prm7_still_means_off(hass):
    c = ble(hass)
    data = c._parse_all_zones({"Z_sts": {"0": zone_info()}, "PRM": [7]})
    assert data[0].hvac_action.value == "off"


# ── BLE connection handling ─────────────────────────────────────────────────

async def test_ble_command_error_disconnects(hass):
    c = ble(hass)
    client = MagicMock(is_connected=True)
    client.write_gatt_char = AsyncMock(side_effect=Exception("boom"))
    client.disconnect = AsyncMock()
    c._client = client
    c._authenticated = True
    assert await c._send_command({"Type": "Change"}) is False
    client.disconnect.assert_awaited_once()
    assert c._client is None


async def test_ble_empty_poll_disconnects(hass):
    c = ble(hass)
    client = MagicMock(is_connected=True)
    client.disconnect = AsyncMock()
    c._client = client
    c._authenticated = True
    with patch.object(c, "_poll_device", AsyncMock(return_value={})):
        with pytest.raises(Exception):
            await c._async_update_data()
    client.disconnect.assert_awaited_once()


async def test_ble_old_client_disconnect_callback_ignored(hass):
    c = ble(hass)
    new = MagicMock()
    c._client = new
    c._authenticated = True
    c._on_disconnect(MagicMock())  # stale client
    assert c._client is new and c._authenticated


@pytest.mark.parametrize("fails", [False, True])
async def test_config_flow_releases_test_connection(hass, fails):
    flow = flow_mod.MicroAirConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    flow._selected_mac = "AA:BB:CC:DD:EE:FF"
    shutdown = AsyncMock()
    test = AsyncMock(side_effect=Exception("x") if fails else None, return_value=True)
    with patch.object(MicroAirBLECoordinator, "test_connection", test), \
         patch.object(MicroAirBLECoordinator, "async_shutdown", shutdown), \
         patch.object(flow, "async_set_unique_id", AsyncMock()), \
         patch.object(flow, "_abort_if_unique_id_configured"):
        result = await flow.async_step_ble_credentials({"email": "", "password": ""})
    shutdown.assert_awaited_once()
    assert result["step_id"] == ("ble_credentials" if fails else "zones")


# ── WiFi ────────────────────────────────────────────────────────────────────

# idx 4:6 network id, 11:13 mode byte 0x50 (cool), 15 fan nibble 1, 16:18 sp 72, 18:20 temp 75
D0 = "0000AB" + "00000" + "50" + "00" + "1" + "48" + "4B"
assert len(D0) == 20 and D0[11:13] == "50" and D0[15] == "1"


async def test_wifi_fan_manual_low_is_valid(hass):
    c = wifi(hass)
    zd = c._parse_zone(0, D0, "")
    assert zd.hvac_mode == HVACMode.COOL
    assert zd.fan_mode == "low" and zd.fan_mode in zd.available_fan_modes
    assert zd.setpoint == 72


async def test_wifi_pending_setpoint_shown(hass):
    c = wifi(hass)
    c._pending_setpoints[0] = 68
    assert c._parse_zone(0, D0, "").setpoint == 68


async def test_wifi_reads_full_multi_segment_response(hass):
    body = "<S><D>" + "A" * 3000 + "</D><D>END</D></S>"
    reader = asyncio.StreamReader()
    writer = MagicMock(drain=AsyncMock(), wait_closed=AsyncMock())

    async def feed():
        reader.feed_data(b"HTTP/1.0 200 OK\r\n\r\n" + body[:10].encode())
        await asyncio.sleep(0.05)  # second TCP segment arrives later
        reader.feed_data(body[10:].encode())
        reader.feed_eof()

    async def fake_open(host, port):
        hass.async_create_task(feed())
        return reader, writer

    c = wifi(hass)
    with patch.object(coord_mod.asyncio, "open_connection", fake_open):
        status, got = await c._post("/ShortStatus")
    assert status == 200 and got == body


async def test_wifi_debounce_sends_latest_value(hass):
    c = wifi(hass)
    sent = []

    async def transmit(zone, cmd):
        sent.append(cmd[-2:])
        if len(sent) == 1:
            c._pending_setpoints[0] = 72  # user tapped again mid-send
        return True

    with patch.object(coord_mod, "SETPOINT_DEBOUNCE_SECONDS", 0.01), \
         patch.object(c, "_transmit", transmit), \
         patch.object(c, "async_request_refresh", AsyncMock()):
        await c.async_set_temperature(70, zone=0)
        await c._setpoint_tasks[0]
    assert sent == ["46", "48"]  # 70 then 72
    assert 0 not in c._pending_setpoints


async def test_wifi_shutdown_cancels_pending(hass):
    c = wifi(hass)
    with patch.object(coord_mod, "SETPOINT_DEBOUNCE_SECONDS", 10):
        await c.async_set_temperature(70, zone=0)
        task = c._setpoint_tasks[0]
        await c.async_shutdown()
        await asyncio.sleep(0)
    assert task.cancelled() or task.done()


# ── Climate entity ──────────────────────────────────────────────────────────

def entity(conn, coordinator, mode, setpoint=72, high=None, low=None):
    entry = MagicMock(title="t")
    coordinator.data = {0: MicroAirData(zone=0, hvac_mode=mode, setpoint=setpoint,
                                        setpoint_high=high, setpoint_low=low)}
    return MicroAirClimate(coordinator, entry, "id", conn, 0)


async def test_wifi_climate_auto_has_single_setpoint(hass):
    e = entity(CONN_TYPE_WIFI, wifi(hass), HVACMode.AUTO)
    assert HVACMode.FAN_ONLY not in e.hvac_modes
    assert not e.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
    assert e.target_temperature == 72
    assert e.target_temperature_high is None


async def test_ble_climate_auto_keeps_range(hass):
    e = entity(CONN_TYPE_BLE, ble(hass), HVACMode.AUTO, setpoint=None, high=76, low=68)
    assert HVACMode.FAN_ONLY in e.hvac_modes
    assert e.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
    assert e.target_temperature is None
    assert (e.target_temperature_low, e.target_temperature_high) == (68, 76)
