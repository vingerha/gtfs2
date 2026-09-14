"""Departures around midnight must be the ones the feed has.

GTFS times a trip that runs into the next day with hours past 24: those
calls leave on the next calendar day but belong to the previous day's
service. For every fixture where some trip calls past 24:00, such calls are
asked about twice, at 23:50 on a day their service runs and just before the
call, the morning after:

    route       get_next_departure from that stop to the trip's next stop
                answers the first departure the feed has after now
    local_stop  get_local_stops_next_departures at that stop lists exactly
                the departures the feed has within the next hour

The expected side is laid out from the fixture itself: every call, on every
day its service runs (calendar windows by weekday minus their removals, plus
calendar_dates additions), at that service day's midnight plus the call's
seconds. Each promise is also checked on a copy of the db whose services are
written as calendar rows instead, since the queries read the two tables on
different paths and no fixture carries the weekly form.

Trains are left out of the route promise: their stations are matched by
name on a path of their own, which is not what this file is about. Days
next to a clock change are skipped, so an hour lost or gained at night is
not taken for a missing departure.

    pytest tests_provider/test_night.py
"""
from __future__ import annotations

import csv
import datetime
import io
import os
import time
import types
import zipfile
import zoneinfo
from pathlib import Path

import pytest
from freezegun import freeze_time
from sqlalchemy.sql import text

import ha_stub

ha_stub.install()

import homeassistant.util.dt as dt_util  # noqa: E402

import fixture_db  # noqa: E402

gtfs_helper = ha_stub.load("gtfs_helper")

FIXTURES = Path(__file__).parent / "fixtures"
PROMISES = ("route", "local_stop")
SHAPES = ("calendar_dates", "calendar")
RAIL = {2, *range(100, 118)}
WINDOW = 60     # minutes a local stop lists ahead
SAMPLE = 40     # night calls asked about, per fixture and promise
UTC = datetime.timezone.utc


def _hours(value):
    try:
        return int(str(value).split(":", 1)[0])
    except ValueError:
        return -1


def _rows(archive, name):
    with archive.open(name) as handle:
        yield from csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8-sig", newline=""))


def _night_promises():
    """(fixture, promise) for each fixture whose trips call past 24:00: the
    local stop always, the route when one of those trips is not a train."""
    found = []
    for path in sorted(FIXTURES.iterdir()):
        archive_path = path / "static.zip"
        if not archive_path.is_file():
            continue
        with zipfile.ZipFile(archive_path) as archive:
            night = {row["trip_id"] for row in _rows(archive, "stop_times.txt")
                     if _hours(row.get("departure_time")) >= 24}
            if not night:
                continue
            route_of = {row["trip_id"]: row["route_id"] for row in _rows(archive, "trips.txt")}
            rail = {row["route_id"] for row in _rows(archive, "routes.txt")
                    if _hours(row.get("route_type")) in RAIL}
        if any(route_of.get(trip_id) not in rail for trip_id in night):
            found.append((path.name, "route"))
        found.append((path.name, "local_stop"))
    return found


_SCHEDULES = {}


def schedule_of(name, shape):
    """The fixture's db, once per shape."""
    if (name, shape) not in _SCHEDULES:
        schedule = fixture_db.build(str(FIXTURES / name))
        if shape == "calendar":
            _fold_to_calendar(schedule)
        _SCHEDULES[(name, shape)] = schedule
    return _SCHEDULES[(name, shape)]


