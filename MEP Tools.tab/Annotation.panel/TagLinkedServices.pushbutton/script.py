# -*- coding: utf-8 -*-
"""Tag Linked Services - entry point.

Reproduces Tag All Not Tagged for content that lives in a LINKED model,
with the geometric and dimensional conditions of the CKR development brief
so that only runs of engineering significance are annotated.

    1. Validate the active view - floor plans only (SC-03).
    2. Discover the link instances (FR-01).
    3. Show the dialog; Preview reruns it, Place commits (FR-08).
    4. Report, always, with a CSV written alongside the log (FR-09).

Every decision lives in a module under lib/ckr_taglinked; this file owns
the workflow, the progress bar and the top-level error handling, and
nothing else. No exception may reach the Revit UI (clause 8.4).

Author: Naveen
Target: Revit 2022-2026 / pyRevit / IronPython
"""

import datetime
import os
import sys
import traceback

# Make the bundle's library importable however pyRevit loads this script.
_BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
_LIB_DIR = os.path.join(_BUNDLE_DIR, 'lib')
if _LIB_DIR not in sys.path:
    sys.path.append(_LIB_DIR)

from pyrevit import forms, revit, script

from Autodesk.Revit.DB import ElementId

from System.Collections.Generic import List

from ckr_taglinked import (
    VERSION,
    compat,
    links,
    report,
    runner,
    settings,
    ui,
    viewvolume,
)

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()
log = compat.get_log()

TITLE = 'Tag Linked Services'
REPORT_DIR = os.path.join(compat.APP_DIR, 'reports')


class Progress(object):
    """Adapts pyRevit's progress bar to what the runner expects (NF-04)."""

    def __init__(self, bar):
        self.bar = bar

    @property
    def cancelled(self):
        try:
            return bool(self.bar.cancelled)
        except Exception:
            return False

    def update(self, current, total):
        try:
            self.bar.update_progress(current, total)
        except Exception:
            pass


def validate():
    """Return the active plan view, or None after explaining why not."""
    view = doc.ActiveView if doc is not None else None
    ok, message = viewvolume.validate_view(view)
    if not ok:
        forms.alert(message, title=TITLE)
        return None
    return view


MAX_LISTED_LINKS = 8


def _listed(lines):
    """Return at most MAX_LISTED_LINKS lines, with a count of the rest."""
    if len(lines) <= MAX_LISTED_LINKS:
        return '\n'.join(lines)
    shown = lines[:MAX_LISTED_LINKS]
    shown.append('- ...and {0} more'.format(len(lines) - MAX_LISTED_LINKS))
    return '\n'.join(shown)


def loaded_links(targets):
    """Return the readable targets, or None after explaining the problem."""
    if not targets:
        forms.alert(
            'This model contains no Revit links.\n\n'
            'Tag Linked Services annotates services that live in a linked '
            'model. For elements in this model, use Auto Tag.',
            title=TITLE)
        return None

    loaded = [target for target in targets if target.loaded]
    if loaded:
        return loaded

    reasons = _listed(links.unavailable_summary(targets))
    if all(target.is_nested for target in targets):
        forms.alert(
            'Every Revit link in this model is a nested link - a link '
            'inside another link:\n\n{0}\n\nPhase 1 tags top-level links '
            'only. Link the services model into this one directly and it '
            'can be tagged.'.format(reasons),
            title=TITLE)
    else:
        forms.alert(
            'None of the Revit links in this model can be read:\n\n{0}\n\n'
            'Load them from Manage > Manage Links > Revit: select the '
            'links and click Reload, then run the tool again.\n\n'
            'A model opened without its links loaded shows exactly '
            'this.'.format(reasons),
            title=TITLE)
    log.warning('No readable links: %s', ' | '.join(
        links.unavailable_summary(targets)))
    return None


def execute(view, values, targets, preview):
    """Run once behind a cancellable progress bar."""
    caption = 'Preview' if preview else 'Tag Linked Services'
    with forms.ProgressBar(title=caption + ': {value} of {max_value}',
                           cancellable=True, step=5) as bar:
        progress = Progress(bar)
        progress.update(0, 1)
        return runner.run(doc, view, values, targets, preview, progress, log)


