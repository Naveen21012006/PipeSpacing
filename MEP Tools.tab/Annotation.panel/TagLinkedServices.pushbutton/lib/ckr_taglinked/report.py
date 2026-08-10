# -*- coding: utf-8 -*-
"""The results report (FR-09).

Three renderings of one RunResult:

    summary_lines()   the completion dialog - what happened, in six lines
    print_report()    the pyRevit output window - the full breakdown, with
                      every rejection reason and every failed element id
    csv_text()        the exportable record

The one rule that runs through all three: a run that rejected elements
reports WHY it rejected them. FR-04.4 - runs failing the length test are
counted and reported, never silently discarded - is the difference between
a tool the engineer trusts and one they check by hand anyway.
"""

from ckr_taglinked import runner

#: The FR-09 buckets, in report order.
BUCKETS = (
    ('placed', 'Tags placed'),
    ('skipped_tagged', 'Skipped - already tagged'),
    ('length', 'Rejected - below minimum visible length'),
    ('range', 'Rejected - outside view range or crop'),
    ('filter', 'Rejected - filter mismatch'),
    ('inclined', 'Rejected - inclined run'),
    ('geometry', 'Rejected - no usable geometry'),
    ('invisible', 'Rejected - not visible in this view'),
    ('errors', 'Errors'),
)


def _bucket(entry):
    """Return the FR-09 tallies for one category, as a dict."""
    counts = {
        'placed': entry.placed,
        'skipped_tagged': entry.skipped_tagged,
        'length': 0,
        'range': 0,
        'filter': 0,
        'inclined': 0,
        'geometry': 0,
        'invisible': 0,
        'errors': len(entry.errors),
    }
    for reason, count in entry.rejected.items():
        if reason.startswith(runner.REJECT_FILTER):
            counts['filter'] += count
        elif reason == runner.REJECT_LENGTH:
            counts['length'] += count
        elif reason == runner.REJECT_RANGE:
            counts['range'] += count
        elif reason == runner.REJECT_INCLINED:
            counts['inclined'] += count
        elif reason == runner.REJECT_GEOMETRY:
            counts['geometry'] += count
        elif reason == runner.REJECT_NOT_VISIBLE:
            counts['invisible'] += count
        else:
            counts['filter'] += count
    return counts


def summary_lines(results):
    """Return the completion dialog text as a list of lines."""
    heading = 'Preview - nothing was created' if results.preview \
        else 'Tag Linked Services completed'
    lines = [heading, '']
    if results.cancelled:
        lines.append('Cancelled by the user; the tags already placed were '
                     'kept.')
        lines.append('')

    lines.append('View:      {0}'.format(results.view_name))
    lines.append('Assessed:  {0} element(s) in {1:.1f}s'.format(
        results.scanned, results.seconds))
    lines.append('')

    verb = 'would be placed' if results.preview else 'placed'
    lines.append('Tags {0}:  {1}'.format(verb, results.placed))
    if results.skipped_tagged:
        lines.append('Already tagged:   {0}'.format(results.skipped_tagged))
    if results.rejected:
        lines.append('Rejected:         {0}'.format(results.rejected))
    if results.errors:
        lines.append('Errors:           {0}'.format(results.errors))

    lines.append('')
    for entry in results.categories:
        counts = _bucket(entry)
        lines.append('{0}: {1} of {2} candidate(s)'.format(
            entry.label, counts['placed'], entry.candidates))

    if results.warnings:
        lines.append('')
        for warning in results.warnings:
            lines.append('! {0}'.format(warning))

    lines.append('')
    lines.append('The output window has the full breakdown.')
    return lines


