# -*- coding: utf-8 -*-
"""Link discovery (FR-01).

One LinkTarget per RevitLinkInstance - not per link type - because a type
placed several times has a different GetTotalTransform() per instance
(FR-01.3), and a tag's identity for duplicate suppression is the
(instance, element) pair.

Unloaded links are returned too, flagged with the reason, so the dialog can
list them disabled rather than pretend they do not exist (FR-01.2).
"""

from Autodesk.Revit.DB import (
    FilteredElementCollector,
    RevitLinkInstance,
    RevitLinkType,
)

from ckr_taglinked import compat


class LinkTarget(object):
    """One placed link instance and everything the run needs from it."""

    def __init__(self, instance, document, transform, loaded, reason,
                 nested):
        self.instance = instance
        self.instance_id = instance.Id
        self.document = document
        self.transform = transform
        self.loaded = loaded
        self.reason = reason
        #: True when this link CONTAINS nested links.
        self.nested = nested
        #: True when this link IS one - a link inside another link.
        self.is_nested = False
        self.name = _instance_name(instance)

    @property
    def id_value(self):
        return compat.id_value(self.instance_id)

    def __repr__(self):
        return '<LinkTarget {0}>'.format(self.name)


def _instance_name(instance):
    """Return a readable name for a link instance.

    ``RevitLinkInstance.Name`` already distinguishes multiple placements of
    one link type ("Services.rvt : 2"), which is exactly what FR-01.3
    needs the user to be able to tell apart.
    """
    name = compat.element_name(instance)
    if name:
        return name
    return 'Link {0}'.format(compat.id_value(instance.Id))


def _link_type(doc, instance):
    """Return the RevitLinkType behind an instance, or None."""
    try:
        return doc.GetElement(instance.GetTypeId())
    except Exception:
        return None


#: Readable reasons for the LinkedFileStatus values that mean "no document".
_STATUS_REASONS = {
    'Unloaded': 'unloaded - reload it in Manage > Manage Links',
    'LocallyUnloaded': 'unloaded for you only - reload it in Manage > '
                       'Manage Links',
    'NotFound': 'the linked file could not be found',
    'InClosedWorkset': 'its workset is closed in this session',
    'Invalid': 'the link reference is invalid',
    'NotLoaded': 'not loaded',
}


def _why_unavailable(doc, instance):
    """Explain why a link handed over no document.

    Asked only after the fact, so a status this Revit version words
    differently can never stop a link that actually works.
    """
    link_type = _link_type(doc, instance)
    if link_type is None:
        return 'its link type could not be resolved'
    for reader in ('GetLinkedFileStatus', 'GetExternalFileReference'):
        method = getattr(link_type, reader, None)
        if method is None:
            continue
        try:
            value = method()
            if reader == 'GetExternalFileReference':
                value = value.GetLinkedFileStatus()
            return _STATUS_REASONS.get(str(value),
                                       'its status is {0}'.format(value))
        except Exception:
            continue
    try:
        if not RevitLinkType.IsLoaded(doc, instance.GetTypeId()):
            return 'it is not loaded'
    except Exception:
        pass
    return 'it is not loaded'


def _is_nested(doc, instance):
    """Return True when this instance is a link inside another link.

    Nested content is out of scope (OS-06), so these are listed disabled
    with the reason rather than tagged (FR-01.4).
    """
    link_type = _link_type(doc, instance)
    if link_type is None:
        return False
    try:
        return bool(link_type.IsNestedLink)
    except Exception:
        return False


def _has_nested_links(link_document):
    """Return True when the link document itself contains link instances.

    Nested content is out of scope (OS-06) and must be reported, never
    silently ignored (FR-01.4).
    """
    if link_document is None:
        return False
    try:
        collector = FilteredElementCollector(link_document) \
            .OfClass(RevitLinkInstance)
        return collector.FirstElement() is not None
    except Exception:
        return False


def discover(doc):
    """Return every RevitLinkInstance in the host document as a LinkTarget.

    Loaded links carry their document and total transform; unloaded ones
    carry the reason instead, so the UI can grey them out with an
    explanation.

    Returns:
        list[LinkTarget]: Sorted by name, so the dialog is stable between
        runs.
    """
    targets = []
    for instance in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
        # The link's DOCUMENT is the authority on whether this link can be
        # read, because reading it is the whole job. Asking the link type
        # instead answers a subtly different question and has been seen to
        # say "not loaded" for links whose document opens perfectly well -
        # a false negative there hides every service in the project.
        document = None
        transform = None
        try:
            document = instance.GetLinkDocument()
        except Exception:
            document = None

        nested = _is_nested(doc, instance)
        if document is None:
            loaded, reason = False, _why_unavailable(doc, instance)
        elif nested:
            # Loaded and readable, but out of scope: reported, not ignored.
            loaded, reason = False, ('a nested link - nested content is not '
                                     'supported')
            document = None
        else:
            loaded, reason = True, ''
            try:
                transform = instance.GetTotalTransform()
            except Exception:
                transform = None

        target = LinkTarget(instance, document, transform, loaded, reason,
                            _has_nested_links(document))
        target.is_nested = nested
        targets.append(target)
    targets.sort(key=lambda target: target.name.lower())
    return targets


def nested_warnings(targets):
    """Return the FR-01.4 message for every selected link with nested links."""
    return ['Nested links detected in {0} - nested content is not '
            'supported'.format(target.name)
            for target in targets if target.nested]


def unavailable_summary(targets):
    """Return one line per link that cannot be read, for the alert text."""
    return ['- {0}: {1}'.format(target.name, target.reason)
            for target in targets if not target.loaded]
