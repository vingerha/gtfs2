"""Support for GTFS Integration."""
from __future__ import annotations

import datetime
import logging
import os
import requests
import pygtfs
from sqlalchemy.sql import text

import homeassistant.util.dt as dt_util
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def get_next_departure(self):
    _LOGGER.debug("Get next departure with data: %s", self._data)
    """Get next departures from data."""
    if self.hass.config.time_zone is None:
        _LOGGER.error("Timezone is not set in Home Assistant configuration")
        timezone = "UTC"
    else:
        timezone=dt_util.get_time_zone(self.hass.config.time_zone)
    schedule = self._data["schedule"]
    start_station_id = self._data["origin"]
    end_station_id = self._data["destination"]
    offset = self._data["offset"]
    include_tomorrow = self._data["include_tomorrow"]
    now = dt_util.now().replace(tzinfo=None) + datetime.timedelta(minutes=offset)
    now_date = now.strftime(dt_util.DATE_STR_FORMAT)
    yesterday = now - datetime.timedelta(days=1)
    yesterday_date = yesterday.strftime(dt_util.DATE_STR_FORMAT)
    tomorrow = now + datetime.timedelta(days=1)
    tomorrow_date = tomorrow.strftime(dt_util.DATE_STR_FORMAT)

    # Fetch all departures for yesterday, today and optionally tomorrow,
    # up to an overkill maximum in case of a departure every minute for those
    # days.
    limit = 24 * 60 * 60 * 2
    tomorrow_select = tomorrow_where = tomorrow_order = ""
    if include_tomorrow:
        _LOGGER.debug("Include Tomorrow")
        limit = int(limit / 2 * 3)
        tomorrow_name = tomorrow.strftime("%A").lower()
        tomorrow_select = f"calendar.{tomorrow_name} AS tomorrow,"
        tomorrow_where = f"OR calendar.{tomorrow_name} = 1"
        tomorrow_order = f"calendar.{tomorrow_name} DESC,"

    sql_query = f"""
        SELECT trip.trip_id, trip.route_id,trip.trip_headsign,route.route_long_name,
               time(origin_stop_time.arrival_time) AS origin_arrival_time,
               time(origin_stop_time.departure_time) AS origin_depart_time,
               date(origin_stop_time.departure_time) AS origin_depart_date,
               origin_stop_time.drop_off_type AS origin_drop_off_type,
               origin_stop_time.pickup_type AS origin_pickup_type,
               origin_stop_time.shape_dist_traveled AS origin_dist_traveled,
               origin_stop_time.stop_headsign AS origin_stop_headsign,
               origin_stop_time.stop_sequence AS origin_stop_sequence,
               origin_stop_time.timepoint AS origin_stop_timepoint,
               time(destination_stop_time.arrival_time) AS dest_arrival_time,
               time(destination_stop_time.departure_time) AS dest_depart_time,
               destination_stop_time.drop_off_type AS dest_drop_off_type,
               destination_stop_time.pickup_type AS dest_pickup_type,
               destination_stop_time.shape_dist_traveled AS dest_dist_traveled,
               destination_stop_time.stop_headsign AS dest_stop_headsign,
               destination_stop_time.stop_sequence AS dest_stop_sequence,
               destination_stop_time.timepoint AS dest_stop_timepoint,
               calendar.{yesterday.strftime("%A").lower()} AS yesterday,
               calendar.{now.strftime("%A").lower()} AS today,
               {tomorrow_select}
               calendar.start_date AS start_date,
               calendar.end_date AS end_date,
               0 as today_cd
        FROM trips trip
        INNER JOIN calendar calendar
                   ON trip.service_id = calendar.service_id
        INNER JOIN stop_times origin_stop_time
                   ON trip.trip_id = origin_stop_time.trip_id
        INNER JOIN stops start_station
                   ON origin_stop_time.stop_id = start_station.stop_id
        INNER JOIN stop_times destination_stop_time
                   ON trip.trip_id = destination_stop_time.trip_id
        INNER JOIN stops end_station
                   ON destination_stop_time.stop_id = end_station.stop_id
        INNER JOIN routes route
                   ON route.route_id = trip.route_id 
        LEFT OUTER JOIN calendar_dates calendar_date_today
            on trip.service_id = calendar_date_today.service_id
        WHERE start_station.stop_id = :origin_station_id
                   AND end_station.stop_id = :end_station_id
        AND origin_stop_sequence < dest_stop_sequence
        AND calendar.start_date <= :today
        AND calendar.end_date >= :today
		UNION ALL
	    SELECT trip.trip_id, trip.route_id,trip.trip_headsign,route.route_long_name,
               time(origin_stop_time.arrival_time) AS origin_arrival_time,
               time(origin_stop_time.departure_time) AS origin_depart_time,
               date(origin_stop_time.departure_time) AS origin_depart_date,
               origin_stop_time.drop_off_type AS origin_drop_off_type,
               origin_stop_time.pickup_type AS origin_pickup_type,
               origin_stop_time.shape_dist_traveled AS origin_dist_traveled,
               origin_stop_time.stop_headsign AS origin_stop_headsign,
               origin_stop_time.stop_sequence AS origin_stop_sequence,
               origin_stop_time.timepoint AS origin_stop_timepoint,
               time(destination_stop_time.arrival_time) AS dest_arrival_time,
               time(destination_stop_time.departure_time) AS dest_depart_time,
               destination_stop_time.drop_off_type AS dest_drop_off_type,
               destination_stop_time.pickup_type AS dest_pickup_type,
               destination_stop_time.shape_dist_traveled AS dest_dist_traveled,
               destination_stop_time.stop_headsign AS dest_stop_headsign,
               destination_stop_time.stop_sequence AS dest_stop_sequence,
               destination_stop_time.timepoint AS dest_stop_timepoint,
               calendar.{yesterday.strftime("%A").lower()} AS yesterday,
               calendar.{now.strftime("%A").lower()} AS today,
               {tomorrow_select}
               calendar.start_date AS start_date,
               calendar.end_date AS end_date,
               calendar_date_today.exception_type as today_cd
        FROM trips trip
        INNER JOIN calendar calendar
            ON trip.service_id = calendar.service_id
        INNER JOIN stop_times origin_stop_time
                   ON trip.trip_id = origin_stop_time.trip_id
        INNER JOIN stops start_station
                   ON origin_stop_time.stop_id = start_station.stop_id
        INNER JOIN stop_times destination_stop_time
                   ON trip.trip_id = destination_stop_time.trip_id
        INNER JOIN stops end_station
                   ON destination_stop_time.stop_id = end_station.stop_id
        INNER JOIN routes route
                   ON route.route_id = trip.route_id 
        INNER JOIN calendar_dates calendar_date_today
				   ON trip.service_id = calendar_date_today.service_id
		WHERE start_station.stop_id = :origin_station_id
		AND end_station.stop_id = :end_station_id
		AND origin_stop_sequence < dest_stop_sequence
		AND (calendar_date_today.date = :today or calendar_date_today.date = :tomorrow)
        ORDER BY today_cd, origin_depart_time
        """  # noqa: S608
    result = schedule.engine.connect().execute(
        text(sql_query),
        {
            "origin_station_id": start_station_id,
            "end_station_id": end_station_id,
            "today": now_date,
            "tomorrow": tomorrow_date,
            "limit": limit,
        },
    )
    # Create lookup timetable for today and possibly tomorrow, taking into
    # account any departures from yesterday scheduled after midnight,
    # as long as all departures are within the calendar date range.
    timetable = {}
    yesterday_start = today_start = tomorrow_start = None
    yesterday_last = today_last = ""
    for row_cursor in result:
        row = row_cursor._asdict()
        if row["yesterday"] == 1 and yesterday_date >= row["start_date"]:
            extras = {"day": "yesterday", "first": None, "last": False}
            if yesterday_start is None:
                yesterday_start = row["origin_depart_date"]
            if yesterday_start != row["origin_depart_date"]:
                idx = f"{now_date} {row['origin_depart_time']}"
                timetable[idx] = {**row, **extras}
                yesterday_last = idx
        if row["today"] == 1 or row["today_cd"] == 1:
            extras = {"day": "today", "first": False, "last": False}
            if today_start is None:
                today_start = row["origin_depart_date"]
                extras["first"] = True
            if today_start == row["origin_depart_date"]:
                idx_prefix = now_date
            else:
                idx_prefix = tomorrow_date
            idx = f"{idx_prefix} {row['origin_depart_time']}"
            timetable[idx] = {**row, **extras}
            today_last = idx
        if (
            "tomorrow" in row
            and row["tomorrow"] == 1
            and tomorrow_date <= row["end_date"]
        ):
            extras = {"day": "tomorrow", "first": False, "last": None}
            if tomorrow_start is None:
                tomorrow_start = row["origin_depart_date"]
                extras["first"] = True
            if tomorrow_start == row["origin_depart_date"]:
                idx = f"{tomorrow_date} {row['origin_depart_time']}"
                timetable[idx] = {**row, **extras}
    # Flag last departures.
    for idx in filter(None, [yesterday_last, today_last]):
        timetable[idx]["last"] = True
    item = {}
    for key in sorted(timetable.keys()):
        if datetime.datetime.strptime(key, "%Y-%m-%d %H:%M:%S") > now:
            item = timetable[key]
            _LOGGER.info(
                "Departure found for station %s @ %s -> %s", start_station_id, key, item
            )
            break

    if item == {}:
        return {}

    # create upcoming timetable
    timetable_remaining = []
    for key in sorted(timetable.keys()):
        if datetime.datetime.strptime(key, "%Y-%m-%d %H:%M:%S") > now:
            timetable_remaining.append(key)
    _LOGGER.debug(
        "Timetable Remaining Departures on this Start/Stop: %s", timetable_remaining
    )
    # create upcoming timetable with line info
    timetable_remaining_line = []
    for key2, value in sorted(timetable.items()):
        if datetime.datetime.strptime(key2, "%Y-%m-%d %H:%M:%S") > now:
            timetable_remaining_line.append(
                str(key2) + " (" + str(value["route_long_name"]) + ")"
            )
    _LOGGER.debug(
        "Timetable Remaining Departures on this Start/Stop, per line: %s",
        timetable_remaining_line,
    )
    # create upcoming timetable with headsign
    timetable_remaining_headsign = []
    for key2, value in sorted(timetable.items()):
        if datetime.datetime.strptime(key2, "%Y-%m-%d %H:%M:%S") > now:
            timetable_remaining_headsign.append(
                str(key2) + " (" + str(value["trip_headsign"]) + ")"
            )
    _LOGGER.debug(
        "Timetable Remaining Departures on this Start/Stop, with headsign: %s",
        timetable_remaining_headsign,
    )

    # Format arrival and departure dates and times, accounting for the
    # possibility of times crossing over midnight.
    origin_arrival = now
    if item["origin_arrival_time"] > item["origin_depart_time"]:
        origin_arrival -= datetime.timedelta(days=1)
    origin_arrival_time = (
        f"{origin_arrival.strftime(dt_util.DATE_STR_FORMAT)} "
        f"{item['origin_arrival_time']}"
    )

    origin_depart_time = f"{now_date} {item['origin_depart_time']}"

    dest_arrival = now
    if item["dest_arrival_time"] < item["origin_depart_time"]:
        dest_arrival += datetime.timedelta(days=1)
    dest_arrival_time = (
        f"{dest_arrival.strftime(dt_util.DATE_STR_FORMAT)} {item['dest_arrival_time']}"
    )

    dest_depart = dest_arrival
    if item["dest_depart_time"] < item["dest_arrival_time"]:
        dest_depart += datetime.timedelta(days=1)
    dest_depart_time = (
        f"{dest_depart.strftime(dt_util.DATE_STR_FORMAT)} {item['dest_depart_time']}"
    )

    depart_time = dt_util.parse_datetime(origin_depart_time).replace(tzinfo=timezone)
    arrival_time = dt_util.parse_datetime(dest_arrival_time).replace(tzinfo=timezone)

    origin_stop_time = {
        "Arrival Time": origin_arrival_time,
        "Departure Time": origin_depart_time,
        "Drop Off Type": item["origin_drop_off_type"],
        "Pickup Type": item["origin_pickup_type"],
        "Shape Dist Traveled": item["origin_dist_traveled"],
        "Headsign": item["origin_stop_headsign"],
        "Sequence": item["origin_stop_sequence"],
        "Timepoint": item["origin_stop_timepoint"],
    }

    destination_stop_time = {
        "Arrival Time": dest_arrival_time,
        "Departure Time": dest_depart_time,
        "Drop Off Type": item["dest_drop_off_type"],
        "Pickup Type": item["dest_pickup_type"],
        "Shape Dist Traveled": item["dest_dist_traveled"],
        "Headsign": item["dest_stop_headsign"],
        "Sequence": item["dest_stop_sequence"],
        "Timepoint": item["dest_stop_timepoint"],
    }

    return {
        "trip_id": item["trip_id"],
        "route_id": item["route_id"],
        "day": item["day"],
        "first": item["first"],
        "last": item["last"],
        "departure_time": depart_time,
        "arrival_time": arrival_time,
        "origin_stop_time": origin_stop_time,
        "destination_stop_time": destination_stop_time,
        "next_departures": timetable_remaining,
        "next_departures_lines": timetable_remaining_line,
        "next_departures_headsign": timetable_remaining_headsign,
        "gtfs_updated_at": dt_util.now().replace(tzinfo=None),
    }


