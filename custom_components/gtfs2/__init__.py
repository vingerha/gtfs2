"""The GTFS integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall

from .const import DOMAIN, PLATFORMS
from .coordinator import GTFSUpdateCoordinator
from .const import DEFAULT_PATH

import voluptuous as vol
from .gtfs_helper import get_gtfs


DOMAIN = "gtfs2"

## service call end

_LOGGER = logging.getLogger(__name__)


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old entry."""
    _LOGGER.debug("Migrating from version %s", config_entry.version)

    if config_entry.version == 1:
        new = {**config_entry.data}
        new["connection_type"] = "username_password"

        config_entry.version = 2
        hass.config_entries.async_update_entry(config_entry, data=new)

    _LOGGER.debug("Migration to version %s successful", config_entry.version)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Pronote from a config entry."""

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


### service call

GTFS_UPDATE_SCHEMA = vol.Schema(
    {
        vol.Required("name"): str,
        vol.Required("url"): str,
        vol.Required("update", default=True): bool,
    }
)


def setup(hass, config):
    """Setup the service example component."""

    def get_or_update_gtfs(call):
        """My GTFS service."""
        _LOGGER.info("Updating GTFS with: %s", call.data)
        get_gtfs(
            hass, DEFAULT_PATH, call.data["name"], call.data["url"], call.data["update"]
        )
        # _LOGGER.info('Compensate temperature to ...', call.data.get('temperature'))

    # Register our service with Home Assistant.
    hass.services.register(
        DOMAIN, "update_gtfs", get_or_update_gtfs, schema=GTFS_UPDATE_SCHEMA
    )

    # Return boolean to indicate that initialization was successfully.
    return True
