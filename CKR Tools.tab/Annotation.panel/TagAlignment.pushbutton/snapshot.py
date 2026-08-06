# -*- coding: utf-8 -*-
"""View capture and ink map for the Auto Tag method.

Two jobs, both about SEEING the drawing instead of only modelling it:

* ``save_after`` - after every run, export the tagged region to
  %APPDATA%/CKR/logs/autotag.png, next to the log. The reviewer (human or
  Claude reading this machine) sees the actual drawn result without
  screenshots - the same pattern Align Tags uses (align_check.png).

* ``capture_ink_map`` - BEFORE placing, export the view with the pipe-tag
  categories temporarily hidden and rasterise it into an :class:`InkGrid`:
  where the paper already has ink (dimensions, text, walls, unselected
  pipework - everything the geometric model is blind to). Row scoring then
  prefers blank paper, so tags spread into genuinely empty areas and stay
  off annotation the layout has never modelled. Hiding our own tag
  categories keeps re-runs stable: a tag must not avoid its own previous
  position.

Everything here is defensive: any failure returns None / does nothing, and
the run continues without vision. The pixel mapping relies on
UIView.GetZoomCorners(), whose two model-space corners correspond to the
exported image's corners; projecting them onto the view's right/up axes
makes the mapping valid for rotated plans too.
"""

import os

import utils

_DARK = 200          # 0-255 luminance below this counts as ink
_STRIDE = 2          # sample every Nth pixel - plenty for an obstacle map


def log_dir():
    """Return %APPDATA%/CKR/logs, creating it if needed."""
    base = os.environ.get('APPDATA') or os.environ.get('LOCALAPPDATA') or ''
    path = os.path.join(base, 'CKR', 'logs')
    try:
        if not os.path.isdir(path):
            os.makedirs(path)
    except Exception:
        pass
    return path


def _export_view_png(doc, base_name, pixels):
    """Export the current view's visible region; return the png path or None.

    Revit decorates the file name it writes, so the export goes to a unique
    base and the newest matching file is renamed onto the exact target.
    """
    try:
        from Autodesk.Revit.DB import (ExportRange, ImageExportOptions,
                                       ImageFileType, ZoomFitType)
        directory = log_dir()
        target = os.path.join(directory, base_name + '.png')
        scratch = os.path.join(directory, base_name + '_raw')

        for name in os.listdir(directory):
            if name.startswith(base_name + '_raw'):
                try:
                    os.remove(os.path.join(directory, name))
                except Exception:
                    pass

        options = ImageExportOptions()
        options.ExportRange = ExportRange.VisibleRegionOfCurrentView
        options.FilePath = scratch
        options.HLRandWFViewsFileType = ImageFileType.PNG
        options.ShadowViewsFileType = ImageFileType.PNG
        options.ZoomType = ZoomFitType.FitToPage
        options.PixelSize = pixels
        doc.ExportImage(options)

        produced = [os.path.join(directory, name)
                    for name in os.listdir(directory)
                    if name.startswith(base_name + '_raw')
                    and name.endswith('.png')]
        if not produced:
            return None
        produced.sort(key=lambda path: os.path.getmtime(path))
        if os.path.exists(target):
            os.remove(target)
        os.rename(produced[-1], target)
        for stale in produced[:-1]:
            try:
                os.remove(stale)
            except Exception:
                pass
        return target
    except Exception as ex:
        utils.logger.debug('View export failed: {0}'.format(ex))
        return None


def save_after(doc):
    """Export the run's result next to the log (autotag.png). Never fatal."""
    return _export_view_png(doc, 'autotag', 2000)


def _zoom_rect(uidoc, view, right, up):
    """The visible region as (u_lo, u_hi, v_lo, v_hi) in view axes, or None."""
    try:
        for uiview in uidoc.GetOpenUIViews():
            if uiview.ViewId == view.Id:
                corners = list(uiview.GetZoomCorners())
                us = [utils.project(c, right) for c in corners]
                vs = [utils.project(c, up) for c in corners]
                return min(us), max(us), min(vs), max(vs)
    except Exception as ex:
        utils.logger.debug('Zoom corners unavailable: {0}'.format(ex))
    return None


