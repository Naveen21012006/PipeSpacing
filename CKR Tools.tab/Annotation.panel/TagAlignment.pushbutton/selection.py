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


def get_selected_elements(uidoc, doc):
    """Return the elements the user wants to tag.

    Uses the active selection when there is one; otherwise prompts for an
    interactive pick.

    Args:
        uidoc: The active UIDocument.
        doc: The active Document.

    Returns:
        list: The selected elements (may be empty if the user cancelled).
    """
    elements = []

    for element_id in uidoc.Selection.GetElementIds():
        element = doc.GetElement(element_id)
        if element is not None:
            elements.append(element)

    if elements:
        utils.logger.debug('Using {} pre-selected element(s).'.format(
            len(elements)))
        return elements

    utils.logger.debug('Nothing pre-selected; prompting interactive pick.')
    try:
        picked = revit.pick_elements(
            message='Select MEP elements to tag and align')
    except Exception as ex:
        utils.logger.debug('Pick cancelled or failed: {}'.format(ex))
        picked = None

    return list(picked) if picked else []
