"""Button entities for Micro-Air EasyTouch (BLE only).

- Reboot the thermostat
- System On / System Off — whole-system power for every zone at once.
  (A climate entity's "off" now turns off only its own zone.)
"""
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
    async_add_entities([
        MicroAirRebootButton(coordinator, entry, mac),
        MicroAirSystemPowerButton(coordinator, entry, mac, on=False),
        MicroAirSystemPowerButton(coordinator, entry, mac, on=True),
    ])


class _MicroAirButton(CoordinatorEntity, ButtonEntity):
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: MicroAirBLECoordinator, entry: ConfigEntry, mac: str
    ) -> None:
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            name=entry.title,
            manufacturer="Micro-Air",
            model="EasyTouch RV Thermostat",
        )


class MicroAirRebootButton(_MicroAirButton):
    """Button that sends a reboot command to the thermostat via BLE."""

    _attr_name = "Reboot"
    _attr_icon = "mdi:restart"

    def __init__(self, coordinator, entry, mac) -> None:
        super().__init__(coordinator, entry, mac)
        self._attr_unique_id = f"{DOMAIN}_{mac}_reboot"

    async def async_press(self) -> None:
        """Send the reboot command."""
        await self.coordinator.async_reboot_device()


class MicroAirSystemPowerButton(_MicroAirButton):
    """Turn the whole system (all zones) on or off."""

    def __init__(self, coordinator, entry, mac, on: bool) -> None:
        super().__init__(coordinator, entry, mac)
        self._on = on
        self._attr_name = "System on" if on else "System off"
        self._attr_icon = "mdi:power-on" if on else "mdi:power-off"
        self._attr_unique_id = f"{DOMAIN}_{mac}_system_{'on' if on else 'off'}"

    async def async_press(self) -> None:
        await self.coordinator.async_set_system_power(self._on)
