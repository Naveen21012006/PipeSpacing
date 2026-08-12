# -*- coding: utf-8 -*-
"""Unit tests for the Drainage Flow Arrows settings persistence.

The module is pure python (no Revit imports): sanitising, merging and
the JSON round-trip are verified here so a corrupt or hand-edited
settings file can never wedge the tool inside Revit.
"""

import os

import flowarrow_core as core
import flowarrow_settings as fa


DEFAULTS = dict(core.DEFAULTS)


# ---------------------------------------------------------------------------
# Number sanitising
# ---------------------------------------------------------------------------
def test_valid_numbers_pass_through():
    assert fa.sanitize_number(1500, 999.0) == 1500.0
    assert fa.sanitize_number('2500', 999.0) == 2500.0
    assert fa.sanitize_number(0.0, 999.0) == 0.0


def test_garbage_falls_back():
    assert fa.sanitize_number('ten metres', 999.0) == 999.0
    assert fa.sanitize_number(None, 999.0) == 999.0
    assert fa.sanitize_number([], 999.0) == 999.0
    assert fa.sanitize_number(float('nan'), 999.0) == 999.0
    assert fa.sanitize_number(float('inf'), 999.0) == 999.0


def test_below_minimum_falls_back():
    assert fa.sanitize_number(-5, 999.0) == 999.0
    assert fa.sanitize_number(50, 999.0, minimum=100.0) == 999.0
    assert fa.sanitize_number(100, 999.0, minimum=100.0) == 100.0


# ---------------------------------------------------------------------------
# Merging stored settings over the defaults
# ---------------------------------------------------------------------------
def test_merge_overrides_only_valid_numbers():
    stored = {'min_pipe_length_mm': 500,
              'multi_arrow_threshold_mm': 'broken',
              'end_clearance_mm': -3}
    merged = fa.merge_numbers(DEFAULTS, stored)
    assert merged['min_pipe_length_mm'] == 500.0
    assert (merged['multi_arrow_threshold_mm'] ==
            DEFAULTS['multi_arrow_threshold_mm'])
    assert merged['end_clearance_mm'] == DEFAULTS['end_clearance_mm']


def test_merge_keeps_unknown_defaults_untouched():
    merged = fa.merge_numbers(DEFAULTS, {'min_pipe_length_mm': 500})
    assert merged['vertical_angle_deg'] == DEFAULTS['vertical_angle_deg']


def test_merge_survives_a_non_dict():
    assert fa.merge_numbers(DEFAULTS, None) == DEFAULTS
    assert fa.merge_numbers(DEFAULTS, 'oops') == DEFAULTS


def test_zero_threshold_is_rejected():
    # The station count divides by the threshold - zero must not get in.
    merged = fa.merge_numbers(DEFAULTS, {'multi_arrow_threshold_mm': 0})
    assert (merged['multi_arrow_threshold_mm'] ==
            DEFAULTS['multi_arrow_threshold_mm'])


# ---------------------------------------------------------------------------
# Remembered arrow type
# ---------------------------------------------------------------------------
def test_remembered_type_label_roundtrip():
    stored = {fa.TYPE_LABEL_KEY: 'MEP-Tag-Pipe Flow Arrow : Flow Left'}
    assert (fa.remembered_type_label(stored) ==
            'MEP-Tag-Pipe Flow Arrow : Flow Left')


def test_remembered_type_label_rejects_junk():
    assert fa.remembered_type_label({}) is None
    assert fa.remembered_type_label({fa.TYPE_LABEL_KEY: ''}) is None
    assert fa.remembered_type_label({fa.TYPE_LABEL_KEY: 42}) is None
    assert fa.remembered_type_label(None) is None


# ---------------------------------------------------------------------------
# File round-trip
# ---------------------------------------------------------------------------
def test_save_and_load_roundtrip(tmp_path):
    path = os.path.join(str(tmp_path), 'sub', 'flow_arrow_settings.json')
    data = {'min_pipe_length_mm': 750.0, fa.TYPE_LABEL_KEY: 'Fam : Type'}
    fa.save(path, data)
    assert fa.load(path) == data


def test_load_missing_or_corrupt_file_returns_empty(tmp_path):
    assert fa.load(os.path.join(str(tmp_path), 'nowhere.json')) == {}
    bad = os.path.join(str(tmp_path), 'bad.json')
    with open(bad, 'w') as handle:
        handle.write('{not json')
    assert fa.load(bad) == {}
    listfile = os.path.join(str(tmp_path), 'list.json')
    with open(listfile, 'w') as handle:
        handle.write('[1, 2]')
    assert fa.load(listfile) == {}
