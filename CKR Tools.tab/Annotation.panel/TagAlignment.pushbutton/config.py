# -*- coding: utf-8 -*-
"""Central configuration for the MEP Tag Alignment tool.

Everything a user or BIM manager may want to change lives here: which
categories are supported, which tag category each maps to, and the annotation
defaults used when creating tags and tidying leaders.

Adding a new category is a one-line change to _CATEGORY_NAME_PAIRS - no other
module needs to be touched.
"""

from Autodesk.Revit.DB import BuiltInCategory, TagOrientation


# ---------------------------------------------------------------------------
# Supported categories
# ---------------------------------------------------------------------------
# (element category, tag category) pairs, referenced by NAME so that a name
# missing from a given Revit version is skipped instead of breaking the tool.
_CATEGORY_NAME_PAIRS = [
    ('OST_PipeCurves', 'OST_PipeTags'),
    ('OST_PipeFitting', 'OST_PipeFittingTags'),
    ('OST_PipeAccessory', 'OST_PipeAccessoryTags'),
    ('OST_DuctCurves', 'OST_DuctTags'),
    ('OST_DuctFitting', 'OST_DuctFittingTags'),
    ('OST_DuctAccessory', 'OST_DuctAccessoryTags'),
    ('OST_CableTray', 'OST_CableTrayTags'),
    ('OST_CableTrayFitting', 'OST_CableTrayFittingTags'),
    ('OST_Conduit', 'OST_ConduitTags'),
    ('OST_ConduitFitting', 'OST_ConduitFittingTags'),
    ('OST_MechanicalEquipment', 'OST_MechanicalEquipmentTags'),
    ('OST_PlumbingFixtures', 'OST_PlumbingFixtureTags'),
    ('OST_DuctTerminal', 'OST_DuctTerminalTags'),
]


def _build_category_map():
    """Return {element category id (int): tag BuiltInCategory}.

    Pairs whose enum names do not exist in this Revit version are skipped, so
    the tool degrades gracefully instead of failing to load.
    """
    mapping = {}
    for element_name, tag_name in _CATEGORY_NAME_PAIRS:
        element_category = getattr(BuiltInCategory, element_name, None)
        tag_category = getattr(BuiltInCategory, tag_name, None)
        if element_category is None or tag_category is None:
            continue
        mapping[int(element_category)] = tag_category
    return mapping


# {element category id (int) -> tag BuiltInCategory}
SUPPORTED_CATEGORIES = _build_category_map()


# ---------------------------------------------------------------------------
# Tag type selection
# ---------------------------------------------------------------------------
# Ask which tag type to use, once per category, whenever new tags will be
# created. You pick from the types actually loaded in the project, so there is
# no name to get wrong. Set False to skip the prompt and fall back to
# PREFERRED_TAG_TYPES (below), then to whichever tag Revit returns first.
ASK_FOR_TAG_TYPE = True


# ---------------------------------------------------------------------------
# Preferred tag family / type per element category
# ---------------------------------------------------------------------------
# Which tag to create for each category, as (family name, type name).
#
# WITHOUT an entry the tool takes the FIRST tag type the collector returns for
# that category, which is arbitrary - that is exactly how a water pipe ends up
# wearing a fire-pipe tag. Pin the ones that matter here.
#
# Either name may be None to match on the other alone, e.g.
#   ('ME-Pipe Size Tag-HL', None)  -> any type in that family
#   (None, 'Pipe Size Tag-HL')     -> that type in any family
# Names are matched case-insensitively. If the pinned tag is not loaded in the
# project, the tool falls back to the first one and logs a warning.
_PREFERRED_TAG_NAMES = {
    'OST_PipeCurves': ('ME-Pipe Size Tag-HL', 'Pipe Size Tag-HL'),
}


def _build_preferred_tag_types():
    """Return {element category id (int): (family name, type name)}."""
    preferred = {}
    for category_name, names in _PREFERRED_TAG_NAMES.items():
        category = getattr(BuiltInCategory, category_name, None)
        if category is None:
            continue
        preferred[int(category)] = names
    return preferred


PREFERRED_TAG_TYPES = _build_preferred_tag_types()


# ---------------------------------------------------------------------------
# Pipe tag by elevation: High Level / Low Level
# ---------------------------------------------------------------------------
# Water-supply pipes are tagged HL or LL automatically, from how high the pipe
# sits above the floor plan's level (its centreline elevation minus the active
# plan view's level elevation): at or above ELEVATION_TAG_THRESHOLD_MM it gets
# the HL tag, below it the LL tag. No prompt for these pipes.
#
# This only touches pipes whose System Type (or classification) name contains
# one of ELEVATION_TAG_SYSTEMS (case-insensitive). Any other pipe - and every
# non-pipe - is untouched and still uses the normal single tag-type choice.
ELEVATION_TAG_ENABLED = True
ELEVATION_TAG_THRESHOLD_MM = 1500.0

