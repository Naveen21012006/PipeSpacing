# -*- coding: utf-8 -*-
"""Unit tests for the final-arrangement solver."""

import pytest

import arrange


def _state(anchor, width, height, segments=None, movable=True):
    """A cluster state whose rect hangs off its anchor (bottom-left)."""
    return {
        'anchor': anchor,
        'rect': (anchor[0], anchor[1], anchor[0] + width,
                 anchor[1] + height),
        'segments': segments or [],
        'movable': movable,
    }


def _replan_factory(dims):
    """Replan callback: rect follows the anchor, segments stay simple."""
    def replan(index, anchor):
        width, height = dims[index]
        return {'rect': (anchor[0], anchor[1], anchor[0] + width,
                         anchor[1] + height),
                'segments': []}
    return replan


def test_no_conflicts_no_moves():
    states = [_state((0.0, 0.0), 10.0, 5.0),
              _state((50.0, 0.0), 10.0, 5.0)]
    _, moved, remaining = arrange.resolve(
        states, _replan_factory({0: (10.0, 5.0), 1: (10.0, 5.0)}), 1.0)
    assert moved == []
    assert remaining == 0


def test_overlapping_stacks_later_yields():
    # Cluster 1 (placed second) overlaps cluster 0: cluster 1 must move,
    # cluster 0 must stay exactly where the user put it.
    states = [_state((0.0, 0.0), 10.0, 6.0),
              _state((4.0, 2.0), 10.0, 6.0)]
    _, moved, remaining = arrange.resolve(
        states, _replan_factory({0: (10.0, 6.0), 1: (10.0, 6.0)}), 1.0)
    assert moved == [1]
    assert remaining == 0
    assert states[0]['anchor'] == (0.0, 0.0)
    # Separated by at least the margin.
    a, b = states[0]['rect'], states[1]['rect']
    assert (b[0] >= a[2] + 1.0 or b[2] <= a[0] - 1.0
            or b[1] >= a[3] + 1.0 or b[3] <= a[1] - 1.0)


def test_pinned_cluster_forces_the_other_to_move():
    states = [_state((0.0, 0.0), 10.0, 6.0, movable=False),
              _state((4.0, 2.0), 10.0, 6.0, movable=False)]
    states[1]['movable'] = True
    _, moved, _ = arrange.resolve(
        states, _replan_factory({0: (10.0, 6.0), 1: (10.0, 6.0)}), 1.0)
    assert moved == [1]

    # Reverse pinning: the EARLIER cluster yields when the later is pinned.
    states = [_state((0.0, 0.0), 10.0, 6.0),
              _state((4.0, 2.0), 10.0, 6.0, movable=False)]
    _, moved, _ = arrange.resolve(
        states, _replan_factory({0: (10.0, 6.0), 1: (10.0, 6.0)}), 1.0)
    assert moved == [0]


def test_both_pinned_is_ignored():
    # Two pre-existing (pinned) annotations overlapping each other are
    # not this tool's business: no moves, no reported conflicts.
    states = [_state((0.0, 0.0), 10.0, 6.0, movable=False),
              _state((4.0, 2.0), 10.0, 6.0, movable=False)]
    _, moved, remaining = arrange.resolve(
        states, _replan_factory({0: (10.0, 6.0), 1: (10.0, 6.0)}), 1.0)
    assert moved == []
    assert remaining == 0


def test_movable_cluster_avoids_pinned_obstacle():
    # A new cluster landing on an old (pinned) annotation must move off
    # it; the obstacle stays exactly where it is.
    states = [_state((4.0, 2.0), 10.0, 6.0),
              _state((0.0, 0.0), 10.0, 6.0, movable=False)]
    _, moved, remaining = arrange.resolve(
        states, _replan_factory({0: (10.0, 6.0), 1: (10.0, 6.0)}), 1.0)
    assert moved == [0]
    assert remaining == 0
    assert states[1]['anchor'] == (0.0, 0.0)


def test_leader_through_stack_pushes_stack_clear():
    # Cluster 0's long horizontal landing passes through cluster 1's text.
    landing = [((0.0, 10.0), (60.0, 10.0))]
    states = [_state((0.0, 20.0), 10.0, 5.0, segments=landing),
              _state((30.0, 8.0), 12.0, 6.0)]
    _, moved, remaining = arrange.resolve(
        states, _replan_factory({0: (10.0, 5.0), 1: (12.0, 6.0)}), 1.0)
    assert moved == [1]
    assert remaining == 0
    rect = states[1]['rect']
    assert rect[1] >= 11.0 or rect[3] <= 9.0   # clear of the landing +- 1


def test_crossing_leaders_separate():
    # Two clusters whose leaders cross: the later one is nudged until the
    # crossing disappears (its segments move with its anchor).
    def replan(index, anchor):
        # Cluster 1's leader runs from its anchor to a fixed arrow.
        seg = [((anchor[0], anchor[1]), (40.0, anchor[1] + 30.0))]
        return {'rect': (anchor[0], anchor[1], anchor[0] + 8.0,
                         anchor[1] + 4.0),
                'segments': seg}
    states = [
        _state((0.0, 10.0), 8.0, 4.0,
               segments=[((0.0, 10.0), (40.0, 0.0))]),
        dict(replan(1, (0.0, 0.0)), anchor=(0.0, 0.0), movable=True),
    ]
    _, moved, remaining = arrange.resolve(states, replan, 1.0)
    assert moved == [1]
    assert remaining == 0


