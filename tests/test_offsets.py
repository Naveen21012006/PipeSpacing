# -*- coding: utf-8 -*-
"""Which text edge lands on the picked column.

ordered_offsets is pure, but it lives in script.py next to the Revit
imports, so it is sliced out of the source the way test_leader_rules
does it rather than importing the module.
"""

import os

import pytest

import engine

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'CKR Tools.tab', 'Annotation.panel', 'AlignTags.pushbutton', 'script.py')


class _CommonStub(object):
    """Unit passthroughs so ordered_plan's logging can run under pytest."""

    @staticmethod
    def mm_to_feet(value):
        return value / 304.8

    @staticmethod
    def feet_to_mm(value):
        return value * 304.8


class _LogStub(object):
    def info(self, *args):
        pass


def _load_pure_parts():
    source = open(_SCRIPT).read()
    start = source.index('def ordered_offsets')
    end = source.index('_MODE_NAMES = {')
    namespace = {'engine': engine, 'common': _CommonStub,
                 'file_log': _LogStub(), 'FITTING_CLEARANCE_MM': 250.0}
    exec(compile(source[start:end], 'script', 'exec'), namespace)
    return namespace


_PARTS = _load_pure_parts()
ordered_offsets = _PARTS['ordered_offsets']
bundle_centre_u = _PARTS['bundle_centre_u']
side_mode = _PARTS['side_mode']
ordered_plan = _PARTS['ordered_plan']
_median = _PARTS['_median']


class _Tag(object):
    """A measured tag: text box (u_lo, u_hi, v_lo, v_hi) and head point."""

    def __init__(self, bbox, head):
        self.bbox2d = bbox
        self.head_ref2d = head
        self.id_value = id(self)


# A 10-wide, 2-tall text box with the head at its CENTRE - the user's
# family, proven by the log's symmetric caps (head ~460mm from either
# edge of ~920mm text). Assuming a left-edge head here is the 2026-08-02
# regression that shifted every stack half a width off its pick.
def _tag():
    return _Tag((100.0, 110.0, 0.0, 2.0), (105.0, 1.0))


@pytest.mark.parametrize('mode', engine.MODES)
def test_the_text_left_edge_lands_on_the_column(mode):
    # head_du carries the family's own head-to-left-edge distance, so
    # the LEFT EDGE sits on the pick - for a centre-head family the head
    # itself lands half a width right of it.
    (head_du, _), _, _ = ordered_offsets(_tag(), mode)
    assert head_du == pytest.approx(5.0)


def test_median_biases_low_because_boxes_only_grow():
    # A stale leader can only ever ENLARGE a box: with half the cluster
    # contaminated, the upper-middle would itself be a contaminated
    # value, so even counts take the lower-middle.
    assert _median([460.0, 460.0, 938.0, 938.0]) == 460.0
    assert _median([441.0, 456.0, 460.0, 938.0]) == 456.0
    assert _median([7.0]) == 7.0


def _plan_cfg():
    return {'angle_deg': 0.0, 'intermittent': False}


def test_one_contaminated_tag_cannot_indent_its_row():
    # End to end through ordered_plan: three clean centre-head tags and
    # one whose old leader balanced its text, doubling the box without
    # tripping the symmetric cap (tag 21419026, user's image). Its head
    # offset snaps to the cluster median, so all four heads share one
    # column instead of one row indenting by half the contamination.
    clean = [_Tag((0.0, 9.2, 0.0, 2.0), (4.6, 1.0)) for _ in range(3)]
    dirty = _Tag((-4.7, 14.0, 0.0, 2.0), (4.65, 1.0))   # symmetric, doubled
    targets = clean + [dirty]
    base_items = [{'key': i, 'pos': 100.0 + 3.0 * i, 'span': (-50.0, 50.0)}
                  for i in range(4)]
    plan = ordered_plan((40.0, 0.0), base_items, targets,
                        engine.LOWER_LEFT, 'v', _plan_cfg(),
                        3.0, 4.0, 3.0)
    heads = set(round(entry['head'][0], 6) for entry in plan)
    assert len(heads) == 1                      # one column, no indent
    assert heads.pop() == pytest.approx(40.0 + 4.6)


