# -*- coding: utf-8 -*-
"""Tag discovery and creation for the MEP Tag Alignment tool.

TagManager guarantees the post-condition the workflow depends on: after
ensure_tags(), every supported element has exactly one usable tag in the
active view. Existing tags are reused as-is - never deleted or recreated.

All document modification happens in the caller's transaction.
"""

from collections import OrderedDict

from Autodesk.Revit.DB import (
    BuiltInParameter,
    FamilySymbol,
    FilteredElementCollector,
    IndependentTag,
)
from Autodesk.Revit.DB.Plumbing import Pipe

import config
import runs
import utils


def _level_elevation(level):
    """Return a level's elevation in INTERNAL coordinates (feet).

    Level.Elevation follows the level type's "Elevation Base" (it can read
    from the Survey Point on coordinated jobs), while pipe geometry is always
    internal coordinates - comparing the two silently misplaces every
    threshold by the datum offset. ProjectElevation is the internal-coordinate
    value, so it is used whenever available.
    """
    try:
        return level.ProjectElevation
    except AttributeError:
        return level.Elevation


class TagManager(object):
    """Finds existing tags and creates the missing ones."""

    def __init__(self, doc, view):
        """
        Args:
            doc: The active Document.
            view: The view tags live in.
        """
        self.doc = doc
        self.view = view
        self._tag_type_cache = {}          # tag category int -> ElementId
        self._elevation_cache = {}         # 'high'/'low' -> ElementId
        self._riser_type_cache = {}        # 'riser' -> ElementId | None
        self._riser_designations = {}      # pipe id -> designation | None
        self._riser_up_ids = None          # picked up-flow pipe ids, or None
        self._riser_down_ids = None        # picked down-flow pipe ids, or None
        self._riser_param_warned = False   # warn once if the param is missing
        self._comments_mode = False        # Auto method: designation -> Comments
        self._existing = self._index_existing_tags()
        self._right, _up = utils.get_view_axes(view)
        self._initial_offset = utils.paper_mm_to_model(
            view, config.TAG_INITIAL_OFFSET_MM)

    # -- discovery ---------------------------------------------------------
    def _index_existing_tags(self):
        """Map tagged-element id -> IndependentTag, for tags in this view."""
        index = {}
        collector = (FilteredElementCollector(self.doc, self.view.Id)
                     .OfClass(IndependentTag)
                     .WhereElementIsNotElementType())

        for tag in collector:
            for tagged_id in self._tagged_element_ids(tag):
                # First tag found wins; we never create a second one.
                index.setdefault(tagged_id, tag)

        utils.logger.debug('Indexed {} already-tagged element(s).'.format(
            len(index)))
        return index

    @staticmethod
    def _tagged_element_ids(tag):
        """Return the ids of the local elements a tag points at.

        Tolerates both the modern GetTaggedLocalElementIds() and the older
        TaggedLocalElementId property.
        """
        try:
            return [utils.element_id_value(element_id)
                    for element_id in tag.GetTaggedLocalElementIds()]
        except AttributeError:
            try:
                return [utils.element_id_value(tag.TaggedLocalElementId)]
            except Exception:
                return []
        except Exception:
            return []

    def find_existing_tag(self, element):
        """Return the tag already annotating this element, or None."""
        return self._existing.get(utils.element_id_value(element.Id))

    # -- Auto (Comments) mode ----------------------------------------------
    def set_comments_mode(self, enabled):
        """Switch the Auto method's Comments behaviour on or off.

        When on, the designation (F/B, AT H/L, ...) is WRITTEN into each pipe's
        built-in Comments parameter and one ordinary tag family shows it - so
        the per-pipe tag-type switching (riser tag, HL/LL tag) is turned off and
        every pipe uses the single chosen pipe tag. When off, the tool keeps its
        original tag-type + tag-parameter behaviour.
        """
        self._comments_mode = bool(enabled)

    # -- tag types ---------------------------------------------------------
    @staticmethod
    def _symbol_matches(symbol, family_name, type_name):
        """True if a tag symbol matches the configured family/type names.

        A name of None matches anything, so a family alone or a type alone is
        enough to pin a tag down. Matching is case-insensitive.
        """
        if family_name:
            actual = utils.get_family_name(symbol)
            if actual.strip().lower() != family_name.strip().lower():
                return False
        if type_name:
            actual = utils.get_element_name(symbol)
            if actual.strip().lower() != type_name.strip().lower():
                return False
        return True

    def _get_tag_type_id(self, element):
        """Return the tag FamilySymbol id to use for this element's category.

        Honours config.PREFERRED_TAG_TYPES. Only when the category is not
        pinned (or the pinned tag is not loaded) does it fall back to the first
        tag type found - and it says so loudly, because "first found" is
        arbitrary and is how a water pipe ends up wearing a fire-pipe tag.

        Cached per element category.
        """
        category_value = utils.get_category_id_value(element)
        tag_category = config.SUPPORTED_CATEGORIES.get(category_value)
        if tag_category is None:
            return None

        # Risers on a plan get a designation tag (T/A, F/B, ...), and
        # water-supply pipes are typed per pipe by elevation (HL / LL) -
        # neither is cached per category. The riser rule is the more specific
        # one, so it goes first; everything else falls through to the normal
        # one-type-per-category selection below.
        riser_id = self._riser_tag_type_id(element, tag_category)
        if riser_id is not None:
            return riser_id

        elevation_id = self._elevation_tag_type_id(element, tag_category)
        if elevation_id is not None:
            return elevation_id

        if category_value in self._tag_type_cache:
            return self._tag_type_cache[category_value]

        symbols = list(FilteredElementCollector(self.doc)
                       .OfClass(FamilySymbol)
                       .OfCategory(tag_category)
                       .ToElements())

        symbol_id = None
        preferred = config.PREFERRED_TAG_TYPES.get(category_value)

        if preferred:
            family_name, type_name = preferred
            for symbol in symbols:
                if self._symbol_matches(symbol, family_name, type_name):
                    symbol_id = symbol.Id
                    break
            if symbol_id is None:
                utils.logger.warning(
                    'Preferred tag "{} : {}" is not loaded in this project; '
                    'falling back to the first {} found.'.format(
                        family_name, type_name,
                        utils.get_category_name(element)))

        if symbol_id is None and symbols:
            fallback = symbols[0]
            symbol_id = fallback.Id
            utils.logger.warning(
                'No preferred tag pinned for {} - using "{} : {}", which is '
                'whichever Revit returned first. Pin the one you want in '
                'config.PREFERRED_TAG_TYPES.'.format(
                    utils.get_category_name(element),
                    utils.get_family_name(fallback),
                    utils.get_element_name(fallback)))

        self._tag_type_cache[category_value] = symbol_id
        return symbol_id

    def _activate(self, tag_type_id):
        """Ensure a tag symbol is active before it is placed."""
        symbol = self.doc.GetElement(tag_type_id)
        if symbol is None:
            return
        try:
            if not symbol.IsActive:
                symbol.Activate()
                self.doc.Regenerate()
        except Exception as ex:
            utils.logger.debug('Could not activate tag type: {}'.format(ex))

    # -- elevation-based pipe tags (High Level / Low Level) ----------------
    def _pipe_system_names(self, pipe):
        """Return the pipe's system type and classification names."""
        names = []
        try:
            system = pipe.MEPSystem
            if system is not None:
                system_type = self.doc.GetElement(system.GetTypeId())
                if system_type is not None:
                    name = utils.get_element_name(system_type)
                    if name:
                        names.append(name)
        except Exception:
            pass

        for parameter_name in ('RBS_PIPING_SYSTEM_TYPE_PARAM',
                               'RBS_SYSTEM_CLASSIFICATION_PARAM'):
            parameter_id = getattr(BuiltInParameter, parameter_name, None)
            if parameter_id is None:
                continue
            try:
                parameter = pipe.get_Parameter(parameter_id)
                if parameter is not None:
                    value = parameter.AsValueString()
                    if value:
                        names.append(value)
            except Exception:
                pass
        return names

    def _is_target_system(self, pipe):
        """True if the elevation (HL/LL) rule applies to this pipe's system."""
        targets = [text.strip().lower()
                   for text in config.ELEVATION_TAG_SYSTEMS if text.strip()]
        if not targets:
            return True  # empty list -> every pipe
        for name in self._pipe_system_names(pipe):
            lowered = name.lower()
            if any(target in lowered for target in targets):
                return True
        return False

    def _floor_elevation(self, pipe):
        """Return the elevation to measure the pipe's height against (feet).

        The active plan view's level when there is one; otherwise the pipe's
        own reference level; failing both, zero.
        """
        try:
            level = self.view.GenLevel
            if level is not None:
                return _level_elevation(level)
        except Exception:
            pass
        try:
            parameter = pipe.get_Parameter(BuiltInParameter.RBS_START_LEVEL_PARAM)
            if parameter is not None:
                level = self.doc.GetElement(parameter.AsElementId())
                if level is not None:
                    return _level_elevation(level)
        except Exception:
            pass
        return 0.0

    def _pipe_height_above_floor(self, pipe):
        """Return the pipe centreline height above the floor (feet), or None."""
        try:
            point = pipe.Location.Curve.Evaluate(0.5, True)
        except Exception:
            return None
        return point.Z - self._floor_elevation(pipe)

    def _find_symbol(self, tag_category, family_name, type_name):
        """Return the id of the loaded tag symbol matching the names, or None."""
        for symbol in (FilteredElementCollector(self.doc)
                       .OfClass(FamilySymbol)
                       .OfCategory(tag_category)
                       .ToElements()):
            if self._symbol_matches(symbol, family_name, type_name):
                return symbol.Id
        return None

    def _elevation_symbol_id(self, tag_category, high):
        """Return the HL (high=True) or LL tag symbol id, resolved once."""
        key = 'high' if high else 'low'
        if key in self._elevation_cache:
            return self._elevation_cache[key]

        family_name, type_name = (config.ELEVATION_TAG_HIGH if high
                                  else config.ELEVATION_TAG_LOW)
        symbol_id = self._find_symbol(tag_category, family_name, type_name)
        if symbol_id is None:
            utils.logger.warning(
                'Elevation tag "{} : {}" is not loaded in this project; that '
                'pipe falls back to the normal tag type.'.format(
                    family_name, type_name))
        self._elevation_cache[key] = symbol_id
        return symbol_id

    def _elevation_applies(self, element):
        """True if the HL/LL elevation rule governs this element's tag type."""
        return (not self._comments_mode
                and config.ELEVATION_TAG_ENABLED
                and isinstance(element, Pipe)
                and self._is_target_system(element))

    def _elevation_tag_type_id(self, element, tag_category):
        """Return the HL/LL tag symbol id for a water-supply pipe, else None.

        High Level at or above config.ELEVATION_TAG_THRESHOLD_MM, Low Level
        below it. Returns None for anything the rule does not cover, or when
        the pipe's height or the HL/LL family cannot be resolved - the caller
        then uses the normal one-type-per-category selection.
        """
        if not self._elevation_applies(element):
            return None
        height = self._pipe_height_above_floor(element)
        if height is None:
            return None
        threshold = utils.mm_to_feet(config.ELEVATION_TAG_THRESHOLD_MM)
        return self._elevation_symbol_id(tag_category, height >= threshold)

    # -- riser designation tags (plan views only) ---------------------------
    def _plan_level_elevation(self):
        """Return the active plan view's level elevation (feet), or None.

        None in sections/elevations/3D, which switches the riser rule off
        there - vertical pipes in a section keep the normal tag flow.
        """
        try:
            level = self.view.GenLevel
            return _level_elevation(level) if level is not None else None
        except Exception:
            return None

    def set_riser_flow_elements(self, up_elements, down_elements):
        """Record which risers the user picked as up-flow and down-flow.

        The pipes clicked in the 'bottom to top' pick flow up; those in the
        'top to bottom' pick flow down. Keyed by element id, so the run
        representative (always one of the picked segments) resolves to the
        right direction. Once this is set - even to empty sets - a vertical
        pipe that was NOT picked as a riser gets no designation, so ordinary
        pipes picked afterwards stay plain.
        """
        self._riser_up_ids = set(
            utils.element_id_value(element.Id) for element in (up_elements or []))
        self._riser_down_ids = set(
            utils.element_id_value(element.Id) for element in (down_elements or []))
        self._riser_designations = {}   # re-evaluate with the new flow

    def _riser_flow(self, pipe):
        """Return 'up' / 'down' for this riser, or None.

        The direction is whichever pick the pipe was clicked in. A pipe in
        neither pick is not a designated riser (None). Before the two-pick has
        run at all (e.g. a section view), there is no flow either, so None.
        """
        if self._riser_up_ids is None:
            return None

        pipe_id = utils.element_id_value(pipe.Id)
        if pipe_id in self._riser_up_ids:
            return 'up'
        if pipe_id in self._riser_down_ids:
            return 'down'
        return None

    def _riser_designation(self, pipe):
        """Return the riser designation for this pipe on this plan, or None.

        Geometry decides which sides of the floor the run exists on; flow
        decides the wording (all six strings live in config.AUTO_RISER_*):

                                  flow UP        flow DOWN
            above only            T/A            F/A
            below and above       F/B - T/A      F/A - T/B
            below only            F/B            T/B

        None whenever the rule does not apply (not a plan view, pipe not
        vertical, no flow picked) - the caller then uses the normal flow.
        Cached per pipe, so the run walk happens once per pipe per session.
        """
        pipe_id = utils.element_id_value(pipe.Id)
        if pipe_id in self._riser_designations:
            return self._riser_designations[pipe_id]

        designation = None
        elevation = self._plan_level_elevation()
        if elevation is not None:
            direction = utils.get_element_direction(pipe)
            if direction is not None and abs(direction.Z) >= 0.7:
                flow = self._riser_flow(pipe)
                extent = runs.riser_extent(pipe) if flow else None
                # Both bounds must be real numbers: an empty walk must fall
                # back to the normal flow, never fabricate a designation
                # (None compares below every number in IronPython 2.7).
                if (extent is not None
                        and extent[0] is not None
                        and extent[1] is not None):
                    tolerance = utils.mm_to_feet(
                        config.RISER_LEVEL_TOLERANCE_MM)
                    below = extent[0] < elevation - tolerance
                    above = extent[1] > elevation + tolerance
                    up = flow == 'up'
                    if below and above:
                        designation = config.auto_riser_through(up)
                    elif above:
                        designation = (config.AUTO_RISER_UP_ABOVE if up
                                       else config.AUTO_RISER_DOWN_ABOVE)
                    elif below:
                        designation = (config.AUTO_RISER_UP_BELOW if up
                                       else config.AUTO_RISER_DOWN_BELOW)

        self._riser_designations[pipe_id] = designation
        return designation

    def _horizontal_designation(self, pipe):
        """Return AT H/L / AT L/L for a horizontal pipe on a plan, or None.

        High Level at or above config.AUTO_HORIZONTAL_THRESHOLD_MM above the
        plan's floor, Low Level below it. None for a vertical pipe (a riser,
        handled by _riser_designation), a non-plan view, or a pipe whose height
        cannot be measured. Every horizontal pipe qualifies - no system filter.
        """
        if not config.AUTO_TAG_ENABLED:
            return None
        if self._plan_level_elevation() is None:
            return None    # not a plan view
        direction = utils.get_element_direction(pipe)
        if direction is None or abs(direction.Z) >= 0.7:
            return None    # vertical (a riser) or no direction
        height = self._pipe_height_above_floor(pipe)
        if height is None:
            return None
        threshold = utils.mm_to_feet(config.AUTO_HORIZONTAL_THRESHOLD_MM)
        return config.AUTO_HL if height >= threshold else config.AUTO_LL

    def _pipe_designation(self, pipe):
        """Return the Auto designation to write into a pipe's Comments, or None.

        A vertical riser gets its F/B / T/A designation (flow x geometry); any
        other pipe on the plan gets AT H/L / AT L/L by height. None when neither
        rule applies (not a pipe, not a plan, no geometry, riser not picked).
        """
        if not isinstance(pipe, Pipe):
            return None
        riser = self._riser_designation(pipe)
        if riser is not None:
            return riser
        return self._horizontal_designation(pipe)

    def _is_riser(self, element):
        """True if this element is a riser that earns a designation."""
        return (config.RISER_TAG_ENABLED
                and isinstance(element, Pipe)
                and self._riser_designation(element) is not None)

    def _riser_tag_type_id(self, element, tag_category):
        """Return the single riser tag symbol id for a designated riser, else None.

        The designation (F/B, T/A, ...) is NOT a tag type - it is written to the
        tag's instance parameter afterwards (see _apply_riser_designation). Here
        we only choose the one riser tag family to place. Returns None for
        anything that is not a designated riser, or when that family is not
        loaded (logged) - the caller then uses the normal selection.
        """
        if self._comments_mode:
            # Auto mode places one ordinary pipe tag on every pipe and puts the
            # designation in Comments, so there is no riser tag type to pick.
            return None
        if not self._is_riser(element):
            return None

        if 'riser' in self._riser_type_cache:
            return self._riser_type_cache['riser']

        family_name, type_name = config.RISER_TAG_TYPE
        symbol_id = self._find_symbol(tag_category, family_name, type_name)
        if symbol_id is None:
            utils.logger.warning(
                'Riser tag "{} : {}" is not loaded in this project; risers '
                'fall back to the normal tag type.'.format(
                    family_name, type_name))
        self._riser_type_cache['riser'] = symbol_id
        return symbol_id

    def _apply_riser_designation(self, tag, element):
        """Write the riser designation onto the tag's instance parameter.

        Called for every riser tag - new or reused - so a re-run keeps the text
        current if the flow pick or the geometry changed. A missing / read-only
        parameter is logged, not fatal: the tag still stands, just without the
        F/B / T/A text, which shows up plainly on the drawing.
        """
        if self._comments_mode:
            return    # Auto mode writes the pipe's Comments instead
        if not self._is_riser(element):
            return
        designation = self._riser_designation(element)
        if designation is None:
            return

        parameter = tag.LookupParameter(config.RISER_DESIGNATION_PARAM)
        if parameter is None or parameter.IsReadOnly:
            if not self._riser_param_warned:
                # Once per run: a missing parameter usually means the riser tag
                # family isn't loaded (so a normal tag was placed) or the name
                # is wrong - the same for every riser, so don't repeat it.
                utils.logger.warning(
                    'Riser designation parameter "{}" is not a writable '
                    'parameter on the tag; the designation was not written. '
                    '(Check RISER_TAG_TYPE is loaded and '
                    'RISER_DESIGNATION_PARAM is right.)'.format(
                        config.RISER_DESIGNATION_PARAM))
                self._riser_param_warned = True
            return
        try:
            parameter.Set(designation)
        except Exception as ex:
            utils.logger.debug(
                'Setting riser designation on tag {} failed: {}'.format(
                    utils.element_id_value(tag.Id), ex))

    # -- runtime tag type selection ----------------------------------------
    def categories_needing_tags(self, elements):
        """Return {category id: category name} for elements with no tag yet.

        Read-only, so the caller can ask which tag type to use *before* any
        transaction is opened - cancelling then costs nothing.

        Args:
            elements (list): Supported MEP elements.

        Returns:
            OrderedDict: Categories that will have new tags created for them.
        """
        pending = OrderedDict()
        for element in elements:
            if self.find_existing_tag(element) is not None:
                continue  # Reused, so no new tag and no type to choose.
            category_value = utils.get_category_id_value(element)
            if category_value is None:
                continue
            tag_category = config.SUPPORTED_CATEGORIES.get(category_value)
            if tag_category is not None and (
                    self._riser_tag_type_id(element, tag_category) is not None
                    or self._elevation_tag_type_id(element, tag_category)
                    is not None):
                continue  # Auto riser/HL/LL type - no tag type to choose.
            if category_value not in pending:
                pending[category_value] = utils.get_category_name(element)
        return pending

    def list_tag_types(self, category_value):
        """Return the tag FamilySymbols loaded for an element category."""
        tag_category = config.SUPPORTED_CATEGORIES.get(category_value)
        if tag_category is None:
            return []
        return list(FilteredElementCollector(self.doc)
                    .OfClass(FamilySymbol)
                    .OfCategory(tag_category)
                    .ToElements())

    def set_tag_type(self, category_value, symbol_id):
        """Pin the tag type to use for a category.

        The user's runtime choice. It goes straight into the cache that
        _get_tag_type_id() consults first, so it beats both the configured
        preference and the first-found fallback.
        """
        self._tag_type_cache[category_value] = symbol_id

    # -- creation ----------------------------------------------------------
    def create_tag(self, element):
        """Create a tag for an element and return it.

        Raises:
            ValueError: If no tag family is loaded for the category, or the
                element has no usable location.
        """
        tag_type_id = self._get_tag_type_id(element)
        if tag_type_id is None:
            raise ValueError('no tag family loaded for category "{}"'.format(
                utils.get_category_name(element)))

        anchor = utils.get_element_anchor(element, self.view)
        if anchor is None:
            raise ValueError('element has no usable location')

        self._activate(tag_type_id)

        # Offset the head so a new tag does not sit on top of its element and
        # its leader starts with a sensible length.
        head = utils.shift(anchor, self._right, self._initial_offset)

        return IndependentTag.Create(
            self.doc,
            tag_type_id,
            self.view.Id,
            utils.get_reference(element),
            config.ADD_LEADER,
            config.TAG_ORIENTATION,
            head,
        )

    # -- public API --------------------------------------------------------
    def write_pipe_comments(self, elements):
        """Write each pipe's Auto designation into its built-in Comments.

        This is a MODEL change, so it must run inside the caller's transaction.
        A single tag family (Size + System Abbreviation + Comments) then shows
        the value. Non-pipes and pipes the rule does not cover are left
        untouched - a blank designation never clears an existing comment.

        Args:
            elements (list): The elements about to be tagged.

        Returns:
            tuple: (written, failures) where failures is a list of
            (element_id, message).
        """
        written = 0
        failures = []
        for element in elements:
            if not isinstance(element, Pipe):
                continue
            designation = self._pipe_designation(element)
            if designation is None:
                continue
            element_id = utils.element_id_value(element.Id)
            try:
                parameter = element.get_Parameter(
                    BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                if parameter is None or parameter.IsReadOnly:
                    parameter = element.LookupParameter('Comments')
                if parameter is None or parameter.IsReadOnly:
                    failures.append(
                        (element_id, 'Comments parameter is not writable'))
                    continue
                parameter.Set(designation)
                written += 1
            except Exception as ex:
                failures.append((element_id, str(ex)))
                utils.logger.error(
                    'Writing Comments on pipe {} failed: {}'.format(
                        element_id, ex))

        utils.logger.debug('Auto Comments written on {} pipe(s), {} failed.'
                           .format(written, len(failures)))
        return written, failures

    def ensure_tags(self, elements):
        """Ensure every element has exactly one tag in the view.

        Existing tags are reused; missing ones are created. A failure on one
        element never stops the others.

        Args:
            elements (list): Supported MEP elements.

        Returns:
            tuple: (tags, created, reused, failures) where failures is a list
            of (element_id, message).
        """
        tags = []
        created = 0
        reused = 0
        failures = []

        for element in elements:
            element_id = utils.element_id_value(element.Id)
            try:
                tag = self.find_existing_tag(element)
                if tag is not None:
                    reused += 1
                else:
                    tag = self.create_tag(element)
                    created += 1

                if tag is not None:
                    tags.append(tag)
                    self._apply_riser_designation(tag, element)
            except Exception as ex:
                failures.append((element_id, str(ex)))
                utils.logger.error('Tagging element {} failed: {}'.format(
                    element_id, ex))

        utils.logger.debug('Tags ready: {} created, {} reused, {} failed.'.format(
            created, reused, len(failures)))
        return tags, created, reused, failures
