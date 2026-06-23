# -*- coding: utf-8 -*-
"""Pipe Spacing Tool.

Automatically adjusts the clear spacing between selected non-sloped pipe
runs based on a user-defined clearance value, while keeping a user-chosen
reference run fixed in place.

A pipe run is the set of connected pipes, fittings and accessories that
form one continuous straight line. Connected segments are grouped together
and re-spaced as a single rigid group. Pipes that run across the reference
direction (headers / crossovers) are treated as connectors: instead of
being spaced, they are reshaped so their ends follow the moved runs.

Workflow
--------
1. Select the pipes to space in the model (one or more parallel runs, plus
   any crossing connectors you want carried along).
2. Graphically pick one of them as the reference pipe (its run stays fixed).
3. Enter the required clear distance (mm) between adjacent pipe surfaces.
4. The tool validates, groups connected runs, re-spaces them and
   auto-corrects the crossing connectors.

The user-entered value is the *clear* distance between the outermost
surfaces of adjacent runs (outside of insulation when present, otherwise
outside of the pipe wall).

Author: Naveen
Target: Revit 2024 / pyRevit / IronPython
"""

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
from pyrevit import revit, DB, forms, script

from Autodesk.Revit.DB import (
    XYZ,
    Line,
    Transaction,
    ElementTransformUtils,
    BuiltInParameter,
    BuiltInCategory,
    ElementId,
    FamilyInstance,
)
from Autodesk.Revit.DB.Plumbing import Pipe, PipeInsulation
from Autodesk.Revit.UI.Selection import ObjectType

from System.Collections.Generic import List

# ---------------------------------------------------------------------------
# Globals / configuration
# ---------------------------------------------------------------------------
doc = revit.doc
uidoc = revit.uidoc

# pyRevit output logger (visible in the pyRevit output window for debugging).
logger = script.get_logger()

# Geometric tolerances expressed in Revit internal units (feet).
# Used to decide whether pipes are parallel, level and non-sloped.
ANGLE_TOLERANCE = 0.001        # radians, ~0.057 degrees
ELEVATION_TOLERANCE = 0.0001   # feet, ~0.03 mm (negligible-distance epsilon)
SLOPE_TOLERANCE = 0.0001       # feet, vertical run allowed across the pipe

# How far apart in elevation pipes may sit and still count as "one level".
# Kept practical (not the sub-mm epsilon above) to absorb modelling noise.
SAME_ELEVATION_TOLERANCE = 5.0 / 304.8   # feet (~5 mm)

# Max perpendicular gap for treating connected segments as one straight run.
COLLINEAR_TOLERANCE = 1.0 / 304.8   # feet (~1 mm)


# ---------------------------------------------------------------------------
# Version compatibility (Revit 2022-2025)
# ---------------------------------------------------------------------------
def _eid(element_id):
    """Return a stable integer key for an ElementId across Revit versions.

    Revit 2024+ exposes the Int64 ``ElementId.Value`` and deprecates
    ``IntegerValue``; Revit 2022/2023 only have ``IntegerValue``. Preferring
    ``Value`` keeps the tool working from 2022 through 2025 (and beyond, once
    ``IntegerValue`` is finally removed).

    Args:
        element_id (ElementId): The id to read.

    Returns:
        int: The element id as a plain integer (used as dict/set keys and in
        user-facing messages).
    """
    try:
        return element_id.Value          # Revit 2024+
    except AttributeError:
        return element_id.IntegerValue   # Revit 2022 / 2023


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------
def mm_to_internal(value_mm):
    """Convert a millimeter value to Revit internal units (feet).

    Args:
        value_mm (float): Length in millimeters.

    Returns:
        float: Length in Revit internal units (decimal feet).
    """
    try:
        # Revit 2022+ API.
        from Autodesk.Revit.DB import UnitUtils, UnitTypeId
        return UnitUtils.ConvertToInternalUnits(value_mm, UnitTypeId.Millimeters)
    except Exception:
        # Fallback for safety: 1 ft = 304.8 mm.
        return value_mm / 304.8


