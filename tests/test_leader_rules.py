# -*- coding: utf-8 -*-
"""Unit tests for the Annotation Dashboard's leader geometry.

Only plan_leader is pure; it is imported directly from the dashboard
bundle so the Revit-dependent parts of that module stay untouched.
"""

import math
import os

import pytest

import engine

_DASH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'CKR Tools.tab', 'Annotation.panel', 'AnnotationDashboard.pushbutton',
    'leader_rules.py')


class _FakeCommon(object):
    """Stands in for common.py: the tests work in view-plane 2D already."""

    @staticmethod
    def to_2d(point, _basis):
        return point


def _load_pure_parts():
    """Import the pure functions without the Revit-only imports."""
    source = open(_DASH).read()
    start = source.index('def resolve_justification')
    end = source.index('def apply_rules')
    namespace = {'math': math, 'engine': engine, 'common': _FakeCommon}
    exec(compile(source[start:end], 'leader_rules', 'exec'), namespace)
    return namespace['plan_leader'], namespace['resolve_justification']


plan_leader, resolve_justification = _load_pure_parts()


class _FakeWrapper(object):
    """A tag that points from ``head`` to ``end`` (2D, view plane)."""

    def __init__(self, head, end):
        self._head = head
        self._end = end

    def primary_leader(self):
        return 0, self._end

    def get_head(self):
        return self._head


def test_level_element_keeps_a_straight_line():
    elbow = plan_leader((0.0, 0.0), (40.0, 0.0), 'v', True, 0.0, 10.0, 2.0)
    assert elbow[1] == pytest.approx(0.0)
    assert 0.0 < elbow[0] < 40.0        # grip parked along the line


def test_horizontal_pipe_gets_true_90_degrees():
    # Elbow directly above the arrow: landing then a square drop.
    elbow = plan_leader((0.0, 0.0), (40.0, -12.0), 'h', True, 0.0, 10.0, 2.0)
    assert elbow == pytest.approx((40.0, 0.0))


def test_vertical_pipe_climb_leans_by_tilt():
    head, end = (0.0, 0.0), (40.0, 12.0)
    elbow = plan_leader(head, end, 'v', True, 0.0, 10.0, 2.0)
    assert elbow[1] == pytest.approx(0.0)
    lean = math.degrees(math.atan2(abs(end[0] - elbow[0]),
                                   abs(end[1] - elbow[1])))
    assert lean == pytest.approx(engine.TILT_DEG, abs=1e-6)


def test_climb_never_overshoots_the_arrow():
    # Even when the rise is huge, the bend stays on the text's side of
    # the arrow (run clamps at 0) and keeps its lean.
    elbow = plan_leader((0.0, 0.0), (5.0, 300.0), 'v', True, 0.0, 10.0, 4.0)
    assert elbow[0] == pytest.approx(0.0)
    assert elbow[1] == pytest.approx(0.0)


def test_slanted_mode_respects_the_elbow_gap():
    # A short slanted leader: the bend stays elbow_gap from the arrow.
    elbow = plan_leader((0.0, 0.0), (5.0, -30.0), 'v', False, 45.0, 10.0,
                        4.0)
    assert elbow[0] == pytest.approx(1.0)   # 5 - 4


def test_slanted_mode_uses_the_landing():
    elbow = plan_leader((0.0, 0.0), (40.0, -20.0), 'v', False, 45.0, 10.0,
                        2.0)
    assert elbow == pytest.approx((10.0, 0.0))


def test_landing_never_passes_the_arrow():
    elbow = plan_leader((0.0, 0.0), (6.0, -20.0), None, False, 45.0, 100.0,
                        2.0)
    assert elbow[0] == pytest.approx(4.0)     # 6 - elbow_gap


def test_left_pointing_leaders_mirror():
    elbow = plan_leader((0.0, 0.0), (-40.0, -12.0), 'h', True, 0.0, 10.0,
                        2.0)
    assert elbow == pytest.approx((-40.0, 0.0))
    elbow = plan_leader((0.0, 0.0), (-40.0, 12.0), 'v', True, 0.0, 10.0,
                        2.0)
    assert elbow[0] < 0.0
    assert elbow[1] == pytest.approx(0.0)


def test_coincident_points_are_left_alone():
    assert plan_leader((3.0, 3.0), (3.0, 3.0), 'v', True, 0.0, 10.0,
                       2.0) is None


# ---------------------------------------------------------------------------
# Justification
# ---------------------------------------------------------------------------
def test_explicit_justification_passes_through():
    tag = _FakeWrapper((0.0, 0.0), (40.0, 0.0))
    assert resolve_justification('left', tag, None) == 'left'
    assert resolve_justification('right', tag, None) == 'right'


def test_unchanged_writes_nothing():
    tag = _FakeWrapper((0.0, 0.0), (40.0, 0.0))
    assert resolve_justification('unchanged', tag, None) is None
    assert resolve_justification(None, tag, None) is None


def test_automatic_justifies_towards_the_element():
    # Element to the right: text hugs its leader on the right edge.
    right = _FakeWrapper((0.0, 0.0), (40.0, -5.0))
    assert resolve_justification('automatic', right, None) == 'right'
    left = _FakeWrapper((0.0, 0.0), (-40.0, -5.0))
    assert resolve_justification('automatic', left, None) == 'left'


def test_automatic_writes_nothing_without_a_leader():
    # No readable leader end: never guess - the wrapper would take any
    # non-'left' value as Right and silently reformat the note.
    class _NoLeader(_FakeWrapper):
        def primary_leader(self):
            return None, None

    assert resolve_justification('automatic',
                                 _NoLeader((0.0, 0.0), None), None) is None


def test_automatic_survives_an_unreadable_tag():
    class _Broken(_FakeWrapper):
        def get_head(self):
            raise RuntimeError('no head')

    assert resolve_justification('automatic',
                                 _Broken((0.0, 0.0), (5.0, 0.0)),
                                 None) is None
