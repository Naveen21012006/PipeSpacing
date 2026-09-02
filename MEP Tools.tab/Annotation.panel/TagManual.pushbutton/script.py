# -*- coding: utf-8 -*-
"""Tag Manual - Auto Tag's tagging with Align Tags' placement.

Built on the user's order (2026-09-02): "take all the tag placements from the
align tag ... Build this tool as new push button named Tag Manual keep the
auto tag push button untouched."

The tool is a marriage of the two siblings, each doing the half it is trusted
with, and NEITHER sibling is modified:

  stage 1 - TAGGING, from TagAlignment.pushbutton (imported live):
      select pipes -> write each horizontal pipe's designation (AT H/L /
      AT L/L by height) into its built-in Comments -> group flat runs one
      tag per run, risers one tag per storey slice -> create or reuse tags.
      One assimilated undo step.

  stage 2 - PLACEMENT, from AlignTags.pushbutton (imported live):
      the field-tested cluster-by-cluster pick loop: the selection splits
      into physical pipe racks, each rack highlights in turn, you pick its
      stack's lowest tag position (Esc = next rack), and a final pass
      resolves cross-rack overlaps. Each pick is its own undo step,
      exactly as in Align Tags itself.

Nothing here is a mirror. Align Tags' script.py cannot be imported by name
(module-name collision with every bundle's own script.py), so it is exec-
loaded under the alias 'aligntags_script' - its ``if __name__ == '__main__'``
guard makes that safe (verified 2026-09-02) - and its functions are called
as-is: partition_targets, report_skips, skip_summary, run_pick_loop,
export_snapshot. If Align Tags' behaviour changes, Tag Manual follows
automatically; there is no copy to drift. (Protocol with the Align Tags
maintainer: those five names stay stable, or a heads-up comes first.)

Author: Naveen
Target: Revit 2022-2026 / pyRevit / IronPython
"""

import io
import os
import sys
import traceback
import types

_BUNDLE_DIR = os.path.dirname(__file__)
_PANEL_DIR = os.path.dirname(_BUNDLE_DIR)
_AUTO_DIR = os.path.join(_PANEL_DIR, 'TagAlignment.pushbutton')
_ALIGN_DIR = os.path.join(_PANEL_DIR, 'AlignTags.pushbutton')
for _path in (_BUNDLE_DIR, _AUTO_DIR, _ALIGN_DIR):
    if _path not in sys.path:
        sys.path.append(_path)

from pyrevit import forms, revit, script

from Autodesk.Revit.DB import Transaction, TransactionGroup
from Autodesk.Revit.DB.Plumbing import Pipe

# TagAlignment's modules (unique names - no collision with Align Tags').
import tool_config as config
import runs
import selection
import tag_manager
import utils
import validation

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()

TITLE = 'Tag Manual'


# ---------------------------------------------------------------------------
# Align Tags, loaded live
# ---------------------------------------------------------------------------
def _load_align():
    """Exec-load AlignTags/script.py under an alias and return the module.

    ``import script`` would collide with this bundle's own entry point, so the
    file is compiled and executed into a module registered as
    'aligntags_script'. Its __main__ guard keeps its command from running;
    everything else - the pick loop, the cluster split, the measurement pass,
    the final arrangement - comes out callable and REAL, not mirrored.
    """
    alias = 'aligntags_script'
    if alias in sys.modules:
        return sys.modules[alias]
    path = os.path.join(_ALIGN_DIR, 'script.py')
    source = io.open(path, encoding='utf-8').read()
    module = types.ModuleType(alias)
    module.__file__ = path
    sys.modules[alias] = module
    try:
        exec(compile(source, path, 'exec'), module.__dict__)
    except Exception:
        sys.modules.pop(alias, None)
        raise
    return module


