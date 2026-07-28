# -*- coding: utf-8 -*-
"""Annotation Dashboard - modeless leader control (spec Feature 3).

An always-on-top palette that applies the Align Tags leader rules without
picking anything:

    Process Visible   every supported tag in the active view (or the
                      current selection, when there is one)
    Dynamic Tagging   the same rules applied automatically to each newly
                      placed tag, via Application.DocumentChanged

Modeless windows may not touch the document directly, so every write is
funnelled through an ExternalEvent handler (_RuleHandler). The
DocumentChanged subscriber only RECORDS ids and raises that event - it
never edits inside the notification, and a re-entrancy guard ignores the
changes the handler itself makes.

Shares AlignTags' modules (engine, wrappers, common, settings) and its
settings file, so both tools stay consistent.

REQUIRES A PERSISTENT ENGINE (see __persistentengine__ below). pyRevit
disposes a command's IronPython engine as soon as the command returns.
A modeless window outlives its command, so its handlers - which are
Python delegates - would be calling into a dead engine: Revit dies with
an unrecoverable 0xe0434352 the moment anything is clicked, and no
try/except can catch it because the failure is below the Python frame.

Author: Naveen
Target: Revit 2022-2026 / pyRevit / IronPython
"""

import os
import sys
import traceback

# Keep this command's engine alive after it returns - without it the
# palette crashes Revit on the first click (field-confirmed 2026-07-28).
__persistentengine__ = True

_BUNDLE_DIR = os.path.dirname(__file__)
_ALIGN_DIR = os.path.join(os.path.dirname(_BUNDLE_DIR),
                          'AlignTags.pushbutton')
for _path in (_BUNDLE_DIR, _ALIGN_DIR):
    if _path not in sys.path:
        sys.path.append(_path)

from pyrevit import forms, revit, script

from Autodesk.Revit.DB import ElementId, Transaction, TransactionStatus
from Autodesk.Revit.UI import ExternalEvent, IExternalEventHandler

import common
import engine
import leader_rules
import settings
import wrappers

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()
file_log = common.get_file_logger()

TITLE = 'Annotation Dashboard'
_JUSTIFICATIONS = ('unchanged', 'left', 'right', 'automatic')


def _guarded(method):
    """Keep a WPF callback from throwing into Revit's message loop.

    The palette is modeless, so by the time the user clicks anything the
    script's own try/except is long gone: every handler needs its own.
    """
    def wrapper(self, sender, args):
        try:
            return method(self, sender, args)
        except Exception:
            details = traceback.format_exc()
            file_log.error('Dashboard callback %s failed:\n%s',
                           getattr(method, '__name__', '?'), details)
            try:
                self.set_status('Something went wrong - see the CKR log.')
            except Exception:
                pass
    wrapper.__name__ = getattr(method, '__name__', 'wrapper')
    return wrapper


def _same_document(left, right):
    """True when both handles point at the same open document."""
    if left is None or right is None:
        return False
    try:
        return bool(left.Equals(right))
    except Exception:
        return left is right


