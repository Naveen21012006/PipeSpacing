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
import tool_config as config
import engine_bridge
import leader_manager
import snapshot
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
    """Return the alignment method to apply, asking only when there is a choice.

    The options come from the alignment registry, so a newly registered
    strategy appears here automatically. With a single method registered - the
    live state since the Cluster methods were retired - there is nothing to
    ask, so the run starts one click sooner.

    Returns:
        str | None: The method name, or None if cancelled / none registered.
    """
    methods = alignment.available_methods()
    if not methods:
        return None
    if len(methods) == 1:
        return methods[0]
    return forms.CommandSwitchWindow.show(
        methods, message='Choose alignment method:')


def pick_risers_by_flow():
    """Loop a direction prompt, picking the risers for each chosen direction.

    A button prompt (CommandSwitchWindow) asks which way to tag; you pick those
    risers and it returns to the prompt. Choose both directions (any order) or
    just one, then 'Done'. Everything merges into one column. Choosing 'Done'
    first (or closing the prompt) leaves both lists empty -> plain tagging.

    Returns:
        (down_elements, up_elements): the accumulated picked elements.
    """
    down_option = 'Top to bottom (down / return)'
    up_option = 'Bottom to top (up / supply)'
    done_option = 'Done - tag now'

    down_elements = []
    up_elements = []
    while True:
        choice = forms.CommandSwitchWindow.show(
            [down_option, up_option, done_option],
            message='Pick a flow direction, then select those risers:')
        if not choice or choice == done_option:
            break
        if choice == down_option:
            down_elements.extend(selection.prompt_for_elements(
                'Risers flowing TOP to BOTTOM (down): click them, then Finish.'))
        else:
            up_elements.extend(selection.prompt_for_elements(
                'Risers flowing BOTTOM to TOP (up): click them, then Finish.'))
    return down_elements, up_elements


def _is_plan_view(view):
    """True if the view looks straight down (a plan), where risers are points."""
    try:
        return view.GenLevel is not None
    except Exception:
        return False


def _is_vertical_pipe(element):
    """True if this element is a pipe running vertically (a riser)."""
    if not isinstance(element, Pipe):
        return False
    direction = utils.get_element_direction(element)
    return direction is not None and abs(direction.Z) >= 0.7


def _unique_elements(elements):
    """Drop repeats by element id, keeping order.

    The two riser picks are independent selections, so the same element (a
    valve or fitting on the riser) can be clicked in both - merging the lists
    would otherwise tag it twice.
    """
    seen = set()
    unique = []
    for element in elements:
        element_id = utils.element_id_value(element.Id)
        if element_id not in seen:
            seen.add(element_id)
            unique.append(element)
    return unique


