# -*- coding: utf-8 -*-
"""Align Tags - entry point.

Workflow (spec, Feature 1):
  1. Use the preselected tags/text notes if any, else prompt a filtered
     selection (picker.py).
  2. Show the configuration dialog (ui.py); settings persist across
     sessions (settings.py).
  3. Repeat: "Pick lowest tag head position. Press Esc to finish." Every
     pick re-aligns the whole set inside its own assimilated transaction
     group, so each pick is exactly one undo step and the last state
     persists when the user presses Esc.

The geometry lives in engine.py (pure, unit-tested outside Revit); Revit
API differences live in wrappers.py. Spacing values are interpreted as
MODEL distances in the active view's plane.

No unhandled exception may surface to the Revit UI: the whole command is
wrapped, failures are logged to %APPDATA%/CKR/logs and reported with a
friendly alert.

Author: Naveen
Target: Revit 2022-2026 / pyRevit / IronPython
"""

import os
import sys
import traceback

_BUNDLE_DIR = os.path.dirname(__file__)
if _BUNDLE_DIR not in sys.path:
    sys.path.append(_BUNDLE_DIR)

from pyrevit import forms, revit, script

from Autodesk.Revit.DB import ElementId, Transaction, TransactionGroup
from Autodesk.Revit.Exceptions import OperationCanceledException
from Autodesk.Revit.UI.Selection import ObjectSnapTypes

from System.Collections.Generic import List

import arrange
import clusters
import common
import engine
import picker
import settings
import ui
import wrappers

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()
file_log = common.get_file_logger()

# pyRevit injects __shiftclick__; guard so the module also imports under
# plain CPython (tests) and older hosts.
try:
    __shiftclick__
except NameError:
    __shiftclick__ = False

TITLE = 'Align Tags'
PICK_PROMPT = 'Pick lowest tag head position. Press Esc to finish.'

# User-chosen constants (corner decision sheet, 2026-07-26):
FITTING_CLEARANCE_MM = 250.0   # arrows keep this far from bends/ends
CLUSTER_SPAN_CAP_MM = 4000.0   # auto-split chains wider than this
BUNDLE_LATERAL_MM = 600.0      # rack width: max pipe-to-pipe offset


_CHAR_WIDTH_FACTOR = 0.6   # mean glyph width as a fraction of text height


def _text_width_hint(wrapper, view):
    """Independent estimate of a tag's text width, in feet, or None.

    Diagnostic only. Derived from the tag's OWN text and its type's text
    size, so a leader that survives suppression cannot contaminate it -
    unlike the bounding box, which does on every tag in the user's
    project. Returns None when the family does not expose what it needs.
    """
    try:
        element = wrapper.element
        if wrapper.kind == 'textnote':
            return float(element.Width)      # exact, already model units
        text = getattr(element, 'TagText', None)
        if not text:
            return None
        longest = max(len(line) for line in (text.splitlines() or [text]))
        symbol = getattr(element, 'Symbol', None)
        if symbol is None:
            return None
        from Autodesk.Revit.DB import BuiltInParameter
        param = symbol.get_Parameter(BuiltInParameter.TEXT_SIZE)
        if param is None:
            return None
        size_ft = float(param.AsDouble())    # paper units
        scale = float(getattr(view, 'Scale', 1) or 1)
        return longest * size_ft * _CHAR_WIDTH_FACTOR * scale
    except Exception as ex:
        common.logger.debug('Text width hint failed: {}'.format(ex))
        return None


def _length_ft(config, key, default_mm):
    """A configured length in feet, falling back to the built-in default.

    The dialog exposes these (spec Feature 2, "all settings"), but the
    tool must still run from a settings file written before they existed.
    """
    try:
        value = float(config.get(key, default_mm))
    except (TypeError, ValueError):
        value = default_mm
    if value < 0.0:
        value = default_mm
    return common.mm_to_feet(value)

# `None` is a keyword in Python, so the no-snapping enum member needs getattr.
_SNAP_NONE = getattr(ObjectSnapTypes, 'None')


# ---------------------------------------------------------------------------
# Target gathering
# ---------------------------------------------------------------------------
def gather_targets():
    """Return wrappers for the tags to align (preselection first)."""
    wrapped, _ = picker.get_preselected(uidoc, doc)
    if wrapped:
        return wrapped
    return picker.prompt_for_tags(uidoc, doc)


def partition_targets(wrapped):
    """Split wrappers into usable targets and per-reason skip lists.

    Spec (task 8): tags without leaders and pinned tags are skipped and
    reported, never silently dropped. Attached-end tags are ALWAYS
    processed - stock Revit tags ship with attached leader ends, so
    skipping them would exclude nearly every real MEP tag; Revit keeps
    their arrowhead on the element while the head and elbow move.
    """
    usable, skipped = [], {'no_leader': [], 'pinned': [], 'no_end': []}
    for wrapper in wrapped:
        if not wrapper.has_leader:
            skipped['no_leader'].append(wrapper)
        elif wrapper.is_pinned:
            skipped['pinned'].append(wrapper)
        elif wrapper.primary_end() is None:
            skipped['no_end'].append(wrapper)
        else:
            usable.append(wrapper)
    return usable, skipped


_SKIP_MESSAGES = {
    'no_leader': 'without a leader - tick "Leader" in the tag\'s '
                 'Properties (or on the Modify ribbon) and rerun',
    'pinned': 'pinned - unpin to align',
    'no_end': 'whose leader end could not be read',
}


def report_skips(skipped):
    """Print one line per skip reason (only when something was skipped)."""
    for reason, items in skipped.items():
        if items:
            ids = ', '.join(str(w.id_value) for w in items[:10])
            output.print_md(':warning: Skipped {0} tag(s) {1}: {2}'.format(
                len(items), _SKIP_MESSAGES[reason], ids))