# ---------------------------------------------------------------------------
# External event plumbing
# ---------------------------------------------------------------------------
class _RuleHandler(IExternalEventHandler):
    """Applies leader rules inside a proper Revit API context.

    The window (or the dynamic watcher) queues a request and raises the
    event; Revit calls Execute() when it is safe to write. Raise() only
    promises *one* Execute for everything queued so far, so requests are
    accumulated rather than overwritten - two tags placed in quick
    succession must not lose the first one.
    """

    def __init__(self):
        self.options = {}
        self.window = None
        self.busy = False        # re-entrancy guard for DocumentChanged
        self._visible = False    # a Process Visible click is waiting
        self._ids = []           # tag ids queued by dynamic tagging
        self._ids_doc = None     # the document those ids belong to

    # -- queueing (called from the UI / event thread) --------------------
    def request_visible(self):
        self._visible = True

    def queue_ids(self, document, id_values):
        if not _same_document(self._ids_doc, document):
            self._ids = []       # never mix ids from two documents
            self._ids_doc = document
        seen = set(self._ids)
        for value in id_values:
            if value not in seen:
                seen.add(value)
                self._ids.append(value)

    def _take_request(self):
        """Claim everything queued so far and reset for the next round."""
        visible, ids, ids_doc = self._visible, self._ids, self._ids_doc
        self._visible, self._ids, self._ids_doc = False, [], None
        return visible, ids, ids_doc

    # pylint: disable=invalid-name
    def Execute(self, app):
        self.busy = True
        try:
            self._run(app)
        except Exception:
            details = traceback.format_exc()
            file_log.error('Dashboard handler failed:\n%s', details)
            self._status('Something went wrong - see the CKR log.')
        finally:
            self.busy = False

    def _run(self, app):
        uidoc = app.ActiveUIDocument
        if uidoc is None:
            return
        document = uidoc.Document
        view = document.ActiveView
        basis = common.view_basis(view)
        visible, ids, ids_doc = self._take_request()

        if visible:
            targets = self._selection_or_visible(app, document, view)
            label = 'view'
        elif not ids:
            return
        elif not _same_document(ids_doc, document):
            # The user moved to another document between the tag being
            # placed and Revit dispatching us; those ids mean nothing here.
            file_log.info('Dashboard: dropped %s queued tag(s) from '
                          'another document.', len(ids))
            return
        else:
            targets = self._wrap_ids(document, ids)
            label = 'new tag(s)'

        if not targets:
            self._status('Nothing to process.')
            return

        txn = Transaction(document, TITLE)
        txn.Start()
        try:
            updated, failures = leader_rules.apply_rules(
                targets, document, basis, self.options)
            status = txn.Commit()
        except Exception:
            if txn.HasStarted():
                txn.RollBack()
            raise

        if status != TransactionStatus.Committed:
            # Revit resolved a failure by rolling us back: nothing landed.
            file_log.error('Dashboard: transaction ended as %s.', status)
            self._status('Revit rolled the change back - nothing was '
                         'updated.')
            return

        message = 'Updated {0} leader(s) in the {1}.'.format(updated, label)
        if failures:
            message += ' {0} could not be set.'.format(failures)
        self._status(message)
        file_log.info('Dashboard: %s updated, %s failed (%s).',
                      updated, failures, label)

    def _wrap_ids(self, document, id_values):
        targets = []
        for id_value in id_values:
            element = document.GetElement(ElementId(id_value))
            wrapper = wrappers.wrap(element, document) \
                if element is not None else None
            if wrapper is None:
                continue
            try:
                if not wrapper.has_leader or wrapper.is_pinned:
                    continue
            except Exception:
                continue
            targets.append(wrapper)
        return targets

    def GetName(self):
        return 'CKR Annotation Dashboard'

    def _selection_or_visible(self, app, document, view):
        """Selection when there is one, else everything in the view."""
        try:
            ids = list(app.ActiveUIDocument.Selection.GetElementIds())
        except Exception:
            ids = []
        if ids:
            targets = []
            for element_id in ids:
                element = document.GetElement(element_id)
                wrapper = wrappers.wrap(element, document) \
                    if element is not None else None
                if wrapper is None:
                    continue
                try:
                    if not wrapper.has_leader or wrapper.is_pinned:
                        continue
                except Exception:
                    continue
                targets.append(wrapper)
            if targets:
                return targets
        return leader_rules.collect_visible(document, view)

    def _status(self, message):
        window = self.window
        if window is not None:
            try:
                window.set_status(message)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Dynamic tagging
