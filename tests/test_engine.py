# -*- coding: utf-8 -*-
"""Unit tests for the Align Tags geometry engine.

The heart of the suite is a brute-force non-crossing property check run over
the full mode x constant-landing x intermittent x switch-side matrix with
several deterministic random layouts - if any two leader polylines intersect,
the sort-key derivation in the engine is wrong.
"""

import math
import random

import pytest

import engine


ALL_MODES = list(engine.MODES)
EPS = 1e-9


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _angle_of(segment_start, segment_end):
    """Return the absolute slant angle of a segment from horizontal, degrees."""
    du = segment_end[0] - segment_start[0]
    dv = segment_end[1] - segment_start[1]
    return abs(math.degrees(math.atan2(abs(dv), abs(du))))


def _horizontal_vs_slant_cross(h_seg, s_seg):
    """True if a horizontal segment strictly crosses another segment.

    Specialised: h_seg is horizontal by construction (a landing). Touching at
    endpoints does not count; only a proper interior crossing does.
    """
    (h1, h2) = h_seg
    v = h1[1]
    lo_u, hi_u = min(h1[0], h2[0]), max(h1[0], h2[0])
    (s1, s2) = s_seg
    lo_v, hi_v = min(s1[1], s2[1]), max(s1[1], s2[1])
    if not (lo_v + EPS < v < hi_v - EPS):
        return False
    if abs(s2[1] - s1[1]) < EPS:
        return False
    t = (v - s1[1]) / (s2[1] - s1[1])
    u = s1[0] + t * (s2[0] - s1[0])
    return lo_u + EPS < u < hi_u - EPS


def _any_leaders_cross(plan):
    """Brute-force check: does any leader segment cross another tag's?

    Landings are horizontal and at distinct heights, so landing-landing
    crossings are impossible; slants are parallel by construction, so
    slant-slant crossings are impossible. That leaves landing-vs-slant
    (checked both ways for safety).
    """
    for i, a in enumerate(plan):
        for j, b in enumerate(plan):
            if i == j:
                continue
            landing_a = (a['head'], a['elbow'])
            for seg in engine.leader_segments(b):
                if _horizontal_vs_slant_cross(landing_a, seg):
                    return True
    return False


def _make_items(mode, count, seed, anchor=(0.0, 0.0)):
    """Deterministic well-posed ends in the mode's element quadrant.

    Ends sit 60-100 units into the exit side and 10-40 units on the element
    side vertically, far enough out that every leader can honour the angle
    (no angle_ok=False) for the spacings used in the matrix test.
    """
    rng = random.Random(seed)
    du_sign = engine.exit_sign(mode)
    dv_sign = engine.slant_sign(mode)
    items = []
    for k in range(count):
        du = rng.uniform(60.0, 100.0) * du_sign
        dv = rng.uniform(10.0, 40.0) * dv_sign
        items.append({'key': k, 'end': (anchor[0] + du, anchor[1] + dv)})
    return items


# ---------------------------------------------------------------------------
# Small pieces
# ---------------------------------------------------------------------------
def test_clamp_angle_passes_normal_values():
    assert engine.clamp_angle(45.0) == 45.0
    assert engine.clamp_angle(30) == 30.0


def test_clamp_angle_clamps_out_of_range():
    assert engine.clamp_angle(0) == engine.MIN_ANGLE_DEG
    assert engine.clamp_angle(-5) == engine.MIN_ANGLE_DEG
    assert engine.clamp_angle(90) == engine.MAX_ANGLE_DEG
    assert engine.clamp_angle(180) == engine.MAX_ANGLE_DEG


def test_clamp_angle_survives_garbage():
    assert engine.clamp_angle(None) == 45.0
    assert engine.clamp_angle('not a number') == 45.0


def test_resolve_mode_identity_and_switch():
    assert engine.resolve_mode(engine.UPPER_LEFT) == engine.UPPER_LEFT
    assert engine.resolve_mode(engine.UPPER_LEFT, True) == engine.UPPER_RIGHT
    assert engine.resolve_mode(engine.LOWER_RIGHT, True) == engine.LOWER_LEFT


def test_resolve_mode_rejects_unknown():
    with pytest.raises(ValueError):
        engine.resolve_mode('diagonal')


def test_signs_per_mode():
    assert engine.exit_sign(engine.UPPER_LEFT) == 1.0
    assert engine.exit_sign(engine.LOWER_LEFT) == 1.0
    assert engine.exit_sign(engine.UPPER_RIGHT) == -1.0
    assert engine.exit_sign(engine.LOWER_RIGHT) == -1.0
    assert engine.slant_sign(engine.UPPER_LEFT) == -1.0
    assert engine.slant_sign(engine.UPPER_RIGHT) == -1.0
    assert engine.slant_sign(engine.LOWER_LEFT) == 1.0
    assert engine.slant_sign(engine.LOWER_RIGHT) == 1.0


# ---------------------------------------------------------------------------
# elbow_for
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('mode', ALL_MODES)
@pytest.mark.parametrize('angle', [15.0, 45.0, 75.0])
def test_elbow_for_honours_angle_and_landing(mode, angle):
    head = (0.0, 0.0)
    # Place the end far enough out that the slant back at `angle` still
    # leaves a positive landing (shallow angles need a longer reach).
    reach = 20.0 / math.tan(math.radians(angle)) + 30.0
    end = (engine.exit_sign(mode) * reach, engine.slant_sign(mode) * 20.0)
    elbow, angle_ok = engine.elbow_for(head, end, mode, angle)

    assert angle_ok
    # Landing is horizontal, on the exit side.
    assert elbow[1] == pytest.approx(head[1])
    assert (elbow[0] - head[0]) * engine.exit_sign(mode) >= 0.0
    # Slant honours the configured angle.
    assert _angle_of(elbow, end) == pytest.approx(angle, abs=1e-6)


@pytest.mark.parametrize('mode', ALL_MODES)
def test_elbow_for_flags_end_behind_head(mode):
    head = (0.0, 0.0)
    # End on the WRONG horizontal side: landing would extend backwards.
    end = (-engine.exit_sign(mode) * 50.0, engine.slant_sign(mode) * 20.0)
    elbow, angle_ok = engine.elbow_for(head, end, mode, 45.0)
    assert not angle_ok
    assert elbow == (0.0, 0.0)


@pytest.mark.parametrize('mode', ALL_MODES)
def test_elbow_for_flags_end_on_stack_side(mode):
    head = (0.0, 0.0)
    # End on the WRONG vertical side (above an upper stack / below a lower).
    end = (engine.exit_sign(mode) * 50.0, -engine.slant_sign(mode) * 20.0)
    _, angle_ok = engine.elbow_for(head, end, mode, 45.0)
    assert not angle_ok


def test_elbow_for_end_level_with_head_is_ok():
    # Exactly level: elbow coincides with the end, angle degenerate but legal.
    elbow, angle_ok = engine.elbow_for((0.0, 0.0), (30.0, 0.0),
                                       engine.UPPER_LEFT, 45.0)
    assert angle_ok
    assert elbow == pytest.approx((30.0, 0.0))


# ---------------------------------------------------------------------------
# plan_alignment - basic shape
# ---------------------------------------------------------------------------
def test_plan_empty_items_returns_empty():
    assert engine.plan_alignment((0, 0), [], engine.UPPER_LEFT, 45,
                                 2.0, 4.0, 3.0) == []


