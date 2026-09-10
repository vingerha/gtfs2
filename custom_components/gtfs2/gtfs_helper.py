"""Support for GTFS Integration."""
from __future__ import annotations

import datetime
import logging
import os
import glob
import json
import requests
import pygtfs
from sqlalchemy.sql import text
import multiprocessing
from multiprocessing import Process
from . import zip_file as zipfile
from pathlib import Path


import homeassistant.util.dt as dt_util
from homeassistant.core import HomeAssistant
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.helpers import entity_registry as er

from .const import (
    DEFAULT_PATH_GEOJSON,
    CONF_API_KEY,
    CONF_API_KEY_LOCATION,
    CONF_API_KEY_NAME,
    CONF_ACCEPT_HEADER_PB,
    DEFAULT_LOCAL_STOP_TIMERANGE, 
    DEFAULT_LOCAL_STOP_TIMERANGE_HISTORY,
    DEFAULT_LOCAL_STOP_RADIUS,
    DEFAULT_PATH_RT,
    DEFAULT_PATH,
    ICON,
    ICONS,
    DOMAIN,
    TIME_STR_FORMAT
    )
from .gtfs_rt_helper import get_rt_route_trip_statuses, get_gtfs_rt, safe_file_part, get_gtfs_feed_entities

_LOGGER = logging.getLogger(__name__)


def _fetch_departure_rows(route_type, origin, destination, schedule):
    """Run the static-GTFS SQL query and return matching rows as plain dicts."""
    if route_type == "2":
        route_type_where = f"route.route_type in (2,100,101,102,103,104,105,106,107,108,109,110,111,112,113,114,115,116,117)"
        start_station_id = str(origin)+'%'
        end_station_id = str(destination)+'%'
        start_station_where = f"AND origin_stop_time.stop_id in (select stop_id from stops where stop_name like :origin_station_id)"
        end_station_where = f"AND destination_stop_time.stop_id in (select stop_id from stops where stop_name like :end_station_id)"
        _LOGGER.debug("Setting up TRAIN Route for start/end : %s / %s ", start_station_id, end_station_id)
    else:
        route_type_where = "1=1"
        start_station_id = origin.split(': ')[0]
        end_station_id = destination.split(': ')[0]
        start_station_where = f"AND origin_stop_time.stop_id = :origin_station_id"
        end_station_where = f"AND destination_stop_time.stop_id = :end_station_id"
        _LOGGER.debug("Setting up Route for start/end : %s / %s ", start_station_id, end_station_id)

    limit = 24 * 60 * 60 * 2
    ## QUERY candidate_trips and cal_expand are used to construct a list of valida_dates, i.e a list where services run
    ## valid_dates is then used in the main query
    sql_query = f"""
       WITH RECURSIVE
          candidate_trips AS MATERIALIZED (
            SELECT trip.trip_id, trip.service_id,
                   origin_stop_time.stop_id AS origin_stop_id,
                   destination_stop_time.stop_id AS destination_stop_id
            FROM trips trip
            INNER JOIN routes route ON route.route_id = trip.route_id
            INNER JOIN stop_times origin_stop_time ON trip.trip_id = origin_stop_time.trip_id
            INNER JOIN stop_times destination_stop_time ON trip.trip_id = destination_stop_time.trip_id
            WHERE {route_type_where}
              {start_station_where}
              {end_station_where}
              AND origin_stop_time.stop_sequence < destination_stop_time.stop_sequence
          ),
          cal_expand(service_id, d, end_date, monday, tuesday, wednesday, thursday, friday, saturday, sunday) AS (
            SELECT service_id, MAX(start_date, date('now', 'localtime')), end_date,
                   monday, tuesday, wednesday, thursday, friday, saturday, sunday
            FROM calendar
            WHERE service_id IN (SELECT service_id FROM candidate_trips)
            UNION ALL
            SELECT service_id, date(d, '+1 day'), end_date, monday, tuesday, wednesday, thursday, friday, saturday, sunday
            FROM cal_expand
            WHERE d < end_date
          ),
          valid_dates AS MATERIALIZED (
            SELECT service_id, d AS date
            FROM cal_expand
            WHERE (
                (CAST(strftime('%w', d) AS INTEGER) = 0 AND sunday    = 1) OR
                (CAST(strftime('%w', d) AS INTEGER) = 1 AND monday    = 1) OR
                (CAST(strftime('%w', d) AS INTEGER) = 2 AND tuesday   = 1) OR
                (CAST(strftime('%w', d) AS INTEGER) = 3 AND wednesday = 1) OR
                (CAST(strftime('%w', d) AS INTEGER) = 4 AND thursday  = 1) OR
                (CAST(strftime('%w', d) AS INTEGER) = 5 AND friday    = 1) OR
                (CAST(strftime('%w', d) AS INTEGER) = 6 AND saturday  = 1)
            )
            AND NOT EXISTS (
              SELECT 1 FROM calendar_dates cd
              WHERE cd.service_id = cal_expand.service_id
                AND cd.date = cal_expand.d AND cd.exception_type = 2
            )
            UNION
                SELECT cd2.service_id, cd2.date
                FROM calendar_dates cd2
                WHERE cd2.service_id IN (SELECT service_id FROM candidate_trips)
                  AND cd2.exception_type = 1
            )
        SELECT distinct trip.trip_id, trip.route_id, trip.trip_headsign, trip.direction_id, trip.trip_short_name,
               route.route_long_name, route.route_short_name,
               start_station.stop_id as origin_stop_id,
               start_station.stop_name as origin_stop_name,
               start_station.stop_timezone as origin_stop_timezone,
               agency.agency_timezone as agency_timezone,
               time(origin_stop_time.arrival_time) AS origin_arrival_time,
               time(origin_stop_time.departure_time) AS origin_depart_time,
               vd.date AS origin_depart_date,
               origin_stop_time.drop_off_type AS origin_drop_off_type,
               origin_stop_time.pickup_type AS origin_pickup_type,
               origin_stop_time.shape_dist_traveled AS origin_dist_traveled,
               origin_stop_time.stop_headsign AS origin_stop_headsign,
               origin_stop_time.stop_sequence AS origin_stop_sequence,
               origin_stop_time.timepoint AS origin_stop_timepoint,
               end_station.stop_id as dest_stop_id,
               end_station.stop_name as dest_stop_name,
               end_station.stop_timezone as dest_stop_timezone,
               time(destination_stop_time.arrival_time) AS dest_arrival_time,
               time(destination_stop_time.departure_time) AS dest_depart_time,
               destination_stop_time.drop_off_type AS dest_drop_off_type,
               destination_stop_time.pickup_type AS dest_pickup_type,
               destination_stop_time.shape_dist_traveled AS dest_dist_traveled,
               destination_stop_time.stop_headsign AS dest_stop_headsign,
               destination_stop_time.stop_sequence AS dest_stop_sequence,
               destination_stop_time.timepoint AS dest_stop_timepoint
        FROM candidate_trips ct
        INNER JOIN trips trip ON trip.trip_id = ct.trip_id
        INNER JOIN stop_times origin_stop_time ON origin_stop_time.trip_id = trip.trip_id AND origin_stop_time.stop_id = ct.origin_stop_id
        INNER JOIN stops start_station ON origin_stop_time.stop_id = start_station.stop_id
        INNER JOIN stop_times destination_stop_time ON destination_stop_time.trip_id = trip.trip_id AND destination_stop_time.stop_id = ct.destination_stop_id
        INNER JOIN stops end_station ON destination_stop_time.stop_id = end_station.stop_id
        INNER JOIN routes route ON route.route_id = trip.route_id
        INNER JOIN agency agency ON route.agency_id = agency.agency_id
        INNER JOIN valid_dates vd ON vd.service_id = trip.service_id
        WHERE datetime(
                vd.date || ' ' || time(origin_stop_time.departure_time),
                CASE WHEN date(origin_stop_time.departure_time) = '1970-01-02'
                THEN '+1 day' ELSE '+0 day' END
              ) >= datetime('now', 'localtime')
        ORDER BY vd.date, origin_stop_time.departure_time
        LIMIT 30;
    """  # noqa: S608

    # Create lookup timetable taking into
    # account any departures from yesterday scheduled after midnight,
    # as long as all departures are within the calendar date range.
    query_params = {
        "route_type_where": route_type_where,
        "start_station_where": start_station_where,
        "end_station_where": end_station_where,
        "origin_station_id": start_station_id,
        "end_station_id": end_station_id
    }

    log_params = {
        **query_params,
    }

    _LOGGER.debug("SQL statement:\n%s", sql_query)
    _LOGGER.debug("SQL parameters:\n%s", log_params)      
                        
    with schedule.engine.connect() as conn:
        result = conn.execute(
            text(sql_query),
            {
                "origin_station_id": start_station_id,
                "end_station_id": end_station_id,
                "limit": limit,
                "route_type": route_type,
            },
        )
        rows = result.fetchall()

    return [row_cursor._asdict() for row_cursor in rows], start_station_id