# ---------------------------------------------------------------------------
class _DynamicWatcher(object):
    """Applies the rules to newly added tags, via DocumentChanged.

    Only records ids and raises the external event: editing inside the
    notification is illegal, and the handler's own edits are ignored
    through the busy guard.
    """

    def __init__(self, handler, event):
        self.handler = handler
        self.event = event
        self.subscribed = False
        self._app = None

    def start(self, uiapp):
        if self.subscribed:
            return
        try:
            self._app = uiapp.Application
            self._app.DocumentChanged += self._on_changed
            self.subscribed = True
            file_log.info('Dashboard: dynamic tagging ON.')
        except Exception as ex:
            common.logger.debug('Dynamic subscribe failed: {}'.format(ex))

    def stop(self):
        if not self.subscribed:
            return
        try:
            self._app.DocumentChanged -= self._on_changed
        except Exception as ex:
            common.logger.debug('Dynamic unsubscribe failed: {}'.format(ex))
        self.subscribed = False
        file_log.info('Dashboard: dynamic tagging OFF.')

    # pylint: disable=invalid-name
    def _on_changed(self, sender, args):
        if self.handler.busy:
            return               # our own edits: never react to them
        try:
            document = args.GetDocument()
            added = []
            for element_id in args.GetAddedElementIds():
                element = document.GetElement(element_id)
                if element is None or not wrappers.is_supported(element):
                    continue
                added.append(common.element_id_value(element_id))
            if not added:
                return
            self.handler.queue_ids(document, added)
            self.event.Raise()
        except Exception as ex:
            common.logger.debug('DocumentChanged failed: {}'.format(ex))


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
# A persistent engine re-runs this script on every button press, so the
# handler/event/watcher must be built ONCE and reused. Module globals are
# rebound on each run, which would leak an ExternalEvent per press and
# orphan the previous DocumentChanged hook - the old palette would then
# stop the wrong watcher on close and keep rewriting tags forever.
# pyRevit's env vars live in the AppDomain, so they outlive both.
_ENV_KEY = 'CKRAnnotationDashboard'


