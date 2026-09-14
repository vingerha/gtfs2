"""Every departure/arrival pair a fixture's routes offer must hold up.

For each route named in a fixture's manifest, the trips of each direction
it runs are sampled, and the promises are checked against the real code, on
a db built from the fixture zip, with the clock pinned to a day the trips
actually run. The list and the queries are asked the way the flow asks them:
one list per line, both ways round, a place per entry, and no direction but
the rotation get_pair_direction keeps at a loop's terminus.

    stop_list   each place once (the records of a parent station, or of one
                name close together), every stop a trip calls at stood for,
                and every trip riding the list one way or the other
    destinations  from an origin, get_destination_stop_list offers every
                place a trip rides to after it, once, in the order every
                ride makes, one branch at a time (busiest first) where the
                rides leave it open, and
                nothing no trip through that origin reaches
    towards     from an origin, a way is asked exactly when its trips go
                to different termini (the next stop telling them apart at a
                loop's terminus, or when the terminus is the origin);
                each way's destinations hold every place its rides reach and
                nothing else, in their order, and the two ways together are
                the whole destination list
    pairs       origin before destination on some trip: get_next_departure
                answers it, on the right places, in riding order, arriving no
                earlier than it departs, on the shortest ride of its trip,
                each departure leaving from a record of the asked place
    swapped     destination before origin: nothing, or a ride the line really
                makes, either way round

Trains (route_type 2) ride their own path in get_next_departure, matched by
stop name with no direction: for them the pairs are checked by name, the
answer must stay on the asked line, and a swapped pair is legitimate, it is
the return journey, so only `pairs` is checked, by name.

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
get_destination_stop_list = gtfs_helper.get_destination_stop_list

FIXTURES = Path(__file__).parent / "fixtures"
KINDS = ("stop_list", "destinations", "towards", "pairs", "swapped")
TRAIN_KINDS = ("pairs",)


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
        self._places = {}

    def hass(self):
        """What get_next_departure reads off hass, and nothing more."""
        return types.SimpleNamespace(config=types.SimpleNamespace(
            path=lambda *parts: str(self.path.joinpath(*parts)),
            time_zone=self.agency_tz))

    def instant_on(self, date_iso: str,
                   at: datetime.time = datetime.time(0, 5)) -> datetime.datetime:
        """A wall time of the agency's zone on that day, 00:05 by default."""
        return datetime.datetime.combine(
            datetime.date.fromisoformat(date_iso), at,
            zoneinfo.ZoneInfo(self.agency_tz))

    def station_of(self, stop_id):
        """The parent station the feed declares for a stop, if any."""
        return self.stations.get(stop_id) or None

    def siblings_of(self, stop_id):
        """Every record of the place this one belongs to.

        A place is what a rider waits at: the records a feed groups under a
        parent station, and without a parent, the records of one name close
        together. The journey's ends are matched on whole places, so
        get_next_departure may answer on any of their records: a pair is
        right when it lands on one of these. The checkout's own rule is read
        (_place_group), a parent-only reading when it has none.
        """
        if stop_id in self._places:
            return self._places[stop_id]
        group = getattr(gtfs_helper, "_place_group", None)
        if group:
            with self.schedule.engine.connect() as conn:
                found = {stop_id} | {row[0] for row in conn.execute(
                    text("SELECT stop_id FROM stops WHERE stop_id IN " + group("s")),
                    {"s": stop_id})}
        else:
            parent = self.station_of(stop_id)
            found = {stop_id} | ({s for s, p in self.stations.items() if p == parent}
                                 if parent else set())
        self._places[stop_id] = found
        return found

    def box_distance(self, a, b):
        """How far apart two records are, in the checkout's place boxes (0
        when it has none): of two places claiming a record, the nearer one
        has it."""
        lat_box = getattr(gtfs_helper, "PLACE_LAT", None)
        lon_box = getattr(gtfs_helper, "PLACE_LON", None)
        if not lat_box or not lon_box:
            return 0
        if not hasattr(self, "_where"):
            with self.schedule.engine.connect() as conn:
                self._where = {row[0]: (row[1], row[2]) for row in conn.execute(
                    text("SELECT stop_id, stop_lat, stop_lon FROM stops"))}
        try:
            (a_lat, a_lon), (b_lat, b_lon) = self._where[a], self._where[b]
            return max(abs(float(a_lat) - float(b_lat)) / lat_box,
                       abs(float(a_lon) - float(b_lon)) / lon_box)
        except (KeyError, TypeError, ValueError):
            return 0


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
    """The first day one of these trips runs, as an ISO date, or None: the
    earliest calendar_dates addition, or the first weekday of a calendar
    window its removals leave, over the trips' services."""
    with schedule.engine.connect() as conn:
        services = {row[0] for row in conn.execute(text(
            "SELECT DISTINCT service_id FROM trips WHERE trip_id IN :trips"
        ).bindparams(bindparam("trips", expanding=True)),
            {"trips": list(trip_ids)})}
        exceptions = conn.execute(text(
            "SELECT service_id, date, exception_type FROM calendar_dates")).fetchall()
        days = {str(d)[:10] for s, d, k in exceptions if k == 1 and s in services}
        removed = {(s, str(d)[:10]) for s, d, k in exceptions if k == 2}
        for row in conn.execute(text(
                "SELECT service_id, monday, tuesday, wednesday, thursday, "
                "friday, saturday, sunday, start_date, end_date FROM calendar")):
            if row[0] not in services or not row[8] or not row[9]:
                continue
            day = datetime.date.fromisoformat(str(row[8])[:10])
            end = datetime.date.fromisoformat(str(row[9])[:10])
            while day <= end:
                if row[1 + day.weekday()] and (row[0], day.isoformat()) not in removed:
                    days.add(day.isoformat())
                    break
                day += datetime.timedelta(days=1)
    return min(days) if days else None