def test_plan_preserves_input_order_and_keys():
    items = _make_items(engine.UPPER_LEFT, 6, seed=1)
    plan = engine.plan_alignment((0, 0), items, engine.UPPER_LEFT, 45,
                                 2.0, 4.0, 3.0)
    assert [p['key'] for p in plan] == [i['key'] for i in items]
    assert sorted(p['row'] for p in plan) == list(range(6))


@pytest.mark.parametrize('mode', ALL_MODES)
def test_plan_stacks_heads_from_anchor(mode):
    anchor = (5.0, -3.0)
    items = _make_items(mode, 5, seed=2, anchor=anchor)
    plan = engine.plan_alignment(anchor, items, mode, 45, 2.0, 4.0, 3.0)

    by_row = sorted(plan, key=lambda p: p['row'])
    for row, entry in enumerate(by_row):
        assert entry['head'][1] == pytest.approx(anchor[1] + row * 2.0)
        assert entry['head'][0] == pytest.approx(anchor[0])
        assert entry['angle_ok']


@pytest.mark.parametrize('mode', ALL_MODES)
def test_plan_slants_all_parallel_at_angle(mode):
    items = _make_items(mode, 7, seed=3)
    plan = engine.plan_alignment((0, 0), items, mode, 60.0, 2.0, 4.0, 3.0)
    for entry in plan:
        assert entry['angle_ok']
        assert _angle_of(entry['elbow'], entry['end']) == pytest.approx(
            60.0, abs=1e-6)


def test_plan_ends_never_move():
    items = _make_items(engine.LOWER_RIGHT, 5, seed=4)
    plan = engine.plan_alignment((0, 0), items, engine.LOWER_RIGHT, 45,
                                 2.0, 4.0, 3.0)
    for entry, item in zip(plan, items):
        assert entry['end'] == pytest.approx(item['end'])


def test_plan_switch_side_mirrors_exit():
    items_l = _make_items(engine.UPPER_RIGHT, 4, seed=5)
    plan = engine.plan_alignment((0, 0), items_l, engine.UPPER_LEFT, 45,
                                 2.0, 4.0, 3.0, switch_side=True)
    # Effective mode is UPPER_RIGHT: elbows strictly LEFT of the heads (the
    # angle_ok guard rules out the trivially-passing collapsed-elbow case).
    for entry in plan:
        assert entry['angle_ok']
        assert entry['elbow'][0] < entry['head'][0] - EPS


def test_plan_non_positive_spacing_still_stacks():
    items = _make_items(engine.UPPER_LEFT, 3, seed=6)
    plan = engine.plan_alignment((0, 0), items, engine.UPPER_LEFT, 45,
                                 0.0, 4.0, 3.0)
    heights = sorted(p['head'][1] for p in plan)
    assert heights[0] < heights[1] < heights[2]


def test_plan_flags_degenerate_end():
    # One end deliberately BEHIND the stack; it must be flagged, not crash.
    items = [
        {'key': 'good', 'end': (80.0, -20.0)},
        {'key': 'bad', 'end': (-80.0, -20.0)},
    ]
    plan = engine.plan_alignment((0, 0), items, engine.UPPER_LEFT, 45,
                                 2.0, 4.0, 3.0)
    flags = dict((p['key'], p['angle_ok']) for p in plan)
    assert flags['good'] is True
    assert flags['bad'] is False
    bad = [p for p in plan if p['key'] == 'bad'][0]
    assert bad['elbow'] == pytest.approx(bad['head'])


def test_plan_tied_ends_keep_input_order():
    # Identical ends tie on the sort key; the documented tie-break is input
    # order (stable sort), so rows must come out 0, 1, 2 in input order.
    items = [{'key': k, 'end': (70.0, -20.0)} for k in range(3)]
    plan = engine.plan_alignment((0, 0), items, engine.UPPER_LEFT, 45,
                                 2.0, 4.0, 3.0)
    assert [p['row'] for p in plan] == [0, 1, 2]


# ---------------------------------------------------------------------------
# Constant landing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('mode', ALL_MODES)
def test_constant_landing_lengths_equal(mode):
    items = _make_items(mode, 6, seed=7)
    plan = engine.plan_alignment((0, 0), items, mode, 45, 2.0, 4.0, 3.0,
                                 constant_landing=True)
    for entry in plan:
        assert entry['angle_ok']
        landing = abs(entry['elbow'][0] - entry['head'][0])
        assert landing == pytest.approx(4.0)
        assert entry['elbow'][1] == pytest.approx(entry['head'][1])
        assert _angle_of(entry['elbow'], entry['end']) == pytest.approx(
            45.0, abs=1e-6)


def test_constant_landing_flags_wrong_side_end():
    items = [{'key': 0, 'end': (80.0, 50.0)}]  # above an UPPER stack
    plan = engine.plan_alignment((0, 0), items, engine.UPPER_LEFT, 45,
                                 2.0, 4.0, 3.0, constant_landing=True)
    assert plan[0]['angle_ok'] is False
    assert plan[0]['elbow'] == pytest.approx(plan[0]['head'])
    # The degenerate head must stay on the anchor column, not fly off along
    # the extrapolated angle-line (review finding).
    assert plan[0]['head'][0] == pytest.approx(0.0)


def test_constant_landing_overrides_intermittent():
    # With both flags on, constant landing wins: full row step, no stagger,
    # every landing still exactly the landing distance.
    items = _make_items(engine.UPPER_LEFT, 4, seed=13)
    plan = engine.plan_alignment((0, 0), items, engine.UPPER_LEFT, 45,
                                 2.0, 4.0, 3.0,
                                 constant_landing=True, intermittent=True)
    by_row = sorted(plan, key=lambda p: p['row'])
    for row, entry in enumerate(by_row):
        assert entry['head'][1] == pytest.approx(row * 2.0)  # NOT halved
        assert abs(entry['elbow'][0] - entry['head'][0]) == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# Intermittent alignment
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('mode', ALL_MODES)
def test_intermittent_two_columns_half_step(mode):
    anchor = (0.0, 0.0)
    items = _make_items(mode, 6, seed=8)
    plan = engine.plan_alignment(anchor, items, mode, 45, 2.0, 4.0, 3.0,
                                 intermittent=True)
    by_row = sorted(plan, key=lambda p: p['row'])
    away = -engine.exit_sign(mode)
    for row, entry in enumerate(by_row):
        assert entry['head'][1] == pytest.approx(row * 1.0)  # half of 2.0
        expected_u = anchor[0] + (3.0 * away if row % 2 else 0.0)
        assert entry['head'][0] == pytest.approx(expected_u)


