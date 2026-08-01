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


def _load_pure_parts():
    source = open(_SCRIPT).read()
    start = source.index('def ordered_offsets')
    end = source.index('def _median')
    namespace = {'engine': engine}
    exec(compile(source[start:end], 'script', 'exec'), namespace)
    return namespace


_PARTS = _load_pure_parts()
ordered_offsets = _PARTS['ordered_offsets']
bundle_centre_u = _PARTS['bundle_centre_u']
side_mode = _PARTS['side_mode']


class _Tag(object):
    """A measured tag: text box (u_lo, u_hi, v_lo, v_hi) and head point."""

    def __init__(self, bbox, head):
        self.bbox2d = bbox
        self.head_ref2d = head
        self.id_value = id(self)


# A 10-wide, 2-tall text box. The user's family is LEFT-JUSTIFIED with
# the head on the text's left edge, so head_u == u_lo when the box is
# clean.
def _tag():
    return _Tag((100.0, 110.0, 0.0, 2.0), (100.0, 1.0))


@pytest.mark.parametrize('mode', engine.MODES)
def test_the_head_lands_exactly_on_the_column(mode):
    # No horizontal bounding-box term AT ALL: for this left-justified
    # family the head IS the left edge, so head u == anchor u, whatever
    # the box measured. This is what makes reruns stable - the box
    # contains the old leader, which moves on every run.
    (head_du, _), _, _ = ordered_offsets(_tag(), mode)
    assert head_du == 0.0


@pytest.mark.parametrize('mode', engine.MODES)
def test_a_contaminated_box_cannot_indent_the_row(mode):
    # The 2026-08-02 regression: a leader stub balancing the text makes
    # the box symmetric, the cap never fires, and the head offset came
    # out half the (doubled) box. Zero means zero regardless.
    dirty = _Tag((99.0, 110.0, 0.0, 2.0), (104.5, 1.0))   # leader inside
    (head_du, _), _, _ = ordered_offsets(dirty, mode)
    assert head_du == 0.0


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
