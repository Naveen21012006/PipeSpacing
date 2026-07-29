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


def _load_ordered_offsets():
    source = open(_SCRIPT).read()
    start = source.index('def ordered_offsets')
    end = source.index('def _median')
    namespace = {'engine': engine}
    exec(compile(source[start:end], 'script', 'exec'), namespace)
    return namespace['ordered_offsets']


ordered_offsets = _load_ordered_offsets()


class _Tag(object):
    """A measured tag: text box (u_lo, u_hi, v_lo, v_hi) and head point."""

    def __init__(self, bbox, head):
        self.bbox2d = bbox
        self.head_ref2d = head


# A 10-wide, 2-tall text box sitting at u 100..110, with the head at its
# centre - the usual case for a tag family.
def _tag():
    return _Tag((100.0, 110.0, 0.0, 2.0), (105.0, 1.0))


def _left_edge_u(offsets, anchor_u):
    """Where the text's left edge lands when the corner is at anchor_u."""
    (head_du, _), _, _ = offsets
    head_u = anchor_u + head_du
    return head_u - 5.0        # head sits 5 right of the left edge


@pytest.mark.parametrize('mode', [engine.UPPER_LEFT, engine.LOWER_LEFT])
def test_left_hand_stacks_align_on_their_outside_edge(mode):
    # Leaders exit RIGHT, so the pipes are to the right: the left edge is
    # the OUTSIDE one, and it is what the click places.
    offsets = ordered_offsets(_tag(), mode)
    assert _left_edge_u(offsets, 40.0) == pytest.approx(40.0)


@pytest.mark.parametrize('mode', [engine.UPPER_RIGHT, engine.LOWER_RIGHT])
def test_right_hand_stacks_align_on_their_inside_edge(mode):
    # Leaders exit LEFT, so the pipes are to the left: the left edge is
    # the INSIDE one. Same rule, and the user's correction - it used to
    # be the right edge, which staggered the landings.
    offsets = ordered_offsets(_tag(), mode)
    assert _left_edge_u(offsets, 40.0) == pytest.approx(40.0)


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
