# -*- coding: utf-8 -*-
"""Element wrappers for the Align Tags tool.

One small class per annotation family hides the API differences between
IndependentTag (element/material/keynote/multi-category tags), the spatial
tags (Room/Space/Area) and TextNote, so the command and the geometry engine
only ever see:

    head          get_head() / set_head(xyz)
    leaders       leader_keys() -> opaque keys, get_end(key),
                  set_elbow(key, xyz), set_end(key, xyz)
    state         has_leader, attached_end, is_pinned, id_value, kind

Revit version notes (supported range 2022-2026):
  * IndependentTag leaders are addressed per tagged Reference
    (GetTaggedReferences / SetLeaderElbow(ref, pt) / SetLeaderEnd(ref, pt)).
    The parameterless LeaderElbow/LeaderEnd properties were REMOVED in 2023,
    so they are only touched behind hasattr as a 2022 safety net - the same
    pattern the Auto Tag bundle ships and has proven in production.
  * GetLeaderEnd(ref) THROWS while the leader end condition is Attached; the
    end of an attached leader is derived from the tagged element instead.
  * TextNote leaders are index-addressed and the Leader objects go stale
    after the note moves, so every operation re-fetches GetLeaders().

Setters return True/False instead of raising: cosmetic leader work must
never abort the transaction, and the caller reports honest counts.
"""

from Autodesk.Revit.DB import (
    ElementId,
    IndependentTag,
    LeaderEndCondition,
    LocationCurve,
    LocationPoint,
    SpatialElementTag,
    TextNote,
    XYZ,
)

import common


def _element_anchor(element, view=None):
    """Best-effort anchor point of an element (for attached leader ends)."""
    if element is None:
        return None
    try:
        location = element.Location
    except Exception:
        location = None
    if isinstance(location, LocationPoint):
        return location.Point
    if isinstance(location, LocationCurve):
        curve = location.Curve
        if curve is not None:
            return curve.Evaluate(0.5, True)
    try:
        bbox = element.get_BoundingBox(view) or element.get_BoundingBox(None)
    except Exception:
        bbox = None
    if bbox is not None:
        return XYZ((bbox.Min.X + bbox.Max.X) / 2.0,
                   (bbox.Min.Y + bbox.Max.Y) / 2.0,
                   (bbox.Min.Z + bbox.Max.Z) / 2.0)
    return None


class _BaseWrapper(object):
    """Common plumbing shared by the three wrapper kinds."""

    kind = '?'

    def __init__(self, element, doc):
        self.element = element
        self.doc = doc
        self.id_value = common.element_id_value(element.Id)

    @property
    def is_pinned(self):
        try:
            return bool(self.element.Pinned)
        except Exception:
            return False

    @property
    def attached_end(self):
        """True when the leader arrowhead is glued to the tagged element."""
        return False

    def set_end(self, key, point):
        """Move a leader arrowhead. Base: not supported."""
        return False

    def make_free(self):
        """Switch the leader end condition to Free. Base: not applicable."""
        return False

    def get_elbow(self, key):
        """Read a leader's current elbow, or None. Base: unavailable."""
        return None

    def toggle_leader(self, on):
        """Turn the leader off/on (used to measure the text alone inside a
        rolled-back transaction). Base: not supported."""
        return False

    def height_hint(self):
        """Known text height without measuring, or None. Base: unknown."""
        return None

    def tagged_curve(self):
        """The tagged element's location-curve endpoints (XYZ pair), or
        None. Base: only IndependentTags point at curve elements."""
        return None

    def set_justification(self, side):
        """Set text justification ('left'/'right'). Base: not applicable."""
        return False

    def primary_leader(self):
        """Return (position, end_point) of the first READABLE leader.

        ``position`` is the index into leader_keys() whose end resolved -
        not necessarily 0. The caller must plan/apply against this same
        position, otherwise the planned elbow lands on the wrong leader
        (review finding). Returns (None, None) when no end is readable.
        """
        for position, key in enumerate(self.leader_keys()):
            end = self.get_end(key)
            if end is not None:
                return position, end
        return None, None

    def primary_end(self, view=None):
        """Return the first readable leader end point, or None."""
        return self.primary_leader()[1]


