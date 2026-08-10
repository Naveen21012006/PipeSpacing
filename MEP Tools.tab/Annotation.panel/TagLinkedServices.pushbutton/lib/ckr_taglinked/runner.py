# -*- coding: utf-8 -*-
"""The run itself: the three-stage funnel of clause 7.5 and its transactions.

Order of work, and why:

    Stage 1  cheap parametric pre-filter in the LINK document (filters.py)
    Stage 2  bounding-box rejection, arithmetic only, no transaction
    ---- classification and visible length (clauses 7.3, 7.4) ----
    Stage 3  create-and-test: the tag is created inside a SubTransaction
             and kept only if it reports a bounding box in the view

Stages 1 and 2 run with no transaction open at all, so the candidate list
- and therefore the progress bar total - is known before anything is
modified. Stage 3 then runs inside one Transaction inside one
TransactionGroup, which is what makes the whole run a single undo step
named "Tag Linked Services" (clause 7.7.1).

Preview does exactly the same work and rolls the group back (FR-08), which
is the only way the preview counts can match the real run exactly (AT-01):
the stage 3 rejections are real rejections and cannot be predicted.

Cancellation commits what has already been placed (clause 7.7.3) - on a
large plan nobody wants to repeat forty seconds of work.
"""

import time

from Autodesk.Revit.DB import (
    FilteredElementCollector,
    IndependentTag,
    Line,
    LocationCurve,
    Transaction,
    TransactionGroup,
)

from ckr_taglinked import (
    categories,
    compat,
    config as config_module,
    core,
    filters,
    placement,
    tagtypes,
    viewvolume,
)

TRANSACTION_NAME = 'Tag Linked Services'

# Rejection reasons reported per category (FR-09).
REJECT_FILTER = 'filter mismatch'
REJECT_LENGTH = 'below minimum visible length'
REJECT_RANGE = 'outside view range or crop region'
REJECT_INCLINED = 'inclined run (excluded)'
REJECT_GEOMETRY = 'no usable location curve'
REJECT_NOT_VISIBLE = 'not visible in this view'


class CategoryResult(object):
    """The tally for one category, in the shape FR-09 asks for."""

    def __init__(self, key, label):
        self.key = key
        self.label = label
        self.candidates = 0
        self.placed = 0
        self.crowded = 0
        self.skipped_tagged = 0
        self.rejected = {}
        self.errors = []          # (element id, message)
        self.non_linear = 0
        self.by_class = {core.HORIZONTAL: 0, core.VERTICAL: 0,
                         core.INCLINED: 0}

    def reject(self, reason, count=1):
        self.rejected[reason] = self.rejected.get(reason, 0) + count

    @property
    def rejected_total(self):
        return sum(self.rejected.values())


class RunResult(object):
    """Everything the report needs, and nothing the UI has to recompute."""

    def __init__(self, preview):
        self.preview = preview
        self.categories = []
        self.warnings = []
        self.blocked = None       # a message when the run never started
        self.cancelled = False
        self.seconds = 0.0
        self.view_name = ''
        self.scanned = 0

    def result_for(self, key):
        for entry in self.categories:
            if entry.key == key:
                return entry
        return None

    @property
    def placed(self):
        return sum(entry.placed for entry in self.categories)

    @property
    def skipped_tagged(self):
        return sum(entry.skipped_tagged for entry in self.categories)

    @property
    def rejected(self):
        return sum(entry.rejected_total for entry in self.categories)

    @property
    def errors(self):
        return sum(len(entry.errors) for entry in self.categories)


class Candidate(object):
    """One element that survived stages 1 and 2 and the length rule."""

    __slots__ = ('target', 'spec', 'element', 'element_id', 'classification',
                 'segment', 'visible_length', 'non_linear')

    def __init__(self, target, spec, element, classification, segment,
                 visible_length, non_linear):
        self.target = target
        self.spec = spec
        self.element = element
        self.element_id = compat.id_value(element.Id)
        self.classification = classification
        self.segment = segment
        self.visible_length = visible_length
        self.non_linear = non_linear


