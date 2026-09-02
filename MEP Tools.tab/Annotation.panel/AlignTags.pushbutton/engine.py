# -*- coding: utf-8 -*-
"""Pure geometry engine for the Align Tags tool.

Given a picked anchor point and the (fixed) leader-end points of the selected
tags, compute where every tag head and leader elbow must go so that:

  * heads stack from the anchor at the configured vertical spacing,
  * every leader's slanted segment is parallel, at the configured angle,
  * every landing (head -> elbow) is a single straight horizontal segment,
  * no two leaders cross.

This module is deliberately free of Revit imports so it runs identically under
CPython (pytest) and IronPython (inside Revit). Everything is 2D: callers
project Revit XYZ points onto the active view's (right, up) axes before calling
in, and lift the results back afterwards. The engine is unit-agnostic - the
caller decides whether spacings are model or paper distances and pre-scales.

Coordinate conventions
----------------------
``u`` grows to the viewer's right, ``v`` grows up. Points are ``(u, v)``
tuples.

Mode semantics
--------------
The four modes name the quadrant the TAG STACK occupies relative to the
tagged elements:

  UPPER_LEFT   tags above-left;  leaders exit heads rightward, slant DOWN
  UPPER_RIGHT  tags above-right; leaders exit heads leftward,  slant DOWN
  LOWER_LEFT   tags below-left;  leaders exit heads rightward, slant UP
  LOWER_RIGHT  tags below-right; leaders exit heads leftward,  slant UP

The picked anchor is always the LOWEST tag head (matching the on-screen
prompt); the stack always grows upward from it. ``switch_side=True`` mirrors
the leader exit side (swaps the Left/Right variant of the chosen mode).

Leader shape per tag: head --(horizontal landing)--> elbow --(slant at
``angle_deg`` from horizontal)--> end. Ends are never moved by this module.

Non-crossing ordering
---------------------
All slanted segments are parallel, so they cannot cross each other; the only
possible crossings are landing-vs-slant. Each end's slant lies on the line
``u(v) = c -/+ v/tan(angle)`` characterised by its intercept ``c``. Sorting
ends by ``c`` (direction depending on mode) and assigning them bottom-to-top
guarantees that at any landing's height every other slant passes on the far
side of that landing's elbow - proven case-by-case in the test suite with a
brute-force segment-intersection check.

Constant landing
----------------
With ``constant_landing`` the elbow stays on the end's angle-line but the
HEAD is re-derived at ``landing_distance`` from the elbow, so all landings
are equal AND all slants keep the angle. Head ``u`` then follows the elbows
instead of forming a strict column; only the anchor's height (and the row
spacing) is taken from the pick.

Intermittent alignment
----------------------
Alternate rows are offset one ``horizontal_spacing`` AWAY from the elements
(so their landings are longer and can never degenerate), and the row step is
halved: same-column neighbours keep the full vertical spacing while the block
occupies half the height. Constant Landing takes precedence: with a fixed
landing every head's horizontal position is dictated by its leader, leaving
nothing to stagger, so ``intermittent`` is ignored entirely (full row step)
while ``constant_landing`` is on.

Degenerate geometry
-------------------
If an end lies on the wrong side of its computed head (behind the stack, or
above an "upper" stack / below a "lower" one) the exact angle cannot be
honoured with the end fixed. The elbow is then clamped to the head's ``u``
(zero landing) and the plan entry is flagged ``angle_ok=False`` so the caller
can report it; the leader is still drawn and the document is never corrupted.

Author: Naveen
"""

from __future__ import division

import math

UPPER_LEFT = 'UL'
UPPER_RIGHT = 'UR'
LOWER_LEFT = 'LL'
LOWER_RIGHT = 'LR'

MODES = (UPPER_LEFT, UPPER_RIGHT, LOWER_LEFT, LOWER_RIGHT)

MIN_ANGLE_DEG = 1.0
MAX_ANGLE_DEG = 89.0
STRAIGHT_ANGLE_MAX = 0.5   # anything below reads as "straight leaders"

# Climbing leader segments (angle 0, tag not level with its pipe) lean this
# many degrees off vertical - never 0 or 90. The lean keeps a leader
# visually distinct from the orthogonal pipework (a strict vertical rise
# next to a vertical pipe reads as part of the pipe), and because every
# climb shares the same tilt they are parallel and can never cross.
# Originally "less than 10 deg" (7.5); raised 2026-09-03 on the user's
# field verdict from the first congested-corner runs: at 7.5 a short
# climb is visually indistinguishable from riding the pipe ("improper
# leader arrows"). Their spec: "create an angle of 75 degree" from
# horizontal = 15 off vertical. One constant, both buttons.
TILT_DEG = 15.0

