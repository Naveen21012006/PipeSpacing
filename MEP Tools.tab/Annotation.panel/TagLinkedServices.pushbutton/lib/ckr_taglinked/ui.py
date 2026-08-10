# -*- coding: utf-8 -*-
"""The configuration dialog (WPF, TagLinkedServicesDialog.xaml).

Presentation only: a settings dict in, an (action, settings dict) pair out.
The dialog never opens a transaction and never creates a tag - Preview and
Place both close it and hand back to script.py, so all document
modification happens in one place, in the command's own context.

Dynamic content - the link list, the tag type combos and the four filter
panels - is built in code rather than by data binding: IronPython objects
do not raise WPF change notifications, so imperative construction is both
simpler and more reliable here. Events are wired in code for the same
reason the rest of the extension does it: the XAML stays loadable by
pyRevit's WPFWindow with no code-behind class.

Every length in this dialog is millimetres. Paper-space values say so on
the label, because the difference matters (clause 5.4).
"""

import os

from pyrevit import forms

from System.Windows import TextWrapping, Thickness
from System.Windows.Controls import CheckBox, TextBlock, TreeViewItem
from System.Windows.Media import Brushes

from ckr_taglinked import categories, config, filters, settings, tagtypes

SAME_AS_RUN = '<same as run tag>'
ORIENTATION_LABELS = (('horizontal', 'Horizontal'),
                      ('vertical', 'Vertical'),
                      ('model', 'Model (along the run)'))

HELP_TEXT = (
    'Tag Linked Services\n'
    '\n'
    'Tags pipes, ducts and cable trays that live in a LINKED model, in the\n'
    'active floor plan, the way Tag All Not Tagged would if it could see\n'
    'into links.\n'
    '\n'
    '1. Links and tags - tick the link instances to read, the categories\n'
    '   to tag, and the tag family:type for each. Risers can use their own\n'
    '   tag family.\n'
    '2. Filters - narrow by family/type, system, size, level or workset.\n'
    '   Ticking nothing in a list means that filter is off.\n'
    '3. Rules - the angle that separates horizontal from vertical, and the\n'
    '   minimum VISIBLE length a run needs before it earns a tag.\n'
    '4. Placement - offsets and clearances in millimetres on the sheet.\n'
    '\n'
    'Preview runs the whole chain and reports the counts without leaving a\n'
    'single tag behind. The counts it reports are the counts you get.\n'
    '\n'
    'Two things worth knowing:\n'
    '  - Length is measured on the part of the run inside THIS view, so a\n'
    '    riser modelled as one tall element is judged per plan.\n'
    '  - Insertion points are kept inside the annotation crop, because a\n'
    '    tag outside it is not drawn on the sheet at all.\n'
    '\n'
    'Profiles are saved per user under\n'
    '%APPDATA%\\CKR\\TagLinkedServices\\profiles.'
)


def _hint(text):
    """Return a small grey caption for a filter panel."""
    block = TextBlock()
    block.Text = text
    block.Foreground = Brushes.Gray
    block.FontSize = 10
    block.Margin = Thickness(0, 0, 0, 4)
    block.TextWrapping = TextWrapping.Wrap
    return block


