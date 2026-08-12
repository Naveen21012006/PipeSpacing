# -*- coding: utf-8 -*-
"""Drainage Flow Arrows - place flow arrows on sloped drainage pipes.

Collects the pipes visible in the active view, lets the user pick which
piping systems are the drainage ones and which flow-arrow family type to
use, then places arrows on every inclined pipe so they point from the
higher endpoint to the lower endpoint - the drainage flow direction.
Preselecting pipes before launching scopes the run to just those pipes
and skips the system picker - the selection is the filter.

Shift+click opens the settings dialog (placement numbers) and asks the
arrow type again; a plain click reuses the saved numbers and the
remembered arrow type. Settings persist to
%APPDATA%/CKR/flow_arrow_settings.json (flowarrow_settings.py).

Per pipe the tool:
    * reads the location line and compares the endpoint elevations,
    * skips short, vertical and flat pipes (each with a recorded reason),
    * computes the arrow positions - one at the midpoint for short runs,
      several spread along long runs, clear of the ends,
    * skips any position that already has a flow arrow within the
      duplicate tolerance, so re-running the tool never doubles up,
    * places the chosen family and rotates it along the pipe: plan
      rotation always, plus the true downhill tilt for model families.

Two kinds of arrow family are supported. A model / generic-annotation
family is placed free and rotated to the flow. A Pipe Tag family (e.g.
MEP-Tag-Pipe Flow Arrow with its Flow Left / Flow Right types) is placed
as a pipe tag instead: the tag aligns itself to the pipe, and the tool
picks the Left or Right type per pipe so the head points downhill.

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
    ElementId,
    ElementTransformUtils,
    FamilyInstance,
    FamilyPlacementType,
    FamilySymbol,
    FilteredElementCollector,
    IndependentTag,
    Line,
    Reference,
    TagOrientation,
    Transaction,
    XYZ,
)
from Autodesk.Revit.DB.Plumbing import Pipe
from Autodesk.Revit.DB.Structure import StructuralType

import flowarrow_core as core
import flowarrow_settings as fa_settings

doc = revit.doc
uidoc = revit.uidoc
logger = script.get_logger()
output = script.get_output()

TITLE = 'Drainage Flow Arrows'
SETTINGS_PATH = os.path.join(os.environ.get('APPDATA', ''), 'CKR',
                             fa_settings.SETTINGS_FILE)


# ===========================================================================
# CONFIGURATION  -  edit here only; no rules are hard-coded in the logic
# ===========================================================================
# Placement rules (millimetres / degrees). Copied from
# flowarrow_core.DEFAULTS so the numbers are visible and editable here.
CONFIG = {
    'min_pipe_length_mm': 1000.0,      # ignore pipes shorter than this
    'multi_arrow_threshold_mm': 10000.0,  # one arrow per started 10 m
    'end_clearance_mm': 1000.0,        # keep arrows this far from the ends
    'duplicate_tolerance_mm': 300.0,   # existing arrow within this = skip
    'min_elevation_diff_mm': 5.0,      # smaller rise/fall counts as flat
    'vertical_angle_deg': 80.0,        # steeper than this = a riser, skip
    'rack_width_mm': 600.0,            # parallel pipes this close form a
                                       # rack and share arrow columns;
                                       # 0 turns the alignment off
    'parallel_angle_tol_deg': 5.0,     # how parallel rack mates must be
}

# A family symbol counts as a flow-arrow candidate when its family name or
# type name contains one of these (compared lower-cased). The candidates
# feed both the type picker and the existing-arrow detection, so keep the
# words specific enough not to match unrelated families.
ARROW_NAME_KEYWORDS = ('arrow', 'flow')

# Tilt model-family arrows to the pipe's true inclination (plan rotation
# is always applied). View-based annotation families cannot tilt.
TILT_ARROWS_TO_SLOPE = True

# Tag-based arrow families carry a Left and a Right type (e.g.
# MEP-Tag-Pipe Flow Arrow : Flow Left / Flow Right). The pair is found by
# these words in the type names (compared lower-cased), and the tool
# picks the side per pipe: flow falling toward the view's right gets the
# Right type, toward the left gets the Left type (straight-up screen flow
# reads bottom-to-top, so it counts as Right). If a project's family
# names the heads the other way round, set SWAP_LEFT_RIGHT to True.
LEFT_TYPE_KEYWORD = 'left'
RIGHT_TYPE_KEYWORD = 'right'
SWAP_LEFT_RIGHT = False

# In the free-instance path an arrow family is rotated assuming its
# graphics are drawn pointing right (+X). A type whose name matches
# LEFT_TYPE_KEYWORD is drawn pointing left instead, so it gets an extra
# half-turn - otherwise every one of its arrows lands 180 deg off.

# A tag anchors by its head point, which in some families is not the
# centre of the drawn arrow. When True, every placed tag is nudged so the
# centre of its drawn graphics sits exactly on the pipe point.
CENTER_TAG_GRAPHICS = True
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


def get_selected_pipes():
    """Return (selected element count, the Pipes among them).

    A preselection scopes the tool to just those pipes; anything else in
    the selection (fittings, valves grabbed by a window selection) is
    ignored. Zero selected elements means no preselection was made.
    """
    try:
        ids = list(uidoc.Selection.GetElementIds())
    except Exception:
        return 0, []
    pipes = []
    for element_id in ids:
        element = doc.GetElement(element_id)
        if isinstance(element, Pipe):
            pipes.append(element)
    return len(ids), pipes


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


def is_pipe_tag_symbol(symbol):
    """True when the symbol belongs to a Pipe Tag family.

    Pipe tags must be placed with IndependentTag.Create with the pipe as
    the host, so they switch the tool into tag mode. The category id is
    compared three independent ways because enum/id conversions differ
    between pyRevit engines - a silent False here would drop the tool
    into the free-instance path and misplace every arrow.
    """
    categories = []
    try:
        categories.append(symbol.Category)
    except Exception:
        pass
    try:
        categories.append(symbol.Family.FamilyCategory)
    except Exception:
        pass

    for category in categories:
        if category is None:
            continue
        try:
            if category.Id == ElementId(BuiltInCategory.OST_PipeTags):
                return True
        except Exception:
            pass
        try:
            if _eid(category.Id) == int(BuiltInCategory.OST_PipeTags):
                return True
        except Exception:
            pass
        try:
            if (category.Name or '').lower() == 'pipe tags':
                return True
        except Exception:
            pass
    return False


def _type_name(symbol):
    """Return a family type's name, trying the type-name parameter too."""
    name = _element_name(symbol)
    if name:
        return name
    for built_in in (BuiltInParameter.SYMBOL_NAME_PARAM,
                     BuiltInParameter.ALL_MODEL_TYPE_NAME):
        value = _param_string(symbol, built_in)
        if value:
            return value
    return ''


