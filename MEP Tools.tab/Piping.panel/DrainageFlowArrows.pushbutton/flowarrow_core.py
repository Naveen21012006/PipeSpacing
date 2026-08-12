# -*- coding: utf-8 -*-
"""Drainage Flow Arrows - pure placement engine.

Everything here is plain geometry on (x, y, z) tuples - no Revit API
imports - so the whole decision chain is unit-tested outside Revit
(tests/test_flowarrow_core.py), the same layering as Align Tags'
engine.py. script.py converts Revit points to millimetre tuples, calls
this module, and converts the answers back.

The chain mirrors the tool spec:

    classify_pipe()      - sloped / too short / vertical / flat
    oriented_endpoints() - which endpoint is higher
    flow_direction()     - unit vector, higher -> lower
    arrow_stations()     - distances along the pipe for each arrow
    arrow_points()       - stations turned into 3D points
    is_near_existing()   - duplicate test against existing arrows
    plan_angle() / tilt_angle() - rotation angles for the family

All distances are in the units of the points passed in (script.py uses
millimetres, matching the configuration block there).

Author: Naveen
Target: CPython (tests) / IronPython (Revit)
"""

import math

# Classification results.
SLOPED = 'sloped'
TOO_SHORT = 'too_short'
VERTICAL = 'vertical'
FLAT = 'flat'

# Sides for tag-based arrow families with a Left and a Right type.
LEFT = 'left'
RIGHT = 'right'

# Default rules (millimetres / degrees). script.py copies these into its
# CONFIGURATION block; tests use them directly.
DEFAULTS = {
    'min_pipe_length_mm': 1000.0,      # ignore pipes shorter than this
    'multi_arrow_threshold_mm': 10000.0,  # one arrow per started 10 m
    'end_clearance_mm': 1000.0,        # keep arrows this far from the ends
    'duplicate_tolerance_mm': 300.0,   # existing arrow within this = skip
    'min_elevation_diff_mm': 5.0,      # smaller rise/fall counts as flat
    'vertical_angle_deg': 80.0,        # steeper than this = a riser, skip
}


# ---------------------------------------------------------------------------
# Vector helpers
# ---------------------------------------------------------------------------
def subtract(a, b):
    """Return the vector a - b."""
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def length(v):
    """Return the 3D length of a vector."""
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def distance(a, b):
    """Return the 3D distance between two points."""
    return length(subtract(a, b))


# ---------------------------------------------------------------------------
# Slope classification
# ---------------------------------------------------------------------------
def classify_pipe(start, end, config):
    """Classify a pipe segment by its endpoints.

    Args:
        start, end: (x, y, z) endpoints of the location line.
        config: rules dict (see DEFAULTS).

    Returns:
        str: SLOPED when the pipe should receive arrows, otherwise
        TOO_SHORT, VERTICAL or FLAT saying why it is skipped.
    """
    run = subtract(end, start)
    total = length(run)
    if total < config['min_pipe_length_mm']:
        return TOO_SHORT

    horizontal = math.hypot(run[0], run[1])
    rise = abs(run[2])
    # Angle from horizontal decides vertical: a riser has almost no plan
    # run, and an arrow drawn on it would sit on a point.
    angle = math.degrees(math.atan2(rise, horizontal))
    if angle > config['vertical_angle_deg']:
        return VERTICAL

    if rise < config['min_elevation_diff_mm']:
        return FLAT
    return SLOPED


def oriented_endpoints(start, end):
    """Return (high, low) - the endpoints ordered by elevation.

    The pipe's own start/end order carries no meaning for drainage; only
    the actual elevations decide the flow.
    """
    if start[2] >= end[2]:
        return start, end
    return end, start


def flow_direction(start, end):
    """Return the unit flow vector: from the higher to the lower endpoint."""
    high, low = oriented_endpoints(start, end)
    v = subtract(low, high)
    n = length(v)
    if n == 0.0:
        return (0.0, 0.0, 0.0)
    return (v[0] / n, v[1] / n, v[2] / n)


# ---------------------------------------------------------------------------
# Arrow positions
# ---------------------------------------------------------------------------
def arrow_stations(pipe_length, config):
    """Return the arrow distances measured from the HIGH end of the pipe.

    Short eligible pipes get one arrow at the midpoint. Pipes longer than
    the multi-arrow threshold get one arrow per started threshold length,
    spread evenly over the span that respects the end clearance. When the
    clearance leaves no usable span (a pipe barely over the minimum
    length), the midpoint arrow is still placed - the clearance shrinks
    before the arrow disappears.
    """
    clearance = config['end_clearance_mm']
    threshold = config['multi_arrow_threshold_mm']

    if pipe_length <= threshold:
        return [pipe_length / 2.0]

    usable = pipe_length - 2.0 * clearance
    if usable <= 0.0:
        return [pipe_length / 2.0]

    count = int(math.ceil(pipe_length / threshold))
    return [clearance + usable * (i + 0.5) / count for i in range(count)]


def arrow_points(high, low, stations):
    """Turn stations (distances from the high end) into 3D points.

    Args:
        high, low: the pipe endpoints, higher first.
        stations: distances along the pipe from the high end.

    Returns:
        list of (x, y, z) points on the pipe centreline.
    """
    total = distance(high, low)
    if total == 0.0:
        return []
    points = []
    for s in stations:
        t = s / total
        points.append((high[0] + (low[0] - high[0]) * t,
                       high[1] + (low[1] - high[1]) * t,
                       high[2] + (low[2] - high[2]) * t))
    return points


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------
def is_near_existing(point, existing_points, tolerance):
    """True when an existing arrow sits within tolerance of the point."""
    for other in existing_points:
        if distance(point, other) <= tolerance:
            return True
    return False


# ---------------------------------------------------------------------------
# Rotation angles
# ---------------------------------------------------------------------------
def plan_angle(direction):
    """Return the plan (Z-axis) rotation in radians for the flow vector.

    Measured from the +X axis, counter-clockwise - the angle to rotate a
    family whose arrow points along +X.
    """
    return math.atan2(direction[1], direction[0])


def tilt_angle(direction):
    """Return the downhill tilt in radians below the horizontal.

    Positive for a downhill flow vector. Applied about the horizontal
    axis perpendicular to the flow (left of it), a positive rotation
    pitches the arrow nose-down along the pipe's true inclination.
    """
    horizontal = math.hypot(direction[0], direction[1])
    return math.atan2(-direction[2], horizontal)


# ---------------------------------------------------------------------------
# Left/right side for tag-based arrow families
# ---------------------------------------------------------------------------
def arrow_side(flow_dx, flow_dy, eps=1e-9):
    """Return which type of a Left/Right tag pair points down the flow.

    A pipe tag that rotates with its pipe is drawn along the pipe in
    Revit's readable orientation: never upside down, and bottom-to-top
    when the pipe runs vertically on screen. So the 'Right' type's head
    points toward the readable end. Given the flow direction projected
    into the view (flow_dx toward the view's right, flow_dy toward its
    up), the flow matches the readable end - RIGHT - when it points
    rightward, or straight up for a vertical run; otherwise the flipped
    type - LEFT - is the one whose head points downhill.
    """
    if flow_dx > eps:
        return RIGHT
    if flow_dx < -eps:
        return LEFT
    return RIGHT if flow_dy > 0.0 else LEFT
