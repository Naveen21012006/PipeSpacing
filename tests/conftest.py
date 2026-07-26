# -*- coding: utf-8 -*-
"""Make the Align Tags bundle importable from the repo-root test suite.

pyRevit bundles are plain folders (with spaces in their names), not packages,
so the bundle directory itself goes on sys.path - exactly what script.py does
inside Revit.
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BUNDLE = os.path.join(
    _REPO_ROOT, 'CKR Tools.tab', 'Annotation.panel', 'AlignTags.pushbutton')

if _BUNDLE not in sys.path:
    sys.path.insert(0, _BUNDLE)
