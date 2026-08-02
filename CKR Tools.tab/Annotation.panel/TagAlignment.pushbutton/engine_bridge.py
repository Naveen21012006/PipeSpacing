# -*- coding: utf-8 -*-
"""Bridge to the Align Tags placement engine, for the Auto method's horizontals.

Goal (docs/autotag-align-handoff.md): tags Auto Tag places come out formatted
EXACTLY like Align Tags at angle 0 with the user's approved defaults, so a stack
made by either tool is indistinguishable.

Per the handoff's prime directive we IMPORT the tested implementation rather than
re-derive it: the sibling ``AlignTags.pushbutton`` provides ``engine``
(layout) and ``clusters`` (physical-bundle grouping), both pure Python with no
Revit imports, and the shared settings file drives spacing. Two things cannot be
imported and are reproduced here with the handoff cited:

  * The per-tag corner offsets (handoff s4). The tested ``ordered_offsets`` /
    ``ordered_plan`` live in Align Tags' ``script.py``, which cannot be imported
    from here - its module name collides with this bundle's own ``script.py``
    and it binds ``doc = revit.doc`` at import. Its offset FORMULA is
    reproduced in ``_offsets`` / ``_offsets_with_fallback`` below, working off
    Auto Tag's own leader-collapsed measurement (the same technique Align Tags'
    ``measure_layout`` uses), median-guarded exactly as ``ordered_plan`` does.
  * Reading the shared settings file - done directly (the handoff sanctions
    "plain json").

The angle is pinned to 0.0 permanently (handoff s1, user decision 2026-08-02):
Auto Tag never reads ``angle_deg`` and always passes 0.0 to ``plan_ordered``.

Only HORIZONTAL pipes go through the engine (angle 0 gives the true 90-degree
L-leaders). Risers are point-like in a plan and the engine has no drop-onto-a-
point mode, so the Auto method keeps its own riser drops.

Everything degrades safely: if the engine can't be imported or a call fails,
``place_horizontals`` returns None and the caller uses its own layout.
Coordinate convention matches the engine: 2D ``(u, v)`` with ``u`` the view's
right axis and ``v`` up - the projections alignment.py already uses. Distances
are model feet.
"""

import json
import os
import sys

import utils

# --- import the sibling engine + clusters, defensively --------------------
_BRIDGE_DIR = os.path.dirname(__file__)
_ALIGN_DIR = os.path.join(os.path.dirname(_BRIDGE_DIR), 'AlignTags.pushbutton')

AVAILABLE = False
engine = None
clusters = None
arrange = None
common = None
try:
    if os.path.isdir(_ALIGN_DIR):
        if _ALIGN_DIR not in sys.path:
            sys.path.append(_ALIGN_DIR)
        import engine as _align_engine        # pure Python, no Revit imports
        engine = _align_engine
        try:
            import clusters as _align_clusters
            clusters = _align_clusters
        except Exception:
            clusters = None
        try:
            import arrange as _align_arrange   # cross-cluster overlap solver
            arrange = _align_arrange
        except Exception:
            arrange = None
        try:
            import common as _align_common     # for CKR_DIR (Revit-side)
            common = _align_common
        except Exception:
            common = None
        AVAILABLE = True
except Exception as ex:  # pragma: no cover - import-time guard
    utils.logger.debug('Align Tags engine unavailable: {}'.format(ex))
    AVAILABLE = False


# The user's approved defaults (handoff s1), used only when the shared settings
# file has no value for a key. angle_deg is deliberately absent: pinned to 0.
_FALLBACK_SETTINGS = {
    'landing_mm': 1524.0,     # horizontal run from text before a bend
    'vertical_mm': 100.0,     # CLEAR GAP between texts (pitch = text height + this)
    'horizontal_mm': 50.0,    # intermittent column offset (inert here)
    'cluster_mm': 1500.0,     # max arrow separation along a bundle; 0 = never split
    'learned_left_mm': 0.0,   # taught head->text-left distance; 0 = never taught
    'mode': 'LL',             # supplies Upper/Lower only; Left/Right is geometric
    'attached_end': True,     # free attached leader ends and PIN them (s1/s3):
                              # the engine's 'end' re-places the arrow exactly
}