_SWITCHED = {
    UPPER_LEFT: UPPER_RIGHT,
    UPPER_RIGHT: UPPER_LEFT,
    LOWER_LEFT: LOWER_RIGHT,
    LOWER_RIGHT: LOWER_LEFT,
}


def clamp_angle(angle_deg):
    """Clamp the leader angle to the valid open range (0, 90) exclusive.

    0 and 90 degrees would make the landing and the slant parallel /
    perpendicular in a way that breaks the elbow construction, so the spec
    excludes them; anything outside [1, 89] is pulled back in.
    """
    try:
        value = float(angle_deg)
    except (TypeError, ValueError):
        return 45.0
    return max(MIN_ANGLE_DEG, min(MAX_ANGLE_DEG, value))


def normalize_angle(angle_deg):
    """Return 0.0 for "straight leaders" (< 0.5 deg), else clamp to [1, 89].

    plan_ordered understands angle 0 as straight, horizontal, elbow-free
    leaders; plan_alignment cannot (its ends are fixed elsewhere), so its
    callers keep using clamp_angle.
    """
    try:
        value = float(angle_deg)
    except (TypeError, ValueError):
        return 45.0
    if value < STRAIGHT_ANGLE_MAX:
        return 0.0
    return clamp_angle(value)


def resolve_mode(mode, switch_side=False):
    """Return the effective mode, honouring the Switch Pick Point Side flag.

    Raises:
        ValueError: If ``mode`` is not one of MODES.
    """
    if mode not in MODES:
        raise ValueError('Unknown alignment mode: {0!r}'.format(mode))
    return _SWITCHED[mode] if switch_side else mode


def exit_sign(mode):
    """Return +1 if leaders exit the head rightward (+u), -1 if leftward."""
    return 1.0 if mode in (UPPER_LEFT, LOWER_LEFT) else -1.0


def slant_sign(mode):
    """Return -1 if slants run DOWN from elbow to end, +1 if UP.

    Upper modes sit above the elements, so their slants descend; lower modes
    ascend.
    """
    return -1.0 if mode in (UPPER_LEFT, UPPER_RIGHT) else 1.0


def _intercept(end, mode, cot):
    """Return the angle-line intercept ``c`` of an end point for a mode.

    The slanted segment through end ``E`` lies on a line of constant slope;
    parameterised by height ``v`` its horizontal position is
    ``u(v) = c + s * v * cot`` where ``s`` depends on the mode. ``c = u(0)``
    identifies the line, and ordering ends by ``c`` is what makes leaders
    non-crossing (see module docstring).
    """
    u, v = end
    if mode == UPPER_LEFT:
        return u + v * cot      # u(v) = c - v*cot
    if mode == UPPER_RIGHT:
        return u - v * cot      # u(v) = c + v*cot
    if mode == LOWER_LEFT:
        return u - v * cot      # u(v) = c + v*cot
    return u + v * cot          # LOWER_RIGHT: u(v) = c - v*cot


def _slant_u_at(end, mode, cot, v):
    """Return the ``u`` of the end's angle-line at height ``v``."""
    c = _intercept(end, mode, cot)
    if mode in (UPPER_LEFT, LOWER_RIGHT):
        return c - v * cot
    return c + v * cot


def _sort_ascending(mode):
    """True when bottom-to-top rows take ends with ASCENDING intercepts.

    Derived per mode from the landing-vs-slant crossing condition (see the
    module docstring); the property test brute-force checks all four.
    """
    return mode in (UPPER_LEFT, LOWER_RIGHT)


def elbow_for(head, end, mode, angle_deg):
    """Return (elbow, angle_ok) joining a fixed head to a fixed end.

    The elbow sits at the head's height, on the end's angle-line - giving a
    horizontal landing plus a slant at ``angle_deg``. Used both by the main
    planner and for the extra leaders of multi-reference tags (each end gets
    its own elbow against the shared head).

    The landing may not extend BEHIND the head (against the mode's exit
    direction); if the geometry demands that, the elbow is clamped to the
    head's ``u`` and ``angle_ok`` is False. Likewise a slant cannot honour
    the angle when the end is vertically on the stack's own side (e.g. above
    an upper-mode head): flagged, elbow clamped to the head.

    Args:
        head: (u, v) tag head position.
        end: (u, v) leader end (arrowhead), never moved.
        mode: One of MODES (already resolved for switch_side).
        angle_deg: Slant angle from horizontal, clamped to [1, 89].

    Returns:
        tuple: ((elbow_u, elbow_v), angle_ok).
    """
    angle = clamp_angle(angle_deg)
    cot = 1.0 / math.tan(math.radians(angle))
    head_u, head_v = head
    end_v = end[1]

    # End on the stack's own vertical side: exact angle impossible.
    vertical_ok = (end_v <= head_v) if slant_sign(mode) < 0 \
        else (end_v >= head_v)

    elbow_u = _slant_u_at(end, mode, cot, head_v)
    sign = exit_sign(mode)

    horizontal_ok = (elbow_u - head_u) * sign >= 0.0
    if not (vertical_ok and horizontal_ok):
        return (head_u, head_v), False
    return (elbow_u, head_v), True


