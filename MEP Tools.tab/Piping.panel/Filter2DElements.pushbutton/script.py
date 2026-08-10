# -*- coding: utf-8 -*-
"""2D Elements Filter - show only the 2D elements of the active view.

Collects every element visible in the active view, sorts it into 2D
annotation and 3D model, and temporarily hides the 3D side with Revit's
Temporary Hide/Isolate - leaving only text notes, detail lines, detail
components, filled regions, dimensions, tags, revision clouds, spot
dimensions, generic annotations, detail groups and the rest of the
view-specific elements on screen for review. A summary reports how many 2D
elements stayed visible and how many 3D elements were hidden.

An element counts as 2D when it is view-specific (it lives in this view
only), or when its category is an annotation category - which keeps datum
annotations such as grids and levels on screen too. Model and analytical
categories count as 3D and are hidden; anything Revit keeps for internal
bookkeeping is left alone.

The tool is non-destructive along every path: no element, no view
visibility setting and no model data is modified - only a temporary view
state is applied, the same one the view control bar's sunglasses icon
clears.

The button toggles, matching Level Adjustment and Workset Filter in the
same panel: click it again on a view it filtered and the model comes back,
click once more and the view is filtered afresh. Each view's state is
remembered for the length of the Revit session. Restoring through Revit's
own Reset Temporary Hide/Isolate works too - the tool notices and simply
starts over.

Author: Naveen
Target: Revit 2022-2026 / pyRevit / IronPython
"""

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
from pyrevit import revit, forms, script

from Autodesk.Revit.DB import (
    CategoryType,
    ElementId,
    FilteredElementCollector,
    TemporaryViewMode,
    Transaction,
    View,
)

from System.Collections.Generic import List

doc = revit.doc
uidoc = revit.uidoc
logger = script.get_logger()

# Title shown on every dialog raised by this tool.
TOOL_TITLE = '2D Elements Filter'

# What the last run hid, per view: {view_id: {'count_2d': visible_2d,
# 'count_3d': elements_hidden}}. Held for the length of the Revit session so
# a second click knows it has a filter of its own to lift, rather than
# someone else's isolation to clear.
HIDDEN_ENVVAR = 'CKR_2DFILTER_HIDDEN'

# Classification buckets for the elements of the view.
CLASS_2D = '2d'
CLASS_3D = '3d'
CLASS_OTHER = 'other'


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


def _name(obj):
    """Return an object's name, or a placeholder when it has none."""
    try:
        return obj.Name
    except Exception:
        return '<unnamed>'


def _count(number):
    """Format a count with thousands separators, e.g. 2387 -> '2,387'."""
    return '{:,}'.format(number)


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


def remember_hidden(view, count_2d, count_3d):
    """Record what this run hid, so the next click can undo exactly that."""
    store = _get_store()
    store[_eid(view.Id)] = {'count_2d': count_2d, 'count_3d': count_3d}
    _set_store(store)


def forget_hidden(view):
    """Drop a view's record once its filter has been lifted."""
    store = _get_store()
    if store.pop(_eid(view.Id), None) is not None:
        _set_store(store)


def hidden_record(view):
    """Return {'count_2d': n, 'count_3d': n} for the view, or None."""
    return _get_store().get(_eid(view.Id))