def test_clean_clusters_are_untouched_by_the_outlier_snap():
    # Slightly different genuine widths must survive: the snap only
    # fires ABOVE 1.4x the median, so honest variation stays put.
    tags = [_Tag((0.0, w, 0.0, 2.0), (w / 2.0, 1.0))
            for w in (8.8, 9.0, 9.2, 9.4)]
    base_items = [{'key': i, 'pos': 100.0 + 3.0 * i, 'span': (-50.0, 50.0)}
                  for i in range(4)]
    plan = ordered_plan((40.0, 0.0), base_items, tags,
                        engine.LOWER_LEFT, 'v', _plan_cfg(),
                        3.0, 4.0, 3.0)
    by_key = {entry['key']: entry['head'][0] for entry in plan}
    for i, w in enumerate((8.8, 9.0, 9.2, 9.4)):
        assert by_key[i] == pytest.approx(40.0 + w / 2.0)


def test_leaders_that_exit_right_clear_the_whole_text():
    # The landing starts at the far edge, a full text width from the
    # corner the click placed.
    _, _, exit_edge = ordered_offsets(_tag(), engine.UPPER_LEFT)
    assert exit_edge == pytest.approx(10.0)


def test_leaders_that_exit_left_start_at_the_corner():
    # The corner IS the exit edge, so the text runs away from the pipes
    # behind it and the landing starts exactly on the column.
    _, _, exit_edge = ordered_offsets(_tag(), engine.UPPER_RIGHT)
    assert exit_edge == pytest.approx(0.0)


@pytest.mark.parametrize('mode', engine.MODES)
def test_every_stack_starts_its_landings_on_one_column(mode):
    # The point of the rule: tags of DIFFERENT widths must still start
    # their landings at the same u, or the leaders comb out ragged.
    narrow = _Tag((100.0, 104.0, 0.0, 2.0), (102.0, 1.0))
    wide = _Tag((100.0, 130.0, 0.0, 2.0), (115.0, 1.0))
    anchor_u = 40.0
    sign = engine.exit_sign(mode)

    starts = []
    for tag in (narrow, wide):
        (head_du, _), _, exit_edge = ordered_offsets(tag, mode)
        corner_u = anchor_u          # plan_ordered puts the corner here
        starts.append(corner_u + sign * exit_edge)

    if sign > 0:
        # Exiting right, the landings start at each text's own far edge,
        # so they differ - that is the shape the user called correct.
        assert starts[0] != pytest.approx(starts[1])
    else:
        # Exiting left, every landing starts on the column.
        assert starts[0] == pytest.approx(starts[1])
        assert starts[0] == pytest.approx(anchor_u)


def test_unmeasured_tag_is_reported_so_the_median_can_stand_in():
    assert ordered_offsets(_Tag(None, (0.0, 0.0)), engine.UPPER_LEFT) is None
    assert ordered_offsets(_Tag((0.0, 1.0, 0.0, 1.0), None),
                           engine.UPPER_RIGHT) is None


# ---------------------------------------------------------------------------
# Which side the stack lands on - decided by the click, not the dialog
# ---------------------------------------------------------------------------
def _v_bundle():
    """Vertical pipes side by side at u = 100, 110, 120."""
    return 'v', [{'key': 0, 'pos': 100.0, 'span': (-50.0, 50.0)},
                 {'key': 1, 'pos': 110.0, 'span': (-50.0, 50.0)},
                 {'key': 2, 'pos': 120.0, 'span': (-50.0, 50.0)}]


def _h_bundle():
    """Horizontal pipes stacked, each running u = 60..160 (centre 110)."""
    return 'h', [{'key': 0, 'pos': 10.0, 'span': (60.0, 160.0)},
                 {'key': 1, 'pos': 20.0, 'span': (60.0, 160.0)},
                 {'key': 2, 'pos': 30.0, 'span': (60.0, 160.0)}]


