# -*- coding: utf-8 -*-
"""Unit tests for the chain clustering module."""

import clusters


def test_empty_and_single():
    assert clusters.chain_clusters([], 5.0) == []
    assert clusters.chain_clusters([(3.0, 4.0)], 5.0) == [[0]]


def test_non_positive_distance_disables_splitting():
    points = [(0.0, 0.0), (100.0, 0.0), (500.0, 500.0)]
    assert clusters.chain_clusters(points, 0.0) == [[0, 1, 2]]
    assert clusters.chain_clusters(points, -1.0) == [[0, 1, 2]]


def test_two_distant_groups_split():
    # The user's scenario: a riser group and a lower junction far apart.
    points = [(100.0, 50.0), (102.0, 48.0), (104.0, 52.0),  # group 1
              (400.0, -200.0), (402.0, -198.0)]             # group 2
    result = clusters.chain_clusters(points, 10.0)
    assert result == [[0, 1, 2], [3, 4]]


def test_chaining_beats_distance_to_centre():
    # Points 1.5 apart in a long run: ends are 7.5 apart (> distance),
    # but chaining keeps the run as ONE cluster.
    points = [(i * 1.5, 0.0) for i in range(6)]
    assert clusters.chain_clusters(points, 2.0) == [[0, 1, 2, 3, 4, 5]]


def test_threshold_is_inclusive():
    assert clusters.chain_clusters([(0.0, 0.0), (2.0, 0.0)], 2.0) == [[0, 1]]
    assert clusters.chain_clusters([(0.0, 0.0), (2.01, 0.0)], 2.0) == \
        [[0], [1]]


def test_reading_order_left_to_right_then_top_to_bottom():
    points = [(500.0, 0.0),    # right cluster
              (0.0, -300.0),   # left-bottom cluster
              (0.0, 300.0)]    # left-top cluster
    result = clusters.chain_clusters(points, 10.0)
    assert result == [[2], [1], [0]]  # left-top, left-bottom, right


def test_members_keep_input_order():
    points = [(4.0, 0.0), (0.0, 0.0), (2.0, 0.0)]
    assert clusters.chain_clusters(points, 3.0) == [[0, 1, 2]]


def test_count_cap_splits_at_largest_gap():
    # Two 6-point sub-runs 1.5 apart chain together (distance 2), but 12
    # members exceed the 10-tag cap: the chain cuts at the widest gap.
    points = [(i * 1.0, 0.0) for i in range(6)] + \
             [(6.5 + i * 1.0, 0.0) for i in range(6)]
    result = clusters.chain_clusters(points, 2.0)
    assert result == [[0, 1, 2, 3, 4, 5], [6, 7, 8, 9, 10, 11]]


def test_span_cap_halves_uniform_corridors():
    # 8 points spaced 1.0: count is fine, but the 7-wide span exceeds the
    # 4.0 cap; equal gaps cut centrally, not one tag at a time.
    points = [(i * 1.0, 0.0) for i in range(8)]
    result = clusters.chain_clusters(points, 2.0, max_span=4.0)
    assert result == [[0, 1, 2, 3], [4, 5, 6, 7]]


def _vpipe(u, lo=0.0, hi=100.0):
    return ((u, lo), (u, hi))


def _hpipe(v, lo=0.0, hi=100.0):
    return ((lo, v), (hi, v))


def test_bundle_parallel_rack_is_one_cluster():
    pipes = [_vpipe(0.0), _vpipe(0.3), _vpipe(0.6)]
    arrows = [(0.0, 50.0), (0.3, 51.0), (0.6, 52.0)]
    assert clusters.bundle_clusters(pipes, arrows, 2.0, 5.0) == [[0, 1, 2]]


def test_bundle_perpendicular_never_chains():
    # A vertical and a horizontal pipe crossing at one point, arrows
    # adjacent - still two clusters, always.
    pipes = [_vpipe(0.0), _hpipe(50.0)]
    arrows = [(0.0, 50.0), (0.5, 50.0)]
    result = clusters.bundle_clusters(pipes, arrows, 2.0, 5.0)
    assert len(result) == 2


def test_bundle_two_racks_laterally_apart_stay_separate():
    # Two vertical risers 10 apart with arrows close in v: different
    # racks, different clusters - proximity chaining got this wrong.
    pipes = [_vpipe(0.0), _vpipe(10.0)]
    arrows = [(0.0, 50.0), (10.0, 50.0)]
    result = clusters.bundle_clusters(pipes, arrows, 2.0, 5.0)
    assert len(result) == 2


def test_bundle_same_rack_far_stations_stay_separate():
    # One long pipe tagged at both ends: same rack, but the stations are
    # 70 apart along the run - two clusters.
    pipes = [_vpipe(0.0), _vpipe(0.0)]
    arrows = [(0.0, 10.0), (0.0, 80.0)]
    result = clusters.bundle_clusters(pipes, arrows, 2.0, 5.0)
    assert len(result) == 2


def test_bundle_chaining_is_transitive():
    # A-B and B-C within the lateral limit, A-C beyond it: still one
    # rack via the chain.
    pipes = [_vpipe(0.0), _vpipe(1.5), _vpipe(3.0)]
    arrows = [(0.0, 50.0), (1.5, 50.0), (3.0, 50.0)]
    assert clusters.bundle_clusters(pipes, arrows, 2.0, 5.0) == [[0, 1, 2]]


def test_bundle_angle_tolerance():
    import math
    # A short pipe 8 degrees off vertical, laterally adjacent: same
    # rack. The same geometry at 20 degrees fails on ANGLE (its midpoint
    # stays within the lateral limit, so the angle test is what decides).
    tilt8 = ((0.5, 0.0), (0.5 + 10.0 * math.sin(math.radians(8.0)),
                          10.0 * math.cos(math.radians(8.0))))
    tilt20 = ((0.5, 0.0), (0.5 + 4.0 * math.sin(math.radians(20.0)),
                           4.0 * math.cos(math.radians(20.0))))
    arrows = [(0.0, 5.0), (1.0, 5.0)]
    assert len(clusters.bundle_clusters(
        [_vpipe(0.0, 0.0, 10.0), tilt8], arrows, 2.0, 5.0)) == 1
    assert len(clusters.bundle_clusters(
        [_vpipe(0.0, 0.0, 4.0), tilt20], arrows, 2.0, 5.0)) == 2


def test_bundle_zero_longitudinal_disables():
    pipes = [_vpipe(0.0), _hpipe(50.0)]
    arrows = [(0.0, 50.0), (0.5, 50.0)]
    assert clusters.bundle_clusters(pipes, arrows, 2.0, 0.0) == [[0, 1]]


def test_bundle_count_cap_applies():
    pipes = [_vpipe(i * 0.5) for i in range(12)]
    arrows = [(i * 0.5, 50.0) for i in range(12)]
    result = clusters.bundle_clusters(pipes, arrows, 2.0, 50.0)
    assert all(len(members) <= clusters.MAX_CLUSTER_TAGS
               for members in result)
    assert sorted(sum(result, [])) == list(range(12))


def test_caps_disabled_when_distance_off():
    # Cluster Distance 0 is the user's explicit "never split" switch -
    # the caps must not override it.
    points = [(i * 1.0, 0.0) for i in range(15)]
    assert clusters.chain_clusters(points, 0.0) == [list(range(15))]


def test_bundle_with_no_pipes_is_empty():
    # Every tag in the pick was a non-pipe annotation: the bundle pass has
    # nothing to group and must hand back an empty list, not one empty
    # cluster (which would place a stack with no tags in it).
    assert clusters.bundle_clusters([], [], 2.0, 6.0) == []