def _interpret_departure_rows(hass, rows, start_station_id, now, now_local_tz,
                               now_date_local_tz, now_time):
    """Turn raw SQL-shaped rows into the `next_departure` dict."""
    _LOGGER.debug("Interpret rows: %s", rows)
    timetable = {}
    for row in rows:
        service_date = row["origin_depart_date"]
        depart_dt_str = f"{service_date} {row['origin_depart_time']}"
        try:
            depart_dt = datetime.datetime.strptime(depart_dt_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            _LOGGER.warning("Could not parse departure datetime: %s", depart_dt_str)
            continue

        if depart_dt <= now:
            continue  # already departed; SQL only filters by date, not time-of-day

        day_label = service_date  # real ISO date beyond tomorrow

        idx = (depart_dt_str, str(row["trip_id"]))
        if idx in timetable:
            _LOGGER.warning("Duplicate timetable key: %s, trip_id: %s", idx, row["trip_id"])
            continue
        timetable[idx] = {**row, "day": day_label, "first": False, "last": False}

    dates_seen = {}
    for idx in sorted(timetable.keys()):
        d = timetable[idx]["origin_depart_date"]
        dates_seen.setdefault(d, []).append(idx)
    for date_key, idxs in dates_seen.items():
        timetable[idxs[0]]["first"] = True
        timetable[idxs[-1]]["last"] = True

    item = {}
    for key in sorted(timetable.keys()):
        item = timetable[key]
        _LOGGER.info("Departure(s) found for station %s @ %s -> %s", start_station_id, key, item)
        break
    _LOGGER.debug("Item(s) from SQL: %s", item)
        
    if item == {}:
        data_returned = {        
        "gtfs_updated_at": dt_util.utcnow().isoformat(),
        }
        _LOGGER.info("No items found in gtfs")
        return {}

    # Define timezone related attribs
    if hass.config.time_zone is None:
        _LOGGER.error("Timezone is not set in Home Assistant configuration")
        timezone = "UTC"
    else:
        timezone = dt_util.get_time_zone(hass.config.time_zone)
        _LOGGER.debug("Timezone HA: %s",timezone)
    _LOGGER.debug("Default timezone: %s",timezone)
    _LOGGER.debug("Agency timezone: %s",item["agency_timezone"])
    _LOGGER.debug("Origin stop timezone: %s",item["origin_stop_timezone"])
    _LOGGER.debug("Dest stop timezone: %s",item["dest_stop_timezone"])
    if item["agency_timezone"] is not None:
        _LOGGER.debug("Setting Orig & Dest TZ based on Agency: %s",item["agency_timezone"])
        timezone = dt_util.get_time_zone(item["agency_timezone"])
        timezone_dest = dt_util.get_time_zone(item["agency_timezone"])  
    elif item["origin_stop_timezone"] is not None:    
        _LOGGER.debug("Setting Orig & Dest TZ based on origin stop: %s",item["origin_stop_timezone"])
        timezone = dt_util.get_time_zone(item["origin_stop_timezone"])
        timezone_dest = dt_util.get_time_zone(item["origin_stop_timezone"]) 
    if item["dest_stop_timezone"] is not None and item["agency_timezone"] is None:
        _LOGGER.debug("Setting Dest TZ based on dest stop: %s",item["dest_stop_timezone"])
        timezone_dest = dt_util.get_time_zone(item["dest_stop_timezone"])  
    else:
        timezone_dest = timezone
    _LOGGER.debug("Defined orig timezone: %s, dest timezone: %s",timezone,timezone_dest)
    _LOGGER.debug("Defined now incl. offset (if configured): %s",now_local_tz)

    # create upcoming timetable, use timezone before resetting to UTC and reset 'item' to match with timezone
    timetable_remaining = []
    ix = 0
    item = {}
    max_remaining = 10
    for key in sorted(timetable.keys()):
        upcoming = datetime.datetime.strptime(key[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone)
        #_LOGGER.debug ("Upcoming_departure_in_defined_timezone: %s, Now_in_defined_timezone_plus_offset: %s, key: %s, ix: %s", upcoming, now_local_tz, key, ix)
        if upcoming > now_local_tz:
            if ix == 0 :
                _LOGGER.debug("Resetting item")
                item = timetable[key]
                ix = ix + 1
            _LOGGER.debug("Adding departure in defined timezone: %s, Now_in_defined_timezone_plus_offset: %s, key: %s, ix: %s", upcoming, now_local_tz, key, ix)
            timetable_remaining.append(dt_util.as_utc(upcoming).isoformat())
            if len(timetable_remaining) >= max_remaining:
                break
    _LOGGER.debug("Timetable Remaining Departures on this Start/Stop: %s", timetable_remaining)
    if item == {}:
        data_returned = {        
        "gtfs_updated_at": dt_util.utcnow().isoformat(),
        }
        _LOGGER.info("No items found in gtfs")
        return {}
    
    # create upcoming timetable with line info, headsign and trips
    timetable_remaining_line = []
    timetable_remaining_headsign = []
    timetable_upcoming_trips = []
    timetable_upcoming_arrivals = []
    max_remaining = 10
    count = 0
    for key, value in sorted(timetable.items()):
        upcoming = datetime.datetime.strptime(key[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone)
        upcoming_arrival = datetime.datetime.combine(
            upcoming.date(),
            datetime.datetime.strptime(value["dest_arrival_time"],"%H:%M:%S").time()).replace(tzinfo=timezone_dest)
        # Arrival after midnight -> next calendar day
        if upcoming_arrival.time() < upcoming.time():
            upcoming_arrival += datetime.timedelta(days=1)
        #_LOGGER.debug ("Upcoming list values for departure in defined tz: %s, Now_in_defined_timezone_plus_offset: %s, key: %s, value %s", upcoming, now_local_tz, key, value)
        if upcoming > now_local_tz:
            _LOGGER.debug("Adding list item for departure/key: %s, Upcoming: %s, Value: %s", key, upcoming, value )
            timetable_remaining_line.append(
                str(dt_util.as_utc(upcoming).isoformat())  + " (" + str(value["route_short_name"]) +  str( ("/" + value["route_long_name"])  if value["route_long_name"] else "") + ")"
            )
            timetable_remaining_headsign.append(
                str(dt_util.as_utc(upcoming).isoformat()) + " (" + str(value["trip_headsign"]) + ")"
            )
            timetable_upcoming_trips.append(
                str(value["trip_id"])
            )
            timetable_upcoming_arrivals.append(
                dt_util.as_utc(upcoming_arrival).isoformat()
            )
            count += 1
            if count >= max_remaining:
                break
            
    #_LOGGER.debug(
    #    "Timetable Remaining Departures on this Start/Stop, per line: %s",
    #    timetable_remaining_line,
    #)
    #_LOGGER.debug(
    #    "Timetable Remaining Departures on this Start/Stop, with headsign: %s",
    #    timetable_remaining_headsign,
    #)
    #_LOGGER.debug(
    #    "Timetable Remaining Trips on this Start/Stop: %s",
    #    timetable_upcoming_trips,
    #)
    #_LOGGER.debug(
    #    "Timetable arrival times on this Start/Stop: %s",
    #    timetable_upcoming_arrivals,
    #)


    # Format arrival and departure dates and times, accounting for the
    # possibility of times crossing over midnight.
    origin_date = datetime.datetime.strptime(item["origin_depart_date"], "%Y-%m-%d")
    origin_arrival = origin_date
    dest_arrival = origin_date
    origin_depart_time = f"{item['origin_depart_date']} {item['origin_depart_time']}"

    if item["origin_arrival_time"] > item["origin_depart_time"]:
        origin_arrival -= datetime.timedelta(days=1)
    origin_arrival_time = (
        f"{origin_arrival.strftime(dt_util.DATE_STR_FORMAT)} "
        f"{item['origin_arrival_time']}"
    )

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
 
    _LOGGER.debug("Orig depart time: %s", origin_depart_time)
    
    depart_time = dt_util.parse_datetime(origin_depart_time).replace(tzinfo=timezone)
    arrival_time = dt_util.parse_datetime(dest_arrival_time).replace(tzinfo=timezone_dest)
    origin_arrival_time = dt_util.as_utc(datetime.datetime.strptime(origin_arrival_time, "%Y-%m-%d %H:%M:%S")).isoformat()
    origin_depart_time = dt_util.as_utc(datetime.datetime.strptime(origin_depart_time, "%Y-%m-%d %H:%M:%S")).isoformat()
    dest_arrival_time = dt_util.as_utc(datetime.datetime.strptime(dest_arrival_time, "%Y-%m-%d %H:%M:%S")).isoformat()
    dest_depart_time = dt_util.as_utc(datetime.datetime.strptime(dest_depart_time, "%Y-%m-%d %H:%M:%S")).isoformat()
    
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
    
    data_returned = {
        "trip_id": item["trip_id"],
        "route_id": item["route_id"],
        "route_short_name": item["route_short_name"],
        "trip_direction_id": item["direction_id"],
        "trip_short_name": item["trip_short_name"],
        "day": item["day"],
        "first": item["first"],
        "last": item["last"],
        "origin_stop_id": item["origin_stop_id"],
        "origin_stop_sequence": item["origin_stop_sequence"],
        "origin_stop_name": item["origin_stop_name"],
        "departure_time": depart_time,
        "arrival_time": arrival_time,
        "origin_stop_time": origin_stop_time,
        "origin_stop_timezone": item["origin_stop_timezone"],
        "destination_stop_time": destination_stop_time,
        "destination_stop_timezone": item["dest_stop_timezone"],
        "destination_stop_id": item["dest_stop_id"],
        "destination_stop_name": item["dest_stop_name"],
        "next_departures": timetable_remaining,
        "next_departures_lines": timetable_remaining_line,
        "next_departures_headsign": timetable_remaining_headsign,
        "next_departures_trip_id": timetable_upcoming_trips,
        "next_departures_destination_arrival_times": timetable_upcoming_arrivals,
    }
    
    return data_returned

def get_next_departure(hass, _data):
    """Get next departures from data."""
    _LOGGER.debug("Get next departure with data: %s", _data)
    if check_extracting(hass, _data['gtfs_dir'],_data['file']):
        _LOGGER.debug("Cannot get next departures on this datasource as still unpacking: %s", _data["file"])
        return {}

    schedule = _data["schedule"]
    route_type = _data["route_type"]

    offset = _data["offset"]
    now = dt_util.now().replace(tzinfo=None) + datetime.timedelta(minutes=offset)
    now_local_tz = dt_util.now() + datetime.timedelta(minutes=offset)
    now_date = now.strftime(dt_util.DATE_STR_FORMAT)
    now_date_local_tz = now_local_tz.strftime(dt_util.DATE_STR_FORMAT)
    now_time = now.strftime(TIME_STR_FORMAT)

    # Fetch all departures

    rows, start_station_id = _fetch_departure_rows(
        route_type, _data["origin"], _data["destination"], schedule,
    )

    return _interpret_departure_rows(
        hass, rows, start_station_id, now, now_local_tz,
        now_date_local_tz, now_time
    )

def get_gtfs(hass, path, data, update=False):
    _LOGGER.debug("Getting gtfs with data: %s", data)
    _headers = None
    gtfs_dir = hass.config.path(path)
    os.makedirs(gtfs_dir, exist_ok=True)
    filename = data["file"]
    url = data["url"]
    if data.get(CONF_API_KEY_LOCATION, None) == "query_string":
      if data.get(CONF_API_KEY, None):
        url = url + "?" + data[CONF_API_KEY_NAME] + "=" + data[CONF_API_KEY]
    if data.get(CONF_API_KEY_LOCATION, None) == "header":
      if data.get(CONF_API_KEY, None):
        _headers = {data[CONF_API_KEY_NAME]: data[CONF_API_KEY]}
    file = data["file"] + ".zip"
    sqlite = data["file"] + ".sqlite"
    check_source_dates = data.get("check_source_dates", False)
    journal = os.path.join(gtfs_dir, filename + ".sqlite-journal")
    if check_extracting(hass, gtfs_dir,filename) and not update :
        _LOGGER.debug("Cannot use this datasource as still unpacking: %s", filename)
        return "extracting"
    if update and data["extract_from"] == "url":
        _pending_remove = os.path.exists(os.path.join(gtfs_dir, file))
    else:
        _pending_remove = False
    if update and data["extract_from"] == "zip" and os.path.exists(os.path.join(gtfs_dir, file)) and os.path.exists(os.path.join(gtfs_dir, sqlite)):
        os.remove(os.path.join(gtfs_dir, sqlite))      
    if data["extract_from"] == "zip":
        if not os.path.exists(os.path.join(gtfs_dir, file)):
            _LOGGER.error("The given GTFS zipfile was not found")
            return "no_zip_file"
    if data["extract_from"] == "url":
        if update or not os.path.exists(os.path.join(gtfs_dir, file)):
            try:
                r = requests.get(url,headers=_headers, allow_redirects=True,timeout=15)
                r.raise_for_status()
                if _pending_remove:
                    remove_datasource(hass, path, filename, True)
                open(os.path.join(gtfs_dir, file), "wb").write(r.content)
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.error("The given URL or GTFS data file/folder was not found: %s", ex)
                return "no_data_file"                
    
    # if update (servicecall) then check if new file does not only have future dates
    if check_source_dates:
        if update and not check_calendar_dates_from_zip(gtfs_dir, file):
            _LOGGER.info('New file contains only dates in the future, extracting terminated')
            return
    
    (gtfs_root, _) = os.path.splitext(file)    
    sqlite_file = f"{gtfs_root}.sqlite?check_same_thread=False"
    joined_path = os.path.join(gtfs_dir, sqlite_file)  

    gtfs = pygtfs.Schedule(joined_path)
   
    if not gtfs.feeds: 
        if data.get("clean_feed_info", False):
            _fork_ctx = multiprocessing.get_context("fork")
            extract = _fork_ctx.Process(target=extract_from_zip, args = (hass, gtfs,gtfs_dir,file,['shapes.txt','transfers.txt','fare_attributes.txt','levels.txt','pathways.txt','translations.txt','feed_info.txt']))
        else: 
            _fork_ctx = multiprocessing.get_context("fork")
            extract = _fork_ctx.Process(target=extract_from_zip, args = (hass, gtfs,gtfs_dir,file,['shapes.txt','transfers.txt','fare_attributes.txt','levels.txt','pathways.txt','translations.txt']))
        extract.start()
        extract.join()
        _LOGGER.info("Exiting main after start subprocess for unpacking: %s", file)
        return "extracting"
    return gtfs

def extract_from_zip(hass, gtfs, gtfs_dir, file, remove_file):
    _LOGGER.debug("Extracting gtfs file: %s", file)
    # first remove shapes from zip to avoid possibly very large db 
    clean = remove_from_zip(remove_file,gtfs_dir, file[:-4])
    if os.fork() != 0:
        return
    pygtfs.append_feed(gtfs, os.path.join(gtfs_dir, file))
    check_datasource_index(hass, gtfs, gtfs_dir, file[:-4])

    
def check_calendar_dates_from_zip(gtfs_dir,file):
    _LOGGER.debug("Checking if file contains only future data: %s ", file)
    filename = os.path.join(gtfs_dir, file)
    # Rename existing sqlite if existing (i.e. in case of a fresh install)
    if os.path.exists(os.path.join(gtfs_dir, file[:-4] + ".sqlite")):
        os.rename (os.path.join(gtfs_dir, file[:-4] + '.sqlite'), os.path.join(gtfs_dir, file[:-4] + '.sqlite_current'))
    #Load the ZIP archive
    zin = zipfile.ZipFile (f"{os.path.join(gtfs_dir, filename)}", 'r')
    check_list=[]
    try:
        for item in zin.infolist():
            if item.filename[0:8] == 'calendar' :
                if item.filename == 'calendar.txt':
                    column = 'start_date'
                else:
                    column = 'date'
                with open(zin.extract(item.filename)) as f:
                    header = f.readline().strip('\n')   #
                    data = f.readlines() 
                    index =header.replace('"','').split(',').index(column)           
                    list = []
                    for line in data:
                        list.append(line.split(',')[index])
                    check_list.append(min(list))
        min_date = datetime.datetime.strptime(min(check_list),"%Y%m%d")
        _LOGGER.debug("Youngest calender date from new files: %s, is: %s", check_list, min_date)
        if min_date > datetime.datetime.now()  :
            _LOGGER.info("New file contains only dates in the future, keeping current")
            if os.path.exists(os.path.join(gtfs_dir, file[:-4] + ".sqlite")):
                os.remove(os.path.join(gtfs_dir, file[:-4] + ".sqlite"))
            os.rename (os.path.join(gtfs_dir, file[:-4] + '.sqlite_current'), os.path.join(gtfs_dir, file[:-4] + '.sqlite'))
            return False
    except Exception as ex:
        _LOGGER.error("Error getting earliest dates from zip, continuing with extract, error: %s", ex)
        _LOGGER.debug(f"Removing/restoring sqlite after error")
        if os.path.exists(os.path.join(gtfs_dir, file[:-4] + ".sqlite")):
            os.remove(os.path.join(gtfs_dir, file[:-4] + ".sqlite"))
        if os.path.exists(os.path.join(gtfs_dir, file[:-4] + ".sqlite")):
            os.rename (os.path.join(gtfs_dir, file[:-4] + '.sqlite'), os.path.join(gtfs_dir, file[:-4] + '.sqlite_current'))
        return False
    _LOGGER.debug(f"New file is not containing only newer dates, removing current/copied sqlite")    
    if os.path.exists(os.path.join(gtfs_dir, file[:-4] + ".sqlite_current")):
        os.remove(os.path.join(gtfs_dir, file[:-4] + ".sqlite_current"))
    return True

def remove_from_zip(delmelist,gtfs_dir,file):
    _LOGGER.debug("Removing data: %s , from zipfile: %s", delmelist, file)
    tempfile = file + "_temp.zip"
    tempfile_out = file + "_temp_out.zip"
    filename = file + ".zip"
    os.rename (os.path.join(gtfs_dir, filename), os.path.join(gtfs_dir, tempfile))
    # Load the ZIP archive
    try: 
        zin = zipfile.ZipFile (f"{os.path.join(gtfs_dir, tempfile)}", 'r')
        zout = zipfile.ZipFile (f"{os.path.join(gtfs_dir, tempfile_out)}", 'w')
        for item in zin.infolist():
            buffer = zin.read(item.filename)
            if (item.filename not in delmelist):
                zout.writestr(item, buffer)
        zout.close()
        zin.close()
        os.rename(os.path.join(gtfs_dir, tempfile_out), os.path.join(gtfs_dir, filename))
        os.remove(os.path.join(gtfs_dir, tempfile)) 
    except Exception as ex:  # pylint: disable=broad-except
        print('Something went wrong with the zipfile... : ', ex)
        return     


def get_route_list(schedule, data):
    _LOGGER.debug("Getting routes with data: %s", data)
    route_type_where = ""
    agency_where = ""
    if data["agency"].split(': ')[0] != "0":
        agency_where = f"and r.agency_id = '{data['agency'].split(': ')[0]}'"
    if data["route_type"] != "99":
        route_type_where = f"and route_type = {data['route_type']}"
    sql_routes = f"""
    SELECT r.route_type, r.route_id, r.route_short_name, r.route_long_name, a.agency_name
    from routes r
    left join agency a on a.agency_id = r.agency_id
    where 1=1
    {route_type_where}
    {agency_where}
    order by agency_name, cast(route_id as decimal)
    """  # noqa: S608
    routes_list = []
    routes = []
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql_routes), {"q": "q"}).fetchall()
    for row_cursor in rows:
        row = row_cursor._asdict()
        routes_list.append(list(row_cursor))
    for x in routes_list:
        val = str(x[0]) + "##" + str(x[1]) + ": (" + str(x[2]) + " - " + str(x[3]) + ") " + str(x[4])
        routes.append(val)
    _LOGGER.debug(f"routes: {routes}")
    return routes

def get_stop_list(schedule, route_id, direction):
    _LOGGER.debug("Getting stops list for route: %s", route_id)
    sql_stops = f"""
    SELECT distinct(s.stop_id), s.stop_name, st.stop_sequence
    from trips t
    inner join stop_times st on st.trip_id = t.trip_id
    inner join stops s on s.stop_id = st.stop_id
    where  t.route_id = '{route_id}'
    and (t.direction_id = {direction} or t.direction_id is null)
    order by st.stop_sequence
    """  # noqa: S608
    stops_list = []
    stops = []
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql_stops), {"q": "q"}).fetchall()
    for row_cursor in rows:
        row = row_cursor._asdict()
        stops_list.append(list(row_cursor))
    for x in stops_list:
        val = x[0] + ": " + x[1] + ' (' + str(x[2]) + ')'
        stops.append(val)
    _LOGGER.debug(f"Route stops: {stops}")
    return stops 
    