def test_intermittent_same_column_keeps_full_spacing():
    items = _make_items(engine.UPPER_LEFT, 6, seed=9)
    plan = engine.plan_alignment((0, 0), items, engine.UPPER_LEFT, 45,
                                 2.0, 4.0, 3.0, intermittent=True)
    col0 = sorted(p['head'][1] for p in plan if p['row'] % 2 == 0)
    for lower, upper in zip(col0, col0[1:]):
        assert upper - lower == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# The non-crossing property - full matrix
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('mode', ALL_MODES)
@pytest.mark.parametrize('constant_landing', [False, True])
@pytest.mark.parametrize('intermittent', [False, True])
@pytest.mark.parametrize('switch_side', [False, True])
@pytest.mark.parametrize('seed', [11, 23, 47])
def test_leaders_never_cross(mode, constant_landing, intermittent,
                             switch_side, seed):
    # switch_side mirrors the effective mode, so generate the ends for the
    # mode that will actually be used.
    effective = engine.resolve_mode(mode, switch_side)
    items = _make_items(effective, 8, seed=seed)
    plan = engine.plan_alignment((0.0, 0.0), items, mode, 45.0,
                                 2.0, 4.0, 3.0,
                                 constant_landing=constant_landing,
                                 intermittent=intermittent,
                                 switch_side=switch_side)
    assert all(p['angle_ok'] for p in plan), 'layout unexpectedly degenerate'
    assert not _any_leaders_cross(plan)


@pytest.mark.parametrize('mode', ALL_MODES)
@pytest.mark.parametrize('angle', [30.0, 60.0])
def test_leaders_never_cross_other_angles(mode, angle):
    items = _make_items(mode, 8, seed=31)
    plan = engine.plan_alignment((0.0, 0.0), items, mode, angle,
                                 2.0, 4.0, 3.0)
    assert all(p['angle_ok'] for p in plan)
    assert not _any_leaders_cross(plan)


# ---------------------------------------------------------------------------
# plan_ordered - order by pipe position, ends derived
# ---------------------------------------------------------------------------
def _v_items(positions, span=(-300.0, 100.0)):
    """Vertical-bundle items (pipes side by side) from pipe u positions."""
    return [{'key': k, 'pos': p, 'span': span}
            for k, p in enumerate(positions)]


def _h_items(positions, span=(0.0, 400.0)):
    """Horizontal-bundle items (pipes stacked) from pipe v positions."""
    return [{'key': k, 'pos': p, 'span': span}
            for k, p in enumerate(positions)]


def test_ordered_empty_and_bad_bundle():
    assert engine.plan_ordered((0, 0), [], engine.UPPER_LEFT, 45,
                               2.0, 4.0, 3.0, 'v') == []
    with pytest.raises(ValueError):
        engine.plan_ordered((0, 0), _v_items([40.0]), engine.UPPER_LEFT, 45,
                            2.0, 4.0, 3.0, 'diagonal')


def test_ordered_v_reads_left_to_right():
    # Input deliberately shuffled; the LEFTMOST pipe must get the TOP tag.
    plan = engine.plan_ordered((0, 0), _v_items([70.0, 40.0, 85.0, 55.0]),
                               engine.UPPER_LEFT, 45, 2.0, 4.0, 3.0, 'v')
    rows = dict((p['end'][0], p['row']) for p in plan)  # pipe u -> row
    assert rows[40.0] == 3 and rows[55.0] == 2
    assert rows[70.0] == 1 and rows[85.0] == 0
    assert [p['key'] for p in plan] == [0, 1, 2, 3]  # input order kept


def test_ordered_h_mirrors_pipe_order():
    # Horizontal pipes below an UPPER stack: topmost pipe gets the top tag.
    plan = engine.plan_ordered((0, 0), _h_items([-20.0, -60.0, -40.0]),
                               engine.UPPER_LEFT, 45, 2.0, 4.0, 3.0, 'h')
    rows = dict((p['end'][1], p['row']) for p in plan)  # pipe v -> row
    assert rows[-60.0] == 0 and rows[-40.0] == 1 and rows[-20.0] == 2


@pytest.mark.parametrize('bundle,positions', [
    ('v', [40.0, 55.0, 70.0, 85.0]),
    ('h', [-70.0, -55.0, -40.0, -25.0]),
])
def test_ordered_geometry_exact(bundle, positions):
    items = _v_items(positions) if bundle == 'v' else _h_items(positions)
    plan = engine.plan_ordered((0.0, 0.0), items, engine.UPPER_LEFT, 45,
                               2.0, 4.0, 3.0, bundle)
    for entry in plan:
        assert entry['angle_ok']
        # Landing exactly the set length, horizontal.
        assert entry['elbow'][1] == pytest.approx(entry['head'][1])
        assert abs(entry['elbow'][0] - entry['head'][0]) == pytest.approx(4.0)
        # Slant exactly at the angle, arrow ON the pipe line.
        assert _angle_of(entry['elbow'], entry['end']) == pytest.approx(
            45.0, abs=1e-6)
    assert not _any_leaders_cross(plan)


def test_ordered_ends_land_on_their_pipes():
    positions = [40.0, 60.0, 80.0]
    plan = engine.plan_ordered((0.0, 0.0), _v_items(positions),
                               engine.UPPER_LEFT, 45, 2.0, 4.0, 3.0, 'v')
    assert sorted(p['end'][0] for p in plan) == positions


def test_ordered_clamps_to_pipe_span():
    # Pipe extent far too short for the slant: arrow clamps, flag raised.
    items = [{'key': 0, 'pos': 80.0, 'span': (-5.0, 100.0)}]
    plan = engine.plan_ordered((0.0, 0.0), items, engine.UPPER_LEFT, 45,
                               2.0, 4.0, 3.0, 'v')
    assert plan[0]['angle_ok'] is False
    assert plan[0]['end'] == pytest.approx((80.0, -5.0))


def test_ordered_flags_pipe_behind_stack():
    plan = engine.plan_ordered((0.0, 0.0), _v_items([-40.0]),
                               engine.UPPER_LEFT, 45, 2.0, 4.0, 3.0, 'v')
    assert plan[0]['angle_ok'] is False
    assert plan[0]['end'] == pytest.approx(plan[0]['elbow'])


def test_ordered_switch_side_keeps_left_to_right():
    # Effective UPPER_RIGHT (exit left), pipes LEFT of the stack: the
    # leftmost pipe still takes the top tag - reading order is universal.
    plan = engine.plan_ordered((0.0, 0.0), _v_items([-85.0, -40.0, -60.0]),
                               engine.UPPER_LEFT, 45, 2.0, 4.0, 3.0, 'v',
                               switch_side=True)
    rows = dict((p['end'][0], p['row']) for p in plan)
    assert rows[-85.0] == 2 and rows[-60.0] == 1 and rows[-40.0] == 0
    for entry in plan:
        assert entry['angle_ok']
        assert entry['elbow'][0] < entry['head'][0]  # exits left
    assert not _any_leaders_cross(plan)


def test_normalize_angle():
    assert engine.normalize_angle(0) == 0.0
    assert engine.normalize_angle(0.4) == 0.0
    assert engine.normalize_angle(0.6) == engine.MIN_ANGLE_DEG
    assert engine.normalize_angle(45) == 45.0
    assert engine.normalize_angle(95) == engine.MAX_ANGLE_DEG
    assert engine.normalize_angle('junk') == 45.0


def test_ordered_corner_anchor_offsets():
    # The anchor is the far-bottom text corner; head and exit edge are
    # per-item offsets from it. Landing starts at the exit edge.
    items = [{'key': 0, 'pos': 80.0, 'span': (-300.0, 100.0),
              'head_offset': (7.0, 3.0), 'exit_edge': 20.0}]
    plan = engine.plan_ordered((10.0, 5.0), items, engine.UPPER_LEFT, 45,
                               2.0, 4.0, 3.0, 'v')
    entry = plan[0]
    assert entry['head'] == pytest.approx((17.0, 8.0))
    assert entry['elbow'] == pytest.approx((34.0, 8.0))  # 10 + 20 + 4
    assert entry['angle_ok']
    assert _angle_of(entry['elbow'], entry['end']) == pytest.approx(
        45.0, abs=1e-6)


