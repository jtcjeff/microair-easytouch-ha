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
    BLE_AUTO_MODE_NUMS,
    BLE_FAN_MODE_DEVICE_TO_HA,
    BLE_HEAT_MODE_NUMS,
    BLE_MODE_ZONE_OFF,
    DEFAULT_HEAT_MODE,
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


def _int_or(value: Any, default: int) -> int:
    """int(value), or default only when the value is missing.

    Unlike `int(value or default)`, a real 0 (e.g. fan "off") is preserved.
    """
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# Z_sts arrays shorter than this are incomplete (index 15 = current mode)
BLE_MIN_ZONE_FIELDS = 16

BLE_FAN_MODES_STANDARD = ["off", "low", "high", "auto"]
WIFI_FAN_MODES = ["off", "low", "medium", "high", "auto"]


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

    # BLE only — the raw device mode number (e.g. 4 = furnace, 7 = electric heat)
    mode_num: int | None = None


ZoneData = dict[int, MicroAirData]   # type alias used throughout


@dataclass
class ZoneConfig:
    """What one zone's equipment supports, from a BLE "Get Config" reply.

    `modes` is the set of device mode numbers the zone offers. An empty set
    means the thermostat didn't tell us, and callers fall back to defaults.
    """

    zone: int
    modes: set[int] = field(default_factory=set)
    min_cool: int | None = None
    max_cool: int | None = None
    min_heat: int | None = None
    max_heat: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def known(self) -> bool:
        """Only trust a config that lists at least one real operating mode.

        The Get Config format is decoded from the app, not confirmed on every
        firmware. A reply that decodes to no cool/heat/auto mode is almost
        certainly being misread, and trusting it would leave the zone with
        nothing but "Off" in Home Assistant — so treat it as unknown.
        """
        return any(
            m in self.modes
            for m in (2, *BLE_HEAT_MODE_NUMS, *BLE_AUTO_MODE_NUMS)
        )

    @property
    def heat_modes(self) -> list[int]:
        return [m for m in BLE_HEAT_MODE_NUMS if m in self.modes]


