"""The GTFS integration."""
from __future__ import annotations

import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall

from .const import DOMAIN, PLATFORMS, DEFAULT_PATH
from .coordinator import GTFSUpdateCoordinator
import voluptuous as vol
from .gtfs_helper import get_gtfs

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up GTFS from a config entry."""

    hass.data.setdefault(DOMAIN, {})

    coordinator = GTFSUpdateCoordinator(hass, entry)

    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


def setup(hass, config):
    """Setup the service example component."""

    def update_gtfs(call):
        """My GTFS service."""
        _LOGGER.debug("Updating GTFS with: %s", call.data)
        get_gtfs(hass, DEFAULT_PATH, call.data["name"], call.data["url"], True)
        return True

    hass.services.register(
        DOMAIN, "update_gtfs", update_gtfs)
    return True
