# -*- coding: utf-8 -*-
"""Level Adjustment - cross-level QA for the active plan view.

Answers one question without touching the model: which piping drawn in this
plan actually belongs to another level?

Every pipe, flex pipe, fitting and accessory (valve) visible in the active
plan view is compared against the view's associated level:

    * its level matches the view level      -> Correct
    * the levels differ                     -> Cross-Level
    * no level can be established at all    -> Unverified

Fittings and accessories are family instances, and the parameter carrying
their level differs from family to family. So the level is resolved in two
passes: the element's own level parameters first, and failing those, the
level of the MEP curves it connects to - a valve with no level of its own
belongs to the run it sits in. Only a genuinely orphaned element ends up
Unverified.

The correct elements are then hidden with Revit's Temporary Hide/Isolate -
along with any insulation they host, so nothing is left floating - and the
cross-level and unverified elements are the only piping left on screen while
the rest of the model stays visible as context.

The isolated view is then held open for review: press Esc and it is restored,
or click an element to keep the isolation and carry on working. The button
toggles too - click it again on a view it isolated and what it hid comes
back, click once more and the view is checked afresh. Each view's set is
remembered for the length of the Revit session.

Nothing is modified along any of those paths - no element parameter is
written, only a view state.

Linked models are out of scope.

Author: Naveen
Target: Revit 2022-2026 / pyRevit / IronPython
"""

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
from pyrevit import revit, forms, script

from Autodesk.Revit.DB import (
    BuiltInCategory,
    BuiltInParameter,
    ElementId,
    FilteredElementCollector,
    MEPCurve,
    TemporaryViewMode,
    Transaction,
    ViewPlan,
)
from Autodesk.Revit.Exceptions import OperationCanceledException
from Autodesk.Revit.UI.Selection import ObjectType

from System.Collections.Generic import List

doc = revit.doc
uidoc = revit.uidoc
logger = script.get_logger()

# Title shown on every dialog raised by this tool.
TOOL_TITLE = 'Level Adjustment'

# What the last run hid, per view: {view_id: [element_id, ...]}. Held for the
# length of the Revit session so a second click knows it has a view of its
# own to restore, rather than someone else's isolation to clear.
HIDDEN_ENVVAR = 'CKR_LEVELADJ_HIDDEN'

# Categories checked, in report order. Add a row to extend the tool - the
# collection, classification and reporting all read from here.
TARGET_CATEGORIES = [
    (BuiltInCategory.OST_PipeCurves, 'Pipes'),
    (BuiltInCategory.OST_FlexPipeCurves, 'Flex pipes'),
    (BuiltInCategory.OST_PipeFitting, 'Fittings'),
    (BuiltInCategory.OST_PipeAccessory, 'Valves / accessories'),
]

# Level parameters tried in order. Pipes carry the first; family instances
# carry one of the others depending on the family template. Looked up by name
# so a parameter missing from a given Revit version is simply skipped.
PICK_PROMPT = ('Level Adjustment: press Esc to restore the view, or click an '
               'element to keep it isolated.')

LEVEL_PARAM_NAMES = (
    'RBS_START_LEVEL_PARAM',              # MEP curves, and most MEP families
    'FAMILY_LEVEL_PARAM',                 # generic family instance "Level"
    'INSTANCE_REFERENCE_LEVEL_PARAM',     # face-based / hosted instances
    'SCHEDULE_LEVEL_PARAM',               # "Schedule Level"
    'INSTANCE_SCHEDULE_ONLY_LEVEL_PARAM',
)


# ---------------------------------------------------------------------------
# Version / element helpers
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


def _is_valid_id(element_id):
    """Return True when an ElementId points at a real element."""
    if element_id is None:
        return False
    try:
        return _eid(element_id) > 0
    except Exception:
        return False


def _element_name(element):
    """Return an element's name, tolerant of IronPython property quirks."""
    if element is None:
        return ''
    try:
        return element.Name
    except Exception:
        try:
            from Autodesk.Revit.DB import Element
            return Element.Name.GetValue(element)
        except Exception:
            return ''


def _level_name(level_id):
    """Return the display name of a level id, or a readable placeholder."""
    name = _element_name(doc.GetElement(level_id))
    return name or '<unnamed level {}>'.format(_eid(level_id))