def _config(mode, switch_side=False):
    return {'mode': mode, 'switch_side': switch_side}


def test_bundle_centre_uses_the_pipes_own_geometry():
    bundle, items = _v_bundle()
    assert bundle_centre_u(bundle, items) == pytest.approx(110.0)
    bundle, items = _h_bundle()
    assert bundle_centre_u(bundle, items) == pytest.approx(110.0)


@pytest.mark.parametrize('dialog', engine.MODES)
def test_clicking_left_of_the_pipes_sends_leaders_right(dialog):
    bundle, items = _v_bundle()
    mode = side_mode(_config(dialog), (20.0, 0.0), bundle, items)
    assert engine.exit_sign(mode) > 0


@pytest.mark.parametrize('dialog', engine.MODES)
def test_clicking_right_of_the_pipes_sends_leaders_left(dialog):
    bundle, items = _v_bundle()
    mode = side_mode(_config(dialog), (200.0, 0.0), bundle, items)
    assert engine.exit_sign(mode) < 0


@pytest.mark.parametrize('dialog,upper', [
    (engine.UPPER_LEFT, True), (engine.UPPER_RIGHT, True),
    (engine.LOWER_LEFT, False), (engine.LOWER_RIGHT, False),
])
def test_upper_lower_still_comes_from_the_dialog(dialog, upper):
    bundle, items = _v_bundle()
    for anchor_u in (20.0, 200.0):
        mode = side_mode(_config(dialog), (anchor_u, 0.0), bundle, items)
        assert (mode in (engine.UPPER_LEFT, engine.UPPER_RIGHT)) is upper


def test_switch_pick_point_side_no_longer_moves_the_stack():
    # The user's own case: Lower-Left with Switch Side ON used to force
    # Lower-Right regardless of the click. The click decides now.
    bundle, items = _v_bundle()
    left_click, right_click = (20.0, 0.0), (200.0, 0.0)
    for switched in (False, True):
        cfg = _config(engine.LOWER_LEFT, switch_side=switched)
        assert side_mode(cfg, left_click, bundle, items) == engine.LOWER_LEFT
        assert side_mode(cfg, right_click, bundle, items) == engine.LOWER_RIGHT


def test_side_is_a_pure_function_of_the_pick():
    # Same inputs, same answer, no matter how often it is called or in
    # what order - nothing carries over from a previous pick.
    bundle, items = _v_bundle()
    cfg = _config(engine.LOWER_LEFT, switch_side=True)
    seen = [side_mode(cfg, (u, 0.0), bundle, items)
            for u in (200.0, 20.0, 200.0, 20.0, 200.0)]
    assert seen == [engine.LOWER_RIGHT, engine.LOWER_LEFT,
                    engine.LOWER_RIGHT, engine.LOWER_LEFT,
                    engine.LOWER_RIGHT]


def test_a_click_on_the_bundle_centre_resolves_deterministically():
    bundle, items = _v_bundle()
    cfg = _config(engine.LOWER_LEFT)
    first = side_mode(cfg, (110.0, 0.0), bundle, items)
    assert first == side_mode(cfg, (110.0, 0.0), bundle, items)


def test_horizontal_bundles_use_their_run_midpoint():
    bundle, items = _h_bundle()
    cfg = _config(engine.UPPER_LEFT)
    # u = 50 is left of the runs' 60..160 extent, u = 200 is right of it.
    assert side_mode(cfg, (50.0, 0.0), bundle, items) == engine.UPPER_LEFT
    assert side_mode(cfg, (200.0, 0.0), bundle, items) == engine.UPPER_RIGHT


def test_an_empty_bundle_does_not_explode():
    assert bundle_centre_u('v', []) == 0.0