# Handoff s3: arrow clearance and the bundle rack width are fixed constants in
# Align Tags (FITTING_CLEARANCE_MM, BUNDLE_LATERAL_MM).
_CLEARANCE_MM = 250.0
_BUNDLE_LATERAL_MM = 600.0
_MAX_CLUSTER_TAGS = 10
_CLUSTER_SPAN_CAP_MM = 4000.0

# Kept small: a fitting with no curve still needs a non-zero extent to clamp on.
_TINY_SPAN = utils.mm_to_feet(1.0)


def _settings_path():
    """Return the shared settings file path (%APPDATA%/CKR/...)."""
    if common is not None:
        try:
            return os.path.join(common.CKR_DIR, 'tag_align_settings.json')
        except Exception:
            pass
    base = os.environ.get('APPDATA') or os.environ.get('LOCALAPPDATA') or ''
    return os.path.join(base, 'CKR', 'tag_align_settings.json')


def load_settings():
    """Return the shared dialog settings, the file's values over the defaults.

    Reads %APPDATA%/CKR/tag_align_settings.json directly so it also sees keys
    Align Tags' settings.load() drops (e.g. learned_left_mm). angle_deg is never
    read - the caller always passes 0.0 (handoff s1). Independent of the engine
    import, so the pitch still follows the shared file even in fallback mode.
    """
    values = dict(_FALLBACK_SETTINGS)
    try:
        with open(_settings_path(), 'r') as handle:
            raw = json.load(handle)
        if isinstance(raw, dict):
            for key in _FALLBACK_SETTINGS:
                if key in raw:
                    values[key] = raw[key]
    except Exception:
        pass  # missing / corrupt -> the approved defaults
    return values


def row_pitch(bounds, view, settings):
    """Return the centre-to-centre row pitch (model feet), per handoff s3.

    Pitch = tallest DRAWN text height + vertical_mm CLEAR GAP. vertical_mm is a
    model distance (Align Tags uses mm_to_feet, not paper-scaled), added to the
    measured text height (also model). When nothing could be measured, a
    multi-line height estimate stands in so the rows never overlap.
    """
    gap = utils.mm_to_feet(float(settings.get('vertical_mm', 100.0)))
    heights = []
    for spans in bounds.values():
        span_v = spans[1] if spans else None
        if span_v:
            heights.append(span_v[1] - span_v[0])
    text_height = max(heights) if heights else utils.paper_mm_to_model(view, 6.0)
    return text_height + gap


# --- per-tag corner offsets (formula from AlignTags.script.ordered_offsets) --
def _head_2d(tag, right, up):
    """Return the tag head projected onto the view axes: (head_u, head_v)."""
    head = tag.TagHeadPosition
    return utils.project(head, right), utils.project(head, up)


def _offsets(index, tags, bounds, right, up, mode):
    """(head_offset, line_offset, exit_edge) from the text's bottom-left corner.

    Mirrors AlignTags.script.ordered_offsets (script.py:483-521): the anchor
    corner is ALWAYS the text bottom-LEFT (both sides); head_offset is the head
    relative to it; line_offset is the text mid-height where Revit attaches the
    leader; exit_edge is the full text width when leaders exit right, else 0.
    Returns None when this tag could not be measured (caller uses the median).
    """
    spans = bounds.get(index)
    if not spans:
        return None
    span_u, span_v = spans
    if span_u is None or span_v is None:
        return None
    u_lo, u_hi = span_u
    v_lo, v_hi = span_v
    head_u, head_v = _head_2d(tags[index], right, up)
    exit_edge = (u_hi - u_lo) if engine.exit_sign(mode) > 0 else 0.0
    return ((head_u - u_lo, head_v - v_lo), (v_hi - v_lo) / 2.0, exit_edge)


