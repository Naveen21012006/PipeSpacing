# -*- coding: utf-8 -*-
"""Configuration dialog for the Align Tags tool (WPF, AlignTagsDialog.xaml).

Pure presentation: reads a settings dict in, returns a settings dict out
(``result`` is None when cancelled). Lengths arrive in mm (how they are
persisted) and are shown in the document's display unit; conversion both
ways lives in common.py so this module never touches Revit unit APIs
directly.

Events are wired in code rather than in the XAML so the XAML stays loadable
by pyRevit's WPFWindow without a code-behind class attribute.
"""

import os

from pyrevit import forms

from System.Windows.Media import Brushes

import common
import engine

HELP_TEXT = (
    'Align Tags\n'
    '\n'
    '1. Select tags / leadered text notes (or preselect them before\n'
    '   starting the tool).\n'
    '2. Choose the stack quadrant, leader angle and spacings, then\n'
    '   Proceed.\n'
    '3. Pick the lowest tag head position. Each pick re-aligns the set;\n'
    '   every pick is one undo step. Press Esc to finish.\n'
    '\n'
    'Vertical Spacing is the clear gap between stacked tags - the tag\n'
    'height is measured and added automatically, so the same gap works\n'
    'at any view scale.\n'
    '\n'
    'Constant Landing gives every leader the same landing length (tag\n'
    'heads follow the leaders). Intermittent Alignment staggers the tags\n'
    'into two columns at half the row height to save space.\n'
    '\n'
    'Settings persist to %APPDATA%\\CKR\\tag_align_settings.json.'
)

_JUSTIFICATION_ORDER = ('unchanged', 'left', 'right', 'automatic')