def get_gtfs(hass, path, data, update=False):
    """Get gtfs file."""
    _LOGGER.debug("Getting gtfs with data: %s", data)
    filename = data["file"]
    url = data["url"]
    file = data["file"] + ".zip"
    gtfs_dir = hass.config.path(path)
    os.makedirs(gtfs_dir, exist_ok=True)
    if update and os.path.exists(os.path.join(gtfs_dir, file)):
        remove_datasource(hass, path, filename)
    if data["extract_from"] == "zip":
        if not os.path.exists(os.path.join(gtfs_dir, file)):
            _LOGGER.error("The given GTFS zipfile was not found")
            return "no_zip_file"
    if data["extract_from"] == "url":
        if not os.path.exists(os.path.join(gtfs_dir, file)):
            try:
                r = requests.get(url, allow_redirects=True)
                open(os.path.join(gtfs_dir, file), "wb").write(r.content)
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.error("The given URL or GTFS data file/folder was not found")
                return "no_data_file"
    (gtfs_root, _) = os.path.splitext(file)

    sqlite_file = f"{gtfs_root}.sqlite?check_same_thread=False"
    joined_path = os.path.join(gtfs_dir, sqlite_file)
    _LOGGER.debug("unpacking: %s", joined_path)
    gtfs = pygtfs.Schedule(joined_path)
    # check or wait for unpack
    journal = os.path.join(gtfs_dir, filename + ".sqlite-journal")
    while os.path.isfile(journal):
        time.sleep(10)
    if not gtfs.feeds:
        pygtfs.append_feed(gtfs, os.path.join(gtfs_dir, file))
    return gtfs