def served_between(patterns, origins, destinations):
    """Some trip of the line really rides from one set to the other."""
    for pattern in patterns:
        before = [i for i, stop in enumerate(pattern) if stop in origins]
        after = [i for i, stop in enumerate(pattern) if stop in destinations]
        if before and after and min(before) < max(after):
            return True
    return False


def pieces_of(places):
    """A ride read as list positions, cut where it comes back to a position
    it already passed (a racket, a loop), repeats in a row dropped."""
    pieces, current = [], []
    for p in places:
        if current and p == current[-1]:
            continue
        if p in current:
            pieces.append(current)
            current = [current[-1], p]
        else:
            current.append(p)
    if len(current) > 1:
        pieces.append(current)
    return pieces


def rides_in_order(piece, size, ends=(), ways=(True, False)):
    """One way along the list, forward or backward.

    A loop has no straight order: its seam, a step across more than half the
    list, is allowed once. Nor has a terminus the two ways share: TAO 40 ends
    Montesquieu, Cheques Postaux quai D, then quai C, and GVB 14 starts in the
    turning loop at Flevopark, so one step against the way is allowed when it
    touches the trip's own first or last stop (ends). A step against the way
    in the middle of a ride still fails.
    """
    for forward in ways:
        wraps = shuffles = 0
        for a, b in zip(piece, piece[1:]):
            step = b - a
            if abs(step) > size / 2:
                wraps += 1
            elif (step > 0) != forward:
                if a in ends or b in ends:
                    shuffles += 1
                else:
                    break
        else:
            if wraps <= 1 and shuffles <= 1:
                return True
    return False


def line_patterns(schedule, route_id):
    """{stop pattern: [trip_id]} for the whole line, both ways round."""
    patterns = {}
    for direction in directions_of(schedule, route_id):
        for pattern, trip_ids in patterns_of(schedule, route_id, direction).items():
            patterns.setdefault(pattern, []).extend(trip_ids)
    return patterns


def rode_past_an_end(schedule, result, origins, destinations):
    """The stops the answer's trip calls at between its two ends that are one
    of those ends again: a shorter ride was on the same trip."""
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT stop_id FROM stop_times WHERE trip_id = :t "
            "AND stop_sequence > :o AND stop_sequence < :d"),
            {"t": result.get("trip_id"), "o": result["origin_stop_sequence"],
             "d": result["destination_stop_time"]["Sequence"]}).fetchall()
    return [r[0] for r in rows if r[0] in origins or r[0] in destinations]


