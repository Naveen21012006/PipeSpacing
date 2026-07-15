# -*- coding: utf-8 -*-
"""Archived tag-alignment strategies - NOT part of the live menu.

These strategies (edge alignment, distribution, plain/stack column-on-a-line)
were superseded by the Cluster-on-Reference-Line methods. Nothing imports this
module, so the code is inert - it is kept only for reference and re-use.

To bring one back: move its class into alignment.py (or import it from here)
and add it to ALIGNMENT_STRATEGIES in alignment.py. The shared helpers still
live in alignment.py and are imported below.
"""

import config
import utils

from alignment import (
    AlignmentStrategy,
    EDGE_LOW,
    EDGE_HIGH,
    EDGE_CENTER,
    _anchor,
    _measure_head_bounds,
    _readable_pitch,
    _tag_element,
    _reference_coordinate_at,
)


# ---------------------------------------------------------------------------
# Ordering / spreading helpers (used only by the archived strategies)
# ---------------------------------------------------------------------------
def _element_order_keys(tags, view, doc):
    """Return each tag's element position across the view (left-to-right).

    Used to order a stacked column by the ELEMENTS rather than by wherever the
    tags currently sit. Returns None if any element cannot be located, so the
    caller falls back to the tags' own order rather than a partial ordering.
    """
    right, _up = utils.get_view_axes(view)

    keys = []
    for tag in tags:
        element = _tag_element(tag, doc)
        if element is None:
            return None
        anchor = utils.get_element_anchor(element, view)
        if anchor is None:
            return None
        keys.append(utils.project(anchor, right))
    return keys


def _column_order(tags, view, context, height_coords):
    """Return tag indices in top-to-bottom column order.

    Left-most element first when element ordering is on and every element can
    be located; otherwise the currently top-most tag first, which preserves
    the visual order.
    """
    if config.ORDER_STACK_BY_ELEMENT:
        keys = _element_order_keys(tags, view, context.get('doc'))
        if keys is not None:
            # Left-most element (smallest key) first -> sits at the top.
            return sorted(range(len(tags)), key=lambda index: keys[index])
    # Top-most tag (largest up-coordinate) first.
    return sorted(range(len(tags)), key=lambda index: height_coords[index],
                  reverse=True)


def _ordering(tags, view, context, is_across):
    """Return (order_keys, reverse_order) for a spread, honouring the config.

    When ORDER_STACK_BY_ELEMENT is on and every element can be located, the
    column is ordered left-to-right by element. For a vertical column the
    sequence is reversed so the left-most element - laid out last, from the
    bottom up - ends up on TOP. Otherwise the tags keep their own order
    (order_keys None, reverse_order False) - the original behaviour.
    """
    order_keys = None
    if config.ORDER_STACK_BY_ELEMENT:
        order_keys = _element_order_keys(tags, view, context.get('doc'))
    reverse_order = order_keys is not None and not is_across
    return order_keys, reverse_order


def _spread_targets(coords, pitch, order_keys=None, reverse_order=False):
    """Return {index: target coordinate} spreading coordinates evenly.

    Tags step off the LOW end of the axis by an equal pitch: the natural spread
    when that is already wide enough to read, and `pitch` when it is not - so
    tags never collide. The column is always anchored at the low end (its
    original behaviour); ordering never moves that anchor.

    order_keys (one value per tag) decides the sequence tags are laid out in;
    without it they keep their own order along the axis. `reverse_order` lays
    that sequence out high-key-first, used for a vertical column so the
    left-most element (smallest key), placed last, sits on top.
    """
    count = len(coords)
    low = min(coords)
    high = max(coords)

    step = pitch
    if count > 1:
        step = max((high - low) / float(count - 1), pitch)

    order = _sorted_indices(order_keys, coords, reverse_order)
    high = low + step * (count - 1) if count > 1 else low
    return _even_targets(low, high, order)


def _sorted_indices(order_keys, coords, reverse_order):
    """Return tag indices in lay-out order (by element, else by position)."""
    keys = order_keys if order_keys is not None else coords
    return sorted(range(len(keys)), key=lambda index: keys[index],
                  reverse=reverse_order)


def _even_targets(low, high, order):
    """Place the given indices evenly between low and high, in order.

    order[0] lands on `low`, order[-1] on `high`; a single item is centred.
    """
    count = len(order)
    if count == 1:
        return {order[0]: (low + high) / 2.0}

    step = (high - low) / float(count - 1)
    return {index: low + position * step
            for position, index in enumerate(order)}


def _average(values):
    """Mean of a sequence (used by the Center / Middle strategies)."""
    return sum(values) / float(len(values))


