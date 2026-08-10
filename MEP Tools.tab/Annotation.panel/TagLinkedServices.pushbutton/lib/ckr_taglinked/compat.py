# -*- coding: utf-8 -*-
"""Every version-divergent Revit call for Tag Linked Services, in one file.

Brief clause 8.1 asks that API calls which differ between Revit 2022 and
2026 be isolated in a single, clearly named place so future version support
is a localised change. This is that place, and it is also where the unit
conversion of clause 7.2, the file paths of clause 8.5 and the small
geometry adaptors between Revit types and core.py's plain tuples live.

Nothing here makes a decision about tagging; it only makes Revit's API
answer the same question the same way on every supported version.

Known divergences handled below:

    ElementId.Value / .IntegerValue      2024+ / 2022-2023
    UnitUtils + UnitTypeId               present 2021+, guarded anyway
    ViewCropRegionShapeManager
      .GetAnnotationCropShape()          not on every version; the model
                                         crop is the safe fallback because
                                         it is always inside the
                                         annotation crop
    IndependentTag.GetTaggedReferences   2022+, singular form kept as a
                                         fallback
    PlanViewRange sentinel level ids     Unlimited / LevelAbove / LevelBelow
"""

import datetime
import os

from Autodesk.Revit.DB import (
    Element,
    ElementId,
    Level,
    XYZ,
)

# ---------------------------------------------------------------------------
# Paths (clause 8.5 / FR-11.2)
# ---------------------------------------------------------------------------
APP_DIR = os.path.join(os.environ.get('APPDATA', ''), 'CKR',
                       'TagLinkedServices')
LOG_DIR = os.path.join(APP_DIR, 'logs')
PROFILE_DIR = os.path.join(APP_DIR, 'profiles')

LOG_RETENTION_DAYS = 30

MM_PER_FOOT = 304.8


# ---------------------------------------------------------------------------
# Element identity and names
# ---------------------------------------------------------------------------
def id_value(element_id):
    """Return a stable integer for an ElementId across Revit versions.

    Revit 2024+ exposes the Int64 ``Value`` and deprecates
    ``IntegerValue``; 2022 and 2023 only have ``IntegerValue``.
    """
    if element_id is None:
        return None
    try:
        return element_id.Value          # Revit 2024+
    except AttributeError:
        return element_id.IntegerValue   # Revit 2022 / 2023


def is_valid_id(element_id):
    """Return True when an ElementId points at a real element."""
    if element_id is None:
        return False
    try:
        return id_value(element_id) > 0
    except Exception:
        return False


def element_name(element):
    """Return an element's name, tolerant of IronPython property quirks."""
    if element is None:
        return ''
    try:
        return element.Name
    except Exception:
        try:
            return Element.Name.GetValue(element)
        except Exception:
            return ''


def family_name(element_type):
    """Return the family name of a type, falling back to its own name.

    System families - PipeType, DuctType, CableTrayType - carry
    ``FamilyName``; loadable families expose it too, so one accessor
    covers the tag symbols and the MEP curve types alike.
    """
    if element_type is None:
        return ''
    try:
        name = element_type.FamilyName
        if name:
            return name
    except Exception:
        pass
    try:
        family = element_type.Family
        if family is not None:
            return element_name(family)
    except Exception:
        pass
    return element_name(element_type)


# ---------------------------------------------------------------------------
# Units (clause 7.2)
# ---------------------------------------------------------------------------
def mm_to_internal(value_mm):
    """Convert millimetres to internal units through the Revit API.

    Falls back to the fixed 304.8 factor if the unit API is unavailable -
    Revit's internal length unit is decimal feet on every supported
    version, so the fallback is exact rather than approximate.
    """
    try:
        from Autodesk.Revit.DB import UnitTypeId, UnitUtils
        return UnitUtils.ConvertToInternalUnits(float(value_mm),
                                                UnitTypeId.Millimeters)
    except Exception:
        return float(value_mm) / MM_PER_FOOT


def internal_to_mm(value_feet):
    """Convert internal units to millimetres through the Revit API."""
    try:
        from Autodesk.Revit.DB import UnitTypeId, UnitUtils
        return UnitUtils.ConvertFromInternalUnits(float(value_feet),
                                                  UnitTypeId.Millimeters)
    except Exception:
        return float(value_feet) * MM_PER_FOOT


