import logging
from datetime import datetime, timedelta

import homeassistant.helpers.config_validation as cv
import homeassistant.util.dt as dt_util
import requests
import voluptuous as vol
from google.transit import gtfs_realtime_pb2
from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import ATTR_LATITUDE, ATTR_LONGITUDE, CONF_NAME
from homeassistant.helpers.entity import Entity
from homeassistant.util import Throttle

_LOGGER = logging.getLogger(__name__)

ATTR_STOP_ID = "Stop ID"
ATTR_ROUTE = "Route"
ATTR_DIRECTION_ID = "Direction ID"
ATTR_DUE_IN = "Due in"
ATTR_DUE_AT = "Due at"
ATTR_NEXT_UP = "Next Service"
ATTR_ICON = "Icon"
ATTR_UNIT_OF_MEASUREMENT = "unit_of_measurement"
ATTR_DEVICE_CLASS = "device_class"

CONF_API_KEY = "api_key"
CONF_X_API_KEY = "x_api_key"
CONF_STOP_ID = "stopid"
CONF_ROUTE = "route"
CONF_DIRECTION_ID = "directionid"
CONF_DEPARTURES = "departures"
CONF_TRIP_UPDATE_URL = "trip_update_url"
CONF_VEHICLE_POSITION_URL = "vehicle_position_url"
CONF_ROUTE_DELIMITER = "route_delimiter"
CONF_ICON = "icon"
CONF_SERVICE_TYPE = "service_type"
CONF_RELATIVE_TIME = "show_relative_time"

DEFAULT_SERVICE = "Service"
DEFAULT_ICON = "mdi:bus"
DEFAULT_DIRECTION = "0"

MIN_TIME_BETWEEN_UPDATES = timedelta(seconds=60)
TIME_STR_FORMAT = "%H:%M"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_TRIP_UPDATE_URL): cv.string,
        vol.Optional(CONF_API_KEY): cv.string,
        vol.Optional(CONF_X_API_KEY): cv.string,
        vol.Optional(CONF_VEHICLE_POSITION_URL): cv.string,
        vol.Optional(CONF_ROUTE_DELIMITER): cv.string,
        
        vol.Optional(CONF_DEPARTURES): [
            {
                vol.Required(CONF_NAME): cv.string,
                vol.Required(CONF_STOP_ID): cv.string,
                vol.Required(CONF_ROUTE): cv.string,
                vol.Optional(CONF_RELATIVE_TIME, default=True): cv.boolean,
                vol.Optional(
                    CONF_DIRECTION_ID,
                    default=DEFAULT_DIRECTION,  # type: ignore
                ): str,
                vol.Optional(
                    CONF_ICON, default=DEFAULT_ICON  # type: ignore
                ): cv.string,
                vol.Optional(
                    CONF_SERVICE_TYPE, default=DEFAULT_SERVICE  # type: ignore
                ): cv.string,
            }
        ],
    }
)

def due_in_minutes(timestamp):
    """Get the remaining minutes from now until a given datetime object."""
    diff = timestamp - dt_util.now().replace(tzinfo=None)
    return int(diff.total_seconds() / 60)


def log_info(data: list, indent_level: int) -> None:
    indents = "   " * indent_level
    info_str = f"{indents}{': '.join(str(x) for x in data)}"
    _LOGGER.info(info_str)


def log_error(data: list, indent_level: int) -> None:
    indents = "   " * indent_level
    info_str = f"{indents}{': '.join(str(x) for x in data)}"
    _LOGGER.error(info_str)


def log_debug(data: list, indent_level: int) -> None:
    indents = "   " * indent_level
    info_str = f"{indents}{' '.join(str(x) for x in data)}"
    _LOGGER.debug(info_str)



def get_gtfs_feed_entities(url: str, headers, label: str):
    feed = gtfs_realtime_pb2.FeedMessage()  # type: ignore

    # TODO add timeout to requests call
    response = requests.get(url, headers=headers, timeout=20)
    if response.status_code == 200:
        log_info([f"Successfully updated {label}", response.status_code], 0)
    else:
        log_error(
            [
                f"Updating {label} got",
                response.status_code,
                response.content,
            ],
            0,
        )

    feed.ParseFromString(response.content)
    return feed.entity


       
## reworked for gtfs2