# ---------------------------------------------------------------------------
# Pick priority: vertical racks first, each family top-to-bottom
# ---------------------------------------------------------------------------
# (User rule, 2026-09-02: "align priority is first of all it should start
# from vertical cluster that is also from top to bottom wise". The same
# vertical-first doctrine as Auto Tag, 2026-08-03, applied to the PICK ORDER.)
#
# Align Tags' split_clusters sorts racks left-to-right; membership is exactly
# what Tag Manual wants, the ORDER is not. So the loaded alias gets a wrapper
# that re-sorts its output - Align Tags' own button is untouched (it runs its
# file fresh, never this instance) and rack membership is never altered.
#
# A pleasant consequence: the final-arrangement cleanup moves the LATER-placed
# rack of a conflicting pair, so placing vertical racks first also means they
# WIN cross-rack conflicts - the priority holds through cleanup, not just at
# the prompt.
def _rack_family(targets, members, basis, to_2d):
    """0 = screen-vertical rack (straight-leader family), 1 = the rest.

    Per member, the drawn direction on screen decides - the same test Auto
    Tag classifies with: a pipe is 'across the page' only when its u-travel
    exceeds its v-travel. A riser point has no drawn direction and counts
    for neither; a rack of only risers (or of tags with no pipe curve)
    belongs with the drop family.
    """
    vertical = horizontal = 0
    for index in members:
        pair = targets[index].tagged_curve()
        if pair is None:
            continue
        u0, v0 = to_2d(pair[0], basis)
        u1, v1 = to_2d(pair[1], basis)
        du, dv = abs(u1 - u0), abs(v1 - v0)
        if du < 1e-9 and dv < 1e-9:
            continue                    # a point in this view: a riser
        if du > dv:
            horizontal += 1
        else:
            vertical += 1
    return 0 if vertical > horizontal else 1


def _rack_position(targets, members):
    """(top_v, centre_u) of a rack, read from the leader arrows."""
    tops, centres = [], []
    for index in members:
        u, v = getattr(targets[index], 'primary_end2d', (0.0, 0.0))
        tops.append(v)
        centres.append(u)
    return max(tops), sum(centres) / float(len(centres))


def _install_pick_priority(align):
    """Re-order the alias's split_clusters: vertical racks first, then
    top-to-bottom within each family (left-to-right as the tie-break).
    Idempotent - the alias survives across clicks in one pyRevit session."""
    if getattr(align, '_tagmanual_pick_priority', False):
        return
    original = align.split_clusters

    def prioritised(targets, config, basis):
        groups = original(targets, config, basis)

        def key(members):
            top_v, centre_u = _rack_position(targets, members)
            return (_rack_family(targets, members, basis,
                                 align.common.to_2d),
                    -top_v, centre_u, min(members))

        return sorted(groups, key=key)

    align.split_clusters = prioritised
    align._tagmanual_pick_priority = True


# ---------------------------------------------------------------------------
# Stage-1 helpers (orchestration shared with Auto Tag's entry point; the
# LOGIC all lives in the imported TagAlignment modules)
# ---------------------------------------------------------------------------
def _is_vertical_pipe(element):
    """True if this element is a pipe running vertically (a riser)."""
    if not isinstance(element, Pipe):
        return False
    direction = utils.get_element_direction(element)
    return direction is not None and abs(direction.Z) >= 0.7


def _one_tag_per_run(supported):
    """Group flat pipes into runs (one tag each); risers stay per slice.

    Same rule as Auto Tag (rule book R4): a riser is sliced one segment per
    storey and every slice carries its own Comments, so grouping would
    collapse a stack onto a single label. Non-pipes tag individually.
    """
    pipes = [e for e in supported if isinstance(e, Pipe)]
    others = [e for e in supported if not isinstance(e, Pipe)]
    flats = [p for p in pipes if not _is_vertical_pipe(p)]
    risers = [p for p in pipes if _is_vertical_pipe(p)]
    return runs.representatives(flats) + risers + others


def _ask_tag_type(manager, category_value, category_name):
    """Ask which tag type to use for one category (list = what is loaded)."""
    symbols = manager.list_tag_types(category_value)
    if not symbols:
        forms.alert(
            'No tag family is loaded for {}. Load one and run again.'.format(
                category_name),
            title=TITLE)
        return None

    label_to_id = {}
    labels = []
    for symbol in symbols:
        label = '{} : {}'.format(
            utils.get_family_name(symbol) or '?',
            utils.get_element_name(symbol) or '?')
        label_to_id[label] = symbol.Id
        labels.append(label)
    choice = forms.CommandSwitchWindow.show(
        sorted(labels),
        message='Tag type for {}:'.format(category_name))
    return label_to_id.get(choice)