def get_agency_list(schedule, data):
    _LOGGER.debug("Getting agencies with data: %s", data)
    sql_agencies = f"""
    SELECT a.agency_id, a.agency_name 
    from agency a
    order by a.agency_name
    """
    agencies_list = []
    agencies = []
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql_agencies), {"q": "q"}).fetchall()
    for row_cursor in rows:
        row = row_cursor._asdict()
        agencies_list.append(list(row_cursor))
    for x in agencies_list:
        val = str(x[0]) + ": " + str(x[1])
        agencies.append(val)
    _LOGGER.debug(f"agencies: {agencies}")
    return agencies

async def get_datasources(hass, path) -> dict[str]:
    _LOGGER.debug(f"Getting datasources for path: {path}")
    gtfs_dir = hass.config.path(path)
    os.makedirs(gtfs_dir, exist_ok=True)
    files = await hass.async_add_executor_job(
            os.listdir, gtfs_dir)
    datasources = []
    for file in files:
        if file.endswith(".sqlite"):
            datasources.append(file.split(".")[0])        
    _LOGGER.debug(f"Datasources in folder: {datasources}")
    return datasources

def remove_datasource(hass, path, filename, include_sqlite):
    gtfs_dir = hass.config.path(path)
    _LOGGER.info(f"Removing datasource: {os.path.join(gtfs_dir, filename)}.*")
    if include_sqlite and os.path.exists(os.path.join(gtfs_dir, filename + ".sqlite")):
        os.remove(os.path.join(gtfs_dir, filename + ".sqlite"))
    if os.path.exists(os.path.join(gtfs_dir, filename + "_temp.zip")):     
        os.remove(os.path.join(gtfs_dir, filename + "_temp.zip"))
    if os.path.exists(os.path.join(gtfs_dir, filename + "_temp_out.zip")):        
        os.remove(os.path.join(gtfs_dir, filename + "_temp_out.zip"))
    if os.path.exists(os.path.join(gtfs_dir, filename + ".sqlite-journal")):        
        os.remove(os.path.join(gtfs_dir, filename + ".sqlite-journal"))
    if os.path.exists(os.path.join(gtfs_dir, filename + ".zip")):        
        os.remove(os.path.join(gtfs_dir, filename + ".zip"))        
    return "removed"
    
