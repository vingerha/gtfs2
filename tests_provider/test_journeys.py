"""Every departure/arrival pair a fixture's routes offer must hold up.

For each route named in a fixture's manifest and each direction it runs,
three promises are checked against the real code, on a db built from the
fixture zip, with the clock pinned to a day the trips actually run:

    stop_list   each stop_id once, in an order every trip agrees with, and
                one entry for two platforms of a place called one after the
                other
    pairs       origin before destination on some trip: get_next_departure
                answers it, on the right stops, in riding order, arriving no
                earlier than it departs
    swapped     destination before origin: nothing, or a ride the line really
                makes (a place served twice, once per platform, is one)

Trains (route_type 2) ride their own path in get_next_departure, matched by
stop name with no direction: for them the pairs are checked by name, the
answer must stay on the asked line, and a swapped pair is legitimate, it is
the return journey, so only `pairs` is checked.

One test per fixture, route, direction and promise; its message lists every
pair that broke the promise. The promises are about what the sensors say,
not how the code says it, so they survive a rewrite of the query.

    pytest tests_provider/
    pytest tests_provider/ -k "palmbus and 21"

This tree is separate from tests/ on purpose: it needs pygtfs, sqlalchemy and
the protobuf bindings (tests_provider/requirements.txt), and its cases come
from real feeds, cut down under tests_provider/fixtures. tests/ha_stub.py is
shared, nothing else is.
"""
from __future__ import annotations

import datetime
import json
import types
import zoneinfo
from pathlib import Path

import pytest
from freezegun import freeze_time
from sqlalchemy import bindparam
from sqlalchemy.sql import text

import ha_stub

ha_stub.install()

import homeassistant.util.dt as dt_util  # noqa: E402

import fixture_db  # noqa: E402

# Loaded on its own rather than through the package, whose __init__ pulls in
# the platforms and with them the rest of Home Assistant.
gtfs_helper = ha_stub.load("gtfs_helper")
get_next_departure = gtfs_helper.get_next_departure
get_stop_list = gtfs_helper.get_stop_list

FIXTURES = Path(__file__).parent / "fixtures"
KINDS = ("stop_list", "pairs", "swapped")


class Fixture:
    """One fixture directory: its manifest, its db, and what the checks read
    from that db over and over."""

    def __init__(self, path: Path) -> None:
        self.name = path.name
        self.path = path
        self.manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        self.schedule = fixture_db.build(str(path))
        self.repaired = _repair_directions(self.schedule)
        with self.schedule.engine.connect() as conn:
            agency_tz = conn.execute(text(
                "SELECT agency_timezone FROM agency "
                "WHERE agency_timezone IS NOT NULL")).fetchone()
            self.route_types = dict(conn.execute(
                text("SELECT route_id, route_type FROM routes")).fetchall())
            self.route_short_names = dict(conn.execute(
                text("SELECT route_id, route_short_name FROM routes")).fetchall())
            self.stop_names = dict(conn.execute(
                text("SELECT stop_id, stop_name FROM stops")).fetchall())
            self.stations = dict(conn.execute(
                text("SELECT stop_id, parent_station FROM stops")).fetchall())
        # get_next_departure compares a departure against "now" in the
        # agency's zone (it overrides the Home Assistant one as soon as the
        # row carries it), so the clock is pinned in that zone: 00:05 UTC is
        # 02:05 in Paris, past a night line's 00:30 departure
        self.agency_tz = agency_tz[0] if agency_tz else "UTC"

    def hass(self):
        """What get_next_departure reads off hass, and nothing more."""
        return types.SimpleNamespace(config=types.SimpleNamespace(
            path=lambda *parts: str(self.path.joinpath(*parts)),
            time_zone=self.agency_tz))

    def instant_on(self, date_iso: str) -> datetime.datetime:
        """00:05 wall time of the agency's zone on that day."""
        return datetime.datetime.combine(
            datetime.date.fromisoformat(date_iso), datetime.time(0, 5),
            zoneinfo.ZoneInfo(self.agency_tz))

    def station_of(self, stop_id):
        """The parent station the feed declares for a stop, if any."""
        return self.stations.get(stop_id) or None

    def siblings_of(self, stop_id):
        """Every record the feed groups with this one under a parent station.

        A stop written once per platform is one place to a rider, and since
        the journey's ends are matched on whole stops, get_next_departure may
        answer on any of their records: a pair is right when it lands on one
        of these, not only on the record the pattern happened to carry.
        """
        parent = self.station_of(stop_id)
        if not parent:
            return {stop_id}
        return {stop_id} | {s for s, p in self.stations.items() if p == parent}


