# -*- coding: utf-8 -*-
"""Tag head alignment strategies.

Each alignment method is a small strategy class registered in
ALIGNMENT_STRATEGIES. Adding one means writing a class and registering it.

The live menu is the two Cluster-on-Reference-Line methods: one tag per run,
clustered on a line you draw and centred on each group's pipes. Earlier
strategies (edge alignment, distribution, plain/stack columns) have been
retired to archive.py, unregistered.

Principles shared by all of them:

* Everything is computed in the *view's* axes (RightDirection / UpDirection),
  not world X/Y, so "left" and "top" mean what the user sees whether the view
  is a plan, a section or an elevation.
* A tag is anchored by its measured text *edge*, not by TagHeadPosition (which
  sits at the text centre), so edges line up cleanly on the reference line.

Only tag heads are moved. MEP elements are never touched.
"""

from collections import OrderedDict

import config
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

    # Set True to run the two-pick riser flow (down risers, then up risers) so
    # each riser gets an F/B / T/A designation. Plain methods leave this False
    # and tag with a single ordinary selection.
    assigns_riser_flow = False

    # Set True for the Auto method: tag the WHOLE selection (the two picks only
    # set riser flow, they do not narrow the selection), write each pipe's
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
        new_heads holds the resulting head position for every tag (the
        L-leader mode needs them even when the tag did not move).
        """
        across_coords = [utils.project(head, right) for head in heads]
        moves = []
        new_heads = {}
        for index, tag in enumerate(tags):
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

    # -- Auto mode: horizontals and risers as two blocks on one line -------
    def _auto_moves(self, tags, view, context, g):
        """Lay a mixed selection out as two blocks on the one reference line.

        The horizontals (H/L, L/L) form the upper block, the risers (F/B, T/A)
        a separate block below it, so the two families read apart. Each block
        reuses its own layout: horizontals get the fanned L-leaders,
        risers the drops onto their points. Either block is skipped when empty,
        so an all-horizontal or all-riser selection degrades to a single block.
        """
        line = g['line']
        right, up = g['right'], g['up']
        heads, bounds, pitch = g['heads'], g['bounds'], g['pitch']
        pipe_up, pipe_across = g['pipe_up'], g['pipe_across']
        pointlike, elements, count = g['pointlike'], g['elements'], g['count']

        line_top = max(utils.project(line.GetEndPoint(0), up),
                       utils.project(line.GetEndPoint(1), up))
        block_gap = utils.paper_mm_to_model(view, config.AUTO_BLOCK_GAP_MM)

        # In a plan every non-point pipe is a flat pipe that earns an L-leader;
        # every point-like pipe is a riser. There is no third category here.
        risers = [i for i in range(count) if pointlike[i]]
        horizontals = [i for i in range(count) if not pointlike[i]]
        leadered_flags = [not pointlike[i] for i in range(count)]

        height_targets = {}
        specs = []
        top = line_top

        if horizontals:
            h_targets, h_specs, h_bottom = self._horizontal_block(
                line, right, up, view, pipe_up, pipe_across, leadered_flags,
                elements, pitch, horizontals, top)
            height_targets.update(h_targets)
            specs.extend(h_specs)
            top = h_bottom - pitch - block_gap

        if risers:
            r_targets, r_specs, _r_bottom = self._riser_block(
                line, right, up, pipe_up, pipe_across, pointlike, pitch,
                risers, top)
            height_targets.update(r_targets)
            specs.extend(r_specs)

        moves, new_heads = self._assemble_moves(
            tags, line, right, up, heads, bounds, height_targets)
        context['leader_plan'] = self._build_leader_plan(
            specs, tags, view, new_heads, right, up, pipe_up, pipe_across,
            elements)
        return moves


class ClusterReferenceLineLeft(_ClusterReferenceLine):
    name = 'Cluster Left on Reference Line'
    description = 'One tag per run; left edges on the line; clusters centred on their pipes.'
    edge = EDGE_LOW


class ClusterReferenceLineRight(_ClusterReferenceLine):
    name = 'Cluster Right on Reference Line'
    description = 'One tag per run; right edges on the line; clusters centred on their pipes.'
    edge = EDGE_HIGH


class ClusterRisersByFlow(_ClusterReferenceLine):
    """Same clustering, but the two-pick riser flow + F/B / T/A designations.

    Placement is identical to the Cluster methods (risers drop onto their
    points, nested); this method additionally has script.py take the two
    direction picks and write the designation onto each riser tag.
    """
    name = 'Cluster Risers by Flow'
    description = ('Risers only: pick the down risers, then the up risers; '
                   'each tag gets its F/B / T/A designation.')
    edge = EDGE_LOW
    assigns_riser_flow = True


class AutoTagPipes(_ClusterReferenceLine):
    """One selection, sorted automatically into horizontals and risers.

    The tool reads each pipe's direction: horizontals get AT H/L / AT L/L by
    height, risers get F/B / T/A by flow (the two picks) and run extent. Every
    pipe's designation is WRITTEN into its Comments, and one ordinary pipe tag
    (Size + System Abbreviation + Comments) shows it - so there is no tag-type
    switching. The two families lay out as two blocks on the one reference line
    (horizontals above, risers below).
    """
    name = 'Auto Tag Pipes'
    description = ('One selection: sorts horizontals (H/L / L/L by height) from '
                   'risers (F/B / T/A by flow) and writes each pipe\'s Comments.')
    edge = EDGE_LOW
    assigns_riser_flow = True
    writes_comments = True

    def compute_moves(self, tags, view, context):
        geometry = self._gather_geometry(tags, view, context)
        if geometry is None:
            return []
        # Risers-as-points only make sense looking straight down; in a section
        # fall back to the single-mode dispatch the Cluster methods use.
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

# One tag per connected same-size run, clustered on the reference line and
# centred on each group's pipes - the whole live menu. Every other strategy
# (Stack, axis alignment, distribution) has been retired to archive.py; import
# and register one here to bring it back.
for _strategy_class in (
        AutoTagPipes,
        ClusterReferenceLineLeft,
        ClusterReferenceLineRight,
        ClusterRisersByFlow,
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