def plan_alignment(anchor, items, mode, angle_deg,
                   vertical_spacing, landing_distance, horizontal_spacing,
                   constant_landing=False, intermittent=False,
                   switch_side=False):
    """Plan head and elbow positions for a set of tags.

    Args:
        anchor: (u, v) picked point - the lowest tag head position.
        items: list of dicts, one per tag: ``{'key': <caller id>,
            'end': (u, v)}``. The key is echoed back untouched.
        mode: One of MODES (the quadrant button the user chose).
        angle_deg: Leader slant angle from horizontal (1-89).
        vertical_spacing: Head-to-head spacing within a column, in the
            caller's units. Must be > 0; a non-positive value falls back to
            a minimal epsilon so rows never coincide.
        landing_distance: Fixed landing length, used when ``constant_landing``.
        horizontal_spacing: Column offset for ``intermittent``.
        constant_landing: All landings exactly ``landing_distance`` long
            (heads re-derived from elbows - see module docstring).
        intermittent: Staggered two-column layout at half the row step.
        switch_side: Mirror the leader exit side of ``mode``.

    Returns:
        list of dicts IN INPUT ORDER, one per item:
            ``key``      the caller's key,
            ``row``      0-based row (0 = lowest),
            ``head``     (u, v) new tag head position,
            ``elbow``    (u, v) leader elbow,
            ``end``      (u, v) echoed input end,
            ``angle_ok`` False when the exact angle could not be honoured.
    """
    if not items:
        return []

    eff_mode = resolve_mode(mode, switch_side)
    angle = clamp_angle(angle_deg)
    cot = 1.0 / math.tan(math.radians(angle))
    sign = exit_sign(eff_mode)

    step = float(vertical_spacing)
    if step <= 0.0:
        step = 1e-9
    # Constant landing dictates each head's horizontal position, so there is
    # no second column to stagger into - intermittent is ignored with it on.
    if constant_landing:
        intermittent = False
    if intermittent:
        step /= 2.0

    landing = abs(float(landing_distance))
    column_offset = abs(float(horizontal_spacing))
    anchor_u, anchor_v = float(anchor[0]), float(anchor[1])

    # Assign ends to rows so leaders never cross: sort by angle-line
    # intercept, direction depending on mode. Ties broken by input order
    # (stable sort), which keeps the plan deterministic.
    order = sorted(
        range(len(items)),
        key=lambda i: _intercept(items[i]['end'], eff_mode, cot),
        reverse=not _sort_ascending(eff_mode))

    results = [None] * len(items)
    for row, item_index in enumerate(order):
        item = items[item_index]
        end = (float(item['end'][0]), float(item['end'][1]))
        head_v = anchor_v + row * step

        if constant_landing:
            # Elbow pinned to the end's angle-line; head derived from it so
            # every landing is exactly `landing` long and the angle holds.
            vertical_ok = (end[1] <= head_v) if slant_sign(eff_mode) < 0 \
                else (end[1] >= head_v)
            if vertical_ok:
                elbow_u = _slant_u_at(end, eff_mode, cot, head_v)
                head_u = elbow_u - sign * landing
                elbow, angle_ok = (elbow_u, head_v), True
            else:
                # End on the stack's own vertical side: the angle-line
                # extrapolation would fling the head far from the pick, so
                # fall back to the anchor column with a collapsed landing.
                head_u = anchor_u
                elbow, angle_ok = (head_u, head_v), False
        else:
            head_u = anchor_u
            if intermittent and row % 2 == 1:
                # Odd rows sit one column AWAY from the elements, so their
                # (longer) landings can never degenerate to zero.
                head_u -= sign * column_offset
            elbow, angle_ok = elbow_for((head_u, head_v), end,
                                        eff_mode, angle)

        results[item_index] = {
            'key': item.get('key'),
            'row': row,
            'head': (head_u, head_v),
            'elbow': elbow,
            'end': end,
            'angle_ok': angle_ok,
        }

    return results


