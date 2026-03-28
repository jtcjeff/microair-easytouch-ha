"""Data coordinators for Micro-Air EasyTouch — WiFi and BLE backends.

coordinator.data is always:  dict[int, MicroAirData]   (zone index → data)

Zone indices are 0-based internally; displayed as Zone 1, 2, 3 in HA.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers import update_coordinator

from .const import (
    BLE_FAN_MODE_DEVICE_TO_HA,
    BLE_JSON_CMD_UUID,
    BLE_JSON_RETURN_UUID,
    BLE_MAX_AUTH_ATTEMPTS,
    BLE_PASSWORD_CMD_UUID,
    BLE_READ_TIMEOUT,
    BLE_STATUS_ONLY_UUID,
    BLE_UPDATE_INTERVAL,
    DEFAULT_ZONE_COUNT,
    DOMAIN,
    SETPOINT_DEBOUNCE_SECONDS,
    WIFI_CMD_FAN_PREFIX,
    WIFI_CMD_SETPOINT_PREFIX,
    WIFI_CONTROL_MODE_MAP,
    WIFI_FAN_MODE_TO_NIBBLE,
    WIFI_FAN_STATE_MAP,
    WIFI_HVAC_HA_TO_CMD,
    WIFI_UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


def _safe(arr: list, idx: int) -> Any:
    """Return arr[idx] or None if out of bounds."""
    try:
        return arr[idx]
    except (IndexError, TypeError):
        return None


# ── Shared data model ────────────────────────────────────────────────────────

@dataclass
class MicroAirData:
    """Normalised device state for a single zone."""

    zone: int = 0

    hvac_mode: HVACMode = HVACMode.OFF
    hvac_action: HVACAction = HVACAction.OFF

    current_temp: float | None = None
    setpoint: float | None = None          # heat/cool/dry setpoint
    setpoint_high: float | None = None     # auto-mode cool setpoint
    setpoint_low: float | None = None      # auto-mode heat setpoint

    fan_mode: str = "auto"
    fan_state: str = "off"

    humidity: float | None = None
    line_voltage: float | None = None

    # Populated by WiFi backend — each zone has its own network_id
    network_id: str = ""

    available_fan_modes: list[str] = field(default_factory=lambda: [
        "off", "low", "medium", "high", "auto",
    ])


ZoneData = dict[int, MicroAirData]   # type alias used throughout


# ── WiFi coordinator ─────────────────────────────────────────────────────────

class MicroAirWiFiCoordinator(update_coordinator.DataUpdateCoordinator[ZoneData]):
    """Polls the EasyTouch thermostat via local HTTP."""

    def __init__(
        self,
        hass: HomeAssistant,
        ip_address: str,
        name: str,
        zone_count: int = DEFAULT_ZONE_COUNT,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=name,
            update_interval=WIFI_UPDATE_INTERVAL,
        )
        self._ip = ip_address
        self._zone_count = zone_count

        # Per-zone pending setpoint debounce
        self._pending_setpoints: dict[int, float] = {}
        self._setpoint_tasks: dict[int, asyncio.Task] = {}

    # ── HTTP helper ───────────────────────────────────────────────────────────

    async def _post(self, path: str, data: bytes | str = b"") -> tuple[int, str]:
        """POST using a raw TCP socket with a minimal HTTP/1.0 request.

        The EasyTouch embedded server rejects all modern HTTP clients.
        We send the absolute minimum HTTP/1.0 request with no extra headers
        so nothing can trigger a premature connection close.
        """
        if isinstance(data, str):
            data = data.encode("utf-8")

        body_len = len(data)
        request = (
            f"POST {path} HTTP/1.0\r\n"
            f"Content-Length: {body_len}\r\n"
            "\r\n"
        ).encode("utf-8") + data

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._ip, 80),
            timeout=10,
        )
        try:
            writer.write(request)
            await writer.drain()

            response = await asyncio.wait_for(reader.read(65536), timeout=10)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

        text = response.decode("utf-8", errors="replace")
        # Split status line from body
        if "\r\n\r\n" in text:
            header_part, body = text.split("\r\n\r\n", 1)
        elif "\n\n" in text:
            header_part, body = text.split("\n\n", 1)
        else:
            header_part, body = text, ""

        status_line = header_part.split("\r\n")[0] if "\r\n" in header_part else header_part.split("\n")[0]
        try:
            status_code = int(status_line.split()[1])
        except (IndexError, ValueError):
            status_code = 200  # assume OK if we got any response

        _LOGGER.debug("Raw HTTP response: status=%s body=%r", status_code, body[:200])
        return status_code, body

    # ── Connection test ──────────────────────────────────────────────────────

    async def test_connection(self) -> bool:
        try:
            status, text = await self._post("/ShortStatus")
            _LOGGER.debug("ShortStatus test — HTTP %s, body: %s", status, text[:200])
            return status == 200
        except Exception as exc:
            raise update_coordinator.UpdateFailed(f"Cannot reach {self._ip}: {exc}") from exc

    # ── Polling ──────────────────────────────────────────────────────────────

    async def _async_update_data(self) -> ZoneData:
        try:
            status, raw = await self._post("/ShortStatus")
            if status != 200:
                raise update_coordinator.UpdateFailed(
                    f"ShortStatus returned HTTP {status}"
                )
            _LOGGER.debug("ShortStatus raw: %s", raw)
            return self._parse_xml(raw)
        except update_coordinator.UpdateFailed:
            raise
        except Exception as exc:
            raise update_coordinator.UpdateFailed(
                f"Error communicating with {self._ip}: {exc}"
            ) from exc

    # ── XML parsing ──────────────────────────────────────────────────────────

    def _parse_xml(self, xml_text: str) -> ZoneData:
        """Parse ShortStatus XML into per-zone MicroAirData.

        The thermostat returns pairs of <D> elements — one pair per zone:
          <D> element 2N   → data0 for zone N (mode, temp, setpoint, fan)
          <D> element 2N+1 → data1 for zone N (line voltage)

        If only 2 elements are present the unit is single-zone.
        """
        from defusedxml.ElementTree import fromstring

        xml = fromstring(xml_text)
        d_elements = [el.text or "" for el in xml.findall("D")]

        _LOGGER.debug(
            "ShortStatus: %d <D> elements found (expecting %d for %d zones)",
            len(d_elements), self._zone_count * 2, self._zone_count,
        )

        if len(d_elements) < 2:
            raise update_coordinator.UpdateFailed(
                f"ShortStatus XML has only {len(d_elements)} <D> elements — "
                "expected at least 2. Check your thermostat IP and firmware."
            )

        result: ZoneData = {}
        # Parse as many zones as we have data for (up to configured zone_count)
        zones_in_data = len(d_elements) // 2
        zones_to_parse = min(zones_in_data, self._zone_count)

        for zone in range(zones_to_parse):
            data0 = d_elements[zone * 2]
            data1 = d_elements[zone * 2 + 1] if (zone * 2 + 1) < len(d_elements) else ""
            result[zone] = self._parse_zone(zone, data0, data1)

        if not result:
            raise update_coordinator.UpdateFailed("No zones could be parsed from ShortStatus")

        return result

    def _parse_zone(self, zone: int, data0: str, data1: str) -> MicroAirData:
        """Parse a single zone's data0 / data1 hex strings."""
        zd = MicroAirData(zone=zone)

        _LOGGER.debug("Zone %d — data0=%r  data1=%r", zone, data0, data1)

        try:
            if len(data0) < 20:
                _LOGGER.warning(
                    "Zone %d data0 too short (%d chars): %r — skipping",
                    zone, len(data0), data0,
                )
                return zd

            zd.network_id = data0[4:6]
            zd.setpoint = float(int(data0[16:18], 16))
            zd.current_temp = float(int(data0[18:20], 16))

            if len(data1) >= 14:
                zd.line_voltage = float(int(data1[10:14], 16))

            control_byte = int(data0[11:13], 16)
            mode_str = WIFI_CONTROL_MODE_MAP.get(control_byte, "off")
            try:
                zd.hvac_mode = HVACMode(mode_str)
            except ValueError:
                zd.hvac_mode = HVACMode.OFF

            status_bits = int(data0[12:14], 16)
            compressor_on = bool(status_bits & 0b010)

            if zd.hvac_mode == HVACMode.OFF:
                zd.hvac_action = HVACAction.OFF
            elif compressor_on:
                if zd.hvac_mode == HVACMode.COOL:
                    zd.hvac_action = HVACAction.COOLING
                elif zd.hvac_mode == HVACMode.HEAT:
                    zd.hvac_action = HVACAction.HEATING
                elif zd.hvac_mode == HVACMode.DRY:
                    zd.hvac_action = HVACAction.DRYING
                elif zd.hvac_mode == HVACMode.AUTO:
                    if zd.current_temp is not None and zd.setpoint is not None:
                        zd.hvac_action = (
                            HVACAction.COOLING
                            if zd.current_temp > zd.setpoint
                            else HVACAction.HEATING
                        )
                    else:
                        zd.hvac_action = HVACAction.IDLE
                else:
                    zd.hvac_action = HVACAction.IDLE
            else:
                zd.hvac_action = HVACAction.IDLE

            fan_nibble = int(data0[15:16], 16)
            fan_mode_str, fan_state_str = WIFI_FAN_STATE_MAP.get(fan_nibble, ("off", "off"))
            zd.fan_mode = fan_mode_str
            zd.fan_state = fan_state_str

        except (ValueError, IndexError) as exc:
            _LOGGER.error("Zone %d parse error: %s  (data0=%r)", zone, exc, data0)

        return zd

    # ── Commands ─────────────────────────────────────────────────────────────

    async def _transmit(self, zone: int, cmd: str) -> bool:
        """POST a raw command string — uses the zone's network_id."""
        zone_data = (self.data or {}).get(zone)
        network_id = zone_data.network_id if zone_data else ""

        if not network_id:
            _LOGGER.warning("Zone %d: cannot transmit — network_id unknown", zone)
            return False

        final_cmd = cmd.replace("xx", network_id)
        _LOGGER.debug("Zone %d: transmitting %s", zone, final_cmd)
        try:
            status, body = await self._post("/Transmission", data=final_cmd)
            _LOGGER.debug("Transmission response: HTTP %s  body: %s", status, body)
            if status != 200 or "<X>OK</X>" not in body:
                _LOGGER.error(
                    "Zone %d: Transmission rejected (HTTP %s): %s",
                    zone, status, body,
                )
                return False
            return True
        except Exception as exc:
            _LOGGER.error("Zone %d: Transmission error: %s", zone, exc)
            return False

    async def async_set_hvac_mode(self, hvac_mode: HVACMode, zone: int = 0) -> None:
        cmd = WIFI_HVAC_HA_TO_CMD.get(hvac_mode.value)
        if cmd and await self._transmit(zone, cmd):
            await self.async_request_refresh()

    async def async_set_temperature(self, setpoint: float, zone: int = 0) -> None:
        """Debounced setpoint push."""
        self._pending_setpoints[zone] = setpoint
        task = self._setpoint_tasks.get(zone)
        if task and not task.done():
            return
        self._setpoint_tasks[zone] = asyncio.create_task(
            self._push_setpoint_delayed(zone)
        )

    async def _push_setpoint_delayed(self, zone: int) -> None:
        await asyncio.sleep(SETPOINT_DEBOUNCE_SECONDS)
        sp = self._pending_setpoints.pop(zone, None)
        if sp is None:
            return
        cmd = WIFI_CMD_SETPOINT_PREFIX + f"{int(sp):02x}"
        if await self._transmit(zone, cmd):
            await self.async_request_refresh()

    async def async_set_fan_mode(self, fan_mode: str, zone: int = 0) -> None:
        nibble = WIFI_FAN_MODE_TO_NIBBLE.get(fan_mode)
        if nibble is None:
            _LOGGER.warning("Unknown fan mode: %s", fan_mode)
            return
        cmd = WIFI_CMD_FAN_PREFIX.format(fan=nibble)
        if await self._transmit(zone, cmd):
            await self.async_request_refresh()


