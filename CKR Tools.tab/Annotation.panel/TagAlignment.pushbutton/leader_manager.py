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

Isolated on purpose: a future mode could instead take a Free End leader and
place staggered arrows / squared landings here, without touching alignment.
All document modification happens in the caller's transaction.
"""

import utils


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
