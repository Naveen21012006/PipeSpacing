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


def _readable_pitch(bounds, view, axis_index):
    """Return the smallest centre-to-centre gap that keeps tag text apart.

    Sized from the tags themselves, so long or wrapped text automatically
    claims more room. config.MIN_TAG_PITCH_MM is the floor when nothing could
    be measured. Without this, tags on pipes 50 mm apart end up 50 mm apart on
    screen - piled on top of each other and unreadable.
    """
    floor = utils.paper_mm_to_model(view, config.MIN_TAG_PITCH_MM)

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
    """Cluster one-tag-per-run tags on a reference line, centred on their pipes.

    Every tag is drawn towards the height of its own pipe, so its leader stays
    short. Where several tags would overlap they de-overlap into a tidy cluster
    centred on that group (see _declutter_blocks), ordered left-to-right with
    the left-most pipe on top. The reference line sets only the horizontal
    position; `edge` picks which tag edge lands on it.
    """

    requires_reference_line = True
    groups_runs = True
    edge = EDGE_LOW

    def compute_moves(self, tags, view, context):
        line = context.get('reference_line')
        if line is None or not tags:
            return []

        doc = context.get('doc')
        right, up = utils.get_view_axes(view)
        heads = [tag.TagHeadPosition for tag in tags]
        bounds = _measure_head_bounds(tags, view, doc)

        # Each tag is pulled toward its own pipe's height (target_up), and its
        # left-to-right position comes from the pipe's across-coordinate
        # (order_key).
        target_up = []
        order_key = []
        for index, tag in enumerate(tags):
            element = _tag_element(tag, doc)
            anchor = utils.get_element_anchor(element, view) if element else None
            if anchor is not None:
                target_up.append(utils.project(anchor, up))
                order_key.append(utils.project(anchor, right))
            else:
                target_up.append(utils.project(heads[index], up))
                order_key.append(utils.project(heads[index], right))

        # Tags whose pipe heights collide form a cluster (centred on the
        # group). WITHIN a cluster, order strictly left-to-right - the
        # left-most pipe on top - regardless of the small height differences
        # between the tagged segments.
        pitch = _readable_pitch(bounds, view, 1)
        by_height = sorted(range(len(tags)), key=lambda i: target_up[i])
        height_targets = {}
        for block in _declutter_blocks(by_height, target_up, pitch):
            top = block['centre'] + (block['n'] - 1) * pitch / 2.0
            left_to_right = sorted(block['members'], key=lambda i: order_key[i])
            for step, index in enumerate(left_to_right):
                height_targets[index] = top - step * pitch

        across_coords = [utils.project(head, right) for head in heads]
        moves = []
        for index, tag in enumerate(tags):
            spans = bounds.get(index)
            span = spans[0] if spans else None
            anchor = _anchor(span, across_coords[index], self.edge)

            target_height = height_targets[index]
            target_across = _reference_coordinate_at(
                line, right, up, target_height)

            delta_across = target_across - anchor
            delta_height = target_height - utils.project(heads[index], up)

            if (abs(delta_across) < config.POSITION_TOLERANCE
                    and abs(delta_height) < config.POSITION_TOLERANCE):
                continue

            new_head = utils.shift(heads[index], right, delta_across)
            new_head = utils.shift(new_head, up, delta_height)
            moves.append((tag, new_head))
        return moves


class ClusterReferenceLineLeft(_ClusterReferenceLine):
    name = 'Cluster Left on Reference Line'
    description = 'One tag per run; left edges on the line; clusters centred on their pipes.'
    edge = EDGE_LOW


class ClusterReferenceLineRight(_ClusterReferenceLine):
    name = 'Cluster Right on Reference Line'
    description = 'One tag per run; right edges on the line; clusters centred on their pipes.'
    edge = EDGE_HIGH


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
        ClusterReferenceLineLeft,
        ClusterReferenceLineRight,
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