class TagLinkedServicesDialog(forms.WPFWindow):
    """The settings dialog. Use show() rather than instantiating this."""

    def __init__(self, doc, targets, values, profile_name):
        xaml = os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))),
            'TagLinkedServicesDialog.xaml')
        forms.WPFWindow.__init__(self, xaml)

        self.doc = doc
        self.targets = targets
        self.action = None
        self.values = config.normalise(values)
        self.profile_name = profile_name or ''

        self._loading = True
        self._contexts = {}          # link id value -> filters.LinkContext
        self._link_checks = []       # (target, CheckBox)
        self._type_nodes = []        # (category key, label, CheckBox)
        self._filter_checks = {'classifications': [], 'system_types': [],
                               'levels': [], 'worksets': []}
        self._tag_labels = tagtypes.labels(doc)

        self._rows = {
            'pipes': {'include': self.PipesInclude, 'h': self.PipesTagH,
                      'v': self.PipesTagV, 'leader': self.PipesLeader,
                      'orient': self.PipesOrient},
            'ducts': {'include': self.DuctsInclude, 'h': self.DuctsTagH,
                      'v': self.DuctsTagV, 'leader': self.DuctsLeader,
                      'orient': self.DuctsOrient},
            'trays': {'include': self.TraysInclude, 'h': self.TraysTagH,
                      'v': self.TraysTagV, 'leader': self.TraysLeader,
                      'orient': self.TraysOrient},
        }

        self._build_links()
        self._update_link_count()
        self._build_tag_rows()
        self._apply(self.values)
        self._rebuild_filters()
        self._build_profiles()
        self._wire()
        self._loading = False

    # -- links --------------------------------------------------------------
    def _build_links(self):
        """One checkbox per link INSTANCE (FR-01.3), unloaded ones disabled."""
        panel = self.LinksPanel
        panel.Children.Clear()
        chosen = set(self.values.get('links') or [])
        first_run = not chosen

        if not self.targets:
            panel.Children.Add(_hint('No Revit links are placed in this '
                                     'model.'))
            return

        for target in self.targets:
            check = CheckBox()
            label = target.name
            if not target.loaded:
                label = '{0}  -  {1}'.format(label, target.reason)
            elif target.nested:
                label = ('{0}  -  contains nested links, which are not '
                         'supported'.format(label))
            check.Content = label
            check.Margin = Thickness(0, 2, 0, 2)
            check.IsEnabled = target.loaded
            check.IsChecked = target.loaded and (
                first_run or target.id_value in chosen)
            check.Checked += self._on_links_changed
            check.Unchecked += self._on_links_changed
            panel.Children.Add(check)
            self._link_checks.append((target, check))

    def _selectable_links(self):
        """Return the checkboxes of links that can actually be ticked."""
        return [check for _target, check in self._link_checks
                if check.IsEnabled]

    def _set_links(self, mode):
        """All / None / Invert over the link list.

        Only loaded links respond: an unloaded or nested one has nothing
        to read, so leaving it out of a bulk action is the same courtesy
        as showing it disabled in the first place.

        The filter lists are rebuilt ONCE at the end rather than on each
        checkbox, which on a federation of a dozen links is the
        difference between instant and a visible stall.
        """
        checks = self._selectable_links()
        self._loading = True
        try:
            for check in checks:
                if mode == 'all':
                    check.IsChecked = True
                elif mode == 'none':
                    check.IsChecked = False
                else:
                    check.IsChecked = not check.IsChecked
        finally:
            self._loading = False
        self._update_link_count()
        self._rebuild_filters()

    def _update_link_count(self):
        """Keep the running count beside the All / None / Invert buttons."""
        checks = self._selectable_links()
        chosen = len([check for check in checks if check.IsChecked])
        enabled = bool(checks)
        for button in (self.LinksAllButton, self.LinksNoneButton,
                       self.LinksInvertButton):
            button.IsEnabled = enabled
        if not enabled:
            self.LinksCount.Text = 'no links can be read'
            return
        self.LinksCount.Text = '{0} of {1} selected'.format(chosen,
                                                            len(checks))

    def selected_targets(self):
        """Return the LinkTargets currently ticked."""
        return [target for target, check in self._link_checks
                if check.IsChecked and target.loaded]

    def _context(self, target):
        """Return a cached LinkContext for a link (built once per dialog)."""
        key = target.id_value
        if key not in self._contexts:
            self._contexts[key] = filters.LinkContext(target)
        return self._contexts[key]

    # -- category rows ------------------------------------------------------
    def _build_tag_rows(self):
        """Fill the tag type and orientation combos (FR-02.2)."""
        for spec in categories.CATEGORIES:
            row = self._rows[spec.key]
            available = self._tag_labels.get(spec.key, [])

            row['h'].Items.Clear()
            for label in available:
                row['h'].Items.Add(label)
            row['v'].Items.Clear()
            row['v'].Items.Add(SAME_AS_RUN)
            for label in available:
                row['v'].Items.Add(label)

            row['orient'].Items.Clear()
            for _key, label in ORIENTATION_LABELS:
                row['orient'].Items.Add(label)

            if not available:
                row['h'].IsEnabled = False
                row['v'].IsEnabled = False
                row['h'].ToolTip = (
                    'No {0} family is loaded in this project. Load one and '
                    'reopen the tool.'.format(spec.tag_label))

    # -- filters ------------------------------------------------------------
    def _rebuild_filters(self, from_settings=False):
        """Rebuild the filter lists from the links currently ticked.

        Args:
            from_settings (bool): Restore the ticks from ``self.values``
                rather than from what is on screen. True when a profile
                has just been loaded; False when the user has merely
                changed which links are read, where the ticks they have
                already made must survive the rebuild.
        """
        keep = self._current_filter_selection(from_settings)
        contexts = [self._context(target)
                    for target in self.selected_targets()]
        options = filters.build_options(contexts)

        self._build_type_tree(options, keep['types'])
        self._build_check_panel(
            self.ClassificationPanel, 'classifications',
            [(value, filters.pretty_classification(value))
             for value in options.sorted_classifications()],
            keep['classifications'],
            'Cable tray carries no classification.')
        self._build_check_panel(
            self.SystemTypePanel, 'system_types',
            [(value, value) for value in options.sorted_system_types()],
            keep['system_types'], None)
        self._build_check_panel(
            self.LevelPanel, 'levels',
            [(value, value) for value in options.sorted_levels()],
            keep['levels'], None)
        self._build_check_panel(
            self.WorksetPanel, 'worksets',
            [(value, value) for value in options.sorted_worksets()],
            keep['worksets'],
            'Only workshared links have worksets.')

    def _current_filter_selection(self, from_settings=False):
        """Return the ticks to restore: from the settings, or from screen."""
        nothing_built = (not self._type_nodes
                         and not self._filter_checks['levels'])
        if from_settings or nothing_built:
            return {'types': dict(self.values['types']),
                    'classifications': list(self.values['classifications']),
                    'system_types': list(self.values['system_types']),
                    'levels': list(self.values['levels']),
                    'worksets': list(self.values['worksets'])}
        return self._read_filters()

    def _build_type_tree(self, options, keep):
        """Build the Category -> Family -> Type checkbox tree (FR-03.1)."""
        tree = self.TypeTree
        tree.Items.Clear()
        self._type_nodes = []

        for spec in categories.CATEGORIES:
            families = options.families(spec.key)
            if not families:
                continue
            wanted = set(keep.get(spec.key) or [])

            category_item = TreeViewItem()
            category_check = CheckBox()
            category_check.Content = spec.label
            category_item.Header = category_check
            category_item.IsExpanded = False
            leaves = []

            for family, names in families:
                family_item = TreeViewItem()
                family_check = CheckBox()
                family_check.Content = family
                family_item.Header = family_check
                family_leaves = []

                for name in names:
                    label = filters.type_label(family, name)
                    leaf_item = TreeViewItem()
                    leaf_check = CheckBox()
                    leaf_check.Content = name
                    leaf_check.IsChecked = label in wanted
                    leaf_item.Header = leaf_check
                    family_item.Items.Add(leaf_item)
                    family_leaves.append(leaf_check)
                    leaves.append(leaf_check)
                    self._type_nodes.append((spec.key, label, leaf_check))

                self._wire_parent(family_check, family_leaves)
                category_item.Items.Add(family_item)

            self._wire_parent(category_check, leaves)
            tree.Items.Add(category_item)

    def _wire_parent(self, parent_check, children):
        """Make a parent checkbox tick or clear everything beneath it."""
        def on_click(_sender, _args):
            state = bool(parent_check.IsChecked)
            for child in children:
                child.IsChecked = state
        parent_check.Click += on_click

    def _build_check_panel(self, panel, key, entries, keep, empty_note):
        """Fill one filter panel with checkboxes; remember them for parsing."""
        panel.Children.Clear()
        self._filter_checks[key] = []
        if not entries:
            panel.Children.Add(_hint(empty_note or 'Nothing to filter on.'))
            return
        panel.Children.Add(_hint('Tick nothing to accept all.'))
        wanted = set(keep or [])
        for value, label in entries:
            check = CheckBox()
            check.Content = label
            check.IsChecked = value in wanted
            check.Margin = Thickness(0, 1, 0, 1)
            panel.Children.Add(check)
            self._filter_checks[key].append((value, check))

    def _read_filters(self):
        """Return the filter selections currently ticked."""
        types = dict((spec.key, []) for spec in categories.CATEGORIES)
        for category_key, label, check in self._type_nodes:
            if check.IsChecked:
                types[category_key].append(label)
        picked = {'types': types}
        for key, entries in self._filter_checks.items():
            picked[key] = [value for value, check in entries
                           if check.IsChecked]
        return picked

    # -- profiles -----------------------------------------------------------
    def _build_profiles(self):
        """Fill the profile combo and select the last-used one (FR-11)."""
        combo = self.ProfileCombo
        combo.Items.Clear()
        for name in settings.list_profiles():
            combo.Items.Add(name)
        if self.profile_name and self.profile_name in list(combo.Items):
            combo.SelectedItem = self.profile_name

    # -- events -------------------------------------------------------------
    def _wire(self):
        self.LinksAllButton.Click += self._on_links_all
        self.LinksNoneButton.Click += self._on_links_none
        self.LinksInvertButton.Click += self._on_links_invert
        self.PreviewButton.Click += self._on_preview
        self.PlaceButton.Click += self._on_place
        self.CancelButton.Click += self._on_cancel
        self.HelpButton.Click += self._on_help
        self.ProfileSaveButton.Click += self._on_profile_save
        self.ProfileDeleteButton.Click += self._on_profile_delete
        self.ProfileCombo.SelectionChanged += self._on_profile_selected

    def _on_links_all(self, _sender, _args):
        self._set_links('all')

    def _on_links_none(self, _sender, _args):
        self._set_links('none')

    def _on_links_invert(self, _sender, _args):
        self._set_links('invert')

    def _on_links_changed(self, _sender, _args):
        if self._loading:
            return
        self._update_link_count()
        self._rebuild_filters()

    def _on_help(self, _sender, _args):
        forms.alert(HELP_TEXT, title='Tag Linked Services - Help')

    def _on_cancel(self, _sender, _args):
        self.action = None
        self.Close()

    def _on_preview(self, _sender, _args):
        self._finish('preview')

    def _on_place(self, _sender, _args):
        self._finish('place')

    def _finish(self, action):
        parsed = self._parse()
        if parsed is None:
            return                    # the problem was reported; stay open
        self.values = parsed
        self.action = action
        self.Close()

    def _on_profile_selected(self, _sender, _args):
        if self._loading:
            return
        name = self.ProfileCombo.SelectedItem
        if not name:
            return
        self._loading = True
        try:
            self.profile_name = name
            self._apply(settings.load_profile(name))
            self._rebuild_filters(from_settings=True)
        finally:
            self._loading = False

    def _on_profile_save(self, _sender, _args):
        parsed = self._parse()
        if parsed is None:
            return
        name = forms.ask_for_string(
            default=self.profile_name or 'Default',
            prompt='Save these settings as:',
            title='Tag Linked Services')
        if not name:
            return
        if settings.save_profile(name, parsed):
            self.profile_name = name
            self._loading = True
            try:
                self._build_profiles()
                self.ProfileCombo.SelectedItem = settings.safe_name(name)
            finally:
                self._loading = False
        else:
            forms.alert('The profile could not be saved. See the log in\n'
                        '%APPDATA%\\CKR\\TagLinkedServices\\logs.',
                        title='Tag Linked Services')

    def _on_profile_delete(self, _sender, _args):
        name = self.ProfileCombo.SelectedItem
        if not name:
            return
        if not forms.alert('Delete the profile "{0}"?'.format(name),
                           title='Tag Linked Services', yes=True, no=True):
            return
        settings.delete_profile(name)
        if self.profile_name == name:
            self.profile_name = ''
        self._loading = True
        try:
            self._build_profiles()
        finally:
            self._loading = False

    # -- settings in and out ------------------------------------------------
    def _apply(self, values):
        """Push a settings dict into the controls."""
        values = config.normalise(values)
        self.values = values

        for spec in categories.CATEGORIES:
            row = self._rows[spec.key]
            saved = values['tags'].get(spec.key, {})
            available = self._tag_labels.get(spec.key, [])

            row['include'].IsChecked = bool(
                values['categories'].get(spec.key)) and bool(available)
            row['include'].IsEnabled = bool(available)
            if not available:
                row['include'].ToolTip = (
                    'No {0} family is loaded in this project.'.format(
                        spec.tag_label))

            horizontal = saved.get('horizontal')
            if horizontal in available:
                row['h'].SelectedItem = horizontal
            elif available:
                row['h'].SelectedIndex = 0

            vertical = saved.get('vertical')
            row['v'].SelectedItem = vertical if vertical in available \
                else SAME_AS_RUN

            row['leader'].IsChecked = bool(saved.get('leader', True))
            orientation = saved.get('orientation', 'horizontal')
            index = 0
            for position, (key, _label) in enumerate(ORIENTATION_LABELS):
                if key == orientation:
                    index = position
            row['orient'].SelectedIndex = index

        self.HorizontalTolBox.Text = '{0:g}'.format(
            values['horizontal_tol_deg'])
        self.VerticalTolBox.Text = '{0:g}'.format(values['vertical_tol_deg'])
        self.MinHorizontalBox.Text = '{0:g}'.format(values['min_horizontal_mm'])
        self.MinVerticalBox.Text = '{0:g}'.format(values['min_vertical_mm'])
        self.MinInclinedBox.Text = '{0:g}'.format(values['min_inclined_mm'])
        self.IncludeInclinedCheck.IsChecked = values['include_inclined']
        self.SkipTaggedCheck.IsChecked = values['skip_tagged']
        self.ExtendDepthCheck.IsChecked = values['extend_to_view_depth']
        self.VerifyVisibleCheck.IsChecked = values['verify_visible']

        self.OffsetHorizontalBox.Text = '{0:g}'.format(
            values['offset_horizontal_mm'])
        self.OffsetVerticalBox.Text = '{0:g}'.format(
            values['offset_vertical_mm'])
        self.SpacingBox.Text = '{0:g}'.format(values['spacing_mm'])

        self.SizeFromBox.Text = '' if values['size_from_mm'] is None \
            else '{0:g}'.format(values['size_from_mm'])
        self.SizeToBox.Text = '' if values['size_to_mm'] is None \
            else '{0:g}'.format(values['size_to_mm'])

    def _number(self, box, label, minimum=None, maximum=None,
                allow_blank=False):
        """Parse one numeric field; alert and return False on nonsense."""
        text = (box.Text or '').strip()
        if not text:
            if allow_blank:
                return None
            forms.alert('{0} must be a number.'.format(label),
                        title='Tag Linked Services')
            return False
        try:
            value = float(text)
        except (TypeError, ValueError):
            forms.alert('{0} must be a number.'.format(label),
                        title='Tag Linked Services')
            return False
        if minimum is not None and value < minimum:
            forms.alert('{0} must be at least {1:g}.'.format(label, minimum),
                        title='Tag Linked Services')
            return False
        if maximum is not None and value > maximum:
            forms.alert('{0} must be at most {1:g}.'.format(label, maximum),
                        title='Tag Linked Services')
            return False
        return value

    def _parse(self):
        """Read every control back into a settings dict, or None if invalid."""
        chosen_links = [target.id_value for target in self.selected_targets()]
        if not chosen_links:
            forms.alert('Tick at least one loaded link to read.',
                        title='Tag Linked Services')
            return None

        values = config.defaults()
        values['links'] = chosen_links

        included = []
        for spec in categories.CATEGORIES:
            row = self._rows[spec.key]
            include = bool(row['include'].IsChecked)
            values['categories'][spec.key] = include
            if include:
                included.append(spec)

            horizontal = row['h'].SelectedItem
            vertical = row['v'].SelectedItem
            values['tags'][spec.key] = {
                'horizontal': horizontal if horizontal else None,
                'vertical': None if vertical in (None, SAME_AS_RUN)
                            else vertical,
                'leader': bool(row['leader'].IsChecked),
                'orientation': ORIENTATION_LABELS[
                    max(0, row['orient'].SelectedIndex)][0],
            }

        if not included:
            forms.alert('Tick at least one category to tag.',
                        title='Tag Linked Services')
            return None
        for spec in included:
            if not values['tags'][spec.key]['horizontal']:
                forms.alert(
                    '{0} is included but no tag type is selected.\n\n'
                    'Load a {1} family if the list is empty; tag families '
                    'are never loaded automatically.'.format(
                        spec.label, spec.tag_label),
                    title='Tag Linked Services')
                return None

        numbers = (
            ('horizontal_tol_deg', self.HorizontalTolBox,
             'Horizontal tolerance', 0.0, 89.0),
            ('vertical_tol_deg', self.VerticalTolBox,
             'Vertical tolerance', 1.0, 90.0),
            ('min_horizontal_mm', self.MinHorizontalBox,
             'Minimum horizontal length', 0.0, None),
            ('min_vertical_mm', self.MinVerticalBox,
             'Minimum vertical length', 0.0, None),
            ('min_inclined_mm', self.MinInclinedBox,
             'Minimum inclined length', 0.0, None),
            ('offset_horizontal_mm', self.OffsetHorizontalBox,
             'Horizontal run offset', 0.0, None),
            ('offset_vertical_mm', self.OffsetVerticalBox,
             'Riser offset', 0.0, None),
            ('spacing_mm', self.SpacingBox, 'Minimum clear spacing',
             0.0, None),
        )
        for key, box, label, minimum, maximum in numbers:
            value = self._number(box, label, minimum, maximum)
            if value is False:
                return None
            values[key] = value

        if values['vertical_tol_deg'] <= values['horizontal_tol_deg']:
            forms.alert('The vertical tolerance must be greater than the '
                        'horizontal one, or every run is inclined.',
                        title='Tag Linked Services')
            return None

        size_from = self._number(self.SizeFromBox, 'Size from', 0.0, None,
                                 allow_blank=True)
        if size_from is False:
            return None
        size_to = self._number(self.SizeToBox, 'Size to', 0.0, None,
                               allow_blank=True)
        if size_to is False:
            return None
        if size_from is not None and size_to is not None \
                and size_to < size_from:
            forms.alert('The size range ends below where it starts.',
                        title='Tag Linked Services')
            return None
        values['size_from_mm'] = size_from
        values['size_to_mm'] = size_to

        values['include_inclined'] = bool(self.IncludeInclinedCheck.IsChecked)
        values['skip_tagged'] = bool(self.SkipTaggedCheck.IsChecked)
        values['extend_to_view_depth'] = bool(self.ExtendDepthCheck.IsChecked)
        values['verify_visible'] = bool(self.VerifyVisibleCheck.IsChecked)

        picked = self._read_filters()
        values['types'] = picked['types']
        values['classifications'] = picked['classifications']
        values['system_types'] = picked['system_types']
        values['levels'] = picked['levels']
        values['worksets'] = picked['worksets']

        return values


def show(doc, targets, values, profile_name=''):
    """Show the dialog modally.

    Returns:
        tuple: (action, values, profile name) where action is 'preview',
        'place' or None when the user cancelled.
    """
    dialog = TagLinkedServicesDialog(doc, targets, values, profile_name)
    dialog.show_dialog()
    return dialog.action, dialog.values, dialog.profile_name