# ---------------------------------------------------------------------------
# Geometry adaptors - Revit types in, plain tuples out
# ---------------------------------------------------------------------------
def to_tuple(point):
    """Return an XYZ as an (x, y, z) tuple for the core layer."""
    return (point.X, point.Y, point.Z)


def to_xyz(point):
    """Return an (x, y, z) tuple as an XYZ for the Revit API."""
    return XYZ(point[0], point[1], point[2])


def transformed_endpoints(curve, transform):
    """Return a curve's endpoints in host coordinates as tuples.

    Clause 7.4: the element's curve is in link coordinates while the view
    range and crop are in host coordinates, so the CURVE is transformed -
    never the view geometry, which breaks for rotated or mirrored links.
    """
    start = curve.GetEndPoint(0)
    end = curve.GetEndPoint(1)
    if transform is not None:
        start = transform.OfPoint(start)
        end = transform.OfPoint(end)
    return to_tuple(start), to_tuple(end)


def bounding_box_corners(bbox, transform=None):
    """Return the eight corners of a BoundingBoxXYZ as host-space tuples.

    The box's own Transform is applied first (Revit hands back boxes in
    their own coordinate system), then the link transform.
    """
    corners = []
    own = None
    try:
        own = bbox.Transform
    except Exception:
        own = None
    for x in (bbox.Min.X, bbox.Max.X):
        for y in (bbox.Min.Y, bbox.Max.Y):
            for z in (bbox.Min.Z, bbox.Max.Z):
                point = XYZ(x, y, z)
                if own is not None:
                    point = own.OfPoint(point)
                if transform is not None:
                    point = transform.OfPoint(point)
                corners.append(to_tuple(point))
    return corners


def curveloop_to_polygon(curve_loop, tessellate=True):
    """Flatten a CurveLoop into a plan polygon of (x, y) vertices.

    Arcs and splines are tessellated so a non-rectangular crop region
    clips correctly (clause 7.4.3). Consecutive duplicates are dropped so
    the polygon has no zero-length edges for the core clipper to divide by.
    """
    points = []
    for curve in curve_loop:
        try:
            if tessellate:
                vertices = list(curve.Tessellate())
            else:
                vertices = [curve.GetEndPoint(0), curve.GetEndPoint(1)]
        except Exception:
            vertices = [curve.GetEndPoint(0), curve.GetEndPoint(1)]
        for vertex in vertices:
            candidate = (vertex.X, vertex.Y)
            if points and abs(points[-1][0] - candidate[0]) < 1e-9 \
                    and abs(points[-1][1] - candidate[1]) < 1e-9:
                continue
            points.append(candidate)
    # The loop closes implicitly; drop a repeated closing vertex.
    if len(points) > 1 and abs(points[0][0] - points[-1][0]) < 1e-9 \
            and abs(points[0][1] - points[-1][1]) < 1e-9:
        points.pop()
    return points


# ---------------------------------------------------------------------------
# Views - crop shapes and the view range
# ---------------------------------------------------------------------------
def crop_loops(view):
    """Return the model crop region as plan polygons, or [] when inactive."""
    try:
        if not view.CropBoxActive:
            return []
        manager = view.GetCropRegionShapeManager()
        return [curveloop_to_polygon(loop) for loop in manager.GetCropShape()]
    except Exception:
        return []


def annotation_crop_loops(view):
    """Return the annotation crop as plan polygons, or [] when there is none.

    Open item 1 of the brief: the accessor is not on every version, and
    the annotation crop can be switched off independently of the model
    crop. Both cases fall back to the MODEL crop, which is safe in the
    only direction that matters - the annotation crop always encloses the
    model crop, so an insertion point clamped into the model crop is
    inside the annotation crop as well.
    """
    try:
        if not view.CropBoxActive:
            return []
        manager = view.GetCropRegionShapeManager()
        active = getattr(manager, 'AnnotationCropActive', None)
        getter = getattr(manager, 'GetAnnotationCropShape', None)
        if active and getter is not None:
            loops = [curveloop_to_polygon(loop) for loop in getter()]
            if loops:
                return loops
    except Exception:
        pass
    return crop_loops(view)


