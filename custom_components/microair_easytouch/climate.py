"""Climate entity for Micro-Air EasyTouch — one entity per zone."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CONNECTION_TYPE,
    CONF_IP_ADDRESS,
    CONF_MAC_ADDRESS,
    CONN_TYPE_BLE,
    CONN_TYPE_WIFI,
    DOMAIN,
)
from .coordinator import MicroAirBLECoordinator, MicroAirData, MicroAirWiFiCoordinator

_LOGGER = logging.getLogger(__name__)

TEMP_MIN = 60.0
TEMP_MAX = 90.0
TEMP_STEP = 1.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    conn_type = entry.data.get(CONF_CONNECTION_TYPE, CONN_TYPE_WIFI)
    zone_count = entry_data["zone_count"]

    device_id = (
        entry.data.get(CONF_IP_ADDRESS)
        if conn_type == CONN_TYPE_WIFI
        else entry.data.get(CONF_MAC_ADDRESS)
    )

    entities = [
        MicroAirClimate(coordinator, entry, device_id, conn_type, zone)
        for zone in range(zone_count)
    ]
    async_add_entities(entities)


class MicroAirClimate(CoordinatorEntity, ClimateEntity):
    """Climate entity for a single zone, backed by either coordinator."""

    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_target_temperature_step = TEMP_STEP
    _attr_min_temp = TEMP_MIN
    _attr_max_temp = TEMP_MAX

    _attr_hvac_modes = [
        HVACMode.OFF,
        HVACMode.COOL,
        HVACMode.HEAT,
        HVACMode.AUTO,
        HVACMode.DRY,
        HVACMode.FAN_ONLY,
    ]

    def __init__(
        self,
        coordinator: MicroAirWiFiCoordinator | MicroAirBLECoordinator,
        entry: ConfigEntry,
        device_id: str,
        conn_type: str,
        zone: int,
    ) -> None:
        super().__init__(coordinator)
        self._zone = zone
        self._conn_type = conn_type

        # Zone 0 → "Zone 1", zone 1 → "Zone 2", etc.
        self._attr_name = f"Zone {zone + 1}"
        self._attr_unique_id = f"{DOMAIN}_{device_id}_zone{zone}_climate"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=entry.title,
            manufacturer="Micro-Air",
            model="EasyTouch RV Thermostat",
        )

    # ── Helpers ──────────────────────────────────────────────────────────────

    @property
    def _zone_data(self) -> MicroAirData | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._zone)

    # ── Supported features ───────────────────────────────────────────────────

    @property
    def supported_features(self) -> ClimateEntityFeature:
        return (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
            | ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )

    @property
    def available(self) -> bool:
        return super().available and self._zone_data is not None

    # ── State properties ─────────────────────────────────────────────────────

    @property
    def hvac_mode(self) -> HVACMode:
        zd = self._zone_data
        return zd.hvac_mode if zd else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction | None:
        zd = self._zone_data
        return zd.hvac_action if zd else None

    @property
    def current_temperature(self) -> float | None:
        zd = self._zone_data
        return zd.current_temp if zd else None

    @property
    def target_temperature(self) -> float | None:
        zd = self._zone_data
        if zd is None:
            return None
        if zd.hvac_mode in (HVACMode.AUTO, HVACMode.HEAT_COOL):
            return None
        return zd.setpoint

    @property
    def target_temperature_high(self) -> float | None:
        zd = self._zone_data
        if zd is None or zd.hvac_mode not in (HVACMode.AUTO, HVACMode.HEAT_COOL):
            return None
        return zd.setpoint_high

    @property
    def target_temperature_low(self) -> float | None:
        zd = self._zone_data
        if zd is None or zd.hvac_mode not in (HVACMode.AUTO, HVACMode.HEAT_COOL):
            return None
        return zd.setpoint_low

    @property
    def fan_mode(self) -> str | None:
        zd = self._zone_data
        return zd.fan_mode if zd else None

    @property
    def fan_modes(self) -> list[str]:
        zd = self._zone_data
        return zd.available_fan_modes if zd else ["off", "low", "high", "auto"]

    @property
    def icon(self) -> str:
        icons = {
            HVACMode.OFF:      "mdi:power",
            HVACMode.HEAT:     "mdi:fire",
            HVACMode.COOL:     "mdi:snowflake",
            HVACMode.AUTO:     "mdi:autorenew",
            HVACMode.HEAT_COOL:"mdi:autorenew",
            HVACMode.FAN_ONLY: "mdi:fan",
            HVACMode.DRY:      "mdi:water-percent",
        }
        return icons.get(self.hvac_mode, "mdi:thermostat")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {"zone": self._zone, "connection_type": self._conn_type}
        zd = self._zone_data
        if zd:
            attrs["fan_state"] = zd.fan_state
            if zd.line_voltage is not None:
                attrs["line_voltage"] = zd.line_voltage
        return attrs

    # ── Commands ─────────────────────────────────────────────────────────────

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self.coordinator.async_set_hvac_mode(hvac_mode, zone=self._zone)

    async def async_turn_on(self) -> None:
        zd = self._zone_data
        last = zd.hvac_mode if zd else HVACMode.OFF
        target = last if last != HVACMode.OFF else HVACMode.COOL
        await self.coordinator.async_set_hvac_mode(target, zone=self._zone)

    async def async_turn_off(self) -> None:
        await self.coordinator.async_set_hvac_mode(HVACMode.OFF, zone=self._zone)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        temp_high = kwargs.get("target_temp_high")
        temp_low = kwargs.get("target_temp_low")

        if isinstance(self.coordinator, MicroAirWiFiCoordinator):
            if temp is not None:
                await self.coordinator.async_set_temperature(temp, zone=self._zone)
        else:
            await self.coordinator.async_set_temperature(
                zone=self._zone,
                setpoint=temp,
                setpoint_high=temp_high,
                setpoint_low=temp_low,
            )

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        if isinstance(self.coordinator, MicroAirWiFiCoordinator):
            await self.coordinator.async_set_fan_mode(fan_mode, zone=self._zone)
        else:
            await self.coordinator.async_set_fan_mode(
                fan_mode, zone=self._zone, hvac_mode=self.hvac_mode
            )