def check_extracting(hass, gtfs_dir,file):
    _LOGGER.debug(f"Checking if extracting: %s", file)
    gtfs_dir = hass.config.path(gtfs_dir)
    filename = file
    journal = os.path.join(gtfs_dir, filename + ".sqlite-journal")
    tempzip = os.path.join(gtfs_dir, filename + "_temp.zip")
    if os.path.exists(journal)  or os.path.exists(tempzip):
        _LOGGER.debug("Extracting: yes")
        return True
    return False    


def check_datasource_index(hass, schedule, gtfs_dir, file):
    _LOGGER.debug("Check datasource index for file: %s", file)
    if check_extracting(hass, gtfs_dir,file):
        _LOGGER.warning("Cannot check indexes on this datasource as still unpacking: %s", file)
        return
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
    sql_index_3 = f"""
    SELECT count(*) as checkidx
    FROM sqlite_master
    WHERE
    type= 'index' and tbl_name = 'shapes' and name like '%shape_id%';
    """
    sql_index_4 = f"""
    SELECT count(*) as checkidx
    FROM sqlite_master
    WHERE
    type= 'index' and tbl_name = 'stops' and name like '%stop_name%';
    """
    sql_index_5 = f"""
    SELECT count(*) as checkidx
    FROM sqlite_master
    WHERE
    type= 'index' and tbl_name = 'routes' and name like '%route_type%';
    """
    sql_index_6 = f"""
    SELECT count(*) as checkidx
    FROM sqlite_master
    WHERE
    type= 'index' and tbl_name = 'trips' and name like '%route_id%';
    """
    sql_add_index_1 = f"""
    create index gtfs2_stop_times_trip_id on stop_times(trip_id)
    """
    sql_add_index_2 = f"""
    create index gtfs2_stop_times_stop_id on stop_times(stop_id)
    """
    sql_add_index_3 = f"""
    create index gtfs2_shapes_shape_id on shapes(shape_id)
    """
    sql_add_index_4 = f"""
    create index gtfs2_stops_stop_name on stops(stop_name)
    """    
    sql_add_index_5 = f"""
    create index gtfs2_routes_route_type on routes(route_type)
    """
    sql_add_index_6 = f"""
    create index gtfs2_trips_route_id on trips(route_id)
    """
    sql_check_route_agency = f"""
    SELECT count(*) as check_agency
    FROM routes where agency_id='None'
    """
    sql_fix_route_agency = f"""
    update routes set agency_id = (select agency_id from agency limit 1)
        where agency_id='None'
    """
    
    with schedule.engine.connect() as conn:
        rows_1a = conn.execute(text(sql_index_1), {"q": "q"}).fetchall()
    for row_cursor in rows_1a:
        _LOGGER.debug("IDX result1: %s", row_cursor._asdict())
        if row_cursor._asdict()['checkidx'] == 0:
            _LOGGER.warning("Adding index 1 to improve performance")
            with schedule.engine.connect() as conn:
                conn.execute(text(sql_add_index_1), {"q": "q"})       
        
    with schedule.engine.connect() as conn:
        rows_2a = conn.execute(text(sql_index_2), {"q": "q"}).fetchall()
    for row_cursor in rows_2a:
        _LOGGER.debug("IDX result2: %s", row_cursor._asdict())
        if row_cursor._asdict()['checkidx'] == 0:
            _LOGGER.warning("Adding index 2 to improve performance")
            with schedule.engine.connect() as conn:
                conn.execute(text(sql_add_index_2), {"q": "q"})
                
    with schedule.engine.connect() as conn:
        rows_3a = conn.execute(text(sql_index_3), {"q": "q"}).fetchall()
    for row_cursor in rows_3a:
        _LOGGER.debug("IDX result3: %s", row_cursor._asdict())
        if row_cursor._asdict()['checkidx'] == 0:
            _LOGGER.warning("Adding index 3 to improve performance")
            with schedule.engine.connect() as conn:
                conn.execute(text(sql_add_index_3), {"q": "q"})
                
    with schedule.engine.connect() as conn:
        rows_4a = conn.execute(text(sql_index_4), {"q": "q"}).fetchall()
    for row_cursor in rows_4a:
        _LOGGER.debug("IDX result4: %s", row_cursor._asdict())
        if row_cursor._asdict()['checkidx'] == 0:
            _LOGGER.warning("Adding index 4 to improve performance")
            with schedule.engine.connect() as conn:
                conn.execute(text(sql_add_index_4), {"q": "q"})
                
    with schedule.engine.connect() as conn:
        rows_5a = conn.execute(text(sql_index_5), {"q": "q"}).fetchall()
    for row_cursor in rows_5a:
        _LOGGER.debug("IDX result5: %s", row_cursor._asdict())
        if row_cursor._asdict()['checkidx'] == 0:
            _LOGGER.warning("Adding index 5 to improve performance")
            with schedule.engine.connect() as conn:
                conn.execute(text(sql_add_index_5), {"q": "q"})

    with schedule.engine.connect() as conn:
        rows_6a = conn.execute(text(sql_index_6), {"q": "q"}).fetchall()
    for row_cursor in rows_6a:
        _LOGGER.debug("IDX result6: %s", row_cursor._asdict())
        if row_cursor._asdict()['checkidx'] == 0:
            _LOGGER.warning("Adding index 6 to improve performance")
            with schedule.engine.connect() as conn:
                conn.execute(text(sql_add_index_6), {"q": "q"})

    with schedule.engine.connect() as conn:
        rows_8a = conn.execute(text(sql_check_route_agency), {"q": "q"}).fetchall()
    for row_cursor in rows_8a:
        _LOGGER.debug("Agency 'None' in routes: %s", row_cursor._asdict())
        if row_cursor._asdict()['check_agency'] > 0:
            _LOGGER.warning("Fix missing agency_id in routes table")
            with schedule.engine.connect() as conn:
                conn.execute(text(sql_fix_route_agency), {"q": "q"})
                conn.commit()