def internal_to_mm(value_ft):
    """Convert a Revit internal length (feet) to millimeters.

    Args:
        value_ft (float): Length in internal units (feet).

    Returns:
        float: Length in millimeters.
    """
    try:
        from Autodesk.Revit.DB import UnitUtils, UnitTypeId
        return UnitUtils.ConvertFromInternalUnits(value_ft, UnitTypeId.Millimeters)
    except Exception:
        return value_ft * 304.8


# ---------------------------------------------------------------------------
# Step 1 & 2 - Selection
# ---------------------------------------------------------------------------
def get_selected_pipes():
    """Return the currently selected pipes, prompting a pick if needed.

    First inspects the active selection. If no pipes are pre-selected, the
    user is asked to pick pipes interactively.

    Returns:
        list[Pipe]: The selected pipe elements (possibly empty).
    """
    pipes = []

    # Inspect the active selection first.
    selection_ids = uidoc.Selection.GetElementIds()
    for el_id in selection_ids:
        el = doc.GetElement(el_id)
        if isinstance(el, Pipe):
            pipes.append(el)

    # Nothing useful pre-selected -> ask the user to pick pipes.
    if not pipes:
        logger.debug('No pipes pre-selected; prompting interactive pick.')
        try:
            picked = revit.pick_elements(message='Select two or more pipes')
        except Exception as ex:
            logger.debug('Pick cancelled or failed: {}'.format(ex))
            picked = None

        if picked:
            for el in picked:
                if isinstance(el, Pipe):
                    pipes.append(el)

    logger.debug('Collected {} pipe(s).'.format(len(pipes)))
    return pipes


def select_reference_pipe(pipes):
    """Prompt the user to graphically pick the reference (fixed) pipe.

    The user picks a single element directly in the Revit model using
    Revit's native selection interface. The picked element must be one of
    the originally selected pipes; if it is not, the user is warned and
    asked to pick again. Pressing Escape cancels the operation.

    Args:
        pipes (list[Pipe]): The originally selected candidate pipes.

    Returns:
        Pipe | None: The chosen reference pipe, or None if the pick was
        cancelled.
    """
    selected_ids = [p.Id for p in pipes]

    # Loop until the user picks a valid pipe or cancels with Escape.
    while True:
        try:
            picked_ref = uidoc.Selection.PickObject(
                ObjectType.Element,
                'Select the reference pipe (it will not move)')
        except Exception as ex:
            # User pressed Escape or otherwise cancelled the pick.
            logger.debug('Reference pick cancelled: {}'.format(ex))
            return None

        reference = doc.GetElement(picked_ref.ElementId)

        # The picked element must belong to the originally selected set.
        if reference is not None and reference.Id in selected_ids:
            logger.debug('Reference pipe selected: {}'.format(
                _eid(reference.Id)))
            return reference

        # Invalid pick -> warn and prompt again.
        forms.alert(
            'The selected reference pipe must be one of the pipes included '
            'in the spacing operation.',
            title='Pipe Spacing')


# ---------------------------------------------------------------------------
# Step 5-8 - Validation
# ---------------------------------------------------------------------------
def _get_pipe_line(pipe):
    """Return the pipe centerline as a Line, or None if not a straight line.

    Args:
        pipe (Pipe): The pipe element.

    Returns:
        Line | None: The centerline if it is a straight Line; otherwise None.
    """
    location = pipe.Location
    if not isinstance(location, DB.LocationCurve):
        return None

    curve = location.Curve
    if isinstance(curve, Line):
        return curve
    return None


