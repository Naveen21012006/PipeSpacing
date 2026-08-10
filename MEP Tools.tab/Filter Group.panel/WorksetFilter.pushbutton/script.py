# -*- coding: utf-8 -*-
"""Workset Filter - temporarily hide chosen worksets in the active view.

Shows every user workset in the project as a checkbox list; the ticked
worksets are hidden in the active view with Revit's Temporary Hide/Isolate,
leaving the unticked worksets on screen. A summary reports how many worksets
were hidden and how many elements that removed from the view.

The tool is non-destructive along every path: no workset property, no view
visibility setting and no element is modified - only a temporary view state
is applied, the same one the view control bar's sunglasses icon clears.

The button toggles, matching Level Adjustment in the same panel: click it
again on a view it filtered and the hidden worksets come back, click once
more and the checkbox list opens afresh. Each view's filter is remembered
for the length of the Revit session. Restoring through Revit's own Reset
Temporary Hide/Isolate works too - the tool notices and simply starts over.

Requires a workshared model; without worksharing there are no worksets to
filter. Linked models are out of scope - a link is one element on one
workset, so a link's workset hides or shows the whole link.

Author: Naveen
Target: Revit 2022-2026 / pyRevit / IronPython
"""

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
from pyrevit import revit, forms, script

from Autodesk.Revit.DB import (
    ElementFilter,
    ElementId,
    ElementWorksetFilter,
    FilteredElementCollector,
    FilteredWorksetCollector,
    LogicalOrFilter,
    TemporaryViewMode,
    Transaction,
    WorksetKind,
)

from System.Collections.Generic import List

doc = revit.doc
uidoc = revit.uidoc
logger = script.get_logger()

# Title shown on every dialog raised by this tool.
TOOL_TITLE = 'Workset Filter'

# What the last run hid, per view: {view_id: {'worksets': [name, ...],
# 'count': elements_hidden}}. Held for the length of the Revit session so a
# second click knows it has a filter of its own to lift, rather than someone
# else's isolation to clear.
HIDDEN_ENVVAR = 'CKR_WORKSETFILTER_HIDDEN'


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


def _wid(workset_id):
    """Return a WorksetId as a plain integer."""
    try:
        return workset_id.IntegerValue
    except AttributeError:
        return int(str(workset_id))


def _name(obj):
    """Return an object's name, or a placeholder when it has none."""
    try:
        return obj.Name
    except Exception:
        return '<unnamed>'


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
    """Return the session record of filtered views, or an empty dict."""
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


def remember_hidden(view, workset_names, element_count):
    """Record what this run hid, so the next click can undo exactly that."""
    store = _get_store()
    store[_eid(view.Id)] = {'worksets': list(workset_names),
                            'count': element_count}
    _set_store(store)


def forget_hidden(view):
    """Drop a view's record once its filter has been lifted."""
    store = _get_store()
    if store.pop(_eid(view.Id), None) is not None:
        _set_store(store)


def hidden_record(view):
    """Return {'worksets': [...], 'count': n} for the view, or None."""
    return _get_store().get(_eid(view.Id))


# ---------------------------------------------------------------------------
# Step 1 - Document and active view validation
# ---------------------------------------------------------------------------
def validate_document():
    """Return True when a workshared project document is open.

    Worksets only exist in workshared models, so anything else is rejected
    with a message saying why rather than an empty workset list.
    """
    if doc is None or doc.IsFamilyDocument:
        forms.alert('Open a project document first.', title=TOOL_TITLE)
        return False

    if not doc.IsWorkshared:
        forms.alert(
            'Worksharing is not enabled in this model, so there are no '
            'worksets to filter.\n\n'
            'Enable worksharing (Collaborate tab > Worksets) and run the '
            'tool again.',
            title=TOOL_TITLE)
        return False

    return True