def sample_origins(pattern):
    """The first stop, one in the middle, and the one before last."""
    picks = sorted({0, len(pattern) // 2, max(0, len(pattern) - 2)})
    return [i for i in picks if i < len(pattern) - 1]


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
TRAIN = ("a train journey is matched by stop name prefix on any line, not "
         "the asked one")
KNOWN = {
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
                kinds = TRAIN_KINDS if train else KINDS
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
        check_train_route(check, fx, route_id, direction, kind)
    else:
        check_route(check, fx, route_id, direction, kind)
    record_property("case", {"fixture": fixture, "route": route_id,
                             "direction": direction, "kind": kind})
    record_property("checks", check.records)
    assert not check.failures, "\n".join(check.failures)


def check_route(check, fx, route_id, direction, kind):
    """The flow's promises on one line, the trips of one direction sampled.

    The rider is offered one list per line, a place per entry, and picks
    where they are, then where they go; no direction is asked, the pair and
    the order of the stops on a trip say which way it is, and a loop's
    terminus keeps the rotation get_pair_direction settles. So the list, the
    destinations and the departures are asked the way the flow asks them,
    with no direction; `direction` only says which trips are sampled.
    """
    schedule = fx.schedule
    everything = line_patterns(schedule, route_id)
    grouped = patterns_of(schedule, route_id, direction)
    entries = get_stop_list(schedule, route_id, None)
    ids = [entry.split(": ", 1)[0] for entry in entries]
    # the entry that stands for each record: the one of its place, the
    # nearer one when two places reach it (TAO N's Liberation-Interives),
    # whichever end the list starts from
    claims = {}
    for n, stop_id in enumerate(ids):
        for member in fx.siblings_of(stop_id):
            claims.setdefault(member, []).append(n)
    entry_of = {member: min(claimants, key=lambda n: (fx.box_distance(ids[n], member), n))
                for member, claimants in claims.items()}

    if kind == "stop_list":
        # The entries read "STOP: Name (12)", the number being the
        # stop_sequence the selector showed; a stop offered twice is one
        # stop_id under two of those numbers, which is what a reader has to
        # be told to find it again in the feed.
        offered_at = {}
        for entry, stop_id in zip(entries, ids):
            place = entry.rsplit(" (", 1)[-1].rstrip(")") if " (" in entry else "?"
            offered_at.setdefault(stop_id, []).append(place)
        repeated = {stop_id: places for stop_id, places in offered_at.items()
                    if len(places) > 1}
        text = "the stop list offers a stop twice"
        if repeated:
            text += ": " + listed([f"{named(fx, stop_id)} at "
                                   + " and ".join(places)
                                   for stop_id, places in repeated.items()])
        check.note(not repeated, text,
                   repeated={stop_id: places
                             for stop_id, places in repeated.items()})
        # A place is one entry, whatever its records: TAO line A offered
        # Jules Verne twice and each choice hid half the trams, Zou 653
        # offered each pole of Pont de la Brague, one per side of the road.
        twice = [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]
                 if b in fx.siblings_of(a)]
        text = "the list offers one place twice"
        if twice:
            text += ": " + listed([f"{named(fx, a)} and {named(fx, b)}" for a, b in twice])
        # folded keeps what the check recorded when it only looked at two
        # platforms of one station listed one after the other
        folded = [(a, b) for a, b in zip(ids, ids[1:])
                  if fx.station_of(a) and fx.station_of(a) == fx.station_of(b)]
        check.note(not twice, text, twice=[list(pair) for pair in twice],
                   folded=[list(pair) for pair in folded])
        for pattern in grouped:
            unoffered = [stop for stop in pattern if stop not in entry_of]
            text = "a trip serves a stop the list does not offer"
            if unoffered:
                text += (": " + listed([named(fx, stop) for stop in unoffered])
                         + f" (on the ride {pattern[0]} .. {pattern[-1]})")
            check.note(not unoffered, text, unoffered=list(unoffered))
            known = [entry_of[stop] for stop in pattern if stop in entry_of]
            ends = (known[0], known[-1]) if known else ()
            ordered = all(rides_in_order(piece, len(ids), ends) for piece in pieces_of(known))
            check.note(ordered, "the list contradicts the riding order "
                                f"{pattern[0]} .. {pattern[-1]}")
        if direction == 0:
            # Which end comes first: the way most trips labelled 0 ride the
            # list, each trip counted, its steps along the list counted
            # (text is a message in this function, the query needs the sqlalchemy one)
            from sqlalchemy.sql import text as sql_text
            forward = backward = 0
            for pattern, trip_ids in grouped.items():
                with schedule.engine.connect() as conn:
                    labelled = conn.execute(sql_text(
                        "SELECT COUNT(*) FROM trips WHERE direction_id = 0 AND trip_id IN :t"
                    ).bindparams(bindparam("t", expanding=True)), {"t": list(trip_ids)}).scalar()
                known = [entry_of[stop] for stop in pattern if stop in entry_of]
                forward += labelled * sum(1 for a, b in zip(known, known[1:]) if b > a)
                backward += labelled * sum(1 for a, b in zip(known, known[1:]) if b < a)
            check.note(forward >= backward,
                       f"the list starts at {named(fx, ids[0])}: direction 0 trips take "
                       f"{forward} steps along it and {backward} against it",
                       forward=forward, backward=backward)
        return

    route_type = str(fx.route_types.get(route_id))
    if kind == "destinations":
        # From an origin, the trips that call at it and the rest of their
        # ride: the list must hold every place such a trip reaches, once, in
        # the order the ride makes, and nothing no trip through that origin
        # reaches, whichever way round the line it goes.
        for pattern in grouped:
            for o in sample_origins(pattern):
                if pattern[o] not in entry_of:
                    continue
                origin = ids[entry_of[pattern[o]]]
                offered = [entry.split(": ", 1)[0] for entry in
                           get_destination_stop_list(schedule, route_id, None, origin)]
                at = {stop_id: n for n, stop_id in enumerate(offered)}
                who = f"from {named(fx, origin)}"
                twice = sorted({s for s in offered if offered.count(s) > 1})
                text = f"a destination is offered twice {who}"
                if twice:
                    text += ": " + listed([named(fx, s) for s in twice])
                check.note(not twice, text, origin=origin, twice=twice)
                after = [stop for stop in pattern[o + 1:]
                         if entry_of.get(stop) != entry_of[origin]]
                missing = [s for s in dict.fromkeys(after)
                           if s not in entry_of or ids[entry_of[s]] not in at]
                text = f"a stop this ride reaches {who} is not offered"
                if missing:
                    text += (": " + listed([named(fx, s) for s in missing])
                             + f" (on the ride {pattern[0]} .. {pattern[-1]})")
                check.note(not missing, text, origin=origin, missing=missing)
                reachable = set()
                for other in everything:
                    hits = [i for i, s in enumerate(other)
                            if entry_of.get(s) == entry_of[origin]]
                    if hits:
                        reachable.update(ids[entry_of[s]] for s in other[hits[0] + 1:]
                                         if s in entry_of)
                stray = [s for s in offered if s not in reachable]
                text = f"a destination no trip reaches {who} is offered"
                if stray:
                    text += ": " + listed([named(fx, s) for s in stray])
                check.note(not stray, text, origin=origin, stray=stray)
                # Riding order across every trip of the line, one branch at a
                # time where the rides leave it open. Read from all the
                # line's trips: a place follows the places any of them calls
                # at just before it on its way from the origin (counted again
                # from a later call at the origin, a place met again on a
                # ride starting a new stretch). Among the places free to come
                # next, one that follows the place just listed goes on with
                # the branch in progress; otherwise, and between several, the
                # busiest by the trips of the rides reaching it, then the
                # nearest by the fewest stops, then the list's order. When
                # none is free (a loop's terminus, reached both ways round),
                # the same among the places left.
                fewest, before, trips_at = {}, {}, {}
                for other in everything:
                    count, previous, stretch, ride = None, None, set(), set()
                    for s in list(other) + [None]:
                        if s is not None and s not in entry_of:
                            continue
                        if s is None or entry_of[s] == entry_of[origin]:
                            for e in ride:
                                trips_at[e] = trips_at.get(e, 0) + len(everything[other])
                            if s is None:
                                break
                            count, previous, stretch, ride = 0, None, set(), set()
                            continue
                        e = ids[entry_of[s]]
                        if count is None:
                            continue
                        count += 1
                        ride.add(e)
                        fewest[e] = min(fewest.get(e, count), count)
                        before.setdefault(e, set())
                        if e in stretch:
                            stretch = {e}
                        elif previous is not None and previous != e:
                            before[e].add(previous)
                            stretch.add(e)
                        else:
                            stretch.add(e)
                        previous = e

                def busiest(e):
                    return (-trips_at.get(e, 0), fewest.get(e, 0), ids.index(e))

                listed_before, ordered, first_break, last = set(), True, None, None
                for e in offered:
                    left = [x for x in offered if x not in listed_before]
                    pool = [x for x in left if not (before.get(x, set()) - listed_before)] or left
                    going_on = [x for x in pool if last in before.get(x, set())]
                    expected = min(going_on or pool, key=busiest)
                    if e != expected and ordered:
                        ordered, first_break = False, [e, expected]
                    listed_before.add(e)
                    last = e
                check.note(ordered, f"the destinations {who} are not in riding order, "
                           f"one branch at a time where the rides leave it open"
                           + (f" ({named(fx, first_break[0])} before {named(fx, first_break[1])})"
                              if first_break else ""),
                           origin=origin,
                           order=[[s, trips_at.get(s, 0), fewest.get(s, 0)] for s in offered])
                # And along this ride, the order it makes: counted again from
                # a later call at the origin, one reshuffle allowed where it
                # touches the ride's own last stop (a terminus's quays). From
                # a loop's terminus every stop is reached both ways round, so
                # there nearest first is the order and this one is recorded
                # as not applying.
                terminus = any(entry_of.get(other[0]) == entry_of[origin]
                               and entry_of.get(other[-1]) == entry_of[origin]
                               for other in everything)
                along = True
                ride = []
                for stop in pattern[o + 1:] + (None,):
                    if stop is None or entry_of.get(stop) == entry_of[origin]:
                        known = [at[ids[entry_of[s]]] for s in ride
                                 if s in entry_of and ids[entry_of[s]] in at]
                        ends = (known[-1],) if known else ()
                        along = along and all(
                            rides_in_order(piece, len(offered) * 4, ends, ways=(True,))
                            for piece in pieces_of(known))
                        ride = []
                    else:
                        ride.append(stop)
                check.note(along or terminus,
                           f"the destinations {who} contradict the riding order "
                           f"{pattern[0]} .. {pattern[-1]}"
                           + (" (a loop's terminus: nearest first applies)" if terminus else ""),
                           origin=origin, loop_terminus=terminus, along=along)
        return

    if kind == "towards":
        seen = set()
        for pattern in grouped:
            for o in sample_origins(pattern):
                if pattern[o] not in entry_of or entry_of[pattern[o]] in seen:
                    continue
                home = entry_of[pattern[o]]
                seen.add(home)
                check_towards(check, fx, route_id, everything, ids, entry_of, home)
        return

    hass = fx.hass()
    with freeze_time(fx.instant_on("1970-01-01")) as clock:
        for pattern, trip_ids in sorted(grouped.items()):
            if any(stop not in entry_of for stop in pattern):
                check.note(False, "a pattern stop has no entry")
                continue
            day = service_date(schedule, trip_ids)
            if day is None:
                check.note(False, "no service date for a pattern")
                continue
            clock.move_to(fx.instant_on(day))
            for o, d in sample_pairs(pattern):
                origin, destination = ids[entry_of[pattern[o]]], ids[entry_of[pattern[d]]]
                if origin == destination:
                    # a racket or a loop from its terminus round to it: two
                    # records of one place, which the list offers once, so no
                    # entry can ask it; recorded, not asked
                    asked = asked_of(pattern, o, d, route_id, None)
                    check.note(True, f"asked {pattern[o]} -> {pattern[d]} on {route_id}: "
                               f"one place ({named(fx, origin)}), not a journey the list offers",
                               asked=asked, got=None, same_place=True)
                    continue
                kept = gtfs_helper.get_pair_direction(schedule, route_id, origin, destination)
                data = _data_for(schedule, route_id, route_type, entries,
                                 entry_of, pattern[o], pattern[d], kept)
                origins, reached = fx.siblings_of(origin), fx.siblings_of(destination)
                if kind == "pairs":
                    result = get_next_departure(hass, data)
                    ok = (isinstance(result, dict) and result
                          and result.get("origin_stop_id") in origins
                          and result.get("destination_stop_id") in reached
                          and result["origin_stop_sequence"]
                          < result["destination_stop_time"]["Sequence"]
                          and result["arrival_time"] >= result["departure_time"])
                    asked = asked_of(pattern, o, d, route_id, kept)
                    got = got_of(result)
                    check.note(ok, answered(asked, got), asked=asked, got=got)
                    if result:
                        # each departure names the record it leaves from, a
                        # record of the asked place
                        leaving = result.get("next_departures_origin_stop_id") or []
                        elsewhere = [s for s in leaving if s not in origins]
                        check.note(bool(leaving) and not elsewhere,
                                   f"asked {origin} -> {destination} on {route_id}: "
                                   f"{len(leaving)} departures leave from "
                                   f"{sorted(set(leaving))}"
                                   + (f", not the asked place: {sorted(set(elsewhere))}" if elsewhere else ""),
                                   asked=asked, got=got, leaving=sorted(set(leaving)))
                        # and it rides the direction the entry keeps, when it
                        # keeps one (a loop's terminus); otherwise the pair
                        # alone decides, and the direction ridden is recorded
                        rode = str(result.get("trip_direction_id"))
                        if kept is not None:
                            check.note(rode == str(kept),
                                       f"asked d{kept} on {route_id}: "
                                       f"trip {got['trip']} rides d{rode}",
                                       asked=asked, got=got)
                        else:
                            check.note(True,
                                       f"asked no direction on {route_id}: "
                                       f"trip {got['trip']} rides d{rode}",
                                       asked=asked, got=got)
                    if ok:
                        # and it is the shortest ride on its trip: a trip
                        # passing an end twice does not board the rider on
                        # the pole across the road for the long way round
                        past = rode_past_an_end(schedule, result, origins, reached)
                        check.note(not past,
                                   f"asked {origin} -> {destination} on {route_id}: "
                                   f"trip {got['trip']} calls at an end again on the way"
                                   + (f" ({listed(past)})" if past else ""),
                                   asked=asked, got=got)
                else:
                    swapped = dict(data, origin=data["destination"],
                                   destination=data["origin"],
                                   direction=str(gtfs_helper.get_pair_direction(
                                       schedule, route_id, destination, origin)))
                    result = get_next_departure(hass, swapped)
                    # Both ways round are offered, so the reverse pair is a
                    # journey whenever some trip of the line rides it. The
                    # promise is that an answer, when there is one, matches a
                    # ride the line actually makes; answering nothing stays
                    # acceptable, the pattern that rides it may not run on
                    # the frozen day.
                    served = served_between(everything, reached, origins)
                    honest = not result or (
                        served
                        and result["origin_stop_sequence"]
                        < result["destination_stop_time"]["Sequence"]
                        and result["arrival_time"] >= result["departure_time"])
                    asked = asked_of(pattern, d, o, route_id, None, served=served)
                    got = got_of(result)
                    check.note(honest, answered(asked, got), asked=asked, got=got)


def check_towards(check, fx, route_id, everything, ids, entry_of, home):
    """The way question from one origin, read from the line's trips.

    A ride runs from a call at the origin to the trip's next call at it, or
    its end. A way is the terminus of the ride's trip, as the bus shows it;
    the next stop comes with it only when that terminus is a loop's (both
    rotations end there) or the origin itself. A trip ending short of a
    terminus goes the way of the trips that leave for the same next stop and
    pass its end, or every stop of its ride but the end (a pole of its own).
    The question is asked when there are two ways or more,
    and each answer's destinations are exactly the places its rides reach.
    """
    schedule = fx.schedule
    origin = ids[home]
    who = f"from {named(fx, origin)}"
    line = [[entry_of[s] for s in pattern if s in entry_of] for pattern in everything]
    loop_termini = {k[0] for k in line if k and k[0] == k[-1]}
    terminus = home in loop_termini
    rides = {}
    for known, pattern in zip(line, everything):
        ride = None
        for n in known + [home]:
            if n == home:
                if ride:
                    end = known[-1]
                    key = (end, ride[0] if end in loop_termini or end == home else None)
                    rides.setdefault(key, []).append((ride, pattern))
                ride = []
            elif ride is not None:
                ride.append(n)
    folded = {}
    for key, calls in rides.items():
        if key[1] is not None:
            continue
        for other, other_calls in rides.items():
            if other != key and other[1] is None and any(
                    ride[0] == mine[0]
                    and (key[0] in [p for p in ride if p != ride[-1]]
                         or {p for p in mine if p != mine[-1]} <= set(ride))
                    for ride, _pattern in other_calls for mine, _mine_pattern in calls):
                folded[key] = other
                break
    ways_seen = {}
    for key, calls in rides.items():
        chain = []
        while key in folded and key not in chain:
            chain.append(key)
            key = folded[key]
        if key in chain:
            # two poles of one terminus fold into each other: the code keeps
            # the smallest stop_id, the entries are compared on ids
            cycle = chain[chain.index(key):]
            key = min(cycle, key=lambda k: tuple(ids[n] if n is not None else "" for n in k))
        ways_seen.setdefault("|".join(ids[n] for n in key if n is not None), []).extend(calls)
    expected = len(ways_seen) >= 2
    ways = gtfs_helper.get_towards(schedule, route_id, origin)
    check.note(bool(ways) == expected and (not ways or len(ways) == len(ways_seen)),
               f"{len(ways)} ways asked {who}, the trips go {len(ways_seen)} ways"
               + (" (a loop's terminus)" if terminus else ""),
               origin=origin, asked=[list(w) for w in ways], ways=len(ways_seen))
    if not ways:
        return
    labels = [label for _way, label in ways]
    check.note(len(set(labels)) == len(labels), f"the ways {who} read {labels}",
               origin=origin, labels=labels)
    whole = [e.split(": ", 1)[0] for e in
             get_destination_stop_list(schedule, route_id, None, origin)]
    sides = {}
    for way, label in ways:
        mine = ways_seen.get(way, [])
        offered = [e.split(": ", 1)[0] for e in
                   get_destination_stop_list(schedule, route_id, None, origin, way)]
        sides[way] = offered
        reached = list(dict.fromkeys(ids[n] for ride, _pattern in mine for n in ride))
        missing = [s for s in reached if s not in offered]
        stray = [s for s in offered if s not in reached]
        check.note(bool(mine) and not missing and not stray,
                   f"towards {label} {who}: {len(offered)} destinations"
                   + (f", missing {listed([named(fx, s) for s in missing])}" if missing else "")
                   + (f", no ride that way reaches {listed([named(fx, s) for s in stray])}" if stray else ""),
                   origin=origin, way=way, offered=offered, missing=missing, stray=stray)
        # the order a ride first meets its places: coming back past them
        # (Palm Bus 21 out round its loop and back down the same street)
        # meets places already listed
        at = {s: i for i, s in enumerate(offered)}
        along = True
        for ride, _pattern in mine:
            met = list(dict.fromkeys(at[ids[n]] for n in ride if ids[n] in at))
            along = along and rides_in_order(met, len(offered) * 4, met[-1:], ways=(True,))
        check.note(along, f"towards {label} {who}: the destinations "
                          f"follow every ride that way", origin=origin, way=way)
        if terminus and offered:
            # at a loop's terminus the answer is the rotation: the entry
            # keeps a label the trips riding that way carry
            far = offered[-1]
            kept = gtfs_helper.get_pair_direction(schedule, route_id, origin, far, way)
            trip_ids = [trip_id for ride, pattern in mine if ids.index(far) in ride
                        for trip_id in everything[pattern]]
            with schedule.engine.connect() as conn:
                carried = {str(row[0]) for row in conn.execute(text(
                    "SELECT DISTINCT direction_id FROM trips WHERE trip_id IN :t"
                ).bindparams(bindparam("t", expanding=True)), {"t": trip_ids or [""]})}
            check.note(kept is not None and str(kept) in carried,
                       f"towards {label} {who} to {named(fx, far)}: "
                       f"the entry keeps direction {kept}, trips that way carry {sorted(carried)}",
                       origin=origin, way=way, kept=kept, carried=sorted(carried))
    lost = [s for s in whole if not any(s in side for side in sides.values())]
    check.note(not lost and set(whole) == set().union(*map(set, sides.values())),
               f"the ways {who} make the whole destination list"
               + (f", lost {listed([named(fx, s) for s in lost])}" if lost else ""),
               origin=origin, lost=lost)


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


def named(fx, stop_id):
    """A stop as a reader can look it up: its id and the name it carries."""
    name = fx.stop_names.get(stop_id)
    return f"{stop_id} {name}" if name else stop_id


def listed(items, limit=3):
    """The first few of a list, then how many were left out."""
    shown = ", ".join(items[:limit])
    rest = len(items) - limit
    return f"{shown}, and {rest} more" if rest > 0 else shown


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
        "loop_direction": direction,
        "route": route_id,
        "offset": 0,
        "include_tomorrow": False,
    }


def check_train_route(check, fx, route_id, direction, kind):
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