def validate_pipes(pipes, reference):
    """Validate the pipe set against all geometric and selection rules.

    Checks performed (in order):
        * At least two pipes selected.
        * All elements are pipes.
        * Reference pipe belongs to the selected set.
        * All pipes are straight lines.
        * No pipe is sloped (constant elevation along its run).
        * Pipes parallel to the reference share the reference elevation.

    Pipes that run across the reference direction are no longer rejected;
    they are handled later as connectors (see partition_pipes()) and may
    sit at other elevations.

    Args:
        pipes (list[Pipe]): The selected pipes.
        reference (Pipe): The chosen reference pipe.

    Returns:
        bool: True if every check passes, otherwise False (a warning dialog
        is shown to the user describing the first failure encountered).
    """
    # --- At least two pipes ------------------------------------------------
    if not pipes or len(pipes) < 2:
        forms.alert('Please select at least two pipes.', title='Pipe Spacing')
        return False

    # --- All elements are pipes -------------------------------------------
    if not all(isinstance(p, Pipe) for p in pipes):
        forms.alert('Selected elements must be pipes.', title='Pipe Spacing')
        return False

    # --- Reference belongs to the set -------------------------------------
    if reference is None or reference.Id not in [p.Id for p in pipes]:
        forms.alert('The reference pipe must be one of the selected pipes.',
                    title='Pipe Spacing')
        return False

    # --- All pipes are straight lines -------------------------------------
    lines = []
    for pipe in pipes:
        line = _get_pipe_line(pipe)
        if line is None:
            forms.alert(
                'Pipe {} is not a straight segment. Pipe Spacing supports '
                'only straight pipes.'.format(_eid(pipe.Id)),
                title='Pipe Spacing')
            return False
        lines.append(line)

    # --- No sloped pipes ---------------------------------------------------
    for line in lines:
        start = line.GetEndPoint(0)
        end = line.GetEndPoint(1)
        if abs(start.Z - end.Z) > SLOPE_TOLERANCE:
            forms.alert(
                'Selected pipes contain sloped elements. Pipe Spacing Tool '
                'supports only non-sloped pipes.',
                title='Pipe Spacing')
            return False

    # Parallel grouping is no longer enforced here: pipes running across the
    # reference are separated out as connectors by partition_pipes().

    # --- Same elevation (run pipes only) -----------------------------------
    # Only the pipes that will actually be spaced (those parallel to the
    # reference) must share its elevation. Crossing connectors are allowed
    # to sit at other levels - they get reshaped to follow the runs.
    ref_dir = _get_pipe_line(reference).Direction.Normalize()
    base_z = _get_pipe_line(reference).GetEndPoint(0).Z
    for pipe, line in zip(pipes, lines):
        direction = line.Direction.Normalize()
        if ref_dir.CrossProduct(direction).GetLength() > ANGLE_TOLERANCE:
            continue  # A crossing connector, not a run pipe.
        dz = line.GetEndPoint(0).Z - base_z
        if abs(dz) > SAME_ELEVATION_TOLERANCE:
            forms.alert(
                'Pipe {} is at a different elevation from the reference '
                '(off by {:.1f} mm). Pipes to be spaced must share the '
                'reference elevation.'.format(
                    _eid(pipe.Id), internal_to_mm(abs(dz))),
                title='Pipe Spacing')
            return False

    logger.debug('All validation checks passed.')
    return True


# ---------------------------------------------------------------------------
# Network grouping - identify connected pipe runs
# ---------------------------------------------------------------------------
def _get_connector_manager(element):
    """Return the ConnectorManager of a pipe/fitting/accessory, or None.

    Args:
        element: A Revit element (Pipe or FamilyInstance).

    Returns:
        ConnectorManager | None: The element's connector manager if it has
        MEP connectors, otherwise None.
    """
    try:
        if isinstance(element, Pipe):
            return element.ConnectorManager
        if isinstance(element, FamilyInstance) and element.MEPModel is not None:
            return element.MEPModel.ConnectorManager
    except Exception as ex:
        logger.debug('Connector manager lookup failed: {}'.format(ex))
    return None


def _connected_elements(element):
    """Return the elements physically connected to the given element.

    Walks every connector of the element and collects the owners of the
    connectors joined to it (excluding the element itself).

    Args:
        element: A Revit element with MEP connectors.

    Returns:
        list: The directly connected elements (may contain duplicates).
    """
    neighbors = []
    manager = _get_connector_manager(element)
    if manager is None:
        return neighbors

    for connector in manager.Connectors:
        try:
            refs = connector.AllRefs
        except Exception:
            continue
        for ref in refs:
            owner = ref.Owner
            if owner is None or owner.Id == element.Id:
                continue
            neighbors.append(owner)
    return neighbors


def _is_fitting_or_accessory(element):
    """True if the element is a pipe fitting or pipe accessory instance.

    Args:
        element: A Revit element.

    Returns:
        bool: True for OST_PipeFitting / OST_PipeAccessory family instances.
    """
    if not isinstance(element, FamilyInstance):
        return False
    category = element.Category
    if category is None:
        return False
    return category.Id in (
        ElementId(BuiltInCategory.OST_PipeFitting),
        ElementId(BuiltInCategory.OST_PipeAccessory),
    )