def _refresh():
    """Redraw the active view, ignoring failures on background documents."""
    try:
        uidoc.RefreshActiveView()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Session record - what this tool hid, and where
# ---------------------------------------------------------------------------
def _get_store():
    """Return the session record of hidden elements, or an empty dict."""
    try:
        return script.get_envvar(HIDDEN_ENVVAR) or {}
    except Exception as ex:
        logger.debug('Session store unavailable: {}'.format(ex))
        return {}


def _set_store(store):
    """Write the session record back, tolerating a read-only environment."""
    try:
        script.set_envvar(HIDDEN_ENVVAR, store)
    except Exception as ex:
        logger.debug('Session store not writable: {}'.format(ex))


def remember_hidden(view, elements):
    """Record what this run hid, so the next click can undo exactly that."""
    store = _get_store()
    store[_eid(view.Id)] = [_eid(element.Id) for element in elements]
    _set_store(store)


def forget_hidden(view):
    """Drop a view's record once its isolation has been lifted."""
    store = _get_store()
    if store.pop(_eid(view.Id), None) is not None:
        _set_store(store)


def hidden_count(view):
    """Return how many elements this tool hid in the view; 0 when none."""
    return len(_get_store().get(_eid(view.Id), []))


# ---------------------------------------------------------------------------
# Step 1 - Active view validation
# ---------------------------------------------------------------------------
def get_view_level(view):
    """Return the level associated with a plan view, or None."""
    try:
        return view.GenLevel
    except Exception as ex:
        logger.debug('GenLevel unavailable on view {}: {}'.format(
            _eid(view.Id), ex))
        return None


def validate_active_view():
    """Return the active view when it is a usable level-based plan view.

    Floor plans, engineering/MEP plans and ceiling plans all qualify - what
    matters is that the view is a ViewPlan with an associated level and that
    it supports temporary visibility modes. Any other view is rejected with an
    informative message.

    Returns:
        ViewPlan | None: The validated active view, or None when the tool
        should stop.
    """
    view = doc.ActiveView

    if view is None or view.IsTemplate or not isinstance(view, ViewPlan):
        forms.alert(
            'The active view is not a plan view.\n\n'
            'Open a floor plan (or another level-based plan view) and run '
            'the tool again.',
            title=TOOL_TITLE)
        return None

    if get_view_level(view) is None:
        forms.alert(
            'The active plan view has no associated level, so there is '
            'nothing to compare against.',
            title=TOOL_TITLE)
        return None

    # Views placed on a sheet being edited, and a few special cases, cannot
    # use Temporary Hide/Isolate at all.
    try:
        if not view.CanUseTemporaryVisibilityModes():
            forms.alert(
                'This view does not support Temporary Hide/Isolate, so the '
                'cross-level elements cannot be isolated.',
                title=TOOL_TITLE)
            return None
    except Exception as ex:
        logger.debug('CanUseTemporaryVisibilityModes check failed: {}'.format(ex))

    return view


# ---------------------------------------------------------------------------
# Step 2 - Restore, or start from a clean view state
# ---------------------------------------------------------------------------
def is_isolation_active(view):
    """Return True when a Temporary Hide/Isolate is in force on the view."""
    try:
        return view.TemporaryViewModes.IsModeActive(
            TemporaryViewMode.TemporaryHideIsolate)
    except Exception as ex:
        logger.debug('Temporary view mode check failed: {}'.format(ex))
        return False


def clear_isolation(view, transaction_name):
    """Lift the Temporary Hide/Isolate on the view in one transaction."""
    with Transaction(doc, transaction_name) as trans:
        trans.Start()
        view.DisableTemporaryViewMode(TemporaryViewMode.TemporaryHideIsolate)
        trans.Commit()  # commit regenerates, so the next collector sees it all