def _median(values):
    """Lower-middle median (matches AlignTags._median: a stale box only grows)."""
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def _offsets_with_fallback(indices, tags, bounds, right, up, mode, settings):
    """Per-index offsets, unmeasured tags borrowing the median, outliers snapped.

    Mirrors AlignTags.script.ordered_plan's robustness (script.py:661-716): one
    bad bounding box cannot knock a tag out of the column, and a width well
    above the cluster median is a leader-contaminated box, not a wide tag, so it
    snaps to the median. When NOTHING measured, the learned head->left distance
    (handoff s4.4) stands in for the head offset, logged.
    """
    measured = dict((i, _offsets(i, tags, bounds, right, up, mode))
                    for i in indices)
    good = [m for m in measured.values() if m is not None]
    if good:
        fallback = ((_median([m[0][0] for m in good]),
                     _median([m[0][1] for m in good])),
                    _median([m[1] for m in good]),
                    _median([m[2] for m in good]))
    else:
        learned = utils.mm_to_feet(float(settings.get('learned_left_mm', 0.0)))
        fallback = ((learned, 0.0), 0.0,
                    2.0 * learned if engine.exit_sign(mode) > 0 else 0.0)
        utils.logger.debug(
            'No tag measurable; falling back to learned_left offsets.')

    median_du = fallback[0][0]
    median_edge = fallback[2]
    resolved = {}
    for index in indices:
        head_offset, line_offset, exit_edge = measured[index] or fallback
        if median_du > 0.0 and head_offset[0] > 1.4 * median_du:
            head_offset = (median_du, head_offset[1])
        if median_edge > 0.0 and exit_edge > 1.4 * median_edge:
            exit_edge = median_edge
        resolved[index] = (head_offset, line_offset, exit_edge)
    return resolved


def _bundle_axis(indices, elements, right, up):
    """'h' or 'v': the dominant run direction of these pipes in view."""
    horizontal = 0
    for index in indices:
        direction = utils.get_element_direction(elements[index])
        if direction is None:
            continue
        if abs(utils.project(direction, right)) >= abs(utils.project(direction, up)):
            horizontal += 1
    return 'h' if horizontal * 2 >= len(indices) else 'v'


def _mode(indices, pipe_across, column_u, settings):
    """Quadrant: Upper/Lower from the dialog mode, Left/Right from geometry.

    Handoff s1/s2: the side is geometric (column u vs the pipes' mean u, as
    AlignTags.script.side_mode does); the dialog contributes only Upper vs
    Lower; switch_side is deliberately ignored in ordered layouts.
    """
    mean_u = sum(pipe_across[i] for i in indices) / float(len(indices))
    stack_right = column_u >= mean_u
    try:
        configured = engine.resolve_mode(settings.get('mode', 'LL'), False)
    except Exception:
        configured = engine.LOWER_LEFT
    upper = configured in (engine.UPPER_LEFT, engine.UPPER_RIGHT)
    if upper:
        return engine.UPPER_RIGHT if stack_right else engine.UPPER_LEFT
    return engine.LOWER_RIGHT if stack_right else engine.LOWER_LEFT


def _lift(reference, u, v, right, up):
    """Return a 3D point at (u, v) in the view plane, at reference's depth."""
    point = utils.shift(reference, right, u - utils.project(reference, right))
    point = utils.shift(point, up, v - utils.project(point, up))
    return point


def _endpoints_2d(element, right, up, midpoint_uv):
    """Return the element curve's two endpoints as (u, v), or a tiny point pair."""
    try:
        curve = element.Location.Curve
        a = curve.GetEndPoint(0)
        b = curve.GetEndPoint(1)
        return ((utils.project(a, right), utils.project(a, up)),
                (utils.project(b, right), utils.project(b, up)))
    except Exception:
        u, v = midpoint_uv
        return ((u, v), (u, v))


def _cluster(indices, elements, right, up, pipe_across, pipe_up, settings):
    """Split the horizontals into physical bundles (AlignTags.clusters).

    Returns a list of index sublists (global indices, input order preserved).
    Falls back to one bundle if clusters is unavailable, the split fails, or
    cluster_mm is 0 (never split).
    """
    cluster_mm = float(settings.get('cluster_mm', 1500.0))
    if clusters is None or cluster_mm <= 0.0 or len(indices) < 2:
        return [list(indices)]
    try:
        pipes = []
        arrows = []
        for index in indices:
            arrow = (pipe_across[index], pipe_up[index])
            pipes.append(_endpoints_2d(elements[index], right, up, arrow))
            arrows.append(arrow)
        groups = clusters.bundle_clusters(
            pipes, arrows,
            utils.mm_to_feet(_BUNDLE_LATERAL_MM),
            utils.mm_to_feet(cluster_mm),
            max_tags=_MAX_CLUSTER_TAGS)
        if not groups:
            return [list(indices)]
        return [[indices[pos] for pos in group] for group in groups]
    except Exception as ex:
        utils.logger.debug('Cluster split failed, one bundle: {}'.format(ex))
        return [list(indices)]