def validate_active_view():
    """Return the active view when it can take a temporary filter.

    Any graphical model view qualifies - plan, section, elevation or 3D.
    What matters is that the view supports temporary visibility modes;
    schedules, sheets and view templates do not, and are rejected with an
    informative message.

    Returns:
        View | None: The validated active view, or None when the tool
        should stop.
    """
    view = doc.ActiveView

    if view is None or view.IsTemplate:
        forms.alert(
            'No usable active view.\n\n'
            'Open a graphical model view and run the tool again.',
            title=TOOL_TITLE)
        return None

    # Schedules, sheets and a few special cases cannot use Temporary
    # Hide/Isolate at all.
    try:
        if not view.CanUseTemporaryVisibilityModes():
            forms.alert(
                'The active view "{}" does not support Temporary '
                'Hide/Isolate.\n\n'
                'Open a plan, section, elevation or 3D view and run the '
                'tool again.'.format(_name(view)),
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


def restore_view(view, record):
    """Lift this tool's filter and drop the record that tracked it.

    Args:
        view (View): The view to put back.
        record (dict): The session record of what the last run hid.

    Returns:
        bool: True when the view was restored.
    """
    try:
        clear_isolation(view, 'Workset Filter - Restore View')
    except Exception as ex:
        logger.error('Restore failed: {}'.format(ex))
        forms.alert('The view could not be restored:\n\n{}'.format(ex),
                    title=TOOL_TITLE)
        return False

    forget_hidden(view)
    _refresh()

    workset_names = record.get('worksets', [])
    forms.alert(
        'View restored.\n\n'
        '{} element(s) from {} workset(s) are visible again in "{}":\n\n'
        '{}\n\n'
        'Click Workset Filter again to choose a new set.'.format(
            record.get('count', 0), len(workset_names), _name(view),
            '\n'.join('  - ' + name for name in workset_names)),
        title=TOOL_TITLE)
    return True


def reset_temporary_hide_isolate(view):
    """Clear an isolation this tool did not create, before scanning.

    A view-based collector only ever returns what is currently on screen, so
    filtering underneath somebody else's isolate would hide a subset and
    report wrong counts. Clearing it first keeps the result honest.

    Returns:
        bool: True when a temporary mode was actually cleared.
    """
    if not is_isolation_active(view):
        return False

    clear_isolation(view, 'Reset Temporary Hide/Isolate')
    logger.debug('Cleared an existing temporary hide/isolate before scanning.')
    return True


# ---------------------------------------------------------------------------
# Step 3 - Load the project worksets
# ---------------------------------------------------------------------------
def get_user_worksets():
    """Return the user-created worksets, sorted by name.

    System worksets (view worksets, project standards, families) are
    excluded by collecting ``WorksetKind.UserWorkset`` only.
    """
    collector = FilteredWorksetCollector(doc).OfKind(WorksetKind.UserWorkset)
    return sorted(collector, key=lambda workset: _name(workset).lower())


# ---------------------------------------------------------------------------
# Step 4 - Workset selection dialog
# ---------------------------------------------------------------------------
def select_worksets_to_hide(worksets):
    """Show the checkbox list and return the worksets the user ticked.

    Returns:
        list | None: The chosen worksets ([] when the user applied with
        nothing ticked), or None when the dialog was cancelled.
    """
    by_name = {}
    for workset in worksets:
        by_name[_name(workset)] = workset

    chosen = forms.SelectFromList.show(
        [_name(workset) for workset in worksets],
        title='Workset Filter - tick the worksets to hide',
        button_name='Hide Selected Worksets',
        multiselect=True)

    if chosen is None:
        return None
    return [by_name[name] for name in chosen]


# ---------------------------------------------------------------------------
# Step 5 - Collect the visible elements of the chosen worksets
# ---------------------------------------------------------------------------
def _can_hide(element, view):
    """Return True when the element may be hidden in the view."""
    try:
        return element.CanBeHidden(view)
    except Exception:
        return False


def collect_workset_elements(view, worksets):
    """Return the visible elements belonging to the chosen worksets.

    One view-scoped collector runs for all the chosen worksets at once - the
    per-workset filters are OR-ed together, and ElementWorksetFilter is a
    quick filter, so large models are read in a single cheap pass. The
    view-scoped collector already excludes hidden elements and everything
    the view range or V/G settings filter out.

    Returns:
        tuple: (element_ids, counts, skipped) where element_ids is a list of
        ElementId to hide, counts maps workset id int -> visible element
        count, and skipped is how many elements refused to be hidden.
    """
    filters = [ElementWorksetFilter(workset.Id) for workset in worksets]
    if len(filters) == 1:
        workset_filter = filters[0]
    else:
        workset_filter = LogicalOrFilter(List[ElementFilter](filters))

    collector = (FilteredElementCollector(doc, view.Id)
                 .WhereElementIsNotElementType()
                 .WherePasses(workset_filter))

    element_ids = []
    counts = {}
    skipped = 0
    for element in collector:
        if not _can_hide(element, view):
            skipped += 1
            continue
        element_ids.append(element.Id)
        workset_key = _wid(element.WorksetId)
        counts[workset_key] = counts.get(workset_key, 0) + 1

    return element_ids, counts, skipped


# ---------------------------------------------------------------------------
# Step 6 - Hide the elements temporarily
# ---------------------------------------------------------------------------
def hide_elements_temporarily(view, element_ids):
    """Apply Temporary Hide/Isolate to the collected elements.

    Only a view state changes - the elements, their worksets and the
    project's workset visibility settings are untouched.

    Returns:
        bool: True when the hide was applied.
    """
    try:
        with Transaction(doc, 'Workset Filter - Hide Worksets') as trans:
            trans.Start()
            view.HideElementsTemporary(List[ElementId](element_ids))
            trans.Commit()
        return True
    except Exception as ex:
        logger.error('Temporary hide failed: {}'.format(ex))
        forms.alert(
            'The selected worksets could not be hidden:\n\n{}'.format(ex),
            title=TOOL_TITLE)
        return False


# ---------------------------------------------------------------------------
# Step 7 - Completion summary
# ---------------------------------------------------------------------------
def show_summary(view, total_worksets, hidden_worksets, counts, skipped):
    """Report what the filter did, workset by workset."""
    hidden_total = 0
    breakdown = []
    for workset in hidden_worksets:
        count = counts.get(_wid(workset.Id), 0)
        hidden_total += count
        breakdown.append('  - {} : {} element(s)'.format(_name(workset), count))

    lines = [
        'Workset Filter applied to "{}".'.format(_name(view)),
        '',
        'Total worksets : {}'.format(total_worksets),
        'Hidden worksets : {}'.format(len(hidden_worksets)),
        'Visible worksets : {}'.format(total_worksets - len(hidden_worksets)),
        '',
        '{} element(s) temporarily hidden:'.format(hidden_total),
    ]
    lines.extend(breakdown)

    if skipped:
        lines.append('')
        lines.append('{} element(s) could not be hidden and stay '
                     'visible.'.format(skipped))

    lines.append('')
    lines.append('Nothing in the model was modified. Click Workset Filter '
                 'again to restore the view.')

    forms.alert('\n'.join(lines), title=TOOL_TITLE)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def main():
    """Entry point that wires the full workflow together."""
    # Step 1: the tool needs a workshared model and a graphical view.
    if not validate_document():
        return
    view = validate_active_view()
    if view is None:
        return

    # Step 2a: the button toggles. A second click on a view this tool
    # filtered puts the hidden worksets back.
    record = hidden_record(view)
    if record and is_isolation_active(view):
        restore_view(view, record)
        return
    if record:
        # The isolation was already lifted through the view control bar; the
        # record is stale, so drop it and fall through to a fresh run.
        forget_hidden(view)

    # Step 2b: an isolate from anywhere else would hide elements from the
    # scan, so the filter starts from a clean view state.
    reset_temporary_hide_isolate(view)

    # Step 3: load the user worksets.
    worksets = get_user_worksets()
    if not worksets:
        forms.alert(
            'No user worksets were found in this model.\n\n'
            'Nothing to filter.',
            title=TOOL_TITLE)
        return

    # Step 4: ask which worksets to hide.
    chosen = select_worksets_to_hide(worksets)
    if chosen is None:
        return  # cancelled - leave the view exactly as it was found
    if not chosen:
        forms.alert(
            'No worksets were selected.\n\n'
            'Nothing was hidden.',
            title=TOOL_TITLE)
        return

    # Step 5: collect the visible elements of the chosen worksets.
    element_ids, counts, skipped = collect_workset_elements(view, chosen)
    if not element_ids:
        forms.alert(
            'The selected workset(s) have no visible elements in '
            '"{}".\n\n'
            'Nothing was hidden.'.format(_name(view)),
            title=TOOL_TITLE)
        return

    # Step 6: hide them, then remember the set for the next click.
    if not hide_elements_temporarily(view, element_ids):
        return
    remember_hidden(view, [_name(workset) for workset in chosen],
                    len(element_ids))
    _refresh()

    # Step 7: report.
    show_summary(view, len(worksets), chosen, counts, skipped)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        logger.error('Unhandled error: {}'.format(exc))
        forms.alert('Unexpected error:\n{}'.format(exc), title=TOOL_TITLE)
