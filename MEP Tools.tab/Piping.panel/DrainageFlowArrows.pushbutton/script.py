# -*- coding: utf-8 -*-
"""Drainage Flow Arrows - place flow arrows on sloped drainage pipes.

Collects the pipes visible in the active view, lets the user pick which
piping systems are the drainage ones and which flow-arrow family type to
use, then places arrows on every inclined pipe so they point from the
higher endpoint to the lower endpoint - the drainage flow direction.

Per pipe the tool:
    * reads the location line and compares the endpoint elevations,
    * skips short, vertical and flat pipes (each with a recorded reason),
    * computes the arrow positions - one at the midpoint for short runs,
      several spread along long runs, clear of the ends,
    * skips any position that already has a flow arrow within the
      duplicate tolerance, so re-running the tool never doubles up,
    * places the chosen family and rotates it along the pipe: plan
      rotation always, plus the true downhill tilt for model families.

All geometry decisions live in flowarrow_core.py (pure, unit-tested
outside Revit); this file is the Revit glue - collection, selection UI,
family placement, transaction and report. All rules sit in the
CONFIGURATION block below.

Author: Naveen
Target: Revit 2022-2026 / pyRevit / IronPython
"""

import math
import os
import sys

_BUNDLE_DIR = os.path.dirname(__file__)
if _BUNDLE_DIR not in sys.path:
    sys.path.append(_BUNDLE_DIR)

from pyrevit import revit, forms, script

from Autodesk.Revit.DB import (
    BuiltInCategory,
    BuiltInParameter,
    ElementTransformUtils,
    FamilyInstance,
    FamilyPlacementType,
    FamilySymbol,
    FilteredElementCollector,
    Line,
    Transaction,
    XYZ,
)
from Autodesk.Revit.DB.Plumbing import Pipe
from Autodesk.Revit.DB.Structure import StructuralType

import flowarrow_core as core

doc = revit.doc
uidoc = revit.uidoc
logger = script.get_logger()
output = script.get_output()

TITLE = 'Drainage Flow Arrows'


# ===========================================================================
# CONFIGURATION  -  edit here only; no rules are hard-coded in the logic
# ===========================================================================
# Placement rules (millimetres / degrees). Copied from
# flowarrow_core.DEFAULTS so the numbers are visible and editable here.
CONFIG = {
    'min_pipe_length_mm': 1000.0,      # ignore pipes shorter than this
    'multi_arrow_threshold_mm': 15000.0,  # longer pipes get several arrows
    'end_clearance_mm': 1000.0,        # keep arrows this far from the ends
    'duplicate_tolerance_mm': 300.0,   # existing arrow within this = skip
    'min_elevation_diff_mm': 5.0,      # smaller rise/fall counts as flat
    'vertical_angle_deg': 80.0,        # steeper than this = a riser, skip
}

# A family symbol counts as a flow-arrow candidate when its family name or
# type name contains one of these (compared lower-cased). The candidates
# feed both the type picker and the existing-arrow detection, so keep the
# words specific enough not to match unrelated families.
ARROW_NAME_KEYWORDS = ('arrow', 'flow')

# Tilt model-family arrows to the pipe's true inclination (plan rotation
# is always applied). View-based annotation families cannot tilt.
TILT_ARROWS_TO_SLOPE = True
# ===========================================================================


# ---------------------------------------------------------------------------
# Version / unit / parameter helpers (repo conventions)
# ---------------------------------------------------------------------------
def _eid(element_id):
    """Return a stable integer id across Revit versions."""
    try:
        return element_id.Value          # Revit 2024+
    except AttributeError:
        return element_id.IntegerValue   # Revit 2022 / 2023


def mm_to_internal(value_mm):
    """Convert a millimetre value to Revit internal units (feet)."""
    try:
        from Autodesk.Revit.DB import UnitUtils, UnitTypeId
        return UnitUtils.ConvertToInternalUnits(value_mm, UnitTypeId.Millimeters)
    except Exception:
        return value_mm / 304.8


def internal_to_mm(value_ft):
    """Convert a Revit internal length (feet) to millimetres."""
    try:
        from Autodesk.Revit.DB import UnitUtils, UnitTypeId
        return UnitUtils.ConvertFromInternalUnits(value_ft, UnitTypeId.Millimeters)
    except Exception:
        return value_ft * 304.8


