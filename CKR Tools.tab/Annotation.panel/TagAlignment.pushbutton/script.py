# -*- coding: utf-8 -*-
"""MEP Tag Alignment - entry point.

Coordinates the workflow and owns the transactions and the UI. It contains no
business logic: every decision is delegated to a module.

    Select MEP elements      -> selection.py
    Validate the selection   -> validation.py
    Create / reuse tags      -> tag_manager.py
    Choose alignment method  -> alignment.py (registry) + this module's UI
    Align tag heads          -> alignment.py
    Maintain leader geometry -> leader_manager.py
    Report                   -> this module

Transactions are kept short and grouped so the whole run is a single undo
step - and a cancel at the method prompt rolls back cleanly.

Author: Naveen
Target: Revit 2024 / pyRevit / IronPython
"""

import os
import sys

# Make the sibling modules importable no matter how pyRevit loads this script.
_BUNDLE_DIR = os.path.dirname(__file__)
if _BUNDLE_DIR not in sys.path:
    sys.path.append(_BUNDLE_DIR)

from pyrevit import revit, forms, script

from Autodesk.Revit.DB import Transaction, TransactionGroup
from Autodesk.Revit.DB.Plumbing import Pipe

import alignment
import config
import leader_manager
import runs
import selection
import tag_manager
import utils
import validation

doc = revit.doc
uidoc = revit.uidoc
logger = script.get_logger()

TITLE = 'MEP Tag Alignment'


# ---------------------------------------------------------------------------
# UI (kept separate from the business logic in the modules)
# ---------------------------------------------------------------------------
def ask_tag_type(manager, category_value, category_name):
    """Ask which tag type to use for one category.

    The list comes from the tag families actually loaded in the project, so
    there is no name to spell wrong - you pick the real thing.

    Returns:
        ElementId | None: The chosen tag symbol, or None if cancelled / none
        available.
    """
    symbols = manager.list_tag_types(category_value)
    if not symbols:
        forms.alert(
            'No tag family is loaded for {}. Load one and run again.'.format(
                category_name),
            title=TITLE)
        return None

    label_to_id = {}
    for symbol in symbols:
        label = '{} : {}'.format(
            utils.get_family_name(symbol) or '?',
            utils.get_element_name(symbol) or '?')
        label_to_id[label] = symbol.Id

    chosen = forms.SelectFromList.show(
        sorted(label_to_id.keys()),
        title='Tag type for {}'.format(category_name),
        button_name='Use this tag',
        multiselect=False)

    if not chosen:
        return None
    return label_to_id[chosen]


def ask_alignment_method():
    """Ask the user which alignment method to apply.

    The options come from the alignment registry, so a newly registered
    strategy appears here automatically.

    Returns:
        str | None: The chosen method name, or None if cancelled.
    """
    return forms.CommandSwitchWindow.show(
        alignment.available_methods(),
        message='Choose alignment method:')