def resolve_left_right_types(symbol):
    """Return the (left, right) tag types of the chosen arrow family.

    The pair is matched by LEFT_TYPE_KEYWORD / RIGHT_TYPE_KEYWORD in the
    type names. A side with no matching type comes back as None and the
    chosen type stands in for it - main() reports that fallback loudly,
    because a single type cannot point both ways.
    """
    left = right = None
    for type_id in symbol.Family.GetFamilySymbolIds():
        sibling = doc.GetElement(type_id)
        name = _type_name(sibling).lower()
        if RIGHT_TYPE_KEYWORD in name:
            right = sibling
        elif LEFT_TYPE_KEYWORD in name:
            left = sibling
    if SWAP_LEFT_RIGHT:
        left, right = right, left
    return left, right


def is_left_named(symbol):
    """True when a free-instance arrow type is named as left-pointing.

    Mirrors the tag-pair rule: RIGHT_TYPE_KEYWORD in the name wins, then
    LEFT_TYPE_KEYWORD. The family name is included for families that put
    the direction there rather than on the type.
    """
    name = '{} {}'.format(_element_name(symbol.Family) or '',
                          _type_name(symbol)).lower()
    if RIGHT_TYPE_KEYWORD in name:
        return False
    return LEFT_TYPE_KEYWORD in name