def _point_on_axis(point, axis_line, tolerance):
    """True if a point lies on the infinite line defined by axis_line.

    Args:
        point (XYZ): The point to test.
        axis_line (Line): The reference axis line.
        tolerance (float): Allowed perpendicular distance (feet).

    Returns:
        bool: True if the point is within tolerance of the axis.
    """
    origin = axis_line.GetEndPoint(0)
    direction = axis_line.Direction.Normalize()
    delta = point - origin
    along = delta.DotProduct(direction)
    perpendicular = delta - direction.Multiply(along)
    return perpendicular.GetLength() <= tolerance


def _is_collinear_pipe(pipe, axis_line, tolerance):
    """True if a pipe is straight and collinear with the run axis line.

    A pipe continues the run only when it is parallel to the axis and both
    of its endpoints lie on the axis line. This keeps each run a single
    straight line and prevents perpendicular branches (e.g. headers) from
    merging two parallel runs into one.

    Args:
        pipe (Pipe): The candidate pipe.
        axis_line (Line): The run's reference axis line.
        tolerance (float): Allowed perpendicular distance (feet).

    Returns:
        bool: True if the pipe lies on the same straight line.
    """
    line = _get_pipe_line(pipe)
    if line is None:
        return False

    cross = axis_line.Direction.Normalize().CrossProduct(
        line.Direction.Normalize())
    if cross.GetLength() > ANGLE_TOLERANCE:
        return False

    return (_point_on_axis(line.GetEndPoint(0), axis_line, tolerance) and
            _point_on_axis(line.GetEndPoint(1), axis_line, tolerance))


def group_connected_runs(pipes):
    """Group selected pipes into connected, collinear pipe runs.

    Starting from each selected pipe, walks the connector network through
    fittings and accessories and along collinear pipe segments. Each run is
    a single movable group containing every pipe, fitting and accessory on
    that straight line. Perpendicular branches are not followed, so two
    parallel runs joined by a header remain separate.

    Args:
        pipes (list[Pipe]): The validated, selected pipes.

    Returns:
        list[dict]: One dict per run with keys:
            'pipes'        - selected pipes belonging to the run
            'element_ids'  - ElementId list of every element to move
    """
    selected_ints = set(_eid(p.Id) for p in pipes)
    pipe_by_int = dict((_eid(p.Id), p) for p in pipes)

    claimed = set()          # int ids already assigned to any run
    runs = []

    for seed in pipes:
        if _eid(seed.Id) in claimed:
            continue

        axis_line = _get_pipe_line(seed)
        run_pipes = []
        run_element_ids = []
        run_element_ints = set()

        # Breadth-first walk constrained to the seed's straight line.
        queue = [seed]
        visited_local = set([_eid(seed.Id)])

        while queue:
            element = queue.pop()
            eint = _eid(element.Id)
            is_pipe = isinstance(element, Pipe)

            if is_pipe:
                # Only stay on the run if collinear with the seed axis.
                if axis_line is not None and not _is_collinear_pipe(
                        element, axis_line, COLLINEAR_TOLERANCE):
                    continue
            elif not _is_fitting_or_accessory(element):
                # Not a pipe, fitting or accessory -> not part of the run.
                continue

            # Element belongs to this run.
            if eint not in run_element_ints:
                run_element_ints.add(eint)
                run_element_ids.append(element.Id)
                claimed.add(eint)
                if eint in selected_ints:
                    run_pipes.append(pipe_by_int[eint])

            # Expand to neighbours not yet seen by this or another run.
            for neighbor in _connected_elements(element):
                nint = _eid(neighbor.Id)
                if nint in visited_local or nint in claimed:
                    continue
                visited_local.add(nint)
                queue.append(neighbor)

        runs.append({
            'pipes': run_pipes,
            'element_ids': run_element_ids,
        })
        logger.debug('Run: {} selected pipe(s), {} total element(s).'.format(
            len(run_pipes), len(run_element_ids)))

    return runs