def restore_isolation(view, announce):
    """Lift this tool's isolation and drop the record that tracked it.

    Dropping the record is what makes the next click scan afresh rather than
    try to restore an already-restored view.

    Args:
        view (ViewPlan): The view to put back.
        announce (bool): Show a confirmation. The Esc path stays silent - the
            view visibly changes, and a dialog on Esc would only be in the way.

    Returns:
        bool: True when the view was restored.
    """
    count = hidden_count(view)
    try:
        clear_isolation(view, 'Level Adjustment - Restore View')
    except Exception as ex:
        logger.error('Restore failed: {}'.format(ex))
        forms.alert('The view could not be restored:\n\n{}'.format(ex),
                    title=TOOL_TITLE)
        return False

    forget_hidden(view)
    _refresh()
    if announce:
        forms.alert(
            'View restored.\n\n'
            '{} correctly assigned element(s) are visible again in "{}".\n\n'
            'Click Level Adjustment to check the view again.'.format(
                count, _element_name(view)),
            title=TOOL_TITLE)
    return True


def reset_temporary_hide_isolate(view):
    """Clear an isolation this tool did not create, before scanning.

    A view-based collector only ever returns what is currently on screen, so
    scanning underneath somebody else's isolate would count a filtered subset
    of the model. Clearing it first keeps the count honest.

    Returns:
        bool: True when a temporary mode was actually cleared.
    """
    if not is_isolation_active(view):
        return False

    clear_isolation(view, 'Reset Temporary Hide/Isolate')
    logger.debug('Cleared an existing temporary hide/isolate before scanning.')
    return True


# ---------------------------------------------------------------------------
# Step 3 - Collect the visible elements
# ---------------------------------------------------------------------------
def collect_visible_elements(view):
    """Return the piping elements visible in the given view.

    A view-scoped collector already excludes hidden elements, anything the
    view range or V/G overrides filter out, and everything inside linked
    models.

    Returns:
        tuple: (elements, labels) where elements is a list and labels maps
        each element id to its category label from TARGET_CATEGORIES.
    """
    elements = []
    labels = {}
    for built_in_category, label in TARGET_CATEGORIES:
        collector = (FilteredElementCollector(doc, view.Id)
                     .OfCategory(built_in_category)
                     .WhereElementIsNotElementType())
        for element in collector:
            elements.append(element)
            labels[_eid(element.Id)] = label
    logger.debug('Found {} visible element(s).'.format(len(elements)))
    return elements, labels


def map_insulation_by_host(view):
    """Return {host_id: [insulation ElementId]} for insulation in the view.

    Insulation is a separate element hosted on a pipe, so hiding the pipe
    alone would leave its insulation drawn on screen. Reading the visible
    insulation once and grouping it by host is both cheaper than querying per
    pipe and a guarantee that everything hidden is genuinely in the view.
    """
    mapping = {}
    collector = (FilteredElementCollector(doc, view.Id)
                 .OfCategory(BuiltInCategory.OST_PipeInsulations)
                 .WhereElementIsNotElementType())
    for insulation in collector:
        try:
            host_id = _eid(insulation.HostElementId)
        except Exception:
            continue
        mapping.setdefault(host_id, []).append(insulation.Id)
    logger.debug('Found insulation on {} host(s).'.format(len(mapping)))
    return mapping


# ---------------------------------------------------------------------------
# Step 4 - Determine each element's level
# ---------------------------------------------------------------------------
def get_own_level_id(element):
    """Return the ElementId of an element's own level, or None.

    Tries every level parameter a pipe, flex pipe, fitting or accessory might
    carry, then the MEPCurve.ReferenceLevel property, then the element's own
    LevelId. Nothing here looks at neighbouring elements.
    """
    for param_name in LEVEL_PARAM_NAMES:
        built_in = getattr(BuiltInParameter, param_name, None)
        if built_in is None:
            continue  # parameter does not exist in this Revit version
        param = element.get_Parameter(built_in)
        if param is None:
            continue
        try:
            level_id = param.AsElementId()
        except Exception:
            continue
        if _is_valid_id(level_id):
            return level_id

    try:
        level = getattr(element, 'ReferenceLevel', None)
        if level is not None and _is_valid_id(level.Id):
            return level.Id
    except Exception:
        pass

    try:
        if _is_valid_id(element.LevelId):
            return element.LevelId
    except Exception:
        pass

    return None


def _connector_manager(element):
    """Return the ConnectorManager of a curve/fitting/accessory, or None."""
    try:
        if isinstance(element, MEPCurve):
            return element.ConnectorManager
        mep_model = getattr(element, 'MEPModel', None)
        if mep_model is not None:
            return mep_model.ConnectorManager
    except Exception:
        pass
    return None