def cluster_by_target(indices, targets, settings):
    """Group tags whose TARGET POINTS chain within cluster_mm of each other.

    Used by the Auto method to keep each group of nearby pipes tagged beside
    itself instead of in one column spanning the whole floor. Chaining on the
    target point (rather than the pipe's direction) treats risers, flat runs
    and fittings alike, so nothing is left out of a cluster.

    Args:
        indices (list[int]): the tag indices to group.
        targets (list): (u, v) target point per tag index.
        settings (dict): shared settings; cluster_mm 0 disables grouping.

    Returns:
        list[list[int]]: index groups. One group holding everything when
        clustering is unavailable, disabled, or fails.
    """
    everything = [list(indices)]
    cluster_mm = float(settings.get('cluster_mm', 1500.0))
    if clusters is None or cluster_mm <= 0.0 or len(indices) < 2:
        return everything
    try:
        groups = clusters.chain_clusters(
            [targets[i] for i in indices],
            utils.mm_to_feet(cluster_mm),
            max_tags=_MAX_CLUSTER_TAGS,
            max_span=utils.mm_to_feet(_CLUSTER_SPAN_CAP_MM))
        if not groups:
            return everything
        return [[indices[position] for position in group] for group in groups]
    except Exception as ex:
        utils.logger.debug('Target clustering failed, one group: {0}'.format(ex))
        return everything


def _datum_column(group, pipe_across, stack_right, landing, widest_text):
    """The datum column u for one bundle, per handoff s2.

    The text block sits one landing clear of the bundle's nearest pipe:
      stack LEFT of pipes (leaders exit right):
          datum = leftmost_pipe_u - landing - widest_text_width
      stack RIGHT of pipes (leaders exit left):
          datum = rightmost_pipe_u + landing
    The datum is ALWAYS the text's bottom-LEFT corner, both sides.
    """
    if stack_right:
        return max(pipe_across[i] for i in group) + landing
    return min(pipe_across[i] for i in group) - landing - widest_text


def _widest_text(group, bounds, settings):
    """The widest measured text width in the group (feet), for the datum.

    Falls back to the learned head->left distance doubled (the head sits at
    the text centre on this family), else zero - a slightly-close stack is
    recoverable, a crash is not.
    """
    widths = []
    for index in group:
        spans = bounds.get(index)
        span_u = spans[0] if spans else None
        if span_u:
            widths.append(span_u[1] - span_u[0])
    if widths:
        return max(widths)
    learned = float(settings.get('learned_left_mm', 0.0))
    return utils.mm_to_feet(2.0 * learned) if learned > 0.0 else 0.0


def _group_anchor_v(group, pipe_up, pitch):
    """The bundle's own vertical anchor: stack centred on its pipes.

    This is what the Align Tags per-cluster CLICK does - the user clicks
    beside the bundle they are tagging - so leaders stay one landing long
    instead of climbing across the sheet. The anchor is the LOWEST row's
    corner, so centre minus half the stack height.
    """
    mean_v = sum(pipe_up[i] for i in group) / float(len(group))
    return mean_v - (len(group) - 1) * pitch / 2.0