def _xyz_to_mm(point):
    """Convert a Revit XYZ (feet) to an (x, y, z) tuple in millimetres."""
    return (internal_to_mm(point.X),
            internal_to_mm(point.Y),
            internal_to_mm(point.Z))


def _mm_to_xyz(point_mm):
    """Convert an (x, y, z) millimetre tuple back to a Revit XYZ."""
    return XYZ(mm_to_internal(point_mm[0]),
               mm_to_internal(point_mm[1]),
               mm_to_internal(point_mm[2]))


def _element_name(element):
    """Return an element's name, tolerant of IronPython property quirks."""
    try:
        return element.Name
    except Exception:
        try:
            from Autodesk.Revit.DB import Element
            return Element.Name.GetValue(element)
        except Exception:
            return ''


def _param_string(element, built_in_param):
    """Return a string parameter value, or None if unavailable/empty."""
    param = element.get_Parameter(built_in_param)
    if param is None:
        return None
    try:
        value = param.AsString()
        if not value:
            value = param.AsValueString()
        return value
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Pipe collection and system selection
# ---------------------------------------------------------------------------
def get_visible_pipes():
    """Return all Pipe elements visible in the active view."""
    collector = (FilteredElementCollector(doc, doc.ActiveView.Id)
                 .OfCategory(BuiltInCategory.OST_PipeCurves)
                 .WhereElementIsNotElementType())
    pipes = [el for el in collector if isinstance(el, Pipe)]
    logger.debug('Found {} visible pipe(s).'.format(len(pipes)))
    return pipes


def get_pipe_system_label(pipe):
    """Return a display label for a pipe's piping system."""
    type_param = pipe.get_Parameter(
        BuiltInParameter.RBS_PIPING_SYSTEM_TYPE_PARAM)
    if type_param is not None:
        type_element = doc.GetElement(type_param.AsElementId())
        if type_element is not None:
            name = _element_name(type_element)
            if name:
                return name
    for built_in in (BuiltInParameter.RBS_SYSTEM_ABBREVIATION_PARAM,
                     BuiltInParameter.RBS_SYSTEM_NAME_PARAM,
                     BuiltInParameter.RBS_SYSTEM_CLASSIFICATION_PARAM):
        value = _param_string(pipe, built_in)
        if value:
            return value
    return '<no system>'


def get_present_systems(pipes):
    """Group visible pipes by piping system label."""
    present = {}
    for pipe in pipes:
        present.setdefault(get_pipe_system_label(pipe), []).append(pipe)
    return present


def select_drainage_systems(present):
    """Ask which piping systems are the drainage ones to process.

    Every system in the view is listed - project naming varies too much
    to guess reliably, and the user knows which systems drain.

    Returns:
        set | None: Chosen system labels, or None if cancelled.
    """
    display_to_label = {}
    for system_label in sorted(present):
        count = len(present[system_label])
        display = '{}  ({} pipe{})'.format(
            system_label, count, '' if count == 1 else 's')
        display_to_label[display] = system_label

    chosen = forms.SelectFromList.show(
        sorted(display_to_label.keys()),
        title='Select Drainage Systems',
        button_name='Next',
        multiselect=True)

    if not chosen:
        return None
    return set(display_to_label[display] for display in chosen)


# ---------------------------------------------------------------------------
# Flow-arrow family discovery and selection
# ---------------------------------------------------------------------------
def _is_arrow_name(text):
    """True when a family/type name matches the arrow keywords."""
    if not text:
        return False
    lowered = text.lower()
    return any(word in lowered for word in ARROW_NAME_KEYWORDS)


def find_arrow_symbols():
    """Return every loadable family symbol that looks like a flow arrow.

    Returns:
        dict: {'Family : Type' label: FamilySymbol}, possibly empty.
    """
    symbols = {}
    collector = FilteredElementCollector(doc).OfClass(FamilySymbol)
    for symbol in collector:
        family = symbol.Family
        if family is None:
            continue
        family_name = _element_name(family)
        type_name = _element_name(symbol)
        if _is_arrow_name(family_name) or _is_arrow_name(type_name):
            label = '{} : {}'.format(family_name, type_name)
            symbols[label] = symbol
    return symbols


def select_arrow_symbol(symbols):
    """Ask which flow-arrow type to place.

    Returns:
        FamilySymbol | None: The chosen symbol, or None if cancelled.
    """
    chosen = forms.SelectFromList.show(
        sorted(symbols.keys()),
        title='Select Flow Arrow Type',
        button_name='Place Arrows',
        multiselect=False)
    if not chosen:
        return None
    return symbols[chosen]


