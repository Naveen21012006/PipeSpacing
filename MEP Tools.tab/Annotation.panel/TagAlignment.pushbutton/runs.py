# -*- coding: utf-8 -*-
"""Group selected pipe segments into runs, for one-tag-per-run tagging.

A run is a maximal chain of pipe segments that are:
    * connected (through fittings / accessories, via their connectors),
    * collinear (share one straight line), and
    * the same nominal size.

Where a riser changes size at a reducer, the same-size rule breaks the chain,
so each size becomes its own run and keeps its own tag. Used only by the
Cluster-on-Reference-Line methods; every other method tags per element.

The connectivity walk mirrors the Pipe Spacing tool's run grouping.
"""

from Autodesk.Revit.DB import Line, BuiltInParameter
from Autodesk.Revit.DB.Plumbing import Pipe

import utils

_ANGLE_TOL = 0.001          # radians (~0.06 deg) for "parallel"
_POINT_TOL = 1.0 / 304.8    # feet (~1 mm) for "on the same line"


# ---------------------------------------------------------------------------
# Geometry / parameter helpers
# ---------------------------------------------------------------------------
def _pipe_line(pipe):
    """Return a pipe's centreline as a straight Line, or None."""
    location = getattr(pipe, "Location", None)
    curve = getattr(location, "Curve", None)
    return curve if isinstance(curve, Line) else None


def _nominal(pipe):
    """Return the pipe nominal diameter (feet, rounded), or None."""
    param = pipe.get_Parameter(BuiltInParameter.RBS_PIPE_DIAMETER_PARAM)
    return round(param.AsDouble(), 6) if param is not None else None


def _connector_manager(element):
    """Return the ConnectorManager of a pipe/fitting/accessory, or None."""
    try:
        if isinstance(element, Pipe):
            return element.ConnectorManager
        mep_model = getattr(element, "MEPModel", None)
        return mep_model.ConnectorManager if mep_model is not None else None
    except Exception:
        return None


def _neighbours(element):
    """Return the elements physically connected to this one."""
    result = []
    manager = _connector_manager(element)
    if manager is None:
        return result
    for connector in manager.Connectors:
        try:
            refs = connector.AllRefs
        except Exception:
            continue
        for ref in refs:
            owner = ref.Owner
            if owner is not None and owner.Id != element.Id:
                result.append(owner)
    return result


def _point_on_axis(point, axis, tol):
    """True if a point lies on the infinite line defined by axis."""
    origin = axis.GetEndPoint(0)
    direction = axis.Direction.Normalize()
    delta = point - origin
    perpendicular = delta - direction.Multiply(delta.DotProduct(direction))
    return perpendicular.GetLength() <= tol


def _collinear(pipe, axis):
    """True if a pipe is straight and collinear with the run's axis line."""
    line = _pipe_line(pipe)
    if line is None:
        return False
    cross = axis.Direction.Normalize().CrossProduct(line.Direction.Normalize())
    if cross.GetLength() > _ANGLE_TOL:
        return False
    return (_point_on_axis(line.GetEndPoint(0), axis, _POINT_TOL) and
            _point_on_axis(line.GetEndPoint(1), axis, _POINT_TOL))


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------
def group_runs(pipes):
    """Group selected pipes into runs.

    Args:
        pipes (list[Pipe]): The selected pipe segments.

    Returns:
        list[list[Pipe]]: One list of segments per run.
    """
    selected = set(utils.element_id_value(p.Id) for p in pipes)
    claimed = set()
    runs = []

    for seed in pipes:
        seed_id = utils.element_id_value(seed.Id)
        if seed_id in claimed:
            continue

        axis = _pipe_line(seed)
        size = _nominal(seed)
        run = []
        run_ids = set()
        stack = [seed]
        seen = set([seed_id])

        while stack:
            element = stack.pop()
            element_id = utils.element_id_value(element.Id)

            if isinstance(element, Pipe):
                # A pipe stays on the run only while it keeps the same line and
                # the same size; a bend or a reducer ends the run, so stop
                # there without walking further.
                if axis is not None and not _collinear(element, axis):
                    continue
                if _nominal(element) != size:
                    continue
                # It is on the run's line. Tag it only if the user selected it,
                # but keep walking either way - an unselected interior segment
                # must BRIDGE the run, not split it.
                if element_id in selected and element_id not in run_ids:
                    run.append(element)
                    run_ids.add(element_id)
                    claimed.add(element_id)

            # Expand through fittings/accessories and along the run.
            for neighbour in _neighbours(element):
                neighbour_id = utils.element_id_value(neighbour.Id)
                if neighbour_id in seen:
                    continue
                seen.add(neighbour_id)
                stack.append(neighbour)

        if run:
            runs.append(run)

    utils.logger.debug("Grouped {} pipe(s) into {} run(s).".format(
        len(pipes), len(runs)))
    return runs