# ---------------------------------------------------------------------------
# Step 1 - Active view validation
# ---------------------------------------------------------------------------
def validate_active_view():
    """Return the active view when it can take a temporary filter.

    Plans, ceiling plans, sections, elevations, drafting views, detail
    views and 3D views all qualify - what matters is that the view supports
    temporary visibility modes; schedules, sheets and view templates do
    not, and are rejected with an informative message.

    Returns:
        View | None: The validated active view, or None when the tool
        should stop.
    """
    if doc is None:
        forms.alert('Open a document first.', title=TOOL_TITLE)
        return None

    view = doc.ActiveView

    if view is None or view.IsTemplate:
        forms.alert(
            'No usable active view.\n\n'
            'Open a graphical view and run the tool again.',
            title=TOOL_TITLE)
        return None

    # Schedules, sheets and a few special cases cannot use Temporary
    # Hide/Isolate at all.
    try:
        if not view.CanUseTemporaryVisibilityModes():
            forms.alert(
                'The active view "{}" does not support Temporary '
                'Hide/Isolate.\n\n'
                'Open a plan, section, elevation, drafting or 3D view and '
                'run the tool again.'.format(_name(view)),
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
        clear_isolation(view, '2D Elements Filter - Restore View')
    except Exception as ex:
        logger.error('Restore failed: {}'.format(ex))
        forms.alert('The view could not be restored:\n\n{}'.format(ex),
                    title=TOOL_TITLE)
        return False

    forget_hidden(view)
    _refresh()

    forms.alert(
        'View restored.\n\n'
        '{} 3D model element(s) are visible again in "{}".\n\n'
        'Click 2D Elements Filter again to re-apply the filter.'.format(
            _count(record.get('count_3d', 0)), _name(view)),
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
# Step 3 - Collect and classify the visible elements
# ---------------------------------------------------------------------------
def classify_element(element):
    """Sort one element into the 2D, 3D or leave-alone bucket.

    View-specific elements are the 2D side by definition - they exist in
    this view only. Everything else is judged by its category type:
    annotation categories (grids, levels, reference planes and the other
    datum marks) stay with the 2D side, model and analytical categories are
    the 3D side, and elements with no category or an internal one are
    Revit bookkeeping that is best left untouched.
    """
    try:
        if element.ViewSpecific:
            return CLASS_2D
    except Exception:
        pass

    category = element.Category
    if category is None:
        return CLASS_OTHER

    category_type = category.CategoryType
    if category_type == CategoryType.Model:
        return CLASS_3D
    if category_type == CategoryType.AnalyticalModel:
        return CLASS_3D
    if category_type == CategoryType.Annotation:
        return CLASS_2D
    return CLASS_OTHER


def _can_hide(element, view):
    """Return True when the element may be hidden in the view."""
    try:
        return element.CanBeHidden(view)
    except Exception:
        return False


def _category_label(element):
    """Return the element's category name for the summary breakdown."""
    category = element.Category
    if category is not None:
        return _name(category)
    return element.GetType().Name


def collect_view_elements(view):
    """Split the visible elements of the view into 2D and 3D.

    One view-scoped collector reads the whole view in a single pass - it
    already excludes hidden elements and everything the view range or V/G
    settings filter out, so classification is the only per-element work.
    ``CanBeHidden`` is asked only of the 3D side, where it matters.

    Returns:
        tuple: (hide_ids, counts_2d, count_3d, skipped) where hide_ids is a
        list of ElementId to hide, counts_2d maps category name -> visible
        2D element count, count_3d is how many 3D elements will be hidden,
        and skipped is how many 3D elements refused to be hidden.
    """
    collector = (FilteredElementCollector(doc, view.Id)
                 .WhereElementIsNotElementType())

    hide_ids = []
    counts_2d = {}
    skipped = 0
    for element in collector:
        # A view element inside a view is a viewport marker (section line,
        # elevation tag callout...), not something to hide from itself.
        if isinstance(element, View):
            continue

        bucket = classify_element(element)
        if bucket == CLASS_2D:
            label = _category_label(element)
            counts_2d[label] = counts_2d.get(label, 0) + 1
        elif bucket == CLASS_3D:
            if _can_hide(element, view):
                hide_ids.append(element.Id)
            else:
                skipped += 1

    return hide_ids, counts_2d, len(hide_ids), skipped


# ---------------------------------------------------------------------------
# Step 4 - Hide the 3D elements temporarily
# ---------------------------------------------------------------------------
def hide_elements_temporarily(view, element_ids):
    """Apply Temporary Hide/Isolate to the collected 3D elements.

    Only a view state changes - the elements, the view's visibility
    settings and the model itself are untouched.

    Returns:
        bool: True when the hide was applied.
    """
    try:
        with Transaction(doc, '2D Elements Filter - Isolate 2D') as trans:
            trans.Start()
            view.HideElementsTemporary(List[ElementId](element_ids))
            trans.Commit()
        return True
    except Exception as ex:
        logger.error('Temporary hide failed: {}'.format(ex))
        forms.alert(
            'The 3D model elements could not be hidden:\n\n{}'.format(ex),
            title=TOOL_TITLE)
        return False


# ---------------------------------------------------------------------------
# Step 5 - Completion summary
# ---------------------------------------------------------------------------
def show_summary(view, counts_2d, count_3d, skipped):
    """Report what the filter did, with the 2D side broken down by category."""
    count_2d = sum(counts_2d.values())

    lines = [
        '2D Elements Filter applied.',
        '',
        'Active view : {}'.format(_name(view)),
        '2D elements visible : {}'.format(_count(count_2d)),
        '3D elements hidden : {}'.format(_count(count_3d)),
        '',
        '2D elements by category:',
    ]
    for label in sorted(counts_2d, key=lambda name: (-counts_2d[name], name)):
        lines.append('  - {} : {}'.format(label, _count(counts_2d[label])))

    if skipped:
        lines.append('')
        lines.append('{} 3D element(s) could not be hidden and stay '
                     'visible.'.format(_count(skipped)))

    lines.append('')
    lines.append('Nothing in the model was modified. Click 2D Elements '
                 'Filter again to restore the view.')

    forms.alert('\n'.join(lines), title=TOOL_TITLE)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def main():
    """Entry point that wires the full workflow together."""
    # Step 1: the tool needs a graphical view that takes temporary modes.
    view = validate_active_view()
    if view is None:
        return

    # Step 2a: the button toggles. A second click on a view this tool
    # filtered puts the 3D model back.
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

    # Step 3: collect the visible elements and split them 2D / 3D.
    hide_ids, counts_2d, count_3d, skipped = collect_view_elements(view)

    if not counts_2d:
        forms.alert(
            'No visible 2D elements were found in "{}".\n\n'
            'Hiding the model would leave the view empty, so nothing was '
            'changed.'.format(_name(view)),
            title=TOOL_TITLE)
        return

    if not hide_ids:
        forms.alert(
            'Every visible element in "{}" is already 2D - there is '
            'nothing to hide.\n\n'
            '{} 2D element(s) are on screen.'.format(
                _name(view), _count(sum(counts_2d.values()))),
            title=TOOL_TITLE)
        return

    # Step 4: hide the 3D side, then remember the run for the next click.
    if not hide_elements_temporarily(view, hide_ids):
        return
    remember_hidden(view, sum(counts_2d.values()), count_3d)
    _refresh()

    # Step 5: report.
    show_summary(view, counts_2d, count_3d, skipped)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        logger.error('Unhandled error: {}'.format(exc))
        forms.alert('Unexpected error:\n{}'.format(exc), title=TOOL_TITLE)