def _fold_to_calendar(schedule):
    """Write each service given as calendar_dates additions as one calendar
    row instead: its first to last day, the weekdays it runs, and a removal
    for each of those weekdays it skips. The same days, in the weekly form."""
    columns = ("monday", "tuesday", "wednesday", "thursday", "friday",
               "saturday", "sunday")
    with schedule.engine.begin() as conn:
        framed = {row[0] for row in conn.execute(text("SELECT service_id FROM calendar"))}
        runs = {}
        for service_id, day in conn.execute(text(
                "SELECT service_id, date FROM calendar_dates WHERE exception_type = 1")):
            if service_id not in framed:
                runs.setdefault(service_id, set()).add(
                    datetime.date.fromisoformat(str(day)[:10]))
        for service_id, days in runs.items():
            first, last = min(days), max(days)
            weekdays = {day.weekday() for day in days}
            conn.execute(text(
                "INSERT INTO calendar (service_id, " + ", ".join(columns)
                + ", start_date, end_date) VALUES (:service, "
                + ", ".join(f":{c}" for c in columns) + ", :start, :end)"),
                {"service": service_id, "start": first.isoformat(), "end": last.isoformat(),
                 **{c: int(i in weekdays) for i, c in enumerate(columns)}})
            conn.execute(text(
                "DELETE FROM calendar_dates WHERE service_id = :service "
                "AND exception_type = 1"), {"service": service_id})
            day = first
            while day <= last:
                if day.weekday() in weekdays and day not in days:
                    conn.execute(text(
                        "INSERT INTO calendar_dates (service_id, date, exception_type) "
                        "VALUES (:service, :day, 2)"),
                        {"service": service_id, "day": day.isoformat()})
                day += datetime.timedelta(days=1)


def _service_days(conn):
    """Every day each service runs, read from both calendar tables."""
    runs, removed = {}, set()
    for service_id, day, kind in conn.execute(text(
            "SELECT service_id, date, exception_type FROM calendar_dates")):
        day = datetime.date.fromisoformat(str(day)[:10])
        if kind == 1:
            runs.setdefault(service_id, set()).add(day)
        elif kind == 2:
            removed.add((service_id, day))
    for row in conn.execute(text(
            "SELECT service_id, monday, tuesday, wednesday, thursday, friday, "
            "saturday, sunday, start_date, end_date FROM calendar")):
        if not row[8] or not row[9]:
            continue
        day = datetime.date.fromisoformat(str(row[8])[:10])
        end = datetime.date.fromisoformat(str(row[9])[:10])
        while day <= end:
            if row[1 + day.weekday()] and (row[0], day) not in removed:
                runs.setdefault(row[0], set()).add(day)
            day += datetime.timedelta(days=1)
    return runs


def _seconds(stored):
    """Seconds from the service day's midnight. The db stores an hour past 24
    on 1970-01-02, so the date in front counts as days."""
    stored = str(stored)
    days = 0
    if " " in stored:
        day, stored = stored.split(" ", 1)
        days = (datetime.date.fromisoformat(day) - datetime.date(1970, 1, 1)).days
    h, m, s = (int(float(part)) for part in stored.split(":"))
    return days * 86400 + h * 3600 + m * 60 + s


def _laid(day, stored, zone):
    """A stop time on its service day, as an instant."""
    return (datetime.datetime.combine(day, datetime.time(0), zone)
            + datetime.timedelta(seconds=_seconds(stored)))


def _plain_day(days, zone):
    """The first day of the service with no clock change from its noon to
    the next day's, or None."""
    for day in sorted(days):
        offsets = {datetime.datetime.combine(day + datetime.timedelta(days=n),
                                             datetime.time(12), zone).utcoffset()
                   for n in (0, 1)}
        if len(offsets) == 1:
            return day
    return None


def _near(days, now):
    lo, hi = now.date() - datetime.timedelta(days=2), now.date() + datetime.timedelta(days=2)
    return [day for day in days if lo <= day <= hi]