def parse_zone_config(zone: int, response: dict[str, Any]) -> ZoneConfig:
    """Parse {"RT": "Config", "CFG": {"Zone": n, "MA": [...], "MAV": n, "SPL": [...]}}.

    Key names come from the EasyTouch app: MA = list of available mode
    numbers, MAV = the same as a bitmask (bit n set → mode n available),
    SPL = setpoint limits [min cool, max cool, min heat, max heat].
    Anything missing or malformed is simply left unknown.
    """
    cfg = response.get("CFG", response)
    if not isinstance(cfg, dict):
        cfg = {}
    zc = ZoneConfig(zone=zone, raw=dict(cfg))

    ma = cfg.get("MA")
    if isinstance(ma, list) and ma:
        zc.modes = {int(m) for m in ma if isinstance(m, (int, float))}
    else:
        mav = cfg.get("MAV")
        if isinstance(mav, (int, float)) and int(mav) > 0:
            mav = int(mav)
            zc.modes = {bit for bit in range(32) if mav & (1 << bit)}
    if zc.modes:
        zc.modes.add(BLE_MODE_ZONE_OFF)  # zone off is always possible

    spl = cfg.get("SPL")
    if isinstance(spl, list) and len(spl) >= 4 and all(
        isinstance(v, (int, float)) for v in spl[:4]
    ):
        zc.min_cool, zc.max_cool, zc.min_heat, zc.max_heat = (int(v) for v in spl[:4])

    return zc


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

            # HTTP/1.0: the server closes the socket when the response is done.
            # A single read() can return only the first TCP segment, which would
            # truncate multi-zone ShortStatus XML — so read until EOF.
            response = await asyncio.wait_for(reader.read(), timeout=10)
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
            zd.available_fan_modes = list(WIFI_FAN_MODES)
            # While a debounced setpoint is waiting to be sent, keep showing it
            # instead of letting a poll snap the UI back to the old value.
            pending = self._pending_setpoints.get(zone)
            zd.setpoint = float(pending if pending is not None else int(data0[16:18], 16))
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
        self._setpoint_tasks[zone] = self.hass.async_create_task(
            self._push_setpoint_delayed(zone)
        )

    async def _push_setpoint_delayed(self, zone: int) -> None:
        ok = False
        while True:
            await asyncio.sleep(SETPOINT_DEBOUNCE_SECONDS)
            sp = self._pending_setpoints.get(zone)
            if sp is None:
                break
            cmd = WIFI_CMD_SETPOINT_PREFIX + f"{int(sp):02x}"
            ok = await self._transmit(zone, cmd)
            if self._pending_setpoints.get(zone) == sp:
                # Nothing newer arrived while we were sending — done.
                self._pending_setpoints.pop(zone, None)
                break
            # The user changed it again mid-send: wait and send the newer value.
        if ok:
            await self.async_request_refresh()

    async def async_shutdown(self) -> None:
        """Cancel any setpoint pushes still waiting when the entry unloads."""
        await super().async_shutdown()
        for task in self._setpoint_tasks.values():
            task.cancel()
        self._setpoint_tasks.clear()
        self._pending_setpoints.clear()

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
    """Communicates with the EasyTouch thermostat via Bluetooth LE.

    Maintains a single persistent BLE connection for the lifetime of the
    integration.  Connecting and disconnecting on every poll causes the
    thermostat to treat each disconnect as "remote session ended" and reset
    zone states — most visibly Zone 3 randomly turning off.
    """

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

        # Persistent connection state
        self._client: Any = None          # BleakClient when connected
        self._connect_lock = asyncio.Lock()  # serialise (re)connection attempts
        self._ble_op_lock = asyncio.Lock()   # serialise reads/writes on the connection
        self._authenticated = False

        # Per-zone equipment config (from "Get Config"), fetched once
        self.zone_configs: dict[int, ZoneConfig] = {}
        self._config_attempts = 0
        # Per-zone heat-mode number to use when HA asks for plain "heat"
        self.heat_mode_preference: dict[int, int] = {}

    # ── Heat source helpers ──────────────────────────────────────────────────

    def zone_config(self, zone: int) -> ZoneConfig | None:
        zc = self.zone_configs.get(zone)
        return zc if zc and zc.known else None

    def available_heat_modes(self, zone: int) -> list[int]:
        """Device heat-mode numbers this zone supports (falls back to furnace)."""
        zc = self.zone_config(zone)
        if zc is None:
            return [DEFAULT_HEAT_MODE]
        return zc.heat_modes

    def heat_mode_for(self, zone: int) -> int | None:
        """Which device mode to send for HA's "heat" on this zone."""
        available = self.available_heat_modes(zone)
        if not available:
            return None
        zd = (self.data or {}).get(zone)
        # Already heating with a valid source → keep it
        if zd and zd.mode_num in available:
            return zd.mode_num
        pref = self.heat_mode_preference.get(zone)
        if pref in available:
            return pref
        return available[0]

    def auto_mode_for(self, zone: int) -> int:
        """Which device auto-mode number to send for HA's "auto"."""
        zc = self.zone_config(zone)
        zd = (self.data or {}).get(zone)
        if zd and zd.mode_num in BLE_AUTO_MODE_NUMS and (
            zc is None or zd.mode_num in zc.modes
        ):
            return zd.mode_num
        if zc is not None:
            for m in (11, 10, 9, 8):
                if m in zc.modes:
                    return m
        return 11

    # ── Disconnect callback (called by bleak from BLE thread) ────────────────

    def _on_disconnect(self, client: Any) -> None:
        """Bleak fires this when the connection drops unexpectedly."""
        if client is not self._client:
            return  # an old client we already dropped on purpose
        _LOGGER.warning(
            "BLE %s connection lost — will reconnect automatically on next poll",
            self._mac,
        )
        self._client = None
        self._authenticated = False

    # ── Connection management ─────────────────────────────────────────────────

    async def _ensure_connected(self) -> Any | None:
        """Return a live BleakClient, (re)connecting if necessary."""
        async with self._connect_lock:
            if self._client is not None and self._client.is_connected:
                return self._client

            # Stale reference — clear it before reconnecting
            self._client = None
            self._authenticated = False

            from homeassistant.components.bluetooth import async_ble_device_from_address
            from bleak_retry_connector import establish_connection
            from bleak import BleakClient

            ble_device = async_ble_device_from_address(
                self.hass, self._mac, connectable=True
            )
            if ble_device is None:
                _LOGGER.error(
                    "BLE device %s not found — ensure it is powered on and in range",
                    self._mac,
                )
                return None

            try:
                _LOGGER.debug("(Re)connecting to %s", self._mac)
                # Do NOT use async-with — we want to keep the connection alive
                client = await establish_connection(
                    BleakClient,
                    ble_device,
                    self._mac,
                    disconnected_callback=self._on_disconnect,
                    max_attempts=2,
                )
                self._client = client
                _LOGGER.debug("Connected to %s (persistent session)", self._mac)
                return client
            except asyncio.TimeoutError:
                _LOGGER.warning("BLE connect timed out for %s", self._mac)
                return None
            except Exception as exc:
                exc_str = str(exc)
                if "InProgress" in exc_str or "in progress" in exc_str.lower():
                    _LOGGER.warning(
                        "BLE busy for %s — close the EasyTouch app; will retry", self._mac
                    )
                else:
                    _LOGGER.error("BLE connect failed for %s: %s", self._mac, exc)
                return None

    async def _reset_connection(self) -> None:
        """Drop the current connection, actually disconnecting it.

        The thermostat only accepts one BLE connection at a time, so simply
        forgetting a client (without disconnecting) can leave a half-open
        session that blocks every later reconnect attempt.
        """
        client = self._client
        self._client = None
        self._authenticated = False
        if client is not None:
            try:
                await client.disconnect()
                _LOGGER.debug("BLE %s disconnected", self._mac)
            except Exception as exc:
                _LOGGER.debug("BLE %s disconnect error (ignored): %s", self._mac, exc)

    async def async_shutdown(self) -> None:
        """Disconnect cleanly when the integration is unloaded."""
        await super().async_shutdown()
        await self._reset_connection()

    # ── Connection test ──────────────────────────────────────────────────────

    async def test_connection(self) -> bool:
        client = await self._ensure_connected()
        if client is None:
            raise update_coordinator.UpdateFailed(
                f"BLE connection to {self._mac} failed"
            )
        async with self._ble_op_lock:
            await self._authenticate(client)
            result = await self._poll_device(client)
        if not result:
            raise update_coordinator.UpdateFailed(
                f"BLE connected but returned no data from {self._mac}"
            )
        return True

    # ── Polling ──────────────────────────────────────────────────────────────

    async def _async_update_data(self) -> ZoneData:
        """Poll the device using the persistent BLE connection."""
        client = await self._ensure_connected()
        if client is None:
            raise update_coordinator.UpdateFailed(
                f"BLE unavailable for {self._mac} — will retry in {BLE_UPDATE_INTERVAL.seconds}s"
            )

        async with self._ble_op_lock:
            if not self._authenticated:
                await self._authenticate(client)
                self._authenticated = True

            result = await self._poll_device(client)
            if result:
                try:
                    await self._load_zone_configs(client)
                except Exception as exc:  # never let config break polling
                    _LOGGER.debug("Zone config request failed: %s", exc)

        if not result:
            # Got a connection but no data — treat as stale and force reconnect
            await self._reset_connection()
            raise update_coordinator.UpdateFailed(
                f"BLE poll missed for {self._mac} — will retry in {BLE_UPDATE_INTERVAL.seconds}s"
            )
        return result

    # ── BLE communication ────────────────────────────────────────────────────

    async def _poll_device(self, client: Any) -> ZoneData:
        """Read current status from an already-connected client.

        Tries BB01 (statusOnly, direct read) first, then falls back to
        sending a Get Status command via EE01 and reading the response from FF01.
        """
        # ── Try BB01 (statusOnly) — direct read, no command needed ───────────
        raw_status = await self._read_status_only(client)
        if raw_status is not None:
            _LOGGER.debug("BB01 status: %s", raw_status)
            return self._parse_all_zones(raw_status)

        # ── Fall back: Get Status via EE01 / FF01 ────────────────────────────
        command: dict[str, Any] = {"Type": "Get Status", "Zone": 0, "TM": int(time.time())}
        if self._email:
            command["EM"] = self._email
        raw_status = await self._request(client, command)
        if raw_status is None:
            return {}

        _LOGGER.debug("FF01 status: %s", raw_status)
        return self._parse_all_zones(raw_status)

    async def _load_zone_configs(self, client: Any) -> None:
        """Ask each zone what equipment it has ("Get Config"), once.

        The EasyTouch app sends {"Type": "Get Config", "Zone": n} per zone and
        reads the zone's available modes (heat pump, furnace, heat strip...)
        from the reply. Best-effort: on failure we keep the default modes and
        try again on the next poll, up to 3 times.
        """
        if self._config_attempts >= 3 or all(
            z in self.zone_configs for z in range(self._zone_count)
        ):
            return
        self._config_attempts += 1
        for zone in range(self._zone_count):
            if zone in self.zone_configs:
                continue
            reply = await self._request(client, {"Type": "Get Config", "Zone": zone})
            if not reply or ("CFG" not in reply and reply.get("RT") != "Config"):
                _LOGGER.debug("Zone %d: no config reply (%r)", zone, reply)
                continue
            zc = parse_zone_config(zone, reply)
            # INFO so users can share it when reporting unusual equipment
            _LOGGER.info(
                "Zone %d config: modes=%s heat=%s setpoint limits=%s/%s cool, %s/%s heat (raw %s)",
                zone + 1, sorted(zc.modes), zc.heat_modes,
                zc.min_cool, zc.max_cool, zc.min_heat, zc.max_heat, zc.raw,
            )
            if not zc.known:
                _LOGGER.warning(
                    "Zone %d: couldn't read its equipment from the thermostat's "
                    "config reply, so all modes are offered. Please report this "
                    "line: %s", zone + 1, reply,
                )
            self.zone_configs[zone] = zc

    async def _request(self, client: Any, command: dict[str, Any]) -> dict[str, Any] | None:
        """Write a JSON command to EE01 and return the JSON reply from FF01."""
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
            return await self._send_and_read(
                client, command, chunks, response_ready, using_notify
            )
        finally:
            if using_notify:
                try:
                    await client.stop_notify(BLE_JSON_RETURN_UUID)
                except Exception:
                    pass

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

    async def _send_and_read(
        self,
        client: Any,
        command: dict[str, Any],
        chunks: list[bytes],
        response_ready: asyncio.Event,
        using_notify: bool,
    ) -> dict[str, Any] | None:
        """Send one JSON command and return the parsed JSON response."""
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
        """Send a Change command using the persistent BLE connection."""
        client = await self._ensure_connected()
        if client is None:
            _LOGGER.error("Cannot send command — BLE device %s not connected", self._mac)
            return False

        try:
            async with self._ble_op_lock:
                if not self._authenticated:
                    await self._authenticate(client)
                    self._authenticated = True
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
                # Force reconnect on next operation
                await self._reset_connection()
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

            if not isinstance(info, list) or len(info) < BLE_MIN_ZONE_FIELDS:
                # Don't report made-up defaults (mode "off", fan "auto") for a
                # zone we didn't get real data for — leave it unavailable.
                _LOGGER.warning(
                    "Zone %s has incomplete data (%s) — skipping",
                    zone_key, info,
                )
                continue

            zd = MicroAirData(zone=zone, available_fan_modes=list(BLE_FAN_MODES_STANDARD))

            try:
                zd.current_temp = _safe(info, BLE_IDX_TEMPERATURE)

                mode_num = _int_or(_safe(info, BLE_IDX_MODE_NUM), 0)
                zd.mode_num = mode_num
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
                    fan_num = _int_or(_safe(info, BLE_IDX_FAN_ONLY_MODE), 0)
                    zd.available_fan_modes = ["off", "low", "high"]
                elif zd.hvac_mode == HVACMode.COOL:
                    fan_num = _int_or(_safe(info, BLE_IDX_COOL_FAN_MODE), 128)
                elif zd.hvac_mode == HVACMode.HEAT:
                    fan_num = _int_or(_safe(info, BLE_IDX_HEAT_FAN_MODE), 128)
                else:
                    fan_num = _int_or(_safe(info, BLE_IDX_AUTO_FAN_MODE), 128)
                zd.fan_mode = BLE_FAN_MODE_DEVICE_TO_HA.get(fan_num, "auto")

                # HVAC action from current_mode_num
                cur_num = _int_or(_safe(info, BLE_IDX_CURRENT_MODE), 0)
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
                    if ct is not None and high is not None and ct > high:
                        zd.hvac_action = HVACAction.COOLING
                    elif ct is not None and low is not None and ct < low:
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
        """Change one zone's mode.

        Off is sent as `mode: 0` ("Zone Off") WITHOUT a `power` key —
        `power: 0` is system-wide and would shut down every zone.
        """
        from .const import BLE_HVAC_HA_TO_DEVICE

        if hvac_mode == HVACMode.OFF:
            changes: dict[str, Any] = {"zone": zone, "mode": BLE_MODE_ZONE_OFF}
        else:
            if hvac_mode == HVACMode.HEAT:
                device_mode = self.heat_mode_for(zone)
                if device_mode is None:
                    _LOGGER.warning("Zone %d has no heat source configured", zone + 1)
                    return
            elif hvac_mode in (HVACMode.AUTO, HVACMode.HEAT_COOL):
                device_mode = self.auto_mode_for(zone)
            else:
                device_mode = BLE_HVAC_HA_TO_DEVICE.get(hvac_mode.value, 0)
            # power:1 makes sure the system is on so the zone can actually run
            changes = {"zone": zone, "power": 1, "mode": device_mode}

        command = {"Type": "Change", "Zone": zone, "Changes": changes}
        if await self._send_command(command):
            await self.async_request_refresh()

    async def async_set_heat_source(self, zone: int, heat_mode: int) -> None:
        """Remember the zone's preferred heat source; switch now if it's heating."""
        self.heat_mode_preference[zone] = heat_mode
        zd = (self.data or {}).get(zone)
        if zd and zd.hvac_mode == HVACMode.HEAT and zd.mode_num != heat_mode:
            command = {
                "Type": "Change",
                "Zone": zone,
                "Changes": {"zone": zone, "power": 1, "mode": heat_mode},
            }
            if await self._send_command(command):
                await self.async_request_refresh()
        else:
            self.async_update_listeners()

    async def async_set_system_power(self, on: bool) -> None:
        """System-wide power — turns every zone on or off together."""
        command = {
            "Type": "Change",
            "Zone": 0,
            "Changes": {"zone": 0, "power": 1 if on else 0},
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
        # No `power` key: changing a setpoint shouldn't switch the system on.
        changes: dict[str, Any] = {"zone": zone}
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

        if len(changes) > 1:
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