def partition_pipes(pipes, reference):
    """Split the selection into pipes to space and crossing connectors.

    Pipes parallel to the reference pipe form the runs that get re-spaced.
    Pipes running in any other direction are treated as connectors (e.g.
    headers or crossovers) that are auto-corrected to follow the runs.

    Args:
        pipes (list[Pipe]): The validated selection.
        reference (Pipe): The chosen reference pipe.

    Returns:
        tuple(list[Pipe], list[Pipe]): (spacing_pipes, connector_pipes).
    """
    ref_dir = _get_pipe_line(reference).Direction.Normalize()

    spacing_pipes = []
    connector_pipes = []
    for pipe in pipes:
        direction = _get_pipe_line(pipe).Direction.Normalize()
        # Parallel to the reference (same or opposite direction) -> a run.
        if ref_dir.CrossProduct(direction).GetLength() <= ANGLE_TOLERANCE:
            spacing_pipes.append(pipe)
        else:
            connector_pipes.append(pipe)

    logger.debug('Partition: {} run pipe(s), {} connector pipe(s).'.format(
        len(spacing_pipes), len(connector_pipes)))
    return spacing_pipes, connector_pipes


# ---------------------------------------------------------------------------
# Connector auto-correction
# ---------------------------------------------------------------------------
def _connector_endpoint_delta(connector, delta_by_int):
    """Return the run translation for the element joined at this connector.

    Args:
        connector (Connector): A connector of a crossing pipe.
        delta_by_int (dict): Map of moved-element id -> translation (XYZ).

    Returns:
        XYZ | None: The translation of the connected run, or None if the
        connector joins nothing that moved.
    """
    try:
        refs = connector.AllRefs
    except Exception:
        return None
    for ref in refs:
        owner = ref.Owner
        if owner is None:
            continue
        delta = delta_by_int.get(_eid(owner.Id))
        if delta is not None:
            return delta
    return None


def plan_connector_corrections(connector_pipes, moves):
    """Pre-compute the new endpoints for each crossing connector pipe.

    Connectivity is read *before* the runs move (while joints are intact).
    Each pipe end is shifted by the translation of the run it connects to,
    so applying the plan after the move keeps the network continuous.

    Args:
        connector_pipes (list[Pipe]): Pipes running across the runs.
        moves (list[dict]): Run moves from calculate_spacing().

    Returns:
        list[tuple]: (pipe, new_end0, new_end1) for connectors that need to
        move. Connectors joining nothing that moved are omitted.
    """
    # Map every moved element -> its run translation.
    delta_by_int = {}
    for move in moves:
        for element_id in move['element_ids']:
            delta_by_int[_eid(element_id)] = move['translation']

    plans = []
    for pipe in connector_pipes:
        line = _get_pipe_line(pipe)
        if line is None:
            continue
        manager = _get_connector_manager(pipe)
        if manager is None:
            continue

        end0 = line.GetEndPoint(0)
        end1 = line.GetEndPoint(1)
        delta0 = XYZ.Zero
        delta1 = XYZ.Zero

        # Assign each connector's run translation to the nearer pipe end.
        for connector in manager.Connectors:
            try:
                origin = connector.Origin
            except Exception:
                continue
            delta = _connector_endpoint_delta(connector, delta_by_int)
            if delta is None:
                continue
            if origin.DistanceTo(end0) <= origin.DistanceTo(end1):
                delta0 = delta
            else:
                delta1 = delta

        if delta0.IsZeroLength() and delta1.IsZeroLength():
            continue  # Nothing this connector joins actually moves.

        plans.append((pipe, end0 + delta0, end1 + delta1))
        logger.debug('Connector {} will be reshaped.'.format(
            _eid(pipe.Id)))

    return plans


def _disconnect_all(pipe):
    """Disconnect every joint of a pipe so its curve can be edited freely."""
    manager = _get_connector_manager(pipe)
    if manager is None:
        return
    for connector in manager.Connectors:
        try:
            refs = list(connector.AllRefs)
        except Exception:
            continue
        for ref in refs:
            try:
                if ref.Owner is not None and ref.Owner.Id != pipe.Id \
                        and connector.IsConnectedTo(ref):
                    connector.DisconnectFrom(ref)
            except Exception:
                pass


