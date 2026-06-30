# -*- coding: utf-8 -*-
"""Pipe Insulation - Auto Apply & Update.

Creates or updates pipe insulation for the visible pipes in the active floor
plan. The user picks which piping systems to insulate (every system present
in the view is listed) and which standard to apply - either auto-matching
each pipe to its own system, or forcing one standard (e.g. apply the CCWS
thicknesses to another system for a particular project). Connected pipe
fittings and accessories (valves) are insulated too, so a whole run is
covered rather than just its straight pipes.

For each pipe the tool:
    * reads its piping system (CCWS / CCWR / HWS / HWR / Condensate Drain),
    * reads its Nominal Diameter (DN), converted to millimetres,
    * looks up the required insulation thickness from INSULATION_STANDARDS,
    * creates insulation if missing, updates the thickness if it is wrong,
      or leaves the pipe untouched if it is already correct.

Each fitting/valve inherits the thickness of the thickest pipe it connects
to (so a reducer takes the larger size).

All business rules live in the CONFIGURATION section below
(INSULATION_STANDARDS / SYSTEM_ALIASES). Changing the standards only means
editing that section - no processing code needs to change, and new systems
can be added without touching the core functions.

Author: Naveen
Target: Revit 2024 / pyRevit / IronPython
"""

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
from pyrevit import revit, DB, forms, script

from Autodesk.Revit.DB import (
    FilteredElementCollector,
    BuiltInCategory,
    BuiltInParameter,
    ElementId,
    Transaction,
    ViewPlan,
)
from Autodesk.Revit.DB.Plumbing import (
    Pipe,
    PipeInsulation,
    PipeInsulationType,
)

doc = revit.doc
uidoc = revit.uidoc
logger = script.get_logger()


# ===========================================================================
# CONFIGURATION  -  edit here only; no rules are hard-coded in the logic
# ===========================================================================
# Required insulation thickness (mm) per piping system, expressed as a list of
# (max_nominal_diameter_mm, insulation_thickness_mm) thresholds in ascending
# order. For a pipe of diameter DN, the FIRST row whose max_dn >= DN applies.
# A pipe whose DN exceeds every row of its system is treated as unsupported
# (skipped). Add or edit rows/systems freely - the processing logic reads
# everything from here.
INSULATION_STANDARDS = {
    "CCWS": [
        (40, 25),
        (100, 30),
        (150, 40),
        (9999, 50),
    ],
    "CCWR": [
        (40, 25),
        (100, 30),
        (150, 40),
        (9999, 50),
    ],
    "HWS": [
        (40, 30),
        (150, 40),
        (9999, 50),
    ],
    "HWR": [
        (40, 30),
        (150, 40),
        (9999, 50),
    ],
    "CD": [
        (50, 19),
    ],
}

# Map a system abbreviation or system-type name (compared UPPER-CASED) to a
# canonical code used by INSULATION_STANDARDS. Exact abbreviations (CCWS, HWS,
# ...) match automatically and need no entry here. Add aliases when a project
# names its systems differently (e.g. a full name instead of an abbreviation).
SYSTEM_ALIASES = {
    "CONDENSATE DRAIN": "CD",
    "CONDENSATE": "CD",
}

# Pipe Insulation Type to use when creating insulation. Leave as None to use
# the first insulation type found in the project, or set a type name string.
INSULATION_TYPE_NAME = None

# Two thicknesses within this tolerance (mm) are treated as equal.
THICKNESS_TOLERANCE_MM = 0.5
# ===========================================================================

# Sentinel + label for the "match each pipe to its own system" option in the
# standard picker (see select_standard()). Any other value is a forced
# standard code from INSULATION_STANDARDS applied to every selected pipe.
AUTO_STANDARD_CODE = 'AUTO'
AUTO_STANDARD_LABEL = 'Auto - match each pipe to its own system'


# ---------------------------------------------------------------------------
# Version / unit / parameter helpers
# ---------------------------------------------------------------------------
def _eid(element_id):
    """Return a stable integer id across Revit versions.

    Revit 2024+ exposes the Int64 ``ElementId.Value`` and deprecates
    ``IntegerValue``; older versions only have ``IntegerValue``.
    """
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
# Step 3 - Collect visible pipes
# ---------------------------------------------------------------------------
def get_visible_pipes():
    """Return all Pipe elements visible in the active view.

    Returns:
        list[Pipe]: Pipes shown in the active floor plan (possibly empty).
    """
    collector = (FilteredElementCollector(doc, doc.ActiveView.Id)
                 .OfCategory(BuiltInCategory.OST_PipeCurves)
                 .WhereElementIsNotElementType())
    pipes = [el for el in collector if isinstance(el, Pipe)]
    logger.debug('Found {} visible pipe(s).'.format(len(pipes)))
    return pipes


