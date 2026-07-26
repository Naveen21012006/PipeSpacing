# -*- coding: utf-8 -*-
"""Shared low-level helpers for the Align Tags tool.

Version compatibility, units, view-plane 2D projection and file logging.
Nothing here knows about tags or alignment. Patterned after the Auto Tag
bundle's utils.py, trimmed to what this tool needs.

File logging goes to %APPDATA%/CKR/logs so failures inside Revit leave a
trail even when the pyRevit output window is closed.
"""

import datetime
import os

from pyrevit import script

from Autodesk.Revit.DB import XYZ

logger = script.get_logger()

MM_PER_FOOT = 304.8

CKR_DIR = os.path.join(os.environ.get('APPDATA', ''), 'CKR')
LOG_DIR = os.path.join(CKR_DIR, 'logs')


# ---------------------------------------------------------------------------
# Version compatibility
# ---------------------------------------------------------------------------
def element_id_value(element_id):
    """Return a stable integer for an ElementId across Revit versions.

    Revit 2024+ exposes the Int64 ``Value`` and deprecates ``IntegerValue``;
    2022/2023 only have ``IntegerValue``.
    """
    try:
        return element_id.Value          # Revit 2024+
    except AttributeError:
        return element_id.IntegerValue   # Revit 2022 / 2023


# ---------------------------------------------------------------------------
# File logging
# ---------------------------------------------------------------------------
class _FileLog(object):
    """Writes log lines straight to disk, bypassing Python's logging module.

    The logging-module route left a 0-byte file across dozens of sessions -
    something in the pyRevit host suppresses those records. Direct
    append-and-close per line cannot be silenced and survives engine
    teardown. A log line is never worth an error: every failure is
    swallowed after a debug note.
    """

    def __init__(self, path):
        self.path = path

    def _write(self, level, message, *args):
        try:
            if args:
                message = message % args
            if not os.path.isdir(LOG_DIR):
                os.makedirs(LOG_DIR)
            stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            with open(self.path, 'a') as handle:
                handle.write('{0} {1} {2}\n'.format(stamp, level, message))
        except Exception as ex:
            logger.debug('File log write failed: {}'.format(ex))

    def info(self, message, *args):
        self._write('INFO', message, *args)

    def warning(self, message, *args):
        self._write('WARNING', message, *args)

    def error(self, message, *args):
        self._write('ERROR', message, *args)


_FILE_LOG = None


def get_file_logger():
    """Return the shared direct-write logger for %APPDATA%/CKR/logs."""
    global _FILE_LOG
    if _FILE_LOG is None:
        _FILE_LOG = _FileLog(os.path.join(LOG_DIR, 'align_tags.log'))
    return _FILE_LOG


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------
def mm_to_feet(value_mm):
    """Convert millimetres to Revit internal units (feet)."""
    return float(value_mm) / MM_PER_FOOT


def feet_to_mm(value_feet):
    """Convert Revit internal units (feet) to millimetres."""
    return float(value_feet) * MM_PER_FOOT


def _length_unit_id(doc):
    """Return the document's display unit ForgeTypeId for lengths, or None."""
    try:
        from Autodesk.Revit.DB import SpecTypeId
        options = doc.GetUnits().GetFormatOptions(SpecTypeId.Length)
        return options.GetUnitTypeId()
    except Exception:
        return None


def display_from_mm(doc, value_mm):
    """Convert a stored mm value to the document's display length unit."""
    unit = _length_unit_id(doc)
    if unit is not None:
        try:
            from Autodesk.Revit.DB import UnitUtils
            return UnitUtils.ConvertFromInternalUnits(
                mm_to_feet(value_mm), unit)
        except Exception:
            pass
    return float(value_mm)


def mm_from_display(doc, value):
    """Convert a value typed in the document's display unit back to mm."""
    unit = _length_unit_id(doc)
    if unit is not None:
        try:
            from Autodesk.Revit.DB import UnitUtils
            return feet_to_mm(
                UnitUtils.ConvertToInternalUnits(float(value), unit))
        except Exception:
            pass
    return float(value)


def length_unit_label(doc):
    """Return a short label for the document's length unit (e.g. 'mm').

    Falls back to 'mm' when the label API is unavailable - the values shown
    are still in the document unit, only the caption degrades.
    """
    unit = _length_unit_id(doc)
    if unit is not None:
        try:
            from Autodesk.Revit.DB import LabelUtils
            return LabelUtils.GetLabelForUnit(unit)
        except Exception:
            pass
    return 'mm'


# ---------------------------------------------------------------------------
# View-plane 2D projection
# ---------------------------------------------------------------------------
def view_basis(view):
    """Return the view's orthonormal (right, up, direction) unit vectors.

    Alignment is computed in these axes rather than world X/Y, so the tool
    behaves identically in plans, sections and elevations.
    """
    return (view.RightDirection.Normalize(),
            view.UpDirection.Normalize(),
            view.ViewDirection.Normalize())


def to_2d(point, basis):
    """Project an XYZ onto the view plane -> (u, v) tuple."""
    right, up, _ = basis
    return (point.DotProduct(right), point.DotProduct(up))


def depth_of(point, basis):
    """Return the point's coordinate along the view direction.

    Kept per element so reconstruction preserves each annotation's original
    depth in the view (matters in sections/elevations).
    """
    return point.DotProduct(basis[2])


def extent_along(element, view, axis):
    """Return (low, high) of an element's bounding box along a view axis.

    Every corner is projected, so the result is correct in rotated views.
    Returns None when Revit reports no bounding box. For tags, the box
    includes the leader - suppress it first when measuring the text alone.
    """
    try:
        bbox = element.get_BoundingBox(view)
    except Exception:
        bbox = None
    if bbox is None:
        return None

    transform = bbox.Transform
    coords = []
    for x in (bbox.Min.X, bbox.Max.X):
        for y in (bbox.Min.Y, bbox.Max.Y):
            for z in (bbox.Min.Z, bbox.Max.Z):
                corner = XYZ(x, y, z)
                if transform is not None:
                    corner = transform.OfPoint(corner)
                coords.append(corner.DotProduct(axis))
    if not coords:
        return None
    return min(coords), max(coords)


def to_3d(uv, depth, basis):
    """Lift a view-plane (u, v) back to a world XYZ at the given depth.

    right/up/direction form an orthonormal world basis, so
    ``P = right*u + up*v + direction*depth`` reconstructs exactly.
    """
    right, up, direction = basis
    return XYZ(
        right.X * uv[0] + up.X * uv[1] + direction.X * depth,
        right.Y * uv[0] + up.Y * uv[1] + direction.Y * depth,
        right.Z * uv[0] + up.Z * uv[1] + direction.Z * depth,
    )