def get_connected_level_ids(element):
    """Return the levels of the MEP curves this element connects to.

    Only curves are followed, never other fittings, so the lookup is one hop
    and cannot recurse. A fitting joining two levels reports both.

    Returns:
        list[ElementId]: Distinct levels of the connected pipes/flex pipes.
    """
    level_ids = []
    seen_levels = set()
    manager = _connector_manager(element)
    if manager is None:
        return level_ids

    try:
        connectors = manager.Connectors
    except Exception:
        return level_ids

    for connector in connectors:
        try:
            refs = connector.AllRefs
        except Exception:
            continue
        for ref in refs:
            owner = ref.Owner
            if owner is None or not isinstance(owner, MEPCurve):
                continue
            level_id = get_own_level_id(owner)
            if level_id is None:
                continue
            key = _eid(level_id)
            if key not in seen_levels:
                seen_levels.add(key)
                level_ids.append(level_id)
    return level_ids


def resolve_level_id(element, view_level_id):
    """Return (level_id, inherited) for an element.

    The element's own level wins. When it has none - common for fitting and
    accessory families - the level of the run it connects to is used instead,
    because a valve with no level of its own belongs to its pipes. A fitting
    that joins two levels is credited to the view's level if it touches it,
    so a riser transition is not reported as a defect.

    Returns:
        tuple: (ElementId | None, bool). The id is None when no level could
        be established at all; the flag says whether it came from neighbours.
    """
    level_id = get_own_level_id(element)
    if level_id is not None:
        return level_id, False

    candidates = get_connected_level_ids(element)
    if not candidates:
        return None, False

    target = _eid(view_level_id)
    for candidate in candidates:
        if _eid(candidate) == target:
            return candidate, True
    return candidates[0], True


# ---------------------------------------------------------------------------
# Step 5 - Compare each element against the view level
# ---------------------------------------------------------------------------
def classify_elements(elements, view_level_id):
    """Split elements into correct, cross-level and unverified groups.

    Args:
        elements (list): The visible piping elements.
        view_level_id (ElementId): Level associated with the active view.

    Returns:
        tuple: (correct, cross, unverified) where

            correct     - list of elements on the view's level,
            cross       - list of (element, level_id, inherited) belonging to
                          another level,
            unverified  - list of elements with no level at all.
    """
    correct, cross, unverified = [], [], []
    target = _eid(view_level_id)

    for element in elements:
        try:
            level_id, inherited = resolve_level_id(element, view_level_id)
        except Exception as ex:
            logger.debug('Level lookup failed for element {}: {}'.format(
                _eid(element.Id), ex))
            level_id, inherited = None, False

        if level_id is None:
            unverified.append(element)       # cannot be verified = an issue
        elif _eid(level_id) == target:
            correct.append(element)
        else:
            cross.append((element, level_id, inherited))

    return correct, cross, unverified


# ---------------------------------------------------------------------------
# Step 6 - Isolation
# ---------------------------------------------------------------------------
def hide_elements_temporarily(view, elements, insulation_by_host):
    """Temporarily hide the given elements, and the insulation they host.

    Only the correctly assigned elements are hidden, so the surrounding model
    stays visible as context for what is left on screen. This is a view
    state, not a model edit, and the next click on the button lifts it.

    Returns:
        bool: True when the hide was applied (or was not needed).
    """
    if not elements:
        return True  # nothing correct to hide; the view already shows the issues

    element_ids = []
    for element in elements:
        element_ids.append(element.Id)
        element_ids.extend(insulation_by_host.get(_eid(element.Id), []))

    try:
        with Transaction(doc, 'Level Adjustment - Isolate Cross-Level') as trans:
            trans.Start()
            view.HideElementsTemporary(List[ElementId](element_ids))
            trans.Commit()
    except Exception as ex:
        logger.error('Temporary hide failed: {}'.format(ex))
        forms.alert(
            'The cross-level elements were identified, but Revit refused to '
            'hide the correctly assigned ones:\n\n{}\n\nSee the output '
            'window for the full list.'.format(ex),
            title=TOOL_TITLE)
        return False

    _refresh()
    return True


# ---------------------------------------------------------------------------
# Step 7 - User feedback
# ---------------------------------------------------------------------------
def _label_of(element, labels):
    """Return an element's category label for reporting."""
    return labels.get(_eid(element.Id), 'Other')


