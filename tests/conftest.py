"""Writes a per-case results report after the test session finishes.

Terminal output shows one line per parametrized case already (pytest's
own `-v` output), but this writes the same findings to a plain text
file, split per case, so results can be reviewed or diffed without
re-running pytest or scrolling terminal history.

Only concerns itself with `test_static_case` (test_static_cases.py);
other tests in this tree are unaffected and not written to the report.
"""
from __future__ import annotations

from pathlib import Path

REPORT_PATH = Path(__file__).parent / "case_route" / "results.txt"


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 - exitstatus required by hook signature
    terminalreporter = session.config.pluginmanager.getplugin("terminalreporter")
    if terminalreporter is None:
        return

    case_reports = []
    for outcome in ("passed", "failed", "error", "skipped"):
        for report in terminalreporter.stats.get(outcome, []):
            if getattr(report, "when", None) not in (None, "call"):
                continue  # skip setup/teardown phase reports, keep the actual test result
            nodeid = getattr(report, "nodeid", "")
            if "::test_static_case[" not in nodeid:
                continue
            case_reports.append((nodeid, outcome, report))

    if not case_reports:
        return

    lines = [f"Static case results -- {len(case_reports)} case(s)", ""]
    for nodeid, outcome, report in sorted(case_reports, key=lambda item: item[0]):
        case_id = nodeid.split("[", 1)[-1].rstrip("]")
        lines.append(f"case: {case_id}")
        lines.append(f"  result: {outcome.upper()}")
        if outcome in ("failed", "error"):
            failure_text = str(report.longrepr)
            failure_lines = failure_text.splitlines()
            # Keep the whole "E   ..." block: the AssertionError line,
            # the value summary, and the "Differing items:" section that
            # actually shows which field changed and how -- not just
            # the first line, which is only the custom message.
            detail_lines = [line for line in failure_lines if line.startswith("E ")]
            if detail_lines:
                lines.append("  detail:")
                for detail_line in detail_lines:
                    lines.append(f"    {detail_line[2:].strip()}")
            else:
                lines.append(f"  detail: {failure_lines[-1].strip() if failure_lines else ''}")
        lines.append("")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
