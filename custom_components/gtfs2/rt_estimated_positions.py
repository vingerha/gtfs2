"""Estimated vehicle positions from GTFS-RT TripUpdates.

For feeds that publish TripUpdates but no VehiclePositions, a vehicle's
position is estimated from its real-time stop times: on a stop while the
feed says it is there, otherwise on the straight line between the last
passed stop and the next one, in proportion to the time elapsed between
them. No shapes are used, so this works on any feed with stops.txt.

This module is standalone: it only reads what get_rt_route_trip_statuses has
already fetched and parsed (self._feed_entities) and writes its own geojson
file, www/gtfs2/<route>_<direction>_estimated.json, next to the GPS one.
It is meant to run only when no vehicle_position_url is configured.

The pure functions (no hass, no database) are kept apart from the I/O so they
can be tested on their own:
    route_matches, extract_events, estimate_position, interpolate
"""
from __future__ import annotations

import binascii
import json
import logging
import os

import homeassistant.util.dt as dt_util
from sqlalchemy.sql import text

from .const import DEFAULT_PATH_GEOJSON
from .gtfs_rt_helper import safe_file_part

_LOGGER = logging.getLogger(__name__)

# A trip whose first listed time is further away than this is treated as not
# yet running and left off the map, so upcoming trips do not pile up on
# their first stop. Side effect: on a feed listing only upcoming stops, a
# running vehicle whose next stop is more than this far away is hidden too.
PRESTART_WINDOW_S = 600

# SQLite caps the number of bound parameters per statement.
_SQL_CHUNK = 500

POSITION_AT_STOP = "at_stop"
POSITION_BETWEEN_STOPS = "between_stops"
POSITION_BEFORE_FIRST_LISTED = "before_first_listed_stop"


# --------------------------------------------------------------------------
# Pure functions
# --------------------------------------------------------------------------

def _as_int(value) -> int:
    """Protobuf-to-json gives ints, feeds served as json may give strings."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def route_matches(rt_route_id, wanted_route_id, delimiter=None) -> bool:
    """Same delimiter rule as get_rt_route_trip_statuses, but exact match only.

    Substring matching ("1" in "10") would pull other lines onto the map.
    """
    if not rt_route_id:
        return False
    rt_route_id = str(rt_route_id)
    if delimiter:
        rt_route_id = rt_route_id.split(delimiter)[0]
    return rt_route_id == str(wanted_route_id)


def extract_events(stop_time_updates) -> list[dict]:
    """Turn stop_time_updates into ordered events with absolute times.

    Returns dicts with stop_id, stop_sequence, arr, dep (epoch seconds).
    A missing arrival takes the departure and vice versa; an update with
    neither time is skipped (delay-only feeds are not supported here).
    """
    events = []
    for stu in stop_time_updates or []:
        arr = _as_int((stu.get("arrival") or {}).get("time"))
        dep = _as_int((stu.get("departure") or {}).get("time"))
        if not arr and not dep:
            continue
        arr = arr or dep
        dep = dep or arr
        events.append({
            "stop_id": str(stu.get("stop_id") or ""),
            "stop_sequence": _as_int(stu.get("stop_sequence")),
            "arr": arr,
            "dep": max(arr, dep),
        })
    if events and all(e["stop_sequence"] > 0 for e in events):
        events.sort(key=lambda e: e["stop_sequence"])
    return events


def interpolate(a: dict, b: dict, fraction: float) -> tuple[float, float]:
    """Point at `fraction` of the way from stop a to stop b.

    Straight line for now. This is the one function to replace when shapes
    are added: same inputs, a point on the shape instead.
    """
    lat = a["lat"] + (b["lat"] - a["lat"]) * fraction
    lon = a["lon"] + (b["lon"] - a["lon"]) * fraction
    return lat, lon


def estimate_position(events: list[dict], now: float,
                      prestart_window: int = PRESTART_WINDOW_S):
    """Estimated (lat, lon, state) for one trip, or None if not on the road.

    `events` must carry lat/lon. None when the trip has finished, or has not
    started and is more than prestart_window away from its first listed stop.
    """
    if not events:
        return None
    first, last = events[0], events[-1]

    if now >= last["dep"]:
        return None
    if now < first["arr"]:
        if first["arr"] - now > prestart_window:
            return None
        return first["lat"], first["lon"], POSITION_BEFORE_FIRST_LISTED

    for i, event in enumerate(events):
        if event["arr"] <= now < event["dep"] or now == event["arr"]:
            return event["lat"], event["lon"], POSITION_AT_STOP
        nxt = events[i + 1] if i + 1 < len(events) else None
        if nxt is not None and event["dep"] <= now < nxt["arr"]:
            span = nxt["arr"] - event["dep"]
            if span <= 0:
                return nxt["lat"], nxt["lon"], POSITION_AT_STOP
            lat, lon = interpolate(event, nxt, (now - event["dep"]) / span)
            return lat, lon, POSITION_BETWEEN_STOPS
    # times not in order: last resort, the last stop already reached
    reached = [e for e in events if e["arr"] <= now]
    if reached:
        return reached[-1]["lat"], reached[-1]["lon"], POSITION_AT_STOP
    return None


# --------------------------------------------------------------------------
# Database lookups
# --------------------------------------------------------------------------

def _chunks(values: list, size: int = _SQL_CHUNK):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def _query_in(conn, sql_template: str, column_values: list) -> list:
    """Run `sql_template` with {in} replaced by a bound IN list, chunked."""
    rows = []
    for chunk in _chunks(column_values):
        names = [f"p{i}" for i in range(len(chunk))]
        sql = sql_template.format(**{"in": ", ".join(":" + n for n in names)})
        rows.extend(conn.execute(text(sql), dict(zip(names, chunk))).fetchall())
    return rows


def _load_trips(conn, trip_ids: list) -> dict:
    """trip_id -> (route_id, direction_id) from the static trips table."""
    rows = _query_in(
        conn,
        "SELECT trip_id, route_id, direction_id FROM trips WHERE trip_id IN ({in})",
        trip_ids,
    )
    return {str(r[0]): (str(r[1]), None if r[2] is None else str(r[2])) for r in rows}


def _load_stop_ids_by_sequence(conn, trip_ids: list) -> dict:
    """(trip_id, stop_sequence) -> stop_id, for updates that carry no stop_id."""
    rows = _query_in(
        conn,
        "SELECT trip_id, stop_sequence, stop_id FROM stop_times WHERE trip_id IN ({in})",
        trip_ids,
    )
    return {(str(r[0]), int(r[1])): str(r[2]) for r in rows}


def _load_stops(conn, stop_ids: list) -> dict:
    """stop_id -> (lat, lon)."""
    rows = _query_in(
        conn,
        "SELECT stop_id, stop_lat, stop_lon FROM stops WHERE stop_id IN ({in})",
        stop_ids,
    )
    return {str(r[0]): (float(r[1]), float(r[2])) for r in rows}


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def _feature_title(self, direction: str, crc: str) -> str:
    """Same label scheme as get_rt_vehicle_positions."""
    icon = str(getattr(self, "_icon", "mdi:bus") or "mdi:bus")
    icon = icon.split(":")[1] if ":" in icon else icon
    try:
        line = str(self._data.get("next_departure", {}).get("route_short_name") or "").strip()
        dest = self.config_entry.data.get("destination", "").split(": ")[-1].split(" (")[0].split(" - ")[0].strip()
    except Exception:  # pylint: disable=broad-except
        line, dest = "", ""
    if line and dest:
        return f"{line} → {dest} {crc}_{icon}"
    return f"{self._route_id}({direction}){crc}_{icon}"


def build_estimated_features(self, feed_entities, schedule, now: float) -> list[dict]:
    """Select this route/direction's trips, estimate, return geojson features."""
    wanted_route = str(self._route_id)
    wanted_dir = None if self._direction in (None, "None", "") else str(self._direction)
    delimiter = getattr(self, "_route_delimiter", None)

    # 1. candidate trips; route and direction may be missing from the feed
    candidates = []
    for entity in feed_entities or []:
        trip_update = entity.get("trip_update") if isinstance(entity, dict) else None
        if not trip_update:
            continue
        trip = trip_update.get("trip") or {}
        trip_id = str(trip.get("trip_id") or "")
        if not trip_id:
            continue
        rt_route = trip.get("route_id") or ""
        if rt_route and not route_matches(rt_route, wanted_route, delimiter):
            continue
        rt_dir = trip.get("direction_id")
        candidates.append({
            "trip_id": trip_id,
            "rt_route": rt_route,
            "rt_dir": None if rt_dir in (None, "") else str(rt_dir),
            "stus": trip_update.get("stop_time_update") or [],
        })
    if not candidates:
        return []

    with schedule.engine.connect() as conn:
        # 2. fill in route/direction from the static trips table where missing
        needs_static = [c["trip_id"] for c in candidates if not c["rt_route"] or c["rt_dir"] is None]
        static_trips = _load_trips(conn, needs_static) if needs_static else {}

        selected = []
        for c in candidates:
            static_route, static_dir = static_trips.get(c["trip_id"], (None, None))
            route = c["rt_route"] or static_route
            if not route or not route_matches(route, wanted_route, delimiter):
                continue
            direction = c["rt_dir"] if c["rt_dir"] is not None else static_dir
            # unknown direction is kept, like the static queries do
            if wanted_dir is not None and direction is not None and direction != wanted_dir:
                continue
            c["direction"] = direction if direction is not None else (wanted_dir or "")
            c["events"] = extract_events(c["stus"])
            if c["events"]:
                selected.append(c)
        if not selected:
            return []

        # 3. stop ids for updates carrying only a stop_sequence
        missing = sorted({c["trip_id"] for c in selected
                          if any(not e["stop_id"] for e in c["events"])})
        by_sequence = _load_stop_ids_by_sequence(conn, missing) if missing else {}
        for c in selected:
            for e in c["events"]:
                if not e["stop_id"]:
                    e["stop_id"] = by_sequence.get((c["trip_id"], e["stop_sequence"]), "")

        # 4. coordinates
        stop_ids = sorted({e["stop_id"] for c in selected for e in c["events"] if e["stop_id"]})
        coords = _load_stops(conn, stop_ids)

    # 5. estimate and build features
    features = []
    for c in selected:
        events = []
        for e in c["events"]:
            if e["stop_id"] in coords:
                e["lat"], e["lon"] = coords[e["stop_id"]]
                events.append(e)
        result = estimate_position(events, now)
        if result is None:
            continue
        lat, lon, state = result
        crc = str(binascii.crc32(c["trip_id"].encode("utf8")))[-3:]
        direction = str(c["direction"])
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "id": f"{wanted_route}_{direction}_{crc}",
                "title": _feature_title(self, direction, crc),
                "trip_id": c["trip_id"],
                "route_id": wanted_route,
                "direction_id": direction,
                "vehicle_id": "",
                "vehicle_label": "",
                "position_type": "estimated",
                "position_state": state,
            },
        })
    return features


