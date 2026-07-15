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