def test_ordered_line_offset_sets_leader_height():
    # The leader line runs at the text mid-height (line_offset), not the
    # head height - heads and landings can sit at different heights.
    items = [{'key': 0, 'pos': 80.0, 'span': (-300.0, 100.0),
              'head_offset': (7.0, 3.0), 'line_offset': 1.5,
              'exit_edge': 20.0}]
    plan = engine.plan_ordered((10.0, 5.0), items, engine.UPPER_LEFT, 0.0,
                               2.0, 4.0, 3.0, 'v')
    entry = plan[0]
    assert entry['head'] == pytest.approx((17.0, 8.0))
    assert entry['elbow'][1] == pytest.approx(6.5)   # 5 + 1.5
    assert entry['elbow'][0] == pytest.approx(55.0)  # mid of edge 30..pipe 80
    assert entry['end'] == pytest.approx((80.0, 6.5))
    assert entry['straight'] is True


def test_ordered_straight_leaders_at_zero_angle():
    # Angle 0 on a vertical bundle: horizontal, elbow-free leaders, arrow
    # at the tag's own height, order still left-to-right.
    plan = engine.plan_ordered((0.0, 0.0), _v_items([70.0, 40.0]),
                               engine.UPPER_LEFT, 0.0, 2.0, 4.0, 3.0, 'v')
    rows = dict((p['end'][0], p['row']) for p in plan)
    assert rows[40.0] == 1 and rows[70.0] == 0
    for entry in plan:
        assert entry['straight'] is True
        assert entry['angle_ok']
        assert entry['end'][1] == pytest.approx(entry['head'][1])
        assert entry['elbow'][1] == pytest.approx(entry['head'][1])
        # Elbow grip parked at the line's MIDPOINT (exit edge is 0 here).
        assert entry['elbow'][0] == pytest.approx(entry['end'][0] / 2.0)


def test_ordered_climb_when_pipe_misses_tag_height():
    # Stack taller than the pipe: the tag whose horizontal would miss the
    # pipe gets a TILTED CLIMB down onto it (intended geometry, not a
    # failure); the rest stay straight.
    items = [
        {'key': 'fits', 'pos': 60.0, 'span': (-100.0, 100.0)},
        {'key': 'over', 'pos': 40.0, 'span': (-100.0, 1.0)},
    ]
    plan = engine.plan_ordered((0.0, 0.0), items, engine.UPPER_LEFT, 0.0,
                               2.0, 4.0, 3.0, 'v')
    by_key = dict((p['key'], p) for p in plan)
    assert by_key['fits']['straight'] is True
    assert by_key['fits']['angle_ok'] is True
    over = by_key['over']       # leftmost pipe -> top row, line_v = 2.0
    assert over['straight'] is True   # the climb IS the intended shape
    assert over['angle_ok'] is True
    assert over['end'][0] == pytest.approx(40.0)      # arrow on the pipe
    assert over['end'][1] < over['head'][1]           # climbs down to it
    clearance = 0.05 * 101.0
    assert -100.0 + clearance <= over['end'][1] <= 1.0 - clearance
    assert _tilt_of(over) == pytest.approx(engine.TILT_DEG, abs=1e-6)


def _tilt_of(entry):
    """Climb segment's lean from VERTICAL, in degrees."""
    du = entry['end'][0] - entry['elbow'][0]
    dv = entry['end'][1] - entry['elbow'][1]
    return abs(math.degrees(math.atan2(abs(du), abs(dv))))


def test_ordered_fan_spacing_floors_at_clearance():
    # Short runs used to collapse the fan to a sliver; with an explicit
    # clearance the spacing floors there, spilling past the 60% window
    # into the near band (still on the pipe).
    plan = engine.plan_ordered((0.0, 0.0), _h_items([-60.0, -50.0, -40.0],
                                                    span=(0.0, 3.0)),
                               engine.UPPER_LEFT, 0.0, 2.0, 4.0, 3.0, 'h',
                               clearance=0.5)
    turns = sorted(p['end'][0] for p in plan)
    # Margin caps at 15% of the extent (0.45); steps floor at the
    # clearance (0.5) since the band affords it.
    assert turns[0] == pytest.approx(0.45)
    assert turns[1] - turns[0] == pytest.approx(0.5)
    assert turns[2] - turns[1] == pytest.approx(0.5)
    assert turns[2] <= 3.0 - 0.45


def test_ordered_short_corner_jog_stays_readable():
    # The user's corner regression in real units (feet): a ~700mm jog,
    # 250mm clearance, three tags. The old margins ate the pipe and left
    # 67mm between climbs; capped margins must yield >= ~150mm.
    plan = engine.plan_ordered(
        (0.0, 0.0), _h_items([-3.0, -2.5, -2.0], span=(0.0, 2.3)),
        engine.UPPER_LEFT, 0.0, 1.46, 4.0, 3.0, 'h', clearance=0.82)
    turns = sorted(p['end'][0] for p in plan)
    gap = min(turns[1] - turns[0], turns[2] - turns[1])
    assert gap >= 0.82 * 0.6        # >= ~150mm between climbs
    assert turns[0] >= 0.0 and turns[2] <= 2.3   # on the pipe


def test_ordered_mixed_stack_fan_starts_at_window_start():
    # Regression (user: "using only the extreme right end"): in a mixed
    # 3-drops + 3-runs stack, the run tags sit at the BOTTOM rows; global-
    # row nearness gave them 3..5 instead of 0..2, overshooting the window
    # and clamping every turn onto the pipes' far end. Sub-group ranking
    # must give the runs steps 0, 1, 2 from the window start.
    items = [
        {'key': 'd1', 'own': 'v', 'pos': 10.0, 'span': (50.0, 200.0)},
        {'key': 'd2', 'own': 'v', 'pos': 20.0, 'span': (50.0, 200.0)},
        {'key': 'd3', 'own': 'v', 'pos': 30.0, 'span': (50.0, 200.0)},
        {'key': 'r1', 'own': 'h', 'pos': 60.0, 'span': (40.0, 100.0)},
        {'key': 'r2', 'own': 'h', 'pos': 55.0, 'span': (40.0, 100.0)},
        {'key': 'r3', 'own': 'h', 'pos': 50.0, 'span': (40.0, 100.0)},
    ]
    plan = engine.plan_ordered((0.0, 0.0), items, engine.UPPER_LEFT, 0.0,
                               2.0, 4.0, 3.0, 'v')
    runs = [p for p in plan if p['key'].startswith('r')]
    turns = sorted(p['end'][0] for p in runs)
    margin = 0.05 * 60.0            # default margin, 15%-cap not binding
    window_start = 40.0 + margin
    assert turns[0] == pytest.approx(window_start)   # step 0 exists!
    assert turns[1] - turns[0] == pytest.approx(turns[2] - turns[1])
    assert turns[2] < 100.0         # nowhere near the far end
    assert not _any_leaders_cross(plan)


