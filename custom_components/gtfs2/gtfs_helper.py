"""Support for GTFS Integration."""
from __future__ import annotations

import datetime
import logging
import statistics
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


def _fetch_departure_rows(route_type, origin, destination, schedule, direction=None, route=None):
    """Run the static-GTFS SQL query and return matching rows as plain dicts.

    direction is only given by an entry at a loop's terminus
    (get_pair_direction, stored as loop_direction); the pair and the order of
    the stops decide it everywhere else, and the direction older entries
    store is not read."""
    if route_type == "2":
        route_type_where = f"route.route_type in (2,100,101,102,103,104,105,106,107,108,109,110,111,112,113,114,115,116,117)"
        start_station_id = str(origin)+'%'
        end_station_id = str(destination)+'%'
        start_station_where = f"AND origin_stop_time.stop_id in (select stop_id from stops where stop_name like :origin_station_id)"
        end_station_where = f"AND destination_stop_time.stop_id in (select stop_id from stops where stop_name like :end_station_id)"
        shortest_ride_where = ""
        direction_where = ""
        route_where = ""
        _LOGGER.debug("Setting up TRAIN Route for start/end : %s / %s ", start_station_id, end_station_id)
    else:
        route_type_where = "1=1"
        start_station_id = origin.split(': ')[0]
        end_station_id = destination.split(': ')[0]
        # both ends are matched on the whole place, every record of it: the
        # entry holds one record, the vehicle may call at another (the other
        # side of the road, the other quay of a terminus)
        origin_group = _place_group("origin_station_id")
        end_group = _place_group("end_station_id")
        start_station_where = "AND origin_stop_time.stop_id IN " + origin_group
        end_station_where = "AND destination_stop_time.stop_id IN " + end_group
        # a trip passing a place twice offers the pair twice (Palm Bus 21 calls
        # at Gare SNCF de Cannes on its way out and on its way back): the ride
        # is the shortest one, no other call at either end between the two
        shortest_ride_where = f"""AND NOT EXISTS (
                SELECT 1 FROM stop_times between_stop
                WHERE between_stop.trip_id = trip.trip_id
                  AND between_stop.stop_sequence > origin_stop_time.stop_sequence
                  AND between_stop.stop_sequence < destination_stop_time.stop_sequence
                  AND (between_stop.stop_id IN {origin_group}
                       OR between_stop.stop_id IN {end_group}))"""
        direction_where = ("AND (trip.direction_id = :direction OR trip.direction_id IS NULL)"
                           if str(direction) in ("0", "1") else "")
        # a place is shared by every line calling at it: the entry's line only
        route_where = "AND trip.route_id = :route" if route else ""
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
              {direction_where}
              {route_where}
              {shortest_ride_where}
              AND origin_stop_time.stop_sequence < destination_stop_time.stop_sequence
          ),
          cal_expand(service_id, d, end_date, monday, tuesday, wednesday, thursday, friday, saturday, sunday) AS (
            SELECT service_id, MAX(start_date, date('now', 'localtime', '-1 day')), end_date,
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
               datetime(vd.date || ' ' || time(origin_stop_time.arrival_time),'+' || CAST(julianday(date(origin_stop_time.arrival_time)) - julianday('1970-01-01') AS INTEGER) || ' days') AS origin_arrival_dt,
               time(origin_stop_time.departure_time) AS origin_depart_time,
			   datetime(vd.date || ' ' || time(origin_stop_time.departure_time),'+' || CAST(julianday(date(origin_stop_time.departure_time)) - julianday('1970-01-01') AS INTEGER) || ' days') AS origin_depart_dt,
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
               datetime(vd.date || ' ' || time(destination_stop_time.arrival_time),'+' || CAST(julianday(date(destination_stop_time.arrival_time)) - julianday('1970-01-01') AS INTEGER) || ' days') AS dest_arrival_dt,
               time(destination_stop_time.departure_time) AS dest_depart_time,
               datetime(vd.date || ' ' || time(destination_stop_time.departure_time),'+' || CAST(julianday(date(destination_stop_time.departure_time)) - julianday('1970-01-01') AS INTEGER) || ' days') AS dest_depart_dt,
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
                "direction": int(direction) if str(direction) in ("0", "1") else None,
                "route": route,
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
        service_date = row["origin_depart_date"]  # service day, for grouping only
        depart_dt_str = row["origin_depart_dt"]    # already a correct full instant
        try:
            depart_dt = datetime.datetime.strptime(depart_dt_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            _LOGGER.warning("Could not parse departure datetime: %s", depart_dt_str)
            continue

        if depart_dt <= now:
            continue  # already departed; SQL now filters by real instant, not just date

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
    timetable_upcoming_origin_stops = []
    max_remaining = 10
    count = 0
    for key, value in sorted(timetable.items()):
        upcoming = datetime.datetime.strptime(key[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone)
        # dest_arrival_dt is already the correct instant - no rollover guessing needed
        upcoming_arrival = datetime.datetime.strptime(
            value["dest_arrival_dt"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone_dest)
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
            # the record it leaves from: a place may be served from either
            timetable_upcoming_origin_stops.append(str(value.get("origin_stop_id")))
            count += 1
            if count >= max_remaining:
                break

    # origin/dest arrival & departure, make datetime and apply timezone
    origin_depart = datetime.datetime.strptime(item["origin_depart_dt"], "%Y-%m-%d %H:%M:%S")
    origin_arrival = datetime.datetime.strptime(item["origin_arrival_dt"], "%Y-%m-%d %H:%M:%S")
    dest_arrival = datetime.datetime.strptime(item["dest_arrival_dt"], "%Y-%m-%d %H:%M:%S")
    dest_depart = datetime.datetime.strptime(item["dest_depart_dt"], "%Y-%m-%d %H:%M:%S")

    _LOGGER.debug("Origin depart time: %s, Dest depart time: %s", origin_depart, dest_depart)

    depart_time = origin_depart.replace(tzinfo=timezone)
    arrival_time = dest_arrival.replace(tzinfo=timezone_dest)
    origin_arrival_time = dt_util.as_utc(origin_arrival.replace(tzinfo=timezone)).isoformat()
    origin_depart_time = dt_util.as_utc(origin_depart.replace(tzinfo=timezone)).isoformat()
    dest_arrival_time = dt_util.as_utc(dest_arrival.replace(tzinfo=timezone_dest)).isoformat()
    dest_depart_time = dt_util.as_utc(dest_depart.replace(tzinfo=timezone_dest)).isoformat()


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
        "next_departures_origin_stop_id": timetable_upcoming_origin_stops,
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
        direction=_data.get("loop_direction"),
        route=(_data.get("route") or "").split(": ")[0] or None,
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

# The trips of one direction ride a handful of distinct stop patterns, a
# few thousand times each over the feed's calendar (TAO tram A: 4214 trips,
# 27 stops). The walk only needs each pattern once, so one trip stands for
# every trip that rides the same stops in the same order: the lowest
# trip_id of the pattern, which is also the trip _ride_of would have walked
# first among them, so the result is the one reading every trip gives.
# The signature is concatenated in scan order on purpose: sorting it
# first costs more than reading every trip did (TAO A: 4.2 s against 2.8
# for the six lines, 1.2 s this way). Should the order ever vary between
# two trips of one pattern, that pattern is read twice, never lost.
_STOP_ROWS = """
    with ride as (
        select t.trip_id, group_concat(st.stop_sequence || ':' || st.stop_id) as stops
        from trips t
        inner join stop_times st on st.trip_id = t.trip_id
        where t.route_id = :route_id
        and (:direction is null or t.direction_id = :direction or t.direction_id is null)
        group by t.trip_id
    ), sample as (
        select min(trip_id) as trip_id from ride group by stops
    )
    SELECT st.trip_id, s.stop_id, s.stop_name, st.stop_sequence, s.parent_station, station.stop_name,
           s.stop_lat, s.stop_lon
    from sample
    inner join stop_times st on st.trip_id = sample.trip_id
    inner join stops s on s.stop_id = st.stop_id
    left join stops station on station.stop_id = s.parent_station
    order by st.trip_id, st.stop_sequence
"""


# A place is what the rider waits at, whatever the feed writes it as. Most
# feeds give each side of the road a record of its own, one per direction,
# and some give one per platform: Zou files the two poles of Pont de la
# Brague, 8 m apart, under one parent station, and half of the line's trips
# are entered on the pole across the road from the way they drive. Picking a
# record hid the trips entered on the other one. So a place is the parent
# station when the feed has one; when it has none (TAO: 9 parents for 1359
# poles), the records of the same name within PLACE_LAT / PLACE_LON of each
# other, about 150 m, which gathered the three poles of Zenith, 107 m apart
# at most, and keeps apart two villages' "Centre". The box is measured from
# the record the entry holds, so the list and the queries agree on it.
PLACE_LAT = 0.00135


PLACE_LON = 0.002


def _place_group(param):
    """SQL "(...)" of every stop_id of the place of the stop bound to :param."""
    return f"""(
    select sibling.stop_id
    from stops chosen, stops sibling
    where chosen.stop_id = :{param}
      and (sibling.stop_id = chosen.stop_id
           or (chosen.parent_station is not null
               and chosen.parent_station <> ''
               and sibling.parent_station = chosen.parent_station)
           or ((chosen.parent_station is null or chosen.parent_station = '')
               and (sibling.parent_station is null or sibling.parent_station = '')
               and sibling.stop_name = chosen.stop_name
               and abs(sibling.stop_lat - chosen.stop_lat) <= {PLACE_LAT}
               and abs(sibling.stop_lon - chosen.stop_lon) <= {PLACE_LON})))"""


_STOP_GROUP = _place_group("origin")


def _same_place(a, b):
    """The rule of _place_group, on (name, parent, lat, lon) tuples."""
    if a[1] or b[1]:
        return bool(a[1]) and a[1] == b[1]
    try:
        return (a[0] == b[0]
                and abs(float(a[2]) - float(b[2])) <= PLACE_LAT
                and abs(float(a[3]) - float(b[3])) <= PLACE_LON)
    except (TypeError, ValueError):
        return False


def _trips_of(rows):
    """{trip_id: [(stop_id, stop_sequence)]} and {stop_id: (name, parent,
    lat, lon, station_name)} out of _STOP_ROWS shaped rows."""
    trips = {}
    info = {}
    for trip_id, stop_id, stop_name, stop_sequence, parent_station, station_name, lat, lon in rows:
        trips.setdefault(trip_id, []).append((stop_id, stop_sequence))
        info[stop_id] = (stop_name, parent_station or "", lat, lon, station_name)
    return trips, info


def _box_distance(a, b):
    """How far apart two (name, parent, lat, lon) records are, in boxes:
    below 1 within PLACE_LAT / PLACE_LON."""
    try:
        return max(abs(float(a[2]) - float(b[2])) / PLACE_LAT,
                   abs(float(a[3]) - float(b[3])) / PLACE_LON)
    except (TypeError, ValueError):
        return 0


def _places_of(trips, info):
    """{stop_id: place}, a place being named by the first of its records the
    line calls at, fullest trip first: that record is what the entry keeps,
    and the one the queries widen to the whole place again.

    A box is measured from its seed only, as the SQL one is from the entry's
    record, so a chain of near records never drifts a place further. Two
    seeds' boxes can still overlap: TAO N has two Liberation-Interives 150 m
    apart, and a third pole within reach of both. Such a record joins the
    nearer seed, whichever the line met first, so the list does not depend
    on the order it reads the trips in.
    """
    seeds = []
    calls = {}
    for _trip_id, trip_stops in sorted(trips.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        for stop_id, _seq in trip_stops:
            if stop_id in calls:
                continue
            calls[stop_id] = True
            if not any(_same_place(info[s], info[stop_id]) for s in seeds):
                seeds.append(stop_id)
    place = {}
    for stop_id in calls:
        near = [s for s in seeds if _same_place(info[s], info[stop_id])]
        place[stop_id] = min(near, key=lambda s: (_box_distance(info[s], info[stop_id]), seeds.index(s)))
    return place


def _segments_of(places):
    """A trip read as places, cut where it comes back to a place it already
    passed: the next piece starts from the last place, so pieces stay tied.
    A racket (Palm Bus 21 out and back through Gare SNCF) or a loop (TAO 22,
    Zenith to Zenith) gives two pieces, each passing a place once."""
    pieces, current = [], []
    for p in places:
        if current and p == current[-1]:
            continue
        if p in current:
            pieces.append(current)
            current = [current[-1], p]
        else:
            current.append(p)
    if len(current) > 1 or not pieces:
        pieces.append(current)
    return pieces


def _chain_of(trips, place):
    """One order of places for the whole line, both ways round.

    direction_id cannot be trusted to split a line: on GVB tram 1 a third of
    the trips carry the other way's label, and the spec itself keeps it for
    publishing timetables, not for routing. The order of the stops can: the
    fullest piece is laid first, and every other piece is read forward or
    backward, whichever way the places it shares with the chain already
    agree with, then its places are slotted in after the place preceding
    them. A piece sharing nothing yet waits for the chain to grow.
    """
    pieces = {}
    for _trip_id, trip_stops in trips.items():
        for piece in _segments_of([place[s] for s, _seq in trip_stops]):
            pieces.setdefault(tuple(piece), 0)
            pieces[tuple(piece)] += 1
    pending = sorted(pieces, key=lambda p: (-len(p), -pieces[p], p))
    order = []
    while pending:
        waiting = []
        for piece in pending:
            position = {p: i for i, p in enumerate(order)}
            shared = [position[p] for p in piece if p in position]
            if not order:
                forward = True
            elif len(shared) < 2:
                waiting.append(piece)
                continue
            else:
                up = sum(1 for a, b in zip(shared, shared[1:]) if b > a)
                down = sum(1 for a, b in zip(shared, shared[1:]) if b < a)
                forward = up >= down
            prev = -1
            for p in (piece if forward else reversed(piece)):
                if p in position:
                    prev = order.index(p)
                    continue
                prev += 1
                order.insert(prev, p)
                position = {q: i for i, q in enumerate(order)}
        if len(waiting) == len(pending):
            # nothing left shares two places with the chain: keep them in
            # riding order at the end rather than lose them
            for piece in waiting:
                order.extend(p for p in piece if p not in order)
            break
        pending = waiting
    return order


# Which end the list starts from. One order serves both ways, so half the
# riders read it backwards whichever end comes first; it follows the way most
# trips labelled direction 0 ride it, the way a timetable of the line is
# usually printed first. Only the reading order comes from the label: nothing
# is built from it, and a wrong one (GVB 1 files 455 Matterhorn > Azartplein
# trips and 324 Azartplein > Surinameplein trips as 0) turns the list round
# and hides nothing.
_HEADING_ROWS = """
    with ride as (
        select t.trip_id, group_concat(st.stop_sequence || ':' || st.stop_id) as stops
        from trips t
        inner join stop_times st on st.trip_id = t.trip_id
        where t.route_id = :route_id and t.direction_id = 0
        group by t.trip_id
    )
    select stops, count(*) from ride group by stops
"""


def _heading_of(order, place, heading):
    """True when the trips of direction 0, weighed by how many run each
    pattern, ride order backwards more than forwards."""
    position = {p: i for i, p in enumerate(order)}
    up = down = 0
    for stops, count in heading:
        calls = sorted((int(seq), stop_id) for seq, stop_id in
                       (call.split(":", 1) for call in (stops or "").split(",") if ":" in call))
        known = [position[place[s]] for _seq, s in calls if s in place]
        up += count * sum(1 for a, b in zip(known, known[1:]) if b > a)
        down += count * sum(1 for a, b in zip(known, known[1:]) if b < a)
    return down > up


def _ride_of(rows, heading=()):
    """One entry per place, in riding order, out of _STOP_ROWS shaped rows.

    Returns the kept [stop_id, name, sequence], stop_id being the record that
    names the place, the station names by stop_id, which the labels read,
    and the {stop_id: place} the entries were drawn from. heading, the
    _HEADING_ROWS of the line, says which end comes first.
    """
    trips, info = _trips_of(rows)
    place = _places_of(trips, info)
    order = _chain_of(trips, place)
    if _heading_of(order, place, heading):
        order.reverse()
    first_seq = {}
    for _trip_id, trip_stops in sorted(trips.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        for stop_id, seq in trip_stops:
            first_seq.setdefault(stop_id, seq)
    kept = [[p, info[p][0], first_seq[p]] for p in order]
    station_names = {stop_id: values[4] for stop_id, values in info.items()}
    return kept, station_names, place


def _labels_of(kept, station_names):
    """{stop_id: readable name} for the stops whose name the line meets
    more than once; a stop met once keeps its plain name.

    Records of one place are already one entry, so a repeat left here is two
    places of the same name. The feed sometimes knows what tells them apart:
    on line 1 in Amsterdam one "Surinameplein" belongs to the Surinameplein
    station and the other to Hoofdweg, two hundred metres away. Often it
    does not: Zou 926 calls at five villages' "Centre". So a repeat carries
    its station when the station adds something, and falls back on its rank
    in the order the line calls at them when it does not. The value keeps
    the id untouched, only the readable part changes.
    """
    by_name = {}
    for x in kept:
        by_name.setdefault(x[1], []).append(x)
    label = {}
    for name, group in by_name.items():
        if len(group) == 1:
            continue
        for x in group:
            station_name = station_names.get(x[0])
            label[x[0]] = (f"{name} ({station_name})"
                           if station_name and station_name not in name
                           else name)
        # the station settles it only if it settles it for everyone: where
        # two of them still read the same, those keep their rank instead,
        # and a stop the station already told apart keeps its plain reading
        still_shared = [x for x in group
                        if [y for y in group if label[y[0]] == label[x[0]]][1:]]
        for n, x in enumerate(still_shared, 1):
            label[x[0]] = f"{label[x[0]]} #{n}"
    return label


def _entries_of(kept, label):
    """The picker's entries, "stop_id: Name (sequence)": get_next_departure
    cuts the id back out of the value, only the name is the user's to read."""
    return [f"{x[0]}: {label.get(x[0], x[1])} ({x[2]})" for x in kept]


def _direction_param(direction):
    """None for no direction (the whole line), else 0 or 1."""
    if direction is None or str(direction) not in ("0", "1"):
        return None
    return int(direction)


def _line_of(conn, route_id, direction=None):
    """_ride_of for a route, its sampled trips kept beside: (kept,
    station_names, place, trips)."""
    rows = conn.execute(text(_STOP_ROWS), {
        "route_id": route_id, "direction": _direction_param(direction)}).fetchall()
    heading = conn.execute(text(_HEADING_ROWS), {"route_id": route_id}).fetchall()
    kept, station_names, place = _ride_of(rows, heading)
    trips, _info = _trips_of(rows)
    return kept, station_names, place, trips


def _loop_termini(trips, place):
    """The places some trip of the line starts and ends at: a loop's terminus
    (TAO 22 runs Zénith to Zénith both ways round)."""
    return {place.get(stops[0][0]) for stops in trips.values()
            if stops and place.get(stops[0][0]) == place.get(stops[-1][0])}


def _calls_out(trips, place, origin_place):
    """(ride, trip_id) for each ride out of the origin place, the ride as
    places: from a call at it to the trip's next call at it, or its end. A
    trip passing the origin twice (Palm Bus 21 out and back through Gare
    Maritime) gives a ride from each."""
    rides = []
    for trip_id, trip_stops in trips.items():
        ride = None
        for stop_id, _seq in trip_stops:
            p = place.get(stop_id, stop_id)
            if p == origin_place:
                if ride:
                    rides.append((ride, trip_id))
                ride = []
            elif ride is not None:
                ride.append(p)
        if ride:
            rides.append((ride, trip_id))
    return rides


def _rides_from(trips, place, origin_place):
    """The rides of _calls_out, without their trips."""
    return [ride for ride, _trip_id in _calls_out(trips, place, origin_place)]


def _ways_of(trips, place, origin_place):
    """The ways out of an origin, {way: [(ride, trip_id)]}: where the bus
    goes, as the bus itself shows it.

    A way is the terminus of the trip, the place it ends at, read from the
    trips and never from direction_id. Only when the terminus tells nothing
    does the next stop come with it: a loop's terminus, which both rotations
    end at (TAO 22 reaches Zénith by Vieux Poirier or the long way round by
    Bois Girault), or the origin itself (from Zénith, by Plissay or by Jean
    Moulin). A trip ending short of a terminus (GVB 1 turns trams at
    Surinameplein) goes the way of the trips that leave for the same next
    stop and pass its end, or every stop of its ride but the end. The way is
    the stop_id of the terminus, the next stop's appended after "|" when it
    is part of it.
    """
    loop_termini = _loop_termini(trips, place)
    rides = {}
    for ride, trip_id in _calls_out(trips, place, origin_place):
        end = place.get(trips[trip_id][-1][0])
        told_by_next = end in loop_termini or end == origin_place
        rides.setdefault((end, ride[0] if told_by_next else None), []).append((ride, trip_id))
    folded = {}
    for key, calls in rides.items():
        if key[1] is not None:
            continue
        # on the way to another terminus: the trips there pass its end, or
        # every stop of its rides but the end when that end is a pole of its
        # own (GVB 1 turns at Surinameplein (Hoofdweg), the line goes on by
        # Surinameplein; TAO 40 ends at quai C, the line goes on by quai D)
        # a ride's body is the ride without its end, which a trip may enter
        # on two records in a row (TAO 3 closes on two Belneuf poles)
        for other, other_calls in rides.items():
            if other != key and other[1] is None and any(
                    ride[0] == mine[0]
                    and (key[0] in [p for p in ride if p != ride[-1]]
                         or {p for p in mine if p != mine[-1]} <= set(ride))
                    for ride, _trip_id in other_calls for mine, _mine_trip in calls):
                folded[key] = other
                break
    ways = {}
    for key, calls in rides.items():
        # two poles of one terminus fold into each other: one key for both
        chain = []
        while key in folded and key not in chain:
            chain.append(key)
            key = folded[key]
        if key in chain:
            key = min(chain[chain.index(key):])
        ways.setdefault("|".join(p for p in key if p), []).extend(calls)
    return ways


def get_towards(schedule, route_id, origin_stop_id):
    """The ways a rider can leave the origin, or nothing to ask.

    Asked only when it settles something: when buses from that place go
    different ways. At the end of a line every bus goes the same way, and
    nothing is asked. From a loop's terminus the two rotations are asked
    (TAO 22 sends buses round both ways at once from Zénith), and the answer
    is the rotation the entry keeps; mid-way round, the short or the long way
    to Zénith. Each answer keeps its own destinations. A line with three
    termini offers three ways: that is what its buses show.

    Returns [(way, label)], in the order of the list. A way reads as its
    terminus; with the next stop before it when the terminus alone tells
    nothing ("Vieux Poirier … Zénith"), and as the next stop alone when the
    terminus is the origin, which is nowhere to go ("Plissay").
    """
    with schedule.engine.connect() as conn:
        kept, station_names, place, trips = _line_of(conn, route_id)
    origin_place = place.get(origin_stop_id, origin_stop_id)
    ways = _ways_of(trips, place, origin_place)
    if len(ways) < 2:
        return []
    label = _labels_of(kept, station_names)
    names = {x[0]: label.get(x[0], x[1]) for x in kept}
    position = {x[0]: i for i, x in enumerate(kept)}
    shown = []
    for way in ways:
        end, _sep, following = way.partition("|")
        if not following or following == end:
            text = names.get(end, end)
        elif end == origin_place:
            text = names.get(following, following)
        else:
            text = f"{names.get(following, following)} … {names.get(end, end)}"
        shown.append((position.get(end, 0), position.get(following, 0), way, text))
    # from a loop's terminus, the trips round the loop and the trips ending
    # at the next stop both read as that stop (Zou 989 at Gare Routière):
    # the ones coming back say so
    texts = [text for _end, _following, _way, text in shown]
    shown = [(end, following, way,
              f"{text} … {names.get(origin_place, origin_place)}"
              if texts.count(text) > 1 and way.startswith(origin_place + "|") else text)
             for end, following, way, text in shown]
    shown.sort()
    _LOGGER.debug("Ways out of %s on %s: %s", origin_stop_id, route_id, shown)
    return [(way, text) for _end, _following, way, text in shown]


def get_stop_list(schedule, route_id, direction=None):
    """Every place a route rides, one entry each, in riding order.

    Without a direction, the whole line both ways round, which is what the
    flow offers: the rider picks where they are, not a label of the feed.
    A direction still narrows it to that direction's trips.
    """
    _LOGGER.debug("Getting stops list for route: %s direction: %s", route_id, direction)
    with schedule.engine.connect() as conn:
        kept, station_names, _place, _trips = _line_of(conn, route_id, direction)
    stops = _entries_of(kept, _labels_of(kept, station_names))
    _LOGGER.debug(f"Route stops: {stops}")
    return stops


def get_destination_stop_list(schedule, route_id, direction, origin_stop_id, towards=None):
    """The places a trip really reaches from the departure place.

    towards, a way get_towards offered, keeps the rides leaving that way
    only: the places on the rider's side, nearest first.

    Only the trips that call at the origin are read, and of each only the
    part after it, so every entry offered can be paired with the origin on
    at least one trip and nothing has to be rejected afterwards. Whether
    that trip runs today is the coordinator's business. The origin is
    matched as a whole place, every record of it, the way the departure
    query matches it; a loop that calls at it twice is read from the first
    call, which keeps the way back on offer. Without a direction both ways
    round are read, each in riding order from the origin. The entries are
    the line's, records and labels, so a stop reads the same on both
    screens; the origin's own place is not offered.
    """
    _LOGGER.debug("Getting destinations for route: %s direction: %s from: %s",
                  route_id, direction, origin_stop_id)
    # same sampling as _STOP_ROWS, on the part of each trip after the origin
    sql = f"""
    with through as (
        select trip_id, min(stop_sequence) as origin_sequence
        from stop_times where stop_id in {_STOP_GROUP} group by trip_id
    ), ride as (
        select t.trip_id, group_concat(st.stop_sequence || ':' || st.stop_id) as stops
        from trips t
        inner join through o on o.trip_id = t.trip_id
        inner join stop_times st on st.trip_id = t.trip_id
            and st.stop_sequence > o.origin_sequence
        where t.route_id = :route_id
        and (:direction is null or t.direction_id = :direction or t.direction_id is null)
        group by t.trip_id
    ), sample as (
        select min(trip_id) as trip_id from ride group by stops
    )
    SELECT st.trip_id, s.stop_id, s.stop_name, st.stop_sequence, s.parent_station, station.stop_name,
           s.stop_lat, s.stop_lon
    from sample
    inner join through o on o.trip_id = sample.trip_id
    inner join stop_times st on st.trip_id = sample.trip_id
        and st.stop_sequence > o.origin_sequence
    inner join stops s on s.stop_id = st.stop_id
    left join stops station on station.stop_id = s.parent_station
    order by st.trip_id, st.stop_sequence
    """  # noqa: S608
    scope = {"route_id": route_id, "direction": _direction_param(direction)}
    with schedule.engine.connect() as conn:
        line, station_names, place, _line_trips = _line_of(conn, route_id, direction)
        rows = conn.execute(text(sql), {**scope, "origin": origin_stop_id}).fetchall()
    position = {x[0]: i for i, x in enumerate(line)}
    by_place = {x[0]: x for x in line}
    trips, _info = _trips_of(rows)
    origin_place = place.get(origin_stop_id, origin_stop_id)
    home = position.get(origin_place, -1)
    # the rows start right after each trip's first call at the origin
    rides = _rides_from({t: [(origin_stop_id, None)] + s for t, s in trips.items()},
                        place, origin_place)
    if towards is not None:
        # the rides of the way get_towards offered, read from the same trips
        way = _ways_of(_line_trips, place, origin_place).get(towards, [])
        chosen = {tuple(ride) for ride, _trip_id in way}
        rides = [ride for ride in rides if tuple(ride) in chosen]
    # Riding order first: a place comes after every place some trip calls at
    # just before it on its way from the origin, so two branches that meet
    # again (GVB 1 reaches Leidseplein by Overtoom or by Jan Pieter
    # Heijestraat) keep each ride's order. A later call at the origin starts
    # the ride again (Palm Bus 21 passes Gare SNCF out and back), and a place
    # met again on the same ride starts a new stretch rather than closing a
    # circle. Among places no ride orders, nearest first: the fewest stops
    # any trip takes to reach them, the line's far side of the origin before
    # its near side. From a loop's terminus every stop is reached both ways
    # round and the rides order nothing for good; the nearest is taken
    # there, the shorter way round.
    reach, before = {}, {}
    for ride in rides:
        count, previous, stretch = 0, None, set()
        for p in ride:
            count += 1
            reach[p] = min(reach.get(p, count), count)
            before.setdefault(p, set())
            if p in stretch:
                stretch = {p}
            elif previous is not None and previous != p:
                before[p].add(previous)
                stretch.add(p)
            else:
                stretch.add(p)
            previous = p

    def nearest(p):
        return (position.get(p, 0) < home, reach[p], position.get(p, 0))

    order, placed = [], set()
    while len(order) < len(reach):
        ready = [p for p in reach if p not in placed and not (before[p] - placed)]
        # nothing free: a loop's rotations order each other round, take the nearest
        p = min(ready or [p for p in reach if p not in placed], key=nearest)
        order.append(p)
        placed.add(p)
    kept = [by_place[p] for p in order if p in by_place]
    stops = _entries_of(kept, _labels_of(line, station_names))
    _LOGGER.debug(f"Destinations from {origin_stop_id}: {stops}")
    return stops


def get_pair_direction(schedule, route_id, origin_stop_id, destination_stop_id, towards=None):
    """The direction an entry must keep for this pair, or None.

    towards, the way the rider answered get_towards with, picks the rotation
    when the trips riding the pair that way agree on one label; otherwise,
    and when nothing was asked, the rules below.

    The pair and the order of the stops on one trip say which way the
    rider goes, whatever the labels. Only a loop leaves it open: TAO 22 runs
    Zenith to Zenith both ways round, and a trip leaving Zenith reaches any
    stop of the loop, the short way on one rotation and the long way on the
    other; on that line the 29 pairs with Zenith at one end are the only
    ones where this happens, out of 870: a trip calls at the terminus at
    both ends, so it rides a pair with the terminus at one end whichever
    way round it goes. Then the rotation with the fewest stops is kept,
    when its trips agree on one direction.

    Stops rather than minutes: on TAO 22 both pick the same rotation for 54
    of the 58 pairs, the 2 that differ are 42 seconds apart, and Zou 989
    gives every stop of a trip the same time, so minutes decide nothing
    there. They settle a tie in stops (a stop halfway round), by the median
    ride time of each rotation; a tie on both keeps no direction.
    """
    with schedule.engine.connect() as conn:
        _kept, _station_names, place, trips = _line_of(conn, route_id)
        labels = dict(conn.execute(text(
            "select trip_id, direction_id from trips where route_id = :route_id"),
            {"route_id": route_id}).fetchall())
    origin = place.get(origin_stop_id, origin_stop_id)
    destination = place.get(destination_stop_id, destination_stop_id)
    termini = _loop_termini(trips, place)
    if origin not in termini and destination not in termini:
        return None
    if towards is not None:
        way = _ways_of(trips, place, origin).get(towards, [])
        told = {str(labels[trip_id]) for ride, trip_id in way
                if destination in ride and labels.get(trip_id) is not None}
        if len(told) == 1:
            direction = told.pop()
            _LOGGER.debug("Pair %s -> %s on %s ridden %s, keeping direction %s",
                          origin_stop_id, destination_stop_id, route_id, towards, direction)
            return direction
    rides = []
    for trip_id, trip_stops in trips.items():
        seq = [place[s] for s, _ in trip_stops]
        best = None
        last_origin = None
        for i, p in enumerate(seq):
            if p == origin:
                last_origin = i
            elif p == destination and last_origin is not None:
                if best is None or i - last_origin < best[1] - best[0]:
                    best = (last_origin, i)
                last_origin = None
        if best:
            rides.append((best[1] - best[0], labels.get(trip_id)))
    if len({label for _length, label in rides}) < 2:
        return None
    fewest = min(length for length, _label in rides)
    agreed = {str(label) for length, label in rides if length == fewest and label is not None}
    if len(agreed) > 1:
        agreed = _quickest_rotations(schedule, route_id, origin_stop_id,
                                     destination_stop_id, agreed)
    direction = agreed.pop() if len(agreed) == 1 else None
    _LOGGER.debug("Pair %s -> %s on %s is served both ways round, keeping direction %s",
                  origin_stop_id, destination_stop_id, route_id, direction)
    return direction


def _clock_seconds(value):
    """Seconds into the service day of a stop_times time, as the database
    holds it: "07:10:00", or a pygtfs datetime on 1970-01-01, the next day
    for a time past midnight."""
    text_value = str(value or "")
    days = 0
    if " " in text_value:
        day, text_value = text_value.split(" ", 1)
        days = max(0, int(day[-2:]) - 1)
    hours, minutes, seconds = text_value.split(":")[:3]
    return days * 86400 + int(hours) * 3600 + int(minutes) * 60 + int(float(seconds))


def _quickest_rotations(schedule, route_id, origin_stop_id, destination_stop_id, candidates):
    """Of the direction labels in candidates, the one whose shortest rides of
    the pair take the least time, by the median over its trips; all of them
    when that does not tell them apart."""
    origin_group = _place_group("origin")
    destination_group = _place_group("destination")
    sql = f"""
    select t.direction_id, o.departure_time, d.arrival_time
    from trips t
    inner join stop_times o on o.trip_id = t.trip_id
    inner join stop_times d on d.trip_id = t.trip_id
    where t.route_id = :route_id
      and o.stop_id in {origin_group}
      and d.stop_id in {destination_group}
      and o.stop_sequence < d.stop_sequence
      and not exists (
          select 1 from stop_times between_stop
          where between_stop.trip_id = t.trip_id
            and between_stop.stop_sequence > o.stop_sequence
            and between_stop.stop_sequence < d.stop_sequence
            and (between_stop.stop_id in {origin_group}
                 or between_stop.stop_id in {destination_group}))
    """  # noqa: S608
    minutes = {}
    try:
        with schedule.engine.connect() as conn:
            for label, departs, arrives in conn.execute(text(sql), {
                    "route_id": route_id, "origin": origin_stop_id,
                    "destination": destination_stop_id}):
                if str(label) in candidates:
                    minutes.setdefault(str(label), []).append(
                        (_clock_seconds(arrives) - _clock_seconds(departs)) / 60)
    except (TypeError, ValueError) as ex:
        _LOGGER.debug("Could not time the rotations of %s -> %s: %s",
                      origin_stop_id, destination_stop_id, ex)
        return set(candidates)
    medians = {label: statistics.median(values) for label, values in minutes.items() if values}
    if len(medians) < 2 or len(set(medians.values())) < len(medians):
        return set(candidates)
    return {min(medians, key=medians.get)}


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
        

def _build_local_stop_element(self, row, base_datetime,
                              timezone_agency, timezone_stop, now_tz,
                              apply_now_filter, feed_entities=None):
    """Build one departure element incl. realtime, for a given service date.

    base_datetime / datetime_label: both are departure_dt from the query.
    """
    self._trip_id = row["trip_id"]
    self._direction = str(row["direction_id"])
    self._trip_short_name = row["trip_short_name"]
    self._route = row["route_id"]
    self._route_id = row["route_id"]
    self._stop_id = row["stop_id"]
    self._stop_sequence = row["stop_sequence"]
    #_LOGGER.debug("Row departure_time: %s", row["departure_time"])
    #_LOGGER.debug("base_datetime / datetime_label: %s", base_datetime)

    # collect departure time from row, using agency timezone as basis, then transforming it to the stop-specific timezone (based on Amtrak)
    self._departure_datetime = datetime.datetime.strptime(
        base_datetime, "%Y-%m-%d %H:%M:%S"
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
        #depart_time_corrected_time = (dt_util.parse_datetime(f"{base_date} {self._departure_time}")).replace(tzinfo=timezone_stop)
        depart_time_corrected_time = dt_util.parse_datetime(base_datetime).replace(tzinfo=timezone_stop)
    #_LOGGER.debug("Departure time corrected based on realtime-time: %s", depart_time_corrected_time)

    if delay_rt != "-" and delay_rt != 0:
        #depart_time_corrected_delay = (dt_util.parse_datetime(f"{base_date} {self._departure_time}") + datetime.timedelta(seconds=delay_rt)).replace(tzinfo=timezone_stop)
        depart_time_corrected_delay = (dt_util.parse_datetime(base_datetime) + datetime.timedelta(seconds=delay_rt)).replace(tzinfo=timezone_stop)
    else:
        delay_rt = "-"
        #depart_time_corrected_delay = dt_util.parse_datetime(f"{base_date} {self._departure_time}").replace(tzinfo=timezone_stop)
        depart_time_corrected_delay = dt_util.parse_datetime(base_datetime).replace(tzinfo=timezone_stop)
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
        "date": datetime.datetime.strptime(base_datetime, "%Y-%m-%d %H:%M:%S").date().isoformat(),
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
                   st.departure_time AS departure_time_raw,
                   st.stop_sequence AS stop_sequence,
                   route.route_long_name, route.route_short_name, route.route_type, route.route_id
            FROM trips trip
            INNER JOIN stop_times st ON trip.trip_id = st.trip_id
            INNER JOIN stops stop ON stop.stop_id = st.stop_id
              AND abs(stop.stop_lat - :latitude) < :radius AND abs(stop.stop_lon - :longitude) < :radius
            INNER JOIN routes route ON route.route_id = trip.route_id
            INNER JOIN agency agency ON route.agency_id = agency.agency_id
          ),
          candidate_dates(date) AS (
            SELECT date(:now_offset, '-1 day')
            UNION ALL
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
               datetime(
                 vd.date || ' ' || time(cs.departure_time_raw),
                 CASE WHEN date(cs.departure_time_raw) = '1970-01-02' THEN '+1 day' ELSE '+0 day' END
               ) AS departure_dt,
               cs.stop_sequence, cs.route_long_name, cs.route_short_name, cs.route_type,
               cs.route_id
        FROM candidate_stops cs
        INNER JOIN valid_dates vd ON vd.service_id = cs.service_id
        WHERE datetime(
                vd.date || ' ' || time(cs.departure_time_raw),
                CASE WHEN date(cs.departure_time_raw) = '1970-01-02' THEN '+1 day' ELSE '+0 day' END
              ) BETWEEN datetime(:now_offset, :timerange_history) AND datetime(:now_offset, :timerange)
        ORDER BY cs.stop_id, vd.date, cs.departure_time_raw;
    """  # noqa: S608        
    
    query_params = {
        "latitude": latitude,
        "longitude": longitude,
        "timerange": time_range,
        "timerange_history": time_range_history,
        "radius": radius,
        "now_offset": now,
    }

    _LOGGER.debug("SQL statement:\n%s", sql_query)
    _LOGGER.debug("SQL parameters:\n%s", query_params)        

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
            self, row, row["departure_dt"], 
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