def category_id(doc, built_in_category):
    """Return the ElementId of a BuiltInCategory in a document.

    ``Category.GetCategory`` is the documented route; the ElementId
    constructor taking a BuiltInCategory is the fallback for a category
    the document does not have (it returns an id that simply matches
    nothing).
    """
    try:
        from Autodesk.Revit.DB import Category
        category = Category.GetCategory(doc, built_in_category)
        if category is not None:
            return category.Id
    except Exception:
        pass
    return ElementId(built_in_category)


def level_elevation(level):
    """Return a level's elevation in INTERNAL coordinates.

    ``Level.Elevation`` follows the level type's Elevation Base and can
    read from the survey point on coordinated jobs, while element geometry
    is always internal coordinates - comparing the two silently offsets
    every view-range threshold by the datum. ``ProjectElevation`` is the
    internal-coordinate value, so it is preferred whenever available.
    """
    try:
        return level.ProjectElevation
    except AttributeError:
        return level.Elevation


def _sentinel_ids():
    """Return the PlanViewRange sentinel ids present on this Revit version."""
    sentinels = {}
    try:
        from Autodesk.Revit.DB import PlanViewRange
        for name in ('Unlimited', 'LevelAbove', 'LevelBelow'):
            value = getattr(PlanViewRange, name, None)
            if value is not None:
                sentinels[name] = id_value(value)
    except Exception:
        pass
    return sentinels


def _neighbour_level(doc, base_elevation, above):
    """Return the level immediately above or below an elevation, or None."""
    from Autodesk.Revit.DB import FilteredElementCollector
    best = None
    best_gap = None
    for level in FilteredElementCollector(doc).OfClass(Level):
        elevation = level_elevation(level)
        gap = elevation - base_elevation
        if above and gap <= 1e-6:
            continue
        if not above and gap >= -1e-6:
            continue
        gap = abs(gap)
        if best_gap is None or gap < best_gap:
            best, best_gap = level, gap
    return best


def plane_elevation(doc, view, view_range, plane):
    """Return the host elevation of one view-range plane, or None.

    None means the plane is Unlimited (clause 7.4.1); the caller
    substitutes an infinity and records the condition.
    """
    try:
        level_id = view_range.GetLevelId(plane)
    except Exception:
        return None
    if not is_valid_id(level_id):
        return None

    sentinels = _sentinel_ids()
    value = id_value(level_id)
    level = doc.GetElement(level_id)

    if not isinstance(level, Level):
        if value == sentinels.get('Unlimited'):
            return None
        base = view.GenLevel
        if base is None:
            return None
        if value == sentinels.get('LevelAbove'):
            level = _neighbour_level(doc, level_elevation(base), True)
        elif value == sentinels.get('LevelBelow'):
            level = _neighbour_level(doc, level_elevation(base), False)
        else:
            return None
        if level is None:
            return None

    try:
        offset = view_range.GetOffset(plane)
    except Exception:
        offset = 0.0
    return level_elevation(level) + offset


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------
def create_link_reference(linked_element, link_instance):
    """Return a host-valid Reference to an element inside a link.

    Clause 7.1: this conversion is the supported route for annotating link
    content. A reference built in the link document alone is rejected by
    ``IndependentTag.Create`` in the host.
    """
    from Autodesk.Revit.DB import Reference
    return Reference(linked_element).CreateLinkReference(link_instance)


def create_tag(doc, tag_type_id, view_id, reference, add_leader,
               orientation, point):
    """Create an IndependentTag on a (link) reference.

    Only the Reference-based overload is used; the older TagMode overload
    is deprecated (clause 7.1).
    """
    from Autodesk.Revit.DB import IndependentTag
    return IndependentTag.Create(doc, tag_type_id, view_id, reference,
                                 add_leader, orientation, to_xyz(point))


def tagged_references(tag):
    """Return every Reference a tag points at, across versions."""
    try:
        return list(tag.GetTaggedReferences())
    except AttributeError:
        try:
            return [tag.GetTaggedReference()]
        except Exception:
            return []
    except Exception:
        return []


