"""Generic runner for every captured static-layer case.

Discovers every `case_N` directory under `fixtures/case_route/` and,
for each one, feeds its two input captures (`fetch_departure_rows`,
`datetime`) into the real `_interpret_departure_rows()` and checks the
result against its two output captures (`interpret_departure_rows`,
`coordinator_data`).

Adding a new case is: capture four files into a new `case_N` folder.
Nothing here needs to change -- no route, stop, or case number is
referenced by name in this file.

Each capture file is the literal text of one `_LOGGER.warning(...)`
line, pasted verbatim from a real run -- not reformatted, not cleaned
up. Parsing one just means undoing the "TEST FIXTURE ...: " logging
prefix and `eval()`-ing the remainder, since the captured value is
valid Python repr syntax (the same thing `pprint`/`%s`-formatting a
dict produces). Kept deliberately dumb: no schema validation, no
normalization -- if a capture's shape changes, the eval fails loudly
or the assertion does.
"""
from __future__ import annotations

import datetime
import re
import zoneinfo
from pathlib import Path

import pytest
from freezegun import freeze_time

import homeassistant.util.dt as dt_util

from custom_components.gtfs2.gtfs_helper import _interpret_departure_rows, TIME_STR_FORMAT

CASE_ROOT = Path(__file__).parent / "case_route"

# Fixed integration config, not per-case diffable data, so it isn't
# stored as its own case file. If a future case genuinely needs a
# different timezone, this becomes a per-case value read from the
# datetime capture instead -- not needed while every case is Paris.
TIMEZONE = "Europe/Paris"

_EVAL_GLOBALS = {"datetime": datetime, "zoneinfo": zoneinfo}


class _FakeConfig:
    def __init__(self, time_zone: str) -> None:
        self.time_zone = time_zone

    def path(self, value: str = "") -> str:
        # gtfs_helper only ever uses this to build a filesystem path for
        # extraction-lock checks; not exercised by this test.
        return value


class _FakeHass:
    """Stand-in for `homeassistant.core.HomeAssistant`.

    `_interpret_departure_rows` only ever reads `hass.config.time_zone`.
    """

    def __init__(self, time_zone: str) -> None:
        self.config = _FakeConfig(time_zone)


_CASE_NUM_RE = re.compile(r"case_(\d+)")


def _discover_cases(case_root: Path) -> list[tuple[str, Path]]:
    """Every case under `case_root`, as (case_id, dir_containing_its_files).

    A case's files may be named `case_1_...` or `case_1a_/case_1b_/...`
    (a sub-letter per file, e.g. from an earlier naming convention) --
    either way, everything sharing the same case *number* is grouped
    into one case, identified as `case_1`, `case_2`, etc.

    Supports both folder layouts too:
    - grouped:  case_route/case_1/case_1..._....txt
    - flat:     case_route/case_1..._....txt directly
    """
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
    """The single file for `case_id` in `case_dir` whose name ends with
    `suffix`. Matched by case number, not an exact `case_1_` prefix, so
    `case_1_...`, `case_1a_...`, `case_1b_...` etc. all count as
    belonging to `case_1` -- while still telling `case_1` apart from
    `case_10`, `case_11`, ... (a plain `str.startswith` alone would not).
    """
    case_num = case_id.split("_", 1)[1]
    prefix = f"case_{case_num}"
    matches = []
    for path in case_dir.iterdir():
        name = path.name
        if not name.startswith(prefix) or not name.endswith(suffix):
            continue
        next_char = name[len(prefix):len(prefix) + 1]
        if next_char.isdigit():
            continue  # this is case_10's file, not case_1's
        matches.append(path)
    if not matches:
        raise FileNotFoundError(f"No file for {case_id!r} ending in {suffix!r} found in {case_dir}")
    if len(matches) > 1:
        raise ValueError(f"Multiple files for {case_id!r} ending in {suffix!r} found in {case_dir}: {matches}")
    return matches[0]


