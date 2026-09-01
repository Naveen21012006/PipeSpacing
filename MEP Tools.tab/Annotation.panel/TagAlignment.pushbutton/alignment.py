# -*- coding: utf-8 -*-
"""Tag head alignment strategies.

Each alignment method is a small strategy class registered in
ALIGNMENT_STRATEGIES. Adding one means writing a class and registering it.

The live menu is a single method: Auto Tag Pipes. Earlier strategies (the
Cluster-on-Reference-Line pair, Cluster Risers by Flow, edge alignment,
distribution, plain/stack columns) are retired - the last three in
archive.py, unregistered.

Principles shared by all of them:

* Everything is computed in the *view's* axes (RightDirection / UpDirection),
  not world X/Y, so "left" and "top" mean what the user sees whether the view
  is a plan, a section or an elevation.
* A tag is anchored by its measured text *edge*, not by TagHeadPosition (which
  sits at the text centre), so edges line up cleanly on the reference line.

Only tag heads are moved. MEP elements are never touched.
"""

from collections import OrderedDict
import math

import tool_config as config
import diagnostics
import engine_bridge
import utils


# ---------------------------------------------------------------------------
# Measuring the tags
# ---------------------------------------------------------------------------
def _measure_head_bounds(tags, view, doc):
    """Return {index: (across_span, height_span)} of every tag's TEXT box.

    Revit's tag bounding box includes the leader, and HasLeader=False does NOT
    reliably remove it - so a raw edge can be the arrowhead, not the text.

    To get the true text extents, each tag head is briefly parked ON its
    element. That collapses the leader to zero length, leaving the bounding
    box as text only. The text extents relative to the head are recorded (they
    are invariant to head position), the heads are restored, and the offsets
    are applied to the real head positions. No leader trickery, no assumption
    about where the head sits inside the text - so Left and Right land equally
    well on a reference line.

    Spans are (low, high) pairs along the view's right and up axes, or None
    when the element or box is unavailable (the caller then falls back to the
    tag head).
    """
    if not tags or doc is None:
        return {}

    right, up = utils.get_view_axes(view)
    real_heads = [tag.TagHeadPosition for tag in tags]

    # Park each head on its element so the leader collapses to nothing.
    anchors = []
    for tag in tags:
        element = _tag_element(tag, doc)
        anchors.append(utils.get_element_anchor(element, view)
                       if element is not None else None)

    parked = []
    for tag, anchor in zip(tags, anchors):
        if anchor is None:
            parked.append(False)
            continue
        try:
            tag.TagHeadPosition = anchor
            parked.append(True)
        except Exception:
            parked.append(False)
    doc.Regenerate()

    # Record the text extents relative to the parked head.
    offsets = {}
    for index, tag in enumerate(tags):
        if not parked[index]:
            offsets[index] = (None, None)
            continue
        head_r = utils.project(tag.TagHeadPosition, right)
        head_u = utils.project(tag.TagHeadPosition, up)
        span_r = utils.project_bounds(tag, view, right)
        span_u = utils.project_bounds(tag, view, up)
        offsets[index] = (
            (span_r[0] - head_r, span_r[1] - head_r) if span_r else None,
            (span_u[0] - head_u, span_u[1] - head_u) if span_u else None,
        )

    # Restore the real head positions.
    for tag, real, was_parked in zip(tags, real_heads, parked):
        if was_parked:
            try:
                tag.TagHeadPosition = real
            except Exception:
                pass
    doc.Regenerate()

    # Apply the recorded offsets to the real heads -> absolute text spans.
    bounds = {}
    for index in range(len(tags)):
        off_r, off_u = offsets[index]
        head_r = utils.project(real_heads[index], right)
        head_u = utils.project(real_heads[index], up)
        bounds[index] = (
            (head_r + off_r[0], head_r + off_r[1]) if off_r else None,
            (head_u + off_u[0], head_u + off_u[1]) if off_u else None,
        )
    return bounds


# Which part of the tag lands on the target coordinate.
EDGE_LOW = 'low'        # left edge (across) / bottom edge (height)
EDGE_HIGH = 'high'      # right edge (across) / top edge (height)
EDGE_CENTER = 'center'

# How many pitch-steps the Auto method will search for a vacant band before it
# settles for its first choice. Generous: a step is one row, and giving up too
# early puts a block on top of another.
_SLOT_TRIES = 200


def _anchor(span, head_coord, edge):
    """Return the coordinate of a tag's chosen edge along one axis.

    Falls back to the tag head when Revit gave us no bounding box, so a tag we
    cannot measure still gets aligned - just on its centre.
    """
    if span is None:
        return head_coord
    low, high = span
    if edge == EDGE_LOW:
        return low
    if edge == EDGE_HIGH:
        return high
    return (low + high) / 2.0


def _readable_pitch(bounds, view, axis_index, floor_mm=config.MIN_TAG_PITCH_MM):
    """Return the smallest centre-to-centre gap that keeps tag text apart.

    Sized from the tags themselves, so long or wrapped text automatically
    claims more room. `floor_mm` is the minimum pitch (paper mm) applied when
    the measurement is small OR unavailable - the caller sets it from the tag
    the method places: a single-line Size tag can use the tight
    config.MIN_TAG_PITCH_MM, but the Auto method's multi-line label (Size +
    System + Comments) needs config.FALLBACK_TAG_PITCH_MM, or the tall tags
    overprint the next one and the column becomes an unreadable pile. Without
    any spacing, tags on pipes 50 mm apart end up 50 mm apart on screen - piled
    on top of each other and unreadable.
    """
    floor = utils.paper_mm_to_model(view, floor_mm)

    sizes = []
    for spans in bounds.values():
        span = spans[axis_index]
        if span:
            sizes.append(span[1] - span[0])

    if not sizes:
        return floor

    gap = utils.paper_mm_to_model(view, config.TAG_GAP_MM)
    return max(max(sizes) + gap, floor)


def _seat_pass(ordered, spans, pitch, snap):
    """One greedy top-down seating pass against ONE lattice. See _seat_level_rows.

    Walks the pipes in reading order giving each the highest lattice row that is
    inside its own span and at least a pitch below the row above.  Returns the
    same (seats, homeless, trace) triple its caller does.
    """
    seats = {}
    homeless = []
    trace = []
    ceiling = None                  # lowest row used so far
    for index in ordered:
        span = spans.get(index)
        if span is None:            # no usable extent - it cannot be level
            homeless.append(index)
            trace.append((index, None, None, None))
            continue
        low, high = span
        top = high if ceiling is None else min(high, ceiling - pitch)
        row = snap(top)
        # Tolerance is essential, not cosmetic. When `top` is already ON the
        # lattice (the usual case: ceiling - pitch), snap returns the same row,
        # but the two values reach it by different arithmetic and can differ by
        # ~1e-13 feet. A bare `row > top` then reads that dust as "rounded up"
        # and drops a whole row - which on the user's 2026-08-04 bundle left an
        # empty row, starved the two pipes below it, and demoted both to drops.
        if row > top + pitch * 1e-6:
            row -= pitch
        if row < low - pitch * 1e-6:   # its own pipe cannot host a free row
            homeless.append(index)
            trace.append((index, low, high, None))
            continue
        seats[index] = row
        trace.append((index, low, high, row))
        ceiling = row
    return seats, homeless, trace


def _seat_level_rows(members, elements, up, pitch, clear, pipe_across, snap):
    """Give each vertical run a straight-leader row ON ITS OWN pipe.

    A straight leader is a level line from the column into the pipe's side, so
    tag i's row has to lie within pipe i's own extent - and nothing more. The
    rows are still one pitch apart and still read leftmost-pipe-on-top, but
    pipes are no longer required to share a common band: a staggered bundle can
    seat every tag even where no single height is inside all of them.

    Seating is greedy from the top (see _seat_pass), run once against the shared
    lattice and again against a floated one if the shared lattice strands a tag.
    A pipe whose span cannot host a free row under the BEST of those phases is
    returned as homeless, and the caller demotes THAT TAG ALONE to a 90-degree
    drop.

    Args:
        members (list[int]): the cluster's screen-vertical tag indices.
        elements (list): tagged elements, indexed like members.
        up: the view's up axis (for get_curve_span).
        pitch, clear (float): row spacing and the inset from a pipe's ends.
        pipe_across (list[float]): each pipe's u, for reading order.
        snap (callable): value -> nearest row on the SHARED lattice.

    Returns:
        (seats, homeless, trace): {index: row_v}, [index] demoted, and a list of
        (index, usable_low, usable_high, row_or_None) for the log - so a
        demotion can be read off the file instead of inferred.
    """
    spans = {}
    for index in members:
        span = utils.get_curve_span(elements[index], up)
        if span is None:
            continue
        low, high = span[0] + clear, span[1] - clear
        if low <= high:
            spans[index] = (low, high)

    # Reading order: leftmost pipe on the top row (the Align Tags convention).
    ordered = sorted(members, key=lambda i: (pipe_across[i], i))

    # The stack wants to sit on the SHARED lattice, so tags from different
    # clusters line up across the drawing. But that lattice's phase comes from
    # wherever the user happened to click the reference line, and a stack that
    # misses a pipe's window by a fraction of a pitch loses a whole straight
    # leader for no geometric reason at all - on the 2026-09-01 bundle four
    # rows FIT inside the four pipes, and the click phase still stranded one.
    #
    # So: try the shared lattice first, and only if it strands a tag, float the
    # stack - anchoring the top row on each pipe's window top in turn, which is
    # where the extra seat comes from when it exists. The stack keeps its exact
    # pitch in every case; only its phase moves, and only when moving it buys a
    # straight leader that the shared lattice could not.
    anchors = [None]
    anchors.extend(sorted(set(high for (_low, high) in spans.values()),
                          reverse=True))
    best = None
    for anchor in anchors:
        if anchor is None:
            snap_to_phase = snap
            drift = 0.0
        else:
            def snap_to_phase(value, _a=anchor):
                return _a - math.floor((_a - value) / pitch + 1e-9) * pitch
            drift = abs(anchor - snap(anchor))
        attempt = _seat_pass(ordered, spans, pitch, snap_to_phase)
        # More tags seated wins outright; ties go to the stack that sits
        # closest to the shared lattice, so we drift only as far as we must.
        score = (len(attempt[0]), -drift)
        if best is None or score > best[0]:
            best = (score, attempt)
        if not attempt[1]:          # nobody stranded - the lattice was fine
            break
    return best[1]


