# -*- coding: utf-8 -*-
"""Unit tests for the Tag Linked Services core layer (brief deliverable 10.4).

Covers classification at the boundary angles, clipping against the view
range and the crop region, and the paper-to-model conversion - the three
things the brief calls out, plus the placement and spacing rules that
depend on them.

None of this needs Revit: core.py and config.py hold no Revit API import,
which is exactly why the acceptance criteria can be checked here rather
than only on a model.
"""

import math

import pytest

from ckr_taglinked import config, core

FT = core.MM_PER_FOOT


def mm(value):
    """Millimetres as internal units, for readable expectations."""
    return core.mm_to_feet(value)


def point(x_mm, y_mm, z_mm):
    return (mm(x_mm), mm(y_mm), mm(z_mm))


# ---------------------------------------------------------------------------
# Clause 7.3 - classification
# ---------------------------------------------------------------------------
class TestClassification(object):

    def test_flat_run_is_horizontal(self):
        assert core.classify((1.0, 0.0, 0.0))[0] == core.HORIZONTAL

    def test_drainage_gradient_is_horizontal(self):
        """AT-02: a 6 m soil pipe at 1:100 is horizontal, not inclined.

        This is the primary regression test for clause 5.1 - a
        direction.Z == 0 test rejects every drainage run in the model.
        """
        run = (6.0, 0.0, 0.06)                       # 1:100
        classification, angle = core.classify(run)
        assert classification == core.HORIZONTAL
        assert angle < 1.0

    def test_shallow_gradients_stay_horizontal(self):
        for gradient in (1 / 100.0, 1 / 80.0, 1 / 60.0, 1 / 40.0):
            run = (1.0, 0.0, gradient)
            assert core.classify(run)[0] == core.HORIZONTAL

    def test_boundary_angles(self):
        just_under = math.tan(math.radians(14.9))
        just_over = math.tan(math.radians(15.1))
        assert core.classify((1.0, 0.0, just_under))[0] == core.HORIZONTAL
        assert core.classify((1.0, 0.0, just_over))[0] == core.INCLINED

    def test_vertical_boundary(self):
        # 75 degrees exactly is vertical; a shade under is inclined.
        steep = (math.cos(math.radians(75.0)), 0.0,
                 math.sin(math.radians(75.0)))
        shallow = (math.cos(math.radians(74.9)), 0.0,
                   math.sin(math.radians(74.9)))
        assert core.classify(steep)[0] == core.VERTICAL
        assert core.classify(shallow)[0] == core.INCLINED

    def test_true_riser_is_vertical(self):
        assert core.classify((0.0, 0.0, -3.2))[0] == core.VERTICAL

    def test_direction_sign_does_not_matter(self):
        assert core.classify((0.0, 0.0, 5.0))[0] == \
            core.classify((0.0, 0.0, -5.0))[0]

    def test_custom_tolerances_are_honoured(self):
        run = (1.0, 0.0, math.tan(math.radians(20.0)))
        assert core.classify(run)[0] == core.INCLINED
        assert core.classify(run, horizontal_tol_deg=25.0)[0] == \
            core.HORIZONTAL

    def test_zero_length_is_unclassified(self):
        assert core.classify((0.0, 0.0, 0.0)) == (None, None)


# ---------------------------------------------------------------------------
# Clause 5.4 / 7.6.3 - paper to model
# ---------------------------------------------------------------------------
class TestPaperSpace(object):

    def test_offset_scales_with_the_view(self):
        """AT-07: 3 mm on paper is 150 mm of model at 1:50, 600 at 1:200."""
        assert core.feet_to_mm(core.paper_mm_to_feet(3.0, 50)) == \
            pytest.approx(150.0)
        assert core.feet_to_mm(core.paper_mm_to_feet(3.0, 200)) == \
            pytest.approx(600.0)

    def test_round_trip(self):
        assert core.feet_to_mm(core.mm_to_feet(1234.5)) == \
            pytest.approx(1234.5)

    def test_missing_scale_falls_back_to_one_to_one(self):
        assert core.paper_mm_to_feet(5.0, 0) == pytest.approx(mm(5.0))


