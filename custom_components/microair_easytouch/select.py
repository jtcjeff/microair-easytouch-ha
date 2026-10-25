"""Equipment-mode selector for BLE Micro-Air EasyTouch devices."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.components.select import SelectEntity

from .const import BLE_EQUIPMENT_MODE_LABELS, CONF_MAC_ADDRESS, DOMAIN
from .coordinator import MicroAirBLECoordinator, MicroAirData

EQUIPMENT_MODE_LABELS = BLE_EQUIPMENT_MODE_LABELS


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    if not isinstance(coordinator, MicroAirBLECoordinator):
        return

    mac = entry.data[CONF_MAC_ADDRESS]
    zones = sorted((coordinator.data or {}).keys())
    async_add_entities(
        MicroAirEquipmentSelect(coordinator, entry, mac, zone)
        for zone in zones
    )


class MicroAirEquipmentSelect(CoordinatorEntity, SelectEntity):
    """Select the EasyTouch heating/cooling equipment source for a zone."""

    _attr_has_entity_name = True
    _attr_name = "Equipment Mode"
    _attr_icon = "mdi:hvac"
    _attr_options = list(EQUIPMENT_MODE_LABELS.values())

    def __init__(
        self,
        coordinator: MicroAirBLECoordinator,
        entry: ConfigEntry,
        mac: str,
        zone: int,
    ) -> None:
        super().__init__(coordinator)
        self._zone = zone
        self._attr_unique_id = f"{DOMAIN}_{mac}_zone{zone}_equipment_mode"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            name=entry.title,
            manufacturer="Micro-Air",
            model="EasyTouch RV Thermostat",
        )

    @property
    def _zone_data(self) -> MicroAirData | None:
        return (self.coordinator.data or {}).get(self._zone)

    @property
    def current_option(self) -> str | None:
        zd = self._zone_data
        if zd is None:
            return None
        return EQUIPMENT_MODE_LABELS.get(zd.equipment_mode or "")

    async def async_select_option(self, option: str) -> None:
        equipment_mode = next(
            (key for key, label in EQUIPMENT_MODE_LABELS.items() if label == option),
            None,
        )
        if equipment_mode is None:
            return
        await self.coordinator.async_set_equipment_mode(equipment_mode, zone=self._zone)