def _shift_clicked():
    try:
        return bool(__shiftclick__)     # noqa: F821 - pyRevit injects it
    except NameError:
        return False


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------
def main():
    if doc is None or uidoc is None:
        forms.alert('Open a project document first.', title=TITLE)
        return
    view = doc.ActiveView
    ok, message = validation.validate_view(view)
    if not ok:
        forms.alert(message, title=TITLE)
        return

    # Preselection must be read before any prompt - a pick clears it.
    preselected = selection.get_preselected_elements(uidoc, doc)
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

    manager = tag_manager.TagManager(doc, view)
    manager.set_comments_mode(True)   # designation -> pipe Comments (R5-R9)
    to_tag = _one_tag_per_run(supported)

    # Tag-type choice happens BEFORE any transaction, so cancelling here
    # leaves the model untouched.
    if config.ASK_FOR_TAG_TYPE:
        pending = manager.categories_needing_tags(to_tag)
        for category_value, category_name in pending.items():
            symbol_id = _ask_tag_type(manager, category_value, category_name)
            if symbol_id is None:
                return
            manager.set_tag_type(category_value, symbol_id)

    # --- stage 1: Comments + tags, one assimilated undo step --------------
    failures = []
    group = TransactionGroup(doc, TITLE + ' - create tags')
    group.Start()
    try:
        with Transaction(doc, 'Create MEP Tags') as transaction:
            transaction.Start()
            written, comment_failures = manager.write_pipe_comments(to_tag)
            failures.extend(comment_failures)
            tags, created, reused, tag_failures = manager.ensure_tags(to_tag)
            failures.extend(tag_failures)
            transaction.Commit()
        if not tags:
            group.RollBack()
            forms.alert(
                'No usable tags could be found or created for the '
                'selection. Check that a tag family is loaded for these '
                'categories.', title=TITLE)
            return
        group.Assimilate()
    except Exception:
        if group.HasStarted():
            group.RollBack()
        raise

    # --- stage 2: Align Tags places them, rack by rack --------------------
    align = _load_align()
    _install_pick_priority(align)   # vertical racks first, top to bottom

    wrapped = []
    for tag in tags:
        wrapper = align.wrappers.wrap(tag, doc)
        if wrapper is not None:
            wrapped.append(wrapper)
    targets, skipped = align.partition_targets(wrapped)
    align.report_skips(skipped)
    if not targets:
        forms.alert(
            'The tags were created, but none can be aligned:\n\n'
            + align.skip_summary(skipped), title=TITLE)
        return

    # Placement settings are Align Tags' own: same file, same dialog, same
    # defaults - a stack placed by either button is indistinguishable.
    loaded = align.settings.load()
    if _shift_clicked() or not os.path.exists(align.settings.SETTINGS_PATH):
        placement = align.ui.show_config(doc, loaded)
        if placement is None:
            output.print_md(
                ':information_source: Placement cancelled - the {0} new / '
                '{1} reused tag(s) and Comments stay as created; run Align '
                'Tags on them whenever you like.'.format(created, reused))
            return
        align.settings.save(placement)
    else:
        placement = loaded
        output.print_md(
            ':gear: Using the saved Align Tags settings. Shift+click the '
            'button to open the placement dialog.')

    attached_in = [w for w in targets if w.attached_end]
    if attached_in:
        if placement['attached_end']:
            output.print_md(
                ':warning: {0} attached leader end(s) will be freed and '
                'pinned on the element so the slant angle is exact; the '
                'arrowhead may shift slightly.'.format(len(attached_in)))
        else:
            output.print_md(
                ':warning: {0} tag(s) have attached leader ends; Revit '
                'controls their arrowheads, so the slant angle is '
                'approximate for them.'.format(len(attached_in)))

    picks, flagged = align.run_pick_loop(targets, placement)

    if picks and flagged:
        output.print_md(
            ':warning: {0} leader(s) could not honour the exact angle on '
            'the final pick (end point behind the stack); their landing '
            'was collapsed instead.'.format(flagged))
    if picks:
        align.export_snapshot()

    # --- the receipt ------------------------------------------------------
    summary = (':white_heavy_check_mark: **Tag Manual**: {0} tag(s) created, '
               '{1} reused, Comments written on {2} pipe(s), {3} pick(s).'
               .format(created, reused, written, picks))
    if ignored:
        summary += ' {0} unsupported element(s) ignored.'.format(len(ignored))
    output.print_md(summary)
    if failures:
        output.print_md(':warning: {0} element(s) failed:'.format(
            len(failures)))
        for element_id, reason in failures:
            output.print_md('  - `{0}`: {1}'.format(element_id, reason))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        details = traceback.format_exc()
        utils.logger.error('Tag Manual failed:\n{}'.format(details))
        forms.alert(
            'Tag Manual hit an unexpected error and stopped. Anything '
            'half-done was rolled back.\n\n{0}'.format(details.splitlines()[-1]),
            title=TITLE)
