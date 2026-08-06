# -*- coding: utf-8 -*-
"""Tag family/type discovery in the HOST document (FR-02).

Tags on linked elements are created in the host document, so the tag types
offered are the annotation symbols loaded there - never in the link. Types
are identified by their 'Family : Type' label rather than by element id so
a saved profile still resolves in the next project (FR-11).

Tag families are never auto-loaded: where an included category has no tag
type, execution is blocked with a message naming the family that is
missing (FR-02.3 / AT-13).
"""

from Autodesk.Revit.DB import FamilySymbol, FilteredElementCollector

from ckr_taglinked import categories, compat


def label_of(symbol):
    """Return the 'Family : Type' label of an annotation symbol."""
    return '{0} : {1}'.format(compat.family_name(symbol) or '?',
                              compat.element_name(symbol) or '?')


def index(doc):
    """Return {category key: {label: ElementId}} of loaded tag types.

    Returns:
        dict: One entry per supported category; the inner dict is empty
        when no tag family of that category is loaded.
    """
    found = {}
    for spec in categories.CATEGORIES:
        types = {}
        try:
            collector = FilteredElementCollector(doc) \
                .OfCategory(spec.tag_category).OfClass(FamilySymbol)
        except Exception:
            collector = []
        for symbol in collector:
            types[label_of(symbol)] = symbol.Id
        found[spec.key] = types
    return found


def labels(doc):
    """Return {category key: sorted list of labels} for the dialog."""
    return dict((key, sorted(types, key=lambda s: s.lower()))
                for key, types in index(doc).items())


def activate(doc, tag_type_id):
    """Ensure a tag symbol is active before it is used to create a tag.

    An inactive symbol makes ``IndependentTag.Create`` throw. Activation
    is a document change, so it must happen inside the caller's
    transaction.
    """
    symbol = doc.GetElement(tag_type_id)
    if symbol is None:
        return False
    try:
        if not symbol.IsActive:
            symbol.Activate()
            doc.Regenerate()
    except Exception:
        return False
    return True
