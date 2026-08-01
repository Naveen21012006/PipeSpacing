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