def create_trip_geojson(self):
    # not in use, awaiting geojson in HA-core to cover this type of geometry
    _LOGGER.debug("Create geojson with data: %s", self._data)
    schedule = self._data["schedule"]
    self._trip_id = self._data["next_departure"]["trip_id"]
    sql_shape = f"""
    SELECT t.trip_id, s.shape_pt_lat, s.shape_pt_lon
    FROM trips t, shapes s
    WHERE
    t.shape_id = s.shape_id
    and t.trip_id = '{self._trip_id}'
    order by s.shape_pt_sequence
    """
    shapes_list = []
    coordinates = []
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql_shape), {"q": "q"}).fetchall()
    for row_cursor in rows:
        row = row_cursor._asdict()
        shapes_list.append(list(row_cursor))
    for x in shapes_list:
        coordinate = []
        coordinate.append(x[2])
        coordinate.append(x[1])
        coordinates.append(coordinate)
    self.geojson = {"features": [{"geometry": {"coordinates": coordinates, "type": "LineString"}, "properties": {"id": self._trip_id, "title": self._trip_id}, "type": "Feature"}], "type": "FeatureCollection"}    
    _LOGGER.debug("Geojson output: %s", json.dumps(self.geojson))
    return None


def _fmt_gtfs_time(value):
    """Render a pygtfs departure_time (seconds since midnight, may exceed 24h) as HH:MM:SS."""
    try:
        s = int(value)
        return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    except (TypeError, ValueError):
        return str(value) if value is not None else None


