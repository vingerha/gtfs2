"""The GTFS integration."""
from __future__ import annotations

import logging
import os
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse

from datetime import timedelta

from .const import DOMAIN, PLATFORMS, DEFAULT_PATH, DEFAULT_PATH_RT, DEFAULT_PATH_GEOJSON, DEFAULT_REFRESH_INTERVAL
from homeassistant.const import CONF_HOST
from .coordinator import GTFSUpdateCoordinator, GTFSLocalStopUpdateCoordinator
import voluptuous as vol
from .gtfs_helper import get_gtfs, update_gtfs_local_stops, get_route_departures, get_trip_stops
from .gtfs_rt_helper import get_gtfs_rt, safe_file_part

_LOGGER = logging.getLogger(__name__)

async def async_migrate_entry(hass, config_entry: ConfigEntry) -> bool:
    """Migrate old entry."""
    _LOGGER.warning("Migrating from version %s", config_entry.version)
      
    if config_entry.version == 4:

        new_options = {**config_entry.options}
        new_data = {**config_entry.data}
        new_data['route_type'] = '99'
        new_options['offset'] = 0
        new_data.pop('offset')
        new_data['agency'] = '0: ALL'        

        config_entry.version = 9
        hass.config_entries.async_update_entry(config_entry, data=new_data)
        hass.config_entries.async_update_entry(config_entry, options=new_options)          
        
    if config_entry.version == 5:

        new_data = {**config_entry.data}
        new_data['route_type'] = '99'
        new_data['agency'] = '0: ALL'

        config_entry.version = 9
        hass.config_entries.async_update_entry(config_entry, data=new_data)  
        
    if config_entry.version == 6:

        new_data = {**config_entry.data}
        new_data['agency'] = '0: ALL'

        config_entry.version = 9
        hass.config_entries.async_update_entry(config_entry, data=new_data)  

    if config_entry.version == 7 or config_entry.version == 8 or config_entry.version == 9:

        new_data = {**config_entry.data}
        new_options = {**config_entry.options}
        if config_entry.options.get('api_key', None):
            new_options['api_key_name'] = "Authorization"
            new_options['api_key'] = config_entry.options.get('api_key')
        if config_entry.options.get('x_api_key', None):
            new_options['api_key_name'] = "x_api_key"            
            new_options['api_key'] = config_entry.options.get('x_api_key')   
        if config_entry.options.get('ocp_apim_subscription_key', None):
            new_options['api_key_name'] = "ocp_apim_subscription_key"
            new_options['api_key'] = config_entry.options.get('ocp_apim_subscription_key')
            new_options.pop('ocp_apim_subscription_key')
        if "x_api_key" in config_entry.options:
            new_options.pop('x_api_key')     

        
        config_entry.version = 10
        
        hass.config_entries.async_update_entry(config_entry, data=new_data)  
        hass.config_entries.async_update_entry(config_entry, options=new_options)             

    _LOGGER.warning("Migration to version %s successful", config_entry.version)

    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up GTFS from a config entry."""
    hass.data.setdefault(DOMAIN, {})
   
    if entry.data.get('device_tracker_id',None):
        coordinator = GTFSLocalStopUpdateCoordinator(hass, entry)
    else:
        coordinator = GTFSUpdateCoordinator(hass, entry)    

    if not coordinator.last_update_success:
        raise ConfigEntryNotReady
      
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator
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
    """Setup the service component."""

    def update_gtfs(call):
        """My GTFS Update service."""
        _LOGGER.debug("Updating GTFS with: %s", call.data)
        get_gtfs(hass, DEFAULT_PATH, call.data, True)
        return True     

    def update_gtfs_rt_local(call):
        """My GTFS RT service."""
        _LOGGER.debug("Updating GTFS RT with: %s", call.data)
        get_gtfs_rt(hass, DEFAULT_PATH_RT, call.data)
        return True  

    async def update_local_stops(call):
        """My GTFS Update Local Stops service."""
        _LOGGER.debug("Updating GTFS Local Stops with: %s", call.data)
        await update_gtfs_local_stops(hass, call.data)
        return True
    
    async def extract_departures(call):
        """My GTFS Departures service."""
        _LOGGER.debug("Retrieving next departures with: %s", call.data)
        departures = await get_route_departures(hass, call.data)
        return departures
        
    async def extract_trip_stops(call):
        """My GTFS Trip Stops service."""
        _LOGGER.debug("Retrieving trip stops with: %s", call.data)
        stops = await get_trip_stops(hass, call.data)
        return stops       

    hass.services.register(
        DOMAIN, "update_gtfs", update_gtfs)
    hass.services.register(
        DOMAIN, "update_gtfs_rt_local", update_gtfs_rt_local)     
    hass.services.register(
        DOMAIN, "update_gtfs_local_stops", update_local_stops)
    hass.services.register(
        DOMAIN, "extract_departures", extract_departures,supports_response=SupportsResponse.OPTIONAL)
    hass.services.register(
        DOMAIN, "extract_trip_stops", extract_trip_stops,supports_response=SupportsResponse.OPTIONAL)     
    return True

async def update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Handle options update."""
    hass.data[DOMAIN][entry.entry_id]['coordinator'].update_interval = timedelta(minutes=1)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the geojson files an entry leaves behind on disk.

    Home Assistant clears the entity registry of a removed entry by itself,
    right after this callback, but nothing knows about the files: the map
    export writes www/gtfs2/<route>_<direction>.json and its _route.json
    companion, and they would stay there for good.

    Both are named after the route and the direction rather than the entry,
    so two entries on the same line share them: only remove them when no
    other entry still needs them.
    """
    route = (entry.data.get("route") or "").split(": ")[0]
    direction = entry.data.get("direction")
    if not route or direction is None:
        return
    still_used = any(
        e.entry_id != entry.entry_id
        and (e.data.get("route") or "").split(": ")[0] == route
        and str(e.data.get("direction")) == str(direction)
        for e in hass.config_entries.async_entries(DOMAIN)
    )
    if still_used:
        _LOGGER.debug("Keeping geojson for route %s direction %s, another entry uses it",
                      route, direction)
        return
    # www/gtfs2, where the export writes them, not the datasource folder
    geojson_dir = hass.config.path(DEFAULT_PATH_GEOJSON)
    base = f"{safe_file_part(route)}_{safe_file_part(direction)}"
    names = [base + ".json", base + "_route.json"]
    # the files written before the ids were sanitised carry the raw name and
    # nothing else would ever remove them; an id that is not a plain file
    # name never wrote in this directory, so it is not looked for there
    legacy = f"{route}_{direction}"
    if os.path.basename(legacy) == legacy and ".." not in legacy:
        names += [legacy + ".json", legacy + "_route.json"]
    for name in dict.fromkeys(names):
        path = os.path.join(geojson_dir, name)
        if os.path.exists(path):
            try:
                os.remove(path)
                _LOGGER.info("Removed %s", path)
            except OSError as ex:
                _LOGGER.warning("Could not remove %s: %s", path, ex)