def test_ordered_long_run_mid_span_pick_bends_at_the_pick():
    # Regression (user: "at zero degree why are these horizontal pipes
    # not turning with a 90 deg bend"): a LONG run picked at mid-span.
    # End-anchored fans put every turn metres behind the text and the
    # pick was refused; the fan must anchor just ahead of the text.
    items = [{'key': 0, 'own': 'h', 'pos': -3.0,
              'span': (-500.0, 500.0), 'exit_edge': 4.0}]
    plan = engine.plan_ordered((0.0, 0.0), items, engine.UPPER_LEFT, 0.0,
                               2.0, 4.0, 3.0, 'h', clearance=0.82)
    entry = plan[0]
    assert entry['angle_ok'] is True
    assert entry['straight'] is True
    # Turn sits just ahead of the text edge (4.0 + clearance-capped
    # margin), nowhere near the pipe's -500 end.
    assert entry['end'][0] == pytest.approx(4.0 + 0.82)
    assert entry['end'][1] == pytest.approx(-3.0)   # on the pipe
    assert entry['elbow'][0] >= 4.0                 # never behind text
    assert _tilt_of(entry) == pytest.approx(0.0, abs=1e-6)  # true 90


def test_ordered_fan_direction_follows_actual_pipe_side():
    # Case-1 regression: mode LOWER-LEFT but the pipes are BELOW the
    # stack. The old fan keyed on the dialog quadrant and gave the TOP
    # row the nearest turn, so its climb sliced the lower landing. The
    # fan must key on the actual side: bottom row turns nearest.
    plan = engine.plan_ordered((0.0, 0.0), _h_items([-60.0, -40.0],
                                                    span=(0.0, 400.0)),
                               engine.LOWER_LEFT, 0.0, 2.0, 4.0, 3.0, 'h')
    by_row = dict((p['row'], p) for p in plan)
    assert by_row[1]['end'][1] == pytest.approx(-40.0)  # top row=top pipe
    assert by_row[0]['end'][0] < by_row[1]['end'][0]    # bottom turns 1st
    assert not _any_leaders_cross(plan)
    for entry in plan:
        assert entry['straight'] is True
        assert _tilt_of(entry) == pytest.approx(0.0, abs=1e-6)  # true 90


def test_ordered_vertical_first_reading_order():
    # User rule: ALL vertical-pipe tags first (left-to-right), then ALL
    # horizontal-pipe tags (top-to-bottom), whatever their positions.
    items = [
        {'key': 'h-top', 'own': 'h', 'pos': 100.0, 'span': (0.0, 400.0)},
        {'key': 'v-right', 'own': 'v', 'pos': 30.0,
         'span': (200.0, 400.0)},
        {'key': 'h-low', 'own': 'h', 'pos': 80.0, 'span': (0.0, 400.0)},
        {'key': 'v-left', 'own': 'v', 'pos': 10.0, 'span': (200.0, 400.0)},
    ]
    plan = engine.plan_ordered((0.0, 0.0), items, engine.UPPER_LEFT, 0.0,
                               2.0, 4.0, 3.0, 'v')
    rows = dict((p['key'], p['row']) for p in plan)
    assert rows['v-left'] == 3    # verticals first, left to right
    assert rows['v-right'] == 2
    assert rows['h-top'] == 1     # then horizontals, top to bottom
    assert rows['h-low'] == 0


def test_ordered_vertical_drop_ladder():
    # Vertical drops ABOVE the stack: each tag climbs at TILT_DEG onto its
    # own pipe, arrows fanned UP the pipes from their lower ends - and the
    # climb is never drawn along the pipe itself.
    items = [{'key': k, 'pos': p, 'span': (50.0, 200.0)}
             for k, p in enumerate([60.0, 80.0])]
    plan = engine.plan_ordered((0.0, 0.0), items, engine.UPPER_LEFT, 0.0,
                               2.0, 4.0, 3.0, 'v')
    by_key = dict((p['key'], p) for p in plan)
    left, right = by_key[0], by_key[1]
    assert left['row'] == 1 and right['row'] == 0   # reads left-to-right
    for entry in (left, right):
        assert entry['straight'] is True
        assert entry['angle_ok'] is True
        assert entry['end'][1] >= 50.0 + 0.05 * 150.0  # clearance off end
        assert entry['elbow'][0] < entry['end'][0]  # NOT along the pipe
        assert _tilt_of(entry) == pytest.approx(engine.TILT_DEG, abs=1e-6)
    # Arrows at distinct heights: nearest row (top) takes the lowest slot.
    assert left['end'][1] < right['end'][1]
    # Parallel climbs: same lean per unit rise, same direction.
    lean_left = (left['end'][0] - left['elbow'][0]) / (
        left['end'][1] - left['elbow'][1])
    lean_right = (right['end'][0] - right['elbow'][0]) / (
        right['end'][1] - right['elbow'][1])
    assert lean_left == pytest.approx(lean_right)


def test_ordered_l_leaders_on_horizontal_pipes_at_zero_angle():
    # Angle 0 + horizontal pipes: horizontal landing at the text height,
    # then a NEAR-VERTICAL climb at TILT_DEG onto the pipe. Equidistant
    # arrow fan, nearest row turning first (user's reference drawing).
    plan = engine.plan_ordered((0.0, 0.0), _h_items([-60.0, -40.0, -20.0],
                                                    span=(0.0, 400.0)),
                               engine.UPPER_LEFT, 0.0, 2.0, 4.0, 3.0, 'h')
    for entry in plan:
        assert entry['angle_ok'] is True
        assert entry['straight'] is True
        # Landing horizontal at the text height; the bend onto a
        # HORIZONTAL pipe is a true 90 degrees (user rule - the climb is
        # perpendicular to the pipe, no tilt needed).
        assert entry['elbow'][1] == pytest.approx(entry['head'][1])
        assert _tilt_of(entry) == pytest.approx(0.0, abs=1e-6)
        assert entry['elbow'][0] == pytest.approx(entry['end'][0])
    # Equidistant arrow fan: steps by the row pitch (2.0), nearest row
    # (row 0, closest to the pipes below an UPPER stack) turning first,
    # 5% inside the pipes' shared extent.
    turns = sorted(p['end'][0] for p in plan)
    assert turns[1] - turns[0] == pytest.approx(2.0)
    assert turns[2] - turns[1] == pytest.approx(2.0)
    assert turns[0] == pytest.approx(20.0)   # 0.05 * 400
    by_row = dict((p['row'], p) for p in plan)
    assert by_row[0]['end'][0] < by_row[1]['end'][0] < by_row[2]['end'][0]


def test_ordered_l_leaders_fit_short_pipes_equidistantly():
    # Pipe only 1 unit long: the fan shrinks so all three turns still fit
    # inside it, equally spaced - the "short pipe" requirement.
    plan = engine.plan_ordered((0.0, 0.0), _h_items([-60.0, -40.0, -20.0],
                                                    span=(0.0, 1.0)),
                               engine.UPPER_LEFT, 0.0, 2.0, 4.0, 3.0, 'h')
    # Near margin 0.05; the fan lives in the NEAR 60% of what remains
    # (0.9 * 0.6 = 0.54), so three turns step by 0.18.
    turns = sorted(p['end'][0] for p in plan)
    assert turns[0] == pytest.approx(0.05)
    assert turns[1] - turns[0] == pytest.approx(0.18)
    assert turns[2] - turns[1] == pytest.approx(0.18)
    assert turns[2] <= 1.0
    assert all(p['straight'] for p in plan)