def _apply_connector_correction(pipe, new_end0, new_end1):
    """Reshape one connector pipe to span its new endpoints.

    Tries to keep existing joints; if Revit refuses (the pipe is still
    connected), the joints are released and the curve is set again.

    Returns:
        bool: True if the pipe was reshaped.
    """
    location = pipe.Location
    if not isinstance(location, DB.LocationCurve):
        return False
    if new_end0.DistanceTo(new_end1) < ELEVATION_TOLERANCE:
        return False  # Degenerate (zero-length) result.

    new_curve = Line.CreateBound(new_end0, new_end1)
    try:
        location.Curve = new_curve
        return True
    except Exception:
        # Retry after releasing the pipe's connections.
        _disconnect_all(pipe)
        location.Curve = new_curve
        return True


# ---------------------------------------------------------------------------
# Step 9-13 - Run data extraction
# ---------------------------------------------------------------------------
def _get_outer_diameter(pipe):
    """Return the outer diameter of a pipe in internal units (feet).

    Args:
        pipe (Pipe): The pipe element.

    Returns:
        float: Outer diameter in feet.
    """
    param = pipe.get_Parameter(BuiltInParameter.RBS_PIPE_OUTER_DIAMETER)
    if param is None:
        # Fall back to the nominal diameter parameter if OD is unavailable.
        param = pipe.get_Parameter(BuiltInParameter.RBS_PIPE_DIAMETER_PARAM)
    return param.AsDouble()


def _get_insulation_thickness(pipe):
    """Return the insulation thickness of a pipe in internal units (feet).

    Uses PipeInsulation.GetInsulationIds() to detect insulation hosted by
    the pipe. Returns 0.0 when no insulation is present.

    Args:
        pipe (Pipe): The pipe element.

    Returns:
        float: Insulation thickness in feet (0.0 if uninsulated).
    """
    try:
        insulation_ids = PipeInsulation.GetInsulationIds(doc, pipe.Id)
    except Exception as ex:
        logger.debug('Insulation lookup failed for {}: {}'.format(pipe.Id, ex))
        return 0.0

    if not insulation_ids:
        return 0.0

    # Sum thickness of all hosted insulation layers (typically one).
    total = 0.0
    for ins_id in insulation_ids:
        insulation = doc.GetElement(ins_id)
        if isinstance(insulation, PipeInsulation):
            total += insulation.Thickness
    return total


def get_run_data(runs):
    """Build per-run geometric data used for spacing calculations.

    Each run is reduced to a single representative geometry so that runs can
    be spaced just like individual pipes were before:
        * line        - centerline Line of the run (its first selected pipe)
        * origin      - centerline start point (XYZ)
        * eff_radius  - largest effective radius (OD/2 + insulation) in the
                        run, so the requested clearance is honoured along the
                        whole run even when segment sizes differ
        * element_ids - every element moved with the run (pipes, fittings,
                        accessories)

    Args:
        runs (list[dict]): Runs produced by group_connected_runs().

    Returns:
        list[dict]: One data dictionary per run (runs without a selected
        pipe to anchor geometry on are skipped).
    """
    data = []
    for run in runs:
        run_pipes = run['pipes']
        if not run_pipes:
            continue

        line = _get_pipe_line(run_pipes[0])

        # Use the largest effective radius found among the run's pipes.
        eff_radius = 0.0
        rep_outer = 0.0
        rep_insulation = 0.0
        for pipe in run_pipes:
            outer_diameter = _get_outer_diameter(pipe)
            insulation = _get_insulation_thickness(pipe)
            radius = (outer_diameter / 2.0) + insulation
            if radius > eff_radius:
                eff_radius = radius
                rep_outer = outer_diameter
                rep_insulation = insulation

        data.append({
            'pipes': run_pipes,
            'element_ids': run['element_ids'],
            'line': line,
            'origin': line.GetEndPoint(0),
            'outer_diameter': rep_outer,
            'insulation': rep_insulation,
            'eff_radius': eff_radius,
        })

        logger.debug(
            'Run ({} pipes, {} elements): OD={:.1f}mm insul={:.1f}mm '
            'effR={:.1f}mm'.format(
                len(run_pipes),
                len(run['element_ids']),
                internal_to_mm(rep_outer),
                internal_to_mm(rep_insulation),
                internal_to_mm(eff_radius),
            )
        )
    return data