# ---------------------------------------------------------------------------
# Clause 7.4 - the view range clip
# ---------------------------------------------------------------------------
class TestBandClip(object):

    def test_riser_through_a_storey_reports_the_storey(self):
        """AT-03: a 30 m riser is Lv per view, never 30 m."""
        riser = (point(0, 0, 0), point(0, 0, 30000))
        clipped = core.clip_to_band(riser, mm(3000), mm(6000))
        assert core.feet_to_mm(core.segment_length(clipped)) == \
            pytest.approx(3000.0)

    def test_riser_per_storey_reports_the_visible_part(self):
        """AT-04: a 3.2 m riser inside the range reports the part in it."""
        riser = (point(0, 0, 2800), point(0, 0, 6000))
        clipped = core.clip_to_band(riser, mm(3000), mm(6000))
        assert core.feet_to_mm(core.segment_length(clipped)) == \
            pytest.approx(3000.0)

    def test_flat_run_inside_survives_whole(self):
        run = (point(0, 0, 4000), point(6000, 0, 4000))
        clipped = core.clip_to_band(run, mm(3000), mm(6000))
        assert core.feet_to_mm(core.segment_length(clipped)) == \
            pytest.approx(6000.0)

    def test_flat_run_outside_is_rejected(self):
        run = (point(0, 0, 9000), point(6000, 0, 9000))
        assert core.clip_to_band(run, mm(3000), mm(6000)) is None

    def test_run_wholly_above_is_rejected(self):
        run = (point(0, 0, 7000), point(0, 0, 9000))
        assert core.clip_to_band(run, mm(3000), mm(6000)) is None

    def test_unlimited_planes_keep_everything(self):
        run = (point(0, 0, -50000), point(0, 0, 50000))
        clipped = core.clip_to_band(run, float('-inf'), float('inf'))
        assert clipped == run

    def test_drainage_run_keeps_its_fall(self):
        run = (point(0, 0, 4000), point(6000, 0, 3940))
        clipped = core.clip_to_band(run, mm(3000), mm(6000))
        assert clipped == run


# ---------------------------------------------------------------------------
# Clause 7.4.3 - the crop region clip
# ---------------------------------------------------------------------------
SQUARE = [[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]]

#: An L-shaped crop, to prove the clipper is not assuming convexity.
L_SHAPE = [[(0.0, 0.0), (10.0, 0.0), (10.0, 4.0), (4.0, 4.0),
            (4.0, 10.0), (0.0, 10.0)]]


class TestCropClip(object):

    def test_segment_inside_survives_whole(self):
        segment = ((2.0, 2.0, 0.0), (8.0, 2.0, 0.0))
        assert core.clip_to_loops(segment, SQUARE) == [segment]

    def test_segment_crossing_the_boundary_is_trimmed(self):
        segment = ((-5.0, 5.0, 0.0), (5.0, 5.0, 0.0))
        pieces = core.clip_to_loops(segment, SQUARE)
        assert len(pieces) == 1
        assert core.segment_length(pieces[0]) == pytest.approx(5.0)

    def test_segment_outside_is_dropped(self):
        segment = ((-5.0, 20.0, 0.0), (5.0, 20.0, 0.0))
        assert core.clip_to_loops(segment, SQUARE) == []

    def test_no_crop_means_no_clip(self):
        segment = ((-100.0, 5.0, 0.0), (100.0, 5.0, 0.0))
        assert core.clip_to_loops(segment, []) == [segment]

    def test_concave_crop_clips_to_its_narrow_part(self):
        """Above the notch the L is only 4 wide, and the clip knows it."""
        segment = ((-2.0, 7.0, 0.0), (12.0, 7.0, 0.0))
        pieces = core.clip_to_loops(segment, L_SHAPE)
        assert len(pieces) == 1
        assert core.segment_length(pieces[0]) == pytest.approx(4.0)

    def test_two_loops_are_a_union(self):
        loops = [SQUARE[0], [(20.0, 0.0), (30.0, 0.0), (30.0, 10.0),
                             (20.0, 10.0)]]
        segment = ((-5.0, 5.0, 0.0), (35.0, 5.0, 0.0))
        pieces = core.clip_to_loops(segment, loops)
        assert len(pieces) == 2
        assert core.segment_length(core.longest_segment(pieces)) == \
            pytest.approx(10.0)

    def test_visible_segment_combines_both_clips(self):
        run = (point(-2000, 5000, 4000), point(20000, 5000, 4000))
        loops = [[(0.0, 0.0), (mm(10000), 0.0), (mm(10000), mm(10000)),
                  (0.0, mm(10000))]]
        segment = core.visible_segment(run, mm(3000), mm(6000), loops)
        assert core.feet_to_mm(core.segment_length(segment)) == \
            pytest.approx(10000.0)

    def test_visible_segment_rejects_outside_the_range(self):
        run = (point(0, 5000, 9000), point(9000, 5000, 9000))
        loops = [[(0.0, 0.0), (mm(10000), 0.0), (mm(10000), mm(10000)),
                  (0.0, mm(10000))]]
        assert core.visible_segment(run, mm(3000), mm(6000), loops) is None