def update_route_geojson(self):
    """Write the journey's ordered stops to www/gtfs2/<route>_<direction>_route.json.

    Companion file to the vehicle-positions geojson. Points only: the geojson
    integration reads nothing else, and since the import strips shapes.txt a
    LineString could only duplicate the stops; a map card rebuilds the path by
    joining the points in stop_sequence order. Each point carries an id and a
    title the way the geojson integration expects, plus the trip_id; what
    describes the whole journey sits on the FeatureCollection.
    Rewritten only when the drawn trip changes (see coordinator).
    """
    schedule = self._data["schedule"]
    departure = self._data.get("next_departure") or {}
    trip_id = departure.get("trip_id", None)
    route_id = departure.get("route_id", None)
    direction = str(departure.get("trip_direction_id", ""))
    if not trip_id or not route_id:
        return
    sql_stops = """
    SELECT st.stop_id, s.stop_name, s.stop_lat, s.stop_lon, st.stop_sequence, st.departure_time
    FROM stop_times st
    JOIN stops s ON s.stop_id = st.stop_id
    WHERE st.trip_id = :trip_id
    ORDER BY st.stop_sequence
    """
    with schedule.engine.connect() as conn:
        stop_rows = conn.execute(text(sql_stops), {"trip_id": trip_id}).fetchall()
    if not stop_rows:
        _LOGGER.debug("No stops found for trip: %s", trip_id)
        return
    features = []
    for row in stop_rows:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [row[3], row[2]]},
            "properties": {
                "id": str(route_id) + "_" + direction + "_" + str(row[4]),
                "title": row[1] + "_stop",
                "trip_id": trip_id,
                "stop_id": row[0],
                "stop_name": row[1],
                "stop_sequence": row[4],
                "departure_time": _fmt_gtfs_time(row[5]),
            },
        })
    geojson_dir = self.hass.config.path(DEFAULT_PATH_GEOJSON)
    os.makedirs(geojson_dir, exist_ok=True)
    # the ids come out of the datasource, so they are not file names until
    # they are made ones: see safe_file_part
    file = os.path.join(geojson_dir, f"{safe_file_part(route_id)}_{safe_file_part(direction)}_route.json")
    _LOGGER.debug("Creating route geojson file: %s", file)
    with open(file, "w") as outfile:
        json.dump({
            "type": "FeatureCollection",
            "properties": {
                "trip_id": trip_id,
                "route_id": str(route_id),
                "direction_id": direction,
            },
            "features": features,
        }, outfile)
    
def get_local_stop_list(hass, schedule, data):
    _LOGGER.debug("Getting local stops list with data: %s", data)
    device_tracker = hass.states.get(data['device_tracker_id'])
    latitude = device_tracker.attributes.get("latitude", None)
    longitude = device_tracker.attributes.get("longitude", None) 
    radius = data.get("radius", DEFAULT_LOCAL_STOP_RADIUS) / 111111
    sql_query = f"""
        SELECT stop.stop_id, stop.stop_name
        FROM stops stop
        where abs(stop.stop_lat - :latitude) < :radius and abs(stop.stop_lon - :longitude) < :radius
        """  
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql_query), {"latitude": latitude, "longitude": longitude, "radius": radius}).fetchall()
    rowcount = 0
    for row_cursor in rows:
        rowcount += 1
    _LOGGER.debug("Local stops list output: %s", rowcount)
    return rowcount
        

def _build_local_stop_element(self, row, base_date, date_label,
                              timezone_agency, timezone_stop, now_tz,
                              apply_now_filter, feed_entities=None):
    """Build one departure element incl. realtime, for a given service date.

    base_date / date_label: 'now_date' for today, 'tomorrow_date' for tomorrow.
    apply_now_filter: True for today (drop already-passed), False for tomorrow.
    feed_entities: already-fetched/parsed RT feed for this refresh cycle, if any
    (avoids re-fetching + re-parsing the same feed once per row/stop).
    Relies on self._icon being set by the caller for this row.
    Returns the element dict, or None if filtered out.
    """
    self._trip_id = row["trip_id"]
    self._direction = str(row["direction_id"])
    self._trip_short_name = row["trip_short_name"]
    self._route = row["route_id"]
    self._route_id = row["route_id"]
    self._stop_id = row["stop_id"]
    self._stop_sequence = row["stop_sequence"]
    #_LOGGER.debug("Row departure_time: %s", row["departure_time"])
    #_LOGGER.debug("Base_date / date_label: %s", base_date)

    # collect departure time from row, using agency timezone as basis, then transforming it to the stop-specific timezone (based on Amtrak)
    self._departure_datetime = datetime.datetime.strptime(
        base_date + " " + row["departure_time"], "%Y-%m-%d %H:%M:%S"
    ).replace(tzinfo=timezone_agency).astimezone(tz=timezone_stop)
    self._departure_datetime_utc = dt_util.as_utc(self._departure_datetime)
    #_LOGGER.debug("Self._departure datetime in agency_tz: %s", self._departure_datetime)
    self._departure_time = self._departure_datetime.replace(tzinfo=None).strftime(TIME_STR_FORMAT)
    #_LOGGER.debug("Self._departure time in stop tz: %s", self._departure_time)

    departure_rt = "-"
    departure_rt_datetime = "-"
    delay_rt = "-"
    delay_rt_derived = "-"
    departures = []

    # Find RT if configured
    if self._realtime:
        self._get_next_service = {}
        _LOGGER.debug("Find rt for local stop route: %s - direction: %s - stop: %s - stop_sequence: %s", self._route, self._direction, self._stop_id, self._stop_sequence)
        next_service = get_rt_route_trip_statuses(self, feed_entities)
        _LOGGER.debug("Next service: %s", next_service)
        if next_service:
            svc = next_service.get(self._route, {}).get(self._direction, {}).get(self._stop_id, [])
            delays = svc.get("delays", []) if svc else []
            departures = svc.get("departures", []) if svc else []
            delay_rt = delays[0] if delays else "-"
            departure_rt = departures[0] if departures else "-"
            departure_rt_datetime = departure_rt
        _LOGGER.debug("Departure rt: %s, Delay rt: %s", departure_rt, delay_rt)

    if departure_rt != "-":
        depart_time_corrected_time = departures[0].astimezone(tz=timezone_stop)
        departure_rt = depart_time_corrected_time.replace(tzinfo=None).strftime(TIME_STR_FORMAT)
        td = abs(depart_time_corrected_time - self._departure_datetime)
        if td.seconds != 0 and depart_time_corrected_time < self._departure_datetime:
            delay_rt_derived = "-" + str(td)
        elif td.seconds != 0:
            delay_rt_derived = str(td)
        _LOGGER.debug("Delay derived: %s, departure_rt: %s", delay_rt_derived, departure_rt)
    else:
        depart_time_corrected_time = (dt_util.parse_datetime(f"{base_date} {self._departure_time}")).replace(tzinfo=timezone_stop)
    #_LOGGER.debug("Departure time corrected based on realtime-time: %s", depart_time_corrected_time)

    if delay_rt != "-" and delay_rt != 0:
        depart_time_corrected_delay = (dt_util.parse_datetime(f"{base_date} {self._departure_time}") + datetime.timedelta(seconds=delay_rt)).replace(tzinfo=timezone_stop)
    else:
        delay_rt = "-"
        depart_time_corrected_delay = dt_util.parse_datetime(f"{base_date} {self._departure_time}").replace(tzinfo=timezone_stop)
    #_LOGGER.debug("Departure time corrected based on realtime-delay: %s", depart_time_corrected_delay)

    if depart_time_corrected_delay > depart_time_corrected_time:
        depart_time_corrected = depart_time_corrected_delay
    else:
        depart_time_corrected = depart_time_corrected_time
    #_LOGGER.debug("Departure time corrected: %s", depart_time_corrected)

    if apply_now_filter and not (depart_time_corrected > now_tz):
        _LOGGER.debug("Departure time corrected: %s, NOT after now in tz with offset: %s", depart_time_corrected, now_tz)
        return None

    return {
        "departure": self._departure_time,
        "departure_datetime": self._departure_datetime_utc,
        "departure_realtime": departure_rt,
        "departure_realtime_datetime": departure_rt_datetime,
        "delay_realtime_derived": delay_rt_derived,
        "delay_realtime": delay_rt,
        "date": date_label,
        "stop_name": row["stop_name"],
        "stop_id": row["stop_id"],
        "route": row["route_short_name"],
        "route_long": row["route_long_name"],
        "headsign": row["trip_headsign"],
        "trip_id": row["trip_id"],
        "direction_id": row["direction_id"],
        "icon": self._icon,
    }                