# ---------------------------------------------------------------------------
# Step 14-17 - Spacing calculation
# ---------------------------------------------------------------------------
def _perpendicular_axis(pipe_direction):
    """Return a horizontal unit vector perpendicular to the pipe direction.

    Because all pipes are non-sloped and parallel, spacing is measured in
    the horizontal plane perpendicular to the (shared) pipe direction.

    Args:
        pipe_direction (XYZ): A pipe's normalized direction vector.

    Returns:
        XYZ: A normalized vector perpendicular to the pipe in the XY plane.
    """
    perp = XYZ(-pipe_direction.Y, pipe_direction.X, 0).Normalize()
    return perp


def calculate_spacing(run_data, clearance_ft):
    """Compute target offsets that re-space runs around the reference run.

    The runs are sorted along the perpendicular axis. The reference run
    stays fixed; every other run gets a single translation vector (along the
    perpendicular axis) so that the clear distance between adjacent run
    surfaces equals the user clearance.

    Formulas:
        eff_radius   = OD/2 + insulation (largest in the run)
        center_dist  = clearance + prev.eff_radius + curr.eff_radius
        offset       = required_center_dist - current_center_dist

    Args:
        run_data (list[dict]): Per-run data from get_run_data().
        clearance_ft (float): Desired clear distance (feet).

    Returns:
        list[dict]: For every non-reference run, a dict with keys
        'element_ids', 'translation' (an XYZ vector) and 'pipes'. Returns an
        empty list when nothing needs to move.
    """
    # Shared perpendicular axis (all runs are parallel).
    direction = run_data[0]['line'].Direction.Normalize()
    perp = _perpendicular_axis(direction)

    # Signed position of each run along the perpendicular axis.
    for entry in run_data:
        entry['position'] = entry['origin'].DotProduct(perp)

    # Sort runs by their perpendicular position (ascending).
    ordered = sorted(run_data, key=lambda e: e['position'])

    # Locate the reference run within the ordered list.
    ref_index = next(
        i for i, e in enumerate(ordered) if e.get('is_reference'))

    # New (target) perpendicular positions, anchored at the reference run.
    new_positions = [0.0] * len(ordered)
    new_positions[ref_index] = ordered[ref_index]['position']

    # Walk outward from the reference toward the higher-position side.
    for i in range(ref_index + 1, len(ordered)):
        prev = ordered[i - 1]
        curr = ordered[i]
        center_dist = clearance_ft + prev['eff_radius'] + curr['eff_radius']
        new_positions[i] = new_positions[i - 1] + center_dist

    # Walk outward from the reference toward the lower-position side.
    for i in range(ref_index - 1, -1, -1):
        nxt = ordered[i + 1]
        curr = ordered[i]
        center_dist = clearance_ft + nxt['eff_radius'] + curr['eff_radius']
        new_positions[i] = new_positions[i + 1] - center_dist

    # Build translation vectors for non-reference runs.
    moves = []
    for i, entry in enumerate(ordered):
        if entry.get('is_reference'):
            continue
        delta = new_positions[i] - entry['position']
        if abs(delta) < ELEVATION_TOLERANCE:
            continue  # Already in the right place.
        translation = perp.Multiply(delta)
        moves.append({
            'element_ids': entry['element_ids'],
            'translation': translation,
            'pipes': entry['pipes'],
        })
        logger.debug('Run offset {:.1f} mm ({} elements).'.format(
            internal_to_mm(delta), len(entry['element_ids'])))

    return moves