def tagged_link_pairs(tag):
    """Return (link instance id, linked element id) pairs for a tag.

    The pair is the identity used for duplicate suppression (FR-07): the
    linked element id alone is ambiguous when one link type is placed as
    several instances.
    """
    pairs = []
    for reference in tagged_references(tag):
        try:
            linked_id = reference.LinkedElementId
        except Exception:
            continue
        if not is_valid_id(linked_id):
            continue
        try:
            host_id = reference.ElementId
        except Exception:
            continue
        pairs.append((id_value(host_id), id_value(linked_id)))
    return pairs


def tag_is_orphaned(tag):
    """Return True when a tag's referenced element no longer exists."""
    try:
        return bool(tag.IsOrphaned)
    except Exception:
        # Older behaviour: no referenced element resolves at all.
        return not tagged_references(tag)


def set_tag_head(tag, point):
    """Move a tag head; returns True when Revit accepted the move."""
    try:
        tag.TagHeadPosition = to_xyz(point)
        return True
    except Exception:
        return False


def tag_orientation(name):
    """Return a TagOrientation member from a UI name.

    'Model' in the brief is ``AnyModelDirection`` in the API - the tag
    reads along the element (clause 7.6.7).
    """
    from Autodesk.Revit.DB import TagOrientation
    lookup = {
        'horizontal': TagOrientation.Horizontal,
        'vertical': TagOrientation.Vertical,
        'model': getattr(TagOrientation, 'AnyModelDirection',
                         TagOrientation.Horizontal),
    }
    return lookup.get(str(name).lower(), TagOrientation.Horizontal)


# ---------------------------------------------------------------------------
# Logging (clause 8.5)
# ---------------------------------------------------------------------------
class RollingLog(object):
    """Append-only daily log file under %APPDATA%, pruned after 30 days.

    Writes go straight to disk, one open-append-close per line: the
    logging module has been observed to swallow records inside the pyRevit
    host, and this log is the primary support tool. A log line is never
    worth an error, so every failure is swallowed.
    """

    def __init__(self, directory=LOG_DIR, stem='tag_linked_services'):
        self.directory = directory
        self.stem = stem
        self._pruned = False
        self._header_written = False

    def _path(self):
        stamp = datetime.datetime.now().strftime('%Y-%m-%d')
        return os.path.join(self.directory,
                            '{0}_{1}.log'.format(self.stem, stamp))

    def _prune(self):
        """Delete log files older than the retention period."""
        if self._pruned:
            return
        self._pruned = True
        cutoff = datetime.datetime.now() - datetime.timedelta(
            days=LOG_RETENTION_DAYS)
        try:
            for name in os.listdir(self.directory):
                if not name.startswith(self.stem) or not name.endswith('.log'):
                    continue
                path = os.path.join(self.directory, name)
                stamp = datetime.datetime.fromtimestamp(os.path.getmtime(path))
                if stamp < cutoff:
                    os.remove(path)
        except Exception:
            pass

    def write(self, level, message, *args):
        """Write one timestamped line; never raises."""
        try:
            if args:
                message = message % args
            if not os.path.isdir(self.directory):
                os.makedirs(self.directory)
            self._prune()
            stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            with open(self._path(), 'a') as handle:
                handle.write('{0} {1} {2}\n'.format(stamp, level, message))
        except Exception:
            pass

    def info(self, message, *args):
        self.write('INFO', message, *args)

    def warning(self, message, *args):
        self.write('WARNING', message, *args)

    def error(self, message, *args):
        self.write('ERROR', message, *args)

    def header(self, doc, view, version):
        """Write the run header of clause 8.3.4 once per run."""
        if self._header_written:
            return
        self._header_written = True
        self.info('--- Tag Linked Services %s | host "%s" | view "%s" ---',
                  version, element_name(doc), element_name(view))


_LOG = None


def get_log():
    """Return the shared rolling log."""
    global _LOG
    if _LOG is None:
        _LOG = RollingLog()
    return _LOG


def invalid_id():
    """Return ElementId.InvalidElementId (kept here for import tidiness)."""
    return ElementId.InvalidElementId
