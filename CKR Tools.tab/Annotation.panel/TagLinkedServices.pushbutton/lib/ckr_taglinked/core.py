# -*- coding: utf-8 -*-
"""Pure geometry and rule logic for Tag Linked Services (brief clause 7.8).

Nothing in this module imports the Revit API. Every function takes plain
numbers, tuples and lists, so the classification, clipping and paper-space
rules can be unit-tested from the repo-root test suite without a Revit
session (brief deliverable 10.4).

Conventions used throughout:

    point       (x, y, z) tuple in HOST internal units (feet)
    segment     (point, point)
    loop        [(x, y), ...] a closed plan polygon; the closing edge is
                implicit, so the first vertex is not repeated
    loops       [loop, ...] - a point inside ANY loop is inside the region,
                which is what a split crop region means

The one unit rule that matters: paper distances are millimetres ON THE
SHEET and only become model distances through the view scale (clause 5.4).
"""

import math

MM_PER_FOOT = 304.8

# Classification results (brief clause 7.3).
HORIZONTAL = 'horizontal'
VERTICAL = 'vertical'
INCLINED = 'inclined'

# Loose tolerance for "is this number zero" on lengths in feet. 1e-9 ft is
# 0.3 nanometres - far below anything Revit models, so it only ever catches
# genuine degeneracy.
EPS = 1e-9


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------
def mm_to_feet(value_mm):
    """Convert millimetres to Revit internal units (feet)."""
    return float(value_mm) / MM_PER_FOOT


def feet_to_mm(value_feet):
    """Convert Revit internal units (feet) to millimetres."""
    return float(value_feet) * MM_PER_FOOT


def paper_mm_to_feet(paper_mm, view_scale):
    """Convert a paper-space distance to a model distance (clause 7.6.3).

    ``view_scale`` is Revit's ``View.Scale`` - the denominator of 1:scale,
    so 50 for 1:50. A 3 mm paper offset is therefore 150 mm of model at
    1:50 and 600 mm at 1:200, which is the whole point of clause 5.4: the
    gap measures the same on both sheets.

    Args:
        paper_mm (float): Distance measured on the printed sheet, in mm.
        view_scale (int): The view's scale denominator.

    Returns:
        float: The equivalent model distance in feet.
    """
    scale = float(view_scale) if view_scale else 1.0
    return mm_to_feet(float(paper_mm) * scale)


# ---------------------------------------------------------------------------
# Vector helpers
# ---------------------------------------------------------------------------
def subtract(a, b):
    """Return the vector a - b as a 3-tuple."""
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def length(vector):
    """Return the magnitude of a 3-vector."""
    return math.sqrt(vector[0] ** 2 + vector[1] ** 2 + vector[2] ** 2)


def distance(a, b):
    """Return the distance between two points."""
    return length(subtract(a, b))


def point_at(p0, p1, t):
    """Return the point at parameter t along the segment p0 -> p1."""
    return (p0[0] + (p1[0] - p0[0]) * t,
            p0[1] + (p1[1] - p0[1]) * t,
            p0[2] + (p1[2] - p0[2]) * t)


def midpoint(segment):
    """Return the midpoint of a (point, point) segment."""
    return point_at(segment[0], segment[1], 0.5)


def segment_length(segment):
    """Return the length of a (point, point) segment."""
    return distance(segment[0], segment[1])


def longest_segment(segments):
    """Return the longest of a list of segments, or None when empty.

    Clause 7.4.4: where clipping yields several surviving pieces - a run
    that leaves and re-enters an L-shaped crop - the longest one is the
    piece the tag belongs on.
    """
    best = None
    best_length = -1.0
    for segment in segments:
        value = segment_length(segment)
        if value > best_length:
            best = segment
            best_length = value
    return best


def plan_direction(segment):
    """Return the unit direction of a segment projected into plan.

    Returns:
        tuple | None: (x, y) unit vector, or None for a run that is
        vertical (or so close to vertical that a plan direction is
        meaningless) - a riser has no direction to offset perpendicular to.
    """
    dx = segment[1][0] - segment[0][0]
    dy = segment[1][1] - segment[0][1]
    span = math.sqrt(dx * dx + dy * dy)
    if span <= EPS:
        return None
    return (dx / span, dy / span)


def perpendicular(direction):
    """Return the plan direction rotated 90 degrees anticlockwise."""
    return (-direction[1], direction[0])


def offset_point(point, vector2d, amount):
    """Return the point moved by ``amount`` along a plan unit vector."""
    return (point[0] + vector2d[0] * amount,
            point[1] + vector2d[1] * amount,
            point[2])


