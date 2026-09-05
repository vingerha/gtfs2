"""Bootstrap for the provider tree, and the two results files.

The Home Assistant stand-ins of tests/ha_stub.py are registered before
any test module is imported. tests/ is put on sys.path for that one
import, so the two trees share a single stub and a single answer to "what
does the integration read off Home Assistant". Nothing else of tests/ is
read from here.

At the end of a session the records each case handed over through
record_property are written next to this file, the way tests/ writes its
own results.txt:

    results.txt   one entry per case with its outcome, and for a case that
                  did not pass the lines that broke the promise; to read
    results.json  every check of every case, passed or not, as fields with
                  the keys sorted; to diff between two checkouts or load
                  into a tool. Written by a plain full run only, so a -k
                  or --runxfail run does not overwrite a full one

Both files in the repo are the run on main, as examples of the output.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

import ha_stub  # noqa: E402

ha_stub.install()


import json  # noqa: E402

HERE = Path(__file__).resolve().parent
_reports = []


def pytest_runtest_logreport(report):
    if "test_journeys.py::" not in report.nodeid:
        return
    if report.when == "call" or (report.when == "setup" and report.failed):
        _reports.append(report)


def _outcome(report):
    if report.when == "setup":
        return "error"
    if hasattr(report, "wasxfail"):
        return "xfailed" if report.skipped else "xpassed"
    if report.failed and str(report.longrepr).startswith("[XPASS(strict)]"):
        return "xpassed"
    return report.outcome


def _reason(report):
    if hasattr(report, "wasxfail"):
        return report.wasxfail
    text = str(report.longrepr)
    if text.startswith("[XPASS(strict)]"):
        return text[len("[XPASS(strict)]"):].strip()
    return None


def _cases():
    cases = []
    for report in _reports:
        props = dict(report.user_properties)
        case = {"id": report.nodeid.split("[", 1)[-1].rstrip("]"),
                "outcome": _outcome(report), "reason": _reason(report),
                "checks": props.get("checks", [])}
        case.update(props.get("case", {}))
        if not case["checks"] and report.longrepr is not None:
            case["error"] = [line[2:].strip() for line in
                             str(report.longrepr).splitlines()
                             if line.startswith("E ")] or [str(report.longrepr)]
        cases.append(case)
    return sorted(cases, key=lambda c: c["id"])


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    if not _reports:
        return
    cases = _cases()
    lines = [f"test_journeys.py case results -- {len(cases)} case(s)", ""]
    for case in cases:
        lines += [f"case: {case['id']}", f"  result: {case['outcome'].upper()}"]
        if case["reason"]:
            lines.append(f"  reason: {case['reason']}")
        broke = [c["text"] for c in case["checks"] if not c["ok"]]
        broke += case.get("error", [])
        if broke:
            lines.append("  detail:")
            lines += [f"    {text}" for text in broke]
        lines.append("")
    (HERE / "results.txt").write_text("\n".join(lines), encoding="utf-8")

    option = session.config.option
    partial = option.keyword or option.markexpr or option.runxfail
    if partial or exitstatus == 2:  # a -k, -m or --runxfail run, or interrupted
        return
    (HERE / "results.json").write_text(
        json.dumps({"cases": cases}, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n", encoding="utf-8")
