"""Build a stand-in datasource from a fixture zip.

The code under test reads its timetable through a schedule object, so a test
has to hand it one. Loading the fixture with pygtfs would work and would cost
seconds per run plus an ORM in the middle of a test; the tables are read
straight out of the zip instead, then reshaped the way pygtfs stores them:
times as datetimes on 1970-01-01, the day after for hours past 24 (that is how
a night departure survives sqlite's time()), dates in ISO form, every optional
column present, and a single agency given an id when the feed left it out.

What comes back is only what a schedule is used for here: an .engine. Nothing
else of pygtfs is imitated, so a query that needs more will fail loudly rather
than answer something made up.

    schedule = fixture_db.build("tests/fixtures/palmbus")
    with schedule.engine.connect() as conn:
        rows = conn.execute(text("SELECT ... FROM trips")).fetchall()
"""
from __future__ import annotations

import csv
import io
import os
import sqlite3
import tempfile
import zipfile
from types import SimpleNamespace

from sqlalchemy import create_engine

# columns that have to compare as numbers: a stop_sequence stored as text sorts
# 10 before 2, which quietly reverses half a journey
NUMERIC = {"stop_sequence", "direction_id", "location_type", "route_type",
           "exception_type", "pickup_type", "drop_off_type"}

# optional GTFS columns the component's SQL reads: pygtfs always creates
# them, a feed is free not to publish them
PYGTFS_COLUMNS = {
    "stop_times": ("drop_off_type", "pickup_type", "shape_dist_traveled",
                   "stop_headsign", "timepoint"),
    "stops": ("stop_timezone", "parent_station", "location_type"),
    "trips": ("direction_id", "trip_headsign", "trip_short_name"),
    "routes": ("route_short_name", "route_long_name"),
    "agency": ("agency_timezone",),
}


def build(fixtures):
    """A schedule-shaped object over the tables of fixtures/static.zip."""
    path = os.path.join(tempfile.mkdtemp(prefix="gtfs2-fixture-"), "fixture.sqlite")
    connection = sqlite3.connect(path)
    with zipfile.ZipFile(os.path.join(fixtures, "static.zip")) as archive:
        for name in archive.namelist():
            table = os.path.splitext(os.path.basename(name))[0]
            with archive.open(name) as handle:
                rows = list(csv.DictReader(
                    io.TextIOWrapper(handle, encoding="utf-8-sig", newline="")))
            if not rows:
                continue
            columns = list(rows[0].keys())
            ddl = ", ".join(
                f'"{c}" {"integer" if c in NUMERIC else "text"}' for c in columns)
            connection.execute(f'create table "{table}" ({ddl})')  # noqa: S608
            placeholders = ", ".join("?" * len(columns))
            connection.executemany(
                f'insert into "{table}" values ({placeholders})',  # noqa: S608
                [tuple(_value(c, row.get(c)) for c in columns) for row in rows])
    _pygtfsify(connection)
    connection.commit()
    connection.close()
    return SimpleNamespace(engine=create_engine("sqlite:///" + path.replace("\\", "/")))


def _pygtfsify(connection):
    """Rewrite raw GTFS values into what pygtfs stores.

    The zip keeps the bytes the provider published; the component's SQL is
    written against a pygtfs database, so the translation happens here.
    """
    for table, columns in PYGTFS_COLUMNS.items():
        present = {row[1] for row in connection.execute(
            f"PRAGMA table_info('{table}')")}  # noqa: S608
        for column in columns:
            if present and column not in present:
                connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}"')
    rows = connection.execute(
        "SELECT rowid, arrival_time, departure_time FROM stop_times").fetchall()
    connection.executemany(
        "UPDATE stop_times SET arrival_time = ?, departure_time = ? WHERE rowid = ?",
        [(_pygtfs_time(arrival), _pygtfs_time(departure), rowid)
         for rowid, arrival, departure in rows])
    connection.execute(
        "UPDATE calendar_dates SET date = substr(date, 1, 4) || '-' || "
        "substr(date, 5, 2) || '-' || substr(date, 7, 2) WHERE length(date) = 8")
    # a single-agency feed may omit agency_id everywhere (TAO does); the
    # component still inner-joins routes to agency, which pygtfs keeps
    # working, so the fixture db must too
    agencies = connection.execute("SELECT count(*) FROM agency").fetchone()[0]
    if agencies == 1:
        connection.execute(
            "UPDATE agency SET agency_id = '(single)' WHERE agency_id IS NULL")
        connection.execute(
            "UPDATE routes SET agency_id = (SELECT agency_id FROM agency) "
            "WHERE agency_id IS NULL")
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "calendar" not in tables:
        connection.execute(
            "CREATE TABLE calendar (service_id text, monday integer, "
            "tuesday integer, wednesday integer, thursday integer, "
            "friday integer, saturday integer, sunday integer, "
            "start_date text, end_date text)")
    else:
        for column in ("start_date", "end_date"):
            connection.execute(
                f"UPDATE calendar SET {column} = "  # noqa: S608
                f"substr({column}, 1, 4) || '-' || substr({column}, 5, 2)"
                f" || '-' || substr({column}, 7, 2) WHERE length({column}) = 8")


def _pygtfs_time(value):
    if value is None or " " in str(value):
        return value
    hours, rest = str(value).split(":", 1)
    hours = int(hours)
    day = 1 + hours // 24
    return f"1970-01-{day:02d} {hours % 24:02d}:{rest}"


def _value(column, raw):
    if raw in (None, ""):
        return None
    if column in NUMERIC:
        try:
            return int(raw)
        except ValueError:
            return None
    return raw