def _tag_host_id(tag):
    """Return the tagged host-model element id, or None when unreadable."""
    try:
        ids = list(tag.GetTaggedLocalElementIds())
        if len(ids) == 1:
            return _eid(ids[0])
    except Exception:
        pass
    try:
        return _eid(tag.TaggedLocalElementId)   # older Revit API
    except Exception:
        return None


def collect_existing_arrow_points(symbols, chosen_symbol, view):
    """Return the locations (mm tuples) of existing flow-arrow instances.

    Instances of ANY candidate arrow family count, so re-running with a
    different type still detects the arrows already placed. View-specific
    arrows (annotation families and pipe tags) only exist per view, so
    those are scoped to the active view; model families are checked
    document-wide.

    Returns:
        dict: {'model': [3D mm points],
               'plan': [(mm point with z=0, host pipe id or None)]}.
        Tags and annotations sit on the view plane while pipes carry
        their true elevation, so those are compared in plan only; model
        arrows keep the full 3D comparison so stacked storeys never
        block each other. A tag's host pipe id makes the duplicate test
        per-pipe - rack columns sit within the tolerance of each other
        laterally and must not suppress each other across pipes.
    """
    family_ids = _candidate_family_ids(symbols)
    view_based = (chosen_symbol.Family.FamilyPlacementType ==
                  FamilyPlacementType.ViewBased)
    if view_based:
        collector = FilteredElementCollector(doc, view.Id)
    else:
        collector = FilteredElementCollector(doc)
    collector = collector.OfClass(FamilyInstance)

    points = {'model': [], 'plan': []}
    for instance in collector:
        try:
            symbol = instance.Symbol
            if symbol is None or _eid(symbol.Family.Id) not in family_ids:
                continue
            point = getattr(instance.Location, 'Point', None)
            if point is None:
                continue
            mm = _xyz_to_mm(point)
            if (symbol.Family.FamilyPlacementType ==
                    FamilyPlacementType.ViewBased):
                points['plan'].append(((mm[0], mm[1], 0.0), None))
            else:
                points['model'].append(mm)
        except Exception as ex:
            logger.debug('Existing-arrow scan skipped an instance: {}'
                         .format(ex))

    # Tag-based arrows are IndependentTags, not FamilyInstances. The
    # drawn graphics (bounding box centre) say where the arrow is; the
    # head position is only the anchor and may sit off the arrow.
    tag_collector = (FilteredElementCollector(doc, view.Id)
                     .OfClass(IndependentTag))
    for tag in tag_collector:
        try:
            symbol = doc.GetElement(tag.GetTypeId())
            if symbol is None or _eid(symbol.Family.Id) not in family_ids:
                continue
            bbox = tag.get_BoundingBox(view)
            if bbox is not None:
                mm = _xyz_to_mm((bbox.Min + bbox.Max) * 0.5)
            else:
                mm = _xyz_to_mm(tag.TagHeadPosition)
            points['plan'].append(((mm[0], mm[1], 0.0), _tag_host_id(tag)))
        except Exception as ex:
            logger.debug('Existing-arrow scan skipped a tag: {}'.format(ex))

    logger.debug('Found {} existing arrow point(s).'.format(
        len(points['model']) + len(points['plan'])))
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


def pick_tag_type(context, direction):
    """Return the Left or Right tag type whose head points down the flow.

    The flow vector is projected onto the view's right/up axes and
    flowarrow_core.arrow_side() decides the side, matching how Revit
    orients a rotates-with-component tag (readable, bottom-to-top when
    vertical on screen). A missing side falls back to the chosen type.
    """
    view = context['view']
    right, up = view.RightDirection, view.UpDirection
    dx = (direction[0] * right.X + direction[1] * right.Y +
          direction[2] * right.Z)
    dy = direction[0] * up.X + direction[1] * up.Y + direction[2] * up.Z
    side = core.arrow_side(dx, dy)
    tag_type = context[side]
    return tag_type if tag_type is not None else context['symbol']


