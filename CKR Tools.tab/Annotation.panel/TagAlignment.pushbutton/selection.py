# -*- coding: utf-8 -*-
"""Selection of MEP elements for the Tag Alignment tool.

Responsible only for *getting* elements from the user - it does not judge
whether they are supported (see validation.py).
"""

from pyrevit import revit

from Autodesk.Revit.DB import CurveElement, Line
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType

import utils


class _LineFilter(ISelectionFilter):
    """Restricts picking to STRAIGHT detail/model lines.

    Arcs and splines are also CurveElements, so they must be rejected here -
    otherwise Revit lets the user pick one and the tool aborts silently
    (GeometryCurve is not a Line), looking just like a cancel.
    """

    # pylint: disable=invalid-name
    def AllowElement(self, element):
        if not isinstance(element, CurveElement):
            return False
        try:
            return isinstance(element.GeometryCurve, Line)
        except Exception:
            return False

    def AllowReference(self, reference, point):
        return False


def pick_reference_line(uidoc, doc):
    """Ask the user for the line the tag heads should line up on.

    Args:
        uidoc: The active UIDocument.
        doc: The active Document.

    Returns:
        Line | None: The picked line's geometry, or None if the pick was
        cancelled or the element is not a straight line.
    """
    try:
        picked = uidoc.Selection.PickObject(
            ObjectType.Element,
            _LineFilter(),
            'Pick the reference line to align the tags to')
    except Exception as ex:
        utils.logger.debug('Reference line pick cancelled: {}'.format(ex))
        return None

    element = doc.GetElement(picked.ElementId)
    if element is None:
        return None

    curve = element.GeometryCurve
    if not isinstance(curve, Line):
        utils.logger.debug('Reference element is not a straight line.')
        return None

    return curve


def get_preselected_elements(uidoc, doc):
    """Return the elements already selected when the tool started.

    MUST be read before any interactive pick: starting a PickObject (e.g. the
    reference-line pick) clears the active selection, so the caller captures
    this at the very top of the workflow.

    Args:
        uidoc: The active UIDocument.
        doc: The active Document.

    Returns:
        list: The pre-selected elements (empty when nothing was selected).
    """
    elements = []
    for element_id in uidoc.Selection.GetElementIds():
        element = doc.GetElement(element_id)
        if element is not None:
            elements.append(element)

    utils.logger.debug('{} element(s) pre-selected.'.format(len(elements)))
    return elements


def prompt_for_elements(message='Select MEP elements to tag and align'):
    """Prompt an interactive pick of MEP elements to tag.

    Args:
        message: The status-bar prompt (used to label the direction picks).

    Returns:
        list: The picked elements (empty if the user picked none / cancelled).
    """
    try:
        picked = revit.pick_elements(message=message)
    except Exception as ex:
        utils.logger.debug('Pick cancelled or failed: {}'.format(ex))
        picked = None

    return list(picked) if picked else []