def _session():
    """The one handler/event/watcher trio for this Revit session."""
    state = script.get_envvar(_ENV_KEY)
    if state is None:
        handler = _RuleHandler()
        event = ExternalEvent.Create(handler)
        state = {'handler': handler,
                 'event': event,
                 'watcher': _DynamicWatcher(handler, event),
                 'window': None}
        script.set_envvar(_ENV_KEY, state)
    return state


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------
class DashboardWindow(forms.WPFWindow):
    """The modeless palette. Owns no document logic - only settings."""

    def __init__(self, state):
        xaml = os.path.join(_BUNDLE_DIR, 'DashboardWindow.xaml')
        forms.WPFWindow.__init__(self, xaml)
        # Held per instance, never read from module globals: a second run
        # of this script rebinds those, and this window must keep talking
        # to the trio it was built with.
        self._state = state
        self._handler = state['handler']
        self._event = state['event']
        self._watcher = state['watcher']
        self._syncing = False
        self._load(settings.load())
        self._wire()
        self._handler.window = self
        self.push_options()

    # -- setup ----------------------------------------------------------
    def _load(self, values):
        unit = common.length_unit_label(doc)
        self.LandingUnit.Text = unit
        self.ElbowUnit.Text = unit
        angle = values.get('dash_angle_deg', 0.0)
        self.AngleSlider.Value = float(angle)
        self.AngleBox.Text = '{0:g}'.format(round(angle, 1))
        self.LandingBox.Text = self._format_mm(
            values.get('dash_landing_mm', 1524.0))
        self.ElbowBox.Text = self._format_mm(
            values.get('dash_elbow_mm', 500.0))
        self.StraightCheck.IsChecked = values.get('dash_straight', True)
        self.AttachedCheck.IsChecked = values.get('dash_attached_end',
                                                  False)
        self.DynamicCheck.IsChecked = False   # never auto-arm on open
        just = values.get('dash_justification', 'unchanged')
        self.JustificationCombo.SelectedIndex = \
            _JUSTIFICATIONS.index(just) if just in _JUSTIFICATIONS else 0

    def _format_mm(self, value_mm):
        return '{0:g}'.format(round(common.display_from_mm(doc, value_mm), 4))

    def _wire(self):
        self.ProcessButton.Click += self._on_process
        self.HelpButton.Click += self._on_help
        self.MinimiseButton.Click += self._on_minimise
        self.CloseButton.Click += self._on_close
        self.DynamicCheck.Checked += self._on_dynamic
        self.DynamicCheck.Unchecked += self._on_dynamic
        self.AngleSlider.ValueChanged += self._on_slider
        self.AngleBox.TextChanged += self._on_angle_text
        self.Closed += self._on_closed

    # -- settings -------------------------------------------------------
    def _mm(self, box, fallback):
        try:
            return common.mm_from_display(doc, float(box.Text))
        except (TypeError, ValueError):
            return fallback

    def push_options(self):
        """Copy the current UI values onto the shared handler + disk."""
        straight = bool(self.StraightCheck.IsChecked)
        try:
            angle = float(self.AngleBox.Text)
        except (TypeError, ValueError):
            angle = 0.0
        angle = engine.normalize_angle(angle)
        landing_mm = self._mm(self.LandingBox, 1524.0)
        elbow_mm = self._mm(self.ElbowBox, 500.0)
        just = _JUSTIFICATIONS[max(0, self.JustificationCombo.SelectedIndex)]

        self._handler.options = {
            'straight': straight or angle == 0.0,
            'angle_deg': angle,
            'landing': common.mm_to_feet(landing_mm),
            'elbow_gap': common.mm_to_feet(elbow_mm),
            'attached_end': bool(self.AttachedCheck.IsChecked),
            # 'automatic' is resolved per tag in leader_rules, from the
            # side its own element sits on.
            'justification': None if just == 'unchanged' else just,
        }

        values = settings.load()
        values.update({
            'dash_angle_deg': angle,
            'dash_landing_mm': landing_mm,
            'dash_elbow_mm': elbow_mm,
            'dash_straight': straight,
            'dash_attached_end': bool(self.AttachedCheck.IsChecked),
            'dash_dynamic': bool(self.DynamicCheck.IsChecked),
            'dash_justification': just,
        })
        settings.save(values)

    def set_status(self, message):
        try:
            self.StatusText.Text = message
        except Exception:
            pass

    # -- events ---------------------------------------------------------
    @_guarded
    def _on_slider(self, _sender, _args):
        if self._syncing:
            return
        self._syncing = True
        try:
            self.AngleBox.Text = '{0:g}'.format(round(self.AngleSlider.Value))
        finally:
            self._syncing = False

    @_guarded
    def _on_angle_text(self, _sender, _args):
        if self._syncing:
            return
        self._syncing = True
        try:
            self.AngleSlider.Value = max(0.0, min(90.0,
                                                  float(self.AngleBox.Text)))
        except (TypeError, ValueError):
            pass
        finally:
            self._syncing = False

    @_guarded
    def _on_process(self, _sender, _args):
        self.push_options()
        self._handler.request_visible()
        self.set_status('Processing...')
        self._event.Raise()

    @_guarded
    def _on_dynamic(self, _sender, _args):
        self.push_options()
        if self.DynamicCheck.IsChecked:
            self._watcher.start(revit.uiapp)
            self.set_status('Dynamic tagging ON - new tags will follow '
                            'these rules.')
        else:
            self._watcher.stop()
            self.set_status('Dynamic tagging off.')

    @_guarded
    def _on_help(self, _sender, _args):
        path = os.path.join(_ALIGN_DIR, 'help.html')
        try:
            import webbrowser
            if os.path.exists(path):
                webbrowser.open('file:///' + path.replace('\\', '/'))
                return
        except Exception as ex:
            common.logger.debug('Help failed: {}'.format(ex))
        forms.alert('Process Visible applies the leader rules to the '
                    'active view (or your selection). Dynamic Tagging '
                    'applies them to each new tag as you place it. Tags '
                    'are never moved - use Align Tags for stacks.',
                    title=TITLE)

    @_guarded
    def _on_minimise(self, _sender, _args):
        from System.Windows import WindowState
        self.WindowState = WindowState.Minimized

    @_guarded
    def _on_close(self, _sender, _args):
        self.Close()

    @_guarded
    def _on_closed(self, _sender, _args):
        self._watcher.stop()     # never outlive the palette
        self._handler.window = None
        self._state['window'] = None


def _reveal(window):
    """Bring an already-open palette forward. False if it has gone."""
    try:
        if not window.IsLoaded:
            return False
        from System.Windows import WindowState
        if window.WindowState == WindowState.Minimized:
            window.WindowState = WindowState.Normal
        window.Activate()
        return True
    except Exception:
        return False


def main():
    if doc is None or uidoc is None:
        forms.alert('Open a project document first.', title=TITLE)
        return

    state = _session()
    # One palette per session: a second press restores the open one
    # rather than stacking another (which would double-tag every new tag).
    if state['window'] is not None and _reveal(state['window']):
        return

    window = DashboardWindow(state)
    state['window'] = window
    window.show()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        details = traceback.format_exc()
        file_log.error('Dashboard failed to open:\n%s', details)
        forms.alert('The Annotation Dashboard could not open. Details '
                    'were logged to {0}.'.format(common.LOG_DIR),
                    title=TITLE)
