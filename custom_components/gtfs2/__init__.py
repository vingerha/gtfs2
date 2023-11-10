"""The GTFS integration."""
from __future__ import annotations

import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall

from datetime import timedelta

from .const import DOMAIN, PLATFORMS, DEFAULT_PATH, DEFAULT_REFRESH_INTERVAL
from .coordinator import GTFSUpdateCoordinator
import voluptuous as vol
from .gtfs_helper import get_gtfs

_LOGGER = logging.getLogger(__name__)

async def async_migrate_entry(hass, config_entry: ConfigEntry) -> bool:
    """Migrate old entry."""
    _LOGGER.debug("Migrating from version %s", config_entry.version)

    if config_entry.version == 1:

        new = {**config_entry.data}
        new['extract_from'] = 'url'
        new.pop('refresh_interval')

        config_entry.version = 2
        hass.config_entries.async_update_entry(config_entry, data=new)

    _LOGGER.debug("Migration to version %s successful", config_entry.version)

    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up GTFS from a config entry."""

    hass.data.setdefault(DOMAIN, {})

    coordinator = GTFSUpdateCoordinator(hass, entry)

    #await coordinator.async_config_entry_first_refresh()
    
    if not coordinator.last_update_success:
        raise ConfigEntryNotReady
    
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
    }
    
    entry.async_on_unload(entry.add_update_listener(update_listener))
      
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

async def update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Handle options update."""
    hass.data[DOMAIN][entry.entry_id]['coordinator'].update_interval = timedelta(minutes=1)

    return True