def create_estimated_vehicle_geojson(self) -> list[dict]:
    """Write www/gtfs2/<route>_<direction>_estimated.json. Runs in the executor.

    Expects get_rt_route_trip_statuses to have run in this refresh, which
    leaves the parsed TripUpdate feed in self._feed_entities.
    """
    feed_entities = getattr(self, "_feed_entities", None)
    now = dt_util.utcnow().timestamp()
    features = []
    if feed_entities:
        features = build_estimated_features(self, feed_entities, self._data["schedule"], now)
    else:
        _LOGGER.debug("Estimated positions: no TripUpdate entities this refresh")

    geojson = {"type": "FeatureCollection", "features": features}
    # an empty collection is still written, so vehicles that finished disappear
    geojson_dir = self.hass.config.path(DEFAULT_PATH_GEOJSON)
    os.makedirs(geojson_dir, exist_ok=True)
    name = f"{safe_file_part(self._route_id)}_{safe_file_part(self._direction)}_estimated.json"
    ## name = f"{safe_file_part(self._route_id)}_{safe_file_part(self._direction)}.json"
    path = os.path.join(geojson_dir, name)
    tmp = path + ".tmp"
    with open(tmp, "w") as outfile:
        json.dump(geojson, outfile)
    os.replace(tmp, path)  # the map card never reads a half-written file
    _LOGGER.debug("Estimated positions: %s vehicle(s) written to %s", len(features), path)
    return features
