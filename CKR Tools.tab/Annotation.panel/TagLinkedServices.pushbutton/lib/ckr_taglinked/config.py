# -*- coding: utf-8 -*-
"""The settings schema, its defaults, and defensive normalisation.

Pure Python - no Revit API - so the defaults and the coercion rules are
unit-testable. Every default here is the one stated in the brief:

    minimum visible horizontal run   3000 mm   (FR-04.3)
    minimum visible vertical run     2000 mm   (FR-04.3)
    minimum visible inclined run     3000 mm, inclined excluded by default
    horizontal / vertical tolerance  15 / 75 degrees (clause 7.3)
    horizontal tag offset            3 mm on paper (clause 7.6.1)
    riser tag offset                 8 mm on paper (clause 7.6.2)
    minimum clear tag spacing        5 mm on paper (clause 7.6.6)

Lengths are stored in millimetres whatever the project's display units,
so a profile survives moving between metric and imperial jobs.

Tag types and filter selections are stored as NAMES ('Family : Type',
level names, workset names). Element ids do not survive a reissued link or
a different project; names do, and they are what the engineer reads.
"""

import copy

from ckr_taglinked import core

#: The category keys the schema knows about. categories.py owns what each
#: one MEANS to Revit; the schema only needs their names, which is what
#: keeps this module free of the Revit API and testable outside it.
CATEGORY_KEYS = ('pipes', 'ducts', 'trays')

ORIENTATIONS = ('horizontal', 'vertical', 'model')

try:                       # IronPython 2.7 hands back unicode from json
    STRING_TYPES = (str, unicode)   # noqa: F821
except NameError:          # CPython 3, used by the test suite
    STRING_TYPES = (str,)


def _default_tags():
    return dict((key, {'horizontal': None,
                       'vertical': None,
                       'leader': True,
                       'orientation': 'horizontal'})
                for key in CATEGORY_KEYS)


DEFAULTS = {
    # FR-01 - selected link instances, by host element id value.
    'links': [],

    # FR-02 - which categories run, and with which tag types.
    'categories': dict((key, key == 'pipes') for key in CATEGORY_KEYS),
    'tags': _default_tags(),

    # FR-03 - filters. Empty means inactive, never "nothing passes".
    'types': dict((key, []) for key in CATEGORY_KEYS),
    'classifications': [],
    'system_types': [],
    'levels': [],
    'worksets': [],
    'size_from_mm': None,
    'size_to_mm': None,

    # FR-04 - classification and length rules.
    'horizontal_tol_deg': 15.0,
    'vertical_tol_deg': 75.0,
    'min_horizontal_mm': 3000.0,
    'min_vertical_mm': 2000.0,
    'min_inclined_mm': 3000.0,
    'include_inclined': False,

    # Clause 7.4.2 - view range.
    'extend_to_view_depth': False,

    # FR-06 / clause 7.6 - placement, all paper millimetres.
    'offset_horizontal_mm': 3.0,
    'offset_vertical_mm': 8.0,
    'spacing_mm': 5.0,

    # FR-07 / clause 7.5.
    'skip_tagged': True,
    'verify_visible': True,
}

_FLOAT_KEYS = ('horizontal_tol_deg', 'vertical_tol_deg', 'min_horizontal_mm',
               'min_vertical_mm', 'min_inclined_mm', 'offset_horizontal_mm',
               'offset_vertical_mm', 'spacing_mm')
_OPTIONAL_FLOAT_KEYS = ('size_from_mm', 'size_to_mm')
_BOOL_KEYS = ('include_inclined', 'extend_to_view_depth', 'skip_tagged',
              'verify_visible')
_LIST_KEYS = ('links', 'classifications', 'system_types', 'levels',
              'worksets')


def defaults():
    """Return a fresh copy of the default settings."""
    return copy.deepcopy(DEFAULTS)


def _as_float(value, fallback):
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def normalise(raw):
    """Return a valid settings dict from anything at all.

    A hand-edited or partially corrupt profile must never stop the tool
    from opening: unknown keys are dropped and bad values fall back to
    their default, one key at a time.
    """
    values = defaults()
    if not isinstance(raw, dict):
        return values

    for key in _FLOAT_KEYS:
        if key in raw:
            values[key] = _as_float(raw[key], values[key])
    for key in _OPTIONAL_FLOAT_KEYS:
        if raw.get(key) in (None, ''):
            values[key] = None
        elif key in raw:
            values[key] = _as_float(raw[key], None)
    for key in _BOOL_KEYS:
        if isinstance(raw.get(key), bool):
            values[key] = raw[key]
    for key in _LIST_KEYS:
        if isinstance(raw.get(key), (list, tuple)):
            values[key] = list(raw[key])

    if isinstance(raw.get('categories'), dict):
        for key in CATEGORY_KEYS:
            flag = raw['categories'].get(key)
            if isinstance(flag, bool):
                values['categories'][key] = flag

    if isinstance(raw.get('types'), dict):
        for key in CATEGORY_KEYS:
            selection = raw['types'].get(key)
            if isinstance(selection, (list, tuple)):
                values['types'][key] = list(selection)

    if isinstance(raw.get('tags'), dict):
        for key in CATEGORY_KEYS:
            row = raw['tags'].get(key)
            if not isinstance(row, dict):
                continue
            target = values['tags'][key]
            for field in ('horizontal', 'vertical'):
                value = row.get(field)
                target[field] = value if isinstance(value, STRING_TYPES) \
                    else None
            if isinstance(row.get('leader'), bool):
                target['leader'] = row['leader']
            if row.get('orientation') in ORIENTATIONS:
                target['orientation'] = row['orientation']

    # A tolerance pair that crosses over would classify everything as
    # inclined; keep them apart rather than refuse to start.
    if values['vertical_tol_deg'] <= values['horizontal_tol_deg']:
        values['horizontal_tol_deg'] = DEFAULTS['horizontal_tol_deg']
        values['vertical_tol_deg'] = DEFAULTS['vertical_tol_deg']

    return values


def minimums_in_feet(values):
    """Return {classification: minimum visible length in feet}.

    Inclined runs are excluded by default (FR-04.3); when they are not
    included at all the caller drops them before this is consulted.
    """
    return {
        core.HORIZONTAL: core.mm_to_feet(values['min_horizontal_mm']),
        core.VERTICAL: core.mm_to_feet(values['min_vertical_mm']),
        core.INCLINED: core.mm_to_feet(values['min_inclined_mm']),
    }


def included_categories(values):
    """Return the keys of the categories switched on."""
    return [key for key in CATEGORY_KEYS if values['categories'].get(key)]