class IndependentTagWrapper(_BaseWrapper):
    """IndependentTag: element, material, keynote and multi-category tags."""

    kind = 'tag'

    @property
    def has_leader(self):
        try:
            return bool(self.element.HasLeader)
        except Exception:
            return False

    @property
    def attached_end(self):
        try:
            return (self.element.LeaderEndCondition ==
                    LeaderEndCondition.Attached)
        except Exception:
            return False

    def make_free(self):
        """Free the leader ends so the arrowheads can be pinned explicitly."""
        try:
            self.element.LeaderEndCondition = LeaderEndCondition.Free
            return True
        except Exception as ex:
            common.get_file_logger().warning(
                'Could not free leader end on tag {0}: {1}'.format(
                    self.id_value, ex))
            return False

    def toggle_leader(self, on):
        try:
            self.element.HasLeader = bool(on)
            return True
        except Exception:
            return False

    def _resolve_tagged(self, reference):
        """Return (element, link_transform) for a reference, link-aware."""
        linked_id = reference.LinkedElementId
        if linked_id != ElementId.InvalidElementId:
            link = self.doc.GetElement(reference.ElementId)
            link_doc = link.GetLinkDocument() if link is not None else None
            if link_doc is None:
                return None, None
            return link_doc.GetElement(linked_id), link.GetTotalTransform()
        return self.doc.GetElement(reference.ElementId), None

    def tagged_curve(self):
        """The first tagged element's curve endpoints in host coordinates.

        Used by the order-by-pipe mode: the pipe's run and extent decide
        the tag's place in the stack and where the arrow may land. None
        for point-located elements (fittings, equipment).
        """
        for reference in self.leader_keys():
            try:
                element, transform = self._resolve_tagged(reference)
                location = getattr(element, 'Location', None)
                curve = (location.Curve
                         if isinstance(location, LocationCurve) else None)
                if curve is None:
                    continue
                start, end = curve.GetEndPoint(0), curve.GetEndPoint(1)
                if transform is not None:
                    start = transform.OfPoint(start)
                    end = transform.OfPoint(end)
                return start, end
            except Exception:
                continue
        return None

    def get_head(self):
        return self.element.TagHeadPosition

    def set_head(self, point):
        try:
            self.element.TagHeadPosition = point
            return True
        except Exception as ex:
            common.get_file_logger().warning(
                'Head move failed on tag {0}: {1}'.format(self.id_value, ex))
            return False

    def leader_keys(self):
        """The tagged References - one leader each. Empty when unavailable."""
        try:
            return list(self.element.GetTaggedReferences())
        except Exception:
            return []

    def get_end(self, reference):
        """The leader end: free end if readable, else the tagged element.

        GetLeaderEnd throws for Attached leaders, so the fallback derives a
        best-effort end from the tagged element: for curve-located elements
        (pipes, ducts) the point on the curve nearest the tag head - close
        to where Revit actually pins an attached arrowhead - otherwise the
        element anchor. The true attached end is not readable; Revit
        re-derives it from the tag position.

        Tags into LINKED models need Reference.LinkedElementId resolved in
        the link's own document (Reference.ElementId is the link instance,
        whose bounding box spans the whole linked model) and the anchor
        mapped back through the link transform. An unloaded link returns
        None so the tag lands in the honest 'no_end' skip report.
        """
        try:
            return self.element.GetLeaderEnd(reference)
        except Exception:
            pass
        try:
            linked_id = reference.LinkedElementId
            if linked_id != ElementId.InvalidElementId:
                link = self.doc.GetElement(reference.ElementId)
                link_doc = (link.GetLinkDocument()
                            if link is not None else None)
                if link_doc is None:
                    return None
                anchor = _element_anchor(link_doc.GetElement(linked_id))
                if anchor is None:
                    return None
                return link.GetTotalTransform().OfPoint(anchor)
            tagged = self.doc.GetElement(reference.ElementId)
            location = getattr(tagged, 'Location', None)
            if isinstance(location, LocationCurve) \
                    and location.Curve is not None:
                try:
                    return location.Curve.Project(self.get_head()).XYZPoint
                except Exception:
                    pass
            return _element_anchor(tagged)
        except Exception:
            return None

    def get_elbow(self, reference):
        try:
            return self.element.GetLeaderElbow(reference)
        except Exception:
            return None

    def set_elbow(self, reference, point):
        if reference is not None:
            try:
                self.element.SetLeaderElbow(reference, point)
                return True
            except Exception:
                pass
        try:
            if hasattr(self.element, 'LeaderElbow'):  # 2022 safety net
                self.element.LeaderElbow = point
                return True
        except Exception:
            pass
        return False

    def set_end(self, reference, point):
        if reference is not None:
            try:
                self.element.SetLeaderEnd(reference, point)
                return True
            except Exception:
                pass
        try:
            if hasattr(self.element, 'LeaderEnd'):  # 2022 safety net
                self.element.LeaderEnd = point
                return True
        except Exception:
            pass
        return False


