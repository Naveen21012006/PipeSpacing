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
    assert core.arrow_stations(10000.0, CFG) == [5000.0]


def test_just_over_threshold_gets_two_arrows():
    assert len(core.arrow_stations(12000.0, CFG)) == 2


def test_long_pipe_gets_one_arrow_per_started_threshold():
    stations = core.arrow_stations(40000.0, CFG)
    assert len(stations) == 4  # ceil(40 / 10)


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


# ---------------------------------------------------------------------------
# Left/right side for tag-based arrow families
# ---------------------------------------------------------------------------
def test_flow_to_screen_right_uses_right_type():
    assert core.arrow_side(1.0, 0.0) == core.RIGHT


def test_flow_to_screen_left_uses_left_type():
    assert core.arrow_side(-1.0, 0.0) == core.LEFT


def test_diagonal_flow_follows_its_horizontal_component():
    assert core.arrow_side(0.7, -0.7) == core.RIGHT
    assert core.arrow_side(-0.7, 0.7) == core.LEFT


def test_vertical_screen_flow_reads_bottom_to_top():
    # Revit draws a vertical pipe's tag reading bottom-to-top, so upward
    # flow matches the readable (Right) head and downward flips to Left.
    assert core.arrow_side(0.0, 1.0) == core.RIGHT
    assert core.arrow_side(0.0, -1.0) == core.LEFT


# ---------------------------------------------------------------------------
# Parallel racks - shared arrow columns
# ---------------------------------------------------------------------------
def _pipe(x0, y, x1, z0=100.0, z1=0.0):
    """A sloped pipe running along X at offset y, higher end first."""
    return ((x0, y, z0), (x1, y, z1))


def test_parallel_neighbours_share_a_column():
    # Staggered segments 300 mm apart: arrows must line up, not sit at
    # each pipe's own midpoint (the staggered-arrows screenshot case).
    pipes = [_pipe(0.0, 0.0, 8000.0), _pipe(1000.0, 300.0, 9000.0)]
    points, racks = core.bundle_arrow_points(pipes, dict(CFG))
    assert racks == [[0, 1]]
    assert points[0][0][0] == pytest.approx(points[1][0][0])


def test_lone_pipe_keeps_its_own_midpoint():
    pipes = [_pipe(0.0, 0.0, 8000.0)]
    points, racks = core.bundle_arrow_points(pipes, dict(CFG))
    assert racks == []
    assert points[0][0][0] == pytest.approx(4000.0)


def test_wide_or_perpendicular_pipes_do_not_cluster():
    wide = [_pipe(0.0, 0.0, 8000.0), _pipe(0.0, 1000.0, 8000.0)]
    assert core.bundle_arrow_points(wide, dict(CFG))[1] == []
    perpendicular = [_pipe(0.0, 0.0, 8000.0),
                     ((4000.0, -3000.0, 100.0), (4000.0, 3000.0, 0.0))]
    assert core.bundle_arrow_points(perpendicular, dict(CFG))[1] == []


def test_collinear_end_to_end_segments_do_not_cluster():
    pipes = [_pipe(0.0, 0.0, 8000.0), _pipe(8000.0, 0.0, 16000.0)]
    points, racks = core.bundle_arrow_points(pipes, dict(CFG))
    assert racks == []
    assert points[0][0][0] == pytest.approx(4000.0)
    assert points[1][0][0] == pytest.approx(12000.0)


def test_racks_chain_transitively():
    pipes = [_pipe(0.0, 0.0, 8000.0),
             _pipe(0.0, 500.0, 8000.0),
             _pipe(0.0, 1000.0, 8000.0)]   # 0-2 are 1 m apart, chained via 1
    points, racks = core.bundle_arrow_points(pipes, dict(CFG))
    assert racks == [[0, 1, 2]]
    xs = [pts[0][0] for pts in points]
    assert xs[0] == pytest.approx(xs[1])
    assert xs[1] == pytest.approx(xs[2])


def test_long_rack_gets_aligned_columns_per_threshold():
    pipes = [_pipe(0.0, 0.0, 25000.0), _pipe(0.0, 300.0, 25000.0)]
    points, racks = core.bundle_arrow_points(pipes, dict(CFG))
    assert racks == [[0, 1]]
    assert len(points[0]) == 3          # ceil(25 / 10) on the overlap
    for a, b in zip(points[0], points[1]):
        assert a[0] == pytest.approx(b[0])


def test_member_that_cannot_reach_the_column_is_nudged():
    # The pipes only overlap over their last 1.5 m, so the shared column
    # (overlap midpoint, x=750) falls inside both pipes' end clearance -
    # each arrow is nudged to its nearest valid spot, never dropped.
    pipes = [_pipe(0.0, 0.0, 20000.0), _pipe(-8000.0, 300.0, 1500.0)]
    points, racks = core.bundle_arrow_points(pipes, dict(CFG))
    assert racks == [[0, 1]]
    # abs=1: the clearance fraction uses the 3D length, x is its plan
    # projection, so a sloped pipe lands a hair inside the exact limit.
    assert points[0][0][0] == pytest.approx(1000.0, abs=1.0)  # at clearance
    assert points[1][0][0] == pytest.approx(500.0, abs=1.0)   # 1 m from end
    assert len(points[0]) == 1 and len(points[1]) == 1


def test_opposite_flows_still_align():
    # One pipe drains left-to-right, its neighbour right-to-left: the
    # arrow positions still form one column.
    a = ((0.0, 0.0, 100.0), (8000.0, 0.0, 0.0))
    b = ((8000.0, 300.0, 100.0), (0.0, 300.0, 0.0))
    points, racks = core.bundle_arrow_points([a, b], dict(CFG))
    assert racks == [[0, 1]]
    assert points[0][0][0] == pytest.approx(points[1][0][0])


def test_rack_width_zero_disables_clustering():
    config = dict(CFG)
    config['rack_width_mm'] = 0.0
    pipes = [_pipe(0.0, 0.0, 8000.0), _pipe(1000.0, 300.0, 9000.0)]
    points, racks = core.bundle_arrow_points(pipes, config)
    assert racks == []
    assert points[0][0][0] == pytest.approx(4000.0)
    assert points[1][0][0] == pytest.approx(5000.0)


def test_arrow_z_stays_on_each_pipes_own_centreline():
    pipes = [_pipe(0.0, 0.0, 8000.0, z0=200.0, z1=0.0),
             _pipe(0.0, 300.0, 8000.0, z0=100.0, z1=0.0)]
    points, racks = core.bundle_arrow_points(pipes, dict(CFG))
    assert racks == [[0, 1]]
    # Same station, but each arrow keeps its own pipe's elevation there.
    assert points[0][0][2] == pytest.approx(100.0)
    assert points[1][0][2] == pytest.approx(50.0)