# ---------------------------------------------------------------------------
# Clause 7.3 - classification by angle
# ---------------------------------------------------------------------------
def angle_from_horizontal(vector):
    """Return the angle of a vector from the horizontal plane, in degrees.

    Returns None for a zero-length vector, which is the caller's cue to
    report the element as unclassified rather than guess.
    """
    span = length(vector)
    if span <= EPS:
        return None
    ratio = abs(vector[2]) / span
    return math.degrees(math.asin(min(1.0, ratio)))


def classify(vector, horizontal_tol_deg=15.0, vertical_tol_deg=75.0):
    """Classify a run direction as horizontal, vertical or inclined.

    Clause 5.1 is the reason this is an angle test and not ``dz == 0``:
    gravity drainage is modelled at 1:100 to 1:40, so a flatness test
    rejects every soil, waste and rainwater run in the model. The default
    15 degree horizontal tolerance is a gradient of about 1:3.7 - far in
    excess of any drainage gradient in use.

    Args:
        vector (tuple): The run direction (need not be normalised).
        horizontal_tol_deg (float): At or below this angle -> horizontal.
        vertical_tol_deg (float): At or above this angle -> vertical.

    Returns:
        tuple: (classification, angle_deg). Both are None when the vector
        has no length.
    """
    theta = angle_from_horizontal(vector)
    if theta is None:
        return None, None
    if theta <= horizontal_tol_deg:
        return HORIZONTAL, theta
    if theta >= vertical_tol_deg:
        return VERTICAL, theta
    return INCLINED, theta


# ---------------------------------------------------------------------------
# Clause 7.4 - clipping to the view range and the crop region
# ---------------------------------------------------------------------------
def clip_to_band(segment, z_bottom, z_top):
    """Clip a segment to an elevation band; None when it misses entirely.

    ``z_bottom`` / ``z_top`` may be -inf / +inf for an Unlimited view range
    plane (clause 7.4.1). A run lying flat inside the band survives whole;
    one lying flat outside it is rejected.

    Args:
        segment (tuple): (point, point) in host coordinates.
        z_bottom (float): Bottom clip elevation, feet, or -inf.
        z_top (float): Top clip elevation, feet, or +inf.

    Returns:
        tuple | None: The surviving (point, point), or None.
    """
    if z_top < z_bottom:
        z_bottom, z_top = z_top, z_bottom

    p0, p1 = segment
    dz = p1[2] - p0[2]

    if abs(dz) <= EPS:
        # Flat run: it is either wholly in the band or wholly out of it.
        if p0[2] < z_bottom - EPS or p0[2] > z_top + EPS:
            return None
        return (p0, p1)

    t_bottom = (z_bottom - p0[2]) / dz
    t_top = (z_top - p0[2]) / dz
    lo = max(0.0, min(t_bottom, t_top))
    hi = min(1.0, max(t_bottom, t_top))
    if hi <= lo:
        return None
    return (point_at(p0, p1, lo), point_at(p0, p1, hi))


def point_in_loop(point2d, loop):
    """Return True when a plan point lies inside one closed polygon.

    Standard even-odd ray cast, so concave crop regions are handled
    correctly (clause 7.4.3).
    """
    x, y = point2d[0], point2d[1]
    inside = False
    count = len(loop)
    j = count - 1
    for i in range(count):
        xi, yi = loop[i][0], loop[i][1]
        xj, yj = loop[j][0], loop[j][1]
        if (yi > y) != (yj > y):
            crossing = xi + (y - yi) * (xj - xi) / (yj - yi)
            if x < crossing:
                inside = not inside
        j = i
    return inside


def point_in_loops(point2d, loops):
    """Return True when a plan point lies inside any of the loops."""
    for loop in loops or []:
        if len(loop) >= 3 and point_in_loop(point2d, loop):
            return True
    return False


def _edge_parameters(segment, loops):
    """Return the sorted segment parameters where it crosses any loop edge."""
    p0, p1 = segment
    x0, y0 = p0[0], p0[1]
    dx, dy = p1[0] - x0, p1[1] - y0
    values = [0.0, 1.0]

    for loop in loops or []:
        count = len(loop)
        if count < 3:
            continue
        for index in range(count):
            ax, ay = loop[index][0], loop[index][1]
            bx, by = loop[(index + 1) % count][0], loop[(index + 1) % count][1]
            ex, ey = bx - ax, by - ay
            denominator = dx * ey - dy * ex
            if abs(denominator) <= EPS:
                continue  # parallel to this edge
            t = ((ax - x0) * ey - (ay - y0) * ex) / denominator
            s = ((ax - x0) * dy - (ay - y0) * dx) / denominator
            if -EPS <= s <= 1.0 + EPS and 0.0 < t < 1.0:
                values.append(t)

    return sorted(set(round(value, 9) for value in values))


