# -*- coding: utf-8 -*-
"""Chain clustering for the Align Tags tool.

Groups tags by where their leader arrows land, so one wide selection can be
aligned as several independent stacks - one pick per group. Pure Python
(no Revit imports): runs identically under pytest and IronPython.

The rule is single-link CHAINING, not distance-to-centre: a point joins a
cluster when it is within the cluster distance of ANY member. A run of
pipes each 1.5 m from the next therefore forms ONE cluster even if its
ends are 10 m apart - exactly how riser groups behave on a plan.
"""

from __future__ import division

import math

# Auto-cap (user rule): a chain along a corridor must never become one
# giant stack. Oversized chains are cut at their largest internal gap.
MAX_CLUSTER_TAGS = 10

# Bundle detection (user rule: "only parallel pipes as one cluster").
PARALLEL_TOL_DEG = 10.0


def _span_of(members, points):
    """Largest axis-aligned extent of a member set."""
    us = [points[i][0] for i in members]
    vs = [points[i][1] for i in members]
    return max(max(us) - min(us), max(vs) - min(vs))


def _split_oversized(members, points, max_tags, max_span):
    """Recursively cut a cluster at its largest gap until it fits the caps."""
    oversized = ((max_tags is not None and len(members) > max_tags)
                 or (max_span is not None and len(members) > 1
                     and _span_of(members, points) > max_span))
    if not oversized or len(members) < 2:
        return [members]

    us = [points[i][0] for i in members]
    vs = [points[i][1] for i in members]
    axis = 0 if (max(us) - min(us)) >= (max(vs) - min(vs)) else 1
    ordered = sorted(members, key=lambda i: points[i][axis])
    gaps = [points[ordered[k + 1]][axis] - points[ordered[k]][axis]
            for k in range(len(ordered) - 1)]
    # Cut at the largest gap; among (near-)equal gaps take the most
    # central one, so a uniform corridor halves instead of shedding one
    # tag at a time.
    best = max(gaps)
    candidates = [k for k, gap in enumerate(gaps) if gap >= best - 1e-9]
    middle = (len(gaps) - 1) / 2.0
    cut = min(candidates, key=lambda k: abs(k - middle)) + 1
    return (_split_oversized(ordered[:cut], points, max_tags, max_span)
            + _split_oversized(ordered[cut:], points, max_tags, max_span))


def bundle_clusters(pipes, arrows, lateral_max, longitudinal_max,
                    max_tags=MAX_CLUSTER_TAGS):
    """Cluster tags into PHYSICAL PIPE BUNDLES.

    Two tags chain into one cluster only when their pipes form one rack:
      1. the pipes run PARALLEL (within PARALLEL_TOL_DEG),
      2. they sit SIDE-BY-SIDE (perpendicular offset <= lateral_max),
      3. the tags mark the same station (arrow separation measured ALONG
         the run <= longitudinal_max - a 30m bundle tagged at both ends
         stays two clusters).
    Chaining is transitive (A||B, B||C -> one rack). This subsumes the
    old direction split: perpendicular pipes can never share a cluster,
    and diagonal racks work too.

    Args:
        pipes: [((u, v), (u, v))] the tagged pipes' 2D endpoints.
        arrows: [(u, v)] each tag's arrow point.
        lateral_max: rack width - max perpendicular pipe separation.
        longitudinal_max: max arrow separation along the run; <= 0
            disables clustering entirely (one group).
        max_tags: oversized-cluster cap (largest-gap split).

    Returns:
        list of index lists in reading order; members keep input order.
    """
    count = len(pipes)
    if count == 0:
        return []
    if longitudinal_max <= 0:
        return [list(range(count))]

    directions = []
    for a, b in pipes:
        du, dv = b[0] - a[0], b[1] - a[1]
        length = math.hypot(du, dv)
        directions.append((du / length, dv / length) if length > 1e-9
                          else (1.0, 0.0))

    cos_tol = math.cos(math.radians(PARALLEL_TOL_DEG))
    parent = list(range(count))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(a, b):
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_a] = root_b

    for i in range(count):
        di = directions[i]
        origin = pipes[i][0]
        for j in range(i + 1, count):
            dj = directions[j]
            if abs(di[0] * dj[0] + di[1] * dj[1]) < cos_tol:
                continue   # not parallel: never the same bundle
            mid_j = ((pipes[j][0][0] + pipes[j][1][0]) / 2.0,
                     (pipes[j][0][1] + pipes[j][1][1]) / 2.0)
            wu, wv = mid_j[0] - origin[0], mid_j[1] - origin[1]
            lateral = abs(wu * (-di[1]) + wv * di[0])
            if lateral > lateral_max:
                continue   # side-by-side test failed: different rack
            au = arrows[j][0] - arrows[i][0]
            av = arrows[j][1] - arrows[i][1]
            longitudinal = abs(au * di[0] + av * di[1])
            if longitudinal > longitudinal_max:
                continue   # same rack, different tagging station
            union(i, j)

    groups = {}
    for i in range(count):
        groups.setdefault(find(i), []).append(i)

    final = []
    for members in groups.values():
        final.extend(_split_oversized(members, arrows, max_tags, None))
    final = [sorted(members) for members in final]

    def reading_order(members):
        centre_u = sum(arrows[i][0] for i in members) / len(members)
        centre_v = sum(arrows[i][1] for i in members) / len(members)
        return (centre_u, -centre_v)

    return sorted(final, key=reading_order)


def chain_clusters(points, distance, max_tags=MAX_CLUSTER_TAGS,
                   max_span=None):
    """Group point indices by single-link chaining within ``distance``.

    Args:
        points: list of (u, v) tuples in view-plane coordinates.
        distance: chaining distance in the same units. A non-positive
            value disables clustering entirely (one group, no caps) -
            the user's explicit "never split" switch.
        max_tags: cut chains larger than this many members (None = off).
        max_span: cut chains wider than this extent (None = off).

    Returns:
        list of index lists in reading order (left-to-right by cluster
        centre, ties top-to-bottom); indices within a cluster keep input
        order. Empty input returns [].
    """
    count = len(points)
    if count == 0:
        return []
    if distance <= 0:
        return [list(range(count))]

    limit_sq = float(distance) * float(distance)
    parent = list(range(count))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]   # path halving
            index = parent[index]
        return index

    def union(a, b):
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_a] = root_b

    for i in range(count):
        u_i, v_i = points[i]
        for j in range(i + 1, count):
            du = points[j][0] - u_i
            dv = points[j][1] - v_i
            if du * du + dv * dv <= limit_sq:
                union(i, j)

    groups = {}
    for i in range(count):
        groups.setdefault(find(i), []).append(i)

    final = []
    for members in groups.values():
        final.extend(_split_oversized(members, points, max_tags, max_span))
    final = [sorted(members) for members in final]  # keep input order

    def reading_order(members):
        centre_u = sum(points[i][0] for i in members) / len(members)
        centre_v = sum(points[i][1] for i in members) / len(members)
        return (centre_u, -centre_v)

    return sorted(final, key=reading_order)
