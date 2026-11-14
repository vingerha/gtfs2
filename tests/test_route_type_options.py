"""Every route type a feed can declare has a label to show.

The sensor reads the route type name straight out of ROUTE_TYPE_OPTIONS to
build its Route attributes, so a type missing from the table is a KeyError
on every update, not a cosmetic gap: the entity never reports a departure.
Riga's trolleybus lines are route_type 11, which is how a feed that works
fine when its routes are relabelled as buses (3) showed no trips at all.
"""
from __future__ import annotations

import ha_stub

const = ha_stub.load("const")

# The base GTFS route types (reference.md): 0-7, 11 trolleybus, 12 monorail.
BASE_ROUTE_TYPES = (0, 1, 2, 3, 4, 5, 6, 7, 11, 12)


def test_sensor_lookup_has_a_label_for_every_base_route_type():
    for route_type in BASE_ROUTE_TYPES:
        assert route_type in const.ROUTE_TYPE_OPTIONS, (
            f"route_type {route_type} has no entry: the sensor's "
            "ROUTE_TYPE_OPTIONS[...] lookup raises KeyError for it"
        )


def test_every_base_route_type_has_its_own_icon():
    """ICONS.get() falls back to the generic train icon, so a missing entry
    is silent: a trolleybus sensor simply shows mdi:train forever."""
    for route_type in BASE_ROUTE_TYPES:
        assert route_type in const.ICONS, (
            f"route_type {route_type} has no icon: the sensor falls back to "
            f"{const.ICON} instead of showing the right mode"
        )
