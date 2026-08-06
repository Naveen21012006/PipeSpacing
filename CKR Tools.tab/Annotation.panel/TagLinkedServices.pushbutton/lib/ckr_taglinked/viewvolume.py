# -*- coding: utf-8 -*-
"""The view test volume: view range plus crop region (clauses 7.4 and 7.5).

Built once per run and read from the hot loop. It answers two questions:

    rejects(corners)    stage 2 - can this bounding box be dismissed on
                        arithmetic alone, without opening a transaction?
    visible(segment)    clause 7.4 - what part of this run is actually
                        inside the view range and the crop, and how long
                        is it?

Both work in HOST coordinates. Link geometry is transformed into this
space before it gets here; the view geometry is never transformed into
link space, because that breaks for links placed with rotation or
mirroring (clause 7.4).
"""

from Autodesk.Revit.DB import PlanViewPlane, ViewPlan

from ckr_taglinked import compat, core

INFINITY = float('inf')


class ViewVolume(object):
    """The elevation band and plan region of one plan view."""

    def __init__(self, z_bottom, z_top, loops, annotation_loops, scale,
                 crop_active, warnings):
        self.z_bottom = z_bottom
        self.z_top = z_top
        self.loops = loops
        self.annotation_loops = annotation_loops
        self.scale = scale
        self.crop_active = crop_active
        self.warnings = warnings
        self.bounds = _loops_bounds(loops)

    def rejects(self, corners):
        """Return True when a bounding box cannot be in this view.

        Arithmetic only - no Revit call, no transaction - because stage 2
        has to remove the large majority of candidates for the
        create-and-test stage to be affordable (clause 7.5).
        """
        z_values = [corner[2] for corner in corners]
        if min(z_values) > self.z_top or max(z_values) < self.z_bottom:
            return True
        if self.bounds is None:
            return False
        x_lo, y_lo, x_hi, y_hi = self.bounds
        x_values = [corner[0] for corner in corners]
        y_values = [corner[1] for corner in corners]
        if min(x_values) > x_hi or max(x_values) < x_lo:
            return True
        if min(y_values) > y_hi or max(y_values) < y_lo:
            return True
        return False

    def visible(self, segment):
        """Return the visible piece of a run, or None (clause 7.4)."""
        return core.visible_segment(segment, self.z_bottom, self.z_top,
                                    self.loops)

    def paper_to_model(self, paper_mm):
        """Convert a paper-space millimetre distance to model feet."""
        return core.paper_mm_to_feet(paper_mm, self.scale)


def _loops_bounds(loops):
    """Return (x_lo, y_lo, x_hi, y_hi) over all loops, or None."""
    xs = []
    ys = []
    for loop in loops or []:
        for vertex in loop:
            xs.append(vertex[0])
            ys.append(vertex[1])
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def validate_view(view):
    """Return (ok, message) for the active view (SC-03 / AT-14).

    Only floor plans and other level-based plan views are supported in
    Phase 1; anything else is refused with a message rather than an
    exception.
    """
    if view is None:
        return False, 'There is no active view.'
    if not isinstance(view, ViewPlan):
        return False, (
            'Tag Linked Services runs on floor plans only.\n\n'
            'The active view is a {0}. Open a plan view and run the tool '
            'again.'.format(view.ViewType))
    if view.IsTemplate:
        return False, 'The active view is a view template.'
    if view.GenLevel is None:
        return False, ('The active plan view has no associated level, so '
                       'its view range cannot be resolved.')
    return True, ''


def build(doc, view, extend_to_view_depth=False):
    """Return the ViewVolume for a plan view.

    Args:
        doc (Document): The host document.
        view (ViewPlan): The active plan view.
        extend_to_view_depth (bool): Substitute the view depth plane for
            the bottom clip plane (clause 7.4.2). Off by default, because
            MEP curves below the bottom clip plane are not normally drawn.

    Returns:
        ViewVolume: With ``warnings`` listing every condition the user
        should know about - unlimited planes and an inactive crop change
        what the run will tag.
    """
    warnings = []
    z_top = INFINITY
    z_bottom = -INFINITY

    try:
        view_range = view.GetViewRange()
    except Exception:
        view_range = None

    if view_range is None:
        warnings.append('The view range could not be read; the elevation '
                        'clip is disabled and every run counts as visible.')
    else:
        top = compat.plane_elevation(doc, view, view_range,
                                     PlanViewPlane.TopClipPlane)
        if top is None:
            warnings.append('The top clip plane is Unlimited; no upper '
                            'elevation limit is applied.')
        else:
            z_top = top

        plane = PlanViewPlane.ViewDepthPlane if extend_to_view_depth \
            else PlanViewPlane.BottomClipPlane
        bottom = compat.plane_elevation(doc, view, view_range, plane)
        if bottom is None:
            warnings.append('The bottom clip plane is Unlimited; no lower '
                            'elevation limit is applied.')
        else:
            z_bottom = bottom

    if z_bottom > z_top:
        z_bottom, z_top = z_top, z_bottom

    crop_active = False
    try:
        crop_active = bool(view.CropBoxActive)
    except Exception:
        crop_active = False

    loops = compat.crop_loops(view) if crop_active else []
    if not loops:
        warnings.append('The crop region is inactive, so results include '
                        'content outside the sheet extent (clause 7.4.5).')

    annotation_loops = compat.annotation_crop_loops(view) if crop_active \
        else []

    try:
        scale = view.Scale or 1
    except Exception:
        scale = 1

    return ViewVolume(z_bottom, z_top, loops, annotation_loops, scale,
                      bool(loops), warnings)