def test_ordered_l_leaders_mirror_for_left_exit():
    # UPPER_RIGHT: leaders exit LEFT; the fan anchors at the pipes' far
    # (right) end and steps leftward.
    plan = engine.plan_ordered((500.0, 0.0), _h_items([-60.0, -40.0],
                                                      span=(0.0, 400.0)),
                               engine.UPPER_RIGHT, 0.0, 2.0, 4.0, 3.0, 'h')
    turns = sorted(p['end'][0] for p in plan)
    assert turns[1] == pytest.approx(380.0)   # 400 - 5%
    assert turns[1] - turns[0] == pytest.approx(2.0)
    for entry in plan:
        assert entry['straight'] is True
        assert entry['elbow'][0] == pytest.approx(entry['end'][0])  # 90 deg
        assert entry['elbow'][0] < 500.0            # left of the stack
        assert _tilt_of(entry) == pytest.approx(0.0, abs=1e-6)


def test_ordered_cross_pipe_gets_tilted_climb_in_vertical_bundle():
    # A horizontal branch inside a vertical bundle at angle 0: the branch
    # tag keeps its stack slot (order_pos) but gets a tilted climb onto
    # its own pipe, fanned inside the branch's real extent.
    items = [
        {'key': 0, 'own': 'v', 'pos': 60.0, 'span': (-300.0, 100.0)},
        {'key': 1, 'own': 'h', 'pos': -30.0, 'span': (20.0, 60.0),
         'order_pos': 40.0},
    ]
    plan = engine.plan_ordered((0.0, 0.0), items, engine.UPPER_LEFT, 0.0,
                               2.0, 4.0, 3.0, 'v')
    by_key = dict((p['key'], p) for p in plan)
    level = by_key[0]      # vertical pipe, tag level with it: straight
    assert level['straight'] is True
    assert level['end'] == pytest.approx((60.0, level['head'][1]))
    cross = by_key[1]      # horizontal branch: true 90-degree bend
    assert cross['angle_ok'] is True
    assert cross['straight'] is True
    assert cross['end'] == pytest.approx((22.0, -30.0))  # 20 + 5% margin
    assert cross['elbow'][1] == pytest.approx(cross['head'][1])
    assert cross['elbow'][0] == pytest.approx(cross['end'][0])
    assert _tilt_of(cross) == pytest.approx(0.0, abs=1e-6)
    # Reading order (user rule): vertical-pipe tags FIRST, then
    # horizontal-pipe tags - the branch drops below the riser tag.
    assert level['row'] == 1 and cross['row'] == 0


@pytest.mark.parametrize('angle', [0.0, 45.0])
def test_ordered_landing_compresses_near_pipe(angle):
    # Pick close to the pipe: the landing shrinks to the available room
    # instead of failing. Text edge at 20, pipe at 22 -> only 2 units of
    # room although the landing setting asks for 4.
    items = [{'key': 0, 'pos': 22.0, 'span': (-300.0, 100.0),
              'exit_edge': 20.0}]
    plan = engine.plan_ordered((0.0, 0.0), items, engine.UPPER_LEFT, angle,
                               2.0, 4.0, 3.0, 'v')
    entry = plan[0]
    assert entry['angle_ok'] is True
    # Straight leaders park the elbow mid-line; slanted keep the (fully
    # compressed) bend point at the pipe.
    expected_elbow = 21.0 if angle == 0.0 else 22.0
    assert entry['elbow'][0] == pytest.approx(expected_elbow)
    assert entry['end'][0] == pytest.approx(22.0)    # arrow on the pipe


def test_ordered_click_inside_old_stack_only_flags_overlapped_text():
    # The re-align regression: picking right of the previous corner used
    # to flag EVERY tag. Now only a tag whose text would actually cross
    # its pipe fails; the rest compress their landings and align.
    items = [{'key': k, 'pos': p, 'span': (-300.0, 100.0),
              'exit_edge': 20.0}
             for k, p in enumerate([40.0, 55.0, 70.0, 85.0])]
    plan = engine.plan_ordered((30.0, 0.0), items, engine.UPPER_LEFT, 0.0,
                               2.0, 4.0, 3.0, 'v')
    flags = dict((p['end'][0] if p['angle_ok'] else 'text-crosses',
                  p['angle_ok']) for p in plan)
    # Text spans 30..50: pipe at 40 is inside the text -> flagged.
    assert flags.get('text-crosses') is False
    # The other three compress and align fine.
    assert sum(1 for p in plan if p['angle_ok']) == 3


def test_ordered_remaining_edges():
    # Zero spacing falls back to an epsilon step (rows never coincide).
    plan = engine.plan_ordered((0.0, 0.0), _v_items([40.0, 60.0]),
                               engine.UPPER_LEFT, 45, 0.0, 4.0, 3.0, 'v')
    heights = sorted(p['head'][1] for p in plan)
    assert heights[0] < heights[1]

    # 'v' + LOWER mode overshooting the pipe's TOP end: clamps to hi.
    items = [{'key': 0, 'pos': 80.0, 'span': (-100.0, 5.0)}]
    plan = engine.plan_ordered((0.0, 0.0), items, engine.LOWER_LEFT, 45,
                               2.0, 4.0, 3.0, 'v')
    assert plan[0]['angle_ok'] is False
    assert plan[0]['end'] == pytest.approx((80.0, 5.0))

    # 'h' + pipe ABOVE an upper stack: wrong side, collapsed and flagged.
    plan = engine.plan_ordered((0.0, 0.0), _h_items([50.0]),
                               engine.UPPER_LEFT, 45, 2.0, 4.0, 3.0, 'h')
    assert plan[0]['angle_ok'] is False
    assert plan[0]['end'] == pytest.approx(plan[0]['elbow'])

    # 'h' span clamps on both sides.
    lo_clamp = engine.plan_ordered(
        (0.0, 0.0), _h_items([-60.0], span=(200.0, 400.0)),
        engine.UPPER_LEFT, 45, 2.0, 4.0, 3.0, 'h')
    assert lo_clamp[0]['angle_ok'] is False
    assert lo_clamp[0]['end'] == pytest.approx((200.0, -60.0))
    hi_clamp = engine.plan_ordered(
        (0.0, 0.0), _h_items([-60.0], span=(0.0, 30.0)),
        engine.UPPER_LEFT, 45, 2.0, 4.0, 3.0, 'h')
    assert hi_clamp[0]['angle_ok'] is False
    assert hi_clamp[0]['end'] == pytest.approx((30.0, -60.0))


def test_ordered_intermittent_staggers_and_halves():
    plan = engine.plan_ordered((0.0, 0.0),
                               _v_items([40.0, 55.0, 70.0, 85.0]),
                               engine.UPPER_LEFT, 45, 2.0, 4.0, 3.0, 'v',
                               intermittent=True)
    by_row = sorted(plan, key=lambda p: p['row'])
    for row, entry in enumerate(by_row):
        assert entry['head'][1] == pytest.approx(row * 1.0)  # half of 2.0
        expected_u = -3.0 if row % 2 else 0.0                # away = left
        assert entry['head'][0] == pytest.approx(expected_u)
        assert abs(entry['elbow'][0] - entry['head'][0]) == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# leader_segments helper
