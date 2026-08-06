# -*- coding: utf-8 -*-
"""Named settings profiles, per user (FR-11).

Profiles live in %APPDATA%\\CKR\\TagLinkedServices\\profiles as one JSON
file each, and the state the dialog closed with is written to
last_used.json so the next run opens exactly where the last one left off
(FR-11.3) - including tweaks the user never saved under a name.

Reading is defensive: a missing, corrupt or hand-edited file falls back to
the defaults one key at a time (config.normalise). Losing a preference is
never worth failing to start.
"""

import json
import os
import re

from ckr_taglinked import compat, config

PROFILE_DIR = compat.PROFILE_DIR
LAST_USED_PATH = os.path.join(compat.APP_DIR, 'last_used.json')

_SAFE_NAME = re.compile(r'[^A-Za-z0-9 _.-]+')


def _ensure(directory):
    if not os.path.isdir(directory):
        os.makedirs(directory)


def safe_name(name):
    """Return a profile name reduced to something safe as a filename."""
    cleaned = _SAFE_NAME.sub('_', (name or '').strip())
    return cleaned[:64] or 'Profile'


def profile_path(name):
    """Return the file path a profile name maps to."""
    return os.path.join(PROFILE_DIR, '{0}.json'.format(safe_name(name)))


def list_profiles():
    """Return the saved profile names, sorted; [] when there are none."""
    try:
        names = [os.path.splitext(entry)[0]
                 for entry in os.listdir(PROFILE_DIR)
                 if entry.lower().endswith('.json')]
    except Exception:
        return []
    return sorted(names, key=lambda text: text.lower())


def _read(path):
    try:
        with open(path, 'r') as handle:
            return json.load(handle)
    except Exception:
        return None


def _write(path, payload):
    """Write JSON through a temporary file so a crash cannot corrupt it."""
    try:
        _ensure(os.path.dirname(path))
        temporary = path + '.tmp'
        with open(temporary, 'w') as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        if os.path.exists(path):
            os.remove(path)
        os.rename(temporary, path)
        return True
    except Exception as ex:
        compat.get_log().warning('Could not write %s: %s', path, ex)
        return False


def load_profile(name):
    """Return a saved profile's settings, normalised; defaults when absent."""
    return config.normalise(_read(profile_path(name)))


def save_profile(name, values):
    """Persist settings under a profile name."""
    return _write(profile_path(name), values)


def delete_profile(name):
    """Delete a saved profile; True when it is gone afterwards."""
    try:
        path = profile_path(name)
        if os.path.exists(path):
            os.remove(path)
        return True
    except Exception as ex:
        compat.get_log().warning('Could not delete profile %s: %s', name, ex)
        return False


def load_last():
    """Return (profile name, settings) from the last run.

    The name may be '' when the last state was never saved under a
    profile - the settings still reload.
    """
    payload = _read(LAST_USED_PATH)
    if not isinstance(payload, dict):
        return '', config.defaults()
    name = payload.get('profile') or ''
    values = payload.get('values')
    if not isinstance(values, dict):
        # An older or hand-written file that holds the settings directly.
        values = payload
        name = ''
    return name, config.normalise(values)


def save_last(name, values):
    """Remember the dialog state for the next run (FR-11.3)."""
    return _write(LAST_USED_PATH, {'profile': name or '', 'values': values})
