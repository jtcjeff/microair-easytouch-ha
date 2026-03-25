"""Micro-Air EasyTouch Home Assistant Integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CONNECTION_TYPE,
    CONF_EMAIL,
    CONF_IP_ADDRESS,
    CONF_MAC_ADDRESS,
    CONF_PASSWORD,
    CONF_ZONE_COUNT,
    CONN_TYPE_BLE,
    CONN_TYPE_WIFI,
    DEFAULT_ZONE_COUNT,
    DOMAIN,
    PLATFORMS_BLE,
    PLATFORMS_WIFI,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Micro-Air EasyTouch from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    conn_type = entry.data.get(CONF_CONNECTION_TYPE, CONN_TYPE_WIFI)
    zone_count = int(entry.data.get(CONF_ZONE_COUNT, DEFAULT_ZONE_COUNT))

    _LOGGER.debug(
        "Setting up %s — connection=%s, zones=%d", entry.title, conn_type, zone_count
    )

    if conn_type == CONN_TYPE_WIFI:
        from .coordinator import MicroAirWiFiCoordinator

        coordinator = MicroAirWiFiCoordinator(
            hass,
            ip_address=entry.data[CONF_IP_ADDRESS],
            name=entry.title,
            zone_count=zone_count,
        )
        platforms = PLATFORMS_WIFI

    else:  # BLE
        from .coordinator import MicroAirBLECoordinator

        coordinator = MicroAirBLECoordinator(
            hass,
            mac_address=entry.data[CONF_MAC_ADDRESS],
            password=entry.data[CONF_PASSWORD],
            email=entry.data[CONF_EMAIL],
            name=entry.title,
            zone_count=zone_count,
        )
        platforms = PLATFORMS_BLE

    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "connection_type": conn_type,
        "zone_count": zone_count,
    }

    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    conn_type = entry.data.get(CONF_CONNECTION_TYPE, CONN_TYPE_WIFI)
    platforms = PLATFORMS_WIFI if conn_type == CONN_TYPE_WIFI else PLATFORMS_BLE

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, platforms):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