def run_representative(run):
    """Return the segment that should carry the run's single tag.

    The longest segment is used, as it is the most visible part of the run.
    """
    best = None
    best_length = -1.0
    for pipe in run:
        line = _pipe_line(pipe)
        length = line.Length if line is not None else 0.0
        if length > best_length:
            best_length = length
            best = pipe
    return best if best is not None else run[0]


def representatives(pipes):
    """Return one representative pipe per run for the given selection."""
    return [run_representative(run) for run in group_runs(pipes)]


# ---------------------------------------------------------------------------
# Riser extent (for the plan-view designation tags)
# ---------------------------------------------------------------------------
_VERTICAL_MIN = 0.7             # |unit Z| at or above this counts as vertical
_RISER_XY_TOL = 150.0 / 304.8   # feet (~150 mm): stack drift across reducers
_RISER_JOG_MAX = 600.0 / 304.8  # feet (~600 mm): off-stack piece walked through


def _vertical_direction(pipe):
    """Return a vertical pipe's unit direction, or None if not vertical."""
    line = _pipe_line(pipe)
    if line is None:
        return None
    direction = line.Direction.Normalize()
    return direction if abs(direction.Z) >= _VERTICAL_MIN else None


def _same_stack(point, anchor, tol):
    """True if a point sits over the anchor's plan position (XY only)."""
    dx = point.X - anchor.X
    dy = point.Y - anchor.Y
    return (dx * dx + dy * dy) ** 0.5 <= tol


def riser_extent(pipe):
    """Return (bottom_z, top_z) of the whole riser stack this pipe is part of.

    Walks the connected vertical run across floors, through couplings,
    fittings AND reducers - unlike group_runs, the size may change, because a
    riser usually reduces as it climbs. The tagged seed pipe ALWAYS counts, so
    the extent is never empty. Another segment stays on the stack while it is
    vertical and sits over the seed's plan position (within _RISER_XY_TOL of
    either seed endpoint). An off-stack pipe shorter than _RISER_JOG_MAX is an
    in-run jog (elbow + nipple + elbow around a beam): the walk passes through
    it without recording it. A longer off-stack pipe is a real takeoff and
    ends the walk in that direction, so a branch never drags the extent along.

    Args:
        pipe (Pipe): Any segment of the riser.

    Returns:
        tuple | None: (bottom_z, top_z) in feet, both always set, or None if
        the pipe itself is not a vertical segment.
    """
    if _vertical_direction(pipe) is None:
        return None
    seed_line = _pipe_line(pipe)
    anchors = (seed_line.GetEndPoint(0), seed_line.GetEndPoint(1))

    def near_stack(point):
        return (_same_stack(point, anchors[0], _RISER_XY_TOL)
                or _same_stack(point, anchors[1], _RISER_XY_TOL))

    bottom = None
    top = None
    stack = [pipe]
    seen = set([utils.element_id_value(pipe.Id)])

    while stack:
        element = stack.pop()

        if isinstance(element, Pipe):
            line = _pipe_line(element)
            if line is None:
                continue
            # The seed is the tagged pipe - its own extent is ground truth
            # even when it is a raked offset piece that misses its own
            # XY tolerance.
            on_stack = element is pipe or (
                _vertical_direction(element) is not None
                and near_stack(line.GetEndPoint(0))
                and near_stack(line.GetEndPoint(1)))
            if on_stack:
                for end in (line.GetEndPoint(0), line.GetEndPoint(1)):
                    if bottom is None or end.Z < bottom:
                        bottom = end.Z
                    if top is None or end.Z > top:
                        top = end.Z
            elif line.Length > _RISER_JOG_MAX:
                continue    # a real takeoff / branch: stop walking this way
            # else: a short in-run jog - pass through, record nothing

        # On-stack pipes, jogs and fittings/accessories keep the walk going.
        for neighbour in _neighbours(element):
            neighbour_id = utils.element_id_value(neighbour.Id)
            if neighbour_id in seen:
                continue
            seen.add(neighbour_id)
            stack.append(neighbour)

    return (bottom, top)