# ---------------------------------------------------------------------------
# FR-07 - what is already tagged in this view
# ---------------------------------------------------------------------------
def tagged_pairs(doc, view):
    """Return the set of (link instance id, linked element id) already tagged.

    Built once per run, before anything is placed. Multi-reference tags
    contribute every reference they carry, so a manually placed tag that
    covers several runs suppresses all of them (brief open item 3).
    """
    pairs = set()
    try:
        collector = FilteredElementCollector(doc, view.Id) \
            .OfClass(IndependentTag).WhereElementIsNotElementType()
    except Exception:
        return pairs
    for tag in collector:
        for pair in compat.tagged_link_pairs(tag):
            pairs.add(pair)
    return pairs


# ---------------------------------------------------------------------------
# Stages 1 and 2 - no document modification
# ---------------------------------------------------------------------------
def _location_curve(element):
    """Return an element's location curve, or None."""
    try:
        location = element.Location
    except Exception:
        return None
    if not isinstance(location, LocationCurve):
        return None
    try:
        return location.Curve
    except Exception:
        return None


def gather(doc, view, values, targets, volume, log, progress=None):
    """Run stages 1 and 2 and the geometric rules over every link.

    Returns:
        tuple: (candidates, results) - results already carries every
        rejection tally, so a preview needs no second pass.
    """
    results = RunResult(preview=False)
    results.view_name = compat.element_name(view)
    minimums = config_module.minimums_in_feet(values)
    include_inclined = values['include_inclined']
    horizontal_tol = values['horizontal_tol_deg']
    vertical_tol = values['vertical_tol_deg']
    skip_tagged = values['skip_tagged']

    already = tagged_pairs(doc, view) if skip_tagged else set()
    if skip_tagged:
        log.info('%s existing tag reference(s) in "%s" will be skipped.',
                 len(already), results.view_name)

    included = config_module.included_categories(values)
    for key in included:
        spec = categories.by_key(key)
        results.categories.append(CategoryResult(key, spec.label))

    results.warnings.extend(_visibility_warnings(view, included))

    # Collect first, assess second: the candidate count is what the
    # progress bar counts down, and stage 1 collection is a handful of
    # collector passes however many elements they return.
    work = []
    for target in targets:
        if target.document is None:
            continue
        if _link_hidden(view, target):
            results.warnings.append(
                'Link "{0}" is hidden in this view; it was skipped.'.format(
                    target.name))
            continue

        context = filters.LinkContext(target)
        for key in included:
            spec = categories.by_key(key)
            sieve = filters.Sieve(spec, values)
            elements = sieve.collect(target.document)
            results.scanned += len(elements)
            log.info('Link "%s": %s %s to assess.', target.name,
                     len(elements), spec.label.lower())
            if elements:
                work.append((target, context, spec, sieve,
                             results.result_for(key), elements))

    candidates = []
    processed = 0
    total = results.scanned or 1
    for target, context, spec, sieve, entry, elements in work:
        for element in elements:
            processed += 1
            if progress is not None and processed % 50 == 0:
                progress.update(processed, total)
                if progress.cancelled:
                    return candidates, results
            try:
                candidate = _assess(element, target, spec, sieve, context,
                                    entry, volume, minimums, horizontal_tol,
                                    vertical_tol, include_inclined, already,
                                    skip_tagged)
            except Exception as ex:
                entry.errors.append((compat.id_value(element.Id),
                                     '{0}'.format(ex)))
                log.error('Element %s in "%s": %s',
                          compat.id_value(element.Id), target.name, ex)
                continue
            if candidate is not None:
                candidates.append(candidate)
                entry.candidates += 1
                entry.by_class[candidate.classification] += 1
                if candidate.non_linear:
                    entry.non_linear += 1

    return candidates, results


def _visibility_warnings(view, included):
    """Warn where the host view hides a category that is about to be tagged.

    FR-05.2. A hidden element category or a hidden tag category means the
    tags will not be drawn; the run is not blocked, because the link's own
    graphics settings can still display the elements, and stage 3 is the
    authority on what is actually visible. But the user is told, because
    "it placed 300 tags and I can see none of them" is otherwise a very
    long afternoon.
    """
    warnings = []
    for key in included:
        spec = categories.by_key(key)
        for category, what in ((spec.category, spec.label),
                               (spec.tag_category, spec.tag_label)):
            try:
                hidden = view.GetCategoryHidden(
                    compat.category_id(view.Document, category))
            except Exception:
                continue
            if hidden:
                warnings.append(
                    '{0} are hidden in this view; any tag placed for them '
                    'may not be drawn.'.format(what))
    return warnings