def test_chain_of_overlaps_converges():
    # Three stacks all dropped on one spot: solver spreads them out.
    dims = {0: (10.0, 5.0), 1: (10.0, 5.0), 2: (10.0, 5.0)}
    states = [_state((0.0, 0.0), 10.0, 5.0),
              _state((1.0, 1.0), 10.0, 5.0),
              _state((2.0, 2.0), 10.0, 5.0)]
    _, moved, remaining = arrange.resolve(
        states, _replan_factory(dims), 1.0)
    assert remaining == 0
    assert 0 not in moved          # first placement never moves
    assert set(moved) <= {1, 2}


def test_deterministic():
    def run():
        states = [_state((0.0, 0.0), 10.0, 6.0),
                  _state((4.0, 2.0), 10.0, 6.0),
                  _state((6.0, 3.0), 10.0, 6.0)]
        arrange.resolve(
            states, _replan_factory({0: (10.0, 6.0), 1: (10.0, 6.0),
                                     2: (10.0, 6.0)}), 1.0)
        return [s['anchor'] for s in states]
    assert run() == run()


# ---------------------------------------------------------------------------
# Push direction on near-vertical leaders (climbs)
# ---------------------------------------------------------------------------
def test_vertical_segment_pushes_the_nearer_way_left():
    # Climb along u=0; the text block sits to its LEFT, so the cheap move
    # is further left.
    push = arrange._seg_rect_push((0.0, 0.0), (0.0, 10.0),
                                  (-6.0, 0.0, -2.0, 10.0), 1.0)
    assert push[1] == 0.0
    assert push[0] > 0.0 or push[0] < 0.0     # a u-move, never a v-move
    assert abs(push[0]) == pytest.approx(0.95)


def test_vertical_segment_pushes_the_nearer_way_right():
    # Same climb, text block to its RIGHT: mirrored, still a u-move.
    push = arrange._seg_rect_push((0.0, 0.0), (0.0, 10.0),
                                  (2.0, 0.0, 6.0, 10.0), 1.0)
    assert push[1] == 0.0
    assert push[0] == pytest.approx(-0.95)


# ---------------------------------------------------------------------------
# Conflict detection corners
# ---------------------------------------------------------------------------
def test_later_leader_through_an_earlier_stack_moves_the_later_one():
    # Cluster 0 is a bare text block; cluster 1's LEADER runs through it.
    # The later cluster must yield, and its push is away from cluster 0.
    a = _state((0.0, 0.0), 10.0, 6.0)
    b = _state((30.0, 0.0), 10.0, 6.0,
               segments=[((5.0, 3.0), (30.0, 3.0))])
    conflict = arrange._find_conflict([a, b], 1.0)
    assert conflict is not None
    i, j, push = conflict
    assert (i, j) == (0, 1)
    assert push != (0.0, 0.0)


def test_count_conflicts_sees_plain_rect_overlap():
    a = _state((0.0, 0.0), 10.0, 6.0)
    b = _state((4.0, 2.0), 10.0, 6.0)
    assert arrange.count_conflicts([a, b], 1.0) == 1
    assert arrange.count_conflicts([a, b], 0.0) == 1


# ---------------------------------------------------------------------------
# Termination guarantees
# ---------------------------------------------------------------------------
def test_solver_stops_at_the_iteration_cap():
    # A replan that ignores the new anchor never clears anything: the
    # loop must still end, and report the conflict honestly.
    states = [_state((0.0, 0.0), 10.0, 6.0),
              _state((4.0, 2.0), 10.0, 6.0)]

    def stubborn_replan(index, _anchor):
        return {'rect': states[index]['rect'], 'segments': []}

    _, moved, remaining = arrange.resolve(states, stubborn_replan, 1.0,
                                          max_iterations=5)
    assert moved == [1]
    assert remaining >= 1          # never claims a clean result


def test_two_pinned_clusters_are_reported_not_looped_on(monkeypatch):
    # Defensive path: if a conflict ever surfaces between two pinned
    # clusters, resolve() must give up immediately rather than spin.
    states = [_state((0.0, 0.0), 10.0, 6.0, movable=False),
              _state((4.0, 2.0), 10.0, 6.0, movable=False)]
    calls = []

    def fake_conflict(_states, _margin):
        calls.append(1)
        return 0, 1, (5.0, 0.0)

    monkeypatch.setattr(arrange, '_find_conflict', fake_conflict)

    def replan(index, anchor):
        raise AssertionError('a pinned cluster must never be replanned')

    _, moved, _ = arrange.resolve(states, replan, 1.0)
    assert moved == []
    assert len(calls) == 1         # gave up on the first look
