# -*- coding: utf-8 -*-
"""Leader rebuilding for the Annotation Dashboard.

The dashboard never moves a tag: each keeps its position and only its
leader is rebuilt, so the same shapes Align Tags produces can be applied
to a whole view - or to every new tag as it is placed.

Shapes (mirroring engine.py, chosen per tag from where it already sits):
  * straight  - element level with the text: one horizontal line;
  * L-bend    - element on a horizontal run: landing + true 90 degrees;
  * climb     - element on a vertical run: landing + TILT_DEG lean, so
                the leader is never drawn along the pipe.

Every document write happens in the caller's transaction. Failures are
counted and reported, never raised: one stubborn tag must not abort a
whole-view pass.
"""

import math

import common
import engine
import wrappers


def _pipe_direction(wrapper, basis):
    """'v', 'h', or None for the tagged element's run direction."""
    pair = wrapper.tagged_curve()
    if pair is None:
        return None
    a = common.to_2d(pair[0], basis)
    b = common.to_2d(pair[1], basis)
    if abs(b[0] - a[0]) < 1e-9 and abs(b[1] - a[1]) < 1e-9:
        return None
    return 'v' if abs(b[1] - a[1]) >= abs(b[0] - a[0]) else 'h'


def resolve_justification(setting, wrapper, basis):
    """Turn the justification setting into 'left'/'right', or None.

    Align Tags derives 'automatic' from the stack quadrant, but the
    dashboard has no stack: each tag is judged on its own, justified
    towards the element it points at, so the text hugs its leader.
    Returns None when nothing should be written - the wrapper treats any
    non-'left' value as Right, so None must never reach it.
    """
    if setting in ('left', 'right'):
        return setting
    if setting != 'automatic':
        return None
    try:
        _position, end3d = wrapper.primary_leader()
        if end3d is None:
            return None
        head = common.to_2d(wrapper.get_head(), basis)
        end = common.to_2d(end3d, basis)
    except Exception:
        return None
    return 'right' if end[0] >= head[0] else 'left'


def plan_leader(head, end, direction, straight, angle_deg, landing,
                elbow_gap):
    """Return the elbow point for one leader, or None to leave it alone.

    Pure geometry: the caller supplies 2D head/end and the settings; the
    result is the 2D elbow. ``elbow_gap`` keeps the bend away from the
    arrowhead so short leaders stay legible.
    """
    du = end[0] - head[0]
    dv = end[1] - head[1]
    if abs(du) < 1e-9 and abs(dv) < 1e-9:
        return None
    sign = 1.0 if du >= 0 else -1.0
    reach = abs(du)

    if not straight:
        angle = engine.clamp_angle(angle_deg)
        # Slanted mode: elbow at the landing, then the slant to the end.
        run = min(landing, max(reach - elbow_gap, 0.0))
        return (head[0] + sign * run, head[1])

    if abs(dv) <= 1e-9:
        return (head[0] + sign * reach / 2.0, head[1])   # already straight

    if direction == 'h':
        # True 90 degrees: land above/below the arrow, then drop.
        return (end[0], head[1])
    if direction == 'v':
        # Climb leaning TILT_DEG off vertical so the leader never rides
        # the pipe. No elbow-gap clamp here: the bend is already a full
        # rise away from the arrow, and clamping would break the lean.
        lean = abs(dv) * math.tan(math.radians(engine.TILT_DEG))
        return (head[0] + sign * max(reach - lean, 0.0), head[1])
    # Unknown direction (fitting, equipment): a plain landing reads best.
    run = min(landing, max(reach - elbow_gap, 0.0))
    return (head[0] + sign * run, head[1])


def apply_rules(targets, doc, basis, options):
    """Rebuild the leaders of every wrapper in ``targets``.

    Args:
        targets: wrappers (already filtered to leadered, unpinned tags).
        doc: the document (for Regenerate).
        basis: view basis from common.view_basis.
        options: dict with ``straight``, ``angle_deg``, ``landing``,
            ``elbow_gap`` (model units), ``attached_end``,
            ``justification``.

    Returns:
        (updated, failures)
    """
    updated = 0
    failures = 0
    justification = options.get('justification')

    for wrapper in targets:
        try:
            if justification:
                side = resolve_justification(justification, wrapper, basis)
                if side:
                    # Justifying can shift the arrowhead, so the leader is
                    # read AFTER it - never before.
                    wrapper.set_justification(side)
            position, end3d = wrapper.primary_leader()
            if end3d is None:
                continue
            keys = wrapper.leader_keys()
            if not keys:
                continue

            if options.get('attached_end') and wrapper.attached_end:
                if wrapper.make_free():
                    wrapper.set_end(keys[position or 0], end3d)

            head3d = wrapper.get_head()
            head = common.to_2d(head3d, basis)
            end = common.to_2d(end3d, basis)
            direction = _pipe_direction(wrapper, basis)
            elbow2d = plan_leader(
                head, end, direction, options['straight'],
                options['angle_deg'], options['landing'],
                options['elbow_gap'])
            if elbow2d is None:
                continue
            depth = common.depth_of(head3d, basis)
            target = common.to_3d(elbow2d, depth, basis)
            if wrapper.set_elbow(keys[position or 0], target):
                check = wrapper.get_elbow(keys[position or 0])
                if check is not None and check.DistanceTo(target) > 0.01:
                    wrapper.set_elbow(keys[position or 0], target)
                updated += 1
            else:
                failures += 1
        except Exception as ex:
            failures += 1
            common.logger.debug('Leader rule failed: {}'.format(ex))

    try:
        doc.Regenerate()
    except Exception:
        pass
    return updated, failures


def collect_visible(doc, view):
    """Supported, leadered, unpinned annotations visible in a view."""
    from Autodesk.Revit.DB import FilteredElementCollector
    found = []
    try:
        collector = FilteredElementCollector(doc, view.Id) \
            .WhereElementIsNotElementType()
        for element in collector:
            wrapper = wrappers.wrap(element, doc)
            if wrapper is None:
                continue
            try:
                if not wrapper.has_leader or wrapper.is_pinned:
                    continue
            except Exception:
                continue
            found.append(wrapper)
    except Exception as ex:
        common.logger.debug('Visible collection failed: {}'.format(ex))
    return found
