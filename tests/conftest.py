"""Writes a per-case results report after the test session finishes.

Terminal output shows one line per parametrized case already (pytest's
own `-v` output), but this writes the same findings to a plain text
file, split per case, so results can be reviewed or diffed without
re-running pytest or scrolling terminal history.

Matches any `test_*_case[...]` node id, not one specific function name
-- so this covers `test_static_case` (test_static_cases.py),
`test_coordinator_case` (test_route_combined.py), and any future
test file following the same one-function-per-case-set pattern,
without needing an update here each time one is added.

Each test module's results are written next to that module's own case
folder (e.g. test_static_cases.py's cases -> tests/case_route/results.txt,
test_route_combined.py's cases -> tests/case_route_combined/results.txt),
derived from the test file's own name rather than a single hardcoded
path -- so results from different test files never overwrite each
other or land in the wrong folder.
"""
from __future__ import annotations

import re
from pathlib import Path

TESTS_DIR = Path(__file__).parent

# test_static_cases.py -> case_route, test_route_combined.py -> case_route_combined
_TEST_FILE_TO_CASE_FOLDER = {
    "test_route_static.py": "case_route",
    "test_route_combined.py": "case_route_combined",
}

_CASE_NODEID_RE = re.compile(r"^(?P<file>[^:]+)::test_\w+\[")


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 - exitstatus required by hook signature
    terminalreporter = session.config.pluginmanager.getplugin("terminalreporter")
    if terminalreporter is None:
        return

    # Group case reports by which test file produced them, so each
    # gets its own report file next to its own case folder.
    by_test_file: dict[str, list[tuple[str, str, object]]] = {}
    for outcome in ("passed", "failed", "error", "skipped"):
        for report in terminalreporter.stats.get(outcome, []):
            if getattr(report, "when", None) not in (None, "call"):
                continue  # skip setup/teardown phase reports, keep the actual test result
            nodeid = getattr(report, "nodeid", "")
            match = _CASE_NODEID_RE.match(nodeid)
            if not match:
                continue
            test_file = Path(match.group("file")).name
            by_test_file.setdefault(test_file, []).append((nodeid, outcome, report))

    for test_file, case_reports in by_test_file.items():
        case_folder = _TEST_FILE_TO_CASE_FOLDER.get(test_file)
        if case_folder is None:
            # An unrecognised test file matched the case-test pattern but
            # isn't in the map above -- write next to the test file's own
            # name rather than silently drop its results.
            case_folder = Path(test_file).stem

        report_path = TESTS_DIR / case_folder / "results.txt"

        lines = [f"{test_file} case results -- {len(case_reports)} case(s)", ""]
        for nodeid, outcome, report in sorted(case_reports, key=lambda item: item[0]):
            case_id = nodeid.split("[", 1)[-1].rstrip("]")
            lines.append(f"case: {case_id}")
            lines.append(f"  result: {outcome.upper()}")
            if outcome in ("failed", "error"):
                failure_text = str(report.longrepr)
                failure_lines = failure_text.splitlines()
                # Keep the whole "E   ..." block: the AssertionError line,
                # the value summary, and the "Differing items:" section
                # that actually shows which field changed and how -- not
                # just the first line, which is only the custom message.
                detail_lines = [line for line in failure_lines if line.startswith("E ")]
                if detail_lines:
                    lines.append("  detail:")
                    for detail_line in detail_lines:
                        lines.append(f"    {detail_line[2:].strip()}")
                else:
                    lines.append(f"  detail: {failure_lines[-1].strip() if failure_lines else ''}")
            lines.append("")

        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("\n".join(lines), encoding="utf-8")
