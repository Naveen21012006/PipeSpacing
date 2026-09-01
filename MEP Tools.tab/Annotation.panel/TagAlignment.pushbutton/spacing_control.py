# -*- coding: utf-8 -*-
"""The Auto Tag spacing control - the Shift+click dialog.

Sets Auto Tag's LOCAL text gap (vertical_mm), stored in
%APPDATA%/CKR/autotag_settings.json ON TOP of the shared Align Tags value:
row pitch = drawn text height + this gap. 'Reset to default' clears the
override so the Align Tags dialog drives the spacing again.

Lives in its own module because pyRevit's Shift+click runs the bundle's
config.py as a SCRIPT - both that script and script.py call in here.
"""

import re

from pyrevit import forms

import engine_bridge
import tool_config

TITLE = 'Auto Tag - text spacing'


def ask_text_gap(confirm=False):
    """Show the gap control. Cancelling anywhere changes nothing.

    Args:
        confirm (bool): show a confirmation after saving - used when invoked
            standalone (Shift+click), where there is no run following to make
            the effect visible.
    """
    local = engine_bridge.load_local()
    if 'gap_paper_mm' in local:
        current = '{:g} mm on paper (local)'.format(
            float(local['gap_paper_mm']))
        default = '{:g}'.format(float(local['gap_paper_mm']))
    else:
        current = '{:g} mm on paper (default)'.format(
            tool_config.AUTO_DEFAULT_GAP_PAPER_MM)
        default = '{:g}'.format(tool_config.AUTO_DEFAULT_GAP_PAPER_MM)

    # PAPER millimetres: what you set is what the printed sheet shows, at any
    # view scale. (The old model-mm gap shrank by the scale factor - a 15 at
    # 1:100 was 0.15 mm on paper, which is why nothing seemed to change.)
    presets = ['Minimal (0.5 mm)', '0.75 mm (default)', '1 mm', '1.5 mm',
               '2 mm', '3 mm', '5 mm']
    choice = forms.CommandSwitchWindow.show(
        presets + ['Custom...', 'Reset to default'],
        message='Gap between texts ON PAPER - now {}:'.format(current))
    if not choice:
        return

    if choice == 'Reset to default':
        local.pop('gap_paper_mm', None)
        local.pop('vertical_mm', None)
        engine_bridge.save_local(local)
        if confirm:
            forms.alert(
                'Override cleared - the default {:g} mm paper gap applies '
                'from the next run.'.format(
                    tool_config.AUTO_DEFAULT_GAP_PAPER_MM),
                title=TITLE)
        return

    if choice == 'Custom...':
        text = forms.ask_for_string(
            default=default,
            prompt='Clear gap between two texts, in mm ON PAPER\n'
                   '(2 = 2 mm on the printed sheet at any scale):',
            title=TITLE)
        if not text:
            return
        try:
            value = float(text)
        except ValueError:
            forms.alert('"{}" is not a number - gap unchanged.'.format(text),
                        title=TITLE)
            return
    else:
        # Take the first number in the label, so the wording around it is free
        # to change ("Minimal (0.5 mm)", "0.75 mm (default)", "1.5 mm").
        found = re.search(r'[0-9]*\.?[0-9]+', choice)
        if not found:
            return
        value = float(found.group())

    local['gap_paper_mm'] = max(0.0, value)
    local.pop('vertical_mm', None)      # one knob, one meaning
    engine_bridge.save_local(local)
    if confirm:
        forms.alert(
            'Text gap set to {:g} mm ON PAPER (local override).\n'
            'Every Auto Tag run uses it until you clear it here.'.format(
                local['gap_paper_mm']),
            title=TITLE)