def place_arrow_tag(tag_type, pipe, point, view):
    """Tag a pipe with the flow-arrow tag, head at the computed point.

    The tag family aligns itself to the pipe (rotates with component),
    so no rotation is applied here - the Left/Right type choice is what
    points the head downhill.
    """
    return IndependentTag.Create(
        doc, tag_type.Id, view.Id, Reference(pipe), False,
        TagOrientation.Horizontal, point)


def rotate_arrow(instance, point, direction, view_based, flip=False):
    """Rotate a placed arrow so it points down the drainage flow.

    The family is assumed to point along +X unrotated (the pyRevit /
    Revit family template convention); flip=True adds a half-turn for
    types drawn pointing left (see is_left_named). Plan rotation about Z
    aligns it with the flow bearing; model families are then tilted about
    the horizontal axis perpendicular to the flow so the arrow follows
    the pipe's true 3D inclination.
    """
    horizontal = math.hypot(direction[0], direction[1])
    if horizontal < 1e-9:
        return  # vertical pipes never reach here; nothing to align to

    angle = core.plan_angle(direction)
    if flip:
        angle += math.pi
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


def classify_work_pipe(pipe, counts, skipped_details):
    """Classify one pipe; return (pipe, high, low, direction) when it is
    sloped and should receive arrows, else None (with the tallies and
    skip reason recorded)."""
    endpoints = get_location_endpoints(pipe)
    if endpoints is None:
        counts['invalid'] += 1
        skipped_details.append((_eid(pipe.Id), 'no straight location line'))
        return None
    start, end = endpoints

    status = core.classify_pipe(start, end, CONFIG)
    if status != core.SLOPED:
        counts['invalid'] += 1
        skipped_details.append((_eid(pipe.Id), _SKIP_REASONS[status]))
        return None
    counts['sloped'] += 1

    high, low = core.oriented_endpoints(start, end)
    return pipe, high, low, core.flow_direction(start, end)


def _near_plan_arrow(flat_mm, pipe_id, plan_entries, tolerance):
    """Plan-distance duplicate test, per pipe where the host is known.

    Rack columns put arrows on neighbouring pipes within the tolerance
    of each other laterally, so an arrow with a known host pipe only
    suppresses new arrows on that same pipe. Hostless entries (free
    annotation instances) keep the plain spatial test.
    """
    for point, host in plan_entries:
        if host is not None and host != pipe_id:
            continue
        if core.distance(flat_mm, point) <= tolerance:
            return True
    return False


