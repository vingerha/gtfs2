"""Generic runner for every vehicle-position case.

Discovers every `case_N` under `case_vehicle_positions/` and, for each one,
runs the real, unmodified `gtfs_rt_helper.get_rt_vehicle_positions()` and
checks the geojson file it writes (and the features it returns) against the
case's expected output.

Nothing about the function's own logic is reimplemented here. Only the one
call that would make a live network request is replaced, and only its direct
return value:

    get_gtfs_feed_entities   -- the requests.get() of the vehicle positions
                                url; replaced with this case's feed_entities,
                                i.e. the decoded feed exactly as
                                get_gtfs_feed_entities hands it over (the
                                output of convert_gtfs_realtime_positions_to_json
                                for a protobuf feed, or the json feed itself)

The selection of vehicles (trip_id, or route_id + direction_id), the naming of
id/title, the [lon, lat] coordinates, `safe_file_part` for the file name and
`update_geojson` writing the file all run for real, unmodified. The file is
written under a per-test temporary directory instead of <config>/www/gtfs2.

Case files, per case N:

    case_N_vehicle_positions_input_sensor.txt         -- json: the sensor's
                                                          config (trip, route,
                                                          direction, icon, line
                                                          short name, destination)
                                                          plus a "label"
    case_N_vehicle_positions_input_feed_entities.txt  -- json: decoded feed
    case_N_vehicle_positions_output_geojson.txt       -- json: expected file

Adding a new case is: drop three files for a new N into the case folder.
Nothing here needs to change.
"""
from __future__ import annotations

import json
import re
import types
from pathlib import Path
from unittest.mock import patch

import pytest

import ha_stub

gtfs_rt_helper = ha_stub.load("gtfs_rt_helper")

CASE_ROOT = Path(__file__).parent / "case_vehicle_positions"

# Same place update_geojson writes to, relative to the hass config dir.
GEOJSON_SUBDIR = gtfs_rt_helper.DEFAULT_PATH_GEOJSON


class _FakeConfig:
    def __init__(self, config_dir: Path) -> None:
        self._config_dir = config_dir

    def path(self, *parts: str) -> str:
        return str(self._config_dir.joinpath(*parts))


class _FakeHass:
    """Stand-in for `homeassistant.core.HomeAssistant`.

    Only `.config.path()` is read, by update_geojson, to find the directory
    the geojson goes into.
    """

    def __init__(self, config_dir: Path) -> None:
        self.config = _FakeConfig(config_dir)


class _FakeConfigEntry:
    """Stand-in for `homeassistant.config_entries.ConfigEntry`; only
    `.data["destination"]` is read, for the feature title."""

    def __init__(self, sensor: dict) -> None:
        self.data = {"destination": sensor["destination"]}


def _make_sensor(sensor: dict, hass: _FakeHass) -> types.SimpleNamespace:
    """The attributes get_rt_vehicle_positions reads off `self`, as the
    coordinator sets them, built from this case's sensor file."""
    return types.SimpleNamespace(
        hass=hass,
        config_entry=_FakeConfigEntry(sensor),
        _vehicle_position_url="http://not-called.invalid/vehicle",  # fetch is patched
        _headers={},
        _trip_id=sensor["trip_id"],
        _trip_list=sensor.get("next_departures_trips", []),  # coordinator: next_departures_trip_id[:10]
        _route_id=sensor["route_id"],
        _direction=sensor["direction"],
        _icon=sensor["icon"],
        _data={"next_departure": {"route_short_name": sensor["route_short_name"]}},
    )


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


def _read_json(case_dir: Path, case_id: str, suffix: str):
    return json.loads(_find_case_file(case_dir, case_id, suffix).read_text(encoding="utf-8"))


CASES = _discover_cases(CASE_ROOT)


@pytest.mark.parametrize("case_id,case_dir", CASES, ids=[c[0] for c in CASES])
def test_vehicle_positions_case(case_id: str, case_dir: Path, tmp_path: Path):
    sensor = _read_json(case_dir, case_id, "_vehicle_positions_input_sensor.txt")
    feed_entities = _read_json(case_dir, case_id, "_vehicle_positions_input_feed_entities.txt")
    expected = _read_json(case_dir, case_id, "_vehicle_positions_output_geojson.txt")
    label = sensor.get("label", "")

    hass = _FakeHass(tmp_path)
    sensor_obj = _make_sensor(sensor, hass)

    with patch.object(gtfs_rt_helper, "get_gtfs_feed_entities", return_value=feed_entities):
        returned = gtfs_rt_helper.get_rt_vehicle_positions(sensor_obj)

    # The file name follows the route file next to it: safe_file_part(route)_safe_file_part(direction)
    file_name = (gtfs_rt_helper.safe_file_part(sensor["route_id"]) + "_"
                 + gtfs_rt_helper.safe_file_part(sensor["direction"]) + ".json")
    written_path = tmp_path / GEOJSON_SUBDIR / file_name
    assert written_path.is_file(), f"[{case_id}] ({label}) geojson not written at {written_path}"
    written = json.loads(written_path.read_text(encoding="utf-8"))

    assert written == expected, (
        f"[{case_id}] ({label}) written geojson did not match "
        f"case_*_vehicle_positions_output_geojson.txt"
    )
    assert returned == expected["features"], (
        f"[{case_id}] ({label}) returned features did not match the expected features"
    )