# ---------------------------------------------------------------------------
# Self-correcting pick - which drawn-corner misses are trusted and fixed
# ---------------------------------------------------------------------------
def _spans(corner_u, corner_v, w=3.0, h=1.1):
    """Post-placement box spans for a text whose corner sits there."""
    return (corner_u, corner_u + w), (corner_v, corner_v + h)


CFS = _PARTS['correction_from_spans']
FT = 1 / 304.8      # 1mm in feet


def test_a_missed_corner_is_corrected_on_the_clean_sides():
    # LL: leaders exit right and rise - both datum sides clean, so a
    # drawn corner 0.5 up-left of the pick corrects in full.
    u_span, v_span = _spans(39.5, 10.5)
    fix = CFS(u_span, v_span, (40.0, 10.0), engine.LOWER_LEFT)
    assert fix == (pytest.approx(-0.5), pytest.approx(0.5))


def test_an_exact_landing_needs_no_correction():
    u_span, v_span = _spans(40.0, 10.0)
    assert CFS(u_span, v_span, (40.0, 10.0), engine.LOWER_LEFT) is None


def test_sub_tolerance_misses_are_left_alone():
    u_span, v_span = _spans(40.0 + 5 * FT, 10.0 - 5 * FT)   # 5mm each way
    assert CFS(u_span, v_span, (40.0, 10.0), engine.LOWER_LEFT) is None


def test_exit_left_stacks_only_trust_the_vertical():
    # LR: the leader leaves by the left edge, so u is contaminated and
    # only v may be corrected.
    u_span, v_span = _spans(39.0, 10.5)
    fix = CFS(u_span, v_span, (40.0, 10.0), engine.LOWER_RIGHT)
    assert fix == (0.0, pytest.approx(0.5))


def test_upper_stacks_only_trust_the_horizontal():
    # UL: leaders descend below the text, so v is contaminated and only
    # u may be corrected.
    u_span, v_span = _spans(39.5, 9.0)
    fix = CFS(u_span, v_span, (40.0, 10.0), engine.UPPER_LEFT)
    assert fix == (pytest.approx(-0.5), 0.0)


def test_upper_right_has_no_clean_side_and_stays_put():
    u_span, v_span = _spans(39.0, 9.0)
    assert CFS(u_span, v_span, (40.0, 10.0), engine.UPPER_RIGHT) is None


def test_a_broken_box_is_never_chased():
    # A residual bigger than any real miss means the box is garbage -
    # correcting from it would fling the stack across the view.
    u_span, v_span = _spans(40.0 + 6000 * FT, 10.0)
    assert CFS(u_span, v_span, (40.0, 10.0), engine.LOWER_LEFT) is None


def test_missing_spans_do_not_explode():
    assert CFS(None, None, (40.0, 10.0), engine.LOWER_LEFT) is None
    fix = CFS(None, _spans(0.0, 10.5)[1], (40.0, 10.0), engine.LOWER_LEFT)
    assert fix == (0.0, pytest.approx(0.5))


def test_upper_stacks_verify_v_through_the_landing_height():
    # The 2026-08-02 report: Lower stacks landed on the pick, Upper ones
    # sat hundreds of mm above it because v was skipped as unverifiable.
    # The drawn landing runs at the text's MID-height and the TOP edge is
    # leader-free for Upper modes, so bottom = 2*mid - top. Text drawn at
    # 10.5..11.6 (mid 11.05) against a pick at 10.0: dv = +0.5.
    u_span, v_span = _spans(39.5, 10.5)
    fix = CFS(u_span, v_span, (40.0, 10.0), engine.UPPER_LEFT,
              elbow_v=11.05)
    assert fix == (pytest.approx(-0.5), pytest.approx(0.5))


def test_upper_right_verifies_v_once_the_landing_is_known():
    # Exit-left keeps u unverifiable, but the elbow trick recovers v.
    u_span, v_span = _spans(39.0, 10.5)
    fix = CFS(u_span, v_span, (40.0, 10.0), engine.UPPER_RIGHT,
              elbow_v=11.05)
    assert fix == (0.0, pytest.approx(0.5))