def clip_to_loops(segment, loops):
    """Return the parts of a segment lying inside the plan region.

    The segment is split at every crossing of every loop edge and each
    sub-interval is kept or dropped on an inside test at its midpoint, so
    concave and multi-loop crops behave correctly. Adjacent surviving
    intervals are merged back into one piece.

    Args:
        segment (tuple): (point, point) in host coordinates.
        loops (list): Plan polygons; an empty list means "no crop", and
            the segment is returned unchanged.

    Returns:
        list: Surviving (point, point) segments, in order along the run.
    """
    if not loops:
        return [segment]

    p0, p1 = segment
    if distance(p0, p1) <= EPS:
        return [segment] if point_in_loops((p0[0], p0[1]), loops) else []

    parameters = _edge_parameters(segment, loops)
    kept = []
    for index in range(len(parameters) - 1):
        lo, hi = parameters[index], parameters[index + 1]
        if hi - lo <= 1e-9:
            continue
        centre = point_at(p0, p1, (lo + hi) / 2.0)
        if point_in_loops((centre[0], centre[1]), loops):
            if kept and abs(kept[-1][1] - lo) <= 1e-9:
                kept[-1][1] = hi          # merge with the previous piece
            else:
                kept.append([lo, hi])

    return [(point_at(p0, p1, lo), point_at(p0, p1, hi)) for lo, hi in kept]


def visible_segment(segment, z_bottom, z_top, loops):
    """Return the visible piece of a run, or None (clause 7.4).

    The elevation clip runs first (cheap, and it rejects most of what the
    view range excludes), then the plan clip against the crop, then the
    longest surviving piece is taken.
    """
    banded = clip_to_band(segment, z_bottom, z_top)
    if banded is None:
        return None
    pieces = clip_to_loops(banded, loops)
    if not pieces:
        return None
    return longest_segment(pieces)


# ---------------------------------------------------------------------------
# Plan region helpers used for the annotation crop clamp (clause 7.6.4)
# ---------------------------------------------------------------------------
def loop_centroid(loop):
    """Return the area centroid of a closed polygon.

    Falls back to the average of the vertices for a degenerate (zero area)
    loop, so the caller always gets a usable interior direction.
    """
    area = 0.0
    cx = 0.0
    cy = 0.0
    count = len(loop)
    for index in range(count):
        x0, y0 = loop[index][0], loop[index][1]
        x1, y1 = loop[(index + 1) % count][0], loop[(index + 1) % count][1]
        cross = x0 * y1 - x1 * y0
        area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(area) <= EPS:
        return (sum(v[0] for v in loop) / count,
                sum(v[1] for v in loop) / count)
    area *= 0.5
    return (cx / (6.0 * area), cy / (6.0 * area))


def _nearest_on_edge(point2d, a, b):
    """Return the closest point to point2d on the segment a-b, in plan."""
    ex, ey = b[0] - a[0], b[1] - a[1]
    span = ex * ex + ey * ey
    if span <= EPS:
        return (a[0], a[1])
    t = ((point2d[0] - a[0]) * ex + (point2d[1] - a[1]) * ey) / span
    t = max(0.0, min(1.0, t))
    return (a[0] + ex * t, a[1] + ey * t)


def nearest_on_loops(point2d, loops):
    """Return (closest boundary point, distance, loop) for a plan point."""
    best = None
    best_distance = None
    best_loop = None
    for loop in loops or []:
        count = len(loop)
        if count < 3:
            continue
        for index in range(count):
            candidate = _nearest_on_edge(point2d, loop[index],
                                         loop[(index + 1) % count])
            span = math.hypot(candidate[0] - point2d[0],
                              candidate[1] - point2d[1])
            if best_distance is None or span < best_distance:
                best, best_distance, best_loop = candidate, span, loop
    return best, best_distance, best_loop


def inside_region(point, loops, inset=0.0):
    """Return True when a point sits inside the region by at least ``inset``."""
    if not loops:
        return True
    point2d = (point[0], point[1])
    if not point_in_loops(point2d, loops):
        return False
    if inset <= 0.0:
        return True
    _, span, _ = nearest_on_loops(point2d, loops)
    return span is None or span >= inset


def clamp_into_region(point, loops, inset=0.0):
    """Move a point inside the plan region, keeping its elevation.

    Clause 5.3: a tag whose insertion point falls outside the annotation
    crop is simply not drawn on the sheet, however visible its element is.
    A point already comfortably inside is returned untouched; anything
    else is pulled to the nearest boundary position and then pushed
    ``inset`` towards the interior so it does not sit exactly on the line.

    Args:
        point (tuple): The candidate insertion point.
        loops (list): The annotation crop polygons; empty means no crop.
        inset (float): Clearance to keep from the boundary, in feet.

    Returns:
        tuple: A point inside the region (the original when it already was).
    """
    if not loops:
        return point
    if inside_region(point, loops, inset):
        return point

    point2d = (point[0], point[1])
    boundary, _, loop = nearest_on_loops(point2d, loops)
    if boundary is None or loop is None:
        return point

    centre = loop_centroid(loop)
    dx, dy = centre[0] - boundary[0], centre[1] - boundary[1]
    span = math.hypot(dx, dy)
    if span <= EPS:
        return (boundary[0], boundary[1], point[2])
    step = inset if inset > 0.0 else 0.0
    return (boundary[0] + dx / span * step,
            boundary[1] + dy / span * step,
            point[2])


