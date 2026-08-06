# -*- coding: utf-8 -*-
"""Tag Linked Services - conditional tagging of linked MEP services.

Implements the CKR development brief "Conditional Tagging Tool for Linked
MEP Services" (Rev. 00) as a pyRevit tool.

Layering follows clause 7.8 of the brief:

    core.py        classification, clipping and the paper-space rules -
                   no Revit API, unit-tested from the repo-root suite
    config.py      the settings schema and its defaults - no Revit API
    compat.py      every version-divergent Revit call, in one file
    categories.py  what differs between pipes, ducts and cable tray
    links.py       link discovery (FR-01)
    tagtypes.py    tag family/type discovery in the host (FR-02)
    filters.py     the filter options and the element sieve (FR-03)
    viewvolume.py  the view range and crop test volume (clause 7.4)
    placement.py   tag creation and placement (FR-06, clause 7.6)
    runner.py      the three-stage funnel and the transactions (7.5, 7.7)
    report.py      the results report (FR-09)
    settings.py    named profiles (FR-11)
    ui.py          the WPF dialog

The command itself is ../script.py.
"""

VERSION = '1.0.0'