def _repair_directions(schedule):
    """Run the checkout's direction repair on the db, when it has one.

    A checkout that repairs direction_ids at import time serves its sensors
    the repaired trips, so the promises are checked on those; a checkout
    without one is checked on what the feed published. Returns how many trips
    moved, or None when there is nothing to run.
    """
    if not (ha_stub.COMPONENT / "direction_repair.py").is_file():
        return None
    return ha_stub.load("direction_repair").repair_trip_directions(schedule)


_LOADED: dict[str, Fixture] = {}


def fixture_of(name: str) -> Fixture:
    if name not in _LOADED:
        _LOADED[name] = Fixture(FIXTURES / name)
    return _LOADED[name]


def directions_of(schedule, route_id):
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT DISTINCT direction_id FROM trips WHERE route_id = :r"),
            {"r": route_id}).fetchall()
    directions = sorted({row[0] for row in rows if row[0] is not None})
    return directions or [None]


def patterns_of(schedule, route_id, direction):
    """{stop pattern: [trip_id]} for one route and direction."""
    where = "AND (t.direction_id = :d OR t.direction_id IS NULL)"
    if direction is None:
        where = "AND t.direction_id IS NULL"
    sql = f"""
    SELECT st.trip_id, st.stop_id, st.stop_sequence
    FROM trips t INNER JOIN stop_times st ON st.trip_id = t.trip_id
    WHERE t.route_id = :r {where}
    ORDER BY st.trip_id, st.stop_sequence
    """  # noqa: S608
    trips = {}
    with schedule.engine.connect() as conn:
        for trip_id, stop_id, _seq in conn.execute(
                text(sql), {"r": route_id, "d": direction}):
            trips.setdefault(trip_id, []).append(stop_id)
    grouped = {}
    for trip_id, stops in trips.items():
        grouped.setdefault(tuple(stops), []).append(trip_id)
    return grouped


def service_date(schedule, trip_ids):
    """The first day one of these trips runs, as an ISO date, or None."""
    with schedule.engine.connect() as conn:
        row = conn.execute(text(
            "SELECT min(cd.date) FROM calendar_dates cd "
            "INNER JOIN trips t ON t.service_id = cd.service_id "
            "WHERE cd.exception_type = 1 AND t.trip_id IN :trips"
        ).bindparams(bindparam("trips", expanding=True)),
            {"trips": list(trip_ids)}).fetchone()
    return row[0]


def circular_like(grouped_by_dir):
    """Both directions ride mostly the same stops in the same order.

    That is a circular line whose two 'directions' are rotations (GVB line
    14): its trips wrap around the loop, and no linear list can agree with
    every wrap point, so the order check has to allow the one wrap.
    """
    if len(grouped_by_dir) != 2:
        return False
    chains = []
    for grouped in grouped_by_dir.values():
        if not grouped:
            return False
        maxlen = max(len(p) for p in grouped)
        chains.append(max((p for p in grouped if len(p) == maxlen),
                          key=lambda p: len(grouped[p])))
    first, second = chains
    pos = {stop: n for n, stop in enumerate(second)}
    shared = [pos[stop] for stop in first if stop in pos]
    if len(shared) <= 0.5 * len(first) or len(shared) < 2:
        return False
    inc = sum(1 for a, b in zip(shared, shared[1:]) if b > a)
    return inc / (len(shared) - 1) > 0.8


def served_between(patterns, origins, destinations):
    """Some trip of this direction really rides from one set to the other."""
    for pattern in patterns:
        before = [i for i, stop in enumerate(pattern) if stop in origins]
        after = [i for i, stop in enumerate(pattern) if stop in destinations]
        if before and after and min(before) < max(after):
            return True
    return False


def sample_pairs(pattern):
    """First to last, first to middle, middle to last: the ends and a leg."""
    seen = []
    first, last = 0, len(pattern) - 1
    middle = len(pattern) // 2
    for pair in ((first, last), (first, middle), (middle, last)):
        o, d = pair
        if o < d and pattern[o] != pattern[d] and pair not in seen:
            seen.append(pair)
    return seen