# ---------------------------------------------------------------------------
# Align: collapse one edge of every tag onto a single coordinate
# ---------------------------------------------------------------------------
class _AxisAlignment(AlignmentStrategy):
    """Lines one edge of every tag up on a single coordinate.

    Subclasses set:
        axis - 'u' (view right) or 'v' (view up)
        edge - which edge of the tag is the anchor (EDGE_LOW/HIGH/CENTER)
        pick - reduces the tags' anchor coordinates to the target
    """

    axis = 'u'
    edge = EDGE_LOW
    pick = staticmethod(min)

    def compute_moves(self, tags, view, context):
        if not tags:
            return []

        right, up = utils.get_view_axes(view)
        is_across = self.axis == 'u'
        axis_vector = right if is_across else up
        axis_index = 0 if is_across else 1

        heads = [tag.TagHeadPosition for tag in tags]
        head_coords = [utils.project(head, axis_vector) for head in heads]
        bounds = _measure_head_bounds(tags, view, context.get('doc'))

        anchors = []
        for index in range(len(tags)):
            spans = bounds.get(index)
            span = spans[axis_index] if spans else None
            anchors.append(_anchor(span, head_coords[index], self.edge))

        target = self.pick(anchors)

        moves = []
        for index, tag in enumerate(tags):
            delta = target - anchors[index]
            if abs(delta) < config.POSITION_TOLERANCE:
                continue  # Already aligned.
            moves.append((tag, utils.shift(heads[index], axis_vector, delta)))
        return moves


class LeftAlignment(_AxisAlignment):
    name = 'Left'
    description = 'Line the tags up on their left edges.'
    axis = 'u'
    edge = EDGE_LOW
    pick = staticmethod(min)


class RightAlignment(_AxisAlignment):
    name = 'Right'
    description = 'Line the tags up on their right edges.'
    axis = 'u'
    edge = EDGE_HIGH
    pick = staticmethod(max)


class TopAlignment(_AxisAlignment):
    name = 'Top'
    description = 'Line the tags up on their top edges.'
    axis = 'v'
    edge = EDGE_HIGH
    pick = staticmethod(max)


class BottomAlignment(_AxisAlignment):
    name = 'Bottom'
    description = 'Line the tags up on their bottom edges.'
    axis = 'v'
    edge = EDGE_LOW
    pick = staticmethod(min)


class CenterAlignment(_AxisAlignment):
    name = 'Center'
    description = 'Line the tag centres up on their average vertical axis.'
    axis = 'u'
    edge = EDGE_CENTER
    pick = staticmethod(_average)


class MiddleAlignment(_AxisAlignment):
    name = 'Middle'
    description = 'Line the tag centres up on their average horizontal axis.'
    axis = 'v'
    edge = EDGE_CENTER
    pick = staticmethod(_average)


# ---------------------------------------------------------------------------
# Distribute: spread the tags out evenly along one axis
# ---------------------------------------------------------------------------
class _AxisDistribution(AlignmentStrategy):
    """Spreads tag heads evenly along one view axis.

    The lowest tag stays put and the rest step off it by an equal pitch - the
    natural spread when it is already wide enough to read, otherwise a
    measured, readable one. Only the chosen axis is touched, so running this
    after Left keeps the heads in their column and merely evens the gaps.
    """

    axis = 'v'

    def compute_moves(self, tags, view, context):
        if len(tags) < 2:
            return []

        right, up = utils.get_view_axes(view)
        is_across = self.axis == 'u'
        axis_vector = right if is_across else up
        axis_index = 0 if is_across else 1

        heads = [tag.TagHeadPosition for tag in tags]
        coords = [utils.project(head, axis_vector) for head in heads]

        bounds = _measure_head_bounds(tags, view, context.get('doc'))
        pitch = _readable_pitch(bounds, view, axis_index)
        order_keys, reverse_order = _ordering(tags, view, context, is_across)
        targets = _spread_targets(coords, pitch, order_keys, reverse_order)

        moves = []
        for index, tag in enumerate(tags):
            delta = targets[index] - coords[index]
            if abs(delta) < config.POSITION_TOLERANCE:
                continue
            moves.append((tag, utils.shift(heads[index], axis_vector, delta)))
        return moves


class EqualVerticalSpacing(_AxisDistribution):
    name = 'Equal Vertical Spacing'
    description = 'Spread tags evenly up the view, without overlapping.'
    axis = 'v'


class EqualHorizontalSpacing(_AxisDistribution):
    name = 'Equal Horizontal Spacing'
    description = 'Spread tags evenly across the view, without overlapping.'
    axis = 'u'