def _fetch_local_stop_rows(schedule, latitude, longitude, radius,
                            time_range, time_range_history, now):
    """Run the local-stop SQL query and return plain dicts. """
    ## QUERY candidate_stops and candidate_dates are used to construct a list of valid_dates, i.e a list where services run
    ## valid_dates is then used in the main query
    sql_query = f"""    
        WITH
          candidate_stops AS MATERIALIZED (
            SELECT stop.stop_id, stop.stop_name, stop.stop_lat AS latitude, stop.stop_lon AS longitude,
                   stop.stop_timezone AS stop_timezone, agency.agency_timezone AS agency_timezone,
                   trip.trip_id, trip.trip_headsign, trip.direction_id, trip.trip_short_name,
                   trip.service_id,
                   time(st.departure_time) AS departure_time, st.stop_sequence AS stop_sequence,
                   route.route_long_name, route.route_short_name, route.route_type, route.route_id
            FROM trips trip
            INNER JOIN stop_times st ON trip.trip_id = st.trip_id
            INNER JOIN stops stop ON stop.stop_id = st.stop_id
              AND abs(stop.stop_lat - :latitude) < :radius AND abs(stop.stop_lon - :longitude) < :radius
            INNER JOIN routes route ON route.route_id = trip.route_id
            INNER JOIN agency agency ON route.agency_id = agency.agency_id
          ),
          candidate_dates(date) AS (
            SELECT date(:now_offset)
            UNION ALL
            SELECT date(:now_offset, '+1 day')
          ),
          valid_dates AS MATERIALIZED (
            SELECT cal.service_id, cd.date
            FROM calendar cal
            CROSS JOIN candidate_dates cd
            WHERE cal.service_id IN (SELECT service_id FROM candidate_stops)
              AND cd.date BETWEEN cal.start_date AND cal.end_date
              AND (
                (CAST(strftime('%w', cd.date) AS INTEGER) = 0 AND cal.sunday    = 1) OR
                (CAST(strftime('%w', cd.date) AS INTEGER) = 1 AND cal.monday   = 1) OR
                (CAST(strftime('%w', cd.date) AS INTEGER) = 2 AND cal.tuesday  = 1) OR
                (CAST(strftime('%w', cd.date) AS INTEGER) = 3 AND cal.wednesday = 1) OR
                (CAST(strftime('%w', cd.date) AS INTEGER) = 4 AND cal.thursday = 1) OR
                (CAST(strftime('%w', cd.date) AS INTEGER) = 5 AND cal.friday   = 1) OR
                (CAST(strftime('%w', cd.date) AS INTEGER) = 6 AND cal.saturday = 1)
              )
              AND NOT EXISTS (
                SELECT 1 FROM calendar_dates ex
                WHERE ex.service_id = cal.service_id AND ex.date = cd.date AND ex.exception_type = 2
              )
            UNION
            SELECT cd2.service_id, cd2.date
            FROM calendar_dates cd2
            INNER JOIN candidate_dates cd ON cd.date = cd2.date
            WHERE cd2.service_id IN (SELECT service_id FROM candidate_stops)
              AND cd2.exception_type = 1
          )
        SELECT cs.stop_id, cs.stop_name, cs.latitude, cs.longitude, cs.stop_timezone, cs.agency_timezone,
               cs.trip_id, cs.trip_headsign, cs.direction_id, cs.trip_short_name,
               cs.departure_time, cs.stop_sequence, cs.route_long_name, cs.route_short_name, cs.route_type,
               vd.date AS calendar_date, cs.route_id
        FROM candidate_stops cs
        INNER JOIN valid_dates vd ON vd.service_id = cs.service_id
        WHERE datetime(vd.date || ' ' || cs.departure_time) BETWEEN
                datetime(:now_offset, :timerange_history) AND datetime(:now_offset, :timerange)
        ORDER BY cs.stop_id, vd.date, cs.departure_time;
    """  # noqa: S608        
    
    query_params = {
        "latitude": latitude,
        "longitude": longitude,
        "timerange": time_range,
        "timerange_history": time_range_history,
        "radius": radius,
        "now_offset": now,
    }

    #_LOGGER.debug("SQL statement:\n%s", sql_query)
    #_LOGGER.debug("SQL parameters:\n%s", query_params)        

    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql_query), {"latitude": latitude, "longitude": longitude, "timerange": time_range, "timerange_history": time_range_history, "radius": radius, "now_offset": now}).fetchall()

    data_returned = [row_cursor._asdict() for row_cursor in rows]
    _LOGGER.debug("Local stop rows returned: %s", data_returned)
    return data_returned


def _interpret_local_stop_rows(self, rows):
    """Turn raw SQL-shaped rows into the local-stops departures list.

    No database: `rows` only needs to be a list of plain dicts
    """
    offset = self._data["offset"]
    timetable = []
    local_stops_list = []
    prev_stop_id = ""
    prev_entry = entry = {}

    # Define timezone
    if self.hass.config.time_zone is None:
        _LOGGER.error("Timezone is not set in Home Assistant configuration, using UTC")
        timezone_local = dt_util.get_time_zone("UTC")
    else:
        timezone_local = dt_util.get_time_zone(self.hass.config.time_zone)
    _LOGGER.debug("Local timezone: %s",timezone_local)
    
    now_tz = dt_util.now().replace(tzinfo=timezone_local) + datetime.timedelta(minutes=offset)
    _LOGGER.debug("Default 'now' on local timezone, incl. offset (if configured): %s",now_tz)

	
    # Set elements for realtime retrieval via local file.
    if self._realtime:
        self._rt_group = "trip"
        self._rt_data = {
            "url": self._trip_update_url,
            CONF_API_KEY : self._headers.get(CONF_API_KEY,None),
            CONF_API_KEY_NAME : self._headers.get(CONF_API_KEY_NAME, None),
            CONF_API_KEY_LOCATION : self._headers.get(CONF_API_KEY_LOCATION,None),
            CONF_ACCEPT_HEADER_PB :self._headers.get(CONF_ACCEPT_HEADER_PB,None),
            "file": self._data["name"] + "_localstop",
            }
        _LOGGER.debug("self rt_data: %s, self headers: %s, self data: %s", self._rt_data, self._headers, self._data)

        check = get_gtfs_rt(self.hass,DEFAULT_PATH_RT,self._rt_data)

        # check if local file created
        if check != "ok":
            _LOGGER.error("Could not download RT data from: %s", self._trip_update_url)
            return {}
        else:
            # use local file created as new url
            self._trip_update_url = "file://" + DEFAULT_PATH_RT + "/" + self._data["name"] + "_localstop.rt"

    # Fetch + parse the RT feed once for this refresh cycle.
    feed_entities = None
    if self._realtime:

        feed_entities = get_gtfs_feed_entities(
            url=self._trip_update_url, headers=self._headers, label="trip_data"
        ) or []

    for row in rows:  
        #_LOGGER.debug("Row from query: %s", row)
        #defining TZ for row
        #_LOGGER.debug("Configured Agency timezone: %s", row['agency_timezone'])
        #_LOGGER.debug("Configured Stop timezone: %s", row['stop_timezone'])
        if row['agency_timezone'] is not None:
            timezone_agency = dt_util.get_time_zone(row['agency_timezone'])
        elif row['stop_timezone'] is not None:
            timezone_agency = dt_util.get_time_zone(row['stop_timezone'])
        else:
            timezone_agency = timezone_local
        if row['stop_timezone'] is not None:
            timezone_stop = dt_util.get_time_zone(row['stop_timezone'])
        else:
            timezone_stop = timezone_local
        _LOGGER.debug("Using Agency timezone: %s", timezone_agency)
        _LOGGER.debug("Using Stop timezone: %s", timezone_stop)

        if row["stop_id"] != prev_stop_id and prev_stop_id != "":
            local_stops_list.append(prev_entry)
            timetable = []

        entry = {"stop_id": row['stop_id'], "stop_name": row['stop_name'], "stop_sequence": row['stop_sequence'], "latitude": row['latitude'], "longitude": row['longitude'], "departure": timetable, "offset": offset}
        self._icon = ICONS.get(row['route_type'], ICON)
       
        element = _build_local_stop_element(
            self, row, row["calendar_date"], row["calendar_date"],
            timezone_agency, timezone_stop, now_tz,
            apply_now_filter=True, feed_entities=feed_entities)
            
        if element is not None:
            if element not in timetable:
                timetable.append(element)
            _LOGGER.debug("Timetable: %s", timetable)

        prev_entry = entry.copy()
        prev_stop_id = str(row["stop_id"])
        entry["departure"] = timetable


    if entry:
        local_stops_list.append(entry)

    for stop in local_stops_list:
        stop["departure"].sort(key=lambda d: d["departure_datetime"])

    data_returned = local_stops_list
    _LOGGER.debug("Interpreted local stop rows returned: %s", data_returned)
    return data_returned