def _link_hidden(view, target):
    """Return True when the link instance itself is hidden in the view.

    Only the unambiguous case is tested here. Finer graphics overrides -
    a link displayed by linked view, a category overridden inside the
    link's own settings - are left to stage 3, which is the only reliable
    answer to "would this tag actually be drawn".
    """
    try:
        return bool(target.instance.IsHidden(view))
    except Exception:
        return False


def _assess(element, target, spec, sieve, context, entry, volume, minimums,
            horizontal_tol, vertical_tol, include_inclined, already,
            skip_tagged):
    """Return a Candidate for one element, or None with the tally updated."""
    # -- stage 1: filters (FR-03), before any geometry ----------------------
    accepted, reason = sieve.accepts(element, context)
    if not accepted:
        entry.reject('{0} ({1})'.format(REJECT_FILTER, reason))
        return None

    # -- FR-07: already tagged in this view ---------------------------------
    if skip_tagged:
        pair = (target.id_value, compat.id_value(element.Id))
        if pair in already:
            entry.skipped_tagged += 1
            return None

    # -- stage 2: bounding box, arithmetic only -----------------------------
    try:
        bbox = element.get_BoundingBox(None)
    except Exception:
        bbox = None
    if bbox is not None:
        corners = compat.bounding_box_corners(bbox, target.transform)
        if volume.rejects(corners):
            entry.reject(REJECT_RANGE)
            return None

    # -- geometry (clauses 7.3 and 7.4) -------------------------------------
    curve = _location_curve(element)
    if curve is None:
        entry.reject(REJECT_GEOMETRY)
        return None

    non_linear = not isinstance(curve, Line)
    start, end = compat.transformed_endpoints(curve, target.transform)
    classification, _angle = core.classify(core.subtract(end, start),
                                           horizontal_tol, vertical_tol)
    if classification is None:
        entry.reject(REJECT_GEOMETRY)
        return None
    if classification == core.INCLINED and not include_inclined:
        entry.reject(REJECT_INCLINED)
        return None

    segment = volume.visible((start, end))
    if segment is None:
        entry.reject(REJECT_RANGE)
        return None

    visible_length = core.segment_length(segment)
    if not core.passes_length(classification, visible_length, minimums):
        entry.reject(REJECT_LENGTH)
        return None

    return Candidate(target, spec, element, classification, segment,
                     visible_length, non_linear)


# ---------------------------------------------------------------------------
# Stage 3 - creation
# ---------------------------------------------------------------------------
def _tag_type_for(values, tag_index, spec, classification):
    """Return the tag type ElementId for a category and classification.

    FR-02.4: riser tags and run tags are commonly different families, so
    each category carries a horizontal and a vertical selection. A blank
    vertical selection falls back to the horizontal one.
    """
    row = values['tags'].get(spec.key, {})
    label = row.get('vertical') if classification == core.VERTICAL else \
        row.get('horizontal')
    if not label:
        label = row.get('horizontal')
    if not label:
        return None
    return tag_index.get(spec.key, {}).get(label)


def missing_tag_types(doc, values):
    """Return messages for included categories with no usable tag type.

    FR-02.3 / AT-13: execution is blocked, with the family named, and no
    partial run happens. Tag families are never auto-loaded.
    """
    index = tagtypes.index(doc)
    problems = []
    for key in config_module.included_categories(values):
        spec = categories.by_key(key)
        available = index.get(key, {})
        if not available:
            problems.append(
                '{0}: no tag family of category "{1}" is loaded in this '
                'project. Load one and run again.'.format(
                    spec.label, spec.tag_label))
            continue
        row = values['tags'].get(key, {})
        label = row.get('horizontal')
        if not label:
            problems.append(
                '{0}: no tag type is selected.'.format(spec.label))
        elif label not in available:
            problems.append(
                '{0}: the selected tag type "{1}" is not loaded in this '
                'project.'.format(spec.label, label))
        vertical = row.get('vertical')
        if vertical and vertical not in available:
            problems.append(
                '{0}: the selected riser tag type "{1}" is not loaded in '
                'this project.'.format(spec.label, vertical))
    return problems