def _night_calls(conn, promise):
    """Calls past 24:00, one per stop (local_stop) or per stop and next stop
    (route, trains left out), spread over the fixture, at most SAMPLE."""
    rows = conn.execute(text(
        "SELECT st.stop_id, st.departure_time, st.trip_id, t.service_id, "
        "t.route_id, t.direction_id, r.route_type, "
        "(SELECT nx.stop_id FROM stop_times nx WHERE nx.trip_id = st.trip_id "
        " AND nx.stop_sequence > st.stop_sequence ORDER BY nx.stop_sequence LIMIT 1) "
        "AS next_stop "
        "FROM stop_times st "
        "INNER JOIN trips t ON t.trip_id = st.trip_id "
        "INNER JOIN routes r ON r.route_id = t.route_id "
        "WHERE st.departure_time >= '1970-01-02' "
        "ORDER BY st.stop_id, st.departure_time, st.trip_id")).fetchall()
    seen, calls = set(), []
    for row in rows:
        if promise == "route":
            if row.next_stop is None or int(row.route_type) in RAIL:
                continue
            key = (row.stop_id, row.next_stop)
        else:
            key = row.stop_id
        if key not in seen:
            seen.add(key)
            calls.append(row)
    step = max(1, len(calls) // SAMPLE)
    return calls[::step][:SAMPLE]


def _first_ride(conn, days, origin, destination, zone, now):
    """The first departure after now of a trip riding origin before
    destination, any line: the stop ids are matched as the sensor matches
    them, exactly."""
    best = None
    for service_id, stored in conn.execute(text(
            "SELECT t.service_id, o.departure_time FROM trips t "
            "INNER JOIN stop_times o ON o.trip_id = t.trip_id "
            "INNER JOIN stop_times x ON x.trip_id = t.trip_id "
            "WHERE o.stop_id = :o AND x.stop_id = :d "
            "AND o.stop_sequence < x.stop_sequence"), {"o": origin, "d": destination}):
        if stored is None:
            continue
        for day in _near(days.get(service_id, ()), now):
            at = _laid(day, stored, zone)
            if at > now and (best is None or at < best):
                best = at
    return best


def _calls_within(conn, days, stop_id, zone, now):
    """(instant, trip) of every call at the stop in the coming WINDOW."""
    until = now + datetime.timedelta(minutes=WINDOW)
    found = set()
    for trip_id, service_id, stored in conn.execute(text(
            "SELECT st.trip_id, t.service_id, st.departure_time FROM stop_times st "
            "INNER JOIN trips t ON t.trip_id = st.trip_id WHERE st.stop_id = :s"),
            {"s": stop_id}):
        if stored is None:
            continue
        for day in _near(days.get(service_id, ()), now):
            at = _laid(day, stored, zone)
            if now < at <= until:
                found.add((at.astimezone(UTC), str(trip_id)))
    return found


def _instant(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.datetime.fromisoformat(value)
    return value.astimezone(UTC)


def _show(at, zone):
    return at.astimezone(zone).strftime("%m-%d %H:%M:%S") if at else "nothing"


def _pin_process_zone(name):
    """The route query asks SQLite for datetime('now', 'localtime'), which
    follows the process zone, not Home Assistant's: pin it to the agency's
    where a process can change it (time.tzset is POSIX only). Returns what
    to put back."""
    if not hasattr(time, "tzset"):
        return None
    before = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    return (before,)


def _unpin_process_zone(saved):
    if saved is None:
        return
    if saved[0] is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved[0]
    time.tzset()


def _hass(zone_name, where=None):
    return types.SimpleNamespace(
        config=types.SimpleNamespace(path=lambda *parts: str(Path(*parts)),
                                     time_zone=zone_name),
        states=types.SimpleNamespace(get=lambda _entity: types.SimpleNamespace(
            attributes={"latitude": where[0], "longitude": where[1]} if where else {})))


def check_route(conn, schedule, days, names, zone_name, zone, call, label, now):
    data = {"schedule": schedule, "gtfs_dir": ".", "file": "fixture",
            "route_type": str(call.route_type), "offset": 0,
            "origin": f"{call.stop_id}: {names.get(call.stop_id)}",
            "destination": f"{call.next_stop}: {names.get(call.next_stop)}",
            "route": call.route_id, "direction": str(call.direction_id),
            "include_tomorrow": True}
    want = _first_ride(conn, days, call.stop_id, call.next_stop, zone, now)
    result = gtfs_helper.get_next_departure(_hass(zone_name), data)
    got = _instant(result.get("departure_time")) if result else None
    want = _instant(want)
    return {"ok": got == want,
            "text": (f"{label} on {now:%Y-%m-%d %H:%M}, {call.stop_id} -> "
                     f"{call.next_stop}: the feed leaves {_show(want, zone)}, "
                     f"got {_show(got, zone)}"),
            "asked": {"origin": call.stop_id, "destination": call.next_stop,
                      "at": now.isoformat()},
            "got": {"departure": got.isoformat() if got else None,
                    "trip": result.get("trip_id") if result else None}}


def check_local_stop(conn, schedule, days, where, zone_name, zone, call, label, now):
    me = types.SimpleNamespace(
        hass=_hass(zone_name, where), _realtime=False,
        _data={"schedule": schedule, "offset": 0, "file": "fixture", "gtfs_dir": ".",
               "device_tracker_id": "person.rider", "radius": 1,
               "timerange": WINDOW, "timerange_history": 15, "name": "night"})
    want = _calls_within(conn, days, call.stop_id, zone, now)
    got = set()
    for entry in gtfs_helper.get_local_stops_next_departures(me) or []:
        for departure in entry.get("departure", []):
            if departure["stop_id"] == call.stop_id:
                got.add((_instant(departure["departure_datetime"]), str(departure["trip_id"])))
    missing = sorted(want - got)
    extra = sorted(got - want)
    detail = ""
    if missing:
        detail += " missing " + ", ".join(f"{_show(a, zone)} {t}" for a, t in missing[:4])
    if extra:
        detail += " not in the feed " + ", ".join(f"{_show(a, zone)} {t}" for a, t in extra[:4])
    return {"ok": not missing and not extra,
            "text": (f"{label} on {now:%Y-%m-%d %H:%M}, stop {call.stop_id}: the feed has "
                     f"{len(want)} in the next {WINDOW} min, got {len(got)}" + detail),
            "asked": {"stop": call.stop_id, "at": now.isoformat()},
            "got": {"listed": len(got), "missing": len(missing), "extra": len(extra)}}


def _cases():
    return [pytest.param(name, promise, shape, id=f"{name}-{promise}-{shape}")
            for name, promise in _night_promises() for shape in SHAPES]


@pytest.mark.parametrize(("fixture", "promise", "shape"), _cases())
def test_night(record_property, fixture, promise, shape):
    schedule = schedule_of(fixture, shape)
    with schedule.engine.connect() as conn:
        zone_name = conn.execute(text(
            "SELECT agency_timezone FROM agency "
            "WHERE agency_timezone IS NOT NULL")).scalar() or "UTC"
        names = dict(conn.execute(text("SELECT stop_id, stop_name FROM stops")).fetchall())
        places = {row[0]: (row[1], row[2]) for row in conn.execute(text(
            "SELECT stop_id, stop_lat, stop_lon FROM stops"))}
        days = _service_days(conn)
        calls = _night_calls(conn, promise)
        if not calls:
            pytest.skip("no call past 24:00 this promise covers")
        zone = zoneinfo.ZoneInfo(zone_name)
        dt_util.set_default_time_zone(dt_util.get_time_zone(zone_name))
        checks = []
        saved = _pin_process_zone(zone_name)
        try:
            for call in calls:
                day = _plain_day(days.get(call.service_id, ()), zone)
                if day is None:
                    continue
                call_at = _laid(day, call.departure_time, zone)
                morning = datetime.datetime.combine(
                    day + datetime.timedelta(days=1), datetime.time(0, 0, 30), zone)
                clocks = (("at 23:50", datetime.datetime.combine(day, datetime.time(23, 50), zone)),
                          ("before the call", max(call_at - datetime.timedelta(minutes=10), morning)))
                for label, now in clocks:
                    with freeze_time(now.astimezone(UTC)):
                        if promise == "route":
                            checks.append(check_route(conn, schedule, days, names, zone_name,
                                                      zone, call, label, now))
                        else:
                            checks.append(check_local_stop(conn, schedule, days,
                                                           places[call.stop_id], zone_name,
                                                           zone, call, label, now))
        finally:
            _unpin_process_zone(saved)
    record_property("case", {"fixture": fixture, "promise": promise, "shape": shape})
    record_property("checks", checks)
    broke = [check["text"] for check in checks if not check["ok"]]
    assert not broke, (f"{len(broke)} of {len(checks)} checks broke the promise:\n  "
                       + "\n  ".join(broke[:15]))