def place_pipe_arrows(pipe, direction, points_mm, context, existing, counts):
    """Place one pipe's arrows at the given points; updates the tallies.

    context carries the placement decisions made once in main():
    'symbol', 'view', 'view_based', 'tag_mode', 'instance_flip',
    'created_tags' and - in tag mode - the core.LEFT / core.RIGHT tag
    types. existing is the {'model', 'plan'} dict from
    collect_existing_arrow_points().
    """
    tolerance = CONFIG['duplicate_tolerance_mm']
    pipe_id = _eid(pipe.Id)

    for point_mm in points_mm:
        flat_mm = (point_mm[0], point_mm[1], 0.0)
        if (core.is_near_existing(point_mm, existing['model'], tolerance) or
                _near_plan_arrow(flat_mm, pipe_id, existing['plan'],
                                 tolerance)):
            counts['existing'] += 1
            continue
        point = _mm_to_xyz(point_mm)
        if context['tag_mode']:
            tag_type = pick_tag_type(context, direction)
            tag = place_arrow_tag(tag_type, pipe, point, context['view'])
            context['created_tags'].append((tag, point))
            existing['plan'].append((flat_mm, pipe_id))
        else:
            instance = place_arrow(context['symbol'], point, context['view'])
            rotate_arrow(instance, point, direction, context['view_based'],
                         context['instance_flip'])
            if context['view_based']:
                existing['plan'].append((flat_mm, None))
            else:
                existing['model'].append(point_mm)
        counts['created'] += 1


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def generate_report(counts, skipped_details, failed_details, notes=None):
    """Print the completion report and show the summary alert.

    notes are short mode lines ("placed as pipe tags, Left type: ...") -
    they make a silent fallback visible instead of plausible-looking.
    """
    summary = ('Drainage Flow Arrow Placement Complete\n\n'
               'Pipes Checked       : {checked}\n'
               'Sloped Pipes        : {sloped}\n'
               'Arrows Created      : {created}\n'
               'Existing Skipped    : {existing}\n'
               'Invalid/Skipped     : {invalid}\n'
               'Failed              : {failed}').format(**counts)

    output.print_md('## Drainage Flow Arrows')
    output.print_md('```\n{}\n```'.format(summary))
    for note in (notes or []):
        output.print_md('- {}'.format(note))

    if skipped_details:
        output.print_md('## Skipped pipes')
        for pipe_id, reason in skipped_details:
            output.print_md('- Pipe {} - {}'.format(pipe_id, reason))

    if failed_details:
        output.print_md('## Failed pipes')
        for pipe_id, message in failed_details:
            output.print_md('- Pipe {} - {}'.format(pipe_id, message))

    alert_text = summary
    if notes:
        alert_text += '\n\n' + '\n'.join(notes)
    forms.alert(alert_text, title=TITLE)


# ---------------------------------------------------------------------------
# Settings dialog (Shift+click)
# ---------------------------------------------------------------------------
_DIALOG_FIELDS = (
    ('MinLengthBox', 'min_pipe_length_mm', 'Minimum Pipe Length'),
    ('ThresholdBox', 'multi_arrow_threshold_mm', 'Multi-Arrow Threshold'),
    ('ClearanceBox', 'end_clearance_mm', 'End Clearance'),
    ('ToleranceBox', 'duplicate_tolerance_mm', 'Duplicate Tolerance'),
    ('RackWidthBox', 'rack_width_mm', 'Rack Width'),
)


class FlowArrowsDialog(forms.WPFWindow):
    """The Shift+click settings dialog.

    self.result holds the validated numbers dict after Save & Run, or
    None when cancelled.
    """

    def __init__(self, values):
        xaml = os.path.join(_BUNDLE_DIR, 'FlowArrowsDialog.xaml')
        forms.WPFWindow.__init__(self, xaml)
        self.result = None
        for box_name, key, _label in _DIALOG_FIELDS:
            getattr(self, box_name).Text = '{0:g}'.format(values[key])
        self.SaveButton.Click += self._on_save
        self.CancelButton.Click += self._on_cancel

    def _on_save(self, sender, args):
        numbers = {}
        problems = []
        for box_name, key, label in _DIALOG_FIELDS:
            value = fa_settings.sanitize_number(
                getattr(self, box_name).Text, None,
                fa_settings.MINIMUMS.get(key, 0.0))
            if value is None:
                problems.append(label)
            else:
                numbers[key] = value
        if problems:
            forms.alert('Not a valid number:\n- {}'.format(
                '\n- '.join(problems)), title=TITLE)
            return
        self.result = numbers
        self.Close()

    def _on_cancel(self, sender, args):
        self.result = None
        self.Close()


def show_settings_dialog(values):
    """Show the dialog prefilled with values; return the numbers or None."""
    dialog = FlowArrowsDialog(values)
    dialog.ShowDialog()
    return dialog.result


def is_config_run():
    """True when the button was Shift+clicked."""
    try:
        return bool(__shiftclick__)  # noqa: F821 - pyRevit injects it
    except NameError:
        return False


