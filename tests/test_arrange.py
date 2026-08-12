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


def test_travel_cap_reverts_runaway_cluster():
    # User decision 2026-08-12: picks are deliberate - the cleanup may
    # drift a cluster about one text width, never drag it across the
    # view (the garage stack: exit pass stretched its leaders because
    # arrows stay pinned while the text walks). Escaping this pinned
    # obstacle needs a 26-unit push against a cap of one text width
    # (10): the cluster snaps back to the pick, is flagged, and the
    # conflict is honestly reported instead of "resolved" far away.
    states = [_state((0.0, 0.0), 100.0, 50.0, movable=False),
              _state((40.0, 20.0), 10.0, 5.0)]
    _, moved, remaining = arrange.resolve(
        states, _replan_factory({0: (100.0, 50.0), 1: (10.0, 5.0)}), 1.0)
    assert moved == []
    assert states[1]['anchor'] == (40.0, 20.0)      # back on the pick
    assert states[1]['capped'] is True
    assert remaining >= 1                           # not swept under the rug


def test_travel_cap_spares_ordinary_moves():
    # A push well inside one text width behaves exactly as before.
    states = [_state((0.0, 0.0), 10.0, 6.0),
              _state((4.0, 2.0), 10.0, 6.0)]
    _, moved, remaining = arrange.resolve(
        states, _replan_factory({0: (10.0, 6.0), 1: (10.0, 6.0)}), 1.0)
    assert moved == [1]
    assert remaining == 0
    assert not any(state.get('capped') for state in states)


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
    # Two clusters whose leaders cross. The later one yields first, but
    # clearing THIS crossing takes ~30 units of drift - nearly four text
    # widths, the exact "stretched across the view on exit" failure the
    # travel cap exists for (2026-08-12). So the later cluster snaps
    # back to its pick, and the EARLIER one clears the crossing with a
    # single small step inside its own cap instead.
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
    assert remaining == 0
    assert states[1]['capped'] is True
    assert states[1]['anchor'] == (0.0, 0.0)     # back on its pick
    assert moved == [0]
    # The earlier cluster helped out, but stayed within its own cap.
    drift = ((states[0]['anchor'][0] - 0.0) ** 2
             + (states[0]['anchor'][1] - 10.0) ** 2) ** 0.5
    assert drift <= 8.0


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
