"""Button entity for Micro-Air EasyTouch — reboot command (BLE only)."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_MAC_ADDRESS, DOMAIN
from .coordinator import MicroAirBLECoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: MicroAirBLECoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    mac = entry.data[CONF_MAC_ADDRESS]
    async_add_entities([MicroAirRebootButton(coordinator, entry, mac)])


class MicroAirRebootButton(CoordinatorEntity, ButtonEntity):
    """Button that sends a reboot command to the thermostat via BLE."""

    _attr_has_entity_name = True
    _attr_name = "Reboot"
    _attr_icon = "mdi:restart"

    def __init__(
        self,
        coordinator: MicroAirBLECoordinator,
        entry: ConfigEntry,
        mac: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{mac}_reboot"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            name=entry.title,
            manufacturer="Micro-Air",
            model="EasyTouch RV Thermostat",
        )

    async def async_press(self) -> None:
        """Send the reboot command."""
        await self.coordinator.async_reboot_device()