def _plan_group(group, tags, elements, view, right, up, pipe_across, pipe_up,
                bounds, pitch, anchor_v, line_u, settings, bundle):
    """Plan one bundle at its own anchor: returns (entries, state).

    `entries` are the engine's per-tag dicts; `state` carries everything needed
    to re-plan this bundle at a new anchor (arrange / handoff s4.3).
    """
    landing = utils.mm_to_feet(float(settings.get('landing_mm', 1524.0)))
    horizontal = utils.mm_to_feet(float(settings.get('horizontal_mm', 50.0)))
    clearance = utils.mm_to_feet(_CLEARANCE_MM)
    gap = utils.mm_to_feet(float(settings.get('vertical_mm', 100.0)))

    # Side: geometric - the reference line says which side of the pipes the
    # user wants the stacks; the datum column itself derives from the pipes
    # (handoff s2), not from the line's own u.
    mean_u = sum(pipe_across[i] for i in group) / float(len(group))
    stack_right = line_u >= mean_u
    mode = _mode(group, pipe_across, line_u, settings)
    offsets = _offsets_with_fallback(group, tags, bounds, right, up, mode,
                                     settings)
    widest = _widest_text(group, bounds, settings)
    datum_u = _datum_column(group, pipe_across, stack_right, landing, widest)

    items = []
    for index in group:
        # Each item is built on the ELEMENT'S OWN axis, not the group's
        # (mirrors AlignTags.script.build_ordered_bundle, script.py:456-469):
        # the engine's angle-0 branches interpret pos/span in the item's own
        # frame, and item['own'] tells them which shape to draw. Within a
        # bundle_clusters group all pipes are parallel so own == bundle; the
        # own axis matters when clustering fell back to one mixed group.
        direction = utils.get_element_direction(elements[index])
        if direction is None:
            own = bundle
        elif abs(utils.project(direction, right)) >= abs(
                utils.project(direction, up)):
            own = 'h'
        else:
            own = 'v'
        if own == 'h':
            pos = pipe_up[index]
            span = utils.get_curve_span(elements[index], right)
            along = pipe_across[index]
        else:
            pos = pipe_across[index]
            span = utils.get_curve_span(elements[index], up)
            along = pipe_up[index]
        if span is None:
            span = (along - _TINY_SPAN, along + _TINY_SPAN)
        head_offset, line_offset, exit_edge = offsets[index]
        items.append({
            'key': index, 'pos': pos, 'span': span, 'own': own,
            'head_offset': head_offset,
            'line_offset': line_offset,
            'exit_edge': exit_edge,
        })

    anchor = (datum_u, anchor_v)
    entries = engine.plan_ordered(
        anchor, items, mode,
        0.0,            # angle pinned to 0 (handoff s1) - never from the file
        pitch, landing, horizontal, bundle,
        intermittent=False, switch_side=False,
        clearance=clearance)
    state = {
        'group': list(group), 'items': items, 'mode': mode, 'anchor': anchor,
        'bundle': bundle, 'pitch': pitch, 'landing': landing,
        'horizontal': horizontal, 'clearance': clearance, 'drawn': entries,
        'widest': widest, 'text_height': max(pitch - gap, 0.0),
    }
    return entries, state


def _stack_rect(state):
    """The text block's (lo_u, lo_v, hi_u, hi_v) for arrange.resolve."""
    anchor_u, anchor_v = state['anchor']
    rows = len(state['group'])
    return (anchor_u, anchor_v,
            anchor_u + max(state['widest'], _TINY_SPAN),
            anchor_v + (rows - 1) * state['pitch'] + state['text_height'])


def _stack_segments(state):
    """Every leader segment of a planned bundle, for arrange.resolve."""
    segments = []
    for entry in state['drawn']:
        try:
            segments.extend(engine.leader_segments(entry))
        except Exception:
            segments.append((entry['head'], entry['end']))
    return segments


def _replan_state(state, anchor):
    """Re-run the engine for one bundle at a new anchor, updating the state."""
    entries = engine.plan_ordered(
        anchor, state['items'], state['mode'],
        0.0,            # angle stays pinned (handoff s1)
        state['pitch'], state['landing'], state['horizontal'],
        state['bundle'],
        intermittent=False, switch_side=False,
        clearance=state['clearance'])
    state['anchor'] = anchor
    state['drawn'] = entries
    return entries


def _entries_to_moves(entries, tags, elements, view, right, up):
    """Lift one bundle's engine entries to 3D (moves, leader_plan)."""
    moves = []
    leader_plan = []
    for entry in entries:
        index = entry['key']
        tag = tags[index]
        head = _lift(tag.TagHeadPosition,
                     entry['head'][0], entry['head'][1], right, up)
        moves.append((tag, head))
        elbow = _lift(head, entry['elbow'][0], entry['elbow'][1], right, up)
        anchor_pt = utils.get_element_anchor(elements[index], view)
        base = anchor_pt if anchor_pt is not None else head
        arrow = _lift(base, entry['end'][0], entry['end'][1], right, up)
        leader_plan.append((tag, elbow, arrow))
    return moves, leader_plan