class Check:
    """Every verification made for one case, as records: the verdict, the
    line a reader sees, and for a pair the asked and answered sides as
    fields. conftest.py writes them to results.txt and results.json."""

    def __init__(self):
        self.records = []

    def note(self, ok, text, **fields):
        self.records.append({"ok": bool(ok), "text": text, **fields})

    @property
    def failures(self):
        return [r["text"] for r in self.records if not r["ok"]]


# Cases known to fail on main today, each with what breaks the promise.
# The marks are strict: the day a fix lands, its marks have to go with it,
# which is how a fix PR and the test that turns green arrive together.
SELECTOR = ("the stop selector offers a stop twice or out of riding order "
            "(discussion #198)")
SELECTOR_GVB = SELECTOR + "; trams 1, 7 and 17 also carry wrong direction_ids"
SWAPPED = ("the swapped pair is answered although the asked direction does "
           "not ride it: the query filters neither route nor direction")
TRAIN = ("a train journey is matched by stop name prefix on any line, not "
         "the asked one")
KNOWN = {
    **{f"gvb-{r}-d{d}-stop_list": SELECTOR_GVB
       for r in ("1", "7", "13", "14", "17") for d in (0, 1)},
    "palmbus-22-d0-stop_list": SELECTOR,
    "palmbus-B-d1-stop_list": SELECTOR,
    **{f"tao-journeys-{r}-d{d}-stop_list": SELECTOR
       for r in ("40", "A", "B") for d in (0, 1)},
    **{f"{line}-d{d}-swapped": SWAPPED
       for line in ("gvb-7", "palmbus-A", "palmbus-B", "tao-journeys-40",
                    "tao-journeys-A", "tao-journeys-B", "tao-journeys-N")
       for d in (0, 1)},
    "sncf-journeys-K8+-d0-pairs": TRAIN,
    "sncf-journeys-K8+-d1-pairs": TRAIN,
    "sncf-journeys-P8(A594575:)-d1-pairs": TRAIN,
    "sncf-journeys-P8(CDD3F95:)-d1-pairs": TRAIN,
}


def _cases():
    cases = []
    if not FIXTURES.is_dir():
        return cases
    for path in sorted(FIXTURES.iterdir()):
        if not (path / "manifest.json").is_file():
            continue
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        routes_kept = manifest.get("routes_kept")
        if not manifest.get("static_only") or not routes_kept:
            continue
        fx = fixture_of(path.name)
        for label, ids in sorted(routes_kept.items()):
            ids = [ids] if isinstance(ids, str) else ids
            for route_id in ids:
                train = fx.route_types.get(route_id) == 2
                kinds = ("pairs",) if train else KINDS
                shown = label if len(ids) == 1 else f"{label}({route_id[-8:]})"
                for direction in directions_of(fx.schedule, route_id):
                    for kind in kinds:
                        case_id = f"{path.name}-{shown}-d{direction}-{kind}"
                        marks = ([pytest.mark.xfail(strict=True, reason=KNOWN[case_id])]
                                 if case_id in KNOWN else [])
                        cases.append(pytest.param(
                            path.name, route_id, direction, kind,
                            id=case_id, marks=marks))
    return cases


CASES = _cases()


@pytest.mark.parametrize("fixture,route_id,direction,kind", CASES)
def test_journeys(record_property, fixture, route_id, direction, kind):
    fx = fixture_of(fixture)
    # HA sets its default zone once at startup from the configured one, which
    # on an install reading a French network is the French one; left in UTC
    # the query picks its calendar day in UTC while the departures are
    # compared in Paris, two different days for a night line
    dt_util.set_default_time_zone(dt_util.get_time_zone(fx.agency_tz))
    check = Check()
    if fx.route_types.get(route_id) == 2:
        check_train_route(check, fx, route_id, direction)
    else:
        check_route(check, fx, route_id, direction, kind)
    record_property("case", {"fixture": fixture, "route": route_id,
                             "direction": direction, "kind": kind})
    record_property("checks", check.records)
    assert not check.failures, "\n".join(check.failures)