def get_next_services(self):
    self.data = self._data
    self._stop = self._stop_id
    self._route = self._route_id
    self._direction = self._direction
    _LOGGER.debug("Get Next Services, route/direction/stop: %s", self.data.get(self._route, {}).get(self._direction, {}).get(self._stop, []))
    
    next_services = self.data.get(self._route, {}).get(self._direction, {}).get(self._stop, [])
    if self.hass.config.time_zone is None:
        _LOGGER.error("Timezone is not set in Home Assistant configuration")
        timezone = "UTC"
    else:
        timezone=dt_util.get_time_zone(self.hass.config.time_zone)
    
    if self._relative :
        return (
            due_in_minutes(next_services[0].arrival_time)
            if len(next_services) > 0
            else "-"
        )
    else:
        return (
            next_services[0].arrival_time.replace(tzinfo=timezone)
            if len(next_services) > 0
            else "-"
        )

def get_rt_route_statuses(self):
        
    vehicle_positions = {}
    

    class StopDetails:
        def __init__(self, arrival_time, position):
            self.arrival_time = arrival_time
            self.position = position

    departure_times = {}

    feed_entities = get_gtfs_feed_entities(
        url=self._trip_update_url, headers=self._headers, label="trip data"
    )

    for entity in feed_entities:
        if entity.HasField("trip_update"):
            # If delimiter specified split the route ID in the gtfs rt feed
            log_debug(
                [
                    "Received Trip ID",
                    entity.trip_update.trip.trip_id,
                    "Route ID:",
                    entity.trip_update.trip.route_id,
                    "direction ID",
                    entity.trip_update.trip.direction_id,
                    "Start Time:",
                    entity.trip_update.trip.start_time,
                    "Start Date:",
                    entity.trip_update.trip.start_date,
                ],
                1,
            )
            if self._route_delimiter is not None:
                route_id_split = entity.trip_update.trip.route_id.split(
                    self._route_delimiter
                )
                if route_id_split[0] == self._route_delimiter:
                    route_id = entity.trip_update.trip.route_id
                else:
                    route_id = route_id_split[0]
                log_debug(
                    [
                        "Feed Route ID",
                        entity.trip_update.trip.route_id,
                        "changed to",
                        route_id,
                    ],
                    1,
                )

            else:
                route_id = entity.trip_update.trip.route_id

            if route_id not in departure_times:
                departure_times[route_id] = {}

            if entity.trip_update.trip.direction_id is not None:
                direction_id = str(entity.trip_update.trip.direction_id)
            else:
                direction_id = DEFAULT_DIRECTION
            if direction_id not in departure_times[route_id]:
                departure_times[route_id][direction_id] = {}

            for stop in entity.trip_update.stop_time_update:
                stop_id = stop.stop_id
                if not departure_times[route_id][direction_id].get(
                    stop_id
                ):
                    departure_times[route_id][direction_id][stop_id] = []
                # Use stop arrival time;
                # fall back on departure time if not available
                if stop.arrival.time == 0:
                    stop_time = stop.departure.time
                else:
                    stop_time = stop.arrival.time
                log_debug(
                    [
                        "Stop:",
                        stop_id,
                        "Stop Sequence:",
                        stop.stop_sequence,
                        "Stop Time:",
                        stop_time,
                    ],
                    2,
                )
                # Ignore arrival times in the past
                if due_in_minutes(datetime.fromtimestamp(stop_time)) >= 0:
                    log_debug(
                        [
                            "Adding route ID",
                            route_id,
                            "trip ID",
                            entity.trip_update.trip.trip_id,
                            "direction ID",
                            entity.trip_update.trip.direction_id,
                            "stop ID",
                            stop_id,
                            "stop time",
                            stop_time,
                        ],
                        3,
                    )

                    details = StopDetails(
                        datetime.fromtimestamp(stop_time),
                        vehicle_positions.get(
                            entity.trip_update.trip.trip_id
                        ),
                    )
                    departure_times[route_id][direction_id][
                        stop_id
                    ].append(details)

    # Sort by arrival time
    for route in departure_times:
        for direction in departure_times[route]:
            for stop in departure_times[route][direction]:
                departure_times[route][direction][stop].sort(
                    key=lambda t: t.arrival_time
                )

    self.info = departure_times
    
    return departure_times

def get_rt_vehicle_positions(self):
    positions = {}
    feed_entities = get_gtfs_feed_entities(
        url=self._vehicle_position_url,
        headers=self._headers,
        label="vehicle positions",
    )

    for entity in feed_entities:
        vehicle = entity.vehicle

        if not vehicle.trip.trip_id:
            # Vehicle is not in service
            continue
        log_debug(
            [
                "Adding position for trip ID",
                vehicle.trip.trip_id,
                "position latitude",
                vehicle.position.latitude,
                "longitude",
                vehicle.position.longitude,
            ],
            2,
        )

        positions[vehicle.trip.trip_id] = vehicle.position

    return positions
        
