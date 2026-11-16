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
    BLE_AUTO_MODE_NUMS,
    CONN_TYPE_BLE,
    CONN_TYPE_WIFI,
    DOMAIN,
    HEAT_SOURCE_BY_MODE,
    HEAT_SOURCE_LABELS,
    MODE_BY_HEAT_SOURCE,
)

LABEL_TO_HEAT_SOURCE = {label: key for key, label in HEAT_SOURCE_LABELS.items()}
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

        if conn_type == CONN_TYPE_WIFI:
            # WiFi has no fan-only command, and its Auto mode uses a single
            # setpoint (the thermostat reports and accepts just one value).
            self._attr_hvac_modes = [
                m for m in self._attr_hvac_modes if m != HVACMode.FAN_ONLY
            ]
            self._attr_supported_features = (
                ClimateEntityFeature.TARGET_TEMPERATURE
                | ClimateEntityFeature.FAN_MODE
                | ClimateEntityFeature.TURN_ON
                | ClimateEntityFeature.TURN_OFF
            )
        else:
            self._attr_supported_features = (
                ClimateEntityFeature.TARGET_TEMPERATURE
                | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
                | ClimateEntityFeature.FAN_MODE
                | ClimateEntityFeature.TURN_ON
                | ClimateEntityFeature.TURN_OFF
            )

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

    def _ble_zone_config(self):
        if self._conn_type != CONN_TYPE_BLE:
            return None
        return self.coordinator.zone_config(self._zone)

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Only offer modes this zone's equipment supports (when known)."""
        zc = self._ble_zone_config()
        if zc is None:
            return self._attr_hvac_modes
        modes = [HVACMode.OFF]
        if 2 in zc.modes:
            modes.append(HVACMode.COOL)
        if zc.heat_modes:
            modes.append(HVACMode.HEAT)
        if any(m in zc.modes for m in BLE_AUTO_MODE_NUMS):
            modes.append(HVACMode.AUTO)
        if 6 in zc.modes:
            modes.append(HVACMode.DRY)
        if 1 in zc.modes:
            modes.append(HVACMode.FAN_ONLY)
        return modes

    # ── Heat source as a preset (shows on the thermostat card) ───────────────

    def _heat_source_labels(self) -> list[str]:
        if self._conn_type != CONN_TYPE_BLE:
            return []
        return [
            HEAT_SOURCE_LABELS[HEAT_SOURCE_BY_MODE[m]]
            for m in self.coordinator.available_heat_modes(self._zone)
            if m in HEAT_SOURCE_BY_MODE
        ]

    @property
    def supported_features(self) -> ClimateEntityFeature:
        features = self._attr_supported_features
        if len(self._heat_source_labels()) > 1:
            features |= ClimateEntityFeature.PRESET_MODE
        return features

    @property
    def preset_modes(self) -> list[str] | None:
        labels = self._heat_source_labels()
        return labels if len(labels) > 1 else None

    @property
    def preset_mode(self) -> str | None:
        if not self.preset_modes:
            return None
        mode = self.coordinator.heat_mode_for(self._zone)
        if mode not in HEAT_SOURCE_BY_MODE:
            return None
        return HEAT_SOURCE_LABELS[HEAT_SOURCE_BY_MODE[mode]]

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        source = LABEL_TO_HEAT_SOURCE.get(preset_mode)
        if source is None or preset_mode not in (self.preset_modes or []):
            raise ValueError(f"Unknown heat source: {preset_mode}")
        await self.coordinator.async_set_heat_source(
            self._zone, MODE_BY_HEAT_SOURCE[source]
        )

    @property
    def min_temp(self) -> float:
        zc = self._ble_zone_config()
        limits = [v for v in ((zc.min_cool, zc.min_heat) if zc else ()) if v]
        return float(min(limits)) if limits else TEMP_MIN

    @property
    def max_temp(self) -> float:
        zc = self._ble_zone_config()
        limits = [v for v in ((zc.max_cool, zc.max_heat) if zc else ()) if v]
        return float(max(limits)) if limits else TEMP_MAX

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
        if self._uses_range(zd):
            return None
        return zd.setpoint

    def _uses_range(self, zd: MicroAirData) -> bool:
        """BLE Auto mode has separate heat/cool setpoints; WiFi Auto has one."""
        return self._conn_type == CONN_TYPE_BLE and zd.hvac_mode in (
            HVACMode.AUTO, HVACMode.HEAT_COOL
        )

    @property
    def target_temperature_high(self) -> float | None:
        zd = self._zone_data
        if zd is None or not self._uses_range(zd):
            return None
        return zd.setpoint_high

    @property
    def target_temperature_low(self) -> float | None:
        zd = self._zone_data
        if zd is None or not self._uses_range(zd):
            return None
        return zd.setpoint_low

    @property
    def fan_mode(self) -> str | None:
        zd = self._zone_data
        return zd.fan_mode if zd else None

    @property
    def fan_modes(self) -> list[str]:
        zd = self._zone_data
        if zd:
            return zd.available_fan_modes
        if self._conn_type == CONN_TYPE_WIFI:
            return ["off", "low", "medium", "high", "auto"]
        return ["off", "low", "high", "auto"]

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
            if zd.mode_num is not None:
                attrs["device_mode"] = zd.mode_num
                if zd.mode_num in HEAT_SOURCE_BY_MODE:
                    attrs["heat_source"] = HEAT_SOURCE_BY_MODE[zd.mode_num]
        zc = self._ble_zone_config()
        if zc is not None:
            attrs["available_heat_sources"] = [
                HEAT_SOURCE_BY_MODE[m] for m in zc.heat_modes
            ]
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