# ---------------------------------------------------------------------------
# Steps 4-5 - System type
# ---------------------------------------------------------------------------
def _match_system_code(text):
    """Map a system abbreviation/name to a canonical code, or None."""
    if not text:
        return None
    key = text.strip().upper()
    if key in INSULATION_STANDARDS:
        return key
    if key in SYSTEM_ALIASES:
        return SYSTEM_ALIASES[key]
    # Tolerate suffixed names like "CCWS-01" or "HWS Supply".
    for code in INSULATION_STANDARDS:
        if key.startswith(code):
            return code
    return None


def get_pipe_system(pipe):
    """Return the canonical system code for a pipe, or None if unsupported.

    Checks (in order) the System Abbreviation, the Piping System Type name,
    the System Name and the System Classification, matching each against the
    configured codes/aliases.

    Args:
        pipe (Pipe): The pipe element.

    Returns:
        str | None: A key of INSULATION_STANDARDS, or None when unsupported.
    """
    candidates = []

    candidates.append(_param_string(
        pipe, BuiltInParameter.RBS_SYSTEM_ABBREVIATION_PARAM))

    type_param = pipe.get_Parameter(
        BuiltInParameter.RBS_PIPING_SYSTEM_TYPE_PARAM)
    if type_param is not None:
        type_element = doc.GetElement(type_param.AsElementId())
        if type_element is not None:
            candidates.append(_element_name(type_element))

    candidates.append(_param_string(
        pipe, BuiltInParameter.RBS_SYSTEM_NAME_PARAM))
    candidates.append(_param_string(
        pipe, BuiltInParameter.RBS_SYSTEM_CLASSIFICATION_PARAM))

    for candidate in candidates:
        code = _match_system_code(candidate)
        if code:
            return code
    return None


# ---------------------------------------------------------------------------
# Step 6 - Nominal diameter
# ---------------------------------------------------------------------------
def get_nominal_diameter(pipe):
    """Return the pipe Nominal Diameter (DN) in millimetres, or None.

    Uses the pipe's nominal "Diameter" parameter only (never OD/ID/insulated
    diameter) and converts from internal units to millimetres.
    """
    param = pipe.get_Parameter(BuiltInParameter.RBS_PIPE_DIAMETER_PARAM)
    if param is None:
        return None
    return internal_to_mm(param.AsDouble())


# ---------------------------------------------------------------------------
# Step 7 - Required insulation from configuration
# ---------------------------------------------------------------------------
def get_required_insulation(system_code, dn_mm):
    """Return the required insulation thickness (mm) from the standards.

    Args:
        system_code (str): Canonical system code (key of INSULATION_STANDARDS).
        dn_mm (float): Nominal diameter in millimetres.

    Returns:
        int | None: Required thickness (mm), or None when the system is
        unknown or the diameter falls outside the configured range.
    """
    table = INSULATION_STANDARDS.get(system_code)
    if not table or dn_mm is None:
        return None
    dn = round(dn_mm)  # nominal sizes are whole mm; absorb tiny float drift
    for max_dn, thickness in table:
        if dn <= max_dn:
            return thickness
    return None


# ---------------------------------------------------------------------------
# Step 8 - Existing insulation
# ---------------------------------------------------------------------------
def get_existing_insulation(host):
    """Return the PipeInsulation hosted by an element, or None.

    The host may be a pipe, a pipe fitting or a pipe accessory (valve).
    """
    try:
        insulation_ids = PipeInsulation.GetInsulationIds(doc, host.Id)
    except Exception as ex:
        logger.debug('Insulation lookup failed for {}: {}'.format(
            _eid(host.Id), ex))
        return None
    for ins_id in insulation_ids:
        element = doc.GetElement(ins_id)
        if isinstance(element, PipeInsulation):
            return element
    return None


# ---------------------------------------------------------------------------
# Step 9 - Create insulation
# ---------------------------------------------------------------------------
def create_insulation(host, insulation_type_id, thickness_mm):
    """Create insulation of the given thickness (mm) on a host element.

    The host may be a pipe, a pipe fitting or a pipe accessory (valve).
    """
    PipeInsulation.Create(
        doc, host.Id, insulation_type_id, mm_to_internal(thickness_mm))