def skip_summary(skipped):
    """One line per reason for the user-facing alert - no window hunting."""
    lines = []
    for reason in ('no_leader', 'pinned', 'no_end'):
        if skipped[reason]:
            lines.append('- {0} {1}'.format(
                len(skipped[reason]), _SKIP_MESSAGES[reason]))
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Alignment application
# ---------------------------------------------------------------------------
def resolve_justification(config):
    """Return 'left'/'right'/None for the text-note justification to apply.

    'automatic' picks the side away from the elements: leaders exiting
    right mean the text extends leftward, so right-justified reads best,
    and vice versa.
    """
    just = config['justification']
    if just in ('left', 'right'):
        return just
    if just == 'automatic':
        eff = engine.resolve_mode(config['mode'], config['switch_side'])
        return 'right' if engine.exit_sign(eff) > 0 else 'left'
    return None


def build_items(targets, basis):
    """Project every wrapper's primary leader end into view-plane 2D.

    The primary leader is the first one whose end READS - not necessarily
    leader 0 - so its position is remembered on the wrapper and apply_plan
    puts the planned elbow on that same leader (review finding).
    """
    items = []
    for index, wrapper in enumerate(targets):
        position, end3d = wrapper.primary_leader()
        wrapper.primary_position = position
        wrapper.primary_end3d = end3d  # kept for pinning freed arrowheads
        end2d = common.to_2d(end3d, basis)
        wrapper.primary_end2d = end2d  # clustering + off-axis anchoring
        items.append({'key': index, 'end': end2d})
    return items


def apply_plan(targets, plan, basis, justification, config,
               move_ends=False):
    """Apply one alignment plan inside a single committed transaction.

    Order matters: justification first (it changes what Coord anchors on a
    text note), then every head, one regenerate, then every elbow - so
    elbows are computed against final head positions and Revit has already
    rebuilt the leaders once.

    ``move_ends`` (order-by-pipe mode): the plan DERIVED the arrow points,
    so free leader ends are pinned to them; attached ends are freed and
    pinned only when the "Attached End Tags" option allows, otherwise the
    elbow alone steers them and Revit keeps the arrow on the pipe.

    Returns:
        tuple: (moved, elbow_failures, angle_flagged)
    """
    moved = 0
    elbow_failures = 0
    angle_flagged = 0
    by_key = dict((entry['key'], entry) for entry in plan)

    txn = Transaction(doc, TITLE)
    txn.Start()
    try:
        if justification:
            for wrapper in targets:
                wrapper.set_justification(justification)

        if move_ends:
            # Re-runs must behave like first runs: previous alignments
            # leave their elbows behind, and Revit can keep the old one
            # when the tag is re-aligned (the "grip stuck near the pipe"
            # bug). The proven cure from the Auto Tag bundle: toggle
            # every leader off, regenerate, toggle back on - Revit
            # rebuilds each leader clean, then head/arrow/elbow are set
            # fresh below. Only done in order-by-pipe mode, where the
            # arrow position is ours to place afterwards.
            rebuilt = []
            for wrapper in targets:
                if wrapper.toggle_leader(False):
                    rebuilt.append(wrapper)
                else:
                    # Toggle refused (free-end tags on some versions):
                    # park the stale elbow on the head instead - the real
                    # elbow is set fresh below, and the verify/retry pass
                    # confirms it took.
                    try:
                        head = wrapper.get_head()
                        for key in wrapper.leader_keys():
                            wrapper.set_elbow(key, head)
                    except Exception as ex:
                        common.logger.debug(
                            'Elbow reset failed: {}'.format(ex))
            if rebuilt:
                doc.Regenerate()
                for wrapper in rebuilt:
                    wrapper.toggle_leader(True)
            doc.Regenerate()

        for index, wrapper in enumerate(targets):
            entry = by_key[index]
            depth = common.depth_of(wrapper.get_head(), basis)
            if wrapper.set_head(common.to_3d(entry['head'], depth, basis)):
                moved += 1
            if not entry['angle_ok']:
                angle_flagged += 1

            keys = wrapper.leader_keys()
            primary = getattr(wrapper, 'primary_position', 0) or 0
            end_ref = getattr(wrapper, 'primary_end3d', None)
            if move_ends and keys and end_ref is not None:
                # Arrow position comes from the plan (on the pipe).
                end_depth = common.depth_of(end_ref, basis)
                new_end = common.to_3d(entry['end'], end_depth, basis)
                if wrapper.attached_end:
                    if config['attached_end'] and wrapper.make_free():
                        wrapper.set_end(keys[primary], new_end)
                else:
                    wrapper.set_end(keys[primary], new_end)
            elif config['attached_end'] and wrapper.attached_end:
                # "Attached End Tags" on: free the leader end and pin the
                # arrowhead where the plan expects it (the point on the
                # element nearest the head) so the angle is honoured.
                if keys and end_ref is not None and wrapper.make_free():
                    wrapper.set_end(keys[primary], end_ref)

        doc.Regenerate()

        for index, wrapper in enumerate(targets):
            entry = by_key[index]
            depth = common.depth_of(wrapper.get_head(), basis)
            keys = wrapper.leader_keys()
            primary = getattr(wrapper, 'primary_position', 0)
            for key_index, key in enumerate(keys):
                if key_index == primary:
                    # Straight leaders carry their elbow at the line's
                    # midpoint (engine) - safe for glued arrows because
                    # the whole line runs at the text centre height.
                    elbow2d = entry['elbow']
                else:
                    # Extra leaders of a multi-reference tag: same head,
                    # own end, own elbow at the configured angle.
                    end3d = wrapper.get_end(key)
                    if end3d is None:
                        elbow_failures += 1
                        continue
                    elbow2d, _ = engine.elbow_for(
                        entry['head'], common.to_2d(end3d, basis),
                        engine.resolve_mode(config['mode'],
                                            config['switch_side']),
                        config['angle_deg'])
                target3d = common.to_3d(elbow2d, depth, basis)
                ok = wrapper.set_elbow(key, target3d)
                if ok:
                    # Self-verify: Revit sometimes accepts the call but
                    # keeps the old elbow - read it back, retry once.
                    check = wrapper.get_elbow(key)
                    if (check is not None
                            and check.DistanceTo(target3d) > 0.01):
                        wrapper.set_elbow(key, target3d)
                        check = wrapper.get_elbow(key)
                        ok = (check is None
                              or check.DistanceTo(target3d) <= 0.01)
                if not ok:
                    elbow_failures += 1

        doc.Regenerate()

        # Ground-truth audit: read back what Revit ACTUALLY did. Every
        # silent-defiance bug so far (ignored HasLeader, stale elbows)
        # was invisible until a screenshot arrived - now the tool checks
        # its own work and confesses in the log immediately.
        audit_bad = 0
        tolerance = common.mm_to_feet(10.0)
        for index, wrapper in enumerate(targets):
            entry = by_key[index]
            try:
                actual = common.to_2d(wrapper.get_head(), basis)
            except Exception:
                continue
            off_u = actual[0] - entry['head'][0]
            off_v = actual[1] - entry['head'][1]
            deviation = (off_u * off_u + off_v * off_v) ** 0.5
            if deviation > tolerance:
                audit_bad += 1
                file_log.warning(
                    'Audit: tag %s head off-plan by %.0fmm.',
                    wrapper.id_value, common.feet_to_mm(deviation))
        if audit_bad:
            output.print_md(
                ':warning: Ground-truth audit: {0} tag(s) did not land '
                'where planned - details in the log.'.format(audit_bad))

        txn.Commit()
    except Exception:
        if txn.HasStarted():
            txn.RollBack()
        raise

    return moved, elbow_failures, angle_flagged