# (family name, type name) of the tag to use on each side of the threshold.
# Either name may be None to match on the other alone; matched case-insensitively
# against the loaded tag families, exactly like PREFERRED_TAG_TYPES.
ELEVATION_TAG_HIGH = ('ME-Pipe Size Tag-HL', 'Pipe Size Tag-HL')   # >= threshold
ELEVATION_TAG_LOW = ('ME-Pipe Size Tag-LL', 'Pipe Size Tag-LL')    # <  threshold

# A pipe gets the HL/LL rule only when its System Type or classification name
# contains one of these (case-insensitive). Add your exact water-supply system
# type name(s) here to be precise; the default catches "... Water ..." names
# (Domestic Cold Water, Domestic Hot Water, ...). An empty list = every pipe.
ELEVATION_TAG_SYSTEMS = ['water']


# ---------------------------------------------------------------------------
# Riser tags: designation by floor and flow (plan views only)
# ---------------------------------------------------------------------------
# On a floor plan, a vertical pipe (riser) is tagged by where its run sits
# relative to the plan's level and which way the system flows:
#
#                          flow UP (supply)   flow DOWN (return)
#   run continues above        T/A                 F/A
#   run passes through        F/B - T/A           F/A - T/B
#   run comes from below       F/B                 T/B
#
# The run's true extent is walked across floors - through couplings, fittings
# and REDUCERS (risers shrink as they climb) - so a riser chopped into one
# segment per storey still reads as one run. Sections and elevations are
# untouched: this rule only fires in plan views.
RISER_TAG_ENABLED = True

# The run must extend at least this far (mm) past the plan's level to count
# as existing below / above that floor.
RISER_LEVEL_TOLERANCE_MM = 100.0

# Flow direction is NOT read from the system name (a return riser can run up,
# a supply branch can run down). It comes from the two picks: the risers you
# click in the "bottom to top" pick flow up, those in the "top to bottom" pick
# flow down. A vertical pipe you never pick as a riser gets no designation.

# The riser tag is ONE family/type; the designation (F/B, T/A, ...) is not a
# type but a value the tool WRITES into a text instance parameter on each placed
# tag, and the family's label shows it (System + that parameter). Per floor is
# automatic - each plan has its own tag holding its own value.
#
# PLACEHOLDER NAMES: set RISER_TAG_TYPE to the riser tag family/type actually
# loaded in your project, and RISER_DESIGNATION_PARAM to the exact instance
# parameter that holds the designation. If the tag is not loaded, risers fall
# back to the normal tag; if the parameter is missing/read-only, the tag places
# without the designation text (logged).
RISER_TAG_TYPE = ('ME-Pipe Riser Tag', 'Standard')
RISER_DESIGNATION_PARAM = 'Riser Designation'


# ---------------------------------------------------------------------------
# Auto Tag Pipes: one method, one tag family, the designation on the pipe
# ---------------------------------------------------------------------------
# The "Auto Tag Pipes" method reads each selected pipe's direction and routes
# it. Every pipe then WRITES its designation into the built-in Comments
# parameter, and a single tag family (Size + System Abbreviation + Comments)
# shows it - no per-pipe tag type, no tag parameter. Per storey is automatic:
# each sliced segment holds its own Comments and its own tag.
#
#   horizontal pipe  ->  by centreline height above the plan's floor:
#                          >= AUTO_HORIZONTAL_THRESHOLD_MM  ->  AUTO_HL
#                          below it                         ->  AUTO_LL
#
#   vertical riser   ->  by which sides of this floor the run reaches
#                        (walked across storeys) x the flow you pick:
#
#                          flow UP (supply)   flow DOWN (return)
#      run continues above       T/A                F/A
#      run passes through     F/B - T/A          F/A - T/B
#      run comes from below       F/B                T/B
#
# Change any wording here and every tag follows. Empty separators or blank
# codes are fine if a project spells them differently.
AUTO_TAG_ENABLED = True

# Horizontals, by height above the plan's floor.
AUTO_HORIZONTAL_THRESHOLD_MM = 1500.0
AUTO_HL = 'AT H/L'          # centreline at or above the threshold
AUTO_LL = 'AT L/L'          # below the threshold

# Risers, by run extent x flow. Passing-through joins the two codes with
# AUTO_RISER_THROUGH_SEP.
AUTO_RISER_THROUGH_SEP = ' - '
AUTO_RISER_UP_ABOVE = 'T/A'         # up,   run continues above this floor only
AUTO_RISER_UP_BELOW = 'F/B'         # up,   run comes from below this floor only
AUTO_RISER_DOWN_ABOVE = 'F/A'       # down, run continues above this floor only
AUTO_RISER_DOWN_BELOW = 'T/B'       # down, run comes from below this floor only


