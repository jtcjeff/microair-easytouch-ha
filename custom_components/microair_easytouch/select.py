"""Per-zone heat source select for Micro-Air EasyTouch (BLE only).

Each zone's equipment is read from the thermostat ("Get Config") at startup.
Zones with a furnace and a heat pump / heat strip get a choice of which one
Home Assistant's "Heat" mode uses. Picking a source while the zone is heating
switches it immediately; otherwise it's remembered for the next time the zone
is set to Heat.
"""
from __future__ import annotations

from homeassistant.components.climate import HVACMode
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_MAC_ADDRESS,
    DOMAIN,
    HEAT_SOURCE_BY_MODE,
    HEAT_SOURCE_LABELS,
    MODE_BY_HEAT_SOURCE,
)
from .coordinator import MicroAirBLECoordinator

LABEL_TO_SOURCE = {label: key for key, label in HEAT_SOURCE_LABELS.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: MicroAirBLECoordinator = entry_data["coordinator"]
    mac = entry.data[CONF_MAC_ADDRESS]
    async_add_entities(
        MicroAirHeatSourceSelect(coordinator, entry, mac, zone)
        for zone in range(entry_data["zone_count"])
    )


class MicroAirHeatSourceSelect(CoordinatorEntity, SelectEntity, RestoreEntity):
    """Which heat source a zone uses when set to Heat."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:fire-circle"

    def __init__(
        self,
        coordinator: MicroAirBLECoordinator,
        entry: ConfigEntry,
        mac: str,
        zone: int,
    ) -> None:
        super().__init__(coordinator)
        self._zone = zone
        self._attr_name = f"Zone {zone + 1} Heat source"
        self._attr_unique_id = f"{DOMAIN}_{mac}_zone{zone}_heat_source"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            name=entry.title,
            manufacturer="Micro-Air",
            model="EasyTouch RV Thermostat",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Restore the user's last choice so "Heat" keeps using it after a restart
        last = await self.async_get_last_state()
        if last and last.state in LABEL_TO_SOURCE:
            mode = MODE_BY_HEAT_SOURCE[LABEL_TO_SOURCE[last.state]]
            self.coordinator.heat_mode_preference.setdefault(self._zone, mode)

    @property
    def options(self) -> list[str]:
        return [
            HEAT_SOURCE_LABELS[HEAT_SOURCE_BY_MODE[m]]
            for m in self.coordinator.available_heat_modes(self._zone)
            if m in HEAT_SOURCE_BY_MODE
        ]

    @property
    def current_option(self) -> str | None:
        mode = self.coordinator.heat_mode_for(self._zone)
        if mode is None or mode not in HEAT_SOURCE_BY_MODE:
            return None
        return HEAT_SOURCE_LABELS[HEAT_SOURCE_BY_MODE[mode]]

    @property
    def available(self) -> bool:
        return (
            super().available
            and (self.coordinator.data or {}).get(self._zone) is not None
            and bool(self.options)
        )

    @property
    def extra_state_attributes(self) -> dict:
        zd = (self.coordinator.data or {}).get(self._zone)
        return {
            "zone": self._zone,
            "detected_from_thermostat": self.coordinator.zone_config(self._zone) is not None,
            "zone_is_heating_mode": bool(zd and zd.hvac_mode == HVACMode.HEAT),
        }

    async def async_select_option(self, option: str) -> None:
        source = LABEL_TO_SOURCE.get(option)
        if source is None:
            return
        await self.coordinator.async_set_heat_source(
            self._zone, MODE_BY_HEAT_SOURCE[source]
        )