def _candidate_family_ids(symbols):
    """Return the set of family ids covered by the candidate symbols."""
    ids = set()
    for symbol in symbols.values():
        ids.add(_eid(symbol.Family.Id))
    return ids


def collect_existing_arrow_points(symbols, chosen_symbol, view):
    """Return the locations (mm tuples) of existing flow-arrow instances.

    Instances of ANY candidate arrow family count, so re-running with a
    different type still detects the arrows already placed. A view-based
    annotation family only exists per view, so detection is scoped to the
    active view for those; model families are checked document-wide.
    """
    family_ids = _candidate_family_ids(symbols)
    view_based = (chosen_symbol.Family.FamilyPlacementType ==
                  FamilyPlacementType.ViewBased)
    if view_based:
        collector = FilteredElementCollector(doc, view.Id)
    else:
        collector = FilteredElementCollector(doc)
    collector = collector.OfClass(FamilyInstance)

    points = []
    for instance in collector:
        try:
            symbol = instance.Symbol
            if symbol is None or _eid(symbol.Family.Id) not in family_ids:
                continue
            location = instance.Location
            point = getattr(location, 'Point', None)
            if point is not None:
                points.append(_xyz_to_mm(point))
        except Exception as ex:
            logger.debug('Existing-arrow scan skipped an instance: {}'
                         .format(ex))
    logger.debug('Found {} existing arrow point(s).'.format(len(points)))
    return points


# ---------------------------------------------------------------------------
# Placement and rotation
# ---------------------------------------------------------------------------
def place_arrow(symbol, point, view):
    """Create one arrow instance at a point and return it.

    View-based (annotation) families are placed in the active view; model
    families are placed free at the 3D point on the pipe centreline.
    """
    placement = symbol.Family.FamilyPlacementType
    if placement == FamilyPlacementType.ViewBased:
        return doc.Create.NewFamilyInstance(point, symbol, view)
    return doc.Create.NewFamilyInstance(
        point, symbol, StructuralType.NonStructural)


def rotate_arrow(instance, point, direction, view_based):
    """Rotate a placed arrow so it points down the drainage flow.

    The family is assumed to point along +X unrotated (the pyRevit /
    Revit family template convention). Plan rotation about Z aligns it
    with the flow bearing; model families are then tilted about the
    horizontal axis perpendicular to the flow so the arrow follows the
    pipe's true 3D inclination.
    """
    horizontal = math.hypot(direction[0], direction[1])
    if horizontal < 1e-9:
        return  # vertical pipes never reach here; nothing to align to

    angle = core.plan_angle(direction)
    if abs(angle) > 1e-9:
        z_axis = Line.CreateBound(point, point + XYZ.BasisZ)
        ElementTransformUtils.RotateElement(doc, instance.Id, z_axis, angle)

    if view_based or not TILT_ARROWS_TO_SLOPE:
        return
    tilt = core.tilt_angle(direction)
    if abs(tilt) > 1e-9:
        flow_plan = XYZ(direction[0], direction[1], 0.0).Normalize()
        left = XYZ.BasisZ.CrossProduct(flow_plan)
        tilt_axis = Line.CreateBound(point, point + left)
        ElementTransformUtils.RotateElement(doc, instance.Id, tilt_axis, tilt)


def get_location_endpoints(pipe):
    """Return the pipe's straight location line endpoints as mm tuples.

    Returns:
        tuple | None: ((start, end) in mm) or None when the pipe has no
        usable straight location line.
    """
    location = pipe.Location
    curve = getattr(location, 'Curve', None)
    if not isinstance(curve, Line):
        return None
    try:
        start = curve.GetEndPoint(0)
        end = curve.GetEndPoint(1)
    except Exception:
        return None
    return _xyz_to_mm(start), _xyz_to_mm(end)


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------
_SKIP_REASONS = {
    core.TOO_SHORT: 'shorter than the minimum length',
    core.VERTICAL: 'vertical pipe',
    core.FLAT: 'no meaningful slope',
}


