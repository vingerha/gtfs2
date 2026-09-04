"""Bootstrap for the provider tree: the Home Assistant stand-ins of
tests/ha_stub.py, registered before any test module is imported.

tests/ is put on sys.path for that one import, so the two trees share a
single stub and a single answer to "what does the integration read off
Home Assistant". Nothing else of tests/ is read from here.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

import ha_stub  # noqa: E402

ha_stub.install()