class AlignTagsDialog(forms.WPFWindow):
    """The configuration dialog. Use show_config() instead of instantiating."""

    def __init__(self, doc, values):
        xaml = os.path.join(os.path.dirname(__file__), 'AlignTagsDialog.xaml')
        forms.WPFWindow.__init__(self, xaml)
        self.doc = doc
        self.result = None
        self._syncing_angle = False

        self._mode_buttons = {
            engine.UPPER_LEFT: self.ModeUL,
            engine.UPPER_RIGHT: self.ModeUR,
            engine.LOWER_LEFT: self.ModeLL,
            engine.LOWER_RIGHT: self.ModeLR,
        }
        self._mode = values.get('mode', engine.UPPER_LEFT)

        self._load(values)
        self._wire()
        self._highlight_mode()

    # -- initial population -------------------------------------------------
    def _load(self, values):
        unit = common.length_unit_label(self.doc)
        self.LandingUnit.Text = unit
        self.VerticalUnit.Text = unit
        self.HorizontalUnit.Text = unit
        self.ClusterUnit.Text = unit
        self.ClearanceUnit.Text = unit
        self.RackUnit.Text = unit

        self.AngleSlider.Value = float(values['angle_deg'])
        self.AngleBox.Text = '{0:g}'.format(round(values['angle_deg'], 1))
        self.LandingBox.Text = self._format_mm(values['landing_mm'])
        self.VerticalBox.Text = self._format_mm(values['vertical_mm'])
        self.HorizontalBox.Text = self._format_mm(values['horizontal_mm'])
        self.ClusterBox.Text = self._format_mm(values.get('cluster_mm',
                                                          2000.0))
        self.ClearanceBox.Text = self._format_mm(values.get('clearance_mm',
                                                            250.0))
        self.RackBox.Text = self._format_mm(values.get('rack_mm', 600.0))

        self.ConstantLandingCheck.IsChecked = values['constant_landing']
        self.IntermittentCheck.IsChecked = values['intermittent']
        self.OrderByPipeCheck.IsChecked = values.get('order_by_pipe', True)
        self.SwitchSideCheck.IsChecked = values['switch_side']
        self.AttachedEndCheck.IsChecked = values['attached_end']
        self.KeepSelectionCheck.IsChecked = values['keep_selection']
        self.SnapsOffCheck.IsChecked = values['snaps_off']

        just = values.get('justification', 'unchanged')
        index = _JUSTIFICATION_ORDER.index(just) \
            if just in _JUSTIFICATION_ORDER else 0
        self.JustificationCombo.SelectedIndex = index

    def _format_mm(self, value_mm):
        return '{0:g}'.format(
            round(common.display_from_mm(self.doc, value_mm), 4))

    # -- events -------------------------------------------------------------
    def _wire(self):
        for button in self._mode_buttons.values():
            button.Click += self._on_mode_click
        self.AngleSlider.ValueChanged += self._on_slider_changed
        self.AngleBox.TextChanged += self._on_angle_text_changed
        self.HelpButton.Click += self._on_help
        self.ProceedButton.Click += self._on_proceed
        self.CancelButton.Click += self._on_cancel

    def _on_mode_click(self, sender, _args):
        self._mode = sender.Tag
        self._highlight_mode()

    def _highlight_mode(self):
        for mode, button in self._mode_buttons.items():
            if mode == self._mode:
                button.BorderBrush = Brushes.SteelBlue
                button.Background = Brushes.AliceBlue
            else:
                button.BorderBrush = Brushes.LightGray
                button.Background = Brushes.White

    def _on_slider_changed(self, _sender, _args):
        if self._syncing_angle:
            return
        self._syncing_angle = True
        try:
            self.AngleBox.Text = '{0:g}'.format(
                round(self.AngleSlider.Value, 0))
        finally:
            self._syncing_angle = False

    def _on_angle_text_changed(self, _sender, _args):
        if self._syncing_angle:
            return
        self._syncing_angle = True
        try:
            value = float(self.AngleBox.Text)
            self.AngleSlider.Value = max(0.0, min(90.0, value))
        except (TypeError, ValueError):
            pass  # keep typing; validated on Proceed
        finally:
            self._syncing_angle = False

    def _on_help(self, _sender, _args):
        """Open the bundled help page; fall back to the text summary."""
        try:
            import webbrowser
            path = os.path.join(os.path.dirname(__file__), 'help.html')
            if os.path.exists(path):
                webbrowser.open('file:///' + path.replace('\\', '/'))
                return
        except Exception as ex:
            common.logger.debug('Help page failed: {}'.format(ex))
        forms.alert(HELP_TEXT, title='Align Tags - Help')

    def _on_cancel(self, _sender, _args):
        self.result = None
        self.Close()

    def _on_proceed(self, _sender, _args):
        parsed = self._parse()
        if parsed is None:
            return  # invalid input reported, dialog stays open
        self.result = parsed
        self.Close()

    # -- validation ---------------------------------------------------------
    def _mm_field(self, box, label, minimum):
        """Parse one length textbox back to mm; alert and return None if bad."""
        try:
            value_mm = common.mm_from_display(self.doc, float(box.Text))
        except (TypeError, ValueError):
            forms.alert('{0} must be a number.'.format(label),
                        title='Align Tags')
            return None
        if value_mm < minimum:
            forms.alert('{0} must be at least {1:g} mm.'.format(
                label, minimum), title='Align Tags')
            return None
        return value_mm

    def _parse(self):
        try:
            angle = float(self.AngleBox.Text)
        except (TypeError, ValueError):
            forms.alert('Angle must be a number between 0 and 89 '
                        '(0 = straight leaders).', title='Align Tags')
            return None
        angle = engine.normalize_angle(angle)

        vertical = self._mm_field(self.VerticalBox, 'Vertical Spacing', 0.01)
        if vertical is None:
            return None
        landing = self._mm_field(self.LandingBox, 'Landing Distance', 0.0)
        if landing is None:
            return None
        horizontal = self._mm_field(
            self.HorizontalBox, 'Horizontal Spacing', 0.0)
        if horizontal is None:
            return None
        cluster = self._mm_field(self.ClusterBox, 'Cluster Distance', 0.0)
        if cluster is None:
            return None
        clearance = self._mm_field(
            self.ClearanceBox, 'Elbow-Arrowhead Distance', 0.0)
        if clearance is None:
            return None
        rack = self._mm_field(self.RackBox, 'Rack Width', 0.0)
        if rack is None:
            return None

        return {
            'mode': self._mode,
            'angle_deg': angle,
            'vertical_mm': vertical,
            'landing_mm': landing,
            'horizontal_mm': horizontal,
            'cluster_mm': cluster,
            'clearance_mm': clearance,
            'rack_mm': rack,
            'constant_landing': bool(self.ConstantLandingCheck.IsChecked),
            'intermittent': bool(self.IntermittentCheck.IsChecked),
            'order_by_pipe': bool(self.OrderByPipeCheck.IsChecked),
            'switch_side': bool(self.SwitchSideCheck.IsChecked),
            'attached_end': bool(self.AttachedEndCheck.IsChecked),
            'keep_selection': bool(self.KeepSelectionCheck.IsChecked),
            'snaps_off': bool(self.SnapsOffCheck.IsChecked),
            'justification': _JUSTIFICATION_ORDER[
                max(0, self.JustificationCombo.SelectedIndex)],
        }


def show_config(doc, values):
    """Show the dialog modally; return the new settings dict or None."""
    dialog = AlignTagsDialog(doc, values)
    dialog.show_dialog()
    return dialog.result