# ---------------------------------------------------------------------------
def test_leader_segments_shape():
    entry = {'head': (0, 0), 'elbow': (2, 0), 'end': (4, -2)}
    assert engine.leader_segments(entry) == [((0, 0), (2, 0)),
                                             ((2, 0), (4, -2))]


# ---------------------------------------------------------------------------
# shortfall - the contract the anchor nudge relies on (F1, 2026-08-09)
# ---------------------------------------------------------------------------
def _reach_item(pos, exit_edge, span=(-300.0, 100.0)):
    return [{'key': 0, 'pos': pos, 'span': span, 'exit_edge': exit_edge}]


def test_a_plan_that_fits_reports_no_shortfall():
    plan = engine.plan_ordered((0.0, 0.0), _reach_item(40.0, 5.0),
                               engine.UPPER_LEFT, 45.0, 2.0, 4.0, 3.0, 'v')
    assert plan[0]['angle_ok']
    assert plan[0]['shortfall'] == 0.0


def test_shortfall_measures_exactly_how_far_the_text_overhangs():
    # Pipe at u=10, text reaching to u=13 from a pick at u=8: the text's
    # leading edge lands 3 past the pipe - 3 is what must be given back.
    plan = engine.plan_ordered((8.0, 0.0), _reach_item(10.0, 5.0),
                               engine.UPPER_LEFT, 45.0, 2.0, 4.0, 3.0, 'v')
    assert not plan[0]['angle_ok']
    assert plan[0]['shortfall'] == pytest.approx(3.0)


@pytest.mark.parametrize('mode,pos,anchor_u', [
    (engine.UPPER_LEFT, 10.0, 8.0),      # leaders exit right
    (engine.UPPER_RIGHT, -10.0, -8.0),   # leaders exit left, mirrored
    (engine.LOWER_LEFT, 10.0, 8.0),
    (engine.LOWER_RIGHT, -10.0, -8.0),
])
def test_retreating_by_the_shortfall_makes_the_pick_legal(mode, pos,
                                                          anchor_u):
    # The exact contract nudge_clear depends on: back off by the reported
    # shortfall and the tag can reach its pipe. If this drifts, the tool
    # silently starts producing broken stacks instead of refusals.
    items = _reach_item(pos, 5.0)
    plan = engine.plan_ordered((anchor_u, 0.0), items, mode, 45.0,
                               2.0, 4.0, 3.0, 'v')
    need = plan[0]['shortfall']
    assert need > 0.0

    moved_u = anchor_u - engine.exit_sign(mode) * need
    retry = engine.plan_ordered((moved_u, 0.0), items, mode, 45.0,
                                2.0, 4.0, 3.0, 'v')
    assert retry[0]['angle_ok']
    assert retry[0]['shortfall'] == 0.0


def test_horizontal_run_reports_a_shortfall_when_the_turn_is_behind():
    # Angle 0 onto a horizontal pipe: picking past the pipe's far end
    # puts the turn behind the text, which retreating also mends.
    items = [{'key': 0, 'pos': -20.0, 'span': (0.0, 30.0),
              'exit_edge': 8.0}]
    plan = engine.plan_ordered((60.0, 0.0), items, engine.UPPER_LEFT, 0.0,
                               2.0, 4.0, 3.0, 'h', clearance=1.0)
    assert not plan[0]['angle_ok']
    assert plan[0]['shortfall'] > 0.0

    need = plan[0]['shortfall']
    retry = engine.plan_ordered((60.0 - need, 0.0), items,
                                engine.UPPER_LEFT, 0.0, 2.0, 4.0, 3.0, 'h',
                                clearance=1.0)
    assert retry[0]['angle_ok']


def test_unfixable_breaks_report_no_shortfall():
    # Pipe on the wrong side vertically - sliding along u cannot mend
    # it, so the nudge must not chase it.
    items = [{'key': 0, 'pos': 50.0, 'span': (0.0, 30.0),
              'exit_edge': 2.0}]
    plan = engine.plan_ordered((0.0, 0.0), items, engine.LOWER_LEFT, 45.0,
                               2.0, 4.0, 3.0, 'h')
    if not plan[0]['angle_ok']:
        assert plan[0]['shortfall'] == 0.0


# ---------------------------------------------------------------------------
# Turn-fan distribution by pipe length (user rule, 2026-08-09)
# ---------------------------------------------------------------------------
def _fan_items(specs):
    """[(pos, span)] -> h-bundle items with a common exit edge."""
    return [{'key': i, 'pos': pos, 'span': span, 'exit_edge': 5.0}
            for i, (pos, span) in enumerate(specs)]


def _turns(plan):
    return {e['key']: e['end'][0] for e in plan}


def test_a_short_pipe_no_longer_drags_the_whole_fan_tight():
    # Two long pipes and one short: the common window is the
    # intersection, starved by the short member. Every tag now spreads
    # over ITS OWN pipe's room - the long-pipe tags fan wide again.
    items = _fan_items([(-20.0, (0.0, 200.0)),
                      (-22.0, (0.0, 200.0)),
                      (-24.0, (0.0, 9.0))])
    plan = engine.plan_ordered((0.0, 0.0), items, engine.UPPER_LEFT, 0.0,
                               2.0, 4.0, 3.0, 'h', clearance=2.0)
    turns = _turns(plan)
    for entry in plan:
        lo, hi = items[entry['key']]['span']
        assert lo <= turns[entry['key']] <= hi     # each on its own pipe
    long_turns = sorted([turns[0], turns[1]])
    # Long pipes keep clearance spacing (the 'no issues' behaviour) -
    # the OLD code piled every turn on one point of the starved window.
    assert long_turns[1] - long_turns[0] >= 2.0 - 1e-9
    assert turns[2] <= 9.0                         # short stays on its pipe


def test_an_unstarved_fan_keeps_the_common_spacing():
    # All pipes long: the window is not starved, so the behaviour is
    # exactly the pre-change one - clearance-spaced common slots.
    items = _fan_items([(-20.0, (0.0, 200.0)),
                      (-22.0, (0.0, 200.0)),
                      (-24.0, (0.0, 200.0))])
    plan = engine.plan_ordered((0.0, 0.0), items, engine.UPPER_LEFT, 0.0,
                               2.0, 4.0, 3.0, 'h', clearance=2.0)
    turns = sorted(_turns(plan).values())
    assert turns[1] - turns[0] == pytest.approx(2.0)
    assert turns[2] - turns[1] == pytest.approx(2.0)


def test_all_short_pipes_spread_instead_of_piling():
    # The sliver case: every pipe short. Arrows must not stack on one
    # point; they spread evenly across each pipe's own room.
    items = _fan_items([(-20.0, (0.0, 14.0)),
                      (-22.0, (0.0, 14.0)),
                      (-24.0, (0.0, 14.0))])
    plan = engine.plan_ordered((0.0, 0.0), items, engine.UPPER_LEFT, 0.0,
                               2.0, 4.0, 3.0, 'h', clearance=2.0)
    turns = sorted(_turns(plan).values())
    assert len(set(round(t, 6) for t in turns)) == 3   # all distinct
    assert turns[1] - turns[0] > 0.5
    assert turns[2] - turns[1] > 0.5