# ---------------------------------------------------------------------------
# Step 10 - Update insulation
# ---------------------------------------------------------------------------
def update_insulation(insulation, thickness_mm):
    """Set an existing insulation to the given thickness (mm).

    Tries to set the thickness parameter directly; if that is unavailable or
    read-only, recreates the insulation with the same type on the same host
    pipe (guaranteed correct).
    """
    thickness_ft = mm_to_internal(thickness_mm)

    bip = getattr(BuiltInParameter, 'RBS_INSULATION_THICKNESS', None)
    if bip is not None:
        param = insulation.get_Parameter(bip)
        if param is not None and not param.IsReadOnly:
            param.Set(thickness_ft)
            return

    # Reliable fallback: recreate with the same type on the same host pipe.
    host_id = insulation.HostElementId
    type_id = insulation.GetTypeId()
    doc.Delete(insulation.Id)
    PipeInsulation.Create(doc, host_id, type_id, thickness_ft)


# ---------------------------------------------------------------------------
# Insulation type lookup
# ---------------------------------------------------------------------------
def _get_default_insulation_type_id():
    """Return the configured (or first available) PipeInsulationType id."""
    types = list(FilteredElementCollector(doc).OfClass(PipeInsulationType))
    if not types:
        return None
    if INSULATION_TYPE_NAME:
        for insulation_type in types:
            if _element_name(insulation_type) == INSULATION_TYPE_NAME:
                return insulation_type.Id
        logger.warning(
            "Insulation type '{}' not found; using the first available.".format(
                INSULATION_TYPE_NAME))
    return types[0].Id


# ---------------------------------------------------------------------------
# Fittings & valves - insulate the whole run, not just the pipes
# ---------------------------------------------------------------------------
def get_visible_pipe_parts():
    """Return visible pipe fittings and accessories (valves) in the view.

    Returns:
        list: Pipe fitting and pipe accessory elements shown in the view.
    """
    parts = []
    for category in (BuiltInCategory.OST_PipeFitting,
                     BuiltInCategory.OST_PipeAccessory):
        collector = (FilteredElementCollector(doc, doc.ActiveView.Id)
                     .OfCategory(category)
                     .WhereElementIsNotElementType())
        parts.extend(collector)
    logger.debug('Found {} fitting/accessory element(s).'.format(len(parts)))
    return parts


def _connector_manager(element):
    """Return the ConnectorManager of a pipe/fitting/accessory, or None."""
    try:
        if isinstance(element, Pipe):
            return element.ConnectorManager
        mep_model = getattr(element, 'MEPModel', None)
        if mep_model is not None:
            return mep_model.ConnectorManager
    except Exception:
        pass
    return None


def get_connected_pipes(element):
    """Return the pipes directly connected to a fitting/accessory.

    Args:
        element: A pipe fitting or accessory.

    Returns:
        list[Pipe]: Distinct pipes joined to the element's connectors.
    """
    pipes = []
    seen = set()
    manager = _connector_manager(element)
    if manager is None:
        return pipes
    for connector in manager.Connectors:
        try:
            refs = connector.AllRefs
        except Exception:
            continue
        for ref in refs:
            owner = ref.Owner
            if isinstance(owner, Pipe):
                key = _eid(owner.Id)
                if key not in seen:
                    seen.add(key)
                    pipes.append(owner)
    return pipes


def _thickness_for_pipe(pipe, standard):
    """Return (thickness_mm, skip_reason) for a pipe under a chosen standard.

    Args:
        pipe (Pipe): The pipe element.
        standard (str): AUTO_STANDARD_CODE to use the pipe's own detected
            system, or a forced standard code (e.g. 'CCWS') applied regardless
            of the pipe's actual system.

    Returns:
        tuple: (thickness_mm, skip_reason). thickness_mm is None when the pipe
        should be skipped, and skip_reason then explains why.
    """
    dn_mm = get_nominal_diameter(pipe)
    if dn_mm is None:
        return None, 'no nominal diameter'

    if standard == AUTO_STANDARD_CODE:
        code = get_pipe_system(pipe)
        if code is None:
            return None, 'system has no matching standard'
    else:
        code = standard

    required = get_required_insulation(code, dn_mm)
    if required is None:
        return None, '{} DN{:.0f} not in standards'.format(code, dn_mm)
    return required, None


def _part_target_thickness(element, selected_labels, standard):
    """Return the insulation thickness (mm) for a fitting/valve, or None.

    A fitting or accessory inherits the insulation of the pipes it joins,
    limited to pipes whose system label is in selected_labels, and is
    insulated to the thickest of them (e.g. a reducer takes the larger size)
    using the chosen standard. Returns None when no connected pipe belongs to
    a selected system (the part is then left out of this run entirely).

    Args:
        element: A pipe fitting or accessory.
        selected_labels (set): System labels chosen by the user.
        standard (str): AUTO_STANDARD_CODE or a forced standard code.
    """
    thicknesses = []
    for pipe in get_connected_pipes(element):
        if get_pipe_system_label(pipe) not in selected_labels:
            continue
        thickness, _reason = _thickness_for_pipe(pipe, standard)
        if thickness is not None:
            thicknesses.append(thickness)
    return max(thicknesses) if thicknesses else None


