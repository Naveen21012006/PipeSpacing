# -*- coding: utf-8 -*-
"""Diagnostic log for the Auto Tag method.

A screenshot shows the symptom; this shows the cause. Every run records what
the tool DECIDED (each tag's target, its reach, the row it got) and what it
DREW (head, elbow, arrow of every leader), then CHECKS ITS OWN OUTPUT for
leader crossings and prints the count. That count is the pass mark - not a
reading of a picture.

Written with a direct open/write/close: Python's ``logging`` module is silenced
under this pyRevit host (a FileHandler creates the file but records never land)
- a trap already paid for in the sibling Align Tags tool.

File: %APPDATA%/CKR/logs/autotag.log, rewritten each run so it always describes
the run you just did. Failing to log never fails the run.
"""

import os

import utils

_EPS = 1e-9


def log_path():
    """Return the log file path (%APPDATA%/CKR/logs/autotag.log)."""
    base = os.environ.get('APPDATA') or os.environ.get('LOCALAPPDATA') or ''
    return os.path.join(base, 'CKR', 'logs', 'autotag.log')


def _mm(value_feet):
    """Feet (Revit internal) -> millimetres, for readable coordinates."""
    return value_feet * utils.MM_PER_FOOT


def _pt(point_uv):
    """Format a 2D (u, v) point in millimetres."""
    return '({0:>8.0f},{1:>8.0f})'.format(_mm(point_uv[0]), _mm(point_uv[1]))