def get_local_stops_next_departures(self):
    # 20260803 Note: this procedure is not using an option to in/exclude 'tomorrow'
    _LOGGER.debug("Get local stop departure with data: %s", self._data)
    if check_extracting(self.hass, self._data['gtfs_dir'],self._data['file']):
        _LOGGER.warning("Cannot get next depurtures on this datasource as still unpacking: %s", self._data["file"])
        return {}
    """Get next departures from data."""
    schedule = self._data["schedule"]
    offset = self._data["offset"]
    now = dt_util.now().replace(tzinfo=None) + datetime.timedelta(minutes=offset)
    now_date = now.strftime(dt_util.DATE_STR_FORMAT)
    device_tracker = self.hass.states.get(self._data['device_tracker_id'])
    latitude = device_tracker.attributes.get("latitude", None)
    longitude = device_tracker.attributes.get("longitude", None)
    time_range = str('+' + str(self._data.get("timerange", DEFAULT_LOCAL_STOP_TIMERANGE)) + ' minute')
    time_range_history = str('-' + str(self._data.get("timerange_history", DEFAULT_LOCAL_STOP_TIMERANGE_HISTORY)) + ' minute')
    radius = self._data.get("radius", DEFAULT_LOCAL_STOP_RADIUS) / 111111
    if not latitude or not longitude:
        _LOGGER.error("No latitude and/or longitude for : %s", self._data['device_tracker_id'])
        return []

    rows = _fetch_local_stop_rows(
        schedule, latitude, longitude, radius, time_range, time_range_history, now
    )
    return _interpret_local_stop_rows(self, rows)


async def update_gtfs_local_stops(hass, data): 
    _LOGGER.debug("Update service for local stops with data: %s", data)
    entries = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get("device_tracker_id") == data["entity_id"] :
            entries.append(entry.entry_id)
    for cf_entry in entries:
        _LOGGER.debug("Reloading local stops for config_entry_id: %s", cf_entry) 
        reload = await hass.config_entries.async_reload(cf_entry)    
    return
    
async def get_route_departures(hass, data):
    _LOGGER.debug("Getting route departures with data: %s", data)
    config_entry = hass.config_entries.async_get_entry(data.get("config_entry",""))
    cf_data = config_entry.data
    cf_options = config_entry.options
    _LOGGER.debug("config entry data: %s, options: %s", cf_data, cf_options)
    
    now = dt_util.now().replace(tzinfo=None)
    now_date = now.strftime(dt_util.DATE_STR_FORMAT)
    cutoff_today = datetime.datetime.strptime(now_date + ' ' + data.get('from_time','00:00:00'), "%Y-%m-%d %H:%M:%S")
    tomorrow = now + datetime.timedelta(days=1)
    tomorrow_date = tomorrow.strftime(dt_util.DATE_STR_FORMAT)
    cutoff_tomorrow = datetime.datetime.strptime(tomorrow_date + ' ' + data.get('from_time','00:00:00'), "%Y-%m-%d %H:%M:%S")
    _LOGGER.debug("Cutoff today: %s, cutoff tomorrow: %s", cutoff_today, cutoff_tomorrow)

    _pygtfs = get_gtfs(
            hass, DEFAULT_PATH, cf_data, False
        ) 
    
    _data = {
            "schedule": _pygtfs,
            "origin": cf_data["origin"],
            "destination": cf_data["destination"],
            "offset": cf_options["offset"] if "offset" in cf_options else 0,
            "include_tomorrow": True,
            "gtfs_dir": DEFAULT_PATH,
            "name": cf_data["name"],
            "file": cf_data["file"],
            "route_type": cf_data["route_type"],
            "route": cf_data["route"],
            "extracting": False,
            "next_departure": {},
            "next_departure_realtime_attr": {},
            "alert": {}
        }
        
    departures = await hass.async_add_executor_job(
                    get_next_departure, hass, _data
                ) 
                
    _LOGGER.debug("Departures received: %s", departures["next_departures"])

    today_departures = []
    tomorrow_departures = []
    for dt_string in departures["next_departures"]:
        dt = datetime.datetime.fromisoformat(dt_string).replace(tzinfo=None)
        dt_date = dt.strftime(dt_util.DATE_STR_FORMAT)
        if dt_date == now_date and cutoff_today < dt:
            today_departures.append(dt_string)
        if dt_date == tomorrow_date and cutoff_tomorrow < dt:
            tomorrow_departures.append(dt_string)
     
    _departures = {
        "today": today_departures if len(today_departures) > 0 else [],
        "tomorrow": tomorrow_departures if len(tomorrow_departures) > 0 else []
    } 
     
    _LOGGER.debug("Departures returned: %s", _departures)   
    _pygtfs.engine.dispose()
    return _departures
    
async def get_trip_stops(hass, data):
    _LOGGER.debug("Getting stoptimes for trip with: %s", data)
    state = hass.states.get(data.get("entity_id",""))
    entity_registry = er.async_get(hass)
    entry = entity_registry.async_get(data.get("entity_id",""))
    config_entry = hass.config_entries.async_get_entry(entry.config_entry_id)
    cf_data = config_entry.data
    origin_station_ids=[]
    origin_station_names=[]
    trips=[]
    if 'device_tracker_id' in state.attributes:
        for trip in state.attributes.get("next_departures_lines",{}):
            trips.append(trip.get("trip_id",""))
            if trip.get("stop_id","") not in origin_station_ids:
                        origin_station_ids.append(trip.get("stop_id",""))
            if trip.get("stop_name","") not in origin_station_names:
                        origin_station_names.append(trip.get("stop_name",""))                       
    else:
        trips = state.attributes.get("next_departures_trips", "[]")
        origin_station_ids.append(state.attributes.get("origin_station_stop_id", ""))
        origin_station_names.append(state.attributes.get("origin_station_stop_name", ""))
    
    trip_list = str(trips).replace("[","(").replace("]",")")

    schedule = get_gtfs(
            hass, DEFAULT_PATH, cf_data, False
        ) 
       
    sql_stops = f"""
    SELECT st.trip_id, s.stop_name, time(st.departure_time), s.stop_id
    from stop_times st 
    inner join stops s on s.stop_id = st.stop_id
    where  st.trip_id in {trip_list}
    order by st.trip_id, st.departure_time, st.stop_sequence
    """  # noqa: S608
    stops_list = []
    stops = []
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql_stops), {"q": "q"}).fetchall()
    for row_cursor in rows:
        row = row_cursor._asdict()
        stops_list.append(list(row_cursor))
    for x in stops_list:
        val = x[0] + ": " + x[1] + ' - ' + str(x[2]) + ' (' + str(x[3]) + ')'
        stops.append(val)

    stopslist = {}
    for trip in trips:
        s = []
        stop_hit = 0
        for tripstop in stops:
            for origin_station_id in origin_station_ids:
                if origin_station_id in tripstop and trip in tripstop:
                    stop_hit = 1
                if trip in tripstop and stop_hit == 1:
                    if tripstop.split(": ")[1] not in s:
                        s.append(tripstop.split(": ")[1].split(" (")[0])
                stopslist[trip] = s
    
    _tripstops = {
        "entity": data.get("entity_id","entity-not-found"),
        "origin_station_id": origin_station_ids[0],
        "origin_station_name": origin_station_names[0],
        "trip_stops": stopslist,
    }
    
    _LOGGER.debug("Tripstops returned: %s", _tripstops)
    schedule.engine.dispose()
    return _tripstops       