def check_route(check, fx, route_id, direction, kind):
    schedule = fx.schedule
    directions = directions_of(schedule, route_id)
    by_dir = {d: patterns_of(schedule, route_id, d) for d in directions}
    circular = circular_like(by_dir)
    grouped = by_dir[direction]
    query_direction = 0 if direction is None else direction
    entries = get_stop_list(schedule, route_id, query_direction)
    ids = [entry.split(": ", 1)[0] for entry in entries]
    position = {stop_id: n for n, stop_id in enumerate(ids)}

    # A trip's stop may not be in the list under its own record when the
    # list offers a station once: the entry offered for its station stands
    # for it, and that is the entry a user would pick. A station a line
    # calls at twice keeps several offered records (GVB 14 lists Dam three
    # times, outbound and both return platforms), so the stand-in is read
    # along the ride, taking the offered record nearest to where the trip
    # already is rather than any of them.
    def place_of(stop_id, previous):
        if stop_id in position:
            return position[stop_id]
        offered = [position[sibling]
                   for sibling in fx.siblings_of(stop_id)
                   if sibling in position]
        if not offered:
            return None
        if previous is None:
            return min(offered)
        return min(offered, key=lambda n: (n < previous, abs(n - previous)))

    if kind == "stop_list":
        check.note(len(ids) == len(set(ids)), "the stop list repeats a stop_id")
        # Two platforms of one place, called one after the other, are one
        # entry: TAO line A offered Jules Verne twice and each choice hid
        # half the trams. Two records of a station the line calls at twice,
        # far apart in the ride, are not that case and stay offered.
        folded = [(a, b) for a, b in zip(ids, ids[1:])
                  if fx.station_of(a) and fx.station_of(a) == fx.station_of(b)]
        check.note(not folded,
                   f"the list offers one station twice in a row {folded[:1]}")
        for pattern in grouped:
            places = []
            for stop in pattern:
                previous = next((p for p in reversed(places) if p is not None), None)
                places.append(place_of(stop, previous))
            check.note(None not in places,
                       "a trip serves a stop the list does not offer")
            known = [place for place in places if place is not None]
            descents = sum(1 for a, b in zip(known, known[1:]) if a >= b)
            # a rotation of a circular line reads k..n then 0..k-1: one
            # descent, ending below its start, is the loop's own seam
            ordered = descents == 0 or (
                circular and descents == 1 and known[-1] < known[0])
            check.note(ordered, "the list contradicts the riding order "
                                f"{pattern[0]} .. {pattern[-1]}")
        return

    hass = fx.hass()
    route_type = str(fx.route_types.get(route_id))
    with freeze_time(fx.instant_on("1970-01-01")) as clock:
        for pattern, trip_ids in sorted(grouped.items()):
            # the same stand-in reading as above, so a pair asks for the
            # entry a user would have picked for that stop
            stood_for = {}
            previous = None
            for stop in pattern:
                stood_for[stop] = place_of(stop, previous)
                previous = stood_for[stop] if stood_for[stop] is not None else previous
            if any(place is None for place in stood_for.values()):
                check.note(False, "a pattern stop has no entry")
                continue
            day = service_date(schedule, trip_ids)
            if day is None:
                check.note(False, "no service date for a pattern")
                continue
            clock.move_to(fx.instant_on(day))
            for o, d in sample_pairs(pattern):
                data = _data_for(schedule, route_id, route_type, entries,
                                 stood_for, pattern[o], pattern[d],
                                 query_direction)
                if kind == "pairs":
                    result = get_next_departure(hass, data)
                    ok = (isinstance(result, dict) and result
                          and result.get("origin_stop_id")
                          in fx.siblings_of(pattern[o])
                          and result.get("destination_stop_id")
                          in fx.siblings_of(pattern[d])
                          and result["origin_stop_sequence"]
                          < result["destination_stop_time"]["Sequence"]
                          and result["arrival_time"] >= result["departure_time"])
                    asked = asked_of(pattern, o, d, route_id, query_direction)
                    got = got_of(result)
                    check.note(ok, answered(asked, got), asked=asked, got=got)
                else:
                    swapped = dict(data, origin=data["destination"],
                                   destination=data["origin"])
                    result = get_next_departure(hass, swapped)
                    # A destination is matched on the whole stop, so the
                    # reverse pair is answerable whenever this direction
                    # really rides from the asked destination to one of the
                    # records of the asked origin's stop, which happens on
                    # the lines that serve a place twice, once per platform.
                    # So the promise is not "nothing": it is that an answer,
                    # when there is one, matches a ride the line actually
                    # makes. Answering nothing stays acceptable, the pattern
                    # that rides it may not run on the frozen day.
                    served = served_between(grouped, fx.siblings_of(pattern[d]),
                                            fx.siblings_of(pattern[o]))
                    honest = not result or (
                        served
                        and result["origin_stop_sequence"]
                        < result["destination_stop_time"]["Sequence"]
                        and result["arrival_time"] >= result["departure_time"])
                    asked = asked_of(pattern, d, o, route_id, query_direction,
                                     served=served)
                    got = got_of(result)
                    check.note(honest, answered(asked, got), asked=asked, got=got)


