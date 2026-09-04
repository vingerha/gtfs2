"""Import one gtfs2 module without a Home Assistant install.

gtfs_helper imports homeassistant at module level, and importing it the plain
way (`from custom_components.gtfs2.gtfs_helper import ...`) also runs the
package `__init__.py`, which pulls in the coordinator, the platforms and with
them a good half of Home Assistant. Installing homeassistant answers all of
that, but it is a heavy pin for a suite whose subject is the integration's own
logic: `_interpret_departure_rows` reads a list of dicts and a few datetimes,
and never touches a hass object beyond `hass.config.time_zone`.

So the handful of homeassistant modules the import chain touches are registered
as stubs, and `load()` imports the wanted module on its own, under a synthetic
package whose `__path__` is the component directory, so its relative imports
(`from .const import ...`) still resolve while `__init__.py` never runs.

Only the functions the code actually calls carry behaviour: utcnow, now, as_utc,
utc_from_timestamp, get_time_zone and parse_datetime. Everything else is an
empty shell, and a shell that is reached raises instead of returning something
plausible: a harness that quietly answers for the code under test is worse than
no harness.

What this does not do is check that the integration uses Home Assistant
correctly. A stub cannot notice that an HA signature moved. It is meant for the
pure-logic cases; a test that exercises hass itself wants the real thing.

Nothing here is imported beyond the standard library, and if homeassistant does
turn out to be installed, `install()` steps aside and leaves the real modules in
place, so a test written against the stub keeps working in an environment that
has HA.

Using it
--------

tests/conftest.py claims the integration's package name before any test module
is imported:

    import ha_stub

    ha_stub.install()
    ha_stub.claim()

From then on a test writes the import it would write anywhere, and it resolves
under the claimed package instead of running `__init__.py`:

    import homeassistant.util.dt as dt_util
    from custom_components.gtfs2.gtfs_helper import _interpret_departure_rows

Nothing in a test file has to know the stub exists. A script outside pytest
asks for a module by name and gets the same object:

    gtfs_helper = ha_stub.load("gtfs_helper")

The clock the stub reads is `datetime.datetime.now`, which is what freezegun
replaces, so `freeze_time` drives dt_util.utcnow() and dt_util.now() as it does
with the real one. Where freezegun is not wanted, the stub can be pinned on its
own, in epoch seconds:

    ha_stub.freeze(datetime.datetime.fromisoformat(
        "2026-09-03T08:05:00+01:00").timestamp())

dt_util.now() answers in the default zone Home Assistant would have set at
startup, so a test that compares against local timetable strings sets it the
same way the integration does:

    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Paris"))

load() takes a component directory too. Pointing it at the copy in another
checkout, under its own name, loads both in one process, which is how the
before and after of a change can be pushed the same fixture and diffed:

    old = ha_stub.load("gtfs_helper", component=other_checkout, alias="before")
    new = ha_stub.load("gtfs_helper")

Two errors say the same thing in different ways. An ImportError naming a
homeassistant module or symbol means install() does not carry it yet. An
AssertionError naming a stubbed symbol means the test reached one that is there
but empty. Either the test walked onto a path this rig was not built for, or
the stub has to grow a real answer. Returning something plausible to make the
run go through is how a harness starts lying about the code it measures.

Some of it should not grow. coordinator, sensor and config_flow are Home
Assistant plumbing, and what a test would check there is that the integration
speaks to HA correctly, which is exactly what a stub cannot answer for. Those
want the real thing, and install() steps aside as soon as it is installed.
"""
from __future__ import annotations

import datetime
import importlib
import sys
import types
from pathlib import Path

COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "gtfs2"
PACKAGE = "custom_components.gtfs2"

_FROZEN: float | None = None
_DEFAULT_TIME_ZONE: datetime.tzinfo = datetime.timezone.utc
_DT_MODULE: types.ModuleType | None = None


def freeze(epoch_seconds: float | None) -> None:
    """Pin the stub clock, or hand it back to the wall clock with None."""
    global _FROZEN
    _FROZEN = None if epoch_seconds is None else float(epoch_seconds)


def frozen_at() -> float | None:
    return _FROZEN