# ---------------------------------------------------------------------------
# Clause 7.6 - placement
# ---------------------------------------------------------------------------
class TestPlacement(object):

    def test_horizontal_tag_sits_beside_the_midpoint(self):
        """Clause 7.6.5: the midpoint of the CLIPPED piece, not the run."""
        segment = ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0))
        first = core.placement_candidates(segment, 1.0)[0]
        assert first[0] == pytest.approx(5.0)
        assert abs(first[1]) == pytest.approx(1.0)
        assert first[2] == pytest.approx(0.0)

    def test_candidates_try_the_other_side_last(self):
        segment = ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0))
        points = core.placement_candidates(segment, 1.0)
        assert len(points) == 6
        assert [p[0] for p in points[:3]] == \
            pytest.approx([5.0, 2.5, 7.5])
        # The first three sit one side of the run, the last three the other.
        assert points[0][1] == pytest.approx(-points[3][1])

    def test_offset_is_perpendicular_to_the_run(self):
        segment = ((0.0, 0.0, 0.0), (0.0, 10.0, 0.0))
        first = core.placement_candidates(segment, 2.0)[0]
        assert first[1] == pytest.approx(5.0)
        assert abs(first[0]) == pytest.approx(2.0)

    def test_a_riser_gets_diagonal_candidates(self):
        segment = ((3.0, 4.0, 0.0), (3.0, 4.0, 10.0))
        points = core.placement_candidates(segment, 2.0)
        assert len(points) == 4
        for candidate in points:
            offset = math.hypot(candidate[0] - 3.0, candidate[1] - 4.0)
            assert offset == pytest.approx(2.0, abs=1e-6)

    def test_riser_candidates_are_distinct(self):
        points = core.riser_candidates((0.0, 0.0, 0.0), 1.0)
        assert len(set(points)) == 4


class TestCropClamp(object):

    def test_a_point_inside_is_left_alone(self):
        inside = (5.0, 5.0, 0.0)
        assert core.clamp_into_region(inside, SQUARE, 0.5) is inside

    def test_a_point_outside_is_pulled_in(self):
        """Clause 5.3: a head outside the annotation crop is not drawn."""
        clamped = core.clamp_into_region((15.0, 5.0, 3.0), SQUARE, 0.5)
        assert core.point_in_loops((clamped[0], clamped[1]), SQUARE)
        assert clamped[2] == pytest.approx(3.0)   # elevation is preserved

    def test_a_point_on_the_edge_is_pushed_clear(self):
        clamped = core.clamp_into_region((10.0, 5.0, 0.0), SQUARE, 1.0)
        assert clamped[0] < 10.0
        assert core.point_in_loops((clamped[0], clamped[1]), SQUARE)

    def test_no_crop_leaves_the_point_alone(self):
        candidate = (999.0, 999.0, 0.0)
        assert core.clamp_into_region(candidate, [], 1.0) is candidate

    def test_centroid_of_a_square(self):
        assert core.loop_centroid(SQUARE[0]) == pytest.approx((5.0, 5.0))


class TestSpacing(object):

    def test_separated_rectangles_are_clear(self):
        a = (0.0, 0.0, 10.0, 4.0)
        b = (12.0, 0.0, 20.0, 4.0)
        assert core.rects_clear(a, b, 1.0)
        assert not core.rects_clear(a, b, 5.0)

    def test_overlapping_rectangles_are_never_clear(self):
        a = (0.0, 0.0, 10.0, 4.0)
        b = (5.0, 1.0, 15.0, 3.0)
        assert not core.rects_clear(a, b, 0.0)

    def test_vertical_separation_is_enough(self):
        a = (0.0, 0.0, 10.0, 4.0)
        b = (0.0, 9.0, 10.0, 13.0)
        assert core.rects_clear(a, b, 5.0)

    def test_conflict_against_a_set(self):
        placed = [(0.0, 0.0, 10.0, 4.0), (30.0, 0.0, 40.0, 4.0)]
        assert core.rect_conflicts((11.0, 0.0, 20.0, 4.0), placed, 5.0)
        assert not core.rect_conflicts((16.0, 0.0, 24.0, 4.0), placed, 5.0)

    def test_gap_is_negative_when_boxes_overlap(self):
        a = (0.0, 0.0, 10.0, 4.0)
        b = (5.0, 1.0, 15.0, 3.0)
        assert core.rect_gap(a, b) < 0.0

    def test_min_gap_finds_the_tightest_neighbour(self):
        placed = [(0.0, 0.0, 10.0, 4.0), (30.0, 0.0, 40.0, 4.0)]
        assert core.min_gap((14.0, 0.0, 20.0, 4.0), placed) == \
            pytest.approx(4.0)

    def test_min_gap_of_an_empty_view_is_infinite(self):
        assert core.min_gap((0.0, 0.0, 1.0, 1.0), []) == float('inf')