def asked_of(pattern, a, b, route, direction, served=None):
    """The asked side of a pair, with where the two stops sit in the
    sequence being checked, so the line can be read without opening the
    zip: stop 35 to stop 1 of a 35-stop ride is a journey against the
    direction. `served` says whether this direction rides it at all."""
    asked = {"origin": pattern[a], "destination": pattern[b], "route": route,
             "direction": direction, "from_stop": a + 1, "to_stop": b + 1,
             "ride_length": len(pattern)}
    if served is not None:
        asked["served"] = served
    return asked


def got_of(result, by_name=False):
    """What get_next_departure gave back: the stops, the line and direction
    of the trip it took them from, and that trip. None when it gave nothing."""
    if not result:
        return None
    if by_name:
        return {"origin": result.get("origin_stop_name"),
                "destination": result.get("destination_stop_name"),
                "route": result.get("route_short_name"),
                "direction": result.get("trip_direction_id"),
                "trip": result.get("trip_id")}
    return {"origin": result.get("origin_stop_id"),
            "destination": result.get("destination_stop_id"),
            "route": result.get("route_id"),
            "direction": result.get("trip_direction_id"),
            "trip": result.get("trip_id")}


def answered(asked, got):
    """The one line a reader sees for a pair: the asked side, then the
    answered side, both rendered from the same records results.json holds."""
    where = (f"stop {asked['from_stop']} to stop {asked['to_stop']} of a "
             f"{asked['ride_length']}-stop ride")
    if asked.get("served") is False:
        where += ", this direction does not make it"
    on = f" on {asked['route']}"
    if asked["direction"] is not None:
        on += f" d{asked['direction']}"
    head = f"asked {asked['origin']} -> {asked['destination']}{on} ({where})"
    if not got:
        return f"{head}: no departure"
    return (f"{head}: got {got['origin']} -> {got['destination']} on "
            f"{got['route']} d{got['direction']}, trip {got['trip']}")


def _data_for(schedule, route_id, route_type, entries, position,
              origin, destination, direction):
    return {
        "schedule": schedule,
        "gtfs_dir": ".", "file": "fixture",
        "route_type": route_type,
        "origin": entries[position[origin]],
        "destination": entries[position[destination]],
        "direction": str(direction),
        "route": route_id,
        "offset": 0,
        "include_tomorrow": False,
    }


def check_train_route(check, fx, route_id, direction):
    schedule = fx.schedule
    short_name = fx.route_short_names[route_id]
    hass = fx.hass()
    grouped = patterns_of(schedule, route_id, direction)
    with freeze_time(fx.instant_on("1970-01-01")) as clock:
        for pattern, trip_ids in sorted(grouped.items()):
            day = service_date(schedule, trip_ids)
            if day is None:
                check.note(False, "no service date for a pattern")
                continue
            clock.move_to(fx.instant_on(day))
            for o, d in sample_pairs(pattern):
                name_o = fx.stop_names[pattern[o]]
                name_d = fx.stop_names[pattern[d]]
                if name_o == name_d:
                    continue
                data = {
                    "schedule": schedule,
                    "gtfs_dir": ".", "file": "fixture",
                    "route_type": "2",
                    "origin": name_o, "destination": name_d,
                    "direction": 0, "route": "train",
                    "line": short_name,
                    "offset": 0, "include_tomorrow": False,
                }
                result = get_next_departure(hass, data)
                ok = (isinstance(result, dict) and result
                      and result.get("origin_stop_name") == name_o
                      and result.get("destination_stop_name") == name_d
                      and result.get("route_short_name") == short_name
                      and result["origin_stop_sequence"]
                      < result["destination_stop_time"]["Sequence"]
                      and result["arrival_time"] >= result["departure_time"])
                asked = asked_of([fx.stop_names[s] for s in pattern], o, d,
                                 short_name, None)
                got = got_of(result, by_name=True)
                check.note(ok, answered(asked, got), asked=asked, got=got)
