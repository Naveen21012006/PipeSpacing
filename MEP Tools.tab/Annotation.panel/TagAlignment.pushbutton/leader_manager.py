# -*- coding: utf-8 -*-
"""Leader refresh for the MEP Tag Alignment tool.

Moving a tag head leaves its leader elbow where it was, which looks messy -
and on some tags (a pipe square-on to the tag) the elbow can even collapse
onto the arrowhead, leaving no grip at all.

The most reliable cure, and the one that matches what Revit does when you
uncheck and recheck a leader by hand, is exactly that: as the final step, turn
every leader off, regenerate, then turn it back on. Revit rebuilds each leader
cleanly from the tag's final position, with a proper elbow and grip. We
deliberately let Revit own the geometry rather than compute elbows ourselves.

Isolated on purpose: the horizontal-pipe mode does exactly what this docstring
foresaw - instead of the toggle-rebuild, it sets an explicit elbow (and, in the
free-end fallback, an explicit arrow) so the leader turns 90 degrees down to the
pipe. See apply_elbows(). All document modification happens in the caller's
transaction.
"""

from Autodesk.Revit.DB import LeaderEndCondition

import utils


def _tagged_reference(tag):
    """Return one Reference the tag points at, for the per-reference leader API.

    Revit 2022+ addresses a tag's leader by the reference it points at; return
    the first one, or None so the caller can fall back to the old properties.
    """
    try:
        references = list(tag.GetTaggedReferences())
        return references[0] if references else None
    except Exception:
        return None


def _set_elbow(tag, reference, point):
    """Set a tag's leader elbow across Revit versions. Returns True on success.

    The reference-based SetLeaderElbow(reference, point) is the API for the
    whole supported range (2022-2025). The parameterless LeaderElbow property
    is only a pre-2023 safety net - it was REMOVED in Revit 2023, so it is
    guarded with hasattr; on 2023+ its absence is a clean miss, not an
    AttributeError. Never raises, so the caller's count stays honest.
    """
    if reference is not None:
        try:
            tag.SetLeaderElbow(reference, point)
            return True
        except Exception:
            pass
    try:
        if hasattr(tag, 'LeaderElbow'):
            tag.LeaderElbow = point
            return True
    except Exception:
        pass
    return False


def _set_end(tag, reference, point):
    """Set a tag's free leader end across Revit versions (see _set_elbow)."""
    if reference is not None:
        try:
            tag.SetLeaderEnd(reference, point)
            return True
        except Exception:
            pass
    try:
        if hasattr(tag, 'LeaderEnd'):
            tag.LeaderEnd = point
            return True
    except Exception:
        pass
    return False


class LeaderManager(object):
    """Refreshes leaders so Revit redraws them cleanly after tags move."""

    def __init__(self, doc, view):
        """
        Args:
            doc: The active Document.
            view: The view the tags live in (kept for a future Free End mode).
        """
        self.doc = doc
        self.view = view

    def maintain(self, tags):
        """Toggle every leader off then on so Revit rebuilds it cleanly.

        Done in two passes with a single regenerate between them, so the whole
        set costs two regenerations rather than two per tag. Tags without a
        leader are left untouched. This is cosmetic work - a failure on one tag
        is logged, never fatal.

        Args:
            tags (list): IndependentTag objects.

        Returns:
            tuple: (refreshed, failures) where failures is a list of
            (tag_id, message).
        """
        toggled = []
        failures = []

        # Pass 1: leaders off.
        for tag in tags:
            tag_id = utils.element_id_value(tag.Id)
            try:
                if tag.HasLeader:
                    tag.HasLeader = False
                    toggled.append(tag)
            except Exception as ex:
                failures.append((tag_id, str(ex)))
                utils.logger.debug('Leader off failed on tag {}: {}'.format(
                    tag_id, ex))

        if not toggled:
            return 0, failures

        self.doc.Regenerate()

        # Pass 2: leaders back on - Revit redraws each from the final position.
        refreshed = 0
        for tag in toggled:
            tag_id = utils.element_id_value(tag.Id)
            try:
                tag.HasLeader = True
                refreshed += 1
            except Exception as ex:
                failures.append((tag_id, str(ex)))
                utils.logger.debug('Leader on failed on tag {}: {}'.format(
                    tag_id, ex))

        self.doc.Regenerate()

        utils.logger.debug('Refreshed {} leader(s).'.format(refreshed))
        return refreshed, failures

    def apply_elbows(self, plan, free_end=False):
        """Give each planned tag an L-shaped (90-degree) leader.

        Used for horizontal pipes: the alignment strategy has already stacked
        the heads in a column and worked out, per tag, where the leader should
        turn down (the elbow) and where it meets the pipe (the arrow). Here we
        just apply that geometry.

        Attached mode (default): set only the elbow; Revit slides the attached
        arrow along the pipe to sit under it, so the leader stays linked and
        follows the pipe. Free-end mode: also pin the arrow point, guaranteeing
        the clean L even if a Revit build won't slide the attached arrow.

        Args:
            plan (list): (tag, elbow_point, arrow_point) tuples.
            free_end (bool): True to pin the arrow (config.HORIZONTAL_LEADER_
                FREE_END). False to leave it attached and elbow-driven.

        Returns:
            tuple: (updated, failures) where failures is a list of
            (tag_id, message). Cosmetic work - one failure is logged, not fatal.
        """
        updated = 0
        failures = []

        for tag, elbow, arrow in plan:
            tag_id = utils.element_id_value(tag.Id)
            try:
                if not tag.HasLeader:
                    tag.HasLeader = True
                reference = _tagged_reference(tag)

                if free_end:
                    try:
                        tag.LeaderEndCondition = LeaderEndCondition.Free
                    except Exception:
                        pass
                    ok = _set_end(tag, reference, arrow)
                    ok = _set_elbow(tag, reference, elbow) and ok
                else:
                    try:
                        tag.LeaderEndCondition = LeaderEndCondition.Attached
                    except Exception:
                        pass
                    ok = _set_elbow(tag, reference, elbow)

                if ok:
                    updated += 1
                else:
                    failures.append(
                        (tag_id, 'could not set the leader elbow on this '
                                 'Revit version'))
            except Exception as ex:
                failures.append((tag_id, str(ex)))
                utils.logger.debug('L-leader failed on tag {}: {}'.format(
                    tag_id, ex))

        self.doc.Regenerate()

        utils.logger.debug('Set {} L-leader(s).'.format(updated))
        return updated, failures