def print_report(results, output):
    """Print the full FR-09 breakdown to the pyRevit output window."""
    title = 'Tag Linked Services - Preview' if results.preview \
        else 'Tag Linked Services'
    output.print_md('# {0}'.format(title))
    output.print_md('View **{0}** - {1} element(s) assessed in '
                    '{2:.1f}s.'.format(results.view_name, results.scanned,
                                       results.seconds))
    if results.preview:
        output.print_md('_Preview only: every tag created for the visibility '
                        'test was rolled back._')
    if results.cancelled:
        output.print_md(':warning: **Cancelled** - the tags already placed '
                        'were committed, the rest were not attempted.')

    for warning in results.warnings:
        output.print_md(':warning: {0}'.format(warning))

    header = '| Category | ' + ' | '.join(label for _key, label in BUCKETS) \
             + ' |\n'
    header += '| :-- |' + ' --: |' * len(BUCKETS) + '\n'
    totals = dict((key, 0) for key, _label in BUCKETS)
    body = ''
    for entry in results.categories:
        counts = _bucket(entry)
        row = [entry.label]
        for key, _label in BUCKETS:
            totals[key] += counts[key]
            row.append(str(counts[key]))
        body += '| ' + ' | '.join(row) + ' |\n'
    body += '| **Total** | ' + ' | '.join(
        '**{0}**'.format(totals[key]) for key, _label in BUCKETS) + ' |'
    output.print_md(header + body)

    for entry in results.categories:
        if entry.candidates:
            output.print_md(
                '**{0}** candidates by run type: {1} horizontal, {2} '
                'vertical, {3} inclined.'.format(
                    entry.label,
                    entry.by_class.get('horizontal', 0),
                    entry.by_class.get('vertical', 0),
                    entry.by_class.get('inclined', 0)))
        if entry.non_linear:
            output.print_md(
                '_{0}: {1} element(s) had non-linear geometry and were '
                'classified from the chord._'.format(entry.label,
                                                     entry.non_linear))
        if entry.crowded:
            output.print_md(
                '_{0}: {1} tag(s) could not achieve the minimum clear '
                'spacing and were placed with a leader._'.format(
                    entry.label, entry.crowded))

    detail = [(entry, reason, count)
              for entry in results.categories
              for reason, count in sorted(entry.rejected.items())]
    if detail:
        output.print_md('## Rejections in detail')
        for entry, reason, count in detail:
            output.print_md('- **{0}** - {1}: {2}'.format(
                entry.label, reason, count))

    errors = [(entry, element_id, message)
              for entry in results.categories
              for element_id, message in entry.errors]
    if errors:
        output.print_md('## Errors')
        for entry, element_id, message in errors:
            output.print_md('- **{0}** element `{1}`: {2}'.format(
                entry.label, element_id, message))


def _cell(value):
    """Quote one CSV field."""
    text = '' if value is None else '{0}'.format(value)
    if any(character in text for character in ',"\n'):
        return '"{0}"'.format(text.replace('"', '""'))
    return text


def csv_text(results):
    """Return the whole report as CSV text (FR-09).

    Written by hand rather than through the csv module, which needs
    different file modes on IronPython and CPython - one less thing to
    diverge between Revit versions.
    """
    rows = [['Tag Linked Services', 'preview' if results.preview else 'run'],
            ['View', results.view_name],
            ['Elements assessed', results.scanned],
            ['Seconds', '{0:.1f}'.format(results.seconds)],
            ['Cancelled', 'yes' if results.cancelled else 'no'],
            []]

    rows.append(['Category'] + [label for _key, label in BUCKETS] +
                ['Candidates'])
    for entry in results.categories:
        counts = _bucket(entry)
        rows.append([entry.label] +
                    [counts[key] for key, _label in BUCKETS] +
                    [entry.candidates])

    detail = [(entry.label, reason, count)
              for entry in results.categories
              for reason, count in sorted(entry.rejected.items())]
    if detail:
        rows.append([])
        rows.append(['Category', 'Rejection reason', 'Count'])
        rows.extend([list(item) for item in detail])

    errors = [(entry.label, element_id, message)
              for entry in results.categories
              for element_id, message in entry.errors]
    if errors:
        rows.append([])
        rows.append(['Category', 'Element id', 'Error'])
        rows.extend([list(item) for item in errors])

    if results.warnings:
        rows.append([])
        rows.append(['Warnings'])
        rows.extend([[warning] for warning in results.warnings])

    return '\n'.join(','.join(_cell(cell) for cell in row) for row in rows)


def export_csv(results, path):
    """Write the CSV report to a path; True on success."""
    try:
        with open(path, 'w') as handle:
            handle.write(csv_text(results))
        return True
    except Exception:
        return False