def get_route_list(schedule):
    sql_routes = f"""
    SELECT route_id, route_short_name, route_long_name from routes
    order by cast(route_id as decimal)
    """  # noqa: S608
    result = schedule.engine.connect().execute(
        text(sql_routes),
        {"q": "q"},
    )
    routes_list = []
    routes = []
    for row_cursor in result:
        row = row_cursor._asdict()
        routes_list.append(list(row_cursor))
    for x in routes_list:
        val = x[0] + ": " + x[1] + " (" + x[2] + ")"
        routes.append(val)
    _LOGGER.debug(f"routes: {routes}")
    return routes


def get_stop_list(schedule, route_id, direction):
    sql_stops = f"""
    SELECT distinct(s.stop_id), s.stop_name
    from trips t
    inner join routes r on r.route_id = t.route_id
    inner join stop_times st on st.trip_id = t.trip_id
    inner join stops s on s.stop_id = st.stop_id
    where  r.route_id = '{route_id}'
    and t.direction_id = {direction}
    order by st.stop_sequence
    """  # noqa: S608
    result = schedule.engine.connect().execute(
        text(sql_stops),
        {"q": "q"},
    )
    stops_list = []
    stops = []
    for row_cursor in result:
        row = row_cursor._asdict()
        stops_list.append(list(row_cursor))
    for x in stops_list:
        val = x[0] + ": " + x[1]
        stops.append(val)
    _LOGGER.debug(f"stops: {stops}")
    return stops


