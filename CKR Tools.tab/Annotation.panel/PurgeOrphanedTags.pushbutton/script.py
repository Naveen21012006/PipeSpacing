# -*- coding: utf-8 -*-
"""Purge Orphaned Tags - the companion utility to Tag Linked Services.

FR-10. Tags on linked elements are stored in the HOST document. When the
link is reissued with elements deleted or regenerated, those tags orphan:
they stay in the view, empty, pointing at nothing. Without this utility the
sheets degrade quietly between link revisions - and that happens on every
issue cycle, not occasionally.

The tool lists every orphaned tag in the active view, asks once, and
deletes them in a single undoable transaction. Nothing else is touched, and
a run that finds nothing says so rather than opening a dialog to confirm
doing nothing.

Author: Naveen
Target: Revit 2022-2026 / pyRevit / IronPython
"""

import traceback

from pyrevit import forms, revit, script

from Autodesk.Revit.DB import (
    ElementId,
    FilteredElementCollector,
    IndependentTag,
    Transaction,
)

from System.Collections.Generic import List

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()
logger = script.get_logger()

TITLE = 'Purge Orphaned Tags'


def id_value(element_id):
    """Return a stable integer id across Revit versions."""
    try:
        return element_id.Value          # Revit 2024+
    except AttributeError:
        return element_id.IntegerValue   # Revit 2022 / 2023


def element_name(element):
    """Return an element's name, tolerant of IronPython property quirks."""
    try:
        return element.Name
    except Exception:
        try:
            from Autodesk.Revit.DB import Element
            return Element.Name.GetValue(element)
        except Exception:
            return ''


def is_orphaned(tag):
    """Return True when a tag's referenced element no longer exists.

    ``IsOrphaned`` is the direct answer on every supported version; the
    fallback covers a tag that resolves no reference at all.
    """
    try:
        return bool(tag.IsOrphaned)
    except Exception:
        pass
    try:
        return not list(tag.GetTaggedReferences())
    except Exception:
        return False


def collect(view):
    """Return the orphaned tags in a view, and the total tag count."""
    try:
        collector = FilteredElementCollector(doc, view.Id) \
            .OfClass(IndependentTag).WhereElementIsNotElementType()
        tags = list(collector)
    except Exception as ex:
        logger.debug('Tag collection failed: {}'.format(ex))
        return [], 0
    return [tag for tag in tags if is_orphaned(tag)], len(tags)


def describe(tags):
    """Print the orphaned tags to the output window before anything is lost."""
    output.print_md('# {0}'.format(TITLE))
    output.print_md('{0} orphaned tag(s) in **{1}**.'.format(
        len(tags), element_name(doc.ActiveView)))
    by_type = {}
    for tag in tags:
        try:
            name = element_name(doc.GetElement(tag.GetTypeId())) or '?'
        except Exception:
            name = '?'
        by_type.setdefault(name, []).append(tag.Id)
    for name in sorted(by_type):
        ids = by_type[name]
        try:
            link = output.linkify(ids, title='Select')
        except Exception:
            link = ''
        output.print_md('- **{0}** - {1} tag(s) {2}'.format(
            name, len(ids), link))


def delete(tags):
    """Delete the tags in one undoable transaction (FR-10.2)."""
    ids = List[ElementId]([tag.Id for tag in tags])
    with Transaction(doc, TITLE) as transaction:
        transaction.Start()
        removed = doc.Delete(ids)
        transaction.Commit()
    try:
        return len(removed)
    except Exception:
        return len(tags)


def main():
    if doc is None or uidoc is None:
        forms.alert('Open a project document first.', title=TITLE)
        return

    view = doc.ActiveView
    if view is None or view.IsTemplate:
        forms.alert('Open the view you want to clean up and run the tool '
                    'again.', title=TITLE)
        return

    tags, total = collect(view)
    if not tags:
        forms.alert(
            'No orphaned tags in "{0}".\n\n{1} tag(s) checked - every one '
            'still points at something.'.format(element_name(view), total),
            title=TITLE)
        return

    describe(tags)

    if not forms.alert(
            'Delete {0} orphaned tag(s) from "{1}"?\n\n'
            'They point at elements that no longer exist - typically '
            'because a link was reissued. The output window lists them; '
            'one Ctrl+Z puts them back.'.format(len(tags),
                                                element_name(view)),
            title=TITLE, yes=True, no=True):
        return

    removed = delete(tags)
    output.print_md('**{0} tag(s) deleted.**'.format(removed))
    forms.alert('{0} orphaned tag(s) deleted from "{1}".\n\n'
                'One Ctrl+Z restores them.'.format(removed,
                                                   element_name(view)),
                title=TITLE)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        details = traceback.format_exc()
        logger.debug(details)
        forms.alert('Purge Orphaned Tags hit an unexpected error and '
                    'stopped. Nothing was deleted.\n\n{0}'.format(details),
                    title=TITLE)