# ---------------------------------------------------------------------------
# Risers in plan: point targets (user rules, 2026-08-10)
# ---------------------------------------------------------------------------
def _riser_items(circle_vs, u=100.0):
    """Drops seen end-on: point spans, u STAGGERED by pipe size.

    The stagger is the real-world case that broke the first fix: a
    height tie-break never fires when the primary left-to-right key
    differs by the elbows' few-hundred-mm offsets.
    """
    return [{'key': i, 'own': 'v', 'pos': u + 0.3 * i, 'span': (v, v)}
            for i, v in enumerate(circle_vs)]


def test_riser_rows_order_top_circle_to_top_tag():
    # All points share one u - the left-to-right key ties. The height
    # must break the tie: topmost circle -> top tag, so leaders can
    # never cross at the column.
    items = _riser_items([5.0, 25.0, 15.0])
    plan = engine.plan_ordered((140.0, 0.0), items, engine.UPPER_RIGHT,
                               0.0, 3.0, 4.0, 3.0, 'v', clearance=2.0)
    by_key = {e['key']: e for e in plan}
    # circle at v=25 is topmost -> highest row; v=5 lowest -> row 0.
    assert by_key[1]['row'] == 2
    assert by_key[2]['row'] == 1
    assert by_key[0]['row'] == 0


def test_riser_leaders_share_one_slant_angle():
    # The user's before/after (2026-08-10): short landing at the text,
    # then every slant PARALLEL at the cluster's common angle, straight
    # into its circle. The bend sits on the landing line.
    items = _riser_items([5.0, 25.0, 15.0])
    plan = engine.plan_ordered((140.0, 0.0), items, engine.UPPER_RIGHT,
                               0.0, 3.0, 4.0, 3.0, 'v', clearance=2.0)
    slopes = []
    for entry in plan:
        own_u = 100.0 + 0.3 * entry['key']
        assert entry['end'][0] == pytest.approx(own_u)      # on the point
        assert entry['elbow'][1] == pytest.approx(entry['line_v'])  # on landing
        assert entry['straight']
        assert entry['angle_ok']
        du = abs(entry['end'][0] - entry['elbow'][0])
        dv = abs(entry['end'][1] - entry['elbow'][1])
        if dv > 1e-9:
            slopes.append(dv / du)
    assert len(slopes) >= 2
    for s in slopes[1:]:
        assert s == pytest.approx(slopes[0])                # parallel


def test_riser_leaders_cannot_cross():
    # Ordered top-to-top with level approaches: no elbow-to-end segment
    # may intersect another. Brute-force check all pairs.
    items = _riser_items([5.0, 25.0, 15.0, 35.0])
    plan = engine.plan_ordered((140.0, 0.0), items, engine.UPPER_RIGHT,
                               0.0, 3.0, 4.0, 3.0, 'v', clearance=2.0)
    per_tag = [engine.leader_segments(entry) for entry in plan]

    def crosses(p1, p2, p3, p4):
        def orient(a, b, c):
            return (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])
        d1, d2 = orient(p3, p4, p1), orient(p3, p4, p2)
        d3, d4 = orient(p1, p2, p3), orient(p1, p2, p4)
        return ((d1 > 1e-9) != (d2 > 1e-9)) and ((d3 > 1e-9) != (d4 > 1e-9))

    # A tag's own two segments share the elbow by construction; only
    # DIFFERENT tags' leaders must never touch.
    for a in range(len(per_tag)):
        for b in range(a + 1, len(per_tag)):
            for s1 in per_tag[a]:
                for s2 in per_tag[b]:
                    assert not crosses(s1[0], s1[1], s2[0], s2[1])


def test_riser_mixed_directions_order_by_arrow_drop():
    # The pinned cluster of 2026-08-10: T/B tags reference RUN pipes, so
    # two items arrive as own='h' with pos at the RUN's level - not the
    # drop's. Sub-grouping by direction then paired same-service rows
    # and forced a crossing. When the cluster holds a point and every
    # item knows its drop (arrow, or is itself a point), the whole stack
    # orders by drop height: topmost drop -> top row.
    items = [
        {'key': 0, 'own': 'v', 'pos': 100.0, 'span': (30.0, 30.0),
         'arrow': (100.0, 30.0)},
        {'key': 1, 'own': 'v', 'pos': 100.3, 'span': (20.0, 20.0),
         'arrow': (100.3, 20.0)},
        {'key': 2, 'own': 'h', 'pos': 27.0, 'span': (100.0, 140.0),
         'arrow': (101.0, 25.0)},
        {'key': 3, 'own': 'h', 'pos': 26.0, 'span': (100.0, 140.0),
         'arrow': (101.0, 15.0)},
    ]
    plan = engine.plan_ordered((160.0, 0.0), items, engine.UPPER_RIGHT,
                               0.0, 3.0, 4.0, 3.0, 'v', clearance=2.0)
    rows = dict((e['key'], e['row']) for e in plan)
    # Drop heights 30 > 25 > 20 > 15: rows interleave the two directions.
    assert rows[0] == 3 and rows[2] == 2
    assert rows[1] == 1 and rows[3] == 0


def test_riser_mixed_without_arrows_keeps_subgroup_order():
    # No arrows to trust -> the proven grouping stands: points take the
    # upper rows, horizontals the lower, deterministically.
    items = [
        {'key': 0, 'own': 'v', 'pos': 100.0, 'span': (20.0, 20.0)},
        {'key': 1, 'own': 'h', 'pos': 30.0, 'span': (100.0, 140.0)},
    ]
    plan = engine.plan_ordered((160.0, 0.0), items, engine.UPPER_RIGHT,
                               0.0, 3.0, 4.0, 3.0, 'v', clearance=2.0)
    rows = dict((e['key'], e['row']) for e in plan)
    assert rows[0] == 1 and rows[1] == 0


def test_pure_run_stack_ignores_arrows():
    # No point in the cluster -> arrows must NOT reorder a run stack;
    # the field-proven top-pipe-to-top-tag rule stands.
    items = [
        {'key': 0, 'own': 'h', 'pos': -20.0, 'span': (10.0, 90.0),
         'arrow': (30.0, -60.0)},          # arrow deliberately misleading
        {'key': 1, 'own': 'h', 'pos': -60.0, 'span': (10.0, 90.0),
         'arrow': (30.0, -20.0)},
    ]
    plan = engine.plan_ordered((0.0, 0.0), items, engine.UPPER_LEFT,
                               0.0, 3.0, 4.0, 3.0, 'h', clearance=2.0)
    rows = dict((e['key'], e['row']) for e in plan)
    assert rows[0] == 1 and rows[1] == 0   # by pipe v, not arrow v


def test_riser_slant_angle_is_clamped_to_a_readable_range():
    # A circle nearly level with its row must not flatten the shared
    # angle below 15 degrees, and a deep drop must not steepen it past
    # 75 - the clamp keeps the parallel sheaf readable.
    import math as _math
    items = _riser_items([5.0, 5.5, 6.0])       # nearly level cluster
    plan = engine.plan_ordered((140.0, 4.0), items, engine.UPPER_RIGHT,
                               0.0, 3.0, 4.0, 3.0, 'v', clearance=2.0)
    for entry in plan:
        du = abs(entry['end'][0] - entry['elbow'][0])
        dv = abs(entry['end'][1] - entry['elbow'][1])
        if dv > 1e-9 and du > 1e-9:
            angle = _math.degrees(_math.atan2(dv, du))
            assert 15.0 - 1e-6 <= angle <= 75.0 + 1e-6