def _parse_rows_capture(text: str) -> list[dict]:
    """Format: a bare Python list-of-dicts literal, nothing else."""
    return eval(text.strip(), _EVAL_GLOBALS)  # noqa: S307 - trusted, locally captured fixture


def _parse_datetime_capture(text: str) -> tuple[str, datetime.datetime]:
    """Format: an optional `label: <free text>` line, then one bare ISO
    datetime. Returns (label, instant); label is "" if absent.
    """
    lines = [line for line in text.strip().splitlines() if line.strip()]
    label = ""
    if lines and lines[0].strip().lower().startswith("label:"):
        label = lines[0].split(":", 1)[1].strip()
        lines = lines[1:]
    if not lines:
        raise ValueError("Datetime capture has no datetime line after stripping the label")
    return label, datetime.datetime.fromisoformat(lines[0].strip())


def _parse_interpret_output_capture(text: str) -> dict:
    """Format: a bare Python dict literal, nothing else"""
    return eval(text.strip(), _EVAL_GLOBALS)  # noqa: S307 - trusted, locally captured fixture


def _parse_coordinator_data_capture(text: str) -> dict:
    """Format: a bare Python dict literal, nothing else.

    The captured dict contains one non-literal value -- the live
    `schedule` object's default repr (`<pygtfs.schedule.Schedule object
    at 0x...>`), which isn't valid Python syntax on its own. It's
    replaced with `None` before parsing since no test compares against
    it; everything else in the capture is parsed as-is.
    """
    body = re.sub(r"<pygtfs\.schedule\.Schedule object at 0x[0-9a-fA-F]+>", "None", text.strip())
    return eval(body, _EVAL_GLOBALS)  # noqa: S307 - trusted, locally captured fixture


CASES = _discover_cases(CASE_ROOT)


@pytest.mark.parametrize("case_id,case_dir", CASES, ids=[c[0] for c in CASES])
def test_static_case(case_id: str, case_dir: Path):
    rows = _parse_rows_capture(
        _find_case_file(case_dir, case_id, "_static_input_fetch_departure_rows.txt").read_text(encoding="utf-8")
    )
    start_station_id = rows[0]["origin_stop_id"]
    label, captured_at = _parse_datetime_capture(
        _find_case_file(case_dir, case_id, "_static_input_datetime.txt").read_text(encoding="utf-8")
    )
    expected_interpret_result = _parse_interpret_output_capture(
        _find_case_file(case_dir, case_id, "_static_output_interpret_departure_rows.txt").read_text(encoding="utf-8")
    )
    coordinator_data = _parse_coordinator_data_capture(
        _find_case_file(case_dir, case_id, "_static_output_coordinator_data.txt").read_text(encoding="utf-8")
    )

    # HA sets its own default timezone once at startup from
    # hass.config.time_zone; without this, dt_util.now() defaults to
    # UTC and `now` (naive) ends up compared against the GTFS schedule's
    # implicitly-local time strings using the wrong wall clock.
    dt_util.set_default_time_zone(dt_util.get_time_zone(TIMEZONE))

    hass = _FakeHass(TIMEZONE)
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

        result = _interpret_departure_rows(
            hass, rows, start_station_id, now, now_local_tz,
            now_date_local_tz, now_time, yesterday_date,
            tomorrow, tomorrow_date, tomorrow_date_local_tz,
        )

    # 1. The real function, given this case's real inputs, must
    #    reproduce this case's real output capture.
    assert result == expected_interpret_result, (
        f"[{case_id}] ({label}) result did not match "
        f"case_*_static_output_interpret_departure_rows.txt"
    )

    # 2. The two output captures for this case must be mutually
    #    consistent -- the coordinator's captured next_departure
    #    sub-dict must be the exact same result, proving both were
    #    captured from the same event and not two mismatched runs.
    assert coordinator_data["next_departure"] == expected_interpret_result, (
        f"[{case_id}] ({label}) output captures 1c/1d are mutually inconsistent"
    )
