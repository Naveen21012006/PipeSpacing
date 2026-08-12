# -*- coding: utf-8 -*-
"""Final-arrangement solver for the Align Tags tool.

After every cluster has been placed, this module detects conflicts BETWEEN
clusters and resolves them by moving whole stacks:

    rect-rect   two stacks' text blocks overlap,
    seg-rect    one cluster's leader passes through another's text block,
    seg-seg     leaders of different clusters cross each other.

Resolution policy (user: "a well built automated version"):
  * The EARLIER-placed cluster wins; the later one yields and moves.
  * Every push clears the specific conflict plus a margin, in the
    direction that costs the smallest displacement.
  * After each move the yielding cluster is REPLANNED via a caller-supplied
    callback (its leaders re-derive from the new anchor), and the scan
    repeats until clean or the iteration cap is hit - remaining conflicts
    are reported, never silently ignored.
  * A cluster may drift AT MOST about one text width from where the user
    put it (user decision 2026-08-12: picks are deliberate and land on
    the click to the millimetre - an uncapped chain of pushes dragged a
    garage stack across the view on exit, stretching its leaders, since
    the arrows stay pinned on the pipes while the text walks). A cluster
    pushed past its cap SNAPS BACK to its original spot, is pinned, gets
    ``capped=True``, and its conflict is left for the report.

Pure Python (no Revit imports): the caller supplies, per cluster, an
anchor, a text-block rectangle, leader segments, and a replan callback.
Rectangles are (lo_u, lo_v, hi_u, hi_v); segments are ((u, v), (u, v)).
"""

from __future__ import division

import math

MAX_ITERATIONS = 60


# ---------------------------------------------------------------------------
# Geometry primitives
# ---------------------------------------------------------------------------
def _rects_overlap(a, b, margin):
    return not (a[2] + margin <= b[0] or b[2] + margin <= a[0]
                or a[3] + margin <= b[1] or b[3] + margin <= a[1])


def _rect_push(mover_rect, fixed_rect, margin):
    """Smallest translation that clears mover_rect from fixed_rect."""
    options = [
        (fixed_rect[0] - (mover_rect[2] + margin), 0.0),   # push left
        (fixed_rect[2] + margin - mover_rect[0], 0.0),     # push right
        (0.0, fixed_rect[1] - (mover_rect[3] + margin)),   # push down
        (0.0, fixed_rect[3] + margin - mover_rect[1]),     # push up
    ]
    return min(options, key=lambda p: abs(p[0]) + abs(p[1]))


def _seg_rect_hit(p, q, rect, margin):
    """Does segment p-q intrude on the margin-inflated rectangle?

    Contact at EXACTLY the margin counts as clear (0.999 factor) - the
    push formulas place things precisely one margin away, and an
    inclusive test here would loop forever on that boundary.
    """
    inflate = margin * 0.999
    lo_u, lo_v = rect[0] - inflate, rect[1] - inflate
    hi_u, hi_v = rect[2] + inflate, rect[3] + inflate
    # Liang-Barsky style clipping of the parametric segment.
    du, dv = q[0] - p[0], q[1] - p[1]
    t0, t1 = 0.0, 1.0
    for delta, lo_edge, hi_edge, start in (
            (du, lo_u, hi_u, p[0]), (dv, lo_v, hi_v, p[1])):
        if abs(delta) < 1e-12:
            if start < lo_edge or start > hi_edge:
                return False
            continue
        r0 = (lo_edge - start) / delta
        r1 = (hi_edge - start) / delta
        if r0 > r1:
            r0, r1 = r1, r0
        t0, t1 = max(t0, r0), min(t1, r1)
        if t0 > t1:
            return False
    return True


def _seg_rect_push(p, q, rect, margin):
    """Push vector clearing the rectangle off segment p-q.

    Landings are near-horizontal and climbs near-vertical, so the cheap
    and correct move is perpendicular to the segment's dominant axis,
    whichever side is closer.
    """
    clear = margin * 1.05    # overshoot slightly so progress is certain
    if abs(q[0] - p[0]) >= abs(q[1] - p[1]):   # horizontal-ish: push in v
        seg_v = (p[1] + q[1]) / 2.0
        down = seg_v - clear - rect[3]
        up = seg_v + clear - rect[1]
        return (0.0, down) if abs(down) <= abs(up) else (0.0, up)
    seg_u = (p[0] + q[0]) / 2.0                # vertical-ish: push in u
    left = seg_u - clear - rect[2]
    right = seg_u + clear - rect[0]
    return (left, 0.0) if abs(left) <= abs(right) else (right, 0.0)


