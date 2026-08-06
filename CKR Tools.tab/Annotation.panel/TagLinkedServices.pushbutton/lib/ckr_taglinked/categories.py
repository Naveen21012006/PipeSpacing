# -*- coding: utf-8 -*-
"""The three supported categories and everything that differs between them.

Scope item SC-02 fixes the categories at Pipes, Ducts and Cable Trays. Each
one tags with its own tag category, measures its size from its own
parameters, and - for cable tray - has no system classification at all
(FR-03: those filters are disabled for tray rather than returning an empty
set).

Adding a fourth category later is a row in CATEGORIES plus a tag family
check; nothing else in the tool names a category directly.
"""

from Autodesk.Revit.DB import BuiltInCategory, BuiltInParameter


def _parameter(name):
    """Return a BuiltInParameter by name, or None on versions without it."""
    return getattr(BuiltInParameter, name, None)


class CategorySpec(object):
    """Everything the tool needs to know about one taggable category."""

    def __init__(self, key, label, tag_label, category, tag_category,
                 size_parameters, system_type_parameter=None,
                 classification_parameter=None):
        self.key = key
        self.label = label
        #: The tag family category as the user sees it in Revit, used
        #: verbatim when execution is blocked for a missing family
        #: (FR-02.3).
        self.tag_label = tag_label
        self.category = category
        self.tag_category = tag_category
        #: Size parameters in priority order; the LARGEST value found is the
        #: size used for range filtering, so a 600x200 duct filters on 600.
        self.size_parameters = [p for p in size_parameters if p is not None]
        self.system_type_parameter = system_type_parameter
        self.classification_parameter = classification_parameter

    @property
    def has_systems(self):
        """True when the category carries system classification / type.

        Cable tray does not, so the UI disables those two filters for it
        (FR-03) instead of quietly filtering everything away.
        """
        return self.system_type_parameter is not None

    def __repr__(self):
        return '<CategorySpec {0}>'.format(self.key)


CATEGORIES = [
    CategorySpec(
        key='pipes',
        label='Pipes',
        tag_label='Pipe Tags',
        category=BuiltInCategory.OST_PipeCurves,
        tag_category=BuiltInCategory.OST_PipeTags,
        size_parameters=[_parameter('RBS_PIPE_DIAMETER_PARAM')],
        system_type_parameter=_parameter('RBS_PIPING_SYSTEM_TYPE_PARAM'),
        classification_parameter=_parameter(
            'RBS_SYSTEM_CLASSIFICATION_PARAM'),
    ),
    CategorySpec(
        key='ducts',
        label='Ducts',
        tag_label='Duct Tags',
        category=BuiltInCategory.OST_DuctCurves,
        tag_category=BuiltInCategory.OST_DuctTags,
        size_parameters=[_parameter('RBS_CURVE_DIAMETER_PARAM'),
                         _parameter('RBS_CURVE_WIDTH_PARAM'),
                         _parameter('RBS_CURVE_HEIGHT_PARAM')],
        system_type_parameter=_parameter('RBS_DUCT_SYSTEM_TYPE_PARAM'),
        classification_parameter=_parameter(
            'RBS_SYSTEM_CLASSIFICATION_PARAM'),
    ),
    CategorySpec(
        key='trays',
        label='Cable Trays',
        tag_label='Cable Tray Tags',
        category=BuiltInCategory.OST_CableTray,
        tag_category=BuiltInCategory.OST_CableTrayTags,
        size_parameters=[_parameter('RBS_CABLETRAY_WIDTH_PARAM'),
                         _parameter('RBS_CABLETRAY_HEIGHT_PARAM')],
    ),
]

CATEGORY_KEYS = [spec.key for spec in CATEGORIES]

_BY_KEY = dict((spec.key, spec) for spec in CATEGORIES)


def by_key(key):
    """Return the CategorySpec for a key, or None."""
    return _BY_KEY.get(key)
