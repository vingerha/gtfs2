"""Generic runner for every captured coordinator case.

Discovers every `case_N` directory under `fixtures/case_route_combined/`
and, for each one, runs the real, unmodified
`GTFSUpdateCoordinator._async_update_data()` and checks its real return
value (`coordinator.data`) against the case's captured output.

Nothing about the coordinator's own logic is reimplemented here. Only
the functions it calls that would otherwise touch a real database or
make a live network call are replaced -- and only the direct return
value each one produces, nothing about how it's used afterward:

    get_gtfs                 -- opens a real GTFS sqlite file
    get_next_departure       -- queries that database; replaced with
                                 the real, unmodified
                                 `_interpret_departure_rows()`'s own
                                 output, computed from this case's rows
    check_datasource_index   -- runs SQL against the database; its
                                 return value isn't even used afterward
                                 in the real code
    update_route_geojson     -- writes a file to disk using real DB
                                 stop data; doesn't touch coordinator
                                 data at all
    get_rt_alerts            -- live network fetch; out of scope here,
                                 returns {} (matches a real capture
                                 with no active alerts)
    get_gtfs_feed_entities   -- the actual requests.get() call inside
                                 get_rt_route_trip_statuses; replaced
                                 with this case's feed_entities

`get_next_services`, `get_rt_route_trip_statuses`, and every line of
`coordinator.py` itself -- including the exact attributes it sets on
itself before calling those two, and the `gtfs_rt_updated_at` line
after -- run for real, unmodified.

Adding a new case is: capture three input files into a new `case_N`
folder, plus the coordinator.data output. Nothing here needs to change.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import re
import zoneinfo
from pathlib import Path
from unittest.mock import patch

import pytest
from freezegun import freeze_time

import ha_stub

ha_stub.install()

import homeassistant.util.dt as dt_util  # noqa: E402

# Loaded on their own rather than through the package, whose __init__
# pulls in the platforms and with them the rest of Home Assistant.
gtfs_helper = ha_stub.load("gtfs_helper")
coordinator_mod = ha_stub.load("coordinator")
# coordinator.py's own `from .gtfs_rt_helper import ...` resolves this
# as a real submodule of the same synthetic package -- already loaded
# as a side effect of loading coordinator_mod, found here to patch the
# one function inside it that touches the network.
import sys  # noqa: E402
gtfs_rt_helper_mod = sys.modules["gtfs2_under_test.gtfs_rt_helper"]

_interpret_departure_rows = gtfs_helper._interpret_departure_rows
TIME_STR_FORMAT = gtfs_helper.TIME_STR_FORMAT

CASE_ROOT = Path(__file__).parent / "case_route_combined"

# Fixed integration config -- not per-case diffable data, so not stored
# as its own case file. Same choice as TIMEZONE in test_static_cases.py.
TIMEZONE = "Europe/Paris"

_EVAL_GLOBALS = {"datetime": datetime, "zoneinfo": zoneinfo}


class _FakeConfig:
    def __init__(self, time_zone: str) -> None:
        self.time_zone = time_zone

    def path(self, value: str = "") -> str:
        return value


class _FakeHass:
    """Stand-in for `homeassistant.core.HomeAssistant`.

    `.async_add_executor_job` is exercised for real by
    `_async_update_data()` -- every call it makes goes through here,
    running the target function synchronously since there's no real
    executor thread pool needed for a single test invocation.
    """

    def __init__(self, time_zone: str) -> None:
        self.config = _FakeConfig(time_zone)

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


class _FakeConfigEntry:
    """Stand-in for `homeassistant.config_entries.ConfigEntry`.

    `.data` and `.options` are read directly by `_async_update_data()`
    -- built here from constants plus this case's own rows, rather
    than a separate captured file, since every value describes the
    same route/stop the rows already describe.
    """

    def __init__(self, rows: list[dict]) -> None:
        origin_row = rows[0]
        self.entry_id = "test_entry"
        self.data = {
            "origin": f"{origin_row['origin_stop_id']}: {origin_row['origin_stop_name']} ({origin_row['origin_stop_sequence']})",
            "destination": f"{origin_row['dest_stop_id']}: {origin_row['dest_stop_name']} ({origin_row['dest_stop_sequence']})",
            "include_tomorrow": True,
            "name": "route_name",
            "file": "zou_proximite",
            "route_type": "3",
            "route": origin_row["route_id"],
            "direction": str(origin_row["direction_id"]),
        }
        self.options = {
            "offset": 0,
            "real_time": True,
            "trip_update_url": None,
            "vehicle_position_url": None,
            "alerts_url": None,
        }


_CASE_NUM_RE = re.compile(r"case_(\d+)")


def _discover_cases(case_root: Path) -> list[tuple[str, Path]]:
    if not case_root.is_dir():
        return []

    cases: dict[str, Path] = {}
    for path in case_root.iterdir():
        if path.is_dir() and path.name.startswith("case_"):
            match = _CASE_NUM_RE.match(path.name)
            if match:
                cases.setdefault(f"case_{match.group(1)}", path)
        elif path.is_file():
            match = _CASE_NUM_RE.match(path.name)
            if match:
                cases.setdefault(f"case_{match.group(1)}", case_root)

    def _case_number(case_id: str) -> float:
        try:
            return int(case_id.split("_")[1])
        except (IndexError, ValueError):
            return float("inf")

    return sorted(cases.items(), key=lambda item: _case_number(item[0]))


def _find_case_file(case_dir: Path, case_id: str, suffix: str) -> Path:
    case_num = case_id.split("_", 1)[1]
    prefix = f"case_{case_num}"
    matches = []
    for path in case_dir.iterdir():
        name = path.name
        if not name.startswith(prefix) or not name.endswith(suffix):
            continue
        next_char = name[len(prefix):len(prefix) + 1]
        if next_char.isdigit():
            continue
        matches.append(path)
    if not matches:
        raise FileNotFoundError(f"No file for {case_id!r} ending in {suffix!r} found in {case_dir}")
    if len(matches) > 1:
        raise ValueError(f"Multiple files for {case_id!r} ending in {suffix!r} found in {case_dir}: {matches}")
    return matches[0]


def _parse_literal(text: str) -> object:
    return eval(text.strip(), _EVAL_GLOBALS)  # noqa: S307 - trusted, locally captured fixture


def _parse_datetime_capture(text: str) -> tuple[str, datetime.datetime]:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    label = ""
    if lines and lines[0].strip().lower().startswith("label:"):
        label = lines[0].split(":", 1)[1].strip()
        lines = lines[1:]
    if not lines:
        raise ValueError("Datetime capture has no datetime line after stripping the label")
    return label, datetime.datetime.fromisoformat(lines[0].strip())


CASES = _discover_cases(CASE_ROOT)


@pytest.mark.parametrize("case_id,case_dir", CASES, ids=[c[0] for c in CASES])
def test_coordinator_case(case_id: str, case_dir: Path):
    rows = _parse_literal(
        _find_case_file(case_dir, case_id, "_static_realtime_input_fetch_departure_rows.txt").read_text(encoding="utf-8")
    )
    label, captured_at = _parse_datetime_capture(
        _find_case_file(case_dir, case_id, "_static_realtime_input_datetime.txt").read_text(encoding="utf-8")
    )
    feed_entities = json.loads(
        _find_case_file(case_dir, case_id, "_static_realtime_input_feed_entities.txt").read_text(encoding="utf-8")
    )
    expected = _parse_literal(
        _find_case_file(case_dir, case_id, "_static_realtime_output_combined.txt").read_text(encoding="utf-8")
    )

    dt_util.set_default_time_zone(dt_util.get_time_zone(TIMEZONE))
    hass = _FakeHass(TIMEZONE)
    entry = _FakeConfigEntry(rows)
    captured_at_utc = captured_at.astimezone(datetime.timezone.utc)

    with freeze_time(captured_at_utc.replace(tzinfo=None), tz_offset=0):
        now = dt_util.now().replace(tzinfo=None)
        now_local_tz = dt_util.now()
        now_date_local_tz = now_local_tz.strftime(dt_util.DATE_STR_FORMAT)
        now_time = now.strftime(TIME_STR_FORMAT)
        yesterday_date = (now - datetime.timedelta(days=1)).strftime(dt_util.DATE_STR_FORMAT)
        tomorrow = now + datetime.timedelta(days=1)
        tomorrow_date = tomorrow.strftime(dt_util.DATE_STR_FORMAT)
        tomorrow_date_local_tz = (dt_util.now() + datetime.timedelta(days=1)).strftime(
            dt_util.DATE_STR_FORMAT
        )

        start_station_id = rows[0]["origin_stop_id"]
        precomputed_next_departure = _interpret_departure_rows(
            hass, rows, start_station_id, now, now_local_tz,
            now_date_local_tz, now_time, yesterday_date,
            tomorrow, tomorrow_date, tomorrow_date_local_tz,
        )

        coord = coordinator_mod.GTFSUpdateCoordinator(hass, entry)

        with patch.object(coordinator_mod, "get_gtfs", return_value="FAKE_SCHEDULE"), \
             patch.object(coordinator_mod, "get_next_departure", return_value=precomputed_next_departure), \
             patch.object(coordinator_mod, "check_datasource_index", return_value=None), \
             patch.object(coordinator_mod, "update_route_geojson", return_value=None), \
             patch.object(coordinator_mod, "get_rt_alerts", return_value={}), \
             patch.object(gtfs_rt_helper_mod, "get_gtfs_feed_entities", return_value=feed_entities):
            result = asyncio.run(coord._async_update_data())

    assert result == expected, (
        f"[{case_id}] ({label}) coordinator.data did not match "
        f"case_*_coordinator_output_data.txt"
    )
