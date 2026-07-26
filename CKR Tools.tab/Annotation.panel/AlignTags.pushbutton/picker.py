# -*- coding: utf-8 -*-
"""Selection for the Align Tags tool.

Preselection is honoured when it contains supported annotations; otherwise
the user is prompted with a filter limited to those categories, matching the
spec's workflow. Judging whether a supported element is *usable* (has a
leader, not pinned...) happens in script.py so the user gets a report rather
than a silent refusal.
"""

from Autodesk.Revit.Exceptions import OperationCanceledException
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType

import common
import wrappers


class AlignSelectionFilter(ISelectionFilter):
    """Limits interactive picking to tags and text notes."""

    # pylint: disable=invalid-name
    def AllowElement(self, element):
        return wrappers.is_supported(element)

    def AllowReference(self, reference, point):
        return False


def get_preselected(uidoc, doc):
    """Return supported wrappers from the active selection.

    MUST be read before any interactive pick - starting a pick clears the
    active selection.

    Returns:
        tuple: (wrapped, ignored_count) - ignored counts the selected
        elements that are not tags/text notes.
    """
    wrapped = []
    ignored = 0
    for element_id in uidoc.Selection.GetElementIds():
        element = doc.GetElement(element_id)
        if element is None:
            continue
        wrapper = wrappers.wrap(element, doc)
        if wrapper is None:
            ignored += 1
        else:
            wrapped.append(wrapper)
    common.logger.debug('{} supported / {} ignored preselected.'.format(
        len(wrapped), ignored))
    return wrapped, ignored


def prompt_for_tags(uidoc, doc):
    """Interactively pick tags/text notes; Esc or empty pick returns [].

    Returns:
        list: wrappers for the picked elements.
    """
    try:
        picked = uidoc.Selection.PickObjects(
            ObjectType.Element,
            AlignSelectionFilter(),
            'Select tags / text notes to align, then Finish')
    except OperationCanceledException:
        return []  # user pressed Esc - a normal exit, not an error
    except Exception as ex:
        common.get_file_logger().warning('Tag selection failed: %s', ex)
        return []

    wrapped = []
    for reference in picked:
        element = doc.GetElement(reference.ElementId)
        wrapper = wrappers.wrap(element, doc) if element is not None else None
        if wrapper is not None:
            wrapped.append(wrapper)
    return wrapped