def _orient(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segs_cross(p1, p2, p3, p4):
    """Proper segment intersection (shared endpoints don't count)."""
    d1 = _orient(p3, p4, p1)
    d2 = _orient(p3, p4, p2)
    d3 = _orient(p1, p2, p3)
    d4 = _orient(p1, p2, p4)
    return ((d1 > 1e-9) != (d2 > 1e-9)) and ((d3 > 1e-9) != (d4 > 1e-9)) \
        and abs(d1 - d2) > 1e-9 and abs(d3 - d4) > 1e-9


# ---------------------------------------------------------------------------
# Conflict scan
# ---------------------------------------------------------------------------
def _find_conflict(states, margin):
    """First conflict in deterministic order, with a suggested push.

    Returns (mover_candidate_a, mover_candidate_b, push_for_b) where the
    push clears cluster b from cluster a; the caller decides who yields.
    """
    count = len(states)
    for i in range(count):
        for j in range(i + 1, count):
            a, b = states[i], states[j]
            if not a.get('movable', True) and not b.get('movable', True):
                continue   # two pinned obstacles: not ours to solve
            if _rects_overlap(a['rect'], b['rect'], margin):
                return i, j, _rect_push(b['rect'], a['rect'], margin)
            for p, q in a['segments']:
                if _seg_rect_hit(p, q, b['rect'], margin):
                    return i, j, _seg_rect_push(p, q, b['rect'], margin)
            for p, q in b['segments']:
                if _seg_rect_hit(p, q, a['rect'], margin):
                    # Clearing a's rect means moving b's segment: move b
                    # the OPPOSITE of what would move a clear.
                    push = _seg_rect_push(p, q, a['rect'], margin)
                    return i, j, (-push[0], -push[1])
            for p, q in a['segments']:
                for r, s in b['segments']:
                    if _segs_cross(p, q, r, s):
                        # Nudge b away from a, vertically - the scan
                        # repeats after the replan, so a small step that
                        # monotonically separates the stacks converges.
                        direction = 1.0 if _centre_v(b) >= _centre_v(a) \
                            else -1.0
                        return i, j, (0.0, direction * 2.0 * margin)
    return None


def _centre_v(state):
    rect = state['rect']
    return (rect[1] + rect[3]) / 2.0


def count_conflicts(states, margin):
    """Total remaining conflicts (for honest reporting)."""
    total = 0
    count = len(states)
    for i in range(count):
        for j in range(i + 1, count):
            a, b = states[i], states[j]
            if (not a.get('movable', True) and not b.get('movable', True)
                    and not (a.get('capped') or b.get('capped'))):
                # Pinned-vs-pinned: pre-existing, not counted. A CAPPED
                # cluster is pinned too, but its conflict is ours - the
                # cap chose honesty over resolution, so report it.
                continue
            if _rects_overlap(a['rect'], b['rect'], margin):
                total += 1
            total += sum(1 for p, q in a['segments']
                         if _seg_rect_hit(p, q, b['rect'], margin))
            total += sum(1 for p, q in b['segments']
                         if _seg_rect_hit(p, q, a['rect'], margin))
            total += sum(1 for p, q in a['segments']
                         for r, s in b['segments']
                         if _segs_cross(p, q, r, s))
    return total


# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------
def resolve(states, replan, margin, max_iterations=MAX_ITERATIONS):
    """Iteratively clear conflicts by moving the LATER cluster of a pair.

    Args:
        states: list of dicts per cluster, in PLACEMENT order:
            ``anchor``   (u, v) current stack anchor,
            ``rect``     (lo_u, lo_v, hi_u, hi_v) text block,
            ``segments`` leader segments [((u,v),(u,v)), ...],
            ``movable``  False pins a cluster (never moved).
        replan: callback(index, anchor) -> {'rect':..., 'segments':...}
            recomputing a cluster's geometry for a new anchor.
        margin: minimum separation between anything and anything.
        max_iterations: hard cap; loop always terminates.

    Returns:
        (states, moved_indices, remaining_conflicts) - states mutated in
        place with final anchors/geometry. A cluster whose pushes would
        exceed its travel cap is reverted to its original state, pinned,
        and flagged ``capped=True``; it never appears in moved_indices,
        and its conflicts count as remaining.
    """
    moved = set()
    originals = {}
    # Travel cap: about one text width of drift, floored for degenerate
    # rects so a failed measurement cannot make every push a revert.
    caps = {}
    for index, state in enumerate(states):
        rect = state['rect']
        caps[index] = max(rect[2] - rect[0], 4.0 * margin)
    for _ in range(max_iterations):
        conflict = _find_conflict(states, margin)
        if conflict is None:
            break
        i, j, push_for_j = conflict
        if states[j].get('movable', True):
            mover, push = j, push_for_j
        elif states[i].get('movable', True):
            mover, push = i, (-push_for_j[0], -push_for_j[1])
        else:
            break   # both pinned: unresolvable, reported below
        if mover not in originals:
            originals[mover] = (states[mover]['anchor'],
                                states[mover]['rect'],
                                states[mover]['segments'])
        anchor_u, anchor_v = states[mover]['anchor']
        new_anchor = (anchor_u + push[0], anchor_v + push[1])
        home = originals[mover][0]
        drift = math.hypot(new_anchor[0] - home[0], new_anchor[1] - home[1])
        if drift > caps[mover]:
            # The user's pick is deliberate: snap back, pin, report.
            home_anchor, home_rect, home_segments = originals[mover]
            states[mover]['anchor'] = home_anchor
            states[mover]['rect'] = home_rect
            states[mover]['segments'] = home_segments
            states[mover]['movable'] = False
            states[mover]['capped'] = True
            moved.discard(mover)
            continue
        fresh = replan(mover, new_anchor)
        states[mover]['anchor'] = new_anchor
        states[mover]['rect'] = fresh['rect']
        states[mover]['segments'] = fresh['segments']
        moved.add(mover)
    return states, sorted(moved), count_conflicts(states, margin)