# ---------------------------------------------------------------------------
# Clause 7.6 - placement candidates and tag spacing
# ---------------------------------------------------------------------------
#: Positions tried along the visible segment, in order (clause 7.6.6). The
#: midpoint first, then a quarter and three-quarters along.
CANDIDATE_FRACTIONS = (0.5, 0.25, 0.75)


def placement_candidates(segment, offset, fractions=CANDIDATE_FRACTIONS,
                         sides=(1.0, -1.0)):
    """Return insertion points for a horizontal run, best first.

    The tag sits ``offset`` to one side of the run, measured perpendicular
    to the run in plan. Every fraction is tried on the preferred side
    before the opposite side is considered, which is the retry ladder of
    clause 7.6.6.

    A run with no plan direction (a riser seen end-on) yields the
    diagonal offsets of clause 7.6.2 instead.

    Args:
        segment (tuple): The visible piece of the run.
        offset (float): Perpendicular offset in feet (model distance).
        fractions (tuple): Positions along the segment to try.
        sides (tuple): Multipliers for the offset direction.

    Returns:
        list: Candidate points, in the order they should be tried.
    """
    direction = plan_direction(segment)
    if direction is None:
        return riser_candidates(midpoint(segment), offset)

    normal = perpendicular(direction)
    points = []
    for side in sides:
        for fraction in fractions:
            anchor = point_at(segment[0], segment[1], fraction)
            points.append(offset_point(anchor, normal, offset * side))
    return points


#: Diagonal directions for riser tags, upper-right first then around the
#: compass, so a crowded riser has somewhere else to go (clause 7.6.2).
_DIAGONALS = ((0.70710678, 0.70710678), (-0.70710678, 0.70710678),
              (-0.70710678, -0.70710678), (0.70710678, -0.70710678))


def riser_candidates(point, offset):
    """Return diagonal insertion points around a riser, best first.

    A riser reads as a small circle in plan, so there is no run direction
    to offset perpendicular to - the tag goes diagonally clear of the
    circle and is given a leader (clause 7.6.2).
    """
    return [offset_point(point, diagonal, offset) for diagonal in _DIAGONALS]


def rect_of(values_u, values_v):
    """Return an axis-aligned (u_lo, v_lo, u_hi, v_hi) rectangle."""
    return (min(values_u), min(values_v), max(values_u), max(values_v))


def rect_gap(a, b):
    """Return the clear separation between two rectangles.

    Separation on either axis is enough - tags side by side with a clear
    gap between them do not conflict, however much they overlap
    vertically - so the larger of the two axis gaps is the answer. The
    value is negative when the rectangles overlap on both axes.
    """
    horizontal_gap = max(b[0] - a[2], a[0] - b[2])
    vertical_gap = max(b[1] - a[3], a[1] - b[3])
    return max(horizontal_gap, vertical_gap)


def rects_clear(a, b, clearance):
    """Return True when two rectangles are at least ``clearance`` apart."""
    if a is None or b is None:
        return True
    return rect_gap(a, b) >= clearance


def min_gap(rect, placed):
    """Return the tightest gap between a rectangle and those already placed.

    Infinity when nothing has been placed yet, so an empty view always
    reads as roomy. This is the score the retry ladder maximises when no
    position achieves the full clearance: the least bad position is still
    better than wherever the last attempt happened to end.
    """
    if rect is None:
        return float('inf')
    best = float('inf')
    for other in placed:
        if other is None:
            continue
        best = min(best, rect_gap(rect, other))
    return best


def rect_conflicts(rect, placed, clearance):
    """Return True when a rectangle is too close to any already placed."""
    return min_gap(rect, placed) < clearance


# ---------------------------------------------------------------------------
# Clause 5.2 / FR-04 - the length rule
# ---------------------------------------------------------------------------
def passes_length(classification, visible_length, minimums):
    """Return True when a run is long enough to deserve a tag.

    ``visible_length`` is Lv - measured on the clipped, visible portion,
    never on the element (clause 5.2), which is what makes a thirty-metre
    riser element behave the same on every plan it passes through.

    Args:
        classification (str): One of HORIZONTAL / VERTICAL / INCLINED.
        visible_length (float): Lv in feet.
        minimums (dict): Classification -> minimum length in feet.

    Returns:
        bool: True when the run passes.
    """
    minimum = minimums.get(classification)
    if minimum is None:
        return True
    return visible_length >= minimum