def place_horizontals(view, right, up, tags, elements, indices,
                      pipe_across, pipe_up, bounds, pitch, top_v, line_u,
                      settings):
    """Lay the horizontal pipe tags out via the Align Tags engine (angle 0).

    Bundles are split with AlignTags.clusters (physical pipe bundles) and each
    is planned by engine.plan_ordered with the per-tag corner offsets of
    handoff s4 - so stacks align text left edges, not heads.

    Each bundle's stack is anchored BESIDE ITS OWN PIPES - vertically centred
    on the bundle, one landing clear horizontally (handoff s2) - which is what
    the Align Tags per-cluster click does. It is NOT stacked in a global column
    under the reference line: that marched every later bundle's tags away from
    its pipes and drew leaders across the whole sheet. Overlaps between
    neighbouring stacks are then resolved with AlignTags.arrange.resolve
    (earlier bundle wins, later one yields), per the import-don't-reimplement
    directive.

    Args:
        view: active view (for element anchors).
        right, up: the view's unit axes.
        tags, elements: parallel lists; indices select the horizontals.
        indices (list[int]): the horizontal tag indices to place.
        pipe_across, pipe_up (list[float]): each pipe midpoint's u / v.
        bounds (dict): _measure_head_bounds output (per-tag text spans).
        pitch (float): row pitch from row_pitch() (model feet).
        top_v (float): top of the reference line (kept for API stability;
            the per-bundle anchors no longer derive from it).
        line_u (float): the reference line's u - decides WHICH SIDE of the
            pipes the stacks sit; the datum column itself derives from the
            pipes (handoff s2).
        settings (dict): shared dialog settings (load_settings()).

    Returns:
        (moves, plan, bottom_v, states) | None: moves is [(tag, head_xyz)];
        plan is [(tag, elbow_xyz, arrow_xyz)]; bottom_v is the lowest stack
        corner used; states re-plan each bundle for the verify-correct pass.
        None when the engine is unavailable or the call fails - the caller
        then uses its own horizontal layout.
    """
    if not AVAILABLE or not indices:
        return None
    try:
        groups = _cluster(indices, elements, right, up,
                          pipe_across, pipe_up, settings)

        # Plan every bundle at its own anchor, beside its own pipes. The
        # bundle axis is PER GROUP (a perpendicular cluster planned on the
        # majority's axis draws its leaders along its own pipes - confirmed
        # pre-flight finding).
        states = []
        for group in groups:
            if not group:
                continue
            bundle = _bundle_axis(group, elements, right, up)
            anchor_v = _group_anchor_v(group, pipe_up, pitch)
            _entries, state = _plan_group(
                group, tags, elements, view, right, up, pipe_across, pipe_up,
                bounds, pitch, anchor_v, line_u, settings, bundle)
            states.append(state)
        if not states:
            return None

        # Cross-bundle overlap resolution (AlignTags.arrange): text blocks and
        # leaders of different stacks keep a fitting-clearance margin apart;
        # the earlier-placed bundle wins, the later one yields and re-plans.
        if arrange is not None and len(states) > 1:
            def _replan(index, new_anchor):
                _replan_state(states[index], new_anchor)
                return {'rect': _stack_rect(states[index]),
                        'segments': _stack_segments(states[index])}
            arrange_states = [
                {'anchor': state['anchor'], 'rect': _stack_rect(state),
                 'segments': _stack_segments(state), 'movable': True}
                for state in states]
            try:
                _final, _moved, remaining = arrange.resolve(
                    arrange_states, _replan, utils.mm_to_feet(_CLEARANCE_MM))
                if remaining:
                    utils.logger.debug(
                        '{} stack conflict(s) unresolved.'.format(remaining))
            except Exception as ex:
                utils.logger.debug('arrange.resolve failed: {}'.format(ex))

        moves = []
        leader_plan = []
        for state in states:
            group_moves, group_plan = _entries_to_moves(
                state['drawn'], tags, elements, view, right, up)
            moves.extend(group_moves)
            leader_plan.extend(group_plan)

        bottom_v = min(state['anchor'][1] for state in states)
        return moves, leader_plan, bottom_v, states
    except Exception as ex:
        utils.logger.debug(
            'Align Tags engine placement failed, using fallback: {}'.format(ex))
        return None


# --- verify and correct (handoff s4.3): measure the DRAWN corner, re-plan ----
_CORRECT_MIN_MM = 10.0     # drawn-corner misses below this are left alone
_CORRECT_MAX_MM = 5000.0   # and above this the box is broken, not the plan


def _drawn_elbow_v(tag, up, planned_v):
    """The v of the elbow Revit actually holds, else the planned one.

    Mirrors AlignTags.script's read-the-drawn-elbow rule: the planned height is
    wrong exactly when the elbow write failed, so trusting it would make the
    correction chase a phantom landing. Revit API access is wrapped - under
    plain CPython (tests) this simply returns planned_v.
    """
    try:
        references = list(tag.GetTaggedReferences())
        if references:
            point = tag.GetLeaderElbow(references[0])
            if point is not None:
                return utils.project(point, up)
    except Exception:
        pass
    return planned_v


