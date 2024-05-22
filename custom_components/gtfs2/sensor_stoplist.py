from datetime import datetime, timezone

import logging
#from typing import Any

#import os
import geopy.distance

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify
import homeassistant.util.dt as dt_util
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EVENT_HOMEASSISTANT_STARTED,    
)

from .const import *
from .coordinator import * 

_LOGGER = logging.getLogger(__name__)



def get_now_utc_iso_to_str () -> str:
    return dt_util.utcnow().isoformat()


class GTFSLocalStopSensorList(CoordinatorEntity, SensorEntity):

    """Implementation of a GTFS local stops departures sensor."""

###########################
### __INIT__
    def __init__( self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        coordinator ) -> None:




        """Initialize the GTFSsensor."""
        self.hass = hass
        self.config_entry = config_entry
        self.coordinator = coordinator

        super().__init__(coordinator)
        provided_name = coordinator.data.get("name", "No Name")
        self._previous_longitude = -1
        self._previous_latitude = -1 
        self._longitude = -1
        self._latitude = -1 
        self._distance = -1 

        self._name =  provided_name + " local_stoplist"
        self._attributes: dict[str, Any] = {}

        self._attr_unique_id = "sensor.gtfs2_" + self._name
        self._attr_unique_id = self._attr_unique_id.lower()
        self._attr_unique_id = self._attr_unique_id.replace(" ", "_")
        self.entity_id = self._attr_unique_id

        self._attr_device_info = DeviceInfo(
            name=f"GTFS - {provided_name}",
            entry_type=DeviceEntryType.SERVICE,
            identifiers={(DOMAIN, f"GTFS - {provided_name}")},
            manufacturer="GTFS",
            model=provided_name,
        )
        self._state: str | None = None
        self._state = "Initialized"
        self._attr_native_value = self._state

            

        self._attributes["gtfs_updated_at"] = get_now_utc_iso_to_str()
        self._attributes["device_tracker_id"] = self.config_entry.data.get('device_tracker_id',None)
        self._attributes["offset"] = self.config_entry.data.get('offset',None)
        self._attributes["latitude"] = self._latitude
        self._attributes["longitude"] = self._longitude
        self._attributes["movement_meters"] = self._distance


        self._attributes["stops"] = []
        self._attr_extra_state_attributes = self._attributes


        async_track_state_change_event(
            self.hass, self.config_entry.data.get('device_tracker_id',None), self.device_tracker_state_listener
        )
        self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, self.home_assistant_started
        )


    async def home_assistant_started(self, event):
        self._update_attrs(p_called_by = "home_assistant_started" )


    async def device_tracker_state_listener(self, event):
        """Handle  device state changes."""
        await self._new_device_tracker_state(state = event.data.get("new_state"))

    async def _new_device_tracker_state(self, state):
        if state is not None and state not in (STATE_UNKNOWN, STATE_UNAVAILABLE) :
 #               _LOGGER.info("device_tracer has state: %s" ,state)
 #               _LOGGER.info("device_tracer has state.state: %s " ,state.state )
 #               _LOGGER.info("device_tracer has state.attributes: %s " ,state.attributes )
 #               _LOGGER.info("device_tracer has attribute latitude= %s " ,state.attributes["latitude"] )
 #               _LOGGER.info("device_tracer has attribute longitude= %s " ,state.attributes["longitude"] )

                self._update_attrs( p_force_update =False , p_called_by = "event/listner", p_longitude = state.attributes["longitude"] , p_latitude = state.attributes["latitude"])
        else:
            _LOGGER.info("device_tracer has an invalid value: %s. " ,state)



    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return self._name

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_attrs(p_called_by = "_handle_coordinator_update")

    def _update_attrs(self, p_force_update = False , p_called_by = "Unknown", p_longitude = -1, p_latitude = -1):  # noqa: C901 PLR0911
        _LOGGER.debug("GTFSLocalStopSensorList: %s, begin of _update_attrs", self._name)


        # main logic is : Update sensor with all stops IF either:
        # - current status in Initialzed (sensor created but empty)
        # - data are geting too old...   
        # - current position has changed ( GPS)

        delta = datetime.datetime.now(timezone.utc)  - ( datetime.datetime.strptime( self._attributes["gtfs_updated_at"] ,"%Y-%m-%dT%H:%M:%S.%f%z" ) )

        if p_longitude == -1 and p_latitude == - 1 :
            # coordinates not received in parameters
            device_tracker = self.hass.states.get( self.config_entry.data.get('device_tracker_id',None))
            self._latitude = device_tracker.attributes.get("latitude", None)
            self._longitude = device_tracker.attributes.get("longitude", None)

        else:
            self._latitude  = p_latitude
            self._longitude = p_longitude

        distance_meters = -1
        if ((self._previous_latitude  != -1) and (self._previous_longitude  != -1 )) : 
            coords_1 = (self._latitude ,self._longitude)
            coords_2 = (self._previous_latitude, self._previous_longitude)
            distance_meters = geopy.distance.geodesic(coords_1, coords_2).meters 


        update = p_force_update
        if update:
                _LOGGER.info("GTFSLocalStopSensorList._update_attrs(caller=%s): %s, Update Forced", 
                    p_called_by,
                    self._name)

        if not update:
            if  ( self._state == "Initialized" ):
                _LOGGER.info("GTFSLocalStopSensorList._update_attrs(caller=%s): %s, State 'Init' => update",p_called_by, self._name)
                update = True

        if not update:
            if  ( self._previous_longitude == -1 ) or ( self._previous_latitude == -1):
                _LOGGER.info("GTFSLocalStopSensorList._update_attrs(caller=%s): %s, Logic Error => update", 
                    p_called_by,self._name)
                # this case should never happens
                # was never updated ???? (= State <> Initialized  and no GSP coordinates ?????)
                update = True

        if not update:
            if delta.total_seconds()  > 300: 
                _LOGGER.info("GTFSLocalStopSensorList._update_attrs(caller=%s): %s, data too old => update",
                    p_called_by, self._name)                
                # existing data are outdated
                update = True

        if not update:
            if distance_meters > 5:
                _LOGGER.info("GTFSLocalStopSensorList._update_attrs(caller=%s): %s, GPS moved %d meters (%2.8f %2.8f) => (%2.8f %2.8f) => update", 
                    p_called_by,
                    self._name, 
                    distance_meters,
                    self._previous_latitude, 
                    self._previous_longitude, 
                    self._latitude, 
                    self._longitude)

                # GPS coordinates are changing
                update = True

        if not update:
                _LOGGER.debug("GTFSLocalStopSensorList._update_attrs(caller=%s): %s, Update skipped", 
                    p_called_by, self._name)
        else:
            
            if ( ( self._state != "Initialized" ) and  (delta.total_seconds() > 5 ) and (delta.total_seconds() < 30)):
                _LOGGER.warning("GTFSLocalStopSensorList._update_attrs(caller=%s): %s, HIGH REFRESH RATE",
                    p_called_by, self._name)
            if  ( self._state != "Initialized" ) and  ( delta.total_seconds() < 5 ):
                _LOGGER.error("GTFSLocalStopSensorList._update_attrs(caller=%s): %s, refresh rate exceeded",
                    p_called_by, self._name)
            else:
                self._attributes["latitude"] = self._latitude
                self._attributes["longitude"] = self._longitude
                self._attributes["movement_meters"] = distance_meters
                self._departure = self.coordinator.data.get("local_stops_next_departures",None)
                self._attributes["stops"] = self._departure
                self._attributes["gtfs_updated_at"] = get_now_utc_iso_to_str()

                if self._longitude != -1 :
                    self._previous_longitude = self._longitude

                if self._latitude != -1 :
                    self._previous_latitude = self._latitude

                self._state = "Updated"
                self._attr_native_value = self._state        
                self._attr_extra_state_attributes = self._attributes
                super()._handle_coordinator_update()

#        return self._attributes