def measure_layout(targets, basis):
    """Measure text sizes with leaders suppressed; return tallest height.

    Vertical Spacing is a clear GAP between stacked tags, so the row pitch
    needs the real text height - which depends on the tag family and the
    view scale. Leaders are toggled off inside a transaction that is then
    ROLLED BACK: the document is untouched, no undo entry appears, but the
    measured numbers survive in memory.

    Also stores on each wrapper (for the order-by-pipe corner anchoring):
        ``bbox2d``     (u_lo, u_hi, v_lo, v_hi) of the text alone,
        ``head_ref2d`` the head position at measure time - the offset
                       between them is constant however the tag moves.
    """
    right, up = basis[0], basis[1]
    tallest = 0.0
    txn = Transaction(doc, 'Measure tags (rolled back)')
    txn.Start()
    try:
        prepared = False
        refused = 0
        for wrapper in targets:
            if wrapper.height_hint() is not None:
                continue   # text notes: known height, leader left alone
            # ALWAYS collapse the leader onto the head first: this Revit
            # host ACCEPTS HasLeader=False yet keeps drawing the leader
            # (log-proven - every session shows inflated boxes with zero
            # toggle refusals), so the toggle alone cannot be trusted. A
            # collapsed leader is a point: the bbox becomes pure text and
            # its centre is the TRUE attachment height - without this,
            # landings came out slightly sloped. All inside the
            # rolled-back transaction; nothing persists.
            try:
                head = wrapper.get_head()
                for key in wrapper.leader_keys():
                    prepared = wrapper.set_end(key, head) or prepared
                    prepared = wrapper.set_elbow(key, head) or prepared
                    # Verify the elbow really moved - this family is
                    # known to ignore elbow calls sometimes; retry.
                    check = wrapper.get_elbow(key)
                    if (check is not None
                            and check.DistanceTo(head) > 0.01):
                        wrapper.set_elbow(key, head)
            except Exception as ex:
                common.logger.debug(
                    'Leader collapse failed: {}'.format(ex))
            if wrapper.toggle_leader(False):
                prepared = True
            else:
                refused += 1
        if prepared:
            doc.Regenerate()
        if refused:
            file_log.info(
                'Leader toggle refused on %s tag(s); collapse covered '
                'them.', refused)
        for wrapper in targets:
            u_span = common.extent_along(wrapper.element, doc.ActiveView,
                                         right)
            v_span = common.extent_along(wrapper.element, doc.ActiveView,
                                         up)
            if u_span and v_span:
                head2d = common.to_2d(wrapper.get_head(), basis)
                u_lo, u_hi = u_span
                v_lo, v_hi = v_span
                # Backstop against leaders that survived suppression: a
                # tag family's head sits at the text centre, so the true
                # text extent is at most 2x the nearer head-to-edge
                # distance per axis - a surviving leader inflates ONE
                # side only. Cap and log when the raw bbox disagrees.
                if wrapper.kind in ('tag', 'spatial'):
                    raw_w = u_hi - u_lo
                    sym_w = 2.0 * min(head2d[0] - u_lo, u_hi - head2d[0])
                    capped = sym_w > 0.0 and raw_w > 1.5 * sym_w
                    if capped:
                        u_lo = head2d[0] - sym_w / 2.0
                        u_hi = head2d[0] + sym_w / 2.0
                    # Three-way comparison so the next Revit run tells us
                    # whether the estimate can be trusted: the bbox is
                    # leader-contaminated on every tag in the user's
                    # project, so the estimate is doing load-bearing work
                    # and the text-derived figure is the only independent
                    # check on it.
                    hint = _text_width_hint(wrapper, doc.ActiveView)
                    file_log.info(
                        'Tag %s width: bbox %.0fmm, estimate %.0fmm, '
                        'text %s -> used %.0fmm%s.',
                        wrapper.id_value,
                        common.feet_to_mm(raw_w),
                        common.feet_to_mm(sym_w) if sym_w > 0.0 else 0.0,
                        '{0:.0f}mm'.format(common.feet_to_mm(hint))
                        if hint else 'n/a',
                        common.feet_to_mm(u_hi - u_lo),
                        ' (leader survived suppression)' if capped else '')
                    sym_h = 2.0 * min(head2d[1] - v_lo, v_hi - head2d[1])
                    if sym_h > 0.0 and (v_hi - v_lo) > 1.5 * sym_h:
                        v_lo = head2d[1] - sym_h / 2.0
                        v_hi = head2d[1] + sym_h / 2.0
                wrapper.bbox2d = (u_lo, u_hi, v_lo, v_hi)
                wrapper.head_ref2d = head2d
                height = wrapper.height_hint()
                if height is None:
                    height = v_hi - v_lo
            else:
                wrapper.bbox2d = None
                wrapper.head_ref2d = None
                height = wrapper.height_hint() or 0.0
            wrapper.text_height = height   # per-cluster pitch uses this
            tallest = max(tallest, height)
    finally:
        txn.RollBack()
    file_log.info('Measured %s tag(s); tallest text %.0fmm; %s toggle '
                  'refusal(s).', len(targets),
                  common.feet_to_mm(tallest), refused)
    return tallest