class SpatialTagWrapper(_BaseWrapper):
    """Room / Space / Area tags - a single, property-addressed leader."""

    kind = 'spatial'

    @property
    def has_leader(self):
        try:
            return bool(self.element.HasLeader)
        except Exception:
            return False

    def get_head(self):
        return self.element.TagHeadPosition

    def set_head(self, point):
        try:
            self.element.TagHeadPosition = point
            return True
        except Exception as ex:
            common.get_file_logger().warning(
                'Head move failed on tag {0}: {1}'.format(self.id_value, ex))
            return False

    def toggle_leader(self, on):
        try:
            self.element.HasLeader = bool(on)
            return True
        except Exception:
            return False

    def leader_keys(self):
        return [0] if self.has_leader else []

    def get_end(self, _key):
        try:
            return self.element.LeaderEnd
        except Exception:
            pass
        # Fall back to the tagged room/space/area location.
        for attr in ('Room', 'Space', 'Area'):
            try:
                return _element_anchor(getattr(self.element, attr))
            except Exception:
                continue
        return None

    def get_elbow(self, _key):
        try:
            return self.element.LeaderElbow
        except Exception:
            return None

    def set_elbow(self, _key, point):
        try:
            self.element.LeaderElbow = point
            return True
        except Exception:
            return False

    def set_end(self, _key, point):
        try:
            self.element.LeaderEnd = point
            return True
        except Exception:
            return False


class TextNoteWrapper(_BaseWrapper):
    """TextNote with leaders. Leaders are index-addressed and re-fetched."""

    kind = 'textnote'

    @property
    def has_leader(self):
        return len(self._leaders()) > 0

    def _leaders(self):
        try:
            return list(self.element.GetLeaders())
        except Exception:
            return []

    def get_head(self):
        return self.element.Coord

    def set_head(self, point):
        try:
            self.element.Coord = point
            return True
        except Exception as ex:
            common.get_file_logger().warning(
                'Move failed on text note {0}: {1}'.format(
                    self.id_value, ex))
            return False

    def leader_keys(self):
        return list(range(len(self._leaders())))

    def _leader_at(self, index):
        leaders = self._leaders()
        return leaders[index] if 0 <= index < len(leaders) else None

    def get_end(self, index):
        leader = self._leader_at(index)
        try:
            return leader.End if leader is not None else None
        except Exception:
            return None

    def get_elbow(self, index):
        leader = self._leader_at(index)
        try:
            return leader.Elbow if leader is not None else None
        except Exception:
            return None

    def set_elbow(self, index, point):
        leader = self._leader_at(index)
        if leader is None:
            return False
        try:
            leader.Elbow = point
            return True
        except Exception:
            return False

    def set_end(self, index, point):
        """Leader.End became settable only in later API releases - guarded."""
        leader = self._leader_at(index)
        if leader is None:
            return False
        try:
            leader.End = point
            return True
        except Exception:
            return False

    def height_hint(self):
        """The note's own height - its bbox would include the leaders."""
        try:
            return float(self.element.Height)
        except Exception:
            return None

    def set_justification(self, side):
        """Set horizontal justification; may shift the visible arrowhead."""
        try:
            from Autodesk.Revit.DB import HorizontalTextAlignment
            value = (HorizontalTextAlignment.Left if side == 'left'
                     else HorizontalTextAlignment.Right)
            self.element.HorizontalAlignment = value
            return True
        except Exception as ex:
            common.get_file_logger().warning(
                'Justification failed on text note {0}: {1}'.format(
                    self.id_value, ex))
            return False


def is_supported(element):
    """True for the element kinds the Align Tags tool can process."""
    return isinstance(element, (IndependentTag, SpatialElementTag, TextNote))


def wrap(element, doc):
    """Return the right wrapper for an element, or None if unsupported."""
    if isinstance(element, IndependentTag):
        return IndependentTagWrapper(element, doc)
    if isinstance(element, SpatialElementTag):
        return SpatialTagWrapper(element, doc)
    if isinstance(element, TextNote):
        return TextNoteWrapper(element, doc)
    return None