class TestLengthRule(object):

    def test_minimums_apply_per_classification(self):
        minimums = {core.HORIZONTAL: mm(3000), core.VERTICAL: mm(2000)}
        assert core.passes_length(core.HORIZONTAL, mm(3200), minimums)
        assert not core.passes_length(core.HORIZONTAL, mm(2900), minimums)
        assert core.passes_length(core.VERTICAL, mm(2100), minimums)

    def test_exactly_the_minimum_passes(self):
        minimums = {core.HORIZONTAL: mm(3000)}
        assert core.passes_length(core.HORIZONTAL, mm(3000), minimums)

    def test_an_absent_minimum_never_rejects(self):
        assert core.passes_length(core.INCLINED, 0.001, {})


# ---------------------------------------------------------------------------
# The settings schema
# ---------------------------------------------------------------------------
class TestConfig(object):

    def test_defaults_match_the_brief(self):
        values = config.defaults()
        assert values['min_horizontal_mm'] == 3000.0
        assert values['min_vertical_mm'] == 2000.0
        assert values['min_inclined_mm'] == 3000.0
        assert values['include_inclined'] is False
        assert values['horizontal_tol_deg'] == 15.0
        assert values['vertical_tol_deg'] == 75.0
        assert values['offset_horizontal_mm'] == 3.0
        assert values['offset_vertical_mm'] == 8.0
        assert values['spacing_mm'] == 5.0
        assert values['skip_tagged'] is True

    def test_rubbish_falls_back_to_defaults(self):
        assert config.normalise(None) == config.defaults()
        assert config.normalise('not a profile') == config.defaults()

    def test_partial_profiles_keep_their_values(self):
        values = config.normalise({'min_vertical_mm': 2500,
                                   'skip_tagged': False})
        assert values['min_vertical_mm'] == 2500.0
        assert values['skip_tagged'] is False
        assert values['min_horizontal_mm'] == 3000.0

    def test_bad_types_do_not_poison_the_rest(self):
        values = config.normalise({'min_horizontal_mm': 'wide',
                                   'min_vertical_mm': 1500})
        assert values['min_horizontal_mm'] == 3000.0
        assert values['min_vertical_mm'] == 1500.0

    def test_crossed_tolerances_are_reset(self):
        values = config.normalise({'horizontal_tol_deg': 80.0,
                                   'vertical_tol_deg': 20.0})
        assert values['horizontal_tol_deg'] == 15.0
        assert values['vertical_tol_deg'] == 75.0

    def test_blank_size_range_means_no_limit(self):
        values = config.normalise({'size_from_mm': '', 'size_to_mm': None})
        assert values['size_from_mm'] is None
        assert values['size_to_mm'] is None

    def test_tag_selection_survives_a_round_trip(self):
        raw = {'tags': {'pipes': {'horizontal': 'Pipe Tag : Standard',
                                  'vertical': 'Riser Tag : Small',
                                  'leader': False,
                                  'orientation': 'model'}}}
        values = config.normalise(raw)
        assert values['tags']['pipes']['horizontal'] == 'Pipe Tag : Standard'
        assert values['tags']['pipes']['vertical'] == 'Riser Tag : Small'
        assert values['tags']['pipes']['leader'] is False
        assert values['tags']['pipes']['orientation'] == 'model'

    def test_unknown_orientation_falls_back(self):
        values = config.normalise({'tags': {'pipes': {
            'orientation': 'sideways'}}})
        assert values['tags']['pipes']['orientation'] == 'horizontal'

    def test_minimums_convert_to_feet(self):
        minimums = config.minimums_in_feet(config.defaults())
        assert core.feet_to_mm(minimums[core.HORIZONTAL]) == \
            pytest.approx(3000.0)
        assert core.feet_to_mm(minimums[core.VERTICAL]) == \
            pytest.approx(2000.0)

    def test_included_categories(self):
        values = config.defaults()
        assert config.included_categories(values) == ['pipes']
        values['categories']['trays'] = True
        assert config.included_categories(values) == ['pipes', 'trays']