def write_csv(results):
    """Write the CSV record of a run and return its path, or None (FR-09)."""
    try:
        if not os.path.isdir(REPORT_DIR):
            os.makedirs(REPORT_DIR)
        stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        name = 'tag_linked_services_{0}{1}.csv'.format(
            stamp, '_preview' if results.preview else '')
        path = os.path.join(REPORT_DIR, name)
        if report.export_csv(results, path):
            return path
    except Exception as ex:
        log.warning('CSV export failed: %s', ex)
    return None


def select_new_tags(results):
    """Leave the tags this run created selected in the model.

    The next thing anyone does after tagging is arrange the tags, and
    Align Tags works on the selection - so the run hands its work
    straight to it, and one Delete undoes a bad run without hunting.

    A preview selects nothing: its tags were rolled back and their ids no
    longer point at anything. Ids are checked before use, because a
    selection call rejects the whole set if one member has gone.

    Returns:
        int: How many tags ended up selected.
    """
    if results.preview or not results.placed_ids:
        return 0
    try:
        alive = [tag_id for tag_id in results.placed_ids
                 if doc.GetElement(tag_id) is not None]
        uidoc.Selection.SetElementIds(List[ElementId](alive))
        return len(alive)
    except Exception as ex:
        log.warning('The new tags could not be selected: %s', ex)
        return 0


def announce(results, selected=0):
    """Print the full report, write the CSV, then show the summary."""
    report.print_report(results, output)
    path = write_csv(results)
    if path:
        output.print_md('CSV report: `{0}`'.format(path))
    lines = report.summary_lines(results)
    if selected:
        lines.append('{0} new tag(s) are selected - run Align Tags to '
                     'arrange them.'.format(selected))
    if path:
        lines.append('A CSV copy is in {0}.'.format(REPORT_DIR))
    forms.alert('\n'.join(lines), title=TITLE)


def main():
    if doc is None or uidoc is None:
        forms.alert('Open a project document first.', title=TITLE)
        return

    view = validate()
    if view is None:
        return

    log.header(doc, view, VERSION)

    targets = links.discover(doc)
    available = loaded_links(targets)
    if available is None:
        return

    profile_name, values = settings.load_last()

    while True:
        action, values, profile_name = ui.show(doc, targets, values,
                                               profile_name)
        if action is None:
            return
        settings.save_last(profile_name, values)

        chosen = set(values.get('links') or [])
        selected = [target for target in available
                    if target.id_value in chosen]
        if not selected:
            forms.alert('None of the ticked links is loaded.', title=TITLE)
            continue

        results = execute(view, values, selected, action == 'preview')

        # FR-01.4: nested content is skipped, and the user is told so.
        results.warnings.extend(links.nested_warnings(selected))

        if results.blocked:
            # FR-02.3 / AT-13: no partial run when a tag family is missing.
            forms.alert('Nothing was tagged:\n\n{0}'.format(results.blocked),
                        title=TITLE)
            log.warning('Run blocked: %s', results.blocked.replace('\n', ' '))
            continue

        # Select before reporting: the summary can then say how many are
        # selected, and a modal dialog does not disturb a selection.
        selected = select_new_tags(results)
        announce(results, selected)
        if selected:
            log.info('%s new tag(s) left selected.', selected)

        if action != 'preview':
            return
        # A preview reopens the dialog with the settings intact, so the
        # numbers can be tuned against the counts they produced.


if __name__ == '__main__':
    try:
        main()
    except Exception:
        details = traceback.format_exc()
        log.error('Unhandled error:\n%s', details)
        script.get_logger().debug(details)
        forms.alert(
            'Tag Linked Services hit an unexpected error and stopped.\n\n'
            'Nothing is left half-done: the run is rolled back to its last '
            'committed step.\n\n'
            'Details were logged to {0}.'.format(compat.LOG_DIR),
            title=TITLE)
