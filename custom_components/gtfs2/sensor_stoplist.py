from datetime import datetime, timezone
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify
from .const import *
from .coordinator import * 

import homeassistant.util.dt as dt_util
import logging

_LOGGER = logging.getLogger(__name__)


class GTFSLocalStopSensorList(CoordinatorEntity, SensorEntity):
    """Implementation of a GTFS local stops departures sensor."""

    def __init__( self, coordinator ) -> None:
        """Initialize the GTFSsensor."""
        super().__init__(coordinator)
        provided_name = coordinator.data.get("name", "No Name")
        self._previous_longitude = -1
        self._previous_latitude = -1

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

        self._attributes["gtfs_updated_at"] = self.coordinator.data["gtfs_updated_at"]
        self._attributes["device_tracker_id"] = self.coordinator.data["device_tracker_id"]
        self._attributes["offset"] = self.coordinator.data["offset"]
        self._attributes["stops"] = []
        self._attr_extra_state_attributes = self._attributes
        self._update_attrs()
        
    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return self._name

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_attrs()
        self._attr_extra_state_attributes = self._attributes
        self._state = "Updated"
        self._attr_native_value = self._state        
        super()._handle_coordinator_update()

    def _update_attrs(self):  # noqa: C901 PLR0911
        _LOGGER.debug("GTFSLocalStopSensorList: %s, update with attr data: %s", self._name, self.coordinator.data)

# TO TO
# if current GPS position is +/same as th last update and last update < 5 min ago
# => no nothing
# else update 

        self._attributes["gtfs_updated_at"] = self.coordinator.data["gtfs_updated_at"]
        self._departure = self.coordinator.data.get("local_stops_next_departures",None)
        self._attributes["stops"] = self._departure
        return self._attributes

