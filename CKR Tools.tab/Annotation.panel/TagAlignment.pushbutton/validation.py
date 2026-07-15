# -*- coding: utf-8 -*-
"""Validation for the MEP Tag Alignment tool.

Decides whether the active view can host tags and which of the selected
elements belong to a supported category. Supported categories come from
config.SUPPORTED_CATEGORIES - this module contains no category list of its
own.
"""

from Autodesk.Revit.DB import ViewType

import config
import utils


# Views in which annotation tags can be placed.
TAGGABLE_VIEW_TYPES = (
    ViewType.FloorPlan,
    ViewType.CeilingPlan,
    ViewType.EngineeringPlan,
    ViewType.AreaPlan,
    ViewType.Section,
    ViewType.Elevation,
    ViewType.Detail,
    ViewType.ThreeD,
)


def validate_view(view):
    """Check that the active view can host tags.

    Args:
        view: The active View.

    Returns:
        tuple: (ok, message). message explains the failure when ok is False.
    """
    if view is None:
        return False, 'There is no active view.'

    if view.IsTemplate:
        return False, 'Tags cannot be placed in a view template.'

    if view.ViewType not in TAGGABLE_VIEW_TYPES:
        return False, (
            'Tags cannot be placed in this type of view ({}). Open a plan, '
            'section, elevation or 3D view.'.format(view.ViewType))

    return True, ''


def filter_supported_elements(elements):
    """Split elements into supported MEP elements and ignored ones.

    Args:
        elements (list): Raw selection.

    Returns:
        tuple: (supported, ignored) lists of elements. Unsupported categories
        are ignored rather than treated as an error.
    """
    supported = []
    ignored = []

    for element in elements:
        category_value = utils.get_category_id_value(element)
        if category_value in config.SUPPORTED_CATEGORIES:
            supported.append(element)
        else:
            ignored.append(element)
            utils.logger.debug('Ignoring {} ({}): unsupported category.'.format(
                utils.element_id_value(element.Id),
                utils.get_category_name(element)))

    return supported, ignored