# ---------------------------------------------------------------------------
# Geometry: does leader A cross leader B?
# ---------------------------------------------------------------------------
def _orient(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segment_cross(p1, p2, p3, p4):
    """Return the crossing point of two segments, or None.

    Proper crossings only: segments that merely touch at an endpoint (a tag's
    own landing meeting its own drop) are not crossings.
    """
    d1 = _orient(p3, p4, p1)
    d2 = _orient(p3, p4, p2)
    d3 = _orient(p1, p2, p3)
    d4 = _orient(p1, p2, p4)
    if (((d1 > _EPS and d2 < -_EPS) or (d1 < -_EPS and d2 > _EPS))
            and ((d3 > _EPS and d4 < -_EPS) or (d3 < -_EPS and d4 > _EPS))):
        denominator = ((p1[0] - p2[0]) * (p3[1] - p4[1])
                       - (p1[1] - p2[1]) * (p3[0] - p4[0]))
        if abs(denominator) < _EPS:
            return None
        a = p1[0] * p2[1] - p1[1] * p2[0]
        b = p3[0] * p4[1] - p3[1] * p4[0]
        x = (a * (p3[0] - p4[0]) - (p1[0] - p2[0]) * b) / denominator
        y = (a * (p3[1] - p4[1]) - (p1[1] - p2[1]) * b) / denominator
        return (x, y)
    return None


def _leader_segments(leader):
    """[(head,elbow), (elbow,arrow)] for one leader, dropping zero-length bits."""
    head, elbow, arrow = leader
    segments = []
    for start, end in ((head, elbow), (elbow, arrow)):
        if abs(start[0] - end[0]) > _EPS or abs(start[1] - end[1]) > _EPS:
            segments.append((start, end))
    return segments


def find_crossings(leaders):
    """Return [(i, j, point)] for every pair of leaders whose lines cross.

    Args:
        leaders (dict): index -> (head_uv, elbow_uv, arrow_uv).
    """
    crossings = []
    indices = sorted(leaders.keys())
    for position, i in enumerate(indices):
        for j in indices[position + 1:]:
            for segment_a in _leader_segments(leaders[i]):
                for segment_b in _leader_segments(leaders[j]):
                    point = _segment_cross(segment_a[0], segment_a[1],
                                           segment_b[0], segment_b[1])
                    if point is not None:
                        crossings.append((i, j, point))
    return crossings


# ---------------------------------------------------------------------------
# The run report
# ---------------------------------------------------------------------------
def write_run(view, settings, pitch, column_u, line_top, outward,
              targets_below, tags, elements, kinds, pipe_up, pipe_across,
              reach, drop_reach, height_targets, order, leaders,
              audit_trail=None, spans_v=None, spans_u=None, demoted=None,
              seat_trace=None):
    """Write one run's decisions, output and self-check. Never raises.

    Args:
        view: the active view (for its name and scale).
        settings (dict): the shared Align Tags settings actually used.
        pitch (float): row pitch, feet.
        column_u, line_top (float): the reference line's across coord and top.
        outward (float): +1 if the pipes lie right of the column, else -1.
        targets_below (bool): the targets sit below the tag column.
        tags, elements (list): parallel, one per tag.
        kinds (dict): index -> 'riser' | 'level' | 'horiz'.
        pipe_up, pipe_across (list): each target's v / u.
        reach (list): each target's outward distance from the column.
        drop_reach (dict): index -> the reach actually used for the drop.
        height_targets (dict): index -> row v.
        order (list): indices, top row first.
        leaders (dict): index -> (head_uv, elbow_uv, arrow_uv).

    Returns:
        int: the number of crossings found (0 is the pass mark), or -1 if the
        log could not be written.
    """
    try:
        path = log_path()
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)

        lines = []
        add = lines.append

        try:
            view_name = view.Name
        except Exception:
            view_name = '?'
        try:
            scale = view.Scale
        except Exception:
            scale = '?'

        add('=== CKR Auto Tag =====================================')
        add('view   {0}   scale 1:{1}'.format(view_name, scale))
        if settings.get('gap_paper_mm') is not None:
            gap_text = '{0}mm PAPER'.format(settings.get('gap_paper_mm'))
        elif 'vertical_mm' in settings.get('_local_keys', ()):
            gap_text = '{0}mm model'.format(settings.get('vertical_mm'))
        else:
            gap_text = '{0}mm PAPER (default)'.format(
                settings.get('_default_gap_paper_mm', '?'))
        add('rule   pitch {0:.0f}mm   landing {1}mm   gap {2}'.format(
            _mm(pitch), settings.get('landing_mm'), gap_text))
        height_source = settings.get('_text_height_source')
        if height_source:
            add('text   height {0:.0f}mm model ({1})'.format(
                _mm(settings.get('_text_height', 0.0)), height_source))
        add('line   u={0:.0f}mm  top={1:.0f}mm   pipes are {2} of the column'
            .format(_mm(column_u), _mm(line_top),
                    'RIGHT' if outward > 0 else 'LEFT'))
        add('order  targets {0} the column -> {1} reach at the TOP row'.format(
            'BELOW' if targets_below else 'ABOVE',
            'farthest' if targets_below else 'nearest'))
        add('sel    {0} tags = {1} horiz-run, {2} vert-run, {3} riser'.format(
            len(tags),
            sum(1 for k in kinds.values() if k == 'horiz'),
            sum(1 for k in kinds.values() if k == 'level'),
            sum(1 for k in kinds.values() if k == 'riser')))
        add('')
        add('--- rows (mm, view axes; row 0 = top) ----------------')
        add('row  idx  element     kind    pipe_u   pipe_v    reach    drop_r'
            '     row_v   span_v   span_u')
        for row, index in enumerate(order):
            try:
                element_id = utils.element_id_value(elements[index].Id)
            except Exception:
                element_id = '?'
            span_v = (spans_v or {}).get(index)
            span_u = (spans_u or {}).get(index)
            add('{0:>3}  {1:>3}  {2:<10} {3:<6} {4:>8.0f} {5:>8.0f} {6:>8.0f}'
                ' {7:>9.0f} {8:>9.0f} {9:>8} {10:>8}'.format(
                    row, index, element_id, kinds.get(index, '?'),
                    _mm(pipe_across[index]), _mm(pipe_up[index]),
                    _mm(reach[index]), _mm(drop_reach.get(index, reach[index])),
                    _mm(height_targets[index]),
                    '-' if span_v is None else '{0:.0f}'.format(_mm(span_v)),
                    '-' if span_u is None else '{0:.0f}'.format(_mm(span_u))))

        add('')
        add('--- leaders (mm) -------------------------------------')
        add('idx  head               elbow              arrow')
        for index in order:
            leader = leaders.get(index)
            if leader is None:
                add('{0:>3}  (no leader planned - normal leader kept)'.format(
                    index))
                continue
            add('{0:>3}  {1}  {2}  {3}'.format(
                index, _pt(leader[0]), _pt(leader[1]), _pt(leader[2])))

        add('')
        if seat_trace:
            add('--- straight-leader seating (vertical runs) -----------')
            add('idx   usable window (mm)        row        verdict')
            for index, low, high, row in seat_trace:
                if low is None:
                    add('{0:>3}   (no readable extent)                    '
                        'DEMOTED'.format(index))
                elif row is None:
                    add('{0:>3}   {1:>9.0f} .. {2:<9.0f}      -          '
                        'DEMOTED (no free row on its own pipe)'.format(
                            index, _mm(low), _mm(high)))
                else:
                    add('{0:>3}   {1:>9.0f} .. {2:<9.0f}  {3:>9.0f}  '
                        'straight'.format(index, _mm(low), _mm(high),
                                          _mm(row)))
            add('')
        if demoted:
            for size, kept, lost in demoted:
                add('  cluster of {0}: {1} kept straight, {2} demoted'.format(
                    size, kept, lost))
            add('')
        if audit_trail:
            add('--- audit passes -------------------------------------')
            add('  the column may rise above the drawn line to clear a')
            add('  crossing or a stub drop (one too short to show a stem')
            add('  above its arrowhead); the smallest lift that works wins.')
            best = min(entry[1:3] for entry in audit_trail)
            for entry in audit_trail:
                lift, found, stubs = entry[0], entry[1], entry[2]
                add('  lift {0} row(s) -> {1} crossing(s), {2} stub(s){3}'
                    .format(lift, found, stubs,
                            '   <- kept' if (found, stubs) == best else ''))
                if (found, stubs) == best:
                    break
            add('')
        add('--- self-check ---------------------------------------')
        crossings = find_crossings(leaders)
        for i, j, point in crossings:
            add('CROSS  leader {0} x leader {1}  at {2}'.format(
                i, j, _pt(point)))
        if crossings:
            add('FAULT  {0} crossing(s)'.format(len(crossings)))
        else:
            add('OK     0 crossings')
        # Completeness is part of the verdict: a run that places only some of
        # the tags can report a low crossing count while the drawing is a
        # scatter of unmoved tags (26 of 40 on 2026-08-03).
        if len(order) < len(tags):
            add('FAULT  {0} of {1} tags NEVER PLACED'.format(
                len(tags) - len(order), len(tags)))
        else:
            add('OK     all {0} tags placed'.format(len(tags)))
        add('')

        handle = open(path, 'w')
        try:
            handle.write('\n'.join(lines))
        finally:
            handle.close()
        return len(crossings)
    except Exception as ex:
        try:
            utils.logger.debug('Auto Tag log failed: {0}'.format(ex))
        except Exception:
            pass
        return -1