def _apply_insulation(host, required, reason, insulation_type_id, counts,
                      skipped_details, error_details, kind):
    """Create/update/leave insulation on one host element and tally results.

    Args:
        host: The element to insulate (pipe, fitting or accessory).
        required (int | None): Required thickness (mm); None means skip.
        reason (str | None): Skip reason when required is None.
        insulation_type_id (ElementId): Insulation type to create with.
        counts (dict): Tallies, updated in place.
        skipped_details (list): Accumulates (id, reason) for skipped hosts.
        error_details (list): Accumulates (id, message) for failed hosts.
        kind (str): 'pipes' or 'parts', for the per-type tally.
    """
    counts['total'] += 1
    counts[kind] += 1
    host_id = _eid(host.Id)
    try:
        if required is None:
            counts['skipped'] += 1
            skipped_details.append((host_id, reason))
            return

        existing = get_existing_insulation(host)
        if existing is None:
            create_insulation(host, insulation_type_id, required)
            counts['created'] += 1
        else:
            current = internal_to_mm(existing.Thickness)
            if abs(current - required) <= THICKNESS_TOLERANCE_MM:
                counts['correct'] += 1
            else:
                update_insulation(existing, required)
                counts['updated'] += 1
    except Exception as ex:
        counts['errors'] += 1
        error_details.append((host_id, str(ex)))
        logger.error('Element {} failed: {}'.format(host_id, ex))


# ---------------------------------------------------------------------------
# Step 13 - Completion report
# ---------------------------------------------------------------------------
def generate_report(counts, skipped_details, error_details):
    """Show the completion report and print details to the output window.

    Args:
        counts (dict): Tallies with keys total, created, updated, correct,
            skipped, errors.
        skipped_details (list[tuple]): (pipe_id, reason) for skipped pipes.
        error_details (list[tuple]): (pipe_id, message) for failed pipes.
    """
    summary = (
        'Pipe Insulation - Completion Report\n\n'
        'Elements checked:       {total}  (pipes {pipes}, '
        'fittings/valves {parts})\n'
        'New insulation created: {created}\n'
        'Insulation updated:     {updated}\n'
        'Already correct:        {correct}\n'
        'Unsupported / skipped:  {skipped}\n'
        'Errors:                 {errors}'.format(**counts))

    output = script.get_output()
    output.print_md('# Pipe Insulation - Completion Report')
    output.print_md(
        '| Result | Count |\n'
        '| :-- | --: |\n'
        '| Elements checked | {total} |\n'
        '| &nbsp;&nbsp;Pipes | {pipes} |\n'
        '| &nbsp;&nbsp;Fittings / valves | {parts} |\n'
        '| New insulation created | {created} |\n'
        '| Existing insulation updated | {updated} |\n'
        '| Already correct | {correct} |\n'
        '| Unsupported / skipped | {skipped} |\n'
        '| Errors | {errors} |'.format(**counts))

    if skipped_details:
        output.print_md('## Unsupported / skipped pipes')
        for pipe_id, reason in skipped_details:
            output.print_md('- Pipe {} - {}'.format(pipe_id, reason))

    if error_details:
        output.print_md('## Errors')
        for pipe_id, message in error_details:
            output.print_md('- Pipe {} - {}'.format(pipe_id, message))

    forms.alert(summary, title='Pipe Insulation')


# ---------------------------------------------------------------------------
# System selection
# ---------------------------------------------------------------------------
def get_pipe_system_label(pipe):
    """Return a display label for a pipe's piping system (every system).

    Unlike get_pipe_system() (which maps to a configured standard code or
    None), this returns the pipe's actual system identifier so that ALL
    systems present in the model can be listed - the System Type name, then
    the abbreviation / system name / classification as fallbacks.
    """
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
    """Group visible pipes by piping system label (all systems, configured or
    not).

    Args:
        pipes (list[Pipe]): The visible pipes.

    Returns:
        dict: {system_label: [pipes]} for every system found in the view.
    """
    present = {}
    for pipe in pipes:
        present.setdefault(get_pipe_system_label(pipe), []).append(pipe)
    return present