# ── BLE coordinator ──────────────────────────────────────────────────────────

class MicroAirBLECoordinator(update_coordinator.DataUpdateCoordinator[ZoneData]):
    """Communicates with the EasyTouch thermostat via Bluetooth LE."""

    def __init__(
        self,
        hass: HomeAssistant,
        mac_address: str,
        password: str,
        email: str,
        name: str,
        zone_count: int = DEFAULT_ZONE_COUNT,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=name,
            update_interval=BLE_UPDATE_INTERVAL,
        )
        self._mac = mac_address
        self._password = password
        self._email = email
        self._zone_count = zone_count

    # ── Connection test ──────────────────────────────────────────────────────

    async def test_connection(self) -> bool:
        result = await self._query_all_zones()
        if not result:
            raise update_coordinator.UpdateFailed(
                f"BLE connection to {self._mac} failed or returned no data"
            )
        return True

    # ── Polling ──────────────────────────────────────────────────────────────

    async def _async_update_data(self) -> ZoneData:
        """Open ONE BLE connection and query all zones within it."""
        result = await self._query_all_zones()
        if not result:
            # Raise UpdateFailed so HA marks entities unavailable until next successful poll.
            # This is a warning-level condition — transient BLE misses are normal.
            raise update_coordinator.UpdateFailed(
                f"BLE poll missed for {self._mac} — will retry in {BLE_UPDATE_INTERVAL.seconds}s"
            )
        return result

    # ── BLE communication ────────────────────────────────────────────────────

    async def _query_all_zones(self) -> ZoneData:
        """Connect once and read all-zone status.

        Strategy (in order):
        1. Read the statusOnly characteristic (BB01) — no command needed, fastest path.
        2. Fall back to sending a Get Status command via EE01 and reading the response
           from FF01 (with NOTIFY accumulation or polled READs).
        """
        from homeassistant.components.bluetooth import async_ble_device_from_address

        ble_device = async_ble_device_from_address(self.hass, self._mac, connectable=True)
        if ble_device is None:
            _LOGGER.error(
                "BLE device %s not found — ensure it is powered on and in Bluetooth range",
                self._mac,
            )
            return {}

        try:
            from bleak_retry_connector import establish_connection
            from bleak import BleakClient

            _LOGGER.debug("Connecting to %s", self._mac)
            async with await establish_connection(
                BleakClient, ble_device, self._mac, max_attempts=2
            ) as client:
                # Auth is best-effort — some devices accept anything, some need the password
                await self._authenticate(client)

                # ── Try BB01 (statusOnly) first — direct read, no command required ──
                raw_status = await self._read_status_only(client)
                if raw_status is not None:
                    _LOGGER.debug("BB01 status: %s", raw_status)
                    return self._parse_all_zones(raw_status)

                # ── Fall back to Get Status via EE01 / FF01 ──────────────────────
                chunks: list[bytes] = []
                response_ready = asyncio.Event()

                def _on_notify(sender: Any, data: bytearray) -> None:
                    chunks.append(bytes(data))
                    try:
                        json.loads(b"".join(chunks).decode("utf-8", errors="replace"))
                        response_ready.set()
                    except (json.JSONDecodeError, ValueError):
                        pass  # More chunks coming

                using_notify = False
                try:
                    await client.start_notify(BLE_JSON_RETURN_UUID, _on_notify)
                    using_notify = True
                    _LOGGER.debug("NOTIFY enabled on jsonReturn")
                except Exception as exc:
                    _LOGGER.debug("NOTIFY unavailable (%s) — will use READ", exc)

                try:
                    raw_status = await self._get_status(
                        client, chunks, response_ready, using_notify
                    )
                finally:
                    if using_notify:
                        try:
                            await client.stop_notify(BLE_JSON_RETURN_UUID)
                        except Exception:
                            pass

                if raw_status is None:
                    return {}

                _LOGGER.debug("Raw status: %s", raw_status)
                return self._parse_all_zones(raw_status)

        except asyncio.TimeoutError:
            _LOGGER.warning("BLE poll timed out for %s — will retry next cycle", self._mac)
            return {}
        except Exception as exc:
            exc_str = str(exc)
            if "InProgress" in exc_str or "in progress" in exc_str.lower():
                _LOGGER.warning(
                    "BLE busy for %s — close the EasyTouch phone app; HA will retry.",
                    self._mac,
                )
            elif "TimeoutError" in exc_str or "timed out" in exc_str.lower():
                _LOGGER.warning(
                    "BLE connection timed out for %s (device may be busy) — will retry next cycle",
                    self._mac,
                )
            else:
                _LOGGER.error("BLE error for %s: %s", self._mac, exc)
            return {}

    async def _read_status_only(self, client: Any) -> dict[str, Any] | None:
        """Try to read the statusOnly characteristic (BB01) without sending a command.

        This characteristic is updated by the device and can be read directly.
        Returns parsed JSON dict on success, None if unavailable or unreadable.
        """
        try:
            raw = await asyncio.wait_for(
                client.read_gatt_char(BLE_STATUS_ONLY_UUID),
                timeout=BLE_READ_TIMEOUT,
            )
            if not raw:
                return None
            text = bytes(raw).decode("utf-8", errors="replace").strip()
            _LOGGER.debug("BB01 raw: %r", text)
            parsed = json.loads(text)
            # Only use this response if it contains zone status data
            if "Z_sts" in parsed:
                return parsed
            _LOGGER.debug("BB01 response has no Z_sts — falling back to Get Status")
            return None
        except (asyncio.TimeoutError, json.JSONDecodeError):
            return None
        except Exception as exc:
            _LOGGER.debug("BB01 read unavailable: %s", exc)
            return None

    async def _authenticate(self, client: Any) -> bool:
        """Write password to the auth characteristic (best-effort).

        If the device does not require authentication the write may fail harmlessly;
        we log the result but do NOT abort — commands will be attempted regardless.
        """
        if not self._password:
            _LOGGER.debug("No password configured — skipping auth")
            return True

        pw_bytes = self._password.encode("utf-8")
        for attempt in range(BLE_MAX_AUTH_ATTEMPTS):
            use_response = (attempt % 2 == 0)
            try:
                await asyncio.wait_for(
                    client.write_gatt_char(
                        BLE_PASSWORD_CMD_UUID, pw_bytes, response=use_response
                    ),
                    timeout=BLE_READ_TIMEOUT,
                )
                await asyncio.sleep(0.3)
                _LOGGER.debug("Auth write succeeded (attempt %d)", attempt + 1)
                return True
            except Exception as exc:
                _LOGGER.debug(
                    "Auth attempt %d failed (response=%s): %s", attempt + 1, use_response, exc
                )
                if attempt < BLE_MAX_AUTH_ATTEMPTS - 1:
                    await asyncio.sleep(0.5)

        _LOGGER.warning(
            "Auth failed after %d attempts — proceeding anyway. "
            "If data is missing, verify your MicroAir app password.",
            BLE_MAX_AUTH_ATTEMPTS,
        )
        return False

    async def _get_status(
        self,
        client: Any,
        chunks: list[bytes],
        response_ready: asyncio.Event,
        using_notify: bool,
    ) -> dict[str, Any] | None:
        """Send a single Get Status and return the parsed JSON response."""
        command: dict[str, Any] = {"Type": "Get Status", "Zone": 0, "TM": int(time.time())}
        if self._email:
            command["EM"] = self._email
        cmd_bytes = json.dumps(command).encode("utf-8")
        _LOGGER.debug("→ %s", command)

        try:
            await asyncio.wait_for(
                client.write_gatt_char(BLE_JSON_CMD_UUID, cmd_bytes, response=True),
                timeout=BLE_READ_TIMEOUT,
            )
        except Exception as exc:
            _LOGGER.error("Write error: %s", exc)
            return None

        if using_notify:
            try:
                await asyncio.wait_for(response_ready.wait(), timeout=BLE_READ_TIMEOUT)
            except asyncio.TimeoutError:
                if not chunks:
                    _LOGGER.warning("No BLE notification received within timeout")
                    return None
                _LOGGER.debug("Notify timeout — trying partial data (%d chunks)", len(chunks))
        else:
            for attempt in range(6):
                await asyncio.sleep(0.5 + attempt * 0.3)
                try:
                    raw = await asyncio.wait_for(
                        client.read_gatt_char(BLE_JSON_RETURN_UUID),
                        timeout=BLE_READ_TIMEOUT,
                    )
                    if raw:
                        chunks.clear()
                        chunks.append(bytes(raw))
                        try:
                            json.loads(b"".join(chunks).decode("utf-8"))
                            break
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            pass
                except Exception as exc:
                    _LOGGER.debug("READ attempt %d: %s", attempt + 1, exc)

        if not chunks:
            return None

        text = b"".join(chunks).decode("utf-8", errors="replace").strip()
        _LOGGER.debug("← %r", text)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            _LOGGER.error("JSON parse failed: %s  raw=%r", exc, text)
            return None

    async def _send_command(self, command: dict[str, Any]) -> bool:
        """Send a Change command over a fresh BLE connection."""
        from homeassistant.components.bluetooth import async_ble_device_from_address
        from bleak_retry_connector import establish_connection
        from bleak import BleakClient

        ble_device = async_ble_device_from_address(self.hass, self._mac, connectable=True)
        if ble_device is None:
            _LOGGER.error("Cannot send command — BLE device %s not found", self._mac)
            return False

        try:
            async with await establish_connection(
                BleakClient, ble_device, self._mac, max_attempts=1
            ) as client:
                await self._authenticate(client)
                cmd_bytes = json.dumps(command).encode("utf-8")
                _LOGGER.debug("Sending: %s", command)
                await asyncio.wait_for(
                    client.write_gatt_char(BLE_JSON_CMD_UUID, cmd_bytes, response=True),
                    timeout=BLE_READ_TIMEOUT,
                )
                return True
        except Exception as exc:
            if "InProgress" in str(exc) or "in progress" in str(exc).lower():
                _LOGGER.warning("BLE busy — command not sent. Close EasyTouch app.")
            else:
                _LOGGER.error("BLE command error: %s", exc)
            return False

    # ── State parsing ────────────────────────────────────────────────────────

    def _parse_all_zones(self, status: dict[str, Any]) -> ZoneData:
        """Parse the Z_sts dict — keys are zone index strings "0", "1", "2"."""
        from .const import (
            BLE_IDX_AUTO_COOL_SP, BLE_IDX_AUTO_HEAT_SP,
            BLE_IDX_AUTO_FAN_MODE, BLE_IDX_COOL_FAN_MODE,
            BLE_IDX_COOL_SP, BLE_IDX_CURRENT_MODE, BLE_IDX_DRY_SP,
            BLE_IDX_FAN_ONLY_MODE, BLE_IDX_HEAT_FAN_MODE,
            BLE_IDX_HEAT_SP, BLE_IDX_MODE_NUM, BLE_IDX_TEMPERATURE,
            BLE_CURRENT_MODE_MAP, BLE_MODE_NUM_TO_HA,
        )

        z_sts: dict[str, list] = status.get("Z_sts", {})
        param: list = status.get("PRM", [])
        result: ZoneData = {}

        for zone_key, info in z_sts.items():
            try:
                zone = int(zone_key)
            except ValueError:
                continue

            if zone >= self._zone_count:
                continue

            zd = MicroAirData(zone=zone)

            try:
                zd.current_temp = _safe(info, BLE_IDX_TEMPERATURE)

                mode_num = int(_safe(info, BLE_IDX_MODE_NUM) or 0)
                ha_mode_str = BLE_MODE_NUM_TO_HA.get(mode_num, "off")
                try:
                    zd.hvac_mode = HVACMode(ha_mode_str)
                except ValueError:
                    zd.hvac_mode = HVACMode.OFF

                # Setpoints
                zd.setpoint_low  = _safe(info, BLE_IDX_AUTO_HEAT_SP)
                zd.setpoint_high = _safe(info, BLE_IDX_AUTO_COOL_SP)
                if zd.hvac_mode == HVACMode.COOL:
                    zd.setpoint = _safe(info, BLE_IDX_COOL_SP)
                elif zd.hvac_mode == HVACMode.HEAT:
                    zd.setpoint = _safe(info, BLE_IDX_HEAT_SP)
                elif zd.hvac_mode == HVACMode.DRY:
                    zd.setpoint = _safe(info, BLE_IDX_DRY_SP)
                elif zd.hvac_mode == HVACMode.AUTO:
                    zd.setpoint = None  # use high/low range
                else:
                    zd.setpoint = _safe(info, BLE_IDX_COOL_SP)

                # Fan mode
                if zd.hvac_mode == HVACMode.FAN_ONLY:
                    fan_num = int(_safe(info, BLE_IDX_FAN_ONLY_MODE) or 0)
                    zd.available_fan_modes = ["off", "low", "high"]
                elif zd.hvac_mode == HVACMode.COOL:
                    fan_num = int(_safe(info, BLE_IDX_COOL_FAN_MODE) or 128)
                elif zd.hvac_mode == HVACMode.HEAT:
                    fan_num = int(_safe(info, BLE_IDX_HEAT_FAN_MODE) or 128)
                else:
                    fan_num = int(_safe(info, BLE_IDX_AUTO_FAN_MODE) or 128)
                zd.fan_mode = BLE_FAN_MODE_DEVICE_TO_HA.get(fan_num, "auto")

                # HVAC action from current_mode_num
                cur_num = int(_safe(info, BLE_IDX_CURRENT_MODE) or 0)
                cur_str = BLE_CURRENT_MODE_MAP.get(cur_num, "off")

                if zd.hvac_mode == HVACMode.OFF or 7 in param:
                    zd.hvac_action = HVACAction.OFF
                elif cur_str in ("cool", "cool_on"):
                    zd.hvac_action = HVACAction.COOLING
                elif cur_str in ("heat", "heat_on"):
                    zd.hvac_action = HVACAction.HEATING
                elif cur_str == "fan":
                    zd.hvac_action = HVACAction.FAN
                elif cur_str == "auto":
                    ct = zd.current_temp
                    high, low = zd.setpoint_high, zd.setpoint_low
                    if ct and high and ct > high:
                        zd.hvac_action = HVACAction.COOLING
                    elif ct and low and ct < low:
                        zd.hvac_action = HVACAction.HEATING
                    else:
                        zd.hvac_action = HVACAction.IDLE
                else:
                    zd.hvac_action = HVACAction.IDLE

            except Exception as exc:
                _LOGGER.error("Zone %s parse error: %s  info=%r", zone_key, exc, info)

            result[zone] = zd

        if not result:
            _LOGGER.warning(
                "No zones parsed from Z_sts. Keys present: %s  Full status: %s",
                list(z_sts.keys()), status,
            )

        return result

    # ── Commands ─────────────────────────────────────────────────────────────

    async def async_set_hvac_mode(self, hvac_mode: HVACMode, zone: int = 0) -> None:
        from .const import BLE_HVAC_HA_TO_DEVICE
        device_mode = BLE_HVAC_HA_TO_DEVICE.get(hvac_mode.value, 0)
        command = {
            "Type": "Change",
            "Zone": zone,
            "Changes": {
                "zone": zone,
                "power": 0 if hvac_mode == HVACMode.OFF else 1,
                "mode": device_mode,
            },
        }
        if await self._send_command(command):
            await self.async_request_refresh()

    async def async_set_temperature(
        self,
        zone: int = 0,
        setpoint: float | None = None,
        setpoint_high: float | None = None,
        setpoint_low: float | None = None,
    ) -> None:
        changes: dict[str, Any] = {"zone": zone, "power": 1}
        zone_data = (self.data or {}).get(zone)
        current_mode = zone_data.hvac_mode if zone_data else HVACMode.OFF

        if setpoint is not None:
            sp = int(setpoint)
            if current_mode == HVACMode.COOL:
                changes["cool_sp"] = sp
            elif current_mode == HVACMode.HEAT:
                changes["heat_sp"] = sp
            elif current_mode == HVACMode.DRY:
                changes["dry_sp"] = sp
        if setpoint_high is not None:
            changes["autoCool_sp"] = int(setpoint_high)
        if setpoint_low is not None:
            changes["autoHeat_sp"] = int(setpoint_low)

        if len(changes) > 2:
            command = {"Type": "Change", "Zone": zone, "Changes": changes}
            if await self._send_command(command):
                await self.async_request_refresh()

    async def async_set_fan_mode(
        self, fan_mode: str, zone: int = 0, hvac_mode: HVACMode | None = None
    ) -> None:
        from .const import BLE_FAN_MODES_FULL, BLE_FAN_MODES_FAN_ONLY
        zone_data = (self.data or {}).get(zone)
        current = hvac_mode or (zone_data.hvac_mode if zone_data else HVACMode.OFF)

        if current == HVACMode.FAN_ONLY:
            fan_value = BLE_FAN_MODES_FAN_ONLY.get(fan_mode, 0)
            changes = {"zone": zone, "fanOnly": fan_value}
        else:
            fan_value = BLE_FAN_MODES_FULL.get(fan_mode, 128)
            fan_key = {
                HVACMode.COOL: "coolFan",
                HVACMode.HEAT: "heatFan",
                HVACMode.AUTO: "autoFan",
            }.get(current, "autoFan")
            changes = {"zone": zone, fan_key: fan_value}

        command = {"Type": "Change", "Zone": zone, "Changes": changes}
        if await self._send_command(command):
            await self.async_request_refresh()

    async def async_reboot_device(self) -> None:
        await self._send_command({"Type": "Reboot", "Zone": 0})