def test_upper_v_exact_when_bottom_landed_on_the_pick():
    # Text spans 10.0..11.1 drawn, mid 10.55: bottom == pick -> no fix.
    u_span, v_span = _spans(40.0, 10.0)
    assert CFS(u_span, v_span, (40.0, 10.0), engine.UPPER_LEFT,
               elbow_v=10.55) is None


def test_upper_without_a_readable_landing_still_skips_v():
    # No elbow available: v stays unverified rather than guessed.
    u_span, v_span = _spans(40.0, 9.0)
    assert CFS(u_span, v_span, (40.0, 10.0), engine.UPPER_RIGHT) is None


def test_the_drawn_leader_direction_overrides_the_mode():
    # The 2026-08-02 log: a Lower-Left pick ABOVE a horizontal run. The
    # mode says leaders rise; the drawing descends them to the pipes, so
    # the box's bottom edge is the ARROWHEAD - a fixed point. Trusting
    # the mode chased it (same dv before and after correction); trusting
    # the drawn direction derives the bottom from landing and top.
    u_span = (39.5, 42.5)
    v_span = (5.0, 15.1)         # bottom = arrow on the pipes, top clean
    fix = CFS(u_span, v_span, (40.0, 10.0), engine.LOWER_LEFT,
              elbow_v=14.55, leader_rises=False)
    # bottom = 2*14.55 - 15.1 = 14.0 -> dv = +4.0, never -5.0
    assert fix == (pytest.approx(-0.5), pytest.approx(4.0))


def test_a_rising_leader_in_an_upper_mode_reads_the_bottom_directly():
    # Mirror case: Upper mode picked below the pipes - leaders rise, so
    # the top edge is the contaminated one and the bottom is clean.
    u_span = (40.0, 43.0)
    v_span = (10.5, 99.0)        # top = arrow far above, bottom clean
    fix = CFS(u_span, v_span, (40.0, 10.0), engine.UPPER_LEFT,
              leader_rises=True)
    assert fix == (0.0, pytest.approx(0.5))


def test_a_level_leader_counts_as_rising():
    # Straight leaders run at the text mid-height: the bottom edge is
    # free either way, and _drawn_correction maps end == elbow to rises.
    u_span, v_span = _spans(40.0, 10.5)
    fix = CFS(u_span, v_span, (40.0, 10.0), engine.LOWER_LEFT,
              leader_rises=True)
    assert fix == (0.0, pytest.approx(0.5))


# ---------------------------------------------------------------------------
# F2: the text-width hint caps poisoned boxes · F1: the nudge, end to end
# ---------------------------------------------------------------------------
def _poisoned_tag(true_w=9.2):
    """A re-run tag: box swallowed metres of old leader, hint knows better."""
    t = _Tag((-40.0, 6.0, 0.0, 2.0), (-17.0, 1.0))   # 46 "wide", head lost
    t.text_width_hint = true_w
    return t


def test_a_poisoned_box_defers_to_the_texts_own_width():
    # 2026-08-09: boxes measured 10-14m of "text". With the hint the
    # width and the head offset come from the text itself (centre-head
    # family), so the datum survives the poisoning.
    (head_du, _), _, exit_edge = ordered_offsets(_poisoned_tag(),
                                                 engine.UPPER_LEFT)
    assert head_du == pytest.approx(9.2 / 2.0)
    assert exit_edge == pytest.approx(9.2)


def test_an_honest_box_is_left_alone_by_the_hint():
    t = _Tag((100.0, 110.0, 0.0, 2.0), (105.0, 1.0))
    t.text_width_hint = 9.5          # within 1.3x of the box's 10.0
    (head_du, _), _, exit_edge = ordered_offsets(t, engine.UPPER_LEFT)
    assert head_du == pytest.approx(5.0)
    assert exit_edge == pytest.approx(10.0)


