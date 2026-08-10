# -*- coding: utf-8 -*-
"""Shared helpers for the MEP Tag Alignment tool.

Logging, element utilities, unit conversion and view-plane geometry. Nothing
here knows about tags or alignment - it is the lowest layer other modules
build on.
"""

from pyrevit import script

from Autodesk.Revit.DB import (
    LocationCurve,
    LocationPoint,
    Reference,
    XYZ,
)

logger = script.get_logger()

MM_PER_FOOT = 304.8


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
# Units
# ---------------------------------------------------------------------------
def mm_to_feet(value_mm):
    """Convert millimetres to Revit internal units (feet)."""
    try:
        from Autodesk.Revit.DB import UnitUtils, UnitTypeId
        return UnitUtils.ConvertToInternalUnits(value_mm, UnitTypeId.Millimeters)
    except Exception:
        return value_mm / MM_PER_FOOT


def paper_mm_to_model(view, value_mm):
    """Convert a paper-space length (mm) to model units for a view.

    Annotation offsets are authored on paper, so they must be scaled by the
    view scale to land at the right model distance.
    """
    return mm_to_feet(value_mm) * view.Scale


# ---------------------------------------------------------------------------
# Elements
# ---------------------------------------------------------------------------
def get_category_id_value(element):
    """Return the element's category id as an int, or None."""
    try:
        category = element.Category
    except Exception:
        return None
    if category is None:
        return None
    return element_id_value(category.Id)


def get_category_name(element):
    """Return the element's category name, or '<none>'."""
    try:
        if element.Category is not None:
            return element.Category.Name
    except Exception:
        pass
    return '<none>'


def get_element_name(element):
    """Return an element's name.

    IronPython often cannot read ``element.Name`` on a FamilySymbol - the
    instance property collides with the static ``Element.Name`` and throws -
    which silently yields an empty name and breaks any comparison against it.
    Falling back to ``Element.Name.GetValue(element)`` is the reliable route.
    """
    try:
        name = element.Name
        if name:
            return name
    except Exception:
        pass

    try:
        from Autodesk.Revit.DB import Element
        return Element.Name.GetValue(element) or ''
    except Exception:
        return ''


def get_family_name(element_type):
    """Return an ElementType's family name, or '' if unavailable."""
    try:
        return element_type.FamilyName or ''
    except Exception:
        return ''


def get_reference(element):
    """Return a Reference to an element, for tagging."""
    return Reference(element)


def get_element_anchor(element, view):
    """Return the best point to anchor a tag/leader on an element.

    Uses the location point for family instances, the curve midpoint for MEP
    curves (pipes, ducts, trays, conduits), and falls back to the centre of
    the element's bounding box.

    Returns:
        XYZ | None: The anchor point, or None if the element has no geometry.
    """
    try:
        location = element.Location
    except Exception:
        location = None

    if isinstance(location, LocationPoint):
        return location.Point

    if isinstance(location, LocationCurve):
        curve = location.Curve
        if curve is not None:
            # Normalized midpoint works for lines and arcs alike.
            return curve.Evaluate(0.5, True)

    bbox = None
    try:
        bbox = element.get_BoundingBox(view)
        if bbox is None:
            bbox = element.get_BoundingBox(None)
    except Exception:
        bbox = None

    if bbox is not None:
        return XYZ(
            (bbox.Min.X + bbox.Max.X) / 2.0,
            (bbox.Min.Y + bbox.Max.Y) / 2.0,
            (bbox.Min.Z + bbox.Max.Z) / 2.0,
        )
    return None


def get_element_direction(element):
    """Return the unit direction of an element's location curve, or None.

    Computed from the curve endpoints (not curve.Direction) so it works for
    lines and arcs alike. Point-located elements (fittings, equipment) have no
    direction and return None - the caller then treats them as non-linear.
    """
    try:
        location = element.Location
        curve = location.Curve if isinstance(location, LocationCurve) else None
        if curve is None:
            return None
        vector = curve.GetEndPoint(1).Subtract(curve.GetEndPoint(0))
        return vector.Normalize() if vector.GetLength() > 1e-9 else None
    except Exception:
        return None


def get_curve_span(element, axis):
    """Return (low, high) of an element's curve endpoints along a view axis.

    Used to keep a leader's turn-down inside the pipe it points at, so an
    attached arrow can't be pushed past the pipe's end.

    Returns:
        tuple | None: (low, high) in feet, or None if the element has no curve.
    """
    try:
        location = element.Location
        curve = location.Curve if isinstance(location, LocationCurve) else None
        if curve is None:
            return None
        start = project(curve.GetEndPoint(0), axis)
        end = project(curve.GetEndPoint(1), axis)
        return (min(start, end), max(start, end))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# View-plane geometry
# ---------------------------------------------------------------------------
def get_view_axes(view):
    """Return the (right, up) unit vectors of a view's plane.

    Alignment is computed in these axes rather than world X/Y, so "left" and
    "top" mean what the user sees in a plan, a section or an elevation.
    """
    return view.RightDirection.Normalize(), view.UpDirection.Normalize()


def project(point, axis):
    """Return the signed coordinate of a point along a unit axis."""
    return point.DotProduct(axis)


def shift(point, axis, distance):
    """Return the point moved by `distance` along a unit axis."""
    return point.Add(axis.Multiply(distance))


def project_bounds(element, view, axis):
    """Return (low, high) of an element's extents along a view axis, in feet.

    Every corner of the bounding box is projected onto the axis, so the result
    is correct whatever the view's orientation.

    Beware: for a tag, Revit's bounding box includes the *leader*. Callers that
    want the tag text alone must suppress the leader first - see
    alignment._measure_head_bounds().

    Returns:
        tuple | None: (low, high), or None if Revit reports no bounding box.
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
                coords.append(project(corner, axis))

    if not coords:
        return None
    return min(coords), max(coords)
