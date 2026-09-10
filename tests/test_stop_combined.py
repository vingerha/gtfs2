"""Generic runner for every captured local-stop static+RT combined case.

Discovers every `case_N` group under `case_stop_combined/` (by shared
case number in the filename, same convention as the other test files)
and, for each one, runs the real, unmodified
`GTFSLocalStopUpdateCoordinator._async_update_data()` and checks its
real return value against the case's captured output.

Only the true I/O boundaries are replaced:

    get_gtfs                          -- opens a real GTFS sqlite file
    check_datasource_index            -- runs SQL against the database
    check_service_dates_table         -- runs SQL against the database
    get_local_stops_next_departures   -- queries the database; replaced
                                          with the real, unmodified
                                          `_interpret_local_stop_rows()`'s
                                          own output, computed from this
                                          case's rows and RT feed
    get_gtfs_rt                       -- downloads the RT feed to a
                                          local file; replaced with a
                                          plain "ok" so the real code's
                                          local-file-staging step
                                          doesn't short-circuit
    get_gtfs_feed_entities            -- reads/parses that file;
                                          replaced with this case's
                                          feed_entities

Every line of `coordinator.py`, `_interpret_local_stop_rows`,
`_build_local_stop_element`, and `get_rt_route_trip_statuses` (trip-mode
matching, used here since local stops always run in `"trip"` mode) run
for real, unmodified.

Adding a new case is: capture four files into a new `case_N` group.
Nothing here needs to change.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import re
import sys
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

CASE_ROOT = Path(__file__).parent / "case_stop_combined"

# Fixed integration config -- not per-case diffable data, so not stored
# as its own case file. Same choice as TIMEZONE in the other suites.
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

    `.data` and `.options` are read directly by `_async_update_data()`.
    `real_time: True` here (unlike test_stop_static.py) is what makes
    the coordinator actually set up its RT attributes before calling
    `get_local_stops_next_departures` -- that call itself is still
    mocked (it touches the database), so this doesn't reach any
    network on its own; it only affects what the coordinator sets on
    itself along the way.
    """

    def __init__(self) -> None:
        self.entry_id = "test_entry"
        self.data = {
            "name": "local_stop_name",
            "file": "zou_proximite",
            "device_tracker_id": "device_tracker.test_device",
        }
        self.options = {
            "offset": 0,
            "timerange": 30,
            "radius": 200,
            "real_time": True,
        }


class _LocalStopContext:
    """Stand-in for the object `_interpret_local_stop_rows` (and, inside
    it, `_build_local_stop_element` and `get_rt_route_trip_statuses`)
    read `self` off of when called directly -- bypassing the
    coordinator's own executor-job call, which is mocked here since it
    also touches the database.

    `_realtime = True` and `_rt_group = "trip"` match what the real
    coordinator sets before calling `get_local_stops_next_departures`
    when `real_time` is enabled in options. The handful of other
    attributes are read by `get_rt_route_trip_statuses` itself
    (`_vehicle_position_url`, `_route_delimiter`) or by the RT-fetch
    block inside `_interpret_local_stop_rows` (`_headers`,
    `_trip_update_url`) -- verified against the real functions, not
    guessed.
    """

    def __init__(self, hass, offset: int, name: str) -> None:
        self.hass = hass
        self._data = {"offset": offset, "name": name}
        self._realtime = True
        self._rt_group = "trip"
        self._headers = {}
        self._trip_update_url = "http://example.invalid/rt"
        self._vehicle_position_url = None
        self._route_delimiter = None


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


def _normalize_datetimes(value):
    """Recursively convert any datetime subclass (e.g. freezegun's
    FakeDatetime, produced by code running inside a `freeze_time` block)
    into a plain `datetime.datetime` with identical field values.

    Must be called *after* the `freeze_time` block has exited:
    freezegun patches `datetime.datetime` itself to be `FakeDatetime`
    while active, so a type-check made from inside the block compares
    the patched class against itself and never triggers -- confirmed
    the hard way in test_route_combined.py.
    """
    if isinstance(value, datetime.datetime) and type(value) is not datetime.datetime:
        return datetime.datetime(
            value.year, value.month, value.day, value.hour,
            value.minute, value.second, value.microsecond, value.tzinfo,
        )
    if isinstance(value, dict):
        return {k: _normalize_datetimes(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_datetimes(v) for v in value]
    return value


CASES = _discover_cases(CASE_ROOT)


@pytest.mark.parametrize("case_id,case_dir", CASES, ids=[c[0] for c in CASES])
def test_stop_combined(case_id: str, case_dir: Path):
    rows = _parse_literal(
        _find_case_file(case_dir, case_id, "_static_realtime_stop_input_fetch_departure_rows.txt").read_text(encoding="utf-8")
    )
    label, captured_at = _parse_datetime_capture(
        _find_case_file(case_dir, case_id, "_static_realtime_stop_input_datetime.txt").read_text(encoding="utf-8")
    )
    feed_entities = json.loads(
        _find_case_file(case_dir, case_id, "_static_realtime_stop_input_feed_entities.txt").read_text(encoding="utf-8")
    )
    expected = _parse_literal(
        _find_case_file(case_dir, case_id, "_static_realtime_stop_output_coordinator_data.txt").read_text(encoding="utf-8")
    )

    dt_util.set_default_time_zone(dt_util.get_time_zone(TIMEZONE))
    hass = _FakeHass(TIMEZONE)
    entry = _FakeConfigEntry()
    captured_at_utc = captured_at.astimezone(datetime.timezone.utc)

    with freeze_time(captured_at_utc.replace(tzinfo=None), tz_offset=0):
        ctx = _LocalStopContext(hass, entry.options["offset"], entry.data["name"])

        with patch.object(gtfs_helper, "get_gtfs_rt", return_value="ok"), \
             patch.object(gtfs_helper, "get_gtfs_feed_entities", return_value=feed_entities):
            precomputed_local_stops = gtfs_helper._interpret_local_stop_rows(ctx, rows)

        coord = coordinator_mod.GTFSLocalStopUpdateCoordinator(hass, entry)

        with patch.object(coordinator_mod, "get_gtfs", return_value="FAKE_SCHEDULE"), \
             patch.object(coordinator_mod, "check_datasource_index", return_value=None), \
             patch.object(coordinator_mod, "check_service_dates_table", return_value=None), \
             patch.object(coordinator_mod, "get_local_stops_next_departures", return_value=precomputed_local_stops):
            result = asyncio.run(coord._async_update_data())

    result = _normalize_datetimes(result)

    assert result == expected, (
        f"[{case_id}] ({label}) coordinator.data did not match "
        f"case_*_static_realtime_stop_output_coordinator_data.txt"
    )