def test_no_hint_keeps_current_behaviour():
    t = _Tag((100.0, 110.0, 0.0, 2.0), (105.0, 1.0))
    (head_du, _), _, exit_edge = ordered_offsets(t, engine.UPPER_LEFT)
    assert head_du == pytest.approx(5.0)
    assert exit_edge == pytest.approx(10.0)


def test_a_close_pick_is_nudged_clear_and_placed():
    # F1 end to end: pick so close to the risers that the text would sit
    # on them. The old behaviour refused; now the stack retreats by the
    # shortfall plus clearance and every leader is valid.
    tags = [_Tag((0.0, 9.2, 0.0, 2.0), (4.6, 1.0)) for _ in range(3)]
    base_items = [{'key': i, 'pos': 100.0 + 3.0 * i, 'span': (-50.0, 50.0)}
                  for i in range(3)]
    cfg = _plan_cfg()
    anchor = (98.0, 0.0)             # text 9.2 wide -> ends past the pipe
    plan = ordered_plan(anchor, base_items, tags, engine.LOWER_LEFT, 'v',
                        cfg, 3.0, 4.0, 3.0)
    assert any(not e['angle_ok'] for e in plan)

    plan2, anchor2, moved = _PARTS['nudge_clear'](
        anchor, base_items, tags, engine.LOWER_LEFT, 'v', cfg,
        3.0, 4.0, 3.0, plan)
    assert moved > 0.0
    assert anchor2[0] < anchor[0]                    # retreated left
    assert all(e['angle_ok'] for e in plan2)         # everything placeable


def test_a_fitting_pick_is_never_nudged():
    tags = [_Tag((0.0, 9.2, 0.0, 2.0), (4.6, 1.0)) for _ in range(3)]
    base_items = [{'key': i, 'pos': 100.0 + 3.0 * i, 'span': (-50.0, 50.0)}
                  for i in range(3)]
    cfg = _plan_cfg()
    anchor = (40.0, 0.0)
    plan = ordered_plan(anchor, base_items, tags, engine.LOWER_LEFT, 'v',
                        cfg, 3.0, 4.0, 3.0)
    plan2, anchor2, moved = _PARTS['nudge_clear'](
        anchor, base_items, tags, engine.LOWER_LEFT, 'v', cfg,
        3.0, 4.0, 3.0, plan)
    assert moved == 0.0
    assert anchor2 == anchor
    assert plan2 is plan


# ---------------------------------------------------------------------------
# R1: exit-left corners correct once handed a derived datum edge
# ---------------------------------------------------------------------------
def test_exit_left_corrects_u_when_handed_the_datum_edge():
    # The right-side gap, closed: the box's left edge is leader, but the
    # caller derives the true edge (learned per-project head-to-edge, or
    # the text-width fallback) and u corrects like any other axis.
    u_span = (5.0, 43.0)         # left = arrow on the pipes, right clean
    v_span = (10.0, 11.1)
    fix = CFS(u_span, v_span, (40.0, 10.0), engine.LOWER_RIGHT,
              leader_rises=True, text_left=39.5)
    assert fix == (pytest.approx(-0.5), 0.0)


def test_exit_left_without_a_derived_edge_still_skips_u():
    u_span = (5.0, 43.0)
    v_span = (10.0, 11.1)
    assert CFS(u_span, v_span, (40.0, 10.0), engine.LOWER_RIGHT,
               leader_rises=True) is None


def test_exit_right_ignores_the_derived_edge_and_measures():
    # When the box CAN be measured, measurement wins over derivation.
    u_span = (39.5, 42.5)
    v_span = (10.0, 11.1)
    fix = CFS(u_span, v_span, (40.0, 10.0), engine.LOWER_LEFT,
              leader_rises=True, text_left=38.0)
    assert fix == (pytest.approx(-0.5), 0.0)