class InkGrid(object):
    """A coarse boolean map of where the exported view already has ink.

    Pure data + arithmetic so it is testable without Revit; only the
    ``from_png`` constructor touches System.Drawing.
    """

    def __init__(self, cells, columns, rows, u_lo, u_hi, v_lo, v_hi):
        self.cells = cells          # flat list, row-major, True = ink
        self.columns = columns
        self.rows = rows
        self.u_lo, self.u_hi = u_lo, u_hi
        self.v_lo, self.v_hi = v_lo, v_hi

    @classmethod
    def from_png(cls, path, rect):
        """Rasterise an exported view into a sampled grid. None on failure."""
        try:
            import clr
            clr.AddReference('System.Drawing')
            from System.Drawing import Bitmap
            u_lo, u_hi, v_lo, v_hi = rect
            bitmap = Bitmap(path)
            try:
                width, height = bitmap.Width, bitmap.Height
                columns = max(1, width // _STRIDE)
                rows = max(1, height // _STRIDE)
                cells = []
                for row in range(rows):
                    y = min(row * _STRIDE, height - 1)
                    for column in range(columns):
                        x = min(column * _STRIDE, width - 1)
                        pixel = bitmap.GetPixel(x, y)
                        luminance = (pixel.R * 299 + pixel.G * 587
                                     + pixel.B * 114) / 1000
                        cells.append(luminance < _DARK)
                return cls(cells, columns, rows, u_lo, u_hi, v_lo, v_hi)
            finally:
                bitmap.Dispose()
        except Exception as ex:
            utils.logger.debug('Ink map rasterise failed: {0}'.format(ex))
            return None

    def ink_fraction(self, u_lo, u_hi, v_lo, v_hi):
        """Fraction of ink in a view-axis rectangle (0.0 clean .. 1.0 solid).

        A rectangle outside the captured region reads as clean - the map
        only ever ADDS a penalty, never blocks.
        """
        if self.u_hi <= self.u_lo or self.v_hi <= self.v_lo:
            return 0.0
        # view v grows UP; image rows grow DOWN.
        c_lo = int((u_lo - self.u_lo) / (self.u_hi - self.u_lo)
                   * self.columns)
        c_hi = int((u_hi - self.u_lo) / (self.u_hi - self.u_lo)
                   * self.columns)
        r_lo = int((self.v_hi - v_hi) / (self.v_hi - self.v_lo) * self.rows)
        r_hi = int((self.v_hi - v_lo) / (self.v_hi - self.v_lo) * self.rows)
        c_lo, c_hi = max(0, c_lo), min(self.columns - 1, c_hi)
        r_lo, r_hi = max(0, r_lo), min(self.rows - 1, r_hi)
        if c_lo > c_hi or r_lo > r_hi:
            return 0.0
        ink = 0
        total = 0
        for row in range(r_lo, r_hi + 1):
            base = row * self.columns
            for column in range(c_lo, c_hi + 1):
                total += 1
                if self.cells[base + column]:
                    ink += 1
        return float(ink) / total if total else 0.0


def capture_ink_map(uidoc, doc, view, hide_categories):
    """Capture the pre-placement ink map, our tag categories hidden.

    The hide happens inside a transaction that is ROLLED BACK, so nothing
    about the document changes; the export just happens while the tags are
    invisible. Returns an InkGrid or None (the layout then runs blind, as
    before - vision is an upgrade, never a dependency).
    """
    rect = None
    try:
        right, up = utils.get_view_axes(view)
        rect = _zoom_rect(uidoc, view, right, up)
    except Exception:
        rect = None
    if rect is None:
        return None

    path = None
    try:
        from Autodesk.Revit.DB import ElementId, Transaction
        transaction = Transaction(doc, 'Auto Tag ink capture')
        transaction.Start()
        try:
            for built_in in hide_categories:
                try:
                    view.SetCategoryHidden(ElementId(built_in), True)
                except Exception:
                    pass
            doc.Regenerate()
            path = _export_view_png(doc, 'autotag_before', 1600)
        finally:
            transaction.RollBack()
    except Exception as ex:
        utils.logger.debug('Ink capture failed: {0}'.format(ex))
        return None

    if path is None:
        return None
    return InkGrid.from_png(path, rect)