def auto_riser_through(flow_up):
    """Return the passing-through designation for an up-/down-flow riser."""
    if flow_up:
        return AUTO_RISER_UP_BELOW + AUTO_RISER_THROUGH_SEP + AUTO_RISER_UP_ABOVE
    return AUTO_RISER_DOWN_ABOVE + AUTO_RISER_THROUGH_SEP + AUTO_RISER_DOWN_BELOW


# How far apart (paper mm, scaled by view scale) the horizontal block and the
# riser block sit on the reference line, on top of the normal tag pitch.
AUTO_BLOCK_GAP_MM = 6.0


# ---------------------------------------------------------------------------
# Tag creation defaults
# ---------------------------------------------------------------------------
# Create new tags with a leader so they can be pulled clear of the element.
ADD_LEADER = True

# Horizontal tag text reads best on MEP drawings.
TAG_ORIENTATION = TagOrientation.Horizontal

# How far a NEW tag head is offset from its element, measured on paper (mm)
# and scaled by the view scale. Keeps the leader from starting at zero length.
# Existing tags are never nudged by this.
TAG_INITIAL_OFFSET_MM = 10.0


# ---------------------------------------------------------------------------
# Leader handling
# ---------------------------------------------------------------------------
# Leaders are refreshed by toggling them off and back on once the tags have
# moved, so Revit rebuilds each one cleanly from the tag's final position -
# exactly what happens when you uncheck/recheck the leader by hand. Revit owns
# the geometry; there is nothing to tune here (see leader_manager.py).
#
# The exception is horizontal pipes (below), where the tool owns the geometry
# so the leader can turn 90 degrees down to the pipe.


# ---------------------------------------------------------------------------
# Horizontal-pipe leaders (L-shaped / 90-degree)
# ---------------------------------------------------------------------------
# When the tagged pipes run horizontally in the view, a level leader would sit
# on top of the pipe. Instead the tags stack in a column on the reference line
# and each leader turns 90 degrees: a horizontal landing from the tag, then a
# vertical drop to the pipe. Each drop lands at the MIDDLE of its own pipe
# segment; where segments share a middle (a parallel bundle) the drops fan
# apart, centred on that middle, so they never stack on one line.
#
# Both distances are paper (mm), scaled by the view scale.
HORIZONTAL_LEADER_STEP_MM = 6.0    # fan spacing between drops that share a middle
HORIZONTAL_LEADER_CLEAR_MM = 2.0   # keep the drop this far inside the pipe ends

# Leader end condition for the horizontal L-leaders:
#   False -> Attached: Revit slides the arrow along the pipe to sit under the
#            elbow; the leader stays linked and auto-follows if the pipe moves.
#            This is the default and matches dragging the grip by hand.
#   True  -> Free end: the tool sets the arrow point explicitly. Guarantees the
#            clean L on any Revit build, but the arrow will not follow later
#            pipe moves. Flip to True only if Attached misbehaves on a version.
HORIZONTAL_LEADER_FREE_END = False


# ---------------------------------------------------------------------------
# Tag spacing (stops stacked tags from overlapping and becoming unreadable)
# ---------------------------------------------------------------------------
# The tool measures each tag's real size and spaces them by
# (tallest tag + TAG_GAP_MM). MIN_TAG_PITCH_MM is the floor applied when that
# measurement is unavailable or very small. Both are paper (mm), scaled by
# the view scale. TAG_GAP_MM is the clear whitespace between two text blocks -
# raise it for airier stacks, lower it for tighter ones.
MIN_TAG_PITCH_MM = 4.0
TAG_GAP_MM = 1.0

# Pitch used ONLY when no tag in the run could be measured (Revit returned no
# bounding box for any of them). MIN_TAG_PITCH_MM (4 mm) suits a single-line
# Size tag, but the Auto method's tag is multi-line (Size + System Abbreviation
# + Comments, ~2-3 lines), so a 4 mm fallback lets those tall tags overprint
# into an unreadable pile. This taller fallback clears a multi-line label. When
# measurement DOES work (the normal case) the measured height wins instead.
FALLBACK_TAG_PITCH_MM = 9.0

# Order a stacked column by the ELEMENTS' left-to-right position rather than
# by wherever the tags happen to sit: the left-most element's tag goes on top,
# the right-most at the bottom. Falls back to the tags' own order if an
# element cannot be located.
ORDER_STACK_BY_ELEMENT = True


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
# Moves smaller than this (feet) are treated as "already aligned".
POSITION_TOLERANCE = 1e-9