def test_exit_left_derived_edge_composes_with_the_landing_trick():
    # Right-side stack ABOVE the pipes: u from the derived edge, v from
    # 2*mid - top - both unverifiable axes recovered at once.
    u_span = (5.0, 43.0)
    v_span = (5.0, 11.6)         # bottom = arrow, top clean
    fix = CFS(u_span, v_span, (40.0, 10.0), engine.UPPER_RIGHT,
              elbow_v=11.05, leader_rises=False, text_left=39.6)
    assert fix == (pytest.approx(-0.4), pytest.approx(0.5))


# ---------------------------------------------------------------------------
# Per-row calibration: a tag's own drawn distance beats every estimate
# ---------------------------------------------------------------------------
def test_a_calibrated_tag_places_by_its_own_drawn_distance():
    # 2026-08-09 image: hint-placed columns come out ragged because each
    # estimate errs differently. Once a tag has been measured from its
    # own drawing, that value wins over box AND hint.
    t = _Tag((100.0, 110.0, 0.0, 2.0), (105.0, 1.0))
    t.learned_left_ft = 4.2
    (head_du, _), _, _ = ordered_offsets(t, engine.UPPER_LEFT)
    assert head_du == pytest.approx(4.2)


def test_calibration_beats_the_hint_on_a_poisoned_box():
    t = _poisoned_tag()
    t.learned_left_ft = 4.2
    (head_du, _), _, _ = ordered_offsets(t, engine.LOWER_RIGHT)
    assert head_du == pytest.approx(4.2)


def test_uncalibrated_tags_keep_the_existing_ladder():
    # No calibration: hint caps a poisoned box; honest boxes measure.
    (head_du, _), _, _ = ordered_offsets(_poisoned_tag(),
                                         engine.LOWER_LEFT)
    assert head_du == pytest.approx(9.2 / 2.0)


def test_calibrated_rows_make_a_flush_column_end_to_end():
    # Three tags whose estimates would scatter them; their calibrated
    # distances differ per tag (centre-head, different widths) and the
    # planned left edges all land on the anchor column.
    widths = (8.0, 9.2, 10.4)
    tags = []
    for w in widths:
        t = _Tag((0.0, w, 0.0, 2.0), (w / 2.0, 1.0))
        t.learned_left_ft = w / 2.0        # its own drawn truth
        tags.append(t)
    base_items = [{'key': i, 'pos': 100.0 + 3.0 * i, 'span': (-50.0, 50.0)}
                  for i in range(3)]
    plan = ordered_plan((40.0, 0.0), base_items, tags, engine.LOWER_LEFT,
                        'v', _plan_cfg(), 3.0, 4.0, 3.0)
    for entry in plan:
        w = widths[entry['key']]
        left = entry['head'][0] - w / 2.0
        assert left == pytest.approx(40.0)


def test_zero_is_a_legitimate_calibrated_distance():
    # This project's family anchors the head ON the text's left edge:
    # calibration measures 0, and 0 must be applied, banked and loaded
    # - treating it as "unset" (0.0 is falsy) discarded the calibration
    # and left reruns on ragged estimates (2026-08-09).
    t = _poisoned_tag()
    t.learned_left_ft = 0.0
    (head_du, _), _, _ = ordered_offsets(t, engine.LOWER_LEFT)
    assert head_du == 0.0


def test_zero_distance_column_is_flush_end_to_end():
    tags = []
    for w in (8.0, 9.2, 10.4):
        t = _Tag((0.0, w, 0.0, 2.0), (0.0, 1.0))   # head ON the left edge
        t.learned_left_ft = 0.0
        tags.append(t)
    base_items = [{'key': i, 'pos': 100.0 + 3.0 * i, 'span': (-50.0, 50.0)}
                  for i in range(3)]
    plan = ordered_plan((40.0, 0.0), base_items, tags, engine.LOWER_LEFT,
                        'v', _plan_cfg(), 3.0, 4.0, 3.0)
    for entry in plan:
        assert entry['head'][0] == pytest.approx(40.0)   # heads ON column