def build_ordered_bundle(targets, basis, straight_mode):
    """Pipe geometry for the order-by-pipe mode, or None if not applicable.

    Applicable when EVERY target tags a curve element (pipe, duct, tray).
    The bundle axis (dominant run direction) decides the STACK order;
    at angle 0 each item also carries its OWN pipe direction and real
    extent so the engine can pick straight / tilted-climb per tag. In
    slanted mode, cross-direction pipes keep the old pin-to-arrow
    behavior.
    """
    curves = []
    for wrapper in targets:
        pair = wrapper.tagged_curve()
        if pair is None:
            return None
        curves.append((common.to_2d(pair[0], basis),
                       common.to_2d(pair[1], basis)))

    run_u = sum(abs(b[0] - a[0]) for a, b in curves)
    run_v = sum(abs(b[1] - a[1]) for a, b in curves)
    bundle = 'v' if run_v >= run_u else 'h'

    items = []
    for i, (a, b) in enumerate(curves):
        own = 'v' if abs(b[1] - a[1]) >= abs(b[0] - a[0]) else 'h'
        if own == 'v':
            item = {'key': i, 'own': 'v', 'pos': (a[0] + b[0]) / 2.0,
                    'span': (min(a[1], b[1]), max(a[1], b[1]))}
        else:
            item = {'key': i, 'own': 'h', 'pos': (a[1] + b[1]) / 2.0,
                    'span': (min(a[0], b[0]), max(a[0], b[0]))}
        if own != bundle:
            # Cross-running pipe: order it in the stack by where its
            # arrow actually sits on the bundle axis.
            arrow = getattr(targets[i], 'primary_end2d', None)
            if arrow is None:
                arrow = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
            item['order_pos'] = arrow[0] if bundle == 'v' else arrow[1]
            if not straight_mode:
                # Slanted mode: pin to the existing arrow point (the old,
                # proven behavior for cross pipes at 45-degree leaders).
                if bundle == 'v':
                    item = {'key': i, 'pos': arrow[0], 'cross': True,
                            'span': (arrow[1], arrow[1])}
                else:
                    item = {'key': i, 'pos': arrow[1], 'cross': True,
                            'span': (arrow[0], arrow[0])}
        items.append(item)
    return bundle, items


def ordered_offsets(wrapper, eff_mode):
    """(head_offset, line_offset, exit_edge) from the anchor corner.

    The anchor corner is the text's bottom corner on the far side from the
    pipes: bottom-LEFT when leaders exit right, bottom-RIGHT when they
    exit left - the point the user expects their click to place. The
    leader line runs at the TEXT's mid-height (line_offset), which is
    where Revit actually attaches it - using the head's height instead
    kinks the landing on families whose head is not the text centre.

    Returns None when this tag's text could not be measured; the caller
    substitutes the median of the tags that could.
    """
    bbox = getattr(wrapper, 'bbox2d', None)
    head = getattr(wrapper, 'head_ref2d', None)
    if bbox is None or head is None:
        return None
    u_lo, u_hi, v_lo, v_hi = bbox
    corner_u = u_lo if engine.exit_sign(eff_mode) > 0 else u_hi
    return ((head[0] - corner_u, head[1] - v_lo),
            (v_hi - v_lo) / 2.0,
            (u_hi - u_lo))


