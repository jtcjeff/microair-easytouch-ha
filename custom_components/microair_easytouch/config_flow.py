"""Config flow for Micro-Air EasyTouch.

Supports two setup paths:
  • WiFi  — enter IP address (and optional device name)
  • BLE   — select a discovered device, enter account password + email
            OR triggered automatically when HA detects an EasyTouch advertisement

Both paths include a zone count step (1–3 zones).
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.const import CONF_NAME, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_CONNECTION_TYPE,
    CONF_EMAIL,
    CONF_IP_ADDRESS,
    CONF_MAC_ADDRESS,
    CONF_ZONE_COUNT,
    CONN_TYPE_BLE,
    CONN_TYPE_WIFI,
    DEFAULT_ZONE_COUNT,
    DOMAIN,
    MAX_ZONE_COUNT,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONNECTION_TYPE, default=CONN_TYPE_WIFI): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(value=CONN_TYPE_WIFI, label="WiFi (local IP)"),
                    selector.SelectOptionDict(value=CONN_TYPE_BLE, label="Bluetooth"),
                ],
            )
        ),
    }
)

STEP_WIFI_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default="EasyTouch Thermostat"): str,
        vol.Required(CONF_IP_ADDRESS): str,
    }
)

STEP_BLE_CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_EMAIL, default=""): str,
        vol.Optional(CONF_PASSWORD, default=""): str,
    }
)

STEP_ZONE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ZONE_COUNT, default=DEFAULT_ZONE_COUNT): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1,
                max=MAX_ZONE_COUNT,
                step=1,
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
    }
)


class MicroAirConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Micro-Air EasyTouch."""

    VERSION = 1

    def __init__(self) -> None:
        self._connection_type: str | None = None
        self._partial_data: dict[str, Any] = {}
        # BLE discovery state
        self._discovered_devices: dict[str, str] = {}   # mac → name
        self._selected_mac: str | None = None
        self._selected_name: str | None = None

    # ── Step 1 — choose connection type ─────────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            self._connection_type = user_input[CONF_CONNECTION_TYPE]
            if self._connection_type == CONN_TYPE_WIFI:
                return await self.async_step_wifi()
            return await self.async_step_ble_pick()

        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA)

    # ── WiFi path ────────────────────────────────────────────────────────────

    async def async_step_wifi(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            ip = user_input[CONF_IP_ADDRESS].strip()
            name = user_input[CONF_NAME].strip()

            await self.async_set_unique_id(f"wifi_{ip}")
            self._abort_if_unique_id_configured()

            try:
                session = async_get_clientsession(self.hass)
                resp = await session.post(f"http://{ip}/ShortStatus", timeout=10)
                if resp.status != 200:
                    errors["base"] = "cannot_connect"
                else:
                    raw = await resp.text()
                    _LOGGER.debug("ShortStatus raw response: %s", raw)
            except Exception as exc:
                _LOGGER.warning("WiFi connection test failed: %s", exc)
                errors["base"] = "cannot_connect"

            if not errors:
                self._partial_data = {
                    CONF_CONNECTION_TYPE: CONN_TYPE_WIFI,
                    CONF_IP_ADDRESS: ip,
                    CONF_NAME: name,
                }
                return await self.async_step_zones()

        return self.async_show_form(
            step_id="wifi",
            data_schema=STEP_WIFI_SCHEMA,
            errors=errors,
        )

    # ── BLE path — device picker ─────────────────────────────────────────────

    async def async_step_ble_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._selected_mac = user_input[CONF_MAC_ADDRESS]
            self._selected_name = self._discovered_devices.get(
                self._selected_mac, f"EasyTouch {self._selected_mac[-5:]}"
            )
            return await self.async_step_ble_credentials()

        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.name and info.name.startswith("EasyTouch"):
                self._discovered_devices[info.address] = info.name

        if not self._discovered_devices:
            errors["base"] = "no_devices_found"

        device_options = [
            selector.SelectOptionDict(value=mac, label=f"{name} ({mac})")
            for mac, name in self._discovered_devices.items()
        ]

        schema = (
            vol.Schema(
                {
                    vol.Required(CONF_MAC_ADDRESS): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=device_options)
                    )
                }
            )
            if device_options
            else vol.Schema({vol.Required(CONF_MAC_ADDRESS): str})
        )

        return self.async_show_form(
            step_id="ble_pick",
            data_schema=schema,
            errors=errors,
            description_placeholders={"count": str(len(self._discovered_devices))},
        )

    # ── BLE path — credentials ───────────────────────────────────────────────

    async def async_step_ble_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]
            mac = self._selected_mac
            name = self._selected_name or f"EasyTouch {mac}"

            await self.async_set_unique_id(f"ble_{mac}")
            self._abort_if_unique_id_configured()

            try:
                from .coordinator import MicroAirBLECoordinator
                coordinator = MicroAirBLECoordinator(
                    self.hass, mac, password, email, name, zone_count=1
                )
                await coordinator.test_connection()
            except Exception as exc:
                _LOGGER.warning("BLE connection test failed: %s", exc)
                errors["base"] = "cannot_connect"

            if not errors:
                self._partial_data = {
                    CONF_CONNECTION_TYPE: CONN_TYPE_BLE,
                    CONF_MAC_ADDRESS: mac,
                    CONF_EMAIL: email,
                    CONF_PASSWORD: password,
                    CONF_NAME: name,
                }
                return await self.async_step_zones()

        return self.async_show_form(
            step_id="ble_credentials",
            data_schema=STEP_BLE_CREDENTIALS_SCHEMA,
            errors=errors,
            description_placeholders={"mac": self._selected_mac or ""},
        )

    # ── Shared — zone count ──────────────────────────────────────────────────

    async def async_step_zones(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask how many zones this thermostat has."""
        if user_input is not None:
            zone_count = int(user_input[CONF_ZONE_COUNT])
            return self.async_create_entry(
                title=self._partial_data.get(CONF_NAME, "EasyTouch Thermostat"),
                data={**self._partial_data, CONF_ZONE_COUNT: zone_count},
            )

        return self.async_show_form(
            step_id="zones",
            data_schema=STEP_ZONE_SCHEMA,
            description_placeholders={"max": str(MAX_ZONE_COUNT)},
        )

    # ── Bluetooth auto-discovery ─────────────────────────────────────────────

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Triggered when HA auto-discovers an EasyTouch advertisement."""
        await self.async_set_unique_id(f"ble_{discovery_info.address}")
        self._abort_if_unique_id_configured()

        self._selected_mac = discovery_info.address
        self._selected_name = discovery_info.name or f"EasyTouch {discovery_info.address[-5:]}"
        self._connection_type = CONN_TYPE_BLE

        self.context["title_placeholders"] = {"name": self._selected_name}
        return await self.async_step_ble_credentials()