def report(method, created, reused, moved, leaders_updated, ignored, failures):
    """Show the completion summary, with any failures in the output window."""
    lines = [
        'MEP Tag Alignment complete.',
        '',
        'Alignment method:  {}'.format(method),
        'Tags created:      {}'.format(created),
        'Tags reused:       {}'.format(reused),
        'Tag heads aligned: {}'.format(moved),
        'Leaders tidied:    {}'.format(leaders_updated),
        'Ignored elements:  {}'.format(ignored),
    ]
    if failures:
        lines.append('Failures:          {}'.format(len(failures)))

    if failures:
        output = script.get_output()
        output.print_md('# {} - failures'.format(TITLE))
        for element_id, message in failures:
            output.print_md('- `{}` - {}'.format(element_id, message))

    forms.alert('\n'.join(lines), title=TITLE)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def main():
    """Run the workflow end to end."""
    view = doc.ActiveView

    # --- validate the view ------------------------------------------------
    view_ok, message = validation.validate_view(view)
    if not view_ok:
        forms.alert(message, title=TITLE)
        return

    # --- select and validate the elements ---------------------------------
    elements = selection.get_selected_elements(uidoc, doc)
    if not elements:
        forms.alert('Select one or more MEP elements first.', title=TITLE)
        return

    supported, ignored = validation.filter_supported_elements(elements)
    if not supported:
        forms.alert(
            'None of the selected elements belong to a supported MEP '
            'category. Select pipes, ducts, cable trays, conduits, their '
            'fittings/accessories, equipment, fixtures or air terminals.',
            title=TITLE)
        return

    manager = tag_manager.TagManager(doc, view)

    # --- choose the alignment method up front -----------------------------
    # Nothing is created until the method (and any input it needs) is known,
    # so backing out of any of these prompts leaves the model untouched. The
    # method also decides WHAT gets tagged: run-grouping methods tag one
    # representative per connected same-size run instead of every segment.
    method = ask_alignment_method()
    if not method:
        logger.debug('Alignment cancelled before anything was created.')
        return

    context = {'doc': doc}
    strategy = alignment.get_strategy(method)

    if strategy is not None and strategy.requires_reference_line:
        reference_line = selection.pick_reference_line(uidoc, doc)
        if reference_line is None:
            logger.debug('No reference line picked; nothing created.')
            return
        context['reference_line'] = reference_line

    # For run-grouping methods, tag one pipe per run; anything that isn't a
    # pipe (fittings, ducts, ...) is still tagged as-is.
    to_tag = supported
    if strategy is not None and strategy.groups_runs:
        pipes = [element for element in supported if isinstance(element, Pipe)]
        others = [element for element in supported if not isinstance(element, Pipe)]
        to_tag = runs.representatives(pipes) + others
        logger.debug('Run grouping: {} pipe(s) -> {} tag(s) + {} other.'.format(
            len(pipes), len(to_tag) - len(others), len(others)))

    # --- choose the tag type for anything that needs a NEW tag ------------
    # Categories whose tags all already exist are never asked about.
    if config.ASK_FOR_TAG_TYPE:
        pending = manager.categories_needing_tags(to_tag)
        for category_value, category_name in pending.items():
            symbol_id = ask_tag_type(manager, category_value, category_name)
            if symbol_id is None:
                logger.debug('Tag type selection cancelled.')
                return
            manager.set_tag_type(category_value, symbol_id)

    leaders = leader_manager.LeaderManager(doc, view)
    failures = []

    group = TransactionGroup(doc, TITLE)
    group.Start()
    try:
        # --- stage 1: every element ends up with exactly one usable tag ---
        with Transaction(doc, 'Create MEP Tags') as transaction:
            transaction.Start()
            tags, created, reused, tag_failures = manager.ensure_tags(to_tag)
            transaction.Commit()
        failures.extend(tag_failures)

        if not tags:
            group.RollBack()
            forms.alert(
                'No usable tags could be found or created for the selection. '
                'Check that a tag family is loaded for these categories.',
                title=TITLE)
            return

        # --- stage 2: align the heads, then repair the leaders ------------
        with Transaction(doc, 'Align MEP Tags') as transaction:
            transaction.Start()
            moved, move_failures = alignment.align_tags(
                tags, view, method, context)
            doc.Regenerate()  # Leaders must see the new head positions.

            # Horizontal runs get explicit L-shaped (90-degree) leaders; the
            # strategy leaves an elbow plan in the context for them. Everything
            # else keeps the clean toggle-rebuild. Managed tags are skipped by
            # maintain() so their elbows are not wiped.
            plan = context.get('leader_plan') or []
            managed = set(utils.element_id_value(tag.Id)
                          for tag, _elbow, _arrow in plan)

            leaders_updated = 0
            leader_failures = []
            if plan:
                set_count, elbow_failures = leaders.apply_elbows(
                    plan, free_end=config.HORIZONTAL_LEADER_FREE_END)
                leaders_updated += set_count
                leader_failures.extend(elbow_failures)

            rest = [tag for tag in tags
                    if utils.element_id_value(tag.Id) not in managed]
            refreshed, refresh_failures = leaders.maintain(rest)
            leaders_updated += refreshed
            leader_failures.extend(refresh_failures)

            transaction.Commit()
        failures.extend(move_failures)
        failures.extend(leader_failures)

        group.Assimilate()  # One undo step for the whole run.

    except Exception as ex:
        if group.HasStarted():
            group.RollBack()
        logger.error('{} failed: {}'.format(TITLE, ex))
        forms.alert('Unexpected error:\n{}'.format(ex), title=TITLE)
        return

    uidoc.RefreshActiveView()
    report(method, created, reused, moved, leaders_updated,
           len(ignored), failures)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        logger.error('Unhandled error: {}'.format(exc))
        forms.alert('Unexpected error:\n{}'.format(exc), title=TITLE)