def process_pipe(pipe, symbol, view, view_based, existing_points, counts,
                 skipped_details):
    """Place the arrows for one pipe; returns nothing, updates the tallies."""
    endpoints = get_location_endpoints(pipe)
    if endpoints is None:
        counts['invalid'] += 1
        skipped_details.append((_eid(pipe.Id), 'no straight location line'))
        return
    start, end = endpoints

    status = core.classify_pipe(start, end, CONFIG)
    if status != core.SLOPED:
        counts['invalid'] += 1
        skipped_details.append((_eid(pipe.Id), _SKIP_REASONS[status]))
        return
    counts['sloped'] += 1

    high, low = core.oriented_endpoints(start, end)
    direction = core.flow_direction(start, end)
    stations = core.arrow_stations(core.distance(high, low), CONFIG)

    for point_mm in core.arrow_points(high, low, stations):
        if core.is_near_existing(point_mm, existing_points,
                                 CONFIG['duplicate_tolerance_mm']):
            counts['existing'] += 1
            continue
        point = _mm_to_xyz(point_mm)
        instance = place_arrow(symbol, point, view)
        rotate_arrow(instance, point, direction, view_based)
        existing_points.append(point_mm)
        counts['created'] += 1


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def generate_report(counts, skipped_details, failed_details):
    """Print the completion report and show the summary alert."""
    summary = ('Drainage Flow Arrow Placement Complete\n\n'
               'Pipes Checked       : {checked}\n'
               'Sloped Pipes        : {sloped}\n'
               'Arrows Created      : {created}\n'
               'Existing Skipped    : {existing}\n'
               'Invalid/Skipped     : {invalid}\n'
               'Failed              : {failed}').format(**counts)

    output.print_md('## Drainage Flow Arrows')
    output.print_md('```\n{}\n```'.format(summary))

    if skipped_details:
        output.print_md('## Skipped pipes')
        for pipe_id, reason in skipped_details:
            output.print_md('- Pipe {} - {}'.format(pipe_id, reason))

    if failed_details:
        output.print_md('## Failed pipes')
        for pipe_id, message in failed_details:
            output.print_md('- Pipe {} - {}'.format(pipe_id, message))

    forms.alert(summary, title=TITLE)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def main():
    """Entry point that wires the full workflow together."""
    view = doc.ActiveView

    # Collect the visible pipes and group them by system.
    pipes = get_visible_pipes()
    present = get_present_systems(pipes)
    if not present:
        forms.alert('No pipes are visible in the active view.', title=TITLE)
        return

    # A flow-arrow family must exist before anything else is asked.
    symbols = find_arrow_symbols()
    if not symbols:
        forms.alert(
            'No flow-arrow family found in this project.\n\n'
            'Load a flow-arrow family (family or type name containing '
            '"{}") and run the tool again.'.format(
                '" or "'.join(ARROW_NAME_KEYWORDS)),
            title=TITLE)
        return

    # Which systems drain, and which arrow type to place.
    selected_labels = select_drainage_systems(present)
    if not selected_labels:
        logger.debug('System selection cancelled.')
        return
    symbol = select_arrow_symbol(symbols)
    if symbol is None:
        logger.debug('Arrow type selection cancelled.')
        return
    view_based = (symbol.Family.FamilyPlacementType ==
                  FamilyPlacementType.ViewBased)

    # Existing arrows anywhere near the work decide the duplicate skips.
    existing_points = collect_existing_arrow_points(symbols, symbol, view)

    work_pipes = [pipe for label in selected_labels for pipe in present[label]]
    counts = {'checked': len(work_pipes), 'sloped': 0, 'created': 0,
              'existing': 0, 'invalid': 0, 'failed': 0}
    skipped_details = []
    failed_details = []

    # One transaction for the whole run - a single undo step. A pipe that
    # fails is recorded and never stops the remaining pipes.
    with Transaction(doc, 'Drainage Flow Arrows') as trans:
        trans.Start()
        if not symbol.IsActive:
            symbol.Activate()
            doc.Regenerate()
        for pipe in work_pipes:
            try:
                process_pipe(pipe, symbol, view, view_based, existing_points,
                             counts, skipped_details)
            except Exception as ex:
                counts['failed'] += 1
                failed_details.append((_eid(pipe.Id), str(ex)))
                logger.debug('Pipe {} failed: {}'.format(_eid(pipe.Id), ex))
        trans.Commit()

    generate_report(counts, skipped_details, failed_details)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        logger.error('Unhandled error: {}'.format(exc))
        forms.alert('Unexpected error:\n{}'.format(exc), title=TITLE)