# ---------------------------------------------------------------------------
# Step 18-20 - Move runs
# ---------------------------------------------------------------------------
def move_runs(moves, connector_plans=None):
    """Move each run and auto-correct connectors in a single transaction.

    Every run is moved as one group with ElementTransformUtils.MoveElements
    so its pipes, fittings and accessories translate together. Crossing
    connector pipes are then reshaped (per the pre-computed plans) so their
    ends follow the runs they join, keeping the network continuous.

    Args:
        moves (list[dict]): Move instructions from calculate_spacing(), each
            with keys 'element_ids' and 'translation'.
        connector_plans (list[tuple]): (pipe, new_end0, new_end1) entries
            from plan_connector_corrections(); may be None or empty.

    Returns:
        tuple(int, int): (runs moved, connector pipes reshaped).
    """
    if not moves:
        return 0, 0

    moved = 0
    corrected = 0
    with Transaction(doc, 'Pipe Spacing') as trans:
        trans.Start()
        for move in moves:
            id_collection = List[ElementId]()
            for element_id in move['element_ids']:
                id_collection.Add(element_id)
            ElementTransformUtils.MoveElements(
                doc, id_collection, move['translation'])
            moved += 1
        # Regenerate so connector reads reflect the moved fitting geometry.
        doc.Regenerate()

        # Reshape crossing connector pipes to follow the moved runs.
        for pipe, new_end0, new_end1 in (connector_plans or []):
            try:
                if _apply_connector_correction(pipe, new_end0, new_end1):
                    corrected += 1
            except Exception as ex:
                logger.debug('Auto-correct failed for pipe {}: {}'.format(
                    _eid(pipe.Id), ex))

        doc.Regenerate()
        trans.Commit()

    logger.debug('Moved {} run(s); reshaped {} connector(s).'.format(
        moved, corrected))
    return moved, corrected


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def main():
    """Entry point that wires the full workflow together."""
    # Step 1-2: gather pipes.
    pipes = get_selected_pipes()
    if not pipes or len(pipes) < 2:
        forms.alert('Please select at least two pipes.', title='Pipe Spacing')
        return

    # Step 3: choose the reference pipe.
    reference = select_reference_pipe(pipes)
    if reference is None:
        logger.debug('Reference selection cancelled.')
        return

    # Step 4: ask for the clearance value (mm).
    clearance_str = forms.ask_for_string(
        default='50',
        prompt='Enter the required clear distance between pipes (mm):',
        title='Pipe Spacing')
    if not clearance_str:
        return

    try:
        clearance_mm = float(clearance_str)
    except ValueError:
        forms.alert('Please enter a valid numeric clearance value (mm).',
                    title='Pipe Spacing')
        return

    if clearance_mm < 0:
        forms.alert('Clearance value cannot be negative.', title='Pipe Spacing')
        return

    # Steps 5-8: validate the selection geometry.
    if not validate_pipes(pipes, reference):
        return

    # Separate the pipes to space (parallel to the reference) from the
    # crossing connectors (headers / crossovers) that will follow the runs.
    spacing_pipes, connector_pipes = partition_pipes(pipes, reference)

    # Steps 9-13: identify connected pipe runs and collect their geometry.
    runs = group_connected_runs(spacing_pipes)
    run_data = get_run_data(runs)

    if len(run_data) < 2:
        forms.alert(
            'Only one run could be spaced from this selection. Select pipes '
            'from at least two separate parallel runs to apply spacing.',
            title='Pipe Spacing')
        return

    # Flag the run that owns the reference pipe so the calculator anchors on it.
    for entry in run_data:
        entry['is_reference'] = any(
            p.Id == reference.Id for p in entry['pipes'])

    if not any(entry['is_reference'] for entry in run_data):
        forms.alert('Could not associate the reference pipe with a run.',
                    title='Pipe Spacing')
        return

    # Steps 14-17: compute spacing / movement offsets per run.
    clearance_ft = mm_to_internal(clearance_mm)
    moves = calculate_spacing(run_data, clearance_ft)

    if not moves:
        forms.alert('Pipes are already at the requested spacing. '
                    'Nothing to move.', title='Pipe Spacing')
        return

    # Plan connector corrections from the pre-move connectivity (joints intact).
    connector_plans = plan_connector_corrections(connector_pipes, moves)

    # Steps 18-19: move the runs and auto-correct connectors, then regenerate.
    try:
        moved, corrected = move_runs(moves, connector_plans)
    except Exception as ex:
        logger.error('Failed to move pipes: {}'.format(ex))
        forms.alert('An error occurred while moving pipes:\n{}'.format(ex),
                    title='Pipe Spacing')
        return

    # Step 20: report success.
    summary = (
        'Pipe spacing complete.\n\n'
        'Reference pipe: {}\n'
        'Clearance: {:.0f} mm\n'
        'Runs moved: {}'.format(
            _eid(reference.Id), clearance_mm, moved))
    if connector_pipes:
        summary += '\nConnectors auto-corrected: {}/{}'.format(
            corrected, len(connector_pipes))
    forms.alert(summary, title='Pipe Spacing')


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        logger.error('Unhandled error: {}'.format(exc))
        forms.alert('Unexpected error:\n{}'.format(exc), title='Pipe Spacing')