def get_datasources(hass, path) -> dict[str]:
    _LOGGER.debug(f"Datasources path: {path}")
    gtfs_dir = hass.config.path(path)
    os.makedirs(gtfs_dir, exist_ok=True)
    _LOGGER.debug(f"Datasources folder: {gtfs_dir}")
    files = os.listdir(gtfs_dir)
    _LOGGER.debug(f"Datasources files: {files}")
    datasources = []
    for file in files:
        if file.endswith(".sqlite"):
            datasources.append(file.split(".")[0])
    _LOGGER.debug(f"datasources: {datasources}")
    return datasources


def remove_datasource(hass, path, filename):
    gtfs_dir = hass.config.path(path)
    _LOGGER.info(f"Removing datasource: {os.path.join(gtfs_dir, filename)}.*")
    os.remove(os.path.join(gtfs_dir, filename + ".zip"))
    os.remove(os.path.join(gtfs_dir, filename + ".sqlite"))
    return "removed"


def check_datasource_index(schedule):
    sql_index_1 = f"""
    SELECT count(*) as checkidx
    FROM sqlite_master
    WHERE
    type= 'index' and tbl_name = 'stop_times' and name like '%trip_id%';
    """
    sql_index_2 = f"""
    SELECT count(*) as checkidx
    FROM sqlite_master
    WHERE
    type= 'index' and tbl_name = 'stop_times' and name like '%stop_id%';
    """
    sql_add_index_1 = f"""
    create index gtfs2_stop_times_trip_id on stop_times(trip_id)
    """
    sql_add_index_2 = f"""
    create index gtfs2_stop_times_stop_id on stop_times(stop_id)
    """
    result_1a = schedule.engine.connect().execute(
        text(sql_index_1),
        {"q": "q"},
    )
    for row_cursor in result_1a:
        _LOGGER.debug("IDX result1: %s", row_cursor._asdict())
        if row_cursor._asdict()['checkidx'] == 0:
            _LOGGER.info("Adding index 1 to improve performance")
            result_1b = schedule.engine.connect().execute(
            text(sql_add_index_1),
            {"q": "q"},
            )        
        
    result_2a = schedule.engine.connect().execute(
        text(sql_index_2),
        {"q": "q"},
    )
    for row_cursor in result_2a:
        _LOGGER.debug("IDX result2: %s", row_cursor._asdict())
        if row_cursor._asdict()['checkidx'] == 0:
            _LOGGER.info("Adding index 2 to improve performance")
            result_2b = schedule.engine.connect().execute(
            text(sql_add_index_2),
            {"q": "q"},
            )