def place_all(doc, view, values, candidates, results, volume, log,
              preview=False, progress=None):
    """Create the tags (or roll them back, in preview).

    Everything is inside one TransactionGroup so the user gets a single
    undo step, and inside it one Transaction with a SubTransaction per tag
    for the stage 3 rollback (clause 7.7.2).
    """
    if not candidates:
        return

    tag_index = tagtypes.index(doc)
    placer = placement.Placer(doc, view, volume, values, log)
    orientation_cache = {}

    group = TransactionGroup(doc, TRANSACTION_NAME)
    group.Start()
    transaction = Transaction(doc, TRANSACTION_NAME)
    transaction.Start()
    try:
        activated = set()
        total = len(candidates)
        for index, candidate in enumerate(candidates):
            if progress is not None:
                progress.update(index + 1, total)
                if progress.cancelled:
                    results.cancelled = True
                    break

            entry = results.result_for(candidate.spec.key)
            tag_type_id = _tag_type_for(values, tag_index, candidate.spec,
                                        candidate.classification)
            if tag_type_id is None:
                entry.errors.append((candidate.element_id,
                                     'no tag type resolved'))
                continue

            key = compat.id_value(tag_type_id)
            if key not in activated:
                tagtypes.activate(doc, tag_type_id)
                activated.add(key)

            row = values['tags'].get(candidate.spec.key, {})
            orientation = orientation_cache.get(candidate.spec.key)
            if orientation is None:
                orientation = compat.tag_orientation(
                    row.get('orientation', 'horizontal'))
                orientation_cache[candidate.spec.key] = orientation

            try:
                reference = compat.create_link_reference(
                    candidate.element, candidate.target.instance)
            except Exception as ex:
                entry.errors.append((candidate.element_id,
                                     'link reference failed: {0}'.format(ex)))
                continue

            status, _tag, detail = placer.place(
                reference, tag_type_id, candidate.classification,
                candidate.segment, bool(row.get('leader', True)),
                orientation)

            if status == placement.PLACED:
                entry.placed += 1
            elif status == placement.PLACED_CROWDED:
                entry.placed += 1
                entry.crowded += 1
            elif status == placement.NOT_VISIBLE:
                entry.reject(REJECT_NOT_VISIBLE)
            else:
                entry.errors.append((candidate.element_id, detail))
                log.error('Tagging element %s failed: %s',
                          candidate.element_id, detail)

        transaction.Commit()
    except Exception:
        if transaction.HasStarted():
            transaction.RollBack()
        group.RollBack()
        raise

    if preview:
        group.RollBack()          # FR-08.2 / AT-11: nothing survives a preview
    else:
        group.Assimilate()        # one undo step (clause 7.7.1)

    if placer.clamped:
        log.info('%s insertion point(s) clamped into the annotation crop.',
                 placer.clamped)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run(doc, view, values, targets, preview=False, progress=None,
        log=None):
    """Execute a full run and return its RunResult.

    Args:
        doc (Document): The host document.
        view (ViewPlan): The active plan view.
        values (dict): Normalised settings (config.normalise).
        targets (list): The selected LinkTargets.
        preview (bool): Report only; roll everything back (FR-08).
        progress: Optional object with ``update(current, total)`` and a
            ``cancelled`` flag (NF-04).
        log: The rolling log.

    Returns:
        RunResult: Never raises for a single bad element (clause 8.4).
    """
    log = log or compat.get_log()
    started = time.time()

    volume = viewvolume.build(doc, view, values['extend_to_view_depth'])

    problems = missing_tag_types(doc, values)
    if problems:
        results = RunResult(preview)
        results.blocked = '\n'.join(problems)
        return results

    candidates, results = gather(doc, view, values, targets, volume, log,
                                 progress)
    results.preview = preview
    results.warnings.extend(volume.warnings)

    if progress is not None and progress.cancelled:
        results.cancelled = True

    if not results.cancelled:
        place_all(doc, view, values, candidates, results, volume, log,
                  preview, progress)

    results.seconds = time.time() - started
    log.info('%s finished in %.1fs: %s placed, %s skipped, %s rejected, '
             '%s error(s).', 'Preview' if preview else 'Run',
             results.seconds, results.placed, results.skipped_tagged,
             results.rejected, results.errors)
    return results