def _count_by_label(elements, labels):
    """Tally a list of elements by category label."""
    counts = {}
    for element in elements:
        label = _label_of(element, labels)
        counts[label] = counts.get(label, 0) + 1
    return counts


def _category_rows(total_counts, correct_counts, cross_counts,
                   unverified_counts):
    """Return per-category report rows in TARGET_CATEGORIES order.

    Categories with nothing visible are left out entirely, so a model without
    flex pipe never shows an empty row.
    """
    rows = []
    for _built_in_category, label in TARGET_CATEGORIES:
        visible = total_counts.get(label, 0)
        if not visible:
            continue
        rows.append((label,
                     visible,
                     correct_counts.get(label, 0),
                     cross_counts.get(label, 0),
                     unverified_counts.get(label, 0)))
    return rows


def _group_by_level(cross, labels):
    """Group cross-level elements by the level they actually belong to.

    Returns:
        list[dict]: {'name', 'ids', 'kinds'} sorted by level name.
    """
    groups = {}
    for element, level_id, _inherited in cross:
        key = _eid(level_id)
        if key not in groups:
            groups[key] = {'name': _level_name(level_id), 'ids': [],
                           'kinds': {}}
        entry = groups[key]
        entry['ids'].append(element.Id)
        label = _label_of(element, labels)
        entry['kinds'][label] = entry['kinds'].get(label, 0) + 1
    return sorted(groups.values(), key=lambda item: item['name'])


def _kinds_text(kinds):
    """Render a category tally as 'Fittings 7 - Pipes 2'."""
    return ' - '.join('{} {}'.format(label, count)
                      for label, count in sorted(kinds.items()))


def _print_id_group(output, label, element_ids, detail=''):
    """Print one report line with a clickable selection link."""
    try:
        link = output.linkify(element_ids, title='Select')
    except Exception:
        link = ''
    suffix = '  _{}_'.format(detail) if detail else ''
    output.print_md('- **{}** - {} element(s){} {}'.format(
        label, len(element_ids), suffix, link))


def generate_report(view, level, elements, labels, correct, cross, unverified,
                    isolated):
    """Show the completion summary and print the detail to the output window.

    Args:
        view (ViewPlan): The active view.
        level (Level): The view's associated level.
        elements (list): Every visible element that was checked.
        labels (dict): element id -> category label.
        correct (list): Elements on the view's level.
        cross (list): (element, level_id, inherited) tuples.
        unverified (list): Elements with no level at all.
        isolated (bool): True when the correct elements were actually hidden.
    """
    view_name = _element_name(view)
    level_name = _element_name(level)

    cross_elements = [item[0] for item in cross]
    inherited_count = len([item for item in cross if item[2]])

    rows = _category_rows(_count_by_label(elements, labels),
                          _count_by_label(correct, labels),
                          _count_by_label(cross_elements, labels),
                          _count_by_label(unverified, labels))

    # ---- summary dialog ----
    lines = [
        'Level Adjustment Completed',
        '',
        'View:        {}'.format(view_name),
        'View Level:  {}'.format(level_name),
        '',
        'Visible elements:  {}'.format(len(elements)),
        'Cross-level:       {}'.format(len(cross)),
    ]
    if unverified:
        lines.append('No level found:    {}'.format(len(unverified)))

    lines.append('')
    lines.append('By category:')
    for label, visible, _ok, cross_n, unverified_n in rows:
        detail = '  {}: {} cross-level of {} visible'.format(
            label, cross_n, visible)
        if unverified_n:
            detail += ', {} unverified'.format(unverified_n)
        lines.append(detail)

    lines.append('')
    if isolated:
        lines.extend([
            '{} correctly assigned element(s) are temporarily hidden.'.format(
                len(correct)),
            '',
            'Press Esc to restore the view, or click an element to keep it',
            'isolated and carry on working.',
        ])
    else:
        lines.append(
            'The view was left unchanged - see the output window for the '
            'elements that need attention.')

    # ---- output window ----
    output = script.get_output()
    output.print_md('# Level Adjustment - {}'.format(view_name))
    output.print_md('View level: **{}**'.format(level_name))

    table = ('| Category | Visible | On this level | Cross-level | No level |\n'
             '| :-- | --: | --: | --: | --: |\n')
    for label, visible, ok_n, cross_n, unverified_n in rows:
        table += '| {} | {} | {} | {} | {} |\n'.format(
            label, visible, ok_n, cross_n, unverified_n)
    table += '| **Total** | **{}** | **{}** | **{}** | **{}** |'.format(
        len(elements), len(correct), len(cross), len(unverified))
    output.print_md(table)

    if cross:
        output.print_md('## Cross-level elements by their own level')
        for group in _group_by_level(cross, labels):
            _print_id_group(output, group['name'], group['ids'],
                            _kinds_text(group['kinds']))
        if inherited_count:
            output.print_md(
                '_{} of these carry no level of their own; the level of the '
                'run they connect to was used._'.format(inherited_count))

    if unverified:
        output.print_md('## Elements with no level at all')
        _print_id_group(output, 'Unverified',
                        [element.Id for element in unverified],
                        _kinds_text(_count_by_label(unverified, labels)))

    forms.alert('\n'.join(lines), title=TOOL_TITLE)


