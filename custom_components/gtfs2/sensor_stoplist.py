from datetime import datetime, timezone
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify
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

import homeassistant.util.dt as dt_util
import logging

_LOGGER = logging.getLogger(__name__)


def get_now_utc_iso_to_str () -> str:
    return dt_util.utcnow().isoformat()

###############################################################
###############################################################
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
#        self._update_attrs(p_called_by = "__init__" )



    async def home_assistant_started(self, event):
        _LOGGER.debug("GTFSLocalStopSensorList.home_assistant_started: %s", self._name)
        self._update_attrs(p_called_by = "home_assistant_started" )


    async def device_tracker_state_listener(self, event):
        _LOGGER.debug("GTFSLocalStopSensorList.device_tracker_state_listener: %s", self._name)
#        await self._new_device_tracker_state(state = event.data.get("new_state"))
 

#    async def _new_device_tracker_state(self, state):
#        if state is not None and state not in (STATE_UNKNOWN, STATE_UNAVAILABLE) :
#                self._update_attrs( p_force_update =False , p_called_by = "event/listner", p_longitude = state.attributes["longitude"] , p_latitude = state.attributes["latitude"])
#        else:
#            _LOGGER.info("device_tracer has an invalid value: %s. " ,state)


    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return self._name

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_attrs(p_called_by = "_handle_coordinator_update")

    def _update_attrs(self, p_called_by = "Unknown"):  # noqa: C901 PLR0911
        _LOGGER.debug("GTFSLocalStopSensorList._update_attrs (caller=%s): %s, update %s", p_called_by, self._name)

# TO TO
# if current GPS position is +/same as th last update and last update < 5 min ago
# => no nothing
# else update 

        self._attributes["gtfs_updated_at"] = get_now_utc_iso_to_str()
        self._departure = self.coordinator.data.get("local_stops_next_departures",None)
        self._attributes["stops"] = self._departure

        self._state = "Updated"
        self._attr_native_value = self._state        
        self._attr_extra_state_attributes = self._attributes
        super()._handle_coordinator_update()

