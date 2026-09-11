"""Data Update coordinator for the GTFS integration."""
from __future__ import annotations

import datetime
from datetime import timedelta
import logging
import re

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
import homeassistant.util.dt as dt_util

from .const import (
    DEFAULT_PATH, 
    DEFAULT_REFRESH_INTERVAL, 
    DEFAULT_LOCAL_STOP_REFRESH_INTERVAL,
    DEFAULT_LOCAL_STOP_TIMERANGE,
    DEFAULT_LOCAL_STOP_RADIUS,
    CONF_API_KEY,
    CONF_API_KEY_NAME,
    CONF_API_KEY_LOCATION,
    CONF_ACCEPT_HEADER_PB,
    ATTR_DUE_IN,
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
    ATTR_RT_UPDATED_AT,
    ICON,
    ICONS
)    
from .gtfs_helper import get_gtfs, get_next_departure, check_datasource_index, create_trip_geojson, check_extracting, get_local_stops_next_departures, update_route_geojson
from .gtfs_rt_helper import get_next_services, get_rt_alerts

_LOGGER = logging.getLogger(__name__)


class GTFSUpdateCoordinator(DataUpdateCoordinator):
    """Data update coordinator for the GTFS integration."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            name=entry.entry_id,
            update_interval=timedelta(minutes=1),
        )
        self.config_entry = entry
        self.hass = hass

        self._pygtfs = ""
        self._data: dict[str, str] = {}
        # the trip whose stops are already exported, so the geojson is
        # rewritten when the journey changes and not on every refresh
        self._route_export_trip = None
        self._stale_markers_cleaned = False

    async def _async_update_data(self) -> dict[str, str]:
        """Get the latest data from GTFS and GTFS relatime, depending refresh interval"""
        data = self.config_entry.data
        options = self.config_entry.options
        previous_data = {} if self.data is None else self.data.copy()
        _LOGGER.debug("Previous data: %s", previous_data)  

        if self._pygtfs and hasattr(self._pygtfs, 'session'):
            try:
                self._pygtfs.session.close()
                self._pygtfs.engine.dispose()
            except Exception:
                pass

        self._pygtfs = get_gtfs(
            self.hass, DEFAULT_PATH, data, False
        )        

        self._data = {
            "schedule": self._pygtfs,
            "origin": data["origin"],
            "destination": data["destination"],
            "offset": options["offset"] if "offset" in options else 0,
            "include_tomorrow": data["include_tomorrow"],
            "gtfs_dir": DEFAULT_PATH,
            "name": data["name"],
            "file": data["file"],
            "route_type": data["route_type"],
            "route": data["route"],
            "extracting": False,
            "next_departure": {},
            "next_departure_realtime_attr": {},
            "alert": {}
        }           
        
        if check_extracting(self.hass, self.hass.config.path(self._data['gtfs_dir']), self._data['file']):   
            _LOGGER.debug("Cannot update this sensor as still unpacking: %s", self._data["file"])
            self._data.update(previous_data)
            self._data["extracting"] = True
            return self._data
        

        # determine static + rt or only static (refresh schedule depending)
        #1. sensor exists with data but refresh interval not yet reached, use existing data
        if "gtfs_updated_at" in previous_data and (
            datetime.datetime.strptime(previous_data["gtfs_updated_at"], '%Y-%m-%dT%H:%M:%S.%f%z')
            + timedelta(minutes=options.get("refresh_interval", DEFAULT_REFRESH_INTERVAL))
        ) > dt_util.utcnow() + timedelta(seconds=1):
            run_static = False
            _LOGGER.debug("No run static refresh: sensor exists but not yet refresh for name: %s", data["name"])
        else:
            run_static = True
            _LOGGER.debug("Run static refresh: sensor without gtfs data OR refresh for name: %s", data["name"])
        
        if not run_static:
            # do nothing awaiting refresh interval and use existing data
            self._data = previous_data
        else:
            check_index = await self.hass.async_add_executor_job(
                    check_datasource_index, self.hass, self._pygtfs, self.hass.config.path(DEFAULT_PATH), data["file"]
                )

            try:
                self._data["next_departure"] = await self.hass.async_add_executor_job(
                    get_next_departure, self.hass, self._data
                )
                self._data["gtfs_updated_at"] = dt_util.utcnow().isoformat()
            except Exception as ex:  # pylint: disable=broad-except
                raise UpdateFailed(f"Error in getting gtfs data: {ex}") from ex
            _LOGGER.debug("GTFS coordinator data from helper: %s", self._data["next_departure"])
            # The planned side of the map: the journey's ordered stops, written
            # next to the vehicle positions. It follows the static data, needs
            # no realtime at all, and only changes with the drawn trip, so it
            # is keyed on the trip rather than rewritten every refresh.
            trip_for_export = self._data.get("next_departure", {}).get("trip_id", None)
            if trip_for_export and trip_for_export != self._route_export_trip:
                try:
                    await self.hass.async_add_executor_job(update_route_geojson, self)
                    self._route_export_trip = trip_for_export
                except Exception as ex:  # pylint: disable=broad-except
                    _LOGGER.error("Error writing route geojson: %s", ex)
        
        # collect and return rt attributes
        # STILL REQUIRES A SOLUTION IF CONNECTION TIMING OUT
        if "real_time" in options:
            if options["real_time"]:
                if not self._data.get("next_departure"):
                    # when there are no more departures, skip the realtime block
                    _LOGGER.debug("GTFS RT: no next departure for this entry, skipping realtime update")
                    return self._data
                self._get_next_service = {}
                """Initialize the info object."""
                self._route_delimiter = None
                self._headers = None
                self._trip_update_url = options.get("trip_update_url", None)
                self._vehicle_position_url = options.get("vehicle_position_url", None)
                self._icon = ICONS.get(int(self._data["route_type"]), ICON)
                self._alerts_url = options.get("alerts_url", None)
                if options.get(CONF_API_KEY_LOCATION, None) == "query_string":
                  if options.get(CONF_API_KEY, None):
                    if self._trip_update_url:
                        self._trip_update_url = self._trip_update_url + "?" + options[CONF_API_KEY_NAME] + "=" + options[CONF_API_KEY]
                    if self._vehicle_position_url:
                        self._vehicle_position_url = self._vehicle_position_url + "?" + options[CONF_API_KEY_NAME] + "=" + options[CONF_API_KEY]
                    if self._alerts_url:
                        self._alerts_url = self._alerts_url + "?" + options[CONF_API_KEY_NAME] + "=" + options[CONF_API_KEY]
                if options.get(CONF_API_KEY_LOCATION, None) == "header":
                    self._headers = {options[CONF_API_KEY_NAME]: options[CONF_API_KEY]}               
                    if options.get(CONF_ACCEPT_HEADER_PB, False):
                        self._headers["Accept"] = "application/x-protobuf"
                self.info = {}
                self._route_id = self._data["next_departure"].get("route_id", None)
                if self._route_id == None:
                    _LOGGER.debug("GTFS RT: no route_id in sensor data, using route_id from config_entry")
                    self._route_id = data["route"].split(": ")[0]
                self._stop_id = self._data["next_departure"].get("origin_stop_id","no_origin_stop: no_origin_stop").split(": ")[0]
                self._stop_sequence = self._data["next_departure"]["origin_stop_sequence"]
                self._destination_id = data["destination"].split(": ")[0]
                self._trip_id = self._data.get('next_departure', {}).get('trip_id', None) 
                self._trip_short_name = self._data.get('next_departure', {}).get('trip_short_name', None) 
                self._direction = str(self._data.get('next_departure', {}).get('trip_direction_id', data["direction"]))
                self._trip_list = self._data["next_departure"].get("next_departures_trip_id", [])[:10]
                self._relative = False
                try:
                    self._get_rt_alerts = await self.hass.async_add_executor_job(get_rt_alerts, self)
                    self._get_next_service = await self.hass.async_add_executor_job(get_next_services, self)
                    self._data["next_departure_realtime_attr"] = self._get_next_service
                    self._data["next_departure_realtime_attr"]["gtfs_rt_updated_at"] = dt_util.utcnow()
                    self._data["alert"] = self._get_rt_alerts
                except Exception as ex:  # pylint: disable=broad-except
                  _LOGGER.error("Error getting gtfs realtime data, for origin: %s with error: %s", data["origin"], ex)
                  return self._data
                if self._vehicle_position_url and not self._stale_markers_cleaned:
                    self._cleanup_stale_vehicle_markers()
                    self._stale_markers_cleaned = True
            else:
                _LOGGER.debug("GTFS RT: RealTime = false, selected in entity options")
        else:
            _LOGGER.debug("GTFS RT: RealTime not selected in entity options")

        return self._data

    def _cleanup_stale_vehicle_markers(self) -> None:
        """One-shot removal of the stale vehicle markers of this route.

        geo_json_events registers every marker of the positions file as a
        geo_location entity and drops its state when the vehicle leaves the
        feed, but the registry entry stays behind. As the marker id embeds the
        trip, every run leaves a new entry and the registry grows without
        bound. Drop the entries of this route that no longer have a state; a
        trip that runs again is simply registered afresh.
        """
        registry = er.async_get(self.hass)
        pattern = re.compile(re.escape(str(self._route_id)) + r"\(\d+\)\d{1,3}$")
        for entry in list(registry.entities.values()):
            if (
                entry.domain == "geo_location"
                and entry.platform == "geo_json_events"
                and pattern.search(entry.unique_id)
                and self.hass.states.get(entry.entity_id) is None
            ):
                _LOGGER.info(
                    "Removing stale vehicle marker %s (unique_id: %s)",
                    entry.entity_id,
                    entry.unique_id,
                )
                registry.async_remove(entry.entity_id)

class GTFSLocalStopUpdateCoordinator(DataUpdateCoordinator):
    """Data update coordinator for getting local stops."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            name=entry.entry_id,
            update_interval=timedelta(minutes=entry.options.get("local_stop_refresh_interval", DEFAULT_LOCAL_STOP_REFRESH_INTERVAL)),
        )
        self.config_entry = entry
        self.hass = hass
        
        self._pygtfs = ""
        self._data: dict[str, str] = {}

    async def _async_update_data(self) -> dict[str, str]:
        """Get the latest data from GTFS and GTFS relatime, depending refresh interval"""      
        data = self.config_entry.data
        options = self.config_entry.options
        previous_data = {} if self.data is None else self.data.copy()
        _LOGGER.debug("Previous data: %s", previous_data)

        if self._pygtfs and hasattr(self._pygtfs, 'session'):
            try:
                self._pygtfs.session.close()
                self._pygtfs.engine.dispose()
            except Exception:
                pass

        self._pygtfs = get_gtfs(
            self.hass, DEFAULT_PATH, data, False
        )

        self._data = {
            "schedule": self._pygtfs,
            "include_tomorrow": True,
            "gtfs_dir": DEFAULT_PATH,
            "name": data["name"],
            "file": data["file"],
            "offset": options["offset"] if "offset" in options else 0,
            "timerange": options.get("timerange", DEFAULT_LOCAL_STOP_TIMERANGE),
            "radius": options.get("radius", DEFAULT_LOCAL_STOP_RADIUS),
            "device_tracker_id": data["device_tracker_id"],
            "extracting": False,
        }           
        self._data["gtfs_updated_at"] = dt_util.utcnow().isoformat() 
        
        if check_extracting(self.hass, self.hass.config.path(self._data['gtfs_dir']), self._data['file']):   
            _LOGGER.debug("Cannot update this sensor as still unpacking: %s", self._data["file"])
            self._data.update(previous_data)
            self._data["extracting"] = True
            return self._data
            
        self._realtime = False
        if "real_time" in options: 
            if options["real_time"]:
                self._realtime = True
                self._get_next_service = {}
                """Initialize the info object."""
                self._route_delimiter = None
                self._headers = {}
                self._rt_group = "trip"
                self._trip_update_url = options.get("trip_update_url", None)
                self._vehicle_position_url = options.get("vehicle_position_url", None)
                self._alerts_url = options.get("alerts_url", None)
                if options.get(CONF_API_KEY_LOCATION, None) == "query_string":
                  if options.get(CONF_API_KEY, None):
                    if self._trip_update_url:
                        self._trip_update_url = self._trip_update_url + "?" + options[CONF_API_KEY_NAME] + "=" + options[CONF_API_KEY]
                if options.get(CONF_API_KEY_LOCATION, None) == "header":
                    self._headers = {options[CONF_API_KEY_NAME]: options[CONF_API_KEY]}   
                    self._headers[CONF_API_KEY_LOCATION] = options.get(CONF_API_KEY_LOCATION,None)
                    self._headers[CONF_API_KEY_NAME] = options.get(CONF_API_KEY_NAME, None)
                    self._headers[CONF_API_KEY] = options.get(CONF_API_KEY, None)
                    self._headers[CONF_ACCEPT_HEADER_PB] = options.get(CONF_ACCEPT_HEADER_PB, False)
                #_LOGGER.debug("RT header: %s", self._headers)

        try:
            self._data["local_stops_next_departures"] = await self.hass.async_add_executor_job(
                    get_local_stops_next_departures, self
                )
        except Exception as ex:
            _LOGGER.error("Error getting local stops data: %s", ex)
            raise UpdateFailed(f"Error in getting local stops data: {ex}")
        #_LOGGER.debug("Data from coordinator: %s", self._data)              
        return self._data
