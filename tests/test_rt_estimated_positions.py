"""rt_estimated_positions: positions estimated from TripUpdates only."""
import json
import types

import pytest
import ha_stub

mod = ha_stub.load("rt_estimated_positions")

sqlalchemy = pytest.importorskip("sqlalchemy")

T0 = 1_791_000_000  # arbitrary epoch


def stu(seq, stop_id, arr=0, dep=0):
    return {"stop_sequence": seq, "stop_id": stop_id,
            "arrival": {"time": arr, "delay": 0}, "departure": {"time": dep, "delay": 0}}


def ev(arr, dep, lat, lon):
    return {"stop_id": "x", "stop_sequence": 1, "arr": arr, "dep": dep, "lat": lat, "lon": lon}


# ---------------------------------------------------------------- pure logic

def test_route_matches_exact_and_delimiter():
    assert mod.route_matches("12", "12")
    assert not mod.route_matches("120", "12")
    assert mod.route_matches("12_A", "12", "_")
    assert not mod.route_matches("", "12")


def test_extract_events_fills_missing_side_and_skips_empty():
    events = mod.extract_events([
        stu(2, "B", arr=T0 + 60),
        stu(1, "A", dep=T0),
        stu(3, "C"),  # no time at all: skipped
    ])
    assert [e["stop_id"] for e in events] == ["A", "B"]
    assert events[0]["arr"] == events[0]["dep"] == T0
    assert events[1]["arr"] == events[1]["dep"] == T0 + 60


def test_estimate_between_stops_is_linear():
    events = [ev(T0, T0, 43.0, 7.0), ev(T0 + 100, T0 + 100, 44.0, 8.0)]
    lat, lon, state = mod.estimate_position(events, T0 + 25)
    assert state == mod.POSITION_BETWEEN_STOPS
    assert lat == pytest.approx(43.25) and lon == pytest.approx(7.25)


def test_estimate_dwell_at_stop():
    events = [ev(T0, T0, 43.0, 7.0), ev(T0 + 100, T0 + 160, 44.0, 8.0), ev(T0 + 300, T0 + 300, 45.0, 9.0)]
    assert mod.estimate_position(events, T0 + 130)[2] == mod.POSITION_AT_STOP
    assert mod.estimate_position(events, T0 + 130)[:2] == (44.0, 8.0)


def test_estimate_finished_and_not_started():
    events = [ev(T0, T0, 43.0, 7.0), ev(T0 + 100, T0 + 100, 44.0, 8.0)]
    assert mod.estimate_position(events, T0 + 100) is None           # finished
    assert mod.estimate_position(events, T0 - 3600) is None          # far future
    assert mod.estimate_position(events, T0 - 60)[2] == mod.POSITION_BEFORE_FIRST_LISTED


# ------------------------------------------------------- with a real sqlite

@pytest.fixture
def schedule(tmp_path):
    engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path / 'gtfs.sqlite'}")
    with engine.begin() as conn:
        t = sqlalchemy.text
        conn.execute(t("CREATE TABLE trips (trip_id TEXT, route_id TEXT, direction_id INTEGER)"))
        conn.execute(t("CREATE TABLE stop_times (trip_id TEXT, stop_sequence INTEGER, stop_id TEXT)"))
        conn.execute(t("CREATE TABLE stops (stop_id TEXT, stop_lat REAL, stop_lon REAL)"))
        conn.execute(t("INSERT INTO trips VALUES ('T1','R1',0),('T2','R1',1),('T3','R2',0)"))
        conn.execute(t("INSERT INTO stop_times VALUES ('T1',1,'A'),('T1',2,'B')"))
        conn.execute(t("INSERT INTO stops VALUES ('A',43.0,7.0),('B',44.0,8.0)"))
    return types.SimpleNamespace(engine=engine)


def make_self(tmp_path, schedule, feed):
    hass = types.SimpleNamespace(config=types.SimpleNamespace(path=lambda p: str(tmp_path / p)))
    return types.SimpleNamespace(
        hass=hass, _route_id="R1", _direction="0", _route_delimiter=None,
        _icon="mdi:bus", _feed_entities=feed,
        _data={"schedule": schedule, "next_departure": {}},
        config_entry=types.SimpleNamespace(data={}),
    )


def trip(trip_id, stus, route_id="", direction_id=None):
    trip_desc = {"trip_id": trip_id, "route_id": route_id}
    if direction_id is not None:
        trip_desc["direction_id"] = direction_id
    return {"id": trip_id, "trip_update": {"trip": trip_desc, "stop_time_update": stus}}


def test_selects_route_and_direction_from_static_when_feed_lacks_them(tmp_path, schedule):
    stus = [stu(1, "A", dep=T0), stu(2, "B", arr=T0 + 100)]
    feed = [trip("T1", stus), trip("T2", stus), trip("T3", stus)]
    self = make_self(tmp_path, schedule, feed)
    features = mod.build_estimated_features(self, feed, schedule, T0 + 50)
    assert [f["properties"]["trip_id"] for f in features] == ["T1"]
    assert features[0]["geometry"]["coordinates"] == pytest.approx([7.5, 43.5])


def test_stop_id_filled_from_sequence(tmp_path, schedule):
    stus = [stu(1, "", dep=T0), stu(2, "", arr=T0 + 100)]
    feed = [trip("T1", stus, route_id="R1", direction_id="0")]
    self = make_self(tmp_path, schedule, feed)
    features = mod.build_estimated_features(self, feed, schedule, T0 + 50)
    assert len(features) == 1


def test_writes_file_also_when_empty(tmp_path, schedule):
    self = make_self(tmp_path, schedule, [])
    ha_stub.freeze(T0)
    try:
        mod.create_estimated_vehicle_geojson(self)
    finally:
        ha_stub.freeze(None)
    written = json.loads((tmp_path / "www/gtfs2/r1_0_estimated.json").read_text())
    assert written == {"type": "FeatureCollection", "features": []}
