"""Sensor entities for Micro-Air EasyTouch — one set per zone."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CONNECTION_TYPE,
    CONF_IP_ADDRESS,
    CONF_MAC_ADDRESS,
    CONN_TYPE_WIFI,
    DOMAIN,
)
from .coordinator import MicroAirBLECoordinator, MicroAirData, MicroAirWiFiCoordinator


@dataclass(frozen=True, kw_only=True)
class MicroAirSensorDescription(SensorEntityDescription):
    value_fn: Any = None   # Callable[[MicroAirData], float | None]


SENSOR_DESCRIPTIONS: tuple[MicroAirSensorDescription, ...] = (
    MicroAirSensorDescription(
        key="current_temperature",
        name="Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        icon="mdi:thermometer",
        value_fn=lambda d: d.current_temp,
    ),
)


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
        MicroAirSensor(coordinator, entry, device_id, description, zone)
        for zone in range(zone_count)
        for description in SENSOR_DESCRIPTIONS
    ]
    async_add_entities(entities)


class MicroAirSensor(CoordinatorEntity, SensorEntity):
    """A sensor for one zone, backed by the shared coordinator."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MicroAirWiFiCoordinator | MicroAirBLECoordinator,
        entry: ConfigEntry,
        device_id: str,
        description: MicroAirSensorDescription,
        zone: int,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._zone = zone
        # "Zone 1 Temperature", "Zone 2 Humidity", etc.
        self._attr_name = f"Zone {zone + 1} {description.name}"
        self._attr_unique_id = f"{DOMAIN}_{device_id}_zone{zone}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=entry.title,
            manufacturer="Micro-Air",
            model="EasyTouch RV Thermostat",
        )

    @property
    def _zone_data(self) -> MicroAirData | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._zone)

    @property
    def native_value(self) -> float | None:
        zd = self._zone_data
        return self.entity_description.value_fn(zd) if zd else None

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        zd = self._zone_data
        return zd is not None and self.entity_description.value_fn(zd) is not None
