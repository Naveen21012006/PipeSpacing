# -*- coding: utf-8 -*-
"""Tag creation and placement (FR-06, clause 7.6).

Every offset and clearance here arrives in millimetres ON THE SHEET and is
multiplied by the view scale before it touches the model, so the same
settings measure identically at 1:50 and 1:200 (clause 5.4 / AT-07).

The Placer also owns stage 3 of clause 7.5: each tag is created inside its
own SubTransaction and kept only if it reports a bounding box in the view.
A tag with no bounding box is not drawn there, and that is the only
reliable confirmation the API offers.
"""

from Autodesk.Revit.DB import SubTransaction

from ckr_taglinked import compat, core

# Placement outcomes, reported per element by the runner.
PLACED = 'placed'
PLACED_CROWDED = 'placed_crowded'
NOT_VISIBLE = 'not_visible'
ERROR = 'error'

#: Paper-space clearance kept between an insertion point and the annotation
#: crop boundary, so a clamped tag lands inside the line rather than on it.
CROP_INSET_MM = 2.0


class Placer(object):
    """Creates tags for one run, honouring the clause 7.6 placement rules."""

    def __init__(self, doc, view, volume, config, log):
        self.doc = doc
        self.view = view
        self.volume = volume
        self.log = log
        self.verify = bool(config.get('verify_visible', True))

        self.horizontal_offset = volume.paper_to_model(
            config.get('offset_horizontal_mm', 3.0))
        self.riser_offset = volume.paper_to_model(
            config.get('offset_vertical_mm', 8.0))
        self.clearance = volume.paper_to_model(
            config.get('spacing_mm', 5.0))
        self.inset = volume.paper_to_model(CROP_INSET_MM)

        #: Rectangles of the tags placed so far, for the clause 7.6.6
        #: minimum clear spacing test.
        self.placed_rects = []
        #: Tags whose insertion point had to be pulled inside the
        #: annotation crop, reported to the log at the end of the run.
        self.clamped = 0
        self._clamped_flags = []

    # -- candidate positions ------------------------------------------------
    def candidates(self, segment, classification):
        """Return insertion points for a run, best first.

        Horizontal and inclined runs get a perpendicular offset at the
        midpoint of the CLIPPED segment - which is clause 7.6.5: where the
        true midpoint of a long run falls outside the crop, the midpoint
        of the visible portion is used instead, because that is the only
        part of it the reader can see.

        Risers get the diagonal offsets of clause 7.6.2.

        Every candidate is then clamped inside the annotation crop
        (clause 7.6.4 / 5.3).
        """
        if classification == core.VERTICAL:
            points = core.riser_candidates(core.midpoint(segment),
                                           self.riser_offset)
        else:
            points = core.placement_candidates(segment,
                                               self.horizontal_offset)

        loops = self.volume.annotation_loops
        clamped = []
        flags = []
        for point in points:
            inside = core.clamp_into_region(point, loops, self.inset)
            clamped.append(inside)
            flags.append(inside is not point)
        #: Whether each candidate had to be moved; only the one actually
        #: used is counted, so the log reports tags that were clamped
        #: rather than positions that were considered.
        self._clamped_flags = flags
        return clamped

    # -- geometry read back -------------------------------------------------
    def _rect(self, tag):
        """Return the tag's plan rectangle in the view, or None.

        A tag that reports no bounding box in this view is not drawn in
        it - the stage 3 test of clause 7.5.
        """
        try:
            bbox = tag.get_BoundingBox(self.view)
        except Exception:
            bbox = None
        if bbox is None:
            return None
        corners = compat.bounding_box_corners(bbox)
        return core.rect_of([corner[0] for corner in corners],
                            [corner[1] for corner in corners])

    # -- creation -----------------------------------------------------------
    def place(self, reference, tag_type_id, classification, segment,
              add_leader, orientation):
        """Create one tag, or explain why it was not created.

        The whole attempt lives in a SubTransaction so a tag that turns
        out not to be visible leaves no trace in the document.

        Returns:
            tuple: (status, tag, detail). Status is one of PLACED,
            PLACED_CROWDED, NOT_VISIBLE or ERROR.
        """
        self._clamped_flags = []
        points = self.candidates(segment, classification)
        if not points:
            return ERROR, None, 'no insertion point could be derived'

        # Clause 7.6.2: a riser tag is a circle with a tag beside it; it
        # is unreadable without a leader, so the leader is forced on.
        leader = True if classification == core.VERTICAL else add_leader

        sub = SubTransaction(self.doc)
        sub.Start()
        try:
            tag = compat.create_tag(self.doc, tag_type_id, self.view.Id,
                                    reference, leader, orientation,
                                    points[0])
        except Exception as ex:
            sub.RollBack()
            return ERROR, None, '{0}'.format(ex)

        if not self.verify:
            # Fast path: no regeneration, so no bounding box and no
            # spacing test. Documented in the dialog as the trade it is.
            sub.Commit()
            self._count_clamp(0)
            return PLACED, tag, ''

        try:
            self.doc.Regenerate()
        except Exception as ex:
            sub.RollBack()
            return ERROR, None, '{0}'.format(ex)

        rect = self._rect(tag)
        if rect is None:
            sub.RollBack()
            return NOT_VISIBLE, None, 'tag has no bounding box in this view'

        rect, crowded, used = self._resolve_spacing(tag, rect, points)
        sub.Commit()
        self.placed_rects.append(rect)
        self._count_clamp(used)

        if crowded:
            return PLACED_CROWDED, tag, 'spacing not achieved'
        return PLACED, tag, ''

    def _count_clamp(self, index):
        """Record that the position actually used had to be clamped."""
        flags = getattr(self, '_clamped_flags', [])
        if index < len(flags) and flags[index]:
            self.clamped += 1

    def _resolve_spacing(self, tag, rect, points):
        """Walk the clause 7.6.6 retry ladder until the tag has room.

        The candidate list is already ordered midpoint / quarter / three
        quarters on the preferred side, then the same three on the
        opposite side. When none of them clears, the tag stays where it
        last landed, keeps its leader, and the shortfall is logged rather
        than hidden.

        Returns:
            tuple: (rectangle, crowded, index of the position used).
        """
        best_index = 0
        best_rect = rect
        best_gap = core.min_gap(rect, self.placed_rects)
        if best_gap >= self.clearance:
            return rect, False, 0

        for index in range(1, len(points)):
            if not compat.set_tag_head(tag, points[index]):
                continue
            try:
                self.doc.Regenerate()
            except Exception:
                break
            candidate = self._rect(tag)
            if candidate is None:
                continue
            gap = core.min_gap(candidate, self.placed_rects)
            if gap > best_gap:
                best_index, best_rect, best_gap = index, candidate, gap
            if gap >= self.clearance:
                return candidate, False, index

        # Nothing cleared. Fall back to the roomiest position tried rather
        # than to wherever the last attempt happened to leave the tag, and
        # give it a leader so it can still be read.
        if compat.set_tag_head(tag, points[best_index]):
            try:
                self.doc.Regenerate()
                best_rect = self._rect(tag) or best_rect
            except Exception:
                pass
        try:
            tag.HasLeader = True
        except Exception:
            pass
        self.log.info('Spacing not achieved for tag %s (%.0fmm clear, %.0fmm '
                      'wanted); placed with leader.',
                      compat.id_value(tag.Id), compat.internal_to_mm(best_gap),
                      compat.internal_to_mm(self.clearance))
        return best_rect, True, best_index
