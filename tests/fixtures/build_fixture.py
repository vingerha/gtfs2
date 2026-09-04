"""Build a frozen static fixture from a GTFS zip, cut down to named routes.

Same idea as capture_rt.py, for the static side: real data, only smaller.
The named routes keep their trips capped per distinct stop pattern, so every
shape the route runs (full runs, short turns, branch variants) survives with
a few specimens while the fixture stays reviewable. manifest.json records
where the zip came from, what was kept and, with --why, what the fixture is
there for.

    python tests/fixtures/build_fixture.py --zip tao_orleans.zip \\
        --out tests/fixtures/tao-journeys --routes 22,N,40,41,A,B \\
        --provider "TAO Orleans" \\
        --source https://chouette.enroute.mobi/api/v1/datas/keolis_orleans/gtfs.zip

Routes are named by route_short_name (every route bearing the name is kept,
a short name is not unique in a national feed) or by exact route_id;
--agency narrows the match to one agency_id first.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
import zipfile
from collections import defaultdict

csv.field_size_limit(10 ** 9)

TABLES = ("agency.txt", "routes.txt", "trips.txt", "stop_times.txt",
          "stops.txt", "calendar.txt", "calendar_dates.txt", "feed_info.txt")


def rows_of(archive, name):
    return csv.DictReader(io.TextIOWrapper(
        archive.open(name), encoding="utf-8-sig", newline=""))


def header_of(archive, name):
    with archive.open(name) as handle:
        return next(csv.reader(
            io.TextIOWrapper(handle, encoding="utf-8-sig", newline="")))


def pick_routes(archive, wanted, agency):
    picked = defaultdict(list)  # short name or id as asked -> [route rows]
    for row in rows_of(archive, "routes.txt"):
        if agency and row.get("agency_id") != agency:
            continue
        for name in wanted:
            if row["route_id"] == name or row.get("route_short_name") == name:
                picked[name].append(row)
    missing = [name for name in wanted if name not in picked]
    if missing:
        raise SystemExit(f"routes not found in the feed: {missing}")
    return picked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--routes", required=True,
                        help="comma separated route_short_names or route_ids")
    parser.add_argument("--agency", help="agency_id to narrow the route match")
    parser.add_argument("--cap", type=int, default=5,
                        help="trips kept per distinct stop pattern")
    parser.add_argument("--provider", required=True)
    parser.add_argument("--source", required=True,
                        help="where the zip came from, recorded in the manifest")
    parser.add_argument("--why", default="")
    args = parser.parse_args()

    archive = zipfile.ZipFile(args.zip)
    wanted = [name.strip() for name in args.routes.split(",") if name.strip()]
    picked = pick_routes(archive, wanted, args.agency)
    route_rows = [row for rows in picked.values() for row in rows]
    route_ids = {row["route_id"] for row in route_rows}
    for name, rows in sorted(picked.items()):
        print(f"route {name}: {len(rows)} feed route(s)")

    trip_meta = {}  # trip_id -> (route_id, direction or None, row)
    for row in rows_of(archive, "trips.txt"):
        if row["route_id"] in route_ids:
            direction = row.get("direction_id", "")
            trip_meta[row["trip_id"]] = (
                row["route_id"],
                int(direction) if direction not in ("", None) else None,
                row)

    header = header_of(archive, "stop_times.txt")
    i_trip = header.index("trip_id")
    i_stop = header.index("stop_id")
    i_seq = header.index("stop_sequence")
    sequences = defaultdict(list)
    st_total = 0
    handle = io.TextIOWrapper(
        archive.open("stop_times.txt"), encoding="utf-8-sig", newline="")
    handle.readline()
    for row in csv.reader(handle):
        st_total += 1
        if row[i_trip] in trip_meta:
            sequences[row[i_trip]].append((int(row[i_seq]), row[i_stop]))

    patterns = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for trip_id, stops in sequences.items():
        route_id, direction, _ = trip_meta[trip_id]
        stops.sort()
        patterns[route_id][direction][tuple(s for _, s in stops)].append(trip_id)

    kept_trips = set()
    for route_id, by_dir in patterns.items():
        capped = {d: {p: sorted(t)[:args.cap] for p, t in pats.items()}
                  for d, pats in by_dir.items()}
        for pats in capped.values():
            for trips in pats.values():
                kept_trips.update(trips)
    print(f"kept trips: {len(kept_trips)} of {len(trip_meta)}")

    tables = {}
    totals = {"stop_times": st_total, "trips": len(trip_meta)}
    tables["routes.txt"] = route_rows
    tables["trips.txt"] = [trip_meta[t][2] for t in sorted(kept_trips)]
    kept_services = {row["service_id"] for row in tables["trips.txt"]}

    st_rows = []
    kept_stops = set()
    handle = io.TextIOWrapper(
        archive.open("stop_times.txt"), encoding="utf-8-sig", newline="")
    handle.readline()
    for row in csv.reader(handle):
        if row[i_trip] in kept_trips:
            st_rows.append(row)
            kept_stops.add(row[i_stop])
    tables["stop_times.txt"] = st_rows

    tables["stops.txt"] = []
    for row in rows_of(archive, "stops.txt"):
        if row["stop_id"] in kept_stops or row.get("parent_station") in kept_stops:
            tables["stops.txt"].append(row)

    agencies = {row.get("agency_id") for row in route_rows}
    tables["agency.txt"] = [row for row in rows_of(archive, "agency.txt")
                            if row["agency_id"] in agencies]
    names = set(archive.namelist())
    for name in ("calendar.txt", "calendar_dates.txt"):
        if name in names:
            tables[name] = [row for row in rows_of(archive, name)
                            if row["service_id"] in kept_services]
    if "feed_info.txt" in names:
        tables["feed_info.txt"] = list(rows_of(archive, "feed_info.txt"))

    os.makedirs(args.out, exist_ok=True)
    with zipfile.ZipFile(os.path.join(args.out, "static.zip"), "w",
                         zipfile.ZIP_DEFLATED) as out:
        for name in TABLES:
            if name not in tables:
                continue
            head = header_of(archive, name)
            buffer = io.StringIO(newline="")
            writer = csv.writer(buffer, lineterminator="\n")
            writer.writerow(head)
            if name == "stop_times.txt":
                writer.writerows(tables[name])
            else:
                writer.writerows(
                    [[row.get(c, "") for c in head] for row in tables[name]])
            out.writestr(name, buffer.getvalue())

    manifest = {
        "captured_at": int(time.time()),
        "provider": args.provider,
        "sources": {"static": args.source},
        "static_only": True,
        "routes_kept": {name: [row["route_id"] for row in rows]
                        for name, rows in picked.items()},
        "trip_cap_per_pattern": args.cap,
        "kept": {os.path.splitext(name)[0]: len(rows)
                 for name, rows in tables.items()},
        "source_totals": totals,
    }
    if args.why:
        manifest["why"] = args.why
    with open(os.path.join(args.out, "manifest.json"), "w",
              encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(manifest["kept"], sort_keys=True))
    print("wrote", os.path.join(args.out, "static.zip"),
          os.path.getsize(os.path.join(args.out, "static.zip")), "bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