def _drawn_residual(state, tags, view, right, up, settings):
    """(du, dv) of the drawn bottom-left corner vs the anchor, or None.

    Mirrors AlignTags.script.correction_from_spans (script.py:598-650): measure
    AFTER placement, in the leader state the tag is actually drawn in. The box
    is only clean where the leader is not: u reads the left edge only when
    leaders exit RIGHT (exit-left derives the edge from head - learned_left_mm);
    v reads the bottom edge when the leader rises, else 2*mid - top. Leader
    direction comes from the DRAWN plan, never from the mode.
    """
    entries = state.get('drawn') or []
    lowest = None
    for entry in entries:
        if lowest is None or entry['head'][1] < lowest['head'][1]:
            lowest = entry
    if lowest is None:
        return None
    tag = tags[lowest['key']]
    anchor_u, anchor_v = state['anchor']

    u_span = utils.project_bounds(tag, view, right)
    v_span = utils.project_bounds(tag, view, up)

    du = dv = 0.0
    if engine.exit_sign(state['mode']) > 0.0:
        if u_span is not None:
            du = u_span[0] - anchor_u
    else:
        learned = float(settings.get('learned_left_mm', 0.0))
        if learned > 0.0:
            head_u = utils.project(tag.TagHeadPosition, right)
            du = (head_u - utils.mm_to_feet(learned)) - anchor_u
        elif u_span is not None and state.get('widest', 0.0) > 0.0:
            # Cold start (never taught): the box's RIGHT edge is leader-free
            # in exit-left mode, so estimate the text-left edge from it minus
            # the measured text width (handoff s4.4's width-estimate fallback).
            du = (u_span[1] - state['widest']) - anchor_u
            utils.logger.debug(
                'Exit-left correction using width estimate (learned_left 0).')
    if v_span is not None:
        # The drawn leader rises when its arrow sits above its landing. For a
        # descending leader the bottom edge is leader, so it derives from the
        # elbow Revit ACTUALLY holds (falling back to the planned one) - the
        # planned height is wrong exactly when the elbow write failed.
        rises = lowest['end'][1] >= lowest['elbow'][1]
        if rises:
            dv = v_span[0] - anchor_v
        else:
            elbow_v = _drawn_elbow_v(tag, up, lowest['elbow'][1])
            dv = (2.0 * elbow_v - v_span[1]) - anchor_v

    floor = utils.mm_to_feet(_CORRECT_MIN_MM)
    cap = utils.mm_to_feet(_CORRECT_MAX_MM)
    if abs(du) > cap or abs(dv) > cap:
        return None                     # silly numbers: broken box
    if abs(du) <= floor:
        du = 0.0
    if abs(dv) <= floor:
        dv = 0.0
    if du == 0.0 and dv == 0.0:
        return None
    return (du, dv)


def correct_placement(states, tags, elements, view, right, up, settings):
    """One verify-correct pass over the placed bundles (handoff s4.3).

    Called AFTER the first placement is applied and regenerated, inside the
    same transaction group. Measures each bundle's drawn bottom-left corner
    against its anchor and re-plans the bundle once with the residual
    subtracted. Returns (moves, plan) - empty lists when everything already
    sits within tolerance, so re-running over clean output shifts nothing
    (handoff s5.5).
    """
    moves = []
    leader_plan = []
    if not AVAILABLE:
        return moves, leader_plan
    for state in states:
        try:
            residual = _drawn_residual(state, tags, view, right, up, settings)
            if residual is None:
                continue
            anchor = (state['anchor'][0] - residual[0],
                      state['anchor'][1] - residual[1])
            entries = engine.plan_ordered(
                anchor, state['items'], state['mode'],
                0.0,        # angle stays pinned (handoff s1)
                state['pitch'], state['landing'], state['horizontal'],
                state['bundle'],
                intermittent=False, switch_side=False,
                clearance=state['clearance'])
            state['anchor'] = anchor
            state['drawn'] = entries
            group_moves, group_plan = _entries_to_moves(
                entries, tags, elements, view, right, up)
            moves.extend(group_moves)
            leader_plan.extend(group_plan)
        except Exception as ex:
            utils.logger.debug('Verify-correct pass skipped: {}'.format(ex))
    return moves, leader_plan
