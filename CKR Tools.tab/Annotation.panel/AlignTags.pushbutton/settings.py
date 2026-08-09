# -*- coding: utf-8 -*-
"""Settings persistence for the Align Tags tool.

All dialog values persist to %APPDATA%/CKR/tag_align_settings.json and are
reloaded on the next run, session-independent (spec, Feature 2). Lengths are
stored in millimetres regardless of the document's display unit, so the file
survives moving between metric and imperial projects; the dialog converts
for display.

Loading is defensive: a missing, corrupt or partially-edited file silently
falls back to the spec defaults - the tool must never fail to start because
of a settings file.
"""

import json
import os

import common
import engine

SETTINGS_PATH = os.path.join(common.CKR_DIR, 'tag_align_settings.json')

# Spec defaults (Feature 2), lengths in mm.
DEFAULTS = {
    'mode': engine.UPPER_LEFT,
    'angle_deg': 45.0,
    'vertical_mm': 60.96,
    'landing_mm': 1524.0,
    'horizontal_mm': 3048.0,
    'cluster_mm': 2000.0,   # 0 disables auto-splitting
    'switch_side': False,
    'attached_end': False,
    'keep_selection': True,
    'snaps_off': False,
    'constant_landing': False,
    'intermittent': False,
    'order_by_pipe': True,
    'justification': 'unchanged',   # unchanged | left | right | automatic
}

_BOOL_KEYS = ('switch_side', 'attached_end', 'keep_selection', 'snaps_off',
              'constant_landing', 'intermittent', 'order_by_pipe')
_FLOAT_KEYS = ('angle_deg', 'vertical_mm', 'landing_mm', 'horizontal_mm',
               'cluster_mm')
_JUSTIFICATIONS = ('unchanged', 'left', 'right', 'automatic')


def load():
    """Return the saved settings merged over the defaults.

    Unknown keys are dropped, wrong-typed values fall back to their default,
    so hand-edited files degrade gracefully.
    """
    values = dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH, 'r') as handle:
            raw = json.load(handle)
    except Exception:
        return values

    if not isinstance(raw, dict):
        return values

    for key in _BOOL_KEYS:
        if isinstance(raw.get(key), bool):
            values[key] = raw[key]
    for key in _FLOAT_KEYS:
        try:
            values[key] = float(raw[key])
        except (KeyError, TypeError, ValueError):
            pass
    if raw.get('mode') in engine.MODES:
        values['mode'] = raw['mode']
    if raw.get('justification') in _JUSTIFICATIONS:
        values['justification'] = raw['justification']

    # 0 is a legal saved value: it means straight leaders.
    values['angle_deg'] = engine.normalize_angle(values['angle_deg'])
    return values


def save(values):
    """Persist settings; write-then-replace so a crash can't corrupt them.

    Returns:
        bool: True on success. Failure is logged, never raised - losing a
        preference is not worth aborting an alignment.
    """
    try:
        if not os.path.isdir(common.CKR_DIR):
            os.makedirs(common.CKR_DIR)
        known = dict((key, values[key]) for key in DEFAULTS if key in values)
        temp_path = SETTINGS_PATH + '.tmp'
        with open(temp_path, 'w') as handle:
            json.dump(known, handle, indent=2, sort_keys=True)
        if os.path.exists(SETTINGS_PATH):
            os.remove(SETTINGS_PATH)
        os.rename(temp_path, SETTINGS_PATH)
        return True
    except Exception as ex:
        common.get_file_logger().warning('Could not save settings: %s', ex)
        return False