# ---------------------------------------------------------------------------
# Step 8 - Hold the isolated view until Esc
# ---------------------------------------------------------------------------
def await_review(view):
    """Keep the view isolated for review, and restore it on Esc.

    Once a script has finished, nothing is left listening for keystrokes.
    Revit's own pick loop is the reliable way to hear Esc: it puts Revit in a
    command state where Esc cancels, exactly as it does for every other tool,
    with no background hook involved. Zooming and panning work throughout.

    Esc restores the view. Clicking an element instead ends the review with
    the isolation left in place and that element selected, so a finding can
    be fixed straight away - the button still restores the view later.
    """
    try:
        reference = uidoc.Selection.PickObject(ObjectType.Element, PICK_PROMPT)
    except OperationCanceledException:
        logger.debug('Esc pressed; restoring the view.')
        restore_isolation(view, announce=False)
        return
    except Exception as ex:
        # Revit refused the pick (a modal state, a view swap). The isolation
        # stays put and the button still toggles it off.
        logger.debug('Review loop ended early: {}'.format(ex))
        return

    try:
        uidoc.Selection.SetElementIds(List[ElementId]([reference.ElementId]))
    except Exception as ex:
        logger.debug('Could not select the picked element: {}'.format(ex))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def main():
    """Entry point that wires the full workflow together."""
    # Step 1: the tool operates on a level-based plan view.
    view = validate_active_view()
    if view is None:
        return
    level = get_view_level(view)

    # Step 2a: the button toggles. A second click on a view this tool isolated
    # puts it back, so the view control bar is never needed.
    if is_isolation_active(view) and hidden_count(view):
        restore_isolation(view, announce=True)
        return

    # Step 2b: an isolate from anywhere else would hide elements from the scan.
    reset_temporary_hide_isolate(view)

    # Step 3: collect what the view actually shows.
    elements, labels = collect_visible_elements(view)
    if not elements:
        forms.alert(
            'No pipes, fittings or valves are visible in "{}".\n\n'
            'Nothing to check.'.format(_element_name(view)),
            title=TOOL_TITLE)
        return

    # Steps 4-5: resolve each element's level and compare.
    correct, cross, unverified = classify_elements(elements, level.Id)

    # Nothing to isolate: leave the view exactly as it was found.
    if not cross and not unverified:
        forms.alert(
            'No cross-level elements found.\n\n'
            'All {} visible element(s) in "{}" belong to level "{}".'.format(
                len(elements), _element_name(view), _element_name(level)),
            title=TOOL_TITLE)
        return

    # Step 6: hide the correct elements - and their insulation - so only the
    # issues remain, then remember the set for the next click.
    isolated = hide_elements_temporarily(view, correct,
                                         map_insulation_by_host(view))
    if isolated:
        remember_hidden(view, correct)

    # Step 7: report.
    generate_report(view, level, elements, labels, correct, cross, unverified,
                    isolated)

    # Step 8: hold the isolated view open for review until Esc.
    if isolated and correct:
        await_review(view)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        logger.error('Unhandled error: {}'.format(exc))
        forms.alert('Unexpected error:\n{}'.format(exc), title=TOOL_TITLE)