def _median(values):
    ordered_values = sorted(values)
    return ordered_values[len(ordered_values) // 2]


def ordered_plan(anchor2d, base_items, targets, eff_mode, bundle, config,
                 vertical, landing, horizontal):
    """One plan_ordered call with per-tag corner offsets for eff_mode.

    A tag whose text box could not be measured borrows the MEDIAN offsets
    of its neighbours (same tag family in practice), so one bad bounding
    box cannot knock a single tag out of the column.
    """
    measured = [ordered_offsets(targets[base['key']], eff_mode)
                for base in base_items]
    good = [m for m in measured if m is not None]
    if good:
        fallback = ((_median([m[0][0] for m in good]),
                     _median([m[0][1] for m in good])),
                    _median([m[1] for m in good]),
                    _median([m[2] for m in good]))
    else:
        fallback = ((0.0, 0.0), 0.0, 0.0)

    items = []
    for base, offsets in zip(base_items, measured):
        head_offset, line_offset, width = offsets or fallback
        item = dict(base)
        item['head_offset'] = head_offset
        item['line_offset'] = line_offset
        item['exit_edge'] = width
        items.append(item)
    return engine.plan_ordered(
        anchor2d, items, eff_mode,
        engine.normalize_angle(config['angle_deg']),
        vertical, landing, horizontal, bundle,
        intermittent=config['intermittent'],
        clearance=_length_ft(config, 'clearance_mm', FITTING_CLEARANCE_MM))


def _broken_count(plan):
    return sum(1 for entry in plan if not entry['angle_ok'])


def nudge_clear(anchor2d, base_items, targets, mode, bundle, config,
                vertical, landing, horizontal, plan):
    """Slide the stack away from the pipes until the text clears them.

    A pick within half a text width of a pipe cannot be honoured as
    clicked - the text itself would sit on the pipe. The tool used to
    refuse such picks, which left tags piled on each other with no way
    forward and no clue how much further to click (user, 2026-07-28: 43
    refusals in one session). The click is now a HINT: the stack moves
    the shortest distance that makes it legal, and the move is reported.

    Returns:
        (plan, anchor2d, moved_ft) - moved_ft is 0.0 when nothing moved.
    """
    needs = [float(entry.get('shortfall', 0.0)) for entry in plan]
    need = max(needs) if needs else 0.0
    if need <= 0.0:
        return plan, anchor2d, 0.0

    moved = need + _length_ft(config, 'clearance_mm', FITTING_CLEARANCE_MM)
    # Leaders exit towards +sign, so the pipes are that way: retreat.
    shifted = (anchor2d[0] - engine.exit_sign(mode) * moved, anchor2d[1])
    retry = ordered_plan(shifted, base_items, targets, mode, bundle,
                         config, vertical, landing, horizontal)
    if not retry or _broken_count(retry) >= _broken_count(plan):
        return plan, anchor2d, 0.0   # moving did not help: leave the pick
    return retry, shifted, moved


_MODE_NAMES = {
    engine.UPPER_LEFT: 'Upper-Left',
    engine.UPPER_RIGHT: 'Upper-Right',
    engine.LOWER_LEFT: 'Lower-Left',
    engine.LOWER_RIGHT: 'Lower-Right',
}


def detect_mode(anchor2d, items):
    """Best-fit quadrant for a pick: where do the leader ends actually lie?

    Majority vote of the ends relative to the picked point: ends mostly
    below the pick mean the stack is Upper-*, ends mostly to the right
    mean *-Left (leaders exit rightward), and so on.
    """
    half = len(items) / 2.0
    below = sum(1 for item in items if item['end'][1] < anchor2d[1])
    right = sum(1 for item in items if item['end'][0] > anchor2d[0])
    return ('U' if below >= half else 'L') + ('L' if right >= half else 'R')


def pick_point(snaps_off, prompt=PICK_PROMPT):
    """One PickPoint; returns XYZ or None when the user pressed Esc."""
    try:
        if snaps_off:
            return uidoc.Selection.PickPoint(_SNAP_NONE, prompt)
        return uidoc.Selection.PickPoint(prompt)
    except OperationCanceledException:
        return None
    except Exception as ex:
        # A pick can also die when the view cannot host one (no work plane,
        # 3D view...) - stop picking, but leave a trail in the CKR log.
        file_log.warning('Pick loop ended abnormally: %s', ex)
        return None


def split_clusters(targets, config, basis):
    """Group target indices by arrow proximity (Auto-Split Clusters).

    Chaining runs on where each leader arrow currently lands, using the
    Cluster Distance setting; 0 (or a single tag) keeps everything as one
    group, which is exactly the pre-clustering behavior.

    A cluster is a PHYSICAL PIPE BUNDLE (user rule 2026-07-26: "i want
    only that parallel pipes as one cluster"): tags chain only when
    their pipes run parallel, sit side-by-side within the rack width,
    and are tagged at the same station along the run. Perpendicular
    pipes can never share a stack; tags without a pipe curve fall back
    to plain proximity chaining among themselves.
    """
    cluster_mm = float(config.get('cluster_mm', 0.0))
    count = len(targets)
    if cluster_mm <= 0.0 or count < 2:
        return [list(range(count))]

    piped, plain = [], []
    curves2d = {}
    for i, wrapper in enumerate(targets):
        pair = wrapper.tagged_curve()
        if pair is None:
            plain.append(i)
        else:
            piped.append(i)
            curves2d[i] = (common.to_2d(pair[0], basis),
                           common.to_2d(pair[1], basis))

    groups = []
    if piped:
        pipes = [curves2d[i] for i in piped]
        arrows = [getattr(targets[i], 'primary_end2d', (0.0, 0.0))
                  for i in piped]
        for members in clusters.bundle_clusters(
                pipes, arrows,
                _length_ft(config, 'rack_mm', BUNDLE_LATERAL_MM),
                common.mm_to_feet(cluster_mm)):
            groups.append([piped[m] for m in members])
    if plain:
        points = [getattr(targets[i], 'primary_end2d', (0.0, 0.0))
                  for i in plain]
        for members in clusters.chain_clusters(
                points, common.mm_to_feet(cluster_mm),
                max_span=common.mm_to_feet(CLUSTER_SPAN_CAP_MM)):
            groups.append([plain[m] for m in members])

    def reading_order(members):
        pts = [getattr(targets[i], 'primary_end2d', (0.0, 0.0))
               for i in members]
        centre_u = sum(p[0] for p in pts) / len(pts)
        centre_v = sum(p[1] for p in pts) / len(pts)
        return (centre_u, -centre_v)

    groups.sort(key=reading_order)
    return groups


_marker_handles = []


def highlight(sub_targets):
    """Mark the ACTIVE cluster's tags with in-canvas markers.

    Plain selection is useless here: starting a pick clears the selection
    highlight instantly, so the user never saw which group was active.
    TemporaryGraphicsManager (Revit 2022+) draws screen markers that
    survive the pick prompt, need no transaction and leave no undo step.
    Falls back to selection on any failure (better than nothing between
    prompts).
    """
    clear_highlight()
    try:
        from Autodesk.Revit.DB import (InCanvasControlData,
                                       TemporaryGraphicsManager)
        manager = TemporaryGraphicsManager.GetTemporaryGraphicsManager(doc)
        image = os.path.join(_BUNDLE_DIR, 'cluster_marker.png')
        view_id = doc.ActiveView.Id
        for wrapper in sub_targets:
            data = InCanvasControlData(image, wrapper.get_head())
            _marker_handles.append(
                (manager, manager.AddControl(data, view_id)))
        return
    except Exception as ex:
        common.logger.debug('In-canvas markers unavailable: {}'.format(ex))
    try:
        ids = List[ElementId]([w.element.Id for w in sub_targets])
        uidoc.Selection.SetElementIds(ids)
    except Exception as ex:
        common.logger.debug('Cluster highlight failed: {}'.format(ex))


def clear_highlight():
    """Remove all active-cluster markers. Never raises."""
    for manager, handle in _marker_handles:
        try:
            manager.RemoveControl(handle)
        except Exception:
            pass
    del _marker_handles[:]


def run_pick_loop(targets, config):
    """Feature 1 + Auto-Split: one pick loop per arrow-proximity cluster.

    Every pick stays one assimilated undo step. Esc ends the current
    cluster's loop and moves to the next; Esc before any pick leaves that
    cluster untouched; Esc on the last cluster finishes the command.
    """
    basis = common.view_basis(doc.ActiveView)
    measure_layout(targets, basis)   # per-tag text sizes (rolled back)
    build_items(targets, basis)      # per-tag arrow points (for clustering)
    justification = resolve_justification(config)
    gap = common.mm_to_feet(config['vertical_mm'])

    groups = split_clusters(targets, config, basis)

    if len(groups) > 1:
        output.print_md(':information_source: Selection split into {0} '
                        'clusters by arrow proximity - one pick each, Esc '
                        'moves on.'.format(len(groups)))

    total_picks = 0
    flagged_last = 0
    records = []
    try:
        for number, indices in enumerate(groups, 1):
            sub_targets = [targets[i] for i in indices]
            if len(groups) > 1:
                highlight(sub_targets)
                prompt = ('Cluster {0} of {1} - pick lowest tag position. '
                          'Esc = next cluster.'.format(number, len(groups)))
            else:
                prompt = PICK_PROMPT
            picks, flagged_last, record = align_set(
                sub_targets, config, basis, gap, justification, prompt)
            total_picks += picks
            records.append(record)
    finally:
        clear_highlight()   # markers must never outlive the command

    if total_picks and len(records) > 1:
        try:
            final_arrangement(records, config, basis)
        except Exception:
            file_log.error('Final arrangement failed:\n%s',
                           traceback.format_exc())
            output.print_md(':warning: Final arrangement skipped - the '
                            'error was logged; your picks are untouched.')
    return total_picks, flagged_last


def _cluster_plan(record, config, anchor2d):
    """Plan one cluster at an arbitrary anchor (same logic as the loop)."""
    effective = engine.resolve_mode(config['mode'], config['switch_side'])
    if record['ordered'] is not None:
        bundle, base_items = record['ordered']
        plan = ordered_plan(anchor2d, base_items, record['targets'],
                            effective, bundle, config, record['vertical'],
                            record['landing'], record['horizontal'])
        if plan and all(not entry['angle_ok'] for entry in plan):
            mirrored = engine.resolve_mode(effective, True)
            retry = ordered_plan(anchor2d, base_items, record['targets'],
                                 mirrored, bundle, config,
                                 record['vertical'], record['landing'],
                                 record['horizontal'])
            if any(entry['angle_ok'] for entry in retry):
                plan = retry
        return plan
    return engine.plan_alignment(
        anchor2d, record['items'], config['mode'], config['angle_deg'],
        record['vertical'], record['landing'], record['horizontal'],
        constant_landing=config['constant_landing'],
        intermittent=config['intermittent'],
        switch_side=config['switch_side'])


def _cluster_geometry(record, plan):
    """Text-block rect + leader segments of a planned cluster."""
    lo_u, lo_v = [], []
    hi_u, hi_v = [], []
    segments = []
    for entry in plan:
        wrapper = record['targets'][entry['key']]
        head = entry['head']
        bbox = getattr(wrapper, 'bbox2d', None)
        ref = getattr(wrapper, 'head_ref2d', None)
        if bbox is not None and ref is not None:
            lo_u.append(head[0] + (bbox[0] - ref[0]))
            hi_u.append(head[0] + (bbox[1] - ref[0]))
            lo_v.append(head[1] + (bbox[2] - ref[1]))
            hi_v.append(head[1] + (bbox[3] - ref[1]))
        else:
            lo_u.append(head[0])
            hi_u.append(head[0])
            lo_v.append(head[1])
            hi_v.append(head[1])
        segments.extend(engine.leader_segments(entry))
    rect = (min(lo_u), min(lo_v), max(hi_u), max(hi_v))
    return {'rect': rect, 'segments': segments}


_MAX_OBSTACLES = 300


def _view_obstacles(basis, exclude_ids):
    """Every OTHER annotation in the view, as pinned solver states.

    Without these, the final arrangement was blind to stacks placed in
    previous runs and today's clusters could land on yesterday's (the
    collisions inside the user's margin bands). Leader-inclusive boxes
    would be metres wide, so each obstacle's text box is estimated
    symmetrically around its head - no document modification needed.
    """
    states = []
    try:
        from Autodesk.Revit.DB import FilteredElementCollector
        collector = FilteredElementCollector(doc, doc.ActiveView.Id) \
            .WhereElementIsNotElementType()
        for element in collector:
            try:
                if common.element_id_value(element.Id) in exclude_ids:
                    continue
                wrapper = wrappers.wrap(element, doc)
                if wrapper is None:
                    continue
                u_span = common.extent_along(element, doc.ActiveView,
                                             basis[0])
                v_span = common.extent_along(element, doc.ActiveView,
                                             basis[1])
                if not u_span or not v_span:
                    continue
                head = common.to_2d(wrapper.get_head(), basis)
                half_w = max(min(head[0] - u_span[0],
                                 u_span[1] - head[0]), 0.05)
                half_h = max(min(head[1] - v_span[0],
                                 v_span[1] - head[1]), 0.05)
                segments = []
                for key in wrapper.leader_keys():
                    end = wrapper.get_end(key)
                    if end is None:
                        continue
                    end2d = common.to_2d(end, basis)
                    elbow = wrapper.get_elbow(key)
                    if elbow is not None:
                        elbow2d = common.to_2d(elbow, basis)
                        segments.append((head, elbow2d))
                        segments.append((elbow2d, end2d))
                    else:
                        segments.append((head, end2d))
                states.append({
                    'anchor': head,
                    'rect': (head[0] - half_w, head[1] - half_h,
                             head[0] + half_w, head[1] + half_h),
                    'segments': segments,
                    'movable': False,
                })
                if len(states) >= _MAX_OBSTACLES:
                    file_log.info('Obstacle scan capped at %s.',
                                  _MAX_OBSTACLES)
                    break
            except Exception:
                continue
    except Exception as ex:
        common.logger.debug('Obstacle scan failed: {}'.format(ex))
    return states


def final_arrangement(records, config, basis):
    """Automatic cross-cluster cleanup (user choice: fully automated).

    Detects stack overlaps, leaders through other stacks, and cross-
    cluster leader crossings, then moves the LATER-placed cluster of each
    conflicting pair until everything clears the configured Elbow-
    Arrowhead clearance as a margin. All moves
    commit as ONE assimilated undo step; anything unresolved is reported,
    never hidden.
    """
    placed = [r for r in records if r['anchor'] is not None]
    if len(placed) < 2:
        return
    margin = _length_ft(config, 'clearance_mm', FITTING_CLEARANCE_MM)

    plans = {}
    states = []
    for index, record in enumerate(placed):
        plan = _cluster_plan(record, config, record['anchor'])
        plans[index] = plan
        state = _cluster_geometry(record, plan)
        state['anchor'] = record['anchor']
        state['movable'] = True
        states.append(state)

    def replan(index, anchor2d):
        plan = _cluster_plan(placed[index], config, anchor2d)
        plans[index] = plan
        return _cluster_geometry(placed[index], plan)

    # Annotations from PREVIOUS runs join as immovable obstacles, so a
    # new stack can never be left sitting on an old one.
    exclude = set()
    for record in records:
        for wrapper in record['targets']:
            exclude.add(wrapper.id_value)
    states.extend(_view_obstacles(basis, exclude))

    states, moved, remaining = arrange.resolve(states, replan, margin)
    if not moved:
        if remaining:
            output.print_md(
                ':information_source: Final arrangement: {0} conflict(s) '
                'could not be auto-resolved.'.format(remaining))
        return

    group = TransactionGroup(doc, TITLE + ' - final arrangement')
    group.Start()
    try:
        for index in moved:
            record = placed[index]
            apply_plan(record['targets'], plans[index], basis, None,
                       config, move_ends=(record['ordered'] is not None))
        group.Assimilate()
    except Exception:
        group.RollBack()
        raise
    note = '' if not remaining else \
        '; {0} conflict(s) remain - adjust manually'.format(remaining)
    output.print_md(
        ':sparkles: Final arrangement: moved {0} cluster(s) to resolve '
        'overlaps{1}. One Ctrl+Z reverts the whole cleanup.'.format(
            len(moved), note))
    file_log.info('Final arrangement: moved %s cluster(s); %s remaining.',
                  len(moved), remaining)


def align_set(targets, config, basis, gap, justification, prompt):
    """The pick loop for ONE cluster of tags.

    Returns (picks, flagged_last, record) - the record carries everything
    the final-arrangement solver needs to replan this cluster at a new
    anchor: its targets, planning inputs, and the last picked anchor
    (None when the cluster was skipped).
    """
    items = build_items(targets, basis)

    # Vertical Spacing is the clear gap between tags; this cluster's
    # tallest text makes up the rest of the row pitch.
    row_height = max([getattr(w, 'text_height', 0.0) for w in targets]
                     or [0.0])
    vertical = row_height + gap
    landing = common.mm_to_feet(config['landing_mm'])
    horizontal = common.mm_to_feet(config['horizontal_mm'])
    file_log.info('Row pitch: %.0fmm text + %.0fmm gap.',
                  common.feet_to_mm(row_height),
                  common.feet_to_mm(gap))

    # Order-by-pipe: applicable when every tag points at a pipe/duct run.
    ordered = None
    if config.get('order_by_pipe', True):
        ordered = build_ordered_bundle(
            targets, basis,
            engine.normalize_angle(config['angle_deg']) == 0.0)
        if ordered is None:
            output.print_md(
                ':information_source: Order-by-pipe needs every selected '
                'tag to point at a pipe/duct run - using standard '
                'alignment for this set.')

    straight_wanted = (ordered is not None and
                       engine.normalize_angle(config['angle_deg']) == 0.0)

    record = {'targets': targets, 'ordered': ordered, 'items': items,
              'vertical': vertical, 'landing': landing,
              'horizontal': horizontal, 'anchor': None}

    picks = 0
    flagged_last = 0
    while True:
        point = pick_point(config['snaps_off'], prompt)
        if point is None:
            break

        anchor2d = common.to_2d(point, basis)
        effective = engine.resolve_mode(config['mode'],
                                        config['switch_side'])

        if ordered is not None:
            bundle, base_items = ordered
            plan = ordered_plan(anchor2d, base_items, targets, effective,
                                bundle, config, vertical, landing,
                                horizontal)
            # Every pipe behind the stack: the pick is on the other side
            # of the run - mirror the exit for THIS pick.
            if plan and all(not entry['angle_ok'] for entry in plan):
                mirrored = engine.resolve_mode(effective, True)
                retry = ordered_plan(anchor2d, base_items, targets,
                                     mirrored, bundle, config, vertical,
                                     landing, horizontal)
                if any(entry['angle_ok'] for entry in retry):
                    plan = retry
                    output.print_md(
                        ':bulb: Quadrant switched to **{0}** for this '
                        'pick - the configured {1} put every pipe behind '
                        'the stack.'.format(_MODE_NAMES[mirrored],
                                            _MODE_NAMES[effective]))
                    effective = mirrored

            # The click is a hint: if the text would sit on a pipe, back
            # the stack off instead of refusing the pick.
            plan, anchor2d, moved = nudge_clear(
                anchor2d, base_items, targets, effective, bundle, config,
                vertical, landing, horizontal, plan)
            if moved > 0.0:
                output.print_md(
                    ':left_right_arrow: Stack moved **{0:.0f} mm** back '
                    'from the pipes - the text would not fit where you '
                    'clicked.'.format(common.feet_to_mm(moved)))
                file_log.info('Anchor nudged %.0fmm clear of the pipes.',
                              common.feet_to_mm(moved))
        else:
            plan = engine.plan_alignment(
                anchor2d, items,
                config['mode'], config['angle_deg'],
                vertical, landing, horizontal,
                constant_landing=config['constant_landing'],
                intermittent=config['intermittent'],
                switch_side=config['switch_side'])

            # Every leader flagged means the configured quadrant
            # contradicts where the user actually picked. Fall back to
            # the quadrant the ends actually imply for THIS pick.
            if plan and all(not entry['angle_ok'] for entry in plan):
                detected = detect_mode(anchor2d, items)
                if detected != effective:
                    plan = engine.plan_alignment(
                        anchor2d, items,
                        detected, config['angle_deg'],
                        vertical, landing, horizontal,
                        constant_landing=config['constant_landing'],
                        intermittent=config['intermittent'])
                    output.print_md(
                        ':bulb: Quadrant switched to **{0}** for this '
                        'pick - the configured {1} put every leader '
                        'behind the stack. Pick the {0} button in the '
                        'dialog to make it permanent.'.format(
                            _MODE_NAMES[detected],
                            _MODE_NAMES[effective]))

        # A pick is never refused for geometry any more (user decision,
        # 2026-07-29): nudge_clear has already bought whatever room it
        # could, and a tag placed imperfectly still beats a tag left
        # buried under four others. Anything still flagged is reported.
        if ordered is not None and plan:
            broken = _broken_count(plan)
            if broken:
                file_log.info('Placed with %s/%s leader(s) still flagged.',
                              broken, len(plan))

        group = TransactionGroup(doc, TITLE)
        group.Start()
        try:
            _, elbow_failures, flagged_last = apply_plan(
                targets, plan, basis, justification, config,
                move_ends=(ordered is not None))
            group.Assimilate()
            picks += 1
            record['anchor'] = anchor2d
            if elbow_failures:
                output.print_md(
                    ':warning: {0} leader elbow(s) could not be set on '
                    'this pick.'.format(elbow_failures))
            if straight_wanted:
                bent = sum(1 for entry in plan
                           if not entry.get('straight'))
                if bent:
                    output.print_md(
                        ':information_source: {0} leader(s) could not '
                        'stay straight (their pipe does not reach the '
                        'tag height) and were given a small slant '
                        'instead.'.format(bent))
        except Exception:
            group.RollBack()
            raise

        # Justification only needs applying once; later picks keep it.
        justification = None

    return picks, flagged_last, record


def export_snapshot():
    """Export the current view region to %APPDATA%/CKR/logs.

    A shared pair of eyes: after every run the actual drawn result sits
    next to the log, so placement can be reviewed (by the user, or by
    Claude reading the file directly) without screenshots.
    """
    try:
        from Autodesk.Revit.DB import (ExportRange, ImageExportOptions,
                                       ImageFileType, ZoomFitType)
        options = ImageExportOptions()
        options.ExportRange = ExportRange.VisibleRegionOfCurrentView
        options.FilePath = os.path.join(common.LOG_DIR, 'align_check')
        options.HLRandWFViewsFileType = ImageFileType.PNG
        options.ShadowViewsFileType = ImageFileType.PNG
        options.ZoomType = ZoomFitType.FitToPage
        options.PixelSize = 2000
        doc.ExportImage(options)
        file_log.info('Snapshot exported to %s (align_check*.png).',
                      common.LOG_DIR)
    except Exception as ex:
        common.logger.debug('Snapshot export failed: {}'.format(ex))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if doc is None or uidoc is None:
        forms.alert('Open a project document first.', title=TITLE)
        return

    wrapped = gather_targets()
    if not wrapped:
        forms.alert('No tags or text notes selected.', title=TITLE)
        return

    loaded = settings.load()
    if __shiftclick__ or not os.path.exists(settings.SETTINGS_PATH):
        # Shift+click opens the dialog; a plain click reuses the last
        # settings (they rarely change between runs). The first ever run
        # always shows the dialog, so nothing is configured invisibly.
        config = ui.show_config(doc, loaded)
        if config is None:
            return  # cancelled; selection left untouched
        settings.save(config)
    else:
        config = loaded
        output.print_md(
            ':gear: Using saved settings (angle {0:g}, {1} mode). '
            'Shift+click the button to open the dialog.'.format(
                config['angle_deg'],
                'order-by-pipe' if config.get('order_by_pipe', True)
                else 'standard'))
    _config = config

    targets, skipped = partition_targets(wrapped)
    report_skips(skipped)
    attached_in = [w for w in targets if w.attached_end]
    if attached_in:
        if config['attached_end']:
            output.print_md(
                ':warning: {0} attached leader end(s) will be freed and '
                'pinned on the element so the slant angle is exact; the '
                'arrowhead may shift slightly.'.format(len(attached_in)))
        else:
            output.print_md(
                ':warning: {0} tag(s) have attached leader ends; Revit '
                'controls their arrowheads, so the slant angle is '
                'approximate for them. Turn on "Attached End Tags" to pin '
                'the arrowheads for exact angles.'.format(len(attached_in)))
    if not targets:
        forms.alert('Nothing can be aligned:\n\n' + skip_summary(skipped),
                    title=TITLE)
        return

    picks, flagged = run_pick_loop(targets, config)

    if picks and flagged:
        output.print_md(
            ':warning: {0} leader(s) could not honour the exact angle on '
            'the final pick (end point behind the stack); their landing '
            'was collapsed instead.'.format(flagged))

    if config['keep_selection']:
        ids = List[ElementId]([w.element.Id for w in targets])
        uidoc.Selection.SetElementIds(ids)
    else:
        uidoc.Selection.SetElementIds(List[ElementId]())

    file_log.info('Align Tags: %s pick(s), %s tag(s), mode %s.',
                  picks, len(targets), config['mode'])
    if picks:
        export_snapshot()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        details = traceback.format_exc()
        file_log.error('Align Tags failed:\n%s', details)
        common.logger.debug(details)
        forms.alert(
            'Align Tags hit an unexpected error and stopped. Nothing is '
            'left half-done - the current pick was rolled back.\n\n'
            'Details were logged to {0}.'.format(common.LOG_DIR),
            title=TITLE)
