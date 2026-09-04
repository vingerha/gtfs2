"""Capture a small, frozen GTFS + GTFS-RT fixture from a live provider.

A realtime feed is rewritten every few seconds, so two runs of the same code
against it never answer the same thing. A test needs bytes that do not move,
which is what this writes.

The fixture is real data, only smaller: the feeds are parsed, a handful of
entities are kept because of what they exercise (a delayed trip, added and
canceled ones, a skipped stop, updates naming a station rather than a
platform, an alert on a station a kept trip calls at, an alert naming a trip,
a few alerts for their shape alone, and one trip nothing says anything about),
and the result is serialised again. The static zip is cut down to the trips
those entities name, plus the stops around them. Nothing is invented:
manifest.json records where every byte came from and what was dropped, and
scenarios.json lists the journeys the kept entities make testable, by trip,
origin and destination.

    python tests/fixtures/capture_rt.py --out tests/fixtures/sncf
    python tests/fixtures/capture_rt.py --out tests/fixtures/other \\
        --static URL --trip-updates URL --alerts URL --provider "Name"
    python tests/fixtures/capture_rt.py --out tests/fixtures/sncf --source-dir raw

SNCF is the default, the feed tests/fixtures/sncf came from. --source-dir
reuses static.zip, trip_updates.pb and service_alerts.pb already downloaded
there (the names a fixture uses itself, so a fixture directory can be cut
down again), which is also the way in for a feed that wants an api key.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import csv
import io
import json
import os
import sys
import zipfile

from google.transit import gtfs_realtime_pb2 as pb

csv.field_size_limit(10 ** 9)

SNCF = {
    "static": "https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip",
    "trip_updates": "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates",
    "service_alerts": "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-service-alerts",
}
# the names a fixture is written under, so that --source-dir accepts one
RAW_NAMES = {
    "static": "static.zip",
    "trip_updates": "trip_updates.pb",
    "service_alerts": "service_alerts.pb",
}
# an alert can name well over a thousand entities; the selectors a scenario
# depends on are always kept, the rest is capped so the fixture stays reviewable
MAX_INFORMED_ENTITY = 12


def fetch(sources, source_dir):
    raw = {}
    for key, url in sources.items():
        if source_dir:
            with open(os.path.join(source_dir, RAW_NAMES[key]), "rb") as handle:
                raw[key] = handle.read()
            print(f"  {key}: {len(raw[key]):,} bytes from {source_dir}")
            continue
        import requests
        response = requests.get(
            url, headers={"User-Agent": "home-assistant-gtfs2"}, timeout=300)
        response.raise_for_status()
        raw[key] = response.content
        print(f"  {key}: {len(raw[key]):,} bytes from {url}")
    return raw


def read_static(blob):
    tables = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        for name in archive.namelist():
            with archive.open(name) as handle:
                text = io.TextIOWrapper(handle, encoding="utf-8-sig", newline="")
                tables[name] = list(csv.DictReader(text))
    return tables


def resolve(short_id, sorted_ids):
    """Static trips named by short_id: the id itself, or ids that extend it
    with a digit.

    SNCF alerts name a trip as OCESN853603F where the static calls it
    OCESN853603F1187_F:TER:... The digit guard is the agency id: without it a
    short train number would also swallow a longer one. A feed whose alerts
    carry the full trip_id hits the exact match.
    """
    out = []
    i = bisect.bisect_left(sorted_ids, short_id)
    while i < len(sorted_ids) and sorted_ids[i].startswith(short_id):
        candidate = sorted_ids[i]
        if candidate == short_id or (len(candidate) > len(short_id)
                                     and candidate[len(short_id)].isdigit()):
            out.append(candidate)
        i += 1
    return out


def trim_alert(entity, keep_first):
    """Copy an alert, putting the selectors a scenario needs first."""
    new = pb.FeedEntity()
    new.CopyFrom(entity)
    selectors = list(new.alert.informed_entity)
    wanted = [s for s in selectors if keep_first(s)]
    others = [s for s in selectors if not keep_first(s)]
    del new.alert.informed_entity[:]
    for selector in (wanted + others)[:MAX_INFORMED_ENTITY]:
        new.alert.informed_entity.add().CopyFrom(selector)
    return new, len(selectors)


def pick_trip_updates(updates, trips, stations):
    """Keep a few of each shape the feed puts on the wire.

    stations are the stop ids that other stops name as parent_station: an
    update that points at one of those, rather than at a platform, is a shape
    of its own, since the static timetable never calls at a station directly.
    """
    quota = {"scheduled": 3, "added": 2, "canceled": 2, "skipped": 2,
             "station_ids": 2}
    taken = collections.Counter()
    kept = pb.FeedMessage()
    kept.header.CopyFrom(updates.header)
    delayed_trip = None
    for entity in updates.entity:
        trip = entity.trip_update.trip
        relation = trip.ScheduleRelationship.Name(trip.schedule_relationship)
        stop_updates = entity.trip_update.stop_time_update
        if any(s.stop_id in stations for s in stop_updates):
            kind = "station_ids"
        elif relation == "ADDED":
            kind = "added"
        elif relation == "CANCELED":
            kind = "canceled"
        elif any(s.schedule_relationship == 1 for s in stop_updates):
            kind = "skipped"
        elif trip.trip_id in trips:
            kind = "scheduled"
        else:
            continue
        delayed = any(s.departure.delay for s in stop_updates)
        if kind == "scheduled" and delayed and delayed_trip is None:
            delayed_trip = trip.trip_id
        elif taken[kind] >= quota[kind]:
            continue
        taken[kind] += 1
        kept.entity.add().CopyFrom(entity)
    return kept, taken, delayed_trip


def pack(tables):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, rows in tables.items():
            if not rows:
                continue
            text = io.StringIO()
            writer = csv.DictWriter(text, fieldnames=list(rows[0].keys()),
                                    lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            archive.writestr(name, text.getvalue())
    return buffer.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--static", default=SNCF["static"],
                        help="url of the static GTFS zip")
    parser.add_argument("--trip-updates", default=SNCF["trip_updates"],
                        help="url of the GTFS-RT trip updates feed")
    parser.add_argument("--alerts", default=SNCF["service_alerts"],
                        help="url of the GTFS-RT service alerts feed")
    parser.add_argument("--provider", default="SNCF via transport.data.gouv.fr",
                        help="recorded in the manifest")
    parser.add_argument("--source-dir",
                        help="reuse static.zip, trip_updates.pb and "
                             "service_alerts.pb found there instead of "
                             "downloading")
    args = parser.parse_args()
    sources = {"static": args.static, "trip_updates": args.trip_updates,
               "service_alerts": args.alerts}

    print("Reading the feeds")
    raw = fetch(sources, args.source_dir)

    static = read_static(raw["static"])
    trips = {r["trip_id"]: r for r in static["trips.txt"]}
    stops = {r["stop_id"]: r for r in static["stops.txt"]}
    routes = {r["route_id"]: r for r in static["routes.txt"]}
    sorted_trip_ids = sorted(trips)
    children = collections.defaultdict(list)
    for stop_id, row in stops.items():
        if row.get("parent_station"):
            children[row["parent_station"]].append(stop_id)
    serves = collections.defaultdict(set)
    for row in static["stop_times.txt"]:
        serves[row["stop_id"]].add(row["trip_id"])

    updates = pb.FeedMessage()
    updates.ParseFromString(raw["trip_updates"])
    alerts = pb.FeedMessage()
    alerts.ParseFromString(raw["service_alerts"])
    print(f"  static: {len(trips):,} trips, {len(stops):,} stops")
    print(f"  trip updates: {len(updates.entity):,} entities")
    print(f"  alerts: {len(alerts.entity):,} entities")

    print("Choosing entities")
    kept_updates, taken, delayed_trip = pick_trip_updates(
        updates, trips, set(children))
    print(f"  trip updates kept: {len(kept_updates.entity)} {dict(taken)}")

    kept_alerts = pb.FeedMessage()
    kept_alerts.header.CopyFrom(alerts.header)
    dropped = {}
    station_alert = None
    station_stop = None
    trip_alert_target = None

    # one alert naming a station that a real trip actually calls at
    for entity in alerts.entity:
        for selector in entity.alert.informed_entity:
            if not selector.stop_id:
                continue
            for child in sorted(children.get(selector.stop_id, [])):
                if serves.get(child):
                    station_alert, station_stop = entity, child
                    break
            if station_alert is not None:
                break
        if station_alert is not None:
            break

    # a trip that calls at that station without starting or ending there, so a
    # journey can pass through the alert rather than begin or end on it
    interior_trip = None
    if station_stop:
        for trip_id in sorted(serves[station_stop]):
            rows = sorted((r for r in static["stop_times.txt"]
                           if r["trip_id"] == trip_id),
                          key=lambda r: int(r["stop_sequence"]))
            index = next((i for i, r in enumerate(rows)
                          if r["stop_id"] == station_stop), None)
            if index is not None and 0 < index < len(rows) - 1:
                interior_trip = trip_id
                break

    # one alert naming a trip the static feed knows under its long id
    for entity in alerts.entity:
        if entity is station_alert:
            continue
        for selector in entity.alert.informed_entity:
            if selector.HasField("trip") and selector.trip.trip_id:
                matches = resolve(selector.trip.trip_id, sorted_trip_ids)
                if matches:
                    trip_alert_target = (entity, selector.trip.trip_id, matches[0])
                    break
        if trip_alert_target is not None:
            break

    chosen = []
    if station_alert is not None:
        chosen.append((station_alert, lambda s: bool(s.stop_id)))
    if trip_alert_target is not None:
        alert_entity, short_id, _ = trip_alert_target
        chosen.append((alert_entity, lambda s, sid=short_id: s.trip.trip_id == sid))
    # then a few for their shape alone: German-first, single language, HTML in
    # the description, a cause other than the proto default
    extra = {"german_first": 2, "single_language": 2, "html": 2, "cause": 2}
    already = {id(e) for e, _ in chosen}
    for entity in alerts.entity:
        if id(entity) in already:
            continue
        alert = entity.alert
        languages = [t.language for t in alert.header_text.translation]
        html = any("<" in t.text for t in alert.description_text.translation)
        if languages[:1] == ["de"] and extra["german_first"]:
            kind = "german_first"
        elif len(set(languages)) == 1 and extra["single_language"]:
            kind = "single_language"
        elif html and extra["html"]:
            kind = "html"
        elif alert.cause != 1 and extra["cause"]:
            kind = "cause"
        else:
            continue
        extra[kind] -= 1
        already.add(id(entity))
        chosen.append((entity, lambda s: False))
    for entity, keep_first in chosen:
        trimmed, before = trim_alert(entity, keep_first)
        if before > MAX_INFORMED_ENTITY:
            dropped[entity.id] = before - MAX_INFORMED_ENTITY
        kept_alerts.entity.add().CopyFrom(trimmed)
    print(f"  alerts kept: {len(kept_alerts.entity)}, "
          f"{sum(dropped.values())} informed_entity dropped over {len(dropped)}")

    # --- cut the static down to what the fixtures name --------------------
    keep_trips = {e.trip_update.trip.trip_id for e in kept_updates.entity}
    keep_trips &= set(trips)
    if station_stop:
        keep_trips |= set(sorted(serves[station_stop])[:2])
    if interior_trip:
        keep_trips.add(interior_trip)
    for entity in kept_alerts.entity:
        for selector in entity.alert.informed_entity:
            if selector.HasField("trip") and selector.trip.trip_id:
                keep_trips.update(resolve(selector.trip.trip_id, sorted_trip_ids)[:2])
    # one trip nothing says anything about, as the control
    quiet_trip = next(t for t in sorted_trip_ids if t not in keep_trips)
    keep_trips.add(quiet_trip)

    stop_times = [r for r in static["stop_times.txt"] if r["trip_id"] in keep_trips]
    keep_stops = {r["stop_id"] for r in stop_times}
    for entity in kept_alerts.entity:
        for selector in entity.alert.informed_entity:
            if selector.stop_id:
                keep_stops.add(selector.stop_id)
    for stop_id in list(keep_stops):
        parent = stops.get(stop_id, {}).get("parent_station")
        if parent:
            keep_stops.add(parent)
    keep_routes = {trips[t]["route_id"] for t in keep_trips if t in trips}
    # a few placeholder-named routes, for the route label work
    placeholders = [r for r in static["routes.txt"]
                    if not any(c.isalpha() for c in r.get("route_long_name", ""))][:3]
    keep_routes.update(r["route_id"] for r in placeholders)
    keep_services = {trips[t]["service_id"] for t in keep_trips if t in trips}

    tables = {
        "agency.txt": static["agency.txt"],
        "feed_info.txt": static.get("feed_info.txt", []),
        "routes.txt": [r for r in static["routes.txt"] if r["route_id"] in keep_routes],
        "trips.txt": [trips[t] for t in sorted(keep_trips) if t in trips],
        "stop_times.txt": stop_times,
        "stops.txt": [stops[s] for s in sorted(keep_stops) if s in stops],
        "calendar.txt": [r for r in static.get("calendar.txt", [])
                         if r["service_id"] in keep_services],
        "calendar_dates.txt": [r for r in static.get("calendar_dates.txt", [])
                               if r["service_id"] in keep_services],
    }

    os.makedirs(args.out, exist_ok=True)
    write(os.path.join(args.out, "static.zip"), pack(tables))
    write(os.path.join(args.out, "trip_updates.pb"), kept_updates.SerializeToString())
    write(os.path.join(args.out, "service_alerts.pb"), kept_alerts.SerializeToString())

    # --- the scenarios, built from what was kept --------------------------
    by_trip = collections.defaultdict(list)
    for row in stop_times:
        by_trip[row["trip_id"]].append(row)
    for rows in by_trip.values():
        rows.sort(key=lambda r: int(r["stop_sequence"]))

    scenarios = []
    if trip_alert_target is not None:
        long_id = trip_alert_target[2]
        if long_id in by_trip:
            scenarios.append(scenario("alert names this trip", long_id, trips, by_trip))
    if station_stop:
        for trip_id in sorted(serves[station_stop] & keep_trips):
            rows = by_trip.get(trip_id, [])
            index = next((i for i, r in enumerate(rows)
                          if r["stop_id"] == station_stop), None)
            if index is not None and index < len(rows) - 1:
                scenarios.append(scenario(
                    "alert names the station of the origin stop", trip_id,
                    trips, by_trip, origin_index=index))
                break
    if interior_trip and interior_trip in by_trip:
        scenarios.append(scenario(
            "alert names a stop along the way", interior_trip, trips, by_trip))
        # the same alert and the same run, boarding one stop later: what the
        # journey has already left behind is not the journey's business, and
        # without this nothing ever exercised the lower bound of the slice
        rows = by_trip[interior_trip]
        index = next((i for i, r in enumerate(rows)
                      if r["stop_id"] == station_stop), None)
        if index is not None and index + 1 <= len(rows) - 2:
            scenarios.append(scenario(
                "alert names a stop already behind you", interior_trip, trips,
                by_trip, origin_index=index + 1))
    if delayed_trip and delayed_trip in by_trip:
        scenarios.append(scenario("trip update carries a delay", delayed_trip,
                                  trips, by_trip))
    scenarios.append(scenario("nothing announced on this trip", quiet_trip,
                              trips, by_trip))

    manifest = {
        "provider": args.provider,
        "sources": sources,
        "captured_at": kept_updates.header.timestamp,
        "alerts_timestamp": kept_alerts.header.timestamp,
        "kept": {
            "trip_update_entities": len(kept_updates.entity),
            "alert_entities": len(kept_alerts.entity),
            "trips": len(tables["trips.txt"]),
            "stop_times": len(tables["stop_times.txt"]),
            "stops": len(tables["stops.txt"]),
            "routes": len(tables["routes.txt"]),
            "calendar": len(tables["calendar.txt"]),
            "calendar_dates": len(tables["calendar_dates.txt"]),
        },
        "source_totals": {
            "trip_update_entities": len(updates.entity),
            "alert_entities": len(alerts.entity),
            "trips": len(trips),
            "stops": len(stops),
            "routes": len(routes),
        },
        "informed_entity_cap": MAX_INFORMED_ENTITY,
        "informed_entity_dropped": dropped,
    }
    write(os.path.join(args.out, "manifest.json"),
          json.dumps(manifest, indent=2, sort_keys=True).encode())
    write(os.path.join(args.out, "scenarios.json"),
          json.dumps({"scenarios": scenarios}, indent=2).encode())
    print(f"\nFixture written to {args.out}")


def scenario(name, trip_id, trips, by_trip, origin_index=0):
    rows = by_trip[trip_id]
    origin = rows[origin_index]
    destination = rows[-1]
    trip = trips[trip_id]
    return {
        "name": name,
        "route_id": trip["route_id"],
        "trip_id": trip_id,
        "direction": trip.get("direction_id", "0"),
        "stop_id": origin["stop_id"],
        "stop_sequence": int(origin["stop_sequence"]),
        "destination_id": destination["stop_id"],
        "destination_sequence": int(destination["stop_sequence"]),
    }


def write(path, blob):
    with open(path, "wb") as handle:
        handle.write(blob)
    print(f"  wrote {os.path.basename(path)}: {len(blob) / 1024:.0f} KB")


if __name__ == "__main__":
    sys.exit(main())