def plan_ordered(anchor, items, mode, angle_deg, vertical_spacing,
                 landing_distance, horizontal_spacing, bundle,
                 intermittent=False, switch_side=False, clearance=None):
    """Plan an alignment ORDERED BY PIPE POSITION, deriving the arrow points.

    Used when every selected tag points at a curve element (pipe, duct...).
    Unlike plan_alignment, the leader ends are OUTPUTS here: each arrow is
    placed where the slant at ``angle_deg`` meets its own pipe, clamped to
    the pipe's extent. Every landing is exactly ``landing_distance`` long,
    every slant is parallel, and the stack reads in drawing order:

      bundle 'v' (pipes run vertically on screen, side by side):
          top tag = LEFTMOST pipe - the stack reads left to right.
      bundle 'h' (pipes run horizontally, stacked):
          top tag = TOPMOST pipe - the stack mirrors the pipe order.

    The picked ANCHOR is the bottom corner of the lowest tag's text on the
    far side from the pipes (bottom-LEFT when leaders exit right, bottom-
    RIGHT when they exit left), and text edges align up that column - what
    a drafter expects to be placing when they click. Per-item offsets tell
    the engine where the Revit head and the leader-exit edge sit relative
    to that corner.

    ``angle_deg`` below 0.5 means STRAIGHT-FIRST leaders, decided PER TAG
    by its own pipe's direction (optional item key ``own``: 'v'/'h',
    default = bundle):
      * tag level with a vertical pipe -> one horizontal line, elbow grip
        parked at the midpoint;
      * any tag that must CLIMB (vertical drop above/below the stack, or
        a horizontal run) -> horizontal landing plus a near-vertical
        segment leaning TILT_DEG off vertical, arrows fanned along the
        pipe with fitting clearance. The tilt keeps leaders distinct from
        the orthogonal pipework and all climbs parallel.
    Optional item key ``order_pos`` overrides the stack-ordering position
    (used for cross-direction pipes whose ``pos`` is on the other axis).

    Args:
        anchor: (u, v) picked point - the far-bottom corner of the lowest
            tag's text.
        items: list of dicts, one per tag:
            ``pos``         the pipe's position across the bundle (its u
                            for bundle 'v', its v for bundle 'h'),
            ``span``        (lo, hi) extent of the pipe along its run, for
                            clamping the arrow onto the real pipe,
            ``head_offset`` (du, dv) from the anchor corner to the tag's
                            head position (optional, default (0, 0)),
            ``exit_edge``   text width - distance from the anchor corner
                            to the leader-exit edge (optional, default 0),
            ``key``         echoed back untouched.
        mode / angle_deg / switch_side: as plan_alignment.
        vertical_spacing: row pitch (caller adds tag height to the gap).
        landing_distance: every landing is exactly this long.
        horizontal_spacing: column offset when ``intermittent``.
        bundle: 'v' or 'h' (see above).
        intermittent: staggered two-column layout at half the row step.
        clearance: fixed distance kept between arrows and pipe ends /
            fittings, in the caller's units (None = 5% of each extent).

    Returns:
        Same shape as plan_alignment, in input order. ``angle_ok`` is False
        when a pipe sits behind the stack (arrow collapsed to the elbow) or
        the slant had to be clamped to the pipe's end (angle approximate).
    """
    if not items:
        return []
    if bundle not in ('v', 'h'):
        raise ValueError('bundle must be "v" or "h", got {0!r}'.format(bundle))

    eff_mode = resolve_mode(mode, switch_side)
    angle = normalize_angle(angle_deg)
    zero = (angle == 0.0)
    tan_a = math.tan(math.radians(angle or MIN_ANGLE_DEG))
    cot = 1.0 / tan_a
    tan_t = math.tan(math.radians(TILT_DEG))
    sign = exit_sign(eff_mode)
    ssign = slant_sign(eff_mode)

    step = float(vertical_spacing)
    if step <= 0.0:
        step = 1e-9
    if intermittent:
        step /= 2.0
    landing = abs(float(landing_distance))
    column_offset = abs(float(horizontal_spacing))
    anchor_u, anchor_v = float(anchor[0]), float(anchor[1])
    count = len(items)

    # Per-item pipe direction (zero mode) and READING ORDER. User rule:
    # ALL vertical-pipe tags first, sorted left-to-right, then ALL
    # horizontal-pipe tags, sorted top-to-bottom - text rows, leaders and
    # arrows all follow the same march. Slanted mode keeps the original
    # bundle ordering (order_pos = a cross pipe's arrow position).
    owns = []
    for item in items:
        if not zero:
            owns.append(bundle)
        elif 'own' in item:
            owns.append(item['own'])
        elif item.get('cross'):
            owns.append('h' if bundle == 'v' else 'v')
        else:
            owns.append(bundle)

    rows = [0] * count
    vertical_drops = False    # riser cluster wholly above/below the rows
    if zero:
        # Risers seen in plan are POINTS, and their circles are staggered
        # a few hundred mm apart by pipe size - so a left-to-right key
        # ordered them by that stagger noise and the leaders crossed
        # (user's images, 2026-08-10, twice: the second because a mere
        # TIE-BREAK on height never fired for staggered circles). For a
        # point, HEIGHT is the primary key, topmost circle <-> top tag -
        # the riser analogue of "leftmost pipe = top tag". True vertical
        # pipes keep the left-to-right rule; in a mixed group the points
        # take the upper rows, deterministically.
        #
        # A riser cluster can also arrive as a MIX of directions: T/B
        # tags reference run pipes or drop stubs, whose own geometry
        # sits at the RUN's level, not the drop's. Sub-grouping by that
        # geometry paired same-service rows and forced a crossing
        # (2026-08-10). When the cluster contains a point and EVERY item
        # knows its drop (its arrow, or is itself a point), the whole
        # stack orders by drop height instead - topmost drop <-> top
        # row, one key, no sub-groups, no crossing.
        def _is_point(i):
            lo, hi = items[i]['span']
            return float(hi) - float(lo) <= 1e-9

        def _drop_v(i):
            arrow = items[i].get('arrow')
            if arrow is not None:
                return float(arrow[1])
            lo, hi = items[i]['span']
            return (float(lo) + float(hi)) / 2.0

        def _drop_u(i):
            arrow = items[i].get('arrow')
            if arrow is not None:
                return float(arrow[0])
            return float(items[i]['pos'])    # a point's pos is its u

        any_point = any(_is_point(i) for i in range(count))
        all_droppable = all(items[i].get('arrow') is not None
                            or _is_point(i) for i in range(count))
        if any_point and all_droppable:
            # Which way do the leaders travel? Circles wholly BELOW or
            # wholly ABOVE the row band mean a VERTICAL approach - and
            # there height is noise (the user's six level circles spread
            # 3.3m sideways, 2026-08-10) while the horizontal position
            # decides everything: a lower row's landing collides with an
            # upper row's drop exactly when it must reach PAST that
            # drop's column. Crossing-free assignment = the row nearest
            # the pipes takes the drop nearest the stack, marching
            # outward - which is the user's "leftmost on top, rightmost
            # on bottom" for a stack above with leaders exiting left,
            # and its mirror on the other three sides. Circles BESIDE
            # the stack keep the height order (topmost circle <-> top
            # tag, the approved parallel-slant look).
            drop_vs = [_drop_v(i) for i in range(count)]
            top_line = anchor_v + (count - 1) * step
            below = max(drop_vs) < anchor_v
            above = min(drop_vs) > top_line
            if below or above:
                vertical_drops = True
                ascend = (below and sign < 0) or (above and sign > 0)
                u_dir = 1.0 if ascend else -1.0
                ordering = sorted(
                    range(count),
                    key=lambda i: (u_dir * _drop_u(i), -_drop_v(i)))
            else:
                ordering = sorted(range(count), key=lambda i: -_drop_v(i))
        else:
            def _v_key(i):
                span_lo = float(items[i]['span'][0])
                span_hi = float(items[i]['span'][1])
                if span_hi - span_lo <= 1e-9:
                    return (0, -(span_lo + span_hi) / 2.0)
                return (1, float(items[i]['pos']))
            verticals = sorted((i for i in range(count)
                                if owns[i] == 'v'), key=_v_key)
            horizontals = sorted((i for i in range(count)
                                  if owns[i] == 'h'),
                                 key=lambda i: -float(items[i]['pos']))
            ordering = verticals + horizontals
        for position, item_index in enumerate(ordering):
            rows[item_index] = count - 1 - position   # first = TOP row
    else:
        order = sorted(range(count),
                       key=lambda i: float(items[i].get('order_pos',
                                                        items[i]['pos'])))
        for rank, item_index in enumerate(order):
            rows[item_index] = (count - 1 - rank) if bundle == 'v' else rank

    if zero:
        # Fan positions rank WITHIN each direction sub-group, not across
        # the whole stack: in a mixed stack the horizontal tags sit at the
        # bottom rows, and ranking by global row pushed every one of
        # their turns several steps past the window - clamping them all
        # onto the far end of the pipes (user: "using only the extreme
        # right end").
        v_indices = [i for i in range(count) if owns[i] == 'v']
        h_indices = [i for i in range(count) if owns[i] == 'h']
        v_top_rank = dict(
            (idx, k) for k, idx in enumerate(
                sorted(v_indices, key=lambda i: -rows[i])))
        h_top_rank = dict(
            (idx, k) for k, idx in enumerate(
                sorted(h_indices, key=lambda i: -rows[i])))
        v_count = max(len(v_indices), 1)
        h_count = max(len(h_indices), 1)

        # Riser points share ONE slant angle so their leaders come out
        # parallel (user's before/after, 2026-08-10: the level approach
        # gave every leader its own shallow slope and read messy; the
        # wanted look is a short landing then parallel slants straight
        # into the circles). The angle is the cluster's own: the median
        # of the direct text-to-circle slopes, clamped to 15-75 degrees;
        # the landings absorb the per-row differences.
        point_slant_tan = None
        v_points = [i for i in v_indices
                    if float(items[i]['span'][1])
                    - float(items[i]['span'][0]) <= 1e-9]
        # Vertical-approach clusters drop STRAIGHT down (or climb
        # straight up) onto their circles - no shared slant (user's
        # choice, 2026-08-10: "clean vertical drop").
        if v_points and not vertical_drops:
            ratios = []
            for i in v_points:
                pv = float(items[i]['span'][0])
                lv = anchor_v + rows[i] * step \
                    + float(items[i].get('line_offset', 0.0))
                edge = anchor_u + sign * abs(
                    float(items[i].get('exit_edge', 0.0)))
                dv = abs(pv - lv)
                du = abs(float(items[i]['pos']) - edge)
                if du > 1e-9 and dv > 1e-9:
                    ratios.append(dv / du)
            if ratios:
                # The STEEPEST direct line sets the shared angle: any
                # shallower and that leader folds behind its text and
                # clamps un-parallel; every other row absorbs the
                # difference in its landing instead.
                ratio = max(ratios)
                ratio = max(math.tan(math.radians(15.0)),
                            min(math.tan(math.radians(75.0)), ratio))
                point_slant_tan = ratio

        if h_indices:
            lo_all = [float(items[i]['span'][0]) for i in h_indices]
            hi_all = [float(items[i]['span'][1]) for i in h_indices]
            w_lo, w_hi = max(lo_all), min(hi_all)
            if w_hi <= w_lo:           # pipes don't overlap: use the union
                w_lo, w_hi = min(lo_all), max(hi_all)
            margin = clearance if clearance is not None \
                else 0.05 * (w_hi - w_lo)
            # A fixed clearance must not strangle a short jog: on a pipe
            # only a few hundred mm long, the margins would eat the whole
            # extent and pack every turn into the sliver left over. Cap
            # the margin at 15% of the extent per end.
            margin = min(margin, 0.15 * (w_hi - w_lo))
            # The fan anchors AHEAD OF THE TEXT, not at the pipe's near
            # end: on a long run picked mid-span the pipe's end is metres
            # behind the stack, and end-anchored turns all failed the
            # behind-the-text check (the "why is there no 90-degree bend"
            # regression). At a corner stub the pipe starts ahead of the
            # text anyway, so both anchors coincide and nothing changes.
            max_exit = max(abs(float(items[i].get('exit_edge', 0.0)))
                           for i in h_indices)
            if sign > 0:
                base = max(anchor_u + max_exit + margin, w_lo + margin)
                far = w_hi - margin
                if far < base:
                    base = far
                full_span = max(far - base, 0.0)
                usable = full_span * 0.6
                turn_lo, turn_hi = base, base + usable
            else:
                base = min(anchor_u - max_exit - margin, w_hi - margin)
                near_end = w_lo + margin
                if base < near_end:
                    base = near_end
                full_span = max(base - near_end, 0.0)
                usable = full_span * 0.6
                turn_hi, turn_lo = base, base - usable
            turn_step = min(step, usable / len(h_indices))
            turn_starved = False
            if clearance is not None and turn_step < clearance:
                # Short runs collapsed the fan to a sliver (user: "gap is
                # too tight"). Readable spacing beats the near-window:
                # spread by the clearance, using the whole forward band.
                turn_step = min(clearance,
                                full_span / len(h_indices))
                # Still under the clearance: the COMMON window - the
                # intersection of every pipe's span - is starved by its
                # shortest member. Each tag then re-slots over its OWN
                # pipe's room instead (below).
                turn_starved = turn_step < clearance

    results = [None] * count
    for item_index in range(count):
        item = items[item_index]
        row = rows[item_index]
        corner_v = anchor_v + row * step
        corner_u = anchor_u
        if intermittent and row % 2 == 1:
            corner_u -= sign * column_offset

        offset = item.get('head_offset', (0.0, 0.0))
        head = (corner_u + float(offset[0]), corner_v + float(offset[1]))
        # Revit attaches the leader at the TEXT's mid-height edge, which is
        # not necessarily the head position - the caller passes the bbox
        # centre as line_offset so landings come out truly horizontal.
        line_v = corner_v + float(item.get('line_offset', offset[1]))
        exit_edge = abs(float(item.get('exit_edge', 0.0)))
        edge_u = corner_u + sign * exit_edge   # leader-exit edge of the text

        pos = float(item['pos'])
        lo, hi = float(item['span'][0]), float(item['span'][1])
        angle_ok = True
        entry_straight = False
        # The bend normally sits on the landing line; a riser approach
        # moves it to the CIRCLE's height instead.
        elbow_v = line_v
        # How far the WHOLE stack must slide away from the pipes (along
        # -sign) for this tag to become placeable. 0 means it is either
        # fine already or broken for a reason sliding cannot mend (pipe
        # on the wrong side, or too short for the slant).
        shortfall = 0.0

        own = owns[item_index]

        if own == 'v':
            # The landing COMPRESSES to fit close picks instead of failing:
            # a tag only genuinely fails when its text itself would cross
            # the pipe.
            avail = sign * (pos - edge_u)
            landing_eff = min(landing, avail) if avail > 0.0 else 0.0
            elbow_u = edge_u + sign * landing_eff
            reach = avail - landing_eff
            clr = clearance if clearance is not None else 0.05 * (hi - lo)
            clr = min(clr, 0.15 * (hi - lo))   # don't strangle short pipes
            band_lo, band_hi = lo + clr, hi - clr
            if band_hi < band_lo:      # pipe shorter than two clearances
                band_lo = band_hi = (lo + hi) / 2.0
            if avail < 0.0:
                end, angle_ok = (elbow_u, line_v), False  # text past pipe
                shortfall = -avail        # slide back by exactly this
            elif zero and (hi - lo) <= 1e-9:
                # A riser seen in plan: the target is a POINT. Short
                # landing from the text, then the cluster's COMMON slant
                # straight into the circle - every riser leader parallel
                # (user's before/after, 2026-08-10, superseding both the
                # 7.5-degree hops and the per-leader level approach).
                pv = (lo + hi) / 2.0
                dv = abs(pv - line_v)
                if vertical_drops:
                    # Cluster wholly above/below the rows: landing out
                    # to the circle's column, then a clean VERTICAL
                    # drop/climb onto it (user's choice, 2026-08-10).
                    elbow_u = pos
                elif point_slant_tan is not None and dv > 1e-9:
                    elbow_u = pos - sign * (dv / point_slant_tan)
                else:
                    # Level with its circle: land straight into it.
                    elbow_u = pos - sign * (clearance
                                            if clearance is not None
                                            else 0.0)
                if sign * (elbow_u - edge_u) < 0.0:
                    elbow_u = edge_u   # never fold behind the text
                end = (pos, pv)
                entry_straight = True
            elif zero and band_lo <= line_v <= band_hi:
                # Level with the pipe: one horizontal line, elbow grip
                # parked at the MIDPOINT (usable grab handle, never blocks
                # manual repositioning; safe for glued arrows because the
                # line runs at the text centre height).
                end = (pos, line_v)
                entry_straight = True
                elbow_u = (edge_u + pos) / 2.0
            elif zero:
                # The tag must CLIMB to its pipe (drop above the stack, or
                # a run below it). Never a strict vertical - that would
                # draw the leader ALONG the pipe - and never a flat clamp:
                # a near-vertical segment at TILT_DEG leaning from the
                # stack side, arrow fanned along the pipe from its nearer
                # end with fitting clearance.
                fan = min(step, max(band_hi - band_lo, 0.0) / v_count)
                if clearance is not None and fan < clearance:
                    # Same readable-spacing floor as the horizontal fan.
                    fan = min(clearance,
                              max(band_hi - band_lo, 0.0) / v_count)
                if line_v < band_lo:
                    # Pipe above: the sub-group's TOP tag reaches first.
                    nearness = v_top_rank[item_index]
                    arrow_v = min(band_lo + nearness * fan, band_hi)
                else:
                    # Pipe below: the sub-group's BOTTOM tag reaches first.
                    nearness = v_count - 1 - v_top_rank[item_index]
                    arrow_v = max(band_hi - nearness * fan, band_lo)
                rise = abs(arrow_v - line_v)
                elbow_u = pos - sign * rise * tan_t
                if sign * (elbow_u - edge_u) < 0.0:
                    elbow_u = edge_u   # never fold behind the text
                end = (pos, arrow_v)
                entry_straight = True
            else:
                end_v = line_v + ssign * reach * tan_a
                if end_v < lo:
                    end_v, angle_ok = lo, False
                elif end_v > hi:
                    end_v, angle_ok = hi, False
                end = (pos, end_v)
        elif zero:
            # Horizontal-own pipe at angle 0: horizontal landing, then a
            # near-vertical climb at TILT_DEG onto the pipe. The row
            # nearest the pipes - by the ACTUAL side the pipe is on, not
            # the dialog quadrant (case-1 crossing bug) - takes the turn
            # nearest the corner side; farther rows step outward so their
            # climbs clear every landing between them and the pipes.
            climb_down = pos < line_v      # this pipe is below the row
            if climb_down:
                # Sub-group's BOTTOM tag (nearest the pipes) turns first.
                nearness = h_count - 1 - h_top_rank[item_index]
            else:
                # Sub-group's TOP tag turns first.
                nearness = h_top_rank[item_index]
            # Common fan first; but the common window is the INTERSECTION
            # of every pipe's span, so one short pipe used to drag every
            # arrow into a cramped sliver. When the window is starved, or
            # a tag's common slot falls off ITS OWN pipe, that tag
            # re-slots evenly over its own room instead - arrows
            # distribute by each pipe's length (user rule, 2026-08-09).
            # An unstarved window keeps the common spacing untouched, so
            # long-pipe fans look exactly as before.
            own_margin = min(margin, 0.15 * max(hi - lo, 0.0))
            slots = max(h_count - 1.0, 1.0)
            if sign > 0:
                turn_u = turn_lo + nearness * turn_step
                own_far = hi - own_margin
                own_room = own_far - base
                if own_room > 0.0 and (turn_starved or turn_u > own_far):
                    # Spread end-to-end over THIS pipe's room, but never
                    # wider than the clearance spacing a long pipe would
                    # get - long-pipe fans stay exactly as before.
                    spacing = own_room / slots
                    if clearance is not None:
                        spacing = min(spacing, max(clearance, turn_step))
                    turn_u = min(base + nearness * spacing, own_far)
            else:
                turn_u = turn_hi - nearness * turn_step
                own_near = lo + own_margin
                own_room = base - own_near
                if own_room > 0.0 and (turn_starved or turn_u < own_near):
                    spacing = own_room / slots
                    if clearance is not None:
                        spacing = min(spacing, max(clearance, turn_step))
                    turn_u = max(base - nearness * spacing, own_near)
            turn_u = max(lo, min(hi, turn_u))   # stay on THIS pipe
            # User rule: onto a HORIZONTAL pipe the bend is a true 90
            # degrees - the climb is perpendicular to the pipe, so it can
            # never ride along it. The TILT_DEG lean stays only for
            # climbs onto VERTICAL pipes, where a vertical segment would
            # overlay the pipe itself.
            elbow_u = turn_u
            end = (turn_u, pos)
            room = sign * (turn_u - edge_u)
            angle_ok = room >= 0.0
            entry_straight = angle_ok
            if not angle_ok:
                shortfall = -room     # the turn sits behind the text
        else:
            elbow_u = edge_u + sign * landing   # no horizontal pipe limit
            drop = ssign * (pos - line_v)
            if drop < 0.0:
                end, angle_ok = (elbow_u, line_v), False  # pipe on wrong side
            else:
                end_u = elbow_u + sign * drop * cot
                if end_u < lo:
                    end_u, angle_ok = lo, False
                elif end_u > hi:
                    end_u, angle_ok = hi, False
                end = (end_u, pos)

        results[item_index] = {
            'key': item.get('key'),
            'row': row,
            'head': head,
            'elbow': (elbow_u, elbow_v),
            'end': end,
            'straight': entry_straight,
            'angle_ok': angle_ok,
            'shortfall': shortfall,
            'line_v': line_v,     # landing height - not always elbow_v
        }

    return results


def leader_segments(plan_entry):
    """Return the two leader segments of a plan entry.

    Convenience for callers and tests: ``[(head, elbow), (elbow, end)]``.
    """
    return [
        (plan_entry['head'], plan_entry['elbow']),
        (plan_entry['elbow'], plan_entry['end']),
    ]