# ---------------------------------------------------------------------------
# Stack: align onto a column AND space it out, in one run
# ---------------------------------------------------------------------------
class _StackStrategy(AlignmentStrategy):
    """Lines the tags up on a column AND spaces them so they can be read.

    What MEP annotation usually wants, and what the pure methods only do half
    of: a tidy column of tags, evenly spaced with no overlap, each leader
    squaring off to its own element.

    The column lands on the outermost existing tag edge, so dragging one tag to
    where you want the column and running this pulls the rest into line.
    """

    edge = EDGE_LOW
    pick = staticmethod(min)

    def compute_moves(self, tags, view, context):
        if not tags:
            return []

        right, up = utils.get_view_axes(view)
        heads = [tag.TagHeadPosition for tag in tags]
        bounds = _measure_head_bounds(tags, view, context.get('doc'))

        # 1. collapse the chosen edge onto one column
        across_coords = [utils.project(head, right) for head in heads]
        anchors = []
        for index in range(len(tags)):
            spans = bounds.get(index)
            span = spans[0] if spans else None
            anchors.append(_anchor(span, across_coords[index], self.edge))
        across_target = self.pick(anchors)

        # 2. spread down the column so the text stays readable, ordered
        #    left-to-right by element (left-most element on top).
        height_coords = [utils.project(head, up) for head in heads]
        pitch = _readable_pitch(bounds, view, 1)
        order_keys, reverse_order = _ordering(
            tags, view, context, is_across=False)
        height_targets = _spread_targets(
            height_coords, pitch, order_keys, reverse_order)

        moves = []
        for index, tag in enumerate(tags):
            delta_across = across_target - anchors[index]
            delta_height = height_targets[index] - height_coords[index]

            if (abs(delta_across) < config.POSITION_TOLERANCE
                    and abs(delta_height) < config.POSITION_TOLERANCE):
                continue

            new_head = utils.shift(heads[index], right, delta_across)
            new_head = utils.shift(new_head, up, delta_height)
            moves.append((tag, new_head))
        return moves


class StackLeft(_StackStrategy):
    name = 'Stack Left'
    description = 'Column of tags on their left edges, evenly spaced.'
    edge = EDGE_LOW
    pick = staticmethod(min)


class StackRight(_StackStrategy):
    name = 'Stack Right'
    description = 'Column of tags on their right edges, evenly spaced.'
    edge = EDGE_HIGH
    pick = staticmethod(max)



# ---------------------------------------------------------------------------
# Reference line: draw a line, tags land on it (plain + evenly stacked)
# ---------------------------------------------------------------------------
class _ReferenceLineStrategy(AlignmentStrategy):
    """Snaps a chosen tag edge onto a reference line the user picks.

    The deterministic option: rather than the tool guessing where the column
    belongs, you draw a line where you want it and the tags line up on it.
    `edge` decides whether their left or right edges touch the line; `spread`
    decides whether the heights are also evened out.
    """

    requires_reference_line = True
    edge = EDGE_LOW
    spread = False

    def compute_moves(self, tags, view, context):
        line = context.get('reference_line')
        if line is None or not tags:
            return []

        right, up = utils.get_view_axes(view)
        heads = [tag.TagHeadPosition for tag in tags]
        bounds = _measure_head_bounds(tags, view, context.get('doc'))

        # Heights: keep them, or stack from the TOP of the line downwards by a
        # fixed readable pitch. The line sets the horizontal alignment and
        # where the top tag sits; the pitch (not the line's length) sets the
        # gap, so a long guide line no longer means wide gaps. Ordered
        # left-to-right by element (left-most element on top).
        height_coords = [utils.project(head, up) for head in heads]
        if self.spread:
            top = max(utils.project(line.GetEndPoint(0), up),
                      utils.project(line.GetEndPoint(1), up))
            pitch = _readable_pitch(bounds, view, 1)
            order = _column_order(tags, view, context, height_coords)
            height_targets = {index: top - position * pitch
                              for position, index in enumerate(order)}
        else:
            height_targets = dict(enumerate(height_coords))

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
            delta_height = target_height - height_coords[index]

            if (abs(delta_across) < config.POSITION_TOLERANCE
                    and abs(delta_height) < config.POSITION_TOLERANCE):
                continue

            new_head = utils.shift(heads[index], right, delta_across)
            new_head = utils.shift(new_head, up, delta_height)
            moves.append((tag, new_head))
        return moves


class ReferenceLineLeft(_ReferenceLineStrategy):
    name = 'Left to Reference Line'
    description = 'Left edges on a line you pick; heights kept.'
    edge = EDGE_LOW
    spread = False


class ReferenceLineRight(_ReferenceLineStrategy):
    name = 'Right to Reference Line'
    description = 'Right edges on a line you pick; heights kept.'
    edge = EDGE_HIGH
    spread = False


class ReferenceLineCenter(_ReferenceLineStrategy):
    name = 'Center to Reference Line'
    description = 'Tag centres on a line you pick; heights kept.'
    edge = EDGE_CENTER
    spread = False


class ReferenceLineStackLeft(_ReferenceLineStrategy):
    name = 'Stack Left on Reference Line'
    description = 'Left edges on a line you pick, spaced evenly.'
    edge = EDGE_LOW
    spread = True


class ReferenceLineStackRight(_ReferenceLineStrategy):
    name = 'Stack Right on Reference Line'
    description = 'Right edges on a line you pick, spaced evenly.'
    edge = EDGE_HIGH
    spread = True


class ReferenceLineStackCenter(_ReferenceLineStrategy):
    name = 'Stack Center on Reference Line'
    description = 'Tag centres on a line you pick, spaced evenly.'
    edge = EDGE_CENTER
    spread = True