def _utcnow() -> datetime.datetime:
    if _FROZEN is None:
        # datetime.now is what freezegun replaces, so freeze_time reaches here
        return datetime.datetime.now(datetime.timezone.utc)
    return datetime.datetime.fromtimestamp(_FROZEN, datetime.timezone.utc)


def _utc_from_timestamp(timestamp: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc)


def _as_utc(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(datetime.timezone.utc)


def _get_time_zone(name: str) -> datetime.tzinfo:
    """The tzinfo of a zone name, the way Home Assistant hands it out.

    Home Assistant answers None for a zone it cannot find, which here would
    quietly send every comparison back to UTC and shift a whole timetable by
    the agency's offset. A test would rather hear about it: a Windows checkout
    with no `tzdata` installed has no zone database at all, and the wrong
    answer would be a passing suite reading the wrong wall clock.
    """
    import zoneinfo
    try:
        return zoneinfo.ZoneInfo(name)
    except zoneinfo.ZoneInfoNotFoundError as err:
        raise RuntimeError(
            f"no time zone {name!r}: either the name is wrong, or this "
            "machine has no IANA zone database (pip install tzdata)") from err


def _set_default_time_zone(time_zone: datetime.tzinfo) -> None:
    global _DEFAULT_TIME_ZONE
    _DEFAULT_TIME_ZONE = time_zone
    if _DT_MODULE is not None:
        _DT_MODULE.DEFAULT_TIME_ZONE = time_zone


def _now(time_zone: datetime.tzinfo | None = None) -> datetime.datetime:
    """The current instant read in a zone rather than in UTC.

    get_next_departure compares a timetable, which is written in the agency's
    local time, against this. Answering UTC here would move every departure by
    the offset of the network being read.
    """
    return _utcnow().astimezone(time_zone or _DEFAULT_TIME_ZONE)


def _parse_datetime(value) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _module(name: str, **attrs) -> types.ModuleType:
    """Register one stub module, and hang it off its parent by attribute.

    Both spellings of an import have to work: `import homeassistant.util.dt`
    reads sys.modules, while `from homeassistant.helpers import entity_registry`
    reads the attribute off the parent package.

    Every stub is a package, empty __path__ and all, so that an import of
    something below it is looked up rather than refused for the wrong reason,
    and a symbol the stub does not carry says what is missing instead of
    reading like a broken Home Assistant install.
    """
    module = types.ModuleType(name)
    module.__path__ = []
    for key, value in attrs.items():
        setattr(module, key, value)

    def __getattr__(item: str):
        if item.startswith("__") and item.endswith("__"):
            # introspection, not the code under test asking for a symbol
            raise AttributeError(item)
        raise ImportError(
            f"{name}.{item} is not stubbed: add it to ha_stub.install(), or "
            "run this test against a real Home Assistant")

    module.__getattr__ = __getattr__
    sys.modules[name] = module
    parent_name, _, leaf = name.rpartition(".")
    if parent_name and parent_name in sys.modules:
        setattr(sys.modules[parent_name], leaf, module)
    return module


class _Unreached:
    """Stands in for a symbol no test is expected to reach."""

    def __init__(self, what: str) -> None:
        self._what = what

    def __call__(self, *args, **kwargs):
        raise AssertionError(
            f"the harness reached {self._what}, which is stubbed out: either "
            "the test drives a path this rig was not built for, or the stub "
            "has to grow")

    def __getattr__(self, item: str) -> "_Unreached":
        return _Unreached(f"{self._what}.{item}")


class _MissingStub:
    """Answers an unstubbed homeassistant import with a message that says so.

    Without this the import machinery reports "No module named
    'homeassistant.data_entry_flow'; 'homeassistant' is not a package", which
    reads like a broken install rather than a stub that has not been written.
    Registered on sys.meta_path only when the stubs are, and consulted only
    after sys.modules, so it never speaks for a module that is stubbed.
    """

    @staticmethod
    def find_spec(name: str, path=None, target=None):
        if name == "homeassistant" or name.startswith("homeassistant."):
            raise ImportError(
                f"{name} is not stubbed: add what the test needs to "
                "ha_stub.install(), or run the test against a real Home "
                "Assistant. Modules like coordinator, sensor and config_flow "
                "are Home Assistant plumbing, and a stub cannot answer for "
                "them honestly")
        return None


class _Platform:
    SENSOR = "sensor"
    BINARY_SENSOR = "binary_sensor"
    SWITCH = "switch"
    UPDATE = "update"


def installed() -> bool:
    """Whether a Home Assistant, real or already stubbed, can be imported."""
    if "homeassistant" in sys.modules:
        return True
    try:
        import homeassistant  # noqa: F401
    except ImportError:
        return False
    return True


def install() -> None:
    """Register the homeassistant modules the gtfs2 import chain touches.

    A real Home Assistant wins: if one is importable, this does nothing at all,
    so the same test reads the real dt_util wherever it is available.
    """
    global _DT_MODULE
    if installed():
        return

    _module("homeassistant")
    _module(
        "homeassistant.const",
        CONF_OFFSET="offset",
        CONF_HOST="host",
        CONF_NAME="name",
        STATE_UNKNOWN="unknown",
        Platform=_Platform,
        ATTR_LATITUDE="latitude",
        ATTR_LONGITUDE="longitude",
    )
    _module("homeassistant.util", Throttle=lambda *a, **k: (lambda fn: fn))
    _DT_MODULE = _module(
        "homeassistant.util.dt",
        utcnow=_utcnow,
        now=_now,
        as_utc=_as_utc,
        utc_from_timestamp=_utc_from_timestamp,
        get_time_zone=_get_time_zone,
        set_default_time_zone=_set_default_time_zone,
        parse_datetime=_parse_datetime,
        DATE_STR_FORMAT="%Y-%m-%d",
        DEFAULT_TIME_ZONE=_DEFAULT_TIME_ZONE,
    )
    _module("homeassistant.helpers")
    _module("homeassistant.helpers.config_validation", string=str, boolean=bool)
    _module("homeassistant.helpers.entity", Entity=object)
    _module("homeassistant.helpers.entity_registry",
            async_get=_Unreached("entity_registry.async_get"))
    _module("homeassistant.helpers.translation",
            async_get_translations=_Unreached("async_get_translations"))
    _module("homeassistant.helpers.dispatcher",
            async_dispatcher_send=_Unreached("async_dispatcher_send"))
    _module("homeassistant.helpers.event",
            async_track_time_change=_Unreached("async_track_time_change"))
    _module("homeassistant.components")
    _module("homeassistant.components.sensor",
            PLATFORM_SCHEMA=_Unreached("PLATFORM_SCHEMA"))
    _module("homeassistant.components.persistent_notification",
            async_create=_Unreached("persistent_notification.async_create"),
            create=_Unreached("persistent_notification.create"))
    _module("homeassistant.core", HomeAssistant=object, ServiceCall=object,
            SupportsResponse=_Unreached("SupportsResponse"),
            callback=lambda fn: fn)
    _module("homeassistant.config_entries", ConfigEntry=object,
            ConfigEntries=object, SOURCE_IMPORT="import")
    _module("homeassistant.exceptions", HomeAssistantError=Exception)
    if _MissingStub not in sys.meta_path:
        sys.meta_path.append(_MissingStub)


def claim(name: str = PACKAGE, component: str | Path = COMPONENT) -> types.ModuleType:
    """Register a synthetic package `name` whose __path__ is `component`.

    Once it sits in sys.modules, an import of `name.something` finds the module
    file through that __path__ and never looks for, let alone runs, the real
    __init__.py. Under the integration's own name that lets a test write the
    import it would write anywhere; under another name it keeps a second
    checkout apart from the first.
    """
    install()
    package = sys.modules.get(name)
    if package is None:
        package = types.ModuleType(name)
        package.__path__ = [str(Path(component))]
        sys.modules[name] = package
    return package


def load(module_name: str, component: str | Path = COMPONENT,
         alias: str = PACKAGE) -> types.ModuleType:
    """Import one module of a gtfs2 component directory, on its own.

    The module is loaded as a submodule of the package claim() registers for
    `alias`, so its relative imports resolve in `component` while that
    directory's __init__.py never runs. Under the default alias it is the very
    module a plain import gives; under another alias, with another component,
    it is a second checkout kept apart in sys.modules.
    """
    claim(alias, component)
    return importlib.import_module(f"{alias}.{module_name}")