def select_pipe_systems(present):
    """Ask the user which piping systems to insulate (all systems listed).

    Args:
        present (dict): {system_label: [pipes]} from get_present_systems().

    Returns:
        set | None: Chosen system labels, or None if the user cancelled.
    """
    # Build a friendly "System  (N pipes)" label per detected system.
    display_to_label = {}
    for system_label in sorted(present):
        count = len(present[system_label])
        display = '{}  ({} pipe{})'.format(
            system_label, count, '' if count == 1 else 's')
        display_to_label[display] = system_label

    chosen = forms.SelectFromList.show(
        sorted(display_to_label.keys()),
        title='Select Pipe Systems to Insulate',
        button_name='Next',
        multiselect=True)

    if not chosen:
        return None
    return set(display_to_label[display] for display in chosen)


def select_standard():
    """Ask which insulation standard to apply to the selected systems.

    Returns:
        str | None: AUTO_STANDARD_CODE to match each pipe to its own system, a
        forced standard code (e.g. 'CCWS'), or None if the user cancelled.
    """
    options = [AUTO_STANDARD_LABEL] + sorted(INSULATION_STANDARDS.keys())
    chosen = forms.SelectFromList.show(
        options,
        title='Select Insulation Standard to Apply',
        button_name='Apply Insulation',
        multiselect=False)

    if not chosen:
        return None
    if chosen == AUTO_STANDARD_LABEL:
        return AUTO_STANDARD_CODE
    return chosen


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def main():
    """Entry point that wires the full workflow together."""
    # Step 1: the tool operates on the active floor plan.
    view = doc.ActiveView
    if not isinstance(view, ViewPlan):
        forms.alert('Open a floor plan view and run the tool again.',
                    title='Pipe Insulation')
        return

    # Step 3: collect the visible pipes and group them by system (all systems).
    pipes = get_visible_pipes()
    present = get_present_systems(pipes)
    if not present:
        forms.alert('No pipes are visible in the active view.',
                    title='Pipe Insulation')
        return

    # An insulation type is required to create insulation.
    insulation_type_id = _get_default_insulation_type_id()
    if insulation_type_id is None:
        forms.alert(
            'No Pipe Insulation Type exists in this project. Create or load '
            'one, then run the tool again.', title='Pipe Insulation')
        return

    # Step A: choose which systems to insulate (every system in the view).
    selected_labels = select_pipe_systems(present)
    if not selected_labels:
        logger.debug('System selection cancelled.')
        return

    # Step B: choose which standard to apply - auto-match each pipe to its own
    # system, or force one standard (e.g. apply CCWS thicknesses to another
    # system for this project).
    standard = select_standard()
    if not standard:
        logger.debug('Standard selection cancelled.')
        return

    # Pipes of the chosen systems, and the fittings/valves that connect to
    # them (each with its inherited thickness under the chosen standard).
    work_pipes = [pipe for label in selected_labels for pipe in present[label]]
    work_parts = []
    for part in get_visible_pipe_parts():
        thickness = _part_target_thickness(part, selected_labels, standard)
        if thickness is not None:
            work_parts.append((part, thickness))

    # User experience: confirm before making any changes.
    standard_text = ('auto-matched standards' if standard == AUTO_STANDARD_CODE
                     else "the {} standard".format(standard))
    proceed = forms.alert(
        '{} pipe(s) and {} fitting(s)/valve(s) will be insulated using '
        '{}.\n\nProceed?'.format(
            len(work_pipes), len(work_parts), standard_text),
        title='Pipe Insulation', yes=True, no=True)
    if not proceed:
        return

    counts = {'total': 0, 'created': 0, 'updated': 0, 'correct': 0,
              'skipped': 0, 'errors': 0, 'pipes': 0, 'parts': 0}
    skipped_details = []
    error_details = []

    # Steps 8-12: process pipes, then their fittings/valves, inside a single
    # transaction. Individual failures are logged and counted but never stop
    # the run. Fittings/valves inherit the thickest connected pipe's value.
    with Transaction(doc, 'Pipe Insulation Auto Apply') as trans:
        trans.Start()
        for pipe in work_pipes:
            required, reason = _thickness_for_pipe(pipe, standard)
            _apply_insulation(pipe, required, reason, insulation_type_id,
                              counts, skipped_details, error_details, 'pipes')
        for part, thickness in work_parts:
            _apply_insulation(part, thickness, None, insulation_type_id,
                              counts, skipped_details, error_details, 'parts')
        trans.Commit()

    # Step 13: report.
    generate_report(counts, skipped_details, error_details)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        logger.error('Unhandled error: {}'.format(exc))
        forms.alert('Unexpected error:\n{}'.format(exc),
                    title='Pipe Insulation')
