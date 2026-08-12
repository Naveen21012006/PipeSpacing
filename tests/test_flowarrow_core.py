# -*- coding: utf-8 -*-
"""Unit tests for the Drainage Flow Arrows placement engine.

The engine (flowarrow_core.py) is pure geometry over (x, y, z) tuples in
millimetres, so the whole decision chain - slope classification, high->low
direction, arrow stations, duplicate detection and the rotation angles -
is verified here without Revit.
"""

import math

import pytest

import flowarrow_core as core


CFG = dict(core.DEFAULTS)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def test_short_pipe_is_skipped():
    assert core.classify_pipe((0, 0, 0), (500, 0, 50), CFG) == core.TOO_SHORT


def test_vertical_pipe_is_skipped():
    # A riser: 3 m drop with a 10 mm plan run.
    assert core.classify_pipe((0, 0, 3000), (10, 0, 0), CFG) == core.VERTICAL


def test_flat_pipe_is_skipped():
    # 10 m dead level (2 mm drift is below the 5 mm threshold).
    assert core.classify_pipe((0, 0, 0), (10000, 0, 2), CFG) == core.FLAT


def test_graded_drainage_pipe_is_sloped():
    # 10 m at a 1:100 fall - the everyday drainage case.
    assert core.classify_pipe((0, 0, 100), (10000, 0, 0), CFG) == core.SLOPED


def test_min_length_uses_3d_length():
    # 900 mm plan run with a 500 mm drop is over 1 m along the pipe.
    assert core.classify_pipe((0, 0, 500), (900, 0, 0), CFG) != core.TOO_SHORT


# ---------------------------------------------------------------------------
# Flow direction (higher -> lower)
# ---------------------------------------------------------------------------
def test_flow_points_from_high_to_low():
    direction = core.flow_direction((0, 0, 0), (10000, 0, -100))
    assert direction[0] > 0 and direction[2] < 0


def test_flow_ignores_start_end_order():
    # Same pipe drawn the other way round: flow must not change.
    a = core.flow_direction((0, 0, 100), (10000, 0, 0))
    b = core.flow_direction((10000, 0, 0), (0, 0, 100))
    assert a == pytest.approx(b)


def test_flow_direction_is_unit_length():
    direction = core.flow_direction((0, 0, 100), (8000, 3000, 0))
    assert core.length(direction) == pytest.approx(1.0)


def test_oriented_endpoints_orders_by_elevation():
    high, low = core.oriented_endpoints((0, 0, 0), (5000, 0, 50))
    assert high == (5000, 0, 50)
    assert low == (0, 0, 0)


# ---------------------------------------------------------------------------
# Arrow stations
# ---------------------------------------------------------------------------
def test_short_pipe_gets_one_midpoint_arrow():
    assert core.arrow_stations(8000.0, CFG) == [4000.0]


def test_threshold_pipe_still_gets_one_arrow():
    assert core.arrow_stations(15000.0, CFG) == [7500.0]


def test_long_pipe_gets_one_arrow_per_started_threshold():
    stations = core.arrow_stations(40000.0, CFG)
    assert len(stations) == 3  # ceil(40 / 15)


def test_long_pipe_stations_respect_end_clearance():
    stations = core.arrow_stations(40000.0, CFG)
    for s in stations:
        assert CFG['end_clearance_mm'] <= s <= 40000.0 - CFG['end_clearance_mm']
    assert stations == sorted(stations)


def test_long_pipe_stations_are_symmetric():
    # Mirroring the pipe end-for-end must give the same arrow spots.
    stations = core.arrow_stations(40000.0, CFG)
    mirrored = sorted(40000.0 - s for s in stations)
    assert stations == pytest.approx(mirrored)


def test_stations_fall_back_to_midpoint_when_clearance_eats_the_pipe():
    tight = dict(CFG)
    tight['multi_arrow_threshold_mm'] = 1000.0
    # 1.5 m pipe, 1 m clearance each end: no usable span, keep the midpoint.
    assert core.arrow_stations(1500.0, tight) == [750.0]


def test_arrow_points_interpolate_on_the_centreline():
    high, low = (0.0, 0.0, 1000.0), (10000.0, 0.0, 0.0)
    span = core.distance(high, low)
    points = core.arrow_points(high, low, [span / 2.0])
    assert points[0] == pytest.approx((5000.0, 0.0, 500.0))


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------
def test_existing_arrow_within_tolerance_is_a_duplicate():
    existing = [(5000.0, 200.0, 0.0)]
    assert core.is_near_existing((5000.0, 0.0, 0.0), existing, 300.0)


def test_existing_arrow_outside_tolerance_is_not_a_duplicate():
    existing = [(5000.0, 400.0, 0.0)]
    assert not core.is_near_existing((5000.0, 0.0, 0.0), existing, 300.0)


def test_no_existing_arrows_is_never_a_duplicate():
    assert not core.is_near_existing((0.0, 0.0, 0.0), [], 300.0)


# ---------------------------------------------------------------------------
# Rotation angles
# ---------------------------------------------------------------------------
def test_plan_angle_measures_from_x_axis():
    assert core.plan_angle((1.0, 0.0, 0.0)) == pytest.approx(0.0)
    assert core.plan_angle((0.0, 1.0, 0.0)) == pytest.approx(math.pi / 2.0)
    assert core.plan_angle((-1.0, 0.0, 0.0)) == pytest.approx(math.pi)


def test_tilt_angle_is_positive_downhill():
    direction = core.flow_direction((0, 0, 100), (10000, 0, 0))
    tilt = core.tilt_angle(direction)
    assert tilt > 0.0
    assert tilt == pytest.approx(math.atan2(100.0, 10000.0))


def test_tilt_angle_is_zero_for_level_direction():
    assert core.tilt_angle((1.0, 0.0, 0.0)) == pytest.approx(0.0)