def report(method, created, reused, moved, leaders_updated, ignored, failures):
    """Show the completion summary, with any failures in the output window."""
    total_tags = created + reused
    lines = [
        'MEP Tag Alignment complete.',
        '',
        'Alignment method:  {}'.format(method),
        'Tags created:      {}'.format(created),
        'Tags reused:       {}'.format(reused),
        'Tag heads aligned: {} of {}'.format(moved, total_tags),
        'Leaders tidied:    {}'.format(leaders_updated),
        'Ignored elements:  {}'.format(ignored),
    ]
    # "aligned X of N" is the honest diagnostic for an overlap pile: if X is far
    # below N, Revit rejected the head moves (they stayed piled at creation) -
    # the move failures below say which. A few unmoved on a re-run is normal
    # (already in position), so this is a number to read, not an alarm.
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

    # Shift+click normally runs the bundle's config.py (the spacing control);
    # this hook covers pyRevit setups where the flag reaches script.py instead.
    try:
        if __shiftclick__:      # noqa: F821 - injected by pyRevit
            import spacing_control
            spacing_control.ask_text_gap()
    except NameError:
        pass

    # --- validate the view ------------------------------------------------
    view_ok, message = validation.validate_view(view)
    if not view_ok:
        forms.alert(message, title=TITLE)
        return

    # Capture any pre-selection NOW, before the reference-line pick: starting a
    # PickObject clears the active selection, so reading it later would lose it.
    preselected = selection.get_preselected_elements(uidoc, doc)

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

    # The Auto method writes each pipe's designation into its Comments and uses
    # one ordinary tag family, so tell the manager to stop switching tag types.
    auto = strategy is not None and getattr(strategy, 'writes_comments', False)
    manager.set_comments_mode(auto)

    if strategy is not None and strategy.requires_reference_line:
        reference_line = selection.pick_reference_line(uidoc, doc)
        if reference_line is None:
            logger.debug('No reference line picked; nothing created.')
            return
        context['reference_line'] = reference_line

    # --- the two labelled riser picks (down risers, then up risers) -------
    # Both riser methods take these picks to learn each riser's flow. Geometry
    # (below / above the floor) is automatic; you supply only the direction, by
    # which pick you click a riser in.
    #
    #   Cluster Risers by Flow -> the picks ARE the selection (risers only).
    #   Auto Tag Pipes         -> the picks only set flow; the WHOLE selection
    #                             (horizontals + risers) is tagged, so the picks
    #                             do not narrow it.
    elements = None
    if (config.RISER_TAG_ENABLED and strategy is not None
            and getattr(strategy, 'assigns_riser_flow', False)
            and _is_plan_view(view)):
        down, up = pick_risers_by_flow()
        manager.set_riser_flow_elements(up, down)
        marked = _unique_elements(list(down) + list(up))
        if auto:
            # Tag the whole selection: the marked risers (flow known) plus every
            # flat pipe from the broad selection. Unmarked verticals are dropped
            # so a riser you did not give a direction never gets a blank tag.
            broad = preselected or selection.prompt_for_elements(
                'Select ALL the pipes to tag (risers included), then Finish.')
            flats = [e for e in broad if not _is_vertical_pipe(e)]
            elements = _unique_elements(marked + flats)
        elif marked:
            elements = marked

    # --- select and validate the elements ---------------------------------
    # The riser picks are the selection when they happened; otherwise a
    # pre-selection (captured before the picks) wins, else pick now.
    if not elements:
        elements = preselected or selection.prompt_for_elements()
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

    # For run-grouping methods, tag one pipe per run; anything that isn't a
    # pipe (fittings, ducts, ...) is still tagged as-is.
    to_tag = supported
    if strategy is not None and strategy.groups_runs:
        pipes = [element for element in supported if isinstance(element, Pipe)]
        others = [element for element in supported if not isinstance(element, Pipe)]
        if auto:
            # Group only the flat pipes into runs. Every marked riser keeps its
            # own tag (and its own Comments), because each storey slice carries
            # a different designation - grouping would collapse them into one.
            flats = [pipe for pipe in pipes if not _is_vertical_pipe(pipe)]
            risers = [pipe for pipe in pipes if _is_vertical_pipe(pipe)]
            to_tag = runs.representatives(flats) + risers + others
        else:
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

    # --- vision: capture the pre-placement ink map (Auto method only) -----
    # Exported with our own tag categories hidden (rolled-back transaction),
    # so a re-run never avoids its own previous tags. Failure means the
    # layout simply runs without the ink term, as before.
    if auto and config.AUTO_SNAPSHOT_ENABLED:
        context['ink_map'] = snapshot.capture_ink_map(
            uidoc, doc, view, list(config.SUPPORTED_CATEGORIES.values()))
        logger.debug('Ink map: {}'.format(
            'captured' if context.get('ink_map') else 'unavailable'))

    leaders = leader_manager.LeaderManager(doc, view)
    failures = []

    group = TransactionGroup(doc, TITLE)
    group.Start()
    try:
        # --- stage 1: every element ends up with exactly one usable tag ---
        with Transaction(doc, 'Create MEP Tags') as transaction:
            transaction.Start()
            # Auto method: stamp each pipe's designation into its Comments first
            # so the single tag family reads the right text once placed.
            if auto:
                _written, comment_failures = manager.write_pipe_comments(to_tag)
                failures.extend(comment_failures)
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
            engine_plan = context.get('engine_leader_plan') or []
            managed = set(utils.element_id_value(tag.Id)
                          for tag, _elbow, _arrow in plan + engine_plan)

            # Engine-planned leaders (the Auto method's horizontals) pin their
            # arrows exactly where the engine put them when the shared
            # attached_end setting says so (handoff s1/s3); riser drops and
            # fallback leaders keep the configured behaviour.
            correct = context.get('auto_correct')
            engine_pinned = bool(
                (correct or {}).get('settings', {}).get('attached_end', True))

            leaders_updated = 0
            leader_failures = []
            if plan:
                set_count, elbow_failures = leaders.apply_elbows(
                    plan, free_end=config.HORIZONTAL_LEADER_FREE_END)
                leaders_updated += set_count
                leader_failures.extend(elbow_failures)
            if engine_plan:
                set_count, elbow_failures = leaders.apply_elbows(
                    engine_plan, free_end=engine_pinned)
                leaders_updated += set_count
                leader_failures.extend(elbow_failures)

            rest = [tag for tag in tags
                    if utils.element_id_value(tag.Id) not in managed]
            refreshed, refresh_failures = leaders.maintain(rest)
            leaders_updated += refreshed
            leader_failures.extend(refresh_failures)

            # --- verify-correct (docs/autotag-align-handoff.md s4.3) ------
            # The tag family re-anchors its text with the leader state, so the
            # Auto method measures the DRAWN corner and re-plans once with the
            # residual - still inside this transaction, one undo step. On a
            # clean re-run the residual is under tolerance and nothing moves
            # (handoff s5.5).
            if correct:
                doc.Regenerate()
                fix_moves, fix_plan = engine_bridge.correct_placement(
                    correct['states'], correct['tags'], correct['elements'],
                    view, *utils.get_view_axes(view),
                    settings=correct['settings'])
                if fix_moves:
                    for tag, position in fix_moves:
                        try:
                            tag.TagHeadPosition = position
                        except Exception as ex:
                            logger.debug('Correction move failed: {}'.format(ex))
                    doc.Regenerate()
                    set_count, elbow_failures = leaders.apply_elbows(
                        fix_plan, free_end=engine_pinned)
                    leader_failures.extend(elbow_failures)

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

    # The drawn result, exported next to the log - the reviewer's eyes.
    if auto and config.AUTO_SNAPSHOT_ENABLED:
        snapshot.save_after(doc)

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
