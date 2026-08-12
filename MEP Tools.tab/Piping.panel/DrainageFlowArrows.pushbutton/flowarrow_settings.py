# -*- coding: utf-8 -*-
"""Settings persistence for the Drainage Flow Arrows tool.

The Shift+click dialog edits the four placement numbers; the last-used
arrow type label is remembered so a plain click skips the type picker.
Everything persists to %APPDATA%/CKR/flow_arrow_settings.json (script.py
owns the path - this module is pure python so the sanitising rules are
unit-tested outside Revit, tests/test_flowarrow_settings.py).

Loading is defensive: a missing, corrupt or hand-edited file silently
falls back to the code defaults - the tool must never fail to start
because of a settings file.
"""

import json
import os

SETTINGS_FILE = 'flow_arrow_settings.json'

# The numbers the dialog edits (all millimetres). Every other CONFIG rule
# stays code-only by design (2026-08-12 decision: keep the dialog small).
NUMBER_KEYS = ('min_pipe_length_mm', 'multi_arrow_threshold_mm',
               'end_clearance_mm', 'duplicate_tolerance_mm',
               'rack_width_mm')

# Hard floors: a stored value below its floor is treated as invalid and
# falls back to the default. The threshold must stay positive - the
# station count divides by it; the rest may legitimately be zero
# (rack width 0 = rack alignment off).
MINIMUMS = {
    'min_pipe_length_mm': 0.0,
    'multi_arrow_threshold_mm': 100.0,
    'end_clearance_mm': 0.0,
    'duplicate_tolerance_mm': 0.0,
    'rack_width_mm': 0.0,
}

# The remembered arrow type ('Family : Type' label from the picker).
TYPE_LABEL_KEY = 'arrow_type_label'


def sanitize_number(value, fallback, minimum=0.0):
    """Return value as a finite float >= minimum, else the fallback."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number != number:                      # NaN
        return fallback
    if number in (float('inf'), float('-inf')):
        return fallback
    if number < minimum:
        return fallback
    return number


def merge_numbers(defaults, stored):
    """Return the defaults overridden by the valid stored numbers.

    Only NUMBER_KEYS are touched; unknown keys in either dict pass
    through from defaults untouched, so the CONFIG dict can be handed in
    whole.
    """
    merged = dict(defaults)
    if not isinstance(stored, dict):
        return merged
    for key in NUMBER_KEYS:
        if key in stored:
            merged[key] = sanitize_number(
                stored[key], defaults[key], MINIMUMS.get(key, 0.0))
    return merged


def remembered_type_label(stored):
    """Return the stored 'Family : Type' label, or None."""
    if not isinstance(stored, dict):
        return None
    label = stored.get(TYPE_LABEL_KEY)
    if isinstance(label, str) and label.strip():
        return label
    try:                                       # IronPython unicode strings
        if isinstance(label, unicode) and label.strip():  # noqa: F821
            return label
    except NameError:
        pass
    return None


def load(path):
    """Return the stored settings dict, or {} when unavailable/corrupt."""
    try:
        with open(path, 'r') as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save(path, data):
    """Write the settings dict as JSON, creating the folder if needed."""
    folder = os.path.dirname(path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    with open(path, 'w') as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
