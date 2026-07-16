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
import utils


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

        # Water-supply pipes are typed per pipe by elevation (HL / LL), never
        # cached per category. Everything else falls through to the normal
        # one-type-per-category selection below.
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
                return level.Elevation
        except Exception:
            pass
        try:
            parameter = pipe.get_Parameter(BuiltInParameter.RBS_START_LEVEL_PARAM)
            if parameter is not None:
                level = self.doc.GetElement(parameter.AsElementId())
                if level is not None:
                    return level.Elevation
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
        return (config.ELEVATION_TAG_ENABLED
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
            if (tag_category is not None
                    and self._elevation_tag_type_id(element, tag_category)
                    is not None):
                continue  # Auto HL/LL by elevation - no tag type to choose.
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
            except Exception as ex:
                failures.append((element_id, str(ex)))
                utils.logger.error('Tagging element {} failed: {}'.format(
                    element_id, ex))

        utils.logger.debug('Tags ready: {} created, {} reused, {} failed.'.format(
            created, reused, len(failures)))
        return tags, created, reused, failures