def save_settings(stored):
    """Persist the settings; a write failure never blocks the run."""
    try:
        fa_settings.save(SETTINGS_PATH, stored)
    except Exception as ex:
        logger.debug('Settings save failed: {}'.format(ex))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def main():
    """Entry point that wires the full workflow together."""
    view = doc.ActiveView

    # Saved settings override the CONFIG numbers. Shift+click opens the
    # dialog to change them (and asks the arrow type again).
    stored = fa_settings.load(SETTINGS_PATH)
    CONFIG.update(fa_settings.merge_numbers(CONFIG, stored))
    config_run = is_config_run()
    if config_run:
        numbers = show_settings_dialog(CONFIG)
        if numbers is None:
            logger.debug('Settings dialog cancelled.')
            return
        CONFIG.update(numbers)
        stored.update(numbers)
        save_settings(stored)

    # A preselection scopes the run to just those pipes. Otherwise every
    # visible pipe is collected and the system picker filters them.
    selection_count, selected_pipes = get_selected_pipes()
    if selection_count and not selected_pipes:
        forms.alert('The current selection contains no pipes.\n\n'
                    'Select the pipes to arrow, or clear the selection to '
                    'process every pipe in the view.', title=TITLE)
        return

    present = None
    if not selected_pipes:
        pipes = get_visible_pipes()
        present = get_present_systems(pipes)
        if not present:
            forms.alert('No pipes are visible in the active view.',
                        title=TITLE)
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

    # The work list: the preselected pipes as-is, or the visible pipes of
    # the drainage systems the user ticks.
    if selected_pipes:
        work_pipes = selected_pipes
        scope_note = ('Scope: {} preselected pipe(s) (from {} selected '
                      'element(s)).'.format(len(selected_pipes),
                                            selection_count))
    else:
        selected_labels = select_drainage_systems(present)
        if not selected_labels:
            logger.debug('System selection cancelled.')
            return
        work_pipes = [pipe for label in selected_labels
                      for pipe in present[label]]
        scope_note = ('Scope: {} pipe(s) in the active view from {} '
                      'system(s).'.format(len(work_pipes),
                                          len(selected_labels)))

    # The remembered arrow type skips the picker on a plain click; a
    # Shift+click, or the type no longer existing in the project, asks
    # again and the new choice is remembered.
    symbol = None
    remembered = fa_settings.remembered_type_label(stored)
    if not config_run and remembered and remembered in symbols:
        symbol = symbols[remembered]
        type_note = ('Arrow type: {} (remembered - Shift+click the button '
                     'to change it).'.format(remembered))
    else:
        symbol = select_arrow_symbol(symbols)
        if symbol is None:
            logger.debug('Arrow type selection cancelled.')
            return
        label = '{} : {}'.format(_element_name(symbol.Family),
                                 _type_name(symbol))
        type_note = 'Arrow type: {}.'.format(label)
        if stored.get(fa_settings.TYPE_LABEL_KEY) != label:
            stored[fa_settings.TYPE_LABEL_KEY] = label
            save_settings(stored)

    # A Pipe Tag family switches the tool into tag mode: the tag aligns
    # itself to the pipe and the Left/Right type points the head downhill.
    context = {
        'symbol': symbol,
        'view': view,
        'view_based': (symbol.Family.FamilyPlacementType ==
                       FamilyPlacementType.ViewBased),
        'tag_mode': is_pipe_tag_symbol(symbol),
        'created_tags': [],
        'instance_flip': False,
        core.LEFT: None,
        core.RIGHT: None,
    }
    notes = [scope_note, type_note]
    if context['tag_mode']:
        left_type, right_type = resolve_left_right_types(symbol)
        context[core.LEFT] = left_type
        context[core.RIGHT] = right_type
        notes.append('Placed as pipe tags. Left type: {} / Right type: {}.'
                     .format(_type_name(left_type) if left_type
                             else 'NOT FOUND',
                             _type_name(right_type) if right_type
                             else 'NOT FOUND'))
        if left_type is None or right_type is None:
            notes.append('WARNING: the Left/Right type pair was not '
                         'resolved, so every arrow uses the selected type '
                         'and pipes draining the other way will point '
                         'wrong. Check the type names against '
                         'LEFT_TYPE_KEYWORD / RIGHT_TYPE_KEYWORD in the '
                         'config.')
    else:
        notes.append('Placed as free instances of {} : {}.'.format(
            _element_name(symbol.Family), _type_name(symbol)))
        context['instance_flip'] = is_left_named(symbol)
        if context['instance_flip']:
            notes.append('Type is named "{}" - its graphics are assumed '
                         'drawn pointing left and were rotated an extra '
                         '180 degrees.'.format(LEFT_TYPE_KEYWORD))

    # Existing arrows anywhere near the work decide the duplicate skips.
    existing = collect_existing_arrow_points(symbols, symbol, view)

    counts = {'checked': len(work_pipes), 'sloped': 0, 'created': 0,
              'existing': 0, 'invalid': 0, 'failed': 0}
    skipped_details = []
    failed_details = []

    # Classification is read-only, so it runs before the transaction -
    # the rack clustering needs every sloped pipe at once to align the
    # arrow columns across parallel neighbours.
    sloped = []
    for pipe in work_pipes:
        try:
            entry = classify_work_pipe(pipe, counts, skipped_details)
        except Exception as ex:
            counts['failed'] += 1
            failed_details.append((_eid(pipe.Id), str(ex)))
            logger.debug('Pipe {} failed: {}'.format(_eid(pipe.Id), ex))
            continue
        if entry is not None:
            sloped.append(entry)

    points_per_pipe, racks = core.bundle_arrow_points(
        [(high, low) for _pipe, high, low, _direction in sloped], CONFIG)
    if racks:
        notes.append('Aligned {} rack(s) of parallel pipes into shared '
                     'arrow columns.'.format(len(racks)))

    # One transaction for the whole run - a single undo step. A pipe that
    # fails is recorded and never stops the remaining pipes.
    with Transaction(doc, 'Drainage Flow Arrows') as trans:
        trans.Start()
        activated = False
        for placed_type in (symbol, context[core.LEFT], context[core.RIGHT]):
            if placed_type is not None and not placed_type.IsActive:
                placed_type.Activate()
                activated = True
        if activated:
            doc.Regenerate()
        for entry, points_mm in zip(sloped, points_per_pipe):
            pipe, _high, _low, direction = entry
            try:
                place_pipe_arrows(pipe, direction, points_mm, context,
                                  existing, counts)
            except Exception as ex:
                counts['failed'] += 1
                failed_details.append((_eid(pipe.Id), str(ex)))
                logger.debug('Pipe {} failed: {}'.format(_eid(pipe.Id), ex))

        # A tag anchors by its head, which in this arrow family is not
        # the centre of the drawn arrow - nudge each placed tag so its
        # graphics sit exactly on the pipe point (in the view plane).
        if (context['tag_mode'] and CENTER_TAG_GRAPHICS and
                context['created_tags']):
            doc.Regenerate()
            view_dir = view.ViewDirection
            for tag, target in context['created_tags']:
                try:
                    bbox = tag.get_BoundingBox(view)
                    if bbox is None:
                        continue
                    centre = (bbox.Min + bbox.Max) * 0.5
                    raw = target - centre
                    offset = raw - view_dir.Multiply(raw.DotProduct(view_dir))
                    if offset.GetLength() > 1e-6:
                        ElementTransformUtils.MoveElement(
                            doc, tag.Id, offset)
                except Exception as ex:
                    logger.debug('Tag centring skipped: {}'.format(ex))
        trans.Commit()

    generate_report(counts, skipped_details, failed_details, notes)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        logger.error('Unhandled error: {}'.format(exc))
        forms.alert('Unexpected error:\n{}'.format(exc), title=TITLE)
