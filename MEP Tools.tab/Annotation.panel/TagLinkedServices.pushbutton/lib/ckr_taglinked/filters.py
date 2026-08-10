# -*- coding: utf-8 -*-
"""Element filtering - the choices offered, and the sieve that applies them.

FR-03. Two halves:

    FilterOptions   what the dialog offers, read from the link documents'
                    TYPES rather than their instances, so populating the
                    dialog stays inside the 3 s of NF-01 even on a 25 000
                    element link.

    Sieve           the per-element test, run as stage 1 of the funnel in
                    clause 7.5 before any geometry is touched.

Everything is matched on NAMES, not element ids. A type id from one link
document means nothing in another, and the same services model is
routinely placed as several instances or reissued with new ids - names are
the only identity that survives both.

An empty selection means "no filter", never "nothing passes" (FR-03).
"""

from Autodesk.Revit.DB import (
    BuiltInParameter,
    FilteredElementCollector,
    Level,
    MEPCurve,
)

from ckr_taglinked import categories, compat

#: Rejection reasons reported by the sieve, for the FR-09 breakdown.
REASON_TYPE = 'family / type'
REASON_CLASSIFICATION = 'system classification'
REASON_SYSTEM_TYPE = 'system type'
REASON_SIZE = 'size range'
REASON_LEVEL = 'reference level'
REASON_WORKSET = 'workset'


def type_label(family, name):
    """Return the 'Family : Type' label used as the type identity."""
    return '{0} : {1}'.format(family or '?', name or '?')


def pretty_classification(value):
    """Return 'DomesticColdWater' as 'Domestic Cold Water' for display."""
    text = str(value)
    out = []
    for index, character in enumerate(text):
        if index and character.isupper() and not text[index - 1].isupper():
            out.append(' ')
        out.append(character)
    return ''.join(out)


# ---------------------------------------------------------------------------
# What the link documents offer
# ---------------------------------------------------------------------------
def _system_type_classes():
    """Return the MEP system type classes available on this Revit version."""
    classes = []
    try:
        from Autodesk.Revit.DB.Plumbing import PipingSystemType
        classes.append(PipingSystemType)
    except Exception:
        pass
    try:
        from Autodesk.Revit.DB.Mechanical import MechanicalSystemType
        classes.append(MechanicalSystemType)
    except Exception:
        pass
    return classes


class LinkContext(object):
    """Per-link lookups built once, then read from the hot loop.

    Holds the maps that turn an element's ids into the names the filters
    are expressed in: system type, level and workset. Building them costs
    one collector pass over the link's TYPES, not its instances.
    """

    def __init__(self, target):
        self.target = target
        self.systems = {}      # system type id value -> (name, classification)
        self.levels = {}       # level id value -> name
        self.worksets = {}     # workset id value -> name
        self.types = {}        # type id value -> (family, name)
        self._load()

    def _load(self):
        document = self.target.document
        if document is None:
            return

        for system_class in _system_type_classes():
            try:
                collector = FilteredElementCollector(document) \
                    .OfClass(system_class)
            except Exception:
                continue
            for system_type in collector:
                try:
                    classification = str(system_type.SystemClassification)
                except Exception:
                    classification = ''
                self.systems[compat.id_value(system_type.Id)] = (
                    compat.element_name(system_type), classification)

        try:
            for level in FilteredElementCollector(document).OfClass(Level):
                self.levels[compat.id_value(level.Id)] = \
                    compat.element_name(level)
        except Exception:
            pass

        self._load_worksets(document)

    def _load_worksets(self, document):
        """Read the link's user worksets, when it is workshared (FR-03.6)."""
        try:
            if not document.IsWorkshared:
                return
            from Autodesk.Revit.DB import FilteredWorksetCollector, WorksetKind
            collector = FilteredWorksetCollector(document) \
                .OfKind(WorksetKind.UserWorkset)
            for workset in collector:
                self.worksets[compat.id_value(workset.Id)] = workset.Name
        except Exception:
            pass

    def type_of(self, element):
        """Return (family, type name) for an element, cached by type id."""
        try:
            type_id = element.GetTypeId()
        except Exception:
            return ('', '')
        key = compat.id_value(type_id)
        cached = self.types.get(key)
        if cached is not None:
            return cached
        element_type = None
        try:
            element_type = self.target.document.GetElement(type_id)
        except Exception:
            element_type = None
        value = (compat.family_name(element_type),
                 compat.element_name(element_type))
        self.types[key] = value
        return value

    def system_of(self, element, spec):
        """Return (system type name, classification) for an element.

        The system type element is the authority on classification, so the
        classification filter never has to match a free-text parameter.
        Falls back to the element's own classification parameter where no
        system type is assigned.
        """
        if spec.system_type_parameter is None:
            return ('', '')
        parameter = element.get_Parameter(spec.system_type_parameter)
        if parameter is not None:
            try:
                system_id = parameter.AsElementId()
            except Exception:
                system_id = None
            if compat.is_valid_id(system_id):
                found = self.systems.get(compat.id_value(system_id))
                if found is not None:
                    return found

        if spec.classification_parameter is not None:
            parameter = element.get_Parameter(spec.classification_parameter)
            if parameter is not None:
                try:
                    return ('', parameter.AsString() or
                            parameter.AsValueString() or '')
                except Exception:
                    pass
        return ('', '')

    def level_of(self, element):
        """Return the element's reference level name, or ''."""
        parameter = None
        built_in = getattr(BuiltInParameter, 'RBS_START_LEVEL_PARAM', None)
        if built_in is not None:
            parameter = element.get_Parameter(built_in)
        if parameter is not None:
            try:
                level_id = parameter.AsElementId()
            except Exception:
                level_id = None
            if compat.is_valid_id(level_id):
                return self.levels.get(compat.id_value(level_id), '')
        try:
            return self.levels.get(compat.id_value(element.LevelId), '')
        except Exception:
            return ''

    def workset_of(self, element):
        """Return the element's workset name, or '' outside a workshared link."""
        if not self.worksets:
            return ''
        try:
            return self.worksets.get(compat.id_value(element.WorksetId), '')
        except Exception:
            return ''


class FilterOptions(object):
    """The choices shown in the dialog, aggregated over the chosen links."""

    def __init__(self):
        #: category key -> {family name -> sorted list of type names}
        self.types = dict((spec.key, {}) for spec in categories.CATEGORIES)
        self.classifications = set()
        self.system_types = set()
        self.levels = set()
        self.worksets = set()

    def add_type(self, category_key, family, name):
        families = self.types.setdefault(category_key, {})
        families.setdefault(family or '?', set()).add(name or '?')

    def families(self, category_key):
        """Return (family, [type names]) pairs sorted for display."""
        families = self.types.get(category_key, {})
        return [(family, sorted(families[family], key=lambda s: s.lower()))
                for family in sorted(families, key=lambda s: s.lower())]

    def sorted_classifications(self):
        return sorted(self.classifications, key=lambda s: s.lower())

    def sorted_system_types(self):
        return sorted(self.system_types, key=lambda s: s.lower())

    def sorted_levels(self):
        return sorted(self.levels, key=lambda s: s.lower())

    def sorted_worksets(self):
        return sorted(self.worksets, key=lambda s: s.lower())


def build_options(contexts):
    """Return the FilterOptions offered by a set of LinkContexts.

    Types come from the link's element TYPES, and classifications from its
    system types - both are cheap collector passes over a few hundred
    elements, which is what keeps the dialog inside NF-01 while the
    instance count runs into five figures.
    """
    options = FilterOptions()
    for context in contexts:
        document = context.target.document
        if document is None:
            continue
        for spec in categories.CATEGORIES:
            try:
                collector = FilteredElementCollector(document) \
                    .OfCategory(spec.category).WhereElementIsElementType()
            except Exception:
                continue
            for element_type in collector:
                options.add_type(spec.key,
                                 compat.family_name(element_type),
                                 compat.element_name(element_type))
        for name, classification in context.systems.values():
            if name:
                options.system_types.add(name)
            if classification:
                options.classifications.add(classification)
        for name in context.levels.values():
            if name:
                options.levels.add(name)
        for name in context.worksets.values():
            if name:
                options.worksets.add(name)
    return options


# ---------------------------------------------------------------------------
# The sieve (stage 1 of clause 7.5)
# ---------------------------------------------------------------------------
class Sieve(object):
    """Applies the FR-03 filters to one category's candidates.

    Tests are ordered cheapest first: the type check is two id lookups,
    the size and system checks are parameter reads, and nothing here
    touches geometry - that is stage 2's job.
    """

    def __init__(self, spec, config):
        self.spec = spec
        selection = config.get('types', {}).get(spec.key) or []
        self.types = set(selection)
        self.classifications = set(config.get('classifications') or [])
        self.system_types = set(config.get('system_types') or [])
        self.levels = set(config.get('levels') or [])
        self.worksets = set(config.get('worksets') or [])

        size_from = config.get('size_from_mm')
        size_to = config.get('size_to_mm')
        self.size_from = (compat.mm_to_internal(size_from)
                          if size_from not in (None, '') else None)
        self.size_to = (compat.mm_to_internal(size_to)
                        if size_to not in (None, '') else None)

    def collect(self, document):
        """Return the category's instances in a link document.

        Category plus "not a type" is all Revit can usefully do here; the
        MEPCurve test drops anything odd that shares the category without
        being a run.
        """
        try:
            collector = FilteredElementCollector(document) \
                .OfCategory(self.spec.category).WhereElementIsNotElementType()
        except Exception:
            return []
        return [element for element in collector
                if isinstance(element, MEPCurve)]

    def size_of(self, element):
        """Return the element's governing size in feet, or None.

        The largest of the category's size parameters wins, so a 600x200
        duct filters on 600 and a round one on its diameter.
        """
        best = None
        for built_in in self.spec.size_parameters:
            parameter = element.get_Parameter(built_in)
            if parameter is None:
                continue
            try:
                value = parameter.AsDouble()
            except Exception:
                continue
            if value and (best is None or value > best):
                best = value
        return best

    def accepts(self, element, context):
        """Return (True, None) when an element passes every active filter.

        Returns:
            tuple: (bool, reason). The reason names the filter that
            rejected it, so FR-09 can report the breakdown instead of one
            opaque total.
        """
        if self.types:
            family, name = context.type_of(element)
            if type_label(family, name) not in self.types:
                return False, REASON_TYPE

        if self.levels and context.levels:
            if context.level_of(element) not in self.levels:
                return False, REASON_LEVEL

        if self.worksets and context.worksets:
            if context.workset_of(element) not in self.worksets:
                return False, REASON_WORKSET

        if (self.classifications or self.system_types) and \
                self.spec.has_systems:
            system_name, classification = context.system_of(element,
                                                            self.spec)
            if self.system_types and system_name not in self.system_types:
                return False, REASON_SYSTEM_TYPE
            if self.classifications and \
                    classification not in self.classifications:
                return False, REASON_CLASSIFICATION

        if self.size_from is not None or self.size_to is not None:
            size = self.size_of(element)
            if size is None:
                return False, REASON_SIZE
            if self.size_from is not None and size < self.size_from - 1e-9:
                return False, REASON_SIZE
            if self.size_to is not None and size > self.size_to + 1e-9:
                return False, REASON_SIZE

        return True, None