def _tag_element(tag, doc):
    """Return the first local element a tag points at, or None."""
    if doc is None:
        return None
    try:
        ids = list(tag.GetTaggedLocalElementIds())
    except AttributeError:
        try:
            ids = [tag.TaggedLocalElementId]
        except Exception:
            ids = []
    except Exception:
        ids = []

    for element_id in ids:
        element = doc.GetElement(element_id)
        if element is not None:
            return element
    return None


# ---------------------------------------------------------------------------
# Base strategy
# ---------------------------------------------------------------------------
class AlignmentStrategy(object):
    """Base class for every alignment method.

    A strategy that needs extra input from the user sets a `requires_*` flag;
    script.py collects it and hands it over in `context`. That keeps the UI out
    of this module while still letting strategies ask for things. `context`
    always carries 'doc'.
    """

    name = 'Base'
    description = ''

    # Set True to have script.py prompt for a line and put it in
    # context['reference_line'] before calling compute_moves().
    requires_reference_line = False

    # Set True to tag one representative per connected same-size run rather than
    # every selected segment (script.py groups the runs before creating tags).
    groups_runs = False

    # Set True for the Auto method: tag the WHOLE selection, write each pipe's
    # designation into its Comments, and lay horizontals and risers out as two
    # blocks on the one reference line. script.py reads this flag.
    writes_comments = False

    def compute_moves(self, tags, view, context):
        """Return the tag head moves this strategy wants to make.

        Args:
            tags (list): IndependentTag objects.
            view: The active view.
            context (dict): 'doc', plus anything the strategy asked for.

        Returns:
            list: (tag, new_head_position) tuples. Tags already in position are
            omitted.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Reference line: you draw the line, the tags land on it
# ---------------------------------------------------------------------------
def _reference_coordinate_at(line, right, up, height):
    """Return the reference line's across-coordinate at a given height.

    Solving for the point on the line at each tag's own height means a vertical
    reference line yields a vertical column and a slanted one yields a slanted
    column - both for free. A horizontal line has no height dependence, so its
    own coordinate is used.
    """
    start = line.GetEndPoint(0)
    direction = line.Direction

    start_height = utils.project(start, up)
    direction_height = utils.project(direction, up)

    # direction_height is a direction cosine, so this is an ANGLE test, not a
    # length one. A line too shallow to define a vertical column can't give a
    # stable across-coordinate per height (the value would explode), so fall
    # back to the line's own position. 0.2 ~= 11 degrees off horizontal.
    if abs(direction_height) < 0.2:
        return utils.project(start, right)

    distance = (height - start_height) / direction_height
    point = start.Add(direction.Multiply(distance))
    return utils.project(point, right)


# ---------------------------------------------------------------------------
# Cluster on reference line: one tag per run, clusters centred on their pipes
# ---------------------------------------------------------------------------
def _declutter_blocks(order, targets, pitch):
    """Group tag indices (given sorted by target height) into clusters.

    Neighbours whose tags would sit closer than `pitch` merge into one block,
    centred on the MEAN of its members' targets - so a clump of tags stays
    centred on its pipes instead of drifting to one end.

    Args:
        order (list[int]): Tag indices sorted ascending by target height.
        targets (list[float]): Target height per tag index.
        pitch (float): Minimum centre-to-centre spacing.

    Returns:
        list[dict]: One block per cluster, each with 'members' (the tag
        indices it holds), 'n' (count) and 'centre' (the block centre height).
    """
    blocks = []
    for index in order:
        blocks.append({'members': [index], 'sum': targets[index], 'n': 1})
        while len(blocks) >= 2:
            lower, upper = blocks[-2], blocks[-1]
            lower_high = lower['sum'] / lower['n'] + (lower['n'] - 1) * pitch / 2.0
            upper_low = upper['sum'] / upper['n'] - (upper['n'] - 1) * pitch / 2.0
            if upper_low - lower_high < pitch:
                blocks[-2:] = [{'members': lower['members'] + upper['members'],
                                'sum': lower['sum'] + upper['sum'],
                                'n': lower['n'] + upper['n']}]
            else:
                break

    for block in blocks:
        block['centre'] = block['sum'] / block['n']
    return blocks


class _ClusterReferenceLine(AlignmentStrategy):
    """Cluster one-tag-per-run tags on a reference line.

    Two behaviours, chosen automatically from the tagged pipes' orientation in
    the view:

    * Vertical pipes (level mode): each tag is drawn towards the height of its
      own pipe, so its leader stays short and level. Where several tags would
      overlap they de-overlap into a tidy cluster centred on that group (see
      _declutter_blocks), ordered left-to-right with the left-most pipe on top.

    * Horizontal pipes (L-leader mode): a level leader would sit on the pipe, so
      the tags stack in a column on the line and each leader turns 90 degrees
      down to the MIDDLE of its own pipe segment. Where several segments share a
      middle (a bundle) their drops fan apart, centred on that middle, so no two
      overlap. See _horizontal_moves; the elbow geometry is applied later by
      leader_manager.apply_elbows() via context['leader_plan'].

    * Risers in a plan (L-leader mode): the pipes project to points, so
      straight leaders from the column converge in a tangled fan. Instead each
      leader turns 90 degrees directly over its own riser - a horizontal
      landing, then a vertical drop onto the point - and the column is ordered
      so the drops nest instead of crossing. See _riser_moves.

    In both cases the reference line sets the across position and `edge` picks
    which tag edge lands on it. Only tag heads are moved here.
    """

    requires_reference_line = True
    groups_runs = True
    edge = EDGE_LOW

    def compute_moves(self, tags, view, context):
        geometry = self._gather_geometry(tags, view, context)
        if geometry is None:
            return []
        return self._dispatch(tags, view, context, geometry)

    # -- geometry gathering / mode dispatch --------------------------------
    def _gather_geometry(self, tags, view, context):
        """Measure the tags and classify how each one's pipe sits in the view.

        Returns a dict of the per-tag arrays every layout mode needs, or None
        when there is nothing to place (no reference line / no tags). The arrays
        are:

            pipe_up / pipe_across : the pipe anchor projected onto the view's
                up / right axes (the tag head is used when the pipe has no
                anchor).
            pointlike : the pipe runs along the view normal - a riser seen in a
                plan, which projects to a point.
            horizontal : the pipe runs more across the view than up it - a
                flat pipe a level leader would sit on top of.
        """
        line = context.get('reference_line')
        if line is None or not tags:
            return None

        doc = context.get('doc')
        count = len(tags)
        right, up = utils.get_view_axes(view)
        heads = [tag.TagHeadPosition for tag in tags]
        bounds = _measure_head_bounds(tags, view, doc)

        # Per-tag geometry in the view's axes: the pipe's height and left-right
        # position, and how the pipe runs relative to the view - along it
        # (horizontal), across it (a point-like riser in a plan), or neither.
        # Riser mode only makes sense in a plan (the view looks straight down,
        # so a vertical pipe projects to a point). In a section the view normal
        # is horizontal, and a pipe crossing the cut is ALSO point-like but is
        # not a riser - it must keep the level layout, so gate on the view.
        normal = right.CrossProduct(up)
        plan_view = abs(normal.Z) >= 0.7
        pipe_up = []
        pipe_across = []
        horizontal = []
        pointlike = []
        elements = []
        for index, tag in enumerate(tags):
            element = _tag_element(tag, doc)
            elements.append(element)
            anchor = utils.get_element_anchor(element, view) if element else None
            if anchor is not None:
                pipe_up.append(utils.project(anchor, up))
                pipe_across.append(utils.project(anchor, right))
            else:
                pipe_up.append(utils.project(heads[index], up))
                pipe_across.append(utils.project(heads[index], right))
            direction = utils.get_element_direction(element) if element else None
            is_point = (direction is not None
                        and abs(utils.project(direction, normal)) >= 0.7)
            pointlike.append(is_point)
            horizontal.append(
                not is_point
                and direction is not None
                and abs(utils.project(direction, up))
                < abs(utils.project(direction, right)))

        # The Auto method places a multi-line tag (Size + System + Comments), so
        # it needs a taller minimum pitch than the single-line Size tag the
        # Cluster methods place - otherwise its tags overprint into a pile.
        floor_mm = (config.FALLBACK_TAG_PITCH_MM if self.writes_comments
                    else config.MIN_TAG_PITCH_MM)
        pitch = _readable_pitch(bounds, view, 1, floor_mm)

        return {
            'line': line, 'right': right, 'up': up, 'normal': normal,
            'plan_view': plan_view, 'count': count,
            'heads': heads, 'bounds': bounds,
            'pipe_up': pipe_up, 'pipe_across': pipe_across,
            'horizontal': horizontal, 'pointlike': pointlike,
            'elements': elements, 'pitch': pitch,
        }

    def _dispatch(self, tags, view, context, g):
        """Route the tags to a single layout mode by what the pipes ARE.

        Risers in a plan project to points - straight leaders from a tag column
        converge on them in a tangled fan, so they get L-leaders dropped on each
        riser's own position. Horizontal pipes get the L-leader column too (a
        level leader would sit on the pipe). Everything else keeps the level
        behaviour (each tag drawn to its own pipe's height).
        """
        count = g['count']
        if (g['plan_view']
                and sum(1 for flag in g['pointlike'] if flag) * 2 > count):
            return self._riser_moves(
                tags, view, context, g['line'], g['right'], g['up'], g['heads'],
                g['bounds'], g['pipe_up'], g['pipe_across'], g['pointlike'],
                g['elements'], g['pitch'])
        if sum(1 for flag in g['horizontal'] if flag) * 2 > count:
            return self._horizontal_moves(
                tags, view, context, g['line'], g['right'], g['up'], g['heads'],
                g['bounds'], g['pipe_up'], g['pipe_across'], g['horizontal'],
                g['elements'], g['pitch'])
        return self._level_moves(
            tags, g['line'], g['right'], g['up'], g['heads'], g['bounds'],
            g['pipe_up'], g['pipe_across'], g['pitch'])

    # -- shared head placement --------------------------------------------
    def _assemble_moves(self, tags, line, right, up, heads, bounds,
                        height_targets):
        """Move each tag's chosen edge onto the line at its target height.

        Returns (moves, new_heads): moves omits tags already in position;
        new_heads holds the resulting head position for every tag it placed. A
        tag with no entry in height_targets is skipped, so a caller can place a
        subset here (the Auto method sends only the risers through this, its
        horizontals going to the Align Tags engine).
        """
        across_coords = [utils.project(head, right) for head in heads]
        moves = []
        new_heads = {}
        for index, tag in enumerate(tags):
            if index not in height_targets:
                continue
            spans = bounds.get(index)
            span = spans[0] if spans else None
            anchor = _anchor(span, across_coords[index], self.edge)

            target_height = height_targets[index]
            target_across = _reference_coordinate_at(
                line, right, up, target_height)

            delta_across = target_across - anchor
            delta_height = target_height - utils.project(heads[index], up)

            new_head = utils.shift(heads[index], right, delta_across)
            new_head = utils.shift(new_head, up, delta_height)
            new_heads[index] = new_head

            if not (abs(delta_across) < config.POSITION_TOLERANCE
                    and abs(delta_height) < config.POSITION_TOLERANCE):
                moves.append((tag, new_head))
        return moves, new_heads

    # -- level mode (vertical pipes) --------------------------------------
    def _level_moves(self, tags, line, right, up, heads, bounds,
                     target_up, order_key, pitch):
        """Draw each tag to its own pipe's height, de-overlapping into clusters.

        Tags whose pipe heights collide form a cluster (centred on the group).
        WITHIN a cluster, order strictly left-to-right - the left-most pipe on
        top - regardless of the small height differences between segments.
        """
        by_height = sorted(range(len(tags)), key=lambda i: target_up[i])
        height_targets = {}
        for block in _declutter_blocks(by_height, target_up, pitch):
            top = block['centre'] + (block['n'] - 1) * pitch / 2.0
            left_to_right = sorted(block['members'], key=lambda i: order_key[i])
            for step, index in enumerate(left_to_right):
                height_targets[index] = top - step * pitch

        moves, _new_heads = self._assemble_moves(
            tags, line, right, up, heads, bounds, height_targets)
        return moves

    # -- L-leader mode (risers: point-like pipes in a plan) ----------------
    def _riser_moves(self, tags, view, context, line, right, up, heads,
                     bounds, pipe_up, pipe_across, pointlike, elements,
                     pitch):
        """Stack every tag in one column; drop each leader onto its own riser.

        A thin wrapper over _riser_block (whole selection, one column anchored
        to the top of the reference line) plus _build_leader_plan.
        """
        column_top = max(utils.project(line.GetEndPoint(0), up),
                         utils.project(line.GetEndPoint(1), up))
        height_targets, specs, _bottom = self._riser_block(
            line, right, up, pipe_up, pipe_across, pointlike, pitch,
            list(range(len(tags))), column_top)
        moves, new_heads = self._assemble_moves(
            tags, line, right, up, heads, bounds, height_targets)
        context['leader_plan'] = self._build_leader_plan(
            specs, tags, view, new_heads, right, up, pipe_up, pipe_across,
            elements)
        return moves

    def _riser_block(self, line, right, up, pipe_up, pipe_across, pointlike,
                     pitch, members, column_top):
        """Lay `members` out as a riser column starting at column_top.

        In a plan a riser is a point, so every leader elbow later goes directly
        over it: a horizontal landing from the tag head, then a vertical drop
        onto the point (built by _build_leader_plan from the returned specs).

        What has to be chosen here is the ORDER of the column, or the drops
        cross the landings between them. The nesting rule: the tag nearest the
        risers' row connects to the riser nearest the column, and each tag
        farther down reaches one riser farther away - so every longer drop wraps
        around the shorter ones. Stray non-riser members keep a normal leader
        and fill the far end of the column.

        Returns (height_targets, specs, bottom): the per-member target heights,
        a ('riser', index) spec per point member, and the column's lowest height
        (so a caller can stack the next block below it).
        """
        n = len(members)
        point_indices = [i for i in members if pointlike[i]]
        others = [i for i in members if not pointlike[i]]
        column_across = _reference_coordinate_at(line, right, up, column_top)

        # Which side of the column the risers sit, and whether their row is
        # above or below the column of tags.
        if point_indices:
            mean_across = (sum(pipe_across[i] for i in point_indices)
                           / float(len(point_indices)))
            side = 1.0 if mean_across >= column_across else -1.0
            mean_up = (sum(pipe_up[i] for i in point_indices)
                       / float(len(point_indices)))
            column_centre = column_top - (n - 1) * pitch / 2.0
            points_above = mean_up >= column_centre
            nearest_first = sorted(point_indices,
                                   key=lambda i: side * pipe_across[i])
        else:
            points_above = True
            nearest_first = []

        others_sorted = sorted(others, key=lambda i: -pipe_up[i])
        if points_above:
            ordered = nearest_first + others_sorted
        else:
            ordered = others_sorted + list(reversed(nearest_first))

        height_targets = {}
        for position, index in enumerate(ordered):
            height_targets[index] = column_top - position * pitch

        specs = [('riser', i) for i in point_indices]
        bottom = column_top - (n - 1) * pitch if n else column_top
        return height_targets, specs, bottom

    # -- L-leader mode (horizontal pipes) ---------------------------------
    def _horizontal_moves(self, tags, view, context, line, right, up, heads,
                          bounds, pipe_up, pipe_across, horizontal, elements,
                          pitch):
        """Stack every tag in one column; plan a 90-degree leader for each.

        A thin wrapper over _horizontal_block (whole selection, one column
        anchored to the top of the reference line) plus _build_leader_plan.
        """
        column_top = max(utils.project(line.GetEndPoint(0), up),
                         utils.project(line.GetEndPoint(1), up))
        height_targets, specs, _bottom = self._horizontal_block(
            line, right, up, view, pipe_up, pipe_across, horizontal, elements,
            pitch, list(range(len(tags))), column_top)
        moves, new_heads = self._assemble_moves(
            tags, line, right, up, heads, bounds, height_targets)
        context['leader_plan'] = self._build_leader_plan(
            specs, tags, view, new_heads, right, up, pipe_up, pipe_across,
            elements)
        return moves

    def _horizontal_block(self, line, right, up, view, pipe_up, pipe_across,
                          leadered_flags, elements, pitch, members, column_top):
        """Lay `members` out as an L-leader column starting at column_top.

        The column is ordered highest-pipe-on-top. Each `leadered` member (one
        flagged in leadered_flags) later gets an elbow at (turn_across,
        tag_height) - a horizontal landing from the head, a vertical drop to the
        pipe - where turn_across is the MIDDLE of that pipe's own segment.
        Segments sharing a middle (a bundle) fan their drops apart, centred on
        that middle. Members NOT flagged sit in the column with a normal leader.

        Returns (height_targets, specs, bottom): the per-member target heights,
        a ('horiz', index, turn_across) spec per leadered member, and the
        column's lowest height.
        """
        n = len(members)

        # Column: highest pipe on top, stacking downward at pitch.
        top_to_bottom = sorted(members, key=lambda i: pipe_up[i], reverse=True)
        height_targets = {}
        rank_of = {}
        for rank, index in enumerate(top_to_bottom):
            height_targets[index] = column_top - rank * pitch
            rank_of[index] = rank

        # Do the pipes sit ABOVE or BELOW the tag column? Inside a fanned
        # cluster this decides which way the drops nest so their leaders do not
        # cross: pipes above -> the top tag (rank 0) takes the near end of the
        # fan; pipes below -> the bottom tag.
        leadered = [i for i in members if leadered_flags[i]]
        leadered_ups = [pipe_up[i] for i in leadered]
        if leadered_ups:
            mean_pipe_up = sum(leadered_ups) / float(len(leadered_ups))
        elif n:
            mean_pipe_up = sum(pipe_up[i] for i in members) / float(n)
        else:
            mean_pipe_up = column_top
        column_centre = column_top - (n - 1) * pitch / 2.0
        pipes_above = mean_pipe_up >= column_centre

        step = utils.paper_mm_to_model(view, config.HORIZONTAL_LEADER_STEP_MM)
        clear = utils.paper_mm_to_model(view, config.HORIZONTAL_LEADER_CLEAR_MM)
        column_across = _reference_coordinate_at(line, right, up, column_top)

        # Each drop lands at the MIDDLE of its own pipe segment (pipe_across).
        # Where several segments share a middle (a parallel bundle) they fan
        # apart by `step`, centred on that middle, using the same de-overlap as
        # the tag column. Within a cluster the drops follow the column order so
        # their leaders nest rather than cross.
        desired = dict((index, pipe_across[index]) for index in leadered)

        turn_of = {}
        across_order = sorted(desired.keys(), key=lambda i: desired[i])
        for block in _declutter_blocks(across_order, desired, step):
            bmembers = block['members']
            leftmost = block['centre'] - (block['n'] - 1) * step / 2.0
            near_is_left = block['centre'] >= column_across
            if pipes_above == near_is_left:
                ordered = sorted(bmembers, key=lambda i: rank_of[i])
            else:
                ordered = sorted(bmembers, key=lambda i: -rank_of[i])
            for offset, index in enumerate(ordered):
                turn = leftmost + offset * step
                span = utils.get_curve_span(elements[index], right)
                if span is not None:
                    low, high = span[0] + clear, span[1] - clear
                    if low <= high:
                        turn = min(max(turn, low), high)
                turn_of[index] = turn

        specs = [('horiz', i, turn_of[i]) for i in leadered]
        bottom = column_top - (n - 1) * pitch if n else column_top
        return height_targets, specs, bottom

    # -- turn the layout specs into leader elbow/arrow points --------------
    def _build_leader_plan(self, specs, tags, view, new_heads, right, up,
                           pipe_up, pipe_across, elements):
        """Turn ('riser'/'horiz', ...) specs into (tag, elbow, arrow) tuples.

        Run after the head moves are assembled, so each elbow starts from the
        tag's final head position. A riser drops straight onto its point; a
        horizontal turns down at turn_across to the pipe. A member whose pipe
        has no anchor keeps its normal leader (riser skipped; horizontal falls
        back to a vertical drop from the elbow).
        """
        plan = []
        for spec in specs:
            index = spec[1]
            head = new_heads.get(index)
            if head is None:
                continue
            if spec[0] == 'riser':
                elbow = utils.shift(
                    head, right,
                    pipe_across[index] - utils.project(head, right))
                anchor_pt = utils.get_element_anchor(elements[index], view)
                if anchor_pt is None:
                    continue    # nothing to point at - keep the normal leader
                plan.append((tags[index], elbow, anchor_pt))
            elif spec[0] == 'level':
                # One straight level leader: the arrow meets the pipe at the
                # tag's own row height (spec[2], clamped inside the run), so
                # the line is level wherever the row falls inside the pipe and
                # gently slants only when clamped at an end. The elbow grip
                # parks at the line's midpoint, never blocking a manual grab.
                arrow_v = spec[2]
                anchor_pt = utils.get_element_anchor(elements[index], view)
                if anchor_pt is None:
                    continue    # nothing to point at - keep the normal leader
                arrow = utils.shift(
                    anchor_pt, up, arrow_v - utils.project(anchor_pt, up))
                mid_u = (utils.project(head, right)
                         + utils.project(arrow, right)) / 2.0
                elbow = utils.shift(
                    head, right, mid_u - utils.project(head, right))
                # Pin the arrow: attached, Revit slides it to the pipe's END
                # and the "straight" leader draws with a kink whenever the row
                # sits mid-pipe (see leader_manager.apply_elbows).
                plan.append((tags[index], elbow, arrow, True))
            else:  # 'horiz'
                turn_across = spec[2]
                elbow = utils.shift(
                    head, right, turn_across - utils.project(head, right))
                anchor_pt = utils.get_element_anchor(elements[index], view)
                if anchor_pt is not None:
                    arrow = utils.shift(
                        anchor_pt, right,
                        turn_across - utils.project(anchor_pt, right))
                else:
                    arrow = utils.shift(
                        elbow, up, pipe_up[index] - utils.project(elbow, up))
                plan.append((tags[index], elbow, arrow))
        return plan

    # -- Auto mode: ONE mixed column on the reference line -----------------
    def _auto_moves(self, tags, view, context, g):
        """Lay the whole selection out as ONE SPREAD column on the line.

        The user-approved model (placement-plan artifact v3, 2026-08-02):
        every tag - horizontals and risers alike - sits ON the
        drawn line, and the column SPREADS along it so each tag sits at its
        own pipe's band instead of bunching at the top (the bunching funnelled
        every leader through one corridor). Default rows: level with the pipe
        for a screen-vertical run; one row ABOVE the pipe for screen-
        horizontal runs and risers, so their leaders land with a visible
        90-degree drop, arrow down. Rows push UP (never down) where the
        default would clash, keeping pipe order - so each leader stays in its
        own band and crossings remain impossible, while drops lengthen only
        where the area is busy.

        Formatting follows the Align Tags contract via the shared settings
        file: row pitch = tallest drawn text height + the vertical_mm clear
        gap, text left edges flush on the line (EDGE_LOW anchoring), angle-0
        leader shapes (level straight leaders into screen-vertical runs,
        90-degree turns onto screen-horizontal runs, drops onto riser points).
        """
        line = g['line']
        right, up = g['right'], g['up']
        heads, bounds = g['heads'], g['bounds']
        pipe_up, pipe_across = g['pipe_up'], g['pipe_across']
        pointlike, elements, count = g['pointlike'], g['elements'], g['count']
        horizontal_flags = g['horizontal']

        line_top = max(utils.project(line.GetEndPoint(0), up),
                       utils.project(line.GetEndPoint(1), up))

        def _snap_to(value):
            """Nearest lattice row measured from the drawn line's top."""
            return line_top - round((line_top - value) / pitch) * pitch

        # Spacing from the SHARED Align Tags settings file (handoff s1/s3).
        settings = engine_bridge.load_settings()
        pitch = engine_bridge.row_pitch(bounds, view, settings, tags)
        step = utils.paper_mm_to_model(view, config.HORIZONTAL_LEADER_STEP_MM)
        clear = utils.paper_mm_to_model(view, config.HORIZONTAL_LEADER_CLEAR_MM)

        # VISION (user-approved 2026-08-03): the pre-placement ink map, when
        # script.py captured one. Row scoring adds a penalty proportional to
        # how much ink already sits where the tag's TEXT would go, so tags
        # prefer blank paper - dimensions, notes, walls and unselected pipes
        # repel them even though the geometric model knows nothing of those.
        ink_map = context.get('ink_map')
        ink_weight = config.AUTO_INK_WEIGHT_ROWS
        widths = [s[0][1] - s[0][0] for s in bounds.values() if s and s[0]]
        heights = [s[1][1] - s[1][0] for s in bounds.values() if s and s[1]]
        text_w = sorted(widths)[len(widths) // 2] if widths else 4.0 * pitch
        text_h = (sorted(heights)[len(heights) // 2] if heights
                  else 0.8 * pitch)
        _ink_cache = {}

        # REACH: how far out from the tag column each target sits. `outward`
        # points from the column towards the pipes, so a bigger reach is a
        # target further away, whichever side the pipes are on.
        column_across = _reference_coordinate_at(line, right, up, line_top)
        mean_across = sum(pipe_across) / float(count) if count else 0.0
        outward = 1.0 if mean_across >= column_across else -1.0
        reach = [outward * (pipe_across[i] - column_across)
                 for i in range(count)]

        # Do the targets sit below the tag column or above it?
        mean_up = sum(pipe_up) / float(count) if count else 0.0
        column_centre = line_top - (count - 1) * pitch / 2.0
        targets_below = mean_up <= column_centre

        # THE STAIRCASE (user markup, 2026-08-02): within a cluster, rows are
        # ordered by REACH - the TOP tag takes the FARTHEST target and each tag
        # below reaches one step nearer, so every leader nests inside the one
        # above it. With the column right of the pipes and the pipes below (the
        # usual case) this reads exactly as the user drew it: top-to-bottom in
        # the column maps to left-to-right along the run.
        #
        # This is what makes crossing impossible, and it replaces ordering by
        # pipe height - under that rule a long drop cut straight through the
        # landings of the tags between it and its pipe.
        #   * a landing never reaches a drop ABOVE it: those sit further out
        #     than the landing itself ever travels;
        #   * a landing never meets a drop BELOW it: each drop starts at its
        #     own row, so none of it exists at a higher tag's height.
        # When the targets sit ABOVE the column the staircase mirrors: the top
        # tag takes the NEAREST target instead.
        #
        # sign is how the reach changes going DOWN one row: negative when the
        # targets are below (the top row is farthest and each row steps in),
        # positive when they are above. Sorting on sign*reach therefore puts
        # the farthest target on the top row in the first case, the nearest in
        # the second - the staircase in both.
        sign = -1.0 if targets_below else 1.0

        # CLUSTERS: one column for a whole floor makes every leader travel the
        # height of the drawing. Tags whose targets chain within cluster_mm form
        # a group, and each group's rows sit in ITS OWN pipes' band - so leaders
        # stay short and local. It also keeps clusters from interfering: a
        # cluster's leaders live inside its own band, so they never reach the
        # rows or the drops of another band. Blocks are laid out top-down and
        # pushed clear of each other, never above the drawn line.
        targets = [(pipe_across[i], pipe_up[i]) for i in range(count)]
        groups = engine_bridge.cluster_by_target(
            list(range(count)), targets, settings)
        groups.sort(key=lambda group:
                    -sum(pipe_up[i] for i in group) / float(len(group)))

        base_kinds = {}
        for index in range(count):
            if pointlike[index]:
                base_kinds[index] = 'riser'
            elif horizontal_flags[index]:
                base_kinds[index] = 'horiz'
            else:
                base_kinds[index] = 'level'

        # Anchors are read once: the audit re-runs the layout several times and
        # must not pay for Revit geometry on every pass.
        anchors_2d = []
        for element in elements:
            point = utils.get_element_anchor(element, view)
            anchors_2d.append(None if point is None else
                              (utils.project(point, right),
                               utils.project(point, up)))

        def _layout(ceiling):
            """Compute one complete arrangement (deterministic).

            Levels place first with straight leaders (unchanged priority);
            every other tag is seated by the exact-geometry greedy below, its
            own placement gated by the same crossing checker the log uses.

            `ceiling` is the highest row a tag may take. It starts at the drawn
            line and the caller raises it only when that removes crossings; the
            row LATTICE stays anchored to the line either way, so a lifted
            column keeps the same rhythm as an unlifted one.
            """
            kinds = dict(base_kinds)

            # Each cluster splits by LEADER KIND, because the two kinds want
            # opposite placements (user rule, 2026-08-02: "straight leader is the
            # priority for the vertical pipes and the horizontal pipes with 90 deg
            # leader to be adjusted accordingly"):
            #   level block - screen-vertical runs. Its rows sit ON the pipes' own
            #       band, so every leader is a straight level line into the pipe's
            #       side. This placement wins; the drop blocks work around it.
            #       Level leaders are all horizontal, so they can never cross.
            #   drop block  - horizontal runs and risers. Its rows sit CLEAR of its
            #       pipes so every drop in the block points the same way, which the
            #       staircase proof depends on (a block inside its own band has
            #       some drops going down and some up, and those cut each other's
            #       landings - 17 crossings in the check that caught this).
            # A vertical run earns a straight leader when ITS OWN pipe can host
            # ITS OWN row. The rule used to demand that every row fit inside the
            # INTERSECTION of all the cluster's pipes, and demoted the whole
            # cluster when it could not - so one short pipe vetoed straight
            # leaders for its longer neighbours, and a 4-tag bundle needed
            # 3*pitch of common overlap. On the user's 2026-08-04 drawing that
            # turned a clean vertical bundle into four 90-degree drops landing
            # inside 2.2mm of paper (arrowheads merged into one blob, one
            # crossing, an uneven column). Tag i's leader only ever touches pipe
            # i, so that is the only span that matters; anything that genuinely
            # cannot be seated is demoted ALONE.
            blocks = []
            level_rows_planned = {}     # index -> the row its own pipe can host
            demoted_note = []           # (cluster size, kept, demoted) for the log
            seat_trace = []             # (index, usable_lo, usable_hi, row) 
            for group in groups:
                level_members = [i for i in group if kinds[i] == 'level']
                drop_members = [i for i in group if kinds[i] != 'level']
                if level_members:
                    seats, homeless, trace = _seat_level_rows(
                        level_members, elements, up, pitch, clear, pipe_across,
                        _snap_to)
                    seat_trace.extend(trace)
                    if homeless:
                        demoted_note.append(
                            (len(level_members), len(seats), len(homeless)))
                    for index in homeless:
                        kinds[index] = 'horiz'
                    drop_members = drop_members + homeless
                    level_members = [i for i in level_members
                                     if i not in homeless]
                    level_rows_planned.update(seats)
                if level_members:
                    blocks.append((level_members, True))
                if drop_members:
                    blocks.append((drop_members, False))
            blocks.sort(key=lambda block:
                        -sum(pipe_up[i] for i in block[0]) / float(len(block[0])))

            height_targets = {}
            drop_reach = {}
            order = []
            occupied = []       # (low, high) level-block bands already taken
            block_of = {}       # tag index -> the block that placed it

            def _free(low, high):
                """True if this band overlaps no block already placed.

                Bands carry half a pitch of padding, so this is the test for "can
                a block's rows sit here without touching another block's rows".
                """
                for taken_low, taken_high in occupied:
                    if not (high < taken_low or low > taken_high):
                        return False
                return True

            def _snap(value):
                """Put a row on the shared pitch lattice measured from the line.

                Every block lands on the same ladder of rows, so the gap between
                any two tags in the finished column is an exact multiple of the
                pitch instead of an arbitrary offset - which is what made the
                spacing read as ragged (277mm here, 2030mm there).
                """
                return line_top - round((line_top - value) / pitch) * pitch

            def _assign_level(local, top, block_index):
                """Give a LEVEL block its rows from `top` down: straight leaders."""
                for row, index in enumerate(local):
                    height_targets[index] = top - row * pitch
                    drop_reach[index] = reach[index]
                    block_of[index] = block_index
                order.extend(local)
                occupied.append((top - (len(local) - 1) * pitch - pitch / 2.0,
                                 top + pitch / 2.0))

            # --- phase 1: the straight leaders, placed first (the priority) ---
            # A vertical run's tag sits ON its pipe's band, so the leader is a dead
            # level line into the pipe's side. Level leaders are all horizontal, so
            # they can never cross each other - which is why they can claim their
            # positions first and let the drops work around them.
            for block_index, (members, is_level) in enumerate(blocks):
                if not is_level:
                    continue
                # Level leaders are horizontal lines - they physically cannot cross
                # each other - so this block needs no staircase and reads in plain
                # drawing order instead: LEFTMOST pipe on the top row, the same
                # convention Align Tags uses for a vertical bundle.
                # Each row was already chosen to sit on ITS OWN pipe during the
                # split, so use those seats verbatim - re-deriving a shared band
                # here is what forced every row into the intersection.
                planned = dict((i, level_rows_planned[i]) for i in members
                               if i in level_rows_planned)
                if len(planned) == len(members):
                    shift = 0.0
                    for _try in range(_SLOT_TRIES):
                        rows = [planned[i] - shift for i in members]
                        if _free(min(rows) - pitch / 2.0,
                                 max(rows) + pitch / 2.0):
                            break
                        shift += pitch      # another block already sits here
                    for index in sorted(members,
                                        key=lambda i: -planned[i]):
                        row = planned[index] - shift
                        height_targets[index] = row
                        drop_reach[index] = reach[index]
                        block_of[index] = block_index
                        order.append(index)
                    lowest = min(planned[i] for i in members) - shift
                    highest = max(planned[i] for i in members) - shift
                    occupied.append((lowest - pitch / 2.0,
                                     highest + pitch / 2.0))
                    continue

                # Fallback: no plan (span unreadable) - the old centred block.
                local = sorted(members, key=lambda i: (pipe_across[i], -pipe_up[i]))
                span_v = (len(local) - 1) * pitch
                band = sum(pipe_up[i] for i in members) / float(len(local))
                top = _snap(band + span_v / 2.0)
                for _try in range(_SLOT_TRIES):
                    if _free(top - span_v, top):
                        break
                    top -= pitch
                _assign_level(local, top, block_index)

            # --- phase 2: the exact-geometry greedy (audit-verified winner) ---
            # The multi-agent audit on the two logged runs (2026-08-03) derived
            # the EXACT pairwise non-crossing condition and refuted every proxy:
            # with one column and 90-degree leaders, the ONLY possible crossing
            # is a landing (or level line) whose row lies inside another
            # leader's drop band while that drop sits NEARER the column.
            # Landings are mutually parallel; drops are mutually parallel;
            # nothing else can touch. So for a tag at row r the whole rule
            # collapses to an interval [LB, UB] of allowed drop positions, and
            # a tag simply takes the nearest lattice row where that interval is
            # non-empty. No staircase, no side-flips, no slabs: feasibility is
            # computed, never assumed. Rows extend DOWNWARD as far as needed
            # (user-sanctioned); the drawn line's top is a hard wall.
            level_rows = [(height_targets[i], reach[i]) for i in order]
            placed = []     # (index, row_v, drop, band_low, band_high)

            # WHICH SIDE each horizontal cluster takes (user rule 2026-08-04:
            # "the horizontal cluster can be pushed above or below based on
            # the situation"). The verticals are already down and immovable,
            # so each horizontal cluster looks at both sides of its own pipes
            # and prefers the one whose drops would pass the FEWEST rows that
            # are already placed. That is what stops a cluster's leaders from
            # running the length of the vertical band - the long parallel
            # lines that read as tangle even when nothing crosses.
            side_pref = {}
            if config.AUTO_CLUSTER_SIDE_BIAS_ROWS > 0:
                settled = [height_targets[i] for i in order]
                for group in groups:
                    members = [i for i in group if kinds[i] != 'level']
                    if not members:
                        continue
                    high = max(pipe_up[i] for i in members)
                    low = min(pipe_up[i] for i in members)
                    reachspan = (len(members) + 1) * pitch
                    above = sum(1 for v in settled
                                if high < v <= high + reachspan)
                    below = sum(1 for v in settled
                                if low - reachspan <= v < low)
                    if above == below:
                        continue                # no reason to prefer a side
                    choice = 1.0 if above < below else -1.0
                    for index in members:
                        side_pref[index] = choice

            def _span_reach(index):
                """The tag's allowed drop range along its own pipe (reach)."""
                if kinds[index] != 'horiz':
                    return reach[index], reach[index]
                span = utils.get_curve_span(elements[index], right)
                if span is None:
                    return reach[index], reach[index]
                low = outward * (span[0] - column_across)
                high = outward * (span[1] - column_across)
                low, high = min(low, high) + clear, max(low, high) - clear
                if low > high:
                    low = high = (low + high) / 2.0
                return low, high

            def _interval(index, row):
                """[LB, UB] of drop positions that cross NOTHING at this row.

                LB: this tag's drop must sit FURTHER out than every leader
                whose row its band passes (their landings stop short of it).
                UB: this tag's row must not sit inside the band of any drop
                that is NEARER the column than its own.
                """
                pipe_v = pipe_up[index]
                band_low, band_high = ((row, pipe_v) if row < pipe_v
                                       else (pipe_v, row))
                lower, upper = _span_reach(index)
                for level_v, level_reach in level_rows:
                    if band_low < level_v < band_high:
                        lower = max(lower, level_reach + clear)
                for _j, row_j, drop_j, b_low, b_high in placed:
                    if band_low < row_j < band_high:
                        lower = max(lower, drop_j + clear)
                    if b_low < row < b_high:
                        upper = min(upper, drop_j - clear)
                return lower, upper

            def _taken_rows():
                return [height_targets[i] for i in order]

            # Screen-vertical pipes are OBSTACLES for drops: a drop at (or
            # within `clear` of) a vertical pipe's own u runs COLLINEAR with
            # that pipe - the checker scores parallel overlap as zero
            # crossings, but on paper the leader is buried inside the bundle
            # (the user's 2026-08-03 report: three leaders riding the pipes
            # at u=58446/58632). Collected from the level tags' real spans.
            vertical_pipes = []
            for i in range(count):
                if base_kinds[i] != 'level':
                    continue
                span_v = utils.get_curve_span(elements[i], up)
                if span_v is None:
                    span_v = (pipe_up[i], pipe_up[i])
                vertical_pipes.append((reach[i], span_v[0], span_v[1]))

            def _dodge_pipes(index, row, drop, lower, upper):
                """Nudge a drop off any vertical pipe its v-range overlaps."""
                pipe_v = pipe_up[index]
                b_low, b_high = ((row, pipe_v) if row < pipe_v
                                 else (pipe_v, row))
                for pipe_reach, v_low, v_high in vertical_pipes:
                    if v_high < b_low or v_low > b_high:
                        continue
                    if abs(drop - pipe_reach) < clear:
                        if pipe_reach + clear <= upper:
                            drop = pipe_reach + clear
                        elif pipe_reach - clear >= lower:
                            drop = pipe_reach - clear
                return drop

            def _row_ink(row):
                """Ink fraction under the tag text at this row (cached)."""
                key = int(round((line_top - row) / pitch))
                if key not in _ink_cache:
                    _ink_cache[key] = ink_map.ink_fraction(
                        column_across, column_across + text_w,
                        row - text_h / 2.0, row + text_h / 2.0)
                return _ink_cache[key]

            def _row_cost(row, base, index=None):
                # Nearest to the pipe wins; rows above it pay one pitch, so
                # the column prefers to grow downward; inked paper repels the
                # text in proportion to how much already sits there; and a row
                # on the side its cluster did NOT choose pays a bias, so the
                # cluster stays together on the quieter side without being
                # forced there when the geometry says otherwise.
                cost = abs(row - base) + (pitch if row > base else 0.0)
                if ink_map is not None and ink_weight > 0.0:
                    cost += ink_weight * pitch * _row_ink(row)
                wanted = side_pref.get(index, 0.0)
                if wanted:
                    on_side = 1.0 if row > base else -1.0
                    if on_side != wanted:
                        cost += config.AUTO_CLUSTER_SIDE_BIAS_ROWS * pitch
                return cost

            def _candidate_rows(index, taken, near_limit=None):
                """Free lattice rows for this tag, best (nearest) first.

                `near_limit` keeps rows at least that far from the tag's own
                pipe - passed as min_drop when a 90-degree leader must show a
                readable vertical segment (user rule 2026-08-03).
                """
                base = pipe_up[index]
                limit = clear if near_limit is None else near_limit
                start = _snap(base)
                rows = [start]
                for k in range(1, _SLOT_TRIES):
                    rows.append(start - k * pitch)
                    rows.append(start + k * pitch)
                rows.sort(key=lambda r: _row_cost(r, base, index))
                for row in rows:
                    if row > ceiling + 1e-9:
                        continue        # never above the column's ceiling
                    if abs(row - base) < limit:
                        continue        # too close to read as a drop
                    if any(abs(row - t) < pitch - 1e-6 for t in taken):
                        continue
                    yield row

            def _place(index, row, drop):
                pipe_v = pipe_up[index]
                band = ((row, pipe_v) if row < pipe_v else (pipe_v, row))
                placed.append((index, row, drop, band[0], band[1]))
                height_targets[index] = row
                drop_reach[index] = drop
                block_of[index] = -1
                order.append(index)

            def _unplace(index):
                for position, entry in enumerate(placed):
                    if entry[0] == index:
                        placed.pop(position)
                        break
                height_targets.pop(index, None)
                drop_reach.pop(index, None)
                block_of.pop(index, None)
                if index in order:
                    order.remove(index)

            def _leader_triple(index):
                """This tag's leader as 2D points, for the real checker."""
                row = height_targets[index]
                if kinds[index] == 'level':
                    arrow_u = pipe_across[index]
                    return ((column_across, row),
                            ((column_across + arrow_u) / 2.0, row),
                            (arrow_u, row))
                drop_u = column_across + outward * drop_reach[index]
                if kinds[index] == 'riser':
                    anchor = anchors_2d[index]
                    tip = (anchor if anchor is not None
                           else (drop_u, pipe_up[index]))
                    return ((column_across, row),
                            (pipe_across[index], row), tip)
                return ((column_across, row), (drop_u, row),
                        (drop_u, pipe_up[index]))

            def _real_crossings():
                leaders = dict((i, _leader_triple(i)) for i in order)
                return len(diagnostics.find_crossings(leaders))

            horiz_tags = [i for i in range(count) if kinds[i] != 'level']
            sequence = sorted(horiz_tags, key=lambda i: (reach[i], i))

            # Greedy with promote-and-retry: short landings place first (their
            # bands block least); a tag that finds no feasible row anywhere is
            # the most constrained, so it goes to the FRONT and the pass
            # restarts. The audit measured dataset A converging with no
            # retries and B with one.
            def _clear_of_pipes(index, row, drop):
                """True if this drop overlaps no vertical pipe's line."""
                return abs(_dodge_pipes(index, row, drop, drop, drop)
                           - drop) < 1e-9

            # A 90-degree drop must be long enough to READ as a drop
            # (user rule 2026-08-03: "some leader length minimal in the
            # 90 deg leader") - one row by default, config.AUTO_MIN_DROP_ROWS.
            min_drop = max(clear, config.AUTO_MIN_DROP_ROWS * pitch)

            def _seat_exact(index):
                """Try to seat one tag by the exact rule. True on success.

                Four preference tiers, best first: a full visible drop AND
                clear of every vertical pipe; then clear-of-pipes with a
                short drop; then a full drop that may ride; then anything
                feasible - completeness always beats aesthetics, and the
                reframe pass repairs what the lower tiers accepted.
                """
                taken = _taken_rows()
                for near_limit, need_clear in ((min_drop, True),
                                               (clear, True),
                                               (min_drop, False),
                                               (clear, False)):
                    for row in _candidate_rows(index, taken, near_limit):
                        lower, upper = _interval(index, row)
                        if kinds[index] == 'riser':
                            if lower - 1e-9 <= reach[index] <= upper + 1e-9:
                                _place(index, row, reach[index])
                                return True
                        elif lower <= upper + 1e-9:
                            drop = _dodge_pipes(
                                index, row,
                                min(max(reach[index], lower), upper),
                                lower, upper)
                            if need_clear and not _clear_of_pipes(index, row,
                                                                  drop):
                                continue
                            _place(index, row, drop)
                            return True
                return False

            skipped = []
            for _retry in range(min(len(sequence), 8) + 1):
                for entry in list(placed):
                    _unplace(entry[0])
                skipped = []
                failed = None
                for index in sequence:
                    if _seat_exact(index):
                        continue
                    if sequence and sequence[0] == index:
                        skipped.append(index)   # promoted and STILL stuck
                    else:
                        failed = index
                        break
                if failed is None:
                    break
                sequence.remove(failed)
                sequence.insert(0, failed)

            # COMPLETENESS - every tag gets a row, no matter what. The retry
            # budget can exhaust MID-PASS, abandoning the tail of the sequence
            # unplaced: on the first field run 26 of 40 tags were left sitting
            # wherever Revit created them, scattered across the drawing. Any
            # tag still without a row gets one more exact-rule attempt (the
            # board has filled since its pass, so feasibility may have
            # changed), and whatever remains joins the checker-scored
            # fallback below. Nothing is ever left behind again.
            for index in sequence:
                if index in height_targets or index in skipped:
                    continue
                if not _seat_exact(index):
                    skipped.append(index)

            # Checker-scored fallback for anything the exact rule could not
            # seat (a zero provably does not exist for it): take the
            # (row, drop) the REAL checker scores lowest. Deterministic and
            # bounded at 120 checker calls per tag.
            for index in skipped:
                if index in height_targets:
                    continue        # seated by a later completeness pass
                taken = _taken_rows()
                span_low, span_high = _span_reach(index)
                base = pipe_up[index]
                start = _snap(base)
                rows = [start]
                for k in range(1, _SLOT_TRIES):
                    rows.append(start - k * pitch)
                    rows.append(start + k * pitch)
                rows.sort(key=lambda r: _row_cost(r, base, index))
                best_choice = None
                tried = 0
                for row in rows:
                    if tried >= 120:
                        break
                    if row > ceiling + 1e-9 or abs(row - base) < clear:
                        continue
                    if any(abs(row - t) < pitch - 1e-6 for t in taken):
                        continue
                    lower, upper = _interval(index, row)
                    drops = [min(max(reach[index], span_low), span_high),
                             span_low, span_high]
                    if lower <= upper + 1e-9:
                        drops.append(min(max(reach[index], lower), upper))
                    drops = [_dodge_pipes(index, row, d, span_low, span_high)
                             for d in drops]
                    for drop in drops:
                        if tried >= 120:
                            break
                        tried += 1
                        _place(index, row, drop)
                        score = (_real_crossings(),
                                 _row_cost(row, base, index))
                        _unplace(index)
                        if best_choice is None or score < best_choice[0]:
                            best_choice = (score, row, drop)
                    if best_choice is not None and best_choice[0][0] == 0:
                        break
                if best_choice is not None:
                    _place(index, best_choice[1], best_choice[2])
                    if best_choice[0][0]:
                        utils.logger.debug(
                            'Auto Tag: tag {0} seated with {1} unavoidable '
                            'crossing(s).'.format(index, best_choice[0][0]))
                else:
                    # Last resort: below everything, clamped onto its pipe.
                    # A tag in a suboptimal row is recoverable; a tag never
                    # placed at all is the scatter the user photographed.
                    bottom = min([line_top] + _taken_rows()) - pitch
                    _place(index, _snap(bottom),
                           min(max(reach[index], span_low), span_high))

            # --- REFRAME: the tool re-reads its own drawing and corrects ---
            # (user direction 2026-08-03: "it has to think and reframe the
            # tags after the first placement... as per the specific area").
            # After placing, the layout critiques itself with the same
            # geometry the log publishes and re-seats what fails the read:
            #   stage 1 - every 90-degree leader gets a VISIBLE drop. A tag
            #       seated closer to its pipe than min_drop moves to the
            #       nearest row a full drop away, which naturally spends any
            #       vacant stretch of the column beside it.
            #   stage 2 - the longest leaders pull nearer, never below
            #       min_drop, so the polish can't recreate a short drop.
            # Every accepted move is gated by the REAL crossing checker.
            baseline = _real_crossings()

            def _try_rows(index, rows_iterable, cap):
                """Trial-move a tag; keep the first checker-approved row."""
                base = pipe_up[index]
                tried = 0
                for row in rows_iterable:
                    if tried >= cap:
                        break
                    tried += 1
                    snapshot = (list(placed), dict(height_targets),
                                dict(drop_reach), list(order))
                    for position, entry in enumerate(placed):
                        if entry[0] == index:
                            band = ((row, base) if row < base
                                    else (base, row))
                            placed[position] = (index, row,
                                                drop_reach[index],
                                                band[0], band[1])
                            height_targets[index] = row
                            break
                    # Fixed-point relax: every drop settles into its interval.
                    for _pass in range(6):
                        for position, entry in enumerate(placed):
                            j = entry[0]
                            if kinds[j] == 'riser':
                                continue
                            lower, upper = _interval(j, entry[1])
                            if lower <= upper + 1e-9:
                                new_drop = min(max(drop_reach[j], lower),
                                               upper)
                                new_drop = _dodge_pipes(j, entry[1],
                                                        new_drop, lower,
                                                        upper)
                                drop_reach[j] = new_drop
                                placed[position] = (j, entry[1], new_drop,
                                                    entry[3], entry[4])
                    feasible = True
                    for entry in placed:
                        j = entry[0]
                        if kinds[j] == 'riser':
                            continue
                        lower, upper = _interval(j, entry[1])
                        if not (lower - 1e-9 <= drop_reach[j]
                                <= upper + 1e-9):
                            feasible = False
                            break
                    if feasible and _real_crossings() <= baseline:
                        return True
                    placed[:] = snapshot[0]
                    height_targets.clear()
                    height_targets.update(snapshot[1])
                    drop_reach.clear()
                    drop_reach.update(snapshot[2])
                    order[:] = snapshot[3]
                return False

            # Stage 1: visible drops (spends the vacant column space).
            for _sweep in range(20):
                violators = [entry[0] for entry in placed
                             if abs(height_targets[entry[0]]
                                    - pipe_up[entry[0]]) < min_drop - 1e-6]
                if not violators:
                    break
                moved_any = False
                for index in violators:
                    taken = [value for i2, value in height_targets.items()
                             if i2 != index]
                    if _try_rows(index,
                                 _candidate_rows(index, taken, min_drop),
                                 30):
                        moved_any = True
                if not moved_any:
                    break

            # Stage 2: pull the worst-verticality tag nearer, min_drop kept.
            for _sweep in range(30):
                if not placed:
                    break
                worst = max(placed,
                            key=lambda entry: abs(entry[1]
                                                  - pipe_up[entry[0]]))
                index = worst[0]
                base = pipe_up[index]
                current_cost = _row_cost(height_targets[index], base, index)
                taken = [value for i2, value in height_targets.items()
                         if i2 != index]

                def _nearer_only(rows_iterable, limit_cost, pipe_v):
                    for row in rows_iterable:
                        if _row_cost(row, pipe_v, index) >= limit_cost:
                            return      # sorted: nothing nearer remains
                        yield row

                if not _try_rows(index,
                                 _nearer_only(
                                     _candidate_rows(index, taken, min_drop),
                                     current_cost, base),
                                 30):
                    break

            # --- RE-DEAL: the drop block reads in its pipes' own order -------
            # (user rule 2026-09-01: "the horizontal cluster is not following
            # top to bottom rule".) The greedy picks each row by how near it
            # sits to that tag's own pipe. For a PARALLEL BUNDLE - pipes a
            # fraction of a pitch apart - that measure cannot tell them apart,
            # so which tag lands on which row comes down to rounding, and the
            # column reads in no order at all.
            #
            # So the block is re-dealt: the SAME rows, the same tags, but
            # sorted by pipe height - top pipe on the top row - and the drops
            # re-fanned along the pipes in the one order the staircase permits
            # (a higher row must reach FARTHER, or its landing would meet the
            # drop below it). The spread halves until every drop fits inside
            # its own pipe's length, so a bundle with little shared run still
            # separates as far as it can instead of stacking on one line.
            #
            # The checker has the last word: a re-deal is kept only if it
            # crosses no more than the arrangement it replaces.
            def _redeal(members):
                """(index, row, drop) per member, in pipe order, or None."""
                rows_free = sorted((height_targets[i] for i in members),
                                   reverse=True)
                ordered = sorted(members, key=lambda i: (-pipe_up[i], i))
                windows = [_span_reach(i) for i in ordered]

                def _fan(spread):
                    """Drop reaches at this spacing, or None if they do not fit.

                    Each drop takes the farthest point on its own pipe that is
                    at least `spread` inside the one above it. Decreasing, so
                    the higher row always reaches farther.
                    """
                    seats = []
                    limit = None
                    for low, high in windows:
                        want = high if limit is None else min(high,
                                                              limit - spread)
                        if want < low - 1e-9:
                            return None
                        seats.append(want)
                        limit = want
                    return seats

                # Widest spacing the pipes can actually carry, up to `step`.
                # Bisected rather than halved: on the 2026-09-01 bundle
                # halving settled for 375mm where 493mm fits, and every
                # millimetre counts when four arrowheads share 6mm of paper.
                best = _fan(0.0)
                if best is None:
                    return None
                low_s, high_s = 0.0, step
                if _fan(step) is not None:
                    best = _fan(step)
                else:
                    for _try in range(20):
                        middle = (low_s + high_s) / 2.0
                        found = _fan(middle)
                        if found is None:
                            high_s = middle
                        else:
                            best, low_s = found, middle
                return list(zip(ordered, rows_free, best))

            for group in groups:
                block = [i for i in group
                         if kinds[i] == 'horiz' and i in height_targets]
                if len(block) < 2:
                    continue
                by_row = sorted(block, key=lambda i: -height_targets[i])
                if by_row == sorted(block, key=lambda i: (-pipe_up[i], i)):
                    continue        # already reads top pipe first
                deal = _redeal(block)
                if deal is None:
                    continue
                # Re-dealing swaps which tag owns which row, so a tag can land
                # nearer its own pipe than the minimum drop allows - an arrow
                # with no stem above it. Pipe order is not worth that, so the
                # stub count is guarded exactly like the crossing count.
                def _stubs_here():
                    return sum(1 for i in block
                               if abs(height_targets[i] - pipe_up[i])
                               < min_drop - 1e-9)

                before = _real_crossings()
                before_stubs = _stubs_here()
                keep = [(i, height_targets[i], drop_reach[i]) for i in block]
                for index in block:
                    _unplace(index)
                # Re-place top row first, each drop CLAMPED by the same
                # interval rule the greedy uses. The fan is only an aim: what
                # the rest of the sheet allows still wins, which is what keeps
                # a re-deal legal instead of merely tidy.
                spoiled = False
                for index, row, want in deal:
                    lower, upper = _interval(index, row)
                    low, high = _span_reach(index)
                    lower, upper = max(lower, low), min(upper, high)
                    if lower > upper + 1e-9:
                        spoiled = True
                        break
                    _place(index, row,
                           _dodge_pipes(index, row,
                                        min(max(want, lower), upper),
                                        lower, upper))
                if (spoiled or _real_crossings() > before
                        or _stubs_here() > before_stubs):
                    for index in block:
                        _unplace(index)
                    for index, row, drop in keep:
                        _place(index, row, drop)

            # Leader specs, by what the target IS on screen:
            #   riser point            -> landing + drop onto the point
            #   run horizontal         -> landing + true 90-degree drop at its
            #                             staircase position along the run
            #   run vertical on screen -> ONE straight level leader at the tag's
            #                             row height, into the pipe's side
            specs = []
            for index in order:
                kind = kinds[index]
                if kind == 'riser':
                    specs.append(('riser', index))
                elif kind == 'horiz':
                    specs.append(('horiz', index,
                                  column_across + outward * drop_reach[index]))
                else:
                    span = utils.get_curve_span(elements[index], up)
                    row_v = height_targets[index]
                    if span is not None:
                        low, high = span[0] + clear, span[1] - clear
                        arrow_v = (min(max(row_v, low), high) if low <= high
                                   else (span[0] + span[1]) / 2.0)
                    else:
                        arrow_v = pipe_up[index]
                    specs.append(('level', index, arrow_v))

            spans_v = {}
            spans_u = {}
            for index in range(count):
                span = utils.get_curve_span(elements[index], up)
                if span is not None:
                    spans_v[index] = span[1] - span[0]
                span = utils.get_curve_span(elements[index], right)
                if span is not None:
                    spans_u[index] = span[1] - span[0]
            return {'kinds': kinds, 'height': height_targets,
                    'drop': drop_reach, 'order': order, 'specs': specs,
                    'block_of': block_of, 'spans_v': spans_v,
                    'spans_u': spans_u, 'demoted': demoted_note,
                    'seat_trace': seat_trace}

        # --- the audit: plot it, look for crossovers, rethink, correct ------
        # An arrangement can be locally right and still tangle where two
        # clusters meet, and no amount of forward reasoning catches every case.
        # So the layout is CHECKED against the same geometric test the log
        # publishes, and when a crossing is found the tool changes its mind:
        # the block responsible is re-planned on the other side of its pipes
        # and the whole arrangement is scored again. It keeps the best of
        # everything it tried, so a repair can never make the drawing worse.
        def _audit(layout):
            """Return the crossings this arrangement would actually draw."""
            leaders = {}
            for spec in layout['specs']:
                index = spec[1]
                row_v = layout['height'][index]
                head_u = _reference_coordinate_at(line, right, up, row_v)
                anchor = anchors_2d[index]
                if spec[0] == 'riser':
                    if anchor is None:
                        continue
                    leaders[index] = ((head_u, row_v),
                                      (pipe_across[index], row_v), anchor)
                elif spec[0] == 'horiz':
                    turn_u = spec[2]
                    leaders[index] = ((head_u, row_v), (turn_u, row_v),
                                      (turn_u, pipe_up[index]))
                else:
                    arrow_u = anchor[0] if anchor else pipe_across[index]
                    leaders[index] = ((head_u, row_v),
                                      ((head_u + arrow_u) / 2.0, row_v),
                                      (arrow_u, spec[2]))
            return diagnostics.find_crossings(leaders)

        # One deterministic pass per ceiling: the layout gates every placement
        # with the real crossing checker itself, so the old flip-and-retry
        # audit loop is gone - _audit stays as the independent self-check.
        #
        # THE LIFT (user decision 2026-09-01). A 90-degree tag has to sit above
        # its pipe for the arrow to point down at it. Where the drawn line
        # leaves less headroom than the drop tags need, the ones that do not
        # fit are pushed BELOW the straight-leader block, and from there every
        # arrow must climb back through every straight leader - on the logged
        # run of that date, a line 493mm above the pipes gave room for one tag
        # of four and drew 9 crossings. Their pipes were too short to reach
        # past the straight leaders' arrows, so no drop position existed that
        # avoided it: the only cure is headroom.
        #
        # So the column may rise ABOVE the drawn line, one row at a time, and
        # keeps the FIRST height that draws no crossings - the smallest lift
        # that works. If none is clean it keeps the best it saw, so lifting can
        # never make the drawing worse than not lifting. The lattice does not
        # move, so a lifted column has the same rhythm as an unlifted one.
        # A STUB DROP counts as a fault too. A 90-degree leader whose drop is
        # shorter than the arrowhead draws as an arrow stuck to the elbow with
        # no stem above it - the reader cannot see which way it points. The
        # 2026-09-01 drawing ended with one such tag, 732mm of drop against an
        # 863mm rule, because the lift stopped the moment crossings reached
        # zero. Headroom fixes both, so the lift now looks for both: it climbs
        # while EITHER a crossing or a stub remains, and scores crossings
        # first (a crossing is a lie about what connects to what; a stub is
        # only hard to read).
        min_drop_rule = max(clear, config.AUTO_MIN_DROP_ROWS * pitch)

        def _stubs(candidate):
            """Drops too short to show a stem above the arrowhead."""
            count = 0
            for spec in candidate['specs']:
                if spec[0] not in ('horiz', 'riser'):
                    continue
                index = spec[1]
                row_v = candidate['height'].get(index)
                if row_v is None:
                    continue
                if abs(row_v - pipe_up[index]) < min_drop_rule - 1e-9:
                    count += 1
            return count

        lift_rows = max(0, int(config.AUTO_CEILING_LIFT_ROWS))
        layout = _layout(line_top)
        crossings = _audit(layout)
        audit_trail = [(0, len(crossings), _stubs(layout))]
        lifted = 0.0
        if (crossings or _stubs(layout)) and lift_rows:
            best = ((len(crossings), _stubs(layout)), 0, layout, crossings)
            for step in range(1, lift_rows + 1):
                trial = _layout(line_top + step * pitch)
                found = _audit(trial)
                score = (len(found), _stubs(trial))
                audit_trail.append((step, score[0], score[1]))
                if score < best[0]:
                    best = (score, step, trial, found)
                if score == (0, 0):
                    break
            layout, crossings, lifted = best[2], best[3], best[1] * pitch
            if best[1]:
                utils.logger.debug(
                    'Auto Tag: column lifted {0} row(s) above the drawn line '
                    '- {1} crossing(s) and {2} stub drop(s), from {3} and '
                    '{4}.'.format(best[1], best[0][0], best[0][1],
                                  audit_trail[0][1], audit_trail[0][2]))
        kinds = layout['kinds']
        height_targets = layout['height']
        drop_reach = layout['drop']
        order = layout['order']
        specs = layout['specs']

        moves, new_heads = self._assemble_moves(
            tags, line, right, up, heads, bounds, height_targets)
        plan = self._build_leader_plan(
            specs, tags, view, new_heads, right, up, pipe_up, pipe_across,
            elements)
        context['leader_plan'] = plan

        # Record what was decided and drawn, and let the run grade itself.
        if config.AUTO_LOG_ENABLED:
            self._log_run(
                view, settings, pitch, column_across, line_top, outward,
                targets_below, tags, elements, kinds, pipe_up, pipe_across,
                reach, drop_reach, height_targets, order, new_heads, plan,
                right, up, audit_trail, layout.get('spans_v'),
                layout.get('spans_u'), layout.get('demoted'),
                layout.get('seat_trace'))
        return moves

    @staticmethod
    def _log_run(view, settings, pitch, column_across, line_top, outward,
                 targets_below, tags, elements, kinds, pipe_up, pipe_across,
                 reach, drop_reach, height_targets, order, new_heads, plan,
                 right, up, audit_trail=None, spans_v=None, spans_u=None,
                 demoted=None, seat_trace=None):
        """Write the diagnostic log for this run (never fatal)."""
        try:
            index_of = {}
            for index, tag in enumerate(tags):
                index_of[utils.element_id_value(tag.Id)] = index

            leaders = {}
            for entry in plan:
                # entries are (tag, elbow, arrow) or (tag, elbow, arrow, free)
                tag, elbow, arrow = entry[0], entry[1], entry[2]
                index = index_of.get(utils.element_id_value(tag.Id))
                head = new_heads.get(index) if index is not None else None
                if head is None:
                    continue
                leaders[index] = (
                    (utils.project(head, right), utils.project(head, up)),
                    (utils.project(elbow, right), utils.project(elbow, up)),
                    (utils.project(arrow, right), utils.project(arrow, up)))

            crossings = diagnostics.write_run(
                view, settings, pitch, column_across, line_top, outward,
                targets_below, tags, elements, kinds, pipe_up, pipe_across,
                reach, drop_reach, height_targets, order, leaders,
                audit_trail, spans_v, spans_u, demoted, seat_trace)
            if crossings > 0:
                utils.logger.warning(
                    'Auto Tag: {0} leader crossing(s) - see {1}'.format(
                        crossings, diagnostics.log_path()))
        except Exception as ex:
            utils.logger.debug('Auto Tag logging skipped: {0}'.format(ex))


class AutoTagPipes(_ClusterReferenceLine):
    """One selection, sorted automatically into horizontals and risers.

    The tool reads each pipe's direction: horizontals get AT H/L / AT L/L by
    height and that designation is WRITTEN into the pipe's Comments, which one
    ordinary pipe tag (Size + System Abbreviation + Comments) shows - so there
    is no tag-type switching. Risers carry no designation (the flow prompt that
    supplied it was removed 2026-09-01) but are still laid out as their own
    block: the two families sit as two blocks on the one reference line,
    horizontals above, risers below.
    """
    name = 'Auto Tag Pipes'
    description = ('One selection: sorts horizontals (H/L / L/L by height) from '
                   'risers and writes each pipe\'s Comments.')
    edge = EDGE_LOW
    writes_comments = True

    def compute_moves(self, tags, view, context):
        geometry = self._gather_geometry(tags, view, context)
        if geometry is None:
            return []
        # Risers-as-points only make sense looking straight down; in a
        # section fall back to the single-mode dispatch.
        if not geometry['plan_view']:
            return self._dispatch(tags, view, context, geometry)
        return self._auto_moves(tags, view, context, geometry)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
# Registration order is the order the user sees in the method picker. Further
# methods (Smart MEP Alignment, collision-aware arrangement, ...) subclass
# AlignmentStrategy and are appended here. Nothing else changes.
ALIGNMENT_STRATEGIES = OrderedDict()

# Auto Tag Pipes is the whole live menu (user decision 2026-08-04). The
# Cluster Left/Right and Cluster Risers by Flow methods were retired with it:
# the first two tagged any MEP category generically with no designations, and
# the third wrote F/B / T/A into a tag parameter using a separate riser tag
# family - the approach Auto Tag Pipes replaced by writing Comments. Earlier
# strategies (stack, axis alignment, distribution) live in archive.py.
for _strategy_class in (
        AutoTagPipes,
):
    ALIGNMENT_STRATEGIES[_strategy_class.name] = _strategy_class()


def available_methods():
    """Return the registered alignment method names, in display order."""
    return list(ALIGNMENT_STRATEGIES.keys())


def get_strategy(name):
    """Return the strategy registered under `name`, or None."""
    return ALIGNMENT_STRATEGIES.get(name)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
def align_tags(tags, view, method_name, context=None):
    """Move tag heads according to the chosen alignment method.

    Must be called inside an open transaction - the strategies briefly suppress
    the leaders to measure the tags.

    Args:
        tags (list): IndependentTag objects to align.
        view: The active view.
        method_name (str): A key of ALIGNMENT_STRATEGIES.
        context (dict): 'doc', plus anything the strategy asked for (e.g. the
            picked reference line).

    Returns:
        tuple: (moved, failures) where failures is a list of (tag_id, message).

    Raises:
        ValueError: If the method name is not registered.
    """
    strategy = get_strategy(method_name)
    if strategy is None:
        raise ValueError('Unknown alignment method: {}'.format(method_name))

    moves = strategy.compute_moves(tags, view, context or {})
    moved = 0
    failures = []

    for tag, position in moves:
        tag_id = utils.element_id_value(tag.Id)
        try:
            tag.TagHeadPosition = position
            moved += 1
        except Exception as ex:
            failures.append((tag_id, str(ex)))
            utils.logger.error('Moving tag {} failed: {}'.format(tag_id, ex))

    utils.logger.debug('{} aligned {} tag head(s).'.format(method_name, moved))
    return moved, failures
