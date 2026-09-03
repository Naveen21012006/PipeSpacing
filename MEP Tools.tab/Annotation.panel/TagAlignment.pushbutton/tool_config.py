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
# Auto Tag Pipes: one method, one tag family, nothing written to the model
# ---------------------------------------------------------------------------
# The "Auto Tag Pipes" method reads each selected pipe's direction only to
# choose its LEADER treatment; a single tag family serves every pipe.
#
# The tools no longer write ANYTHING to the tagged elements. Two features
# were removed on the user's orders, in sequence:
#   2026-09-01  riser designations (F/B, T/A, ...) went with the flow prompt
#               that was their only input.
#   2026-09-03  the AT H/L / AT L/L Comments write went entirely ("you should
#               not add any words in the comment section"). AUTO_TAG_ENABLED,
#               AUTO_HORIZONTAL_THRESHOLD_MM, AUTO_HL and AUTO_LL left with
#               it - dead knobs are traps, not options.
#
# Values written by runs BEFORE 2026-09-03 remain in those pipes' Comments
# (and on their tags) until cleared by hand; the tools will neither refresh
# nor remove them.

# How far apart (paper mm, scaled by view scale) the horizontal block and the
# riser block sit on the reference line, on top of the normal tag pitch.
AUTO_BLOCK_GAP_MM = 6.0

# Write a diagnostic log of every Auto Tag run to
# %APPDATA%\CKR\logs\autotag.log: the row decisions, the leader geometry, and
# a self-check that counts leader crossings. Cheap, and it turns "the tags look
# tangled" into a named fault with coordinates. Set False to switch it off.
AUTO_LOG_ENABLED = True

# After working out an arrangement the Auto method AUDITS it against the same
# geometric crossing test the log publishes. If any leader crosses another, the
# column is allowed to rise ABOVE the drawn reference line, one row at a time,
# and the FIRST height that draws no crossings is kept - the smallest lift that
# works. If none is clean the best of them is kept, so lifting can never make
# the drawing worse than not lifting.
#
# The lift exists because a 90-degree tag has to sit above its pipe for the
# arrow to point down at it. Click the line too close above the pipework and
# the drop tags that do not fit are pushed below the straight-leader block,
# where every arrow must climb back through every straight leader: on the
# 2026-09-01 run a line 493mm above the pipes held one drop tag of four and
# drew 9 crossings, and no drop position could have avoided them - those pipes
# were too short to reach past the straight leaders' arrows. Headroom was the
# only cure, so the tool now finds it instead of reporting the damage.
#
# This is a count of ROW PITCHES. 0 restores the old hard wall (the column
# never leaves the line, and crossings are reported instead of repaired).
AUTO_CEILING_LIFT_ROWS = 8

# Minimum VISIBLE length of a 90-degree leader's drop, in row-pitches. A tag
# seated almost level with its own pipe draws a drop too short to read (the
# arrow crowds the pipe line); the reframe pass re-seats such tags onto the
# nearest row a full drop away - which also spends any vacant stretch of the
# column. 1.0 = one row (the approved default); 0 disables the rule.
AUTO_MIN_DROP_ROWS = 1.0

# Default clear gap between two texts, in PAPER millimetres (scale-true), used
# when no Shift+click override is set (user's chosen value, 2026-08-04). A
# true 0 would let a fractional measurement error touch the line above; the
# Shift+click presets still reach down to 0.5mm and up to 5mm.
AUTO_DEFAULT_GAP_PAPER_MM = 0.75

# Take the drawn text height from the tag TYPE's text size rather than Revit's
# bounding box. The box includes padding the drawing never shows - on the
# user's family it measured 4.75mm on paper where the text is 2.5mm, so the
# pitch carried ~2mm of paper air that no gap setting could remove. Set False
# to fall back to the measured box.
AUTO_TEXT_HEIGHT_FROM_TYPE = True

# The tag label's TEXT SIZE in PAPER millimetres - the height of one printed
# line. Revit keeps this on the "Tag Label" sub-element inside the family, and
# that is NOT reachable from the placed tag at runtime (the 2026-08-04 log
# proved it: "text height 1187mm model (measured)"), so it is stated here.
# Read it off the tag's Type Properties > Text > Text Size. None = try the
# runtime lookup and fall back to Revit's padded bounding box.
AUTO_TEXT_SIZE_PAPER_MM = 2.0

# Drawn line height as a multiple of that text size. "Text Size" is the
# NOMINAL font size; the glyphs actually drawn are taller - a slashed diameter
# symbol and the "/" in H/L reach above and below the nominal box - so a 2mm
# font paints roughly 2.7mm of line. Taking the size at face value made the
# rows collide (2026-08-04). Raise this if text still touches; lower it for a
# tighter column.
AUTO_TEXT_LINE_FACTOR = 1.35

# Vision. Before placing, the Auto method exports the view (with its own tag
# categories hidden) and rasterises it into an INK MAP - where the paper
# already has dimensions, text, walls, unselected pipework. Row scoring then
# prefers blank paper, so tags spread into genuinely empty areas and stay off
# annotation the geometric model has never seen. After every run the tagged
# result is also exported to %APPDATA%\CKR\logs\autotag.png, next to the log,
# so a reviewer sees the drawn outcome without screenshots.
AUTO_SNAPSHOT_ENABLED = True

# How strongly ink repels a tag's text: a fully-inked row position costs the
# same as being this many rows further from the pipe. 0 disables the ink term
# while keeping the snapshots.
AUTO_INK_WEIGHT_ROWS = 3.0

# How strongly a horizontal cluster is held to the side it chose, in row
# pitches. After the vertical clusters are placed (they always go first and
# keep their straight leaders), each horizontal cluster picks the side of its
# own pipes whose drops would pass the fewest already-placed rows - so its
# leaders stay out of the vertical band instead of running its whole length.
# This is a BIAS, not a wall: strong geometry can still win. 0 disables it.
AUTO_CLUSTER_SIDE_BIAS_ROWS = 6.0


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
# This inset is charged TWICE against every pipe - once at each end - so it is
# the dominant cost on short runs. At 2.0 it took 4 mm off pipes only 11 mm long
# on the user's 2026-09-01 bundle, leaving room for three rows where four were
# wanted, and the fourth tag lost its straight leader for no reason but this
# constant. 0.5 keeps the arrow off the very end of the pipe while leaving short
# runs enough window to seat a full stack.
HORIZONTAL_LEADER_CLEAR_MM = 0.5   # keep the drop this far inside the pipe ends

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
