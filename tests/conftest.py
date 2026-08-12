# -*- coding: utf-8 -*-
"""Make the tool bundles importable from the repo-root test suite.

pyRevit bundles are plain folders (with spaces in their names), not packages,
so the bundle directory itself goes on sys.path - exactly what script.py does
inside Revit.

Tag Linked Services keeps its modules in a package under the bundle's lib
folder, so that folder goes on the path instead and the tests import
``ckr_taglinked.<module>``. Only the modules with no Revit API import are
reachable outside Revit, which is the point of the layering.
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ANNOTATION = os.path.join(_REPO_ROOT, 'MEP Tools.tab', 'Annotation.panel')
_PIPING = os.path.join(_REPO_ROOT, 'MEP Tools.tab', 'Piping.panel')
_BUNDLE = os.path.join(_ANNOTATION, 'AlignTags.pushbutton')
_TAGLINKED_LIB = os.path.join(_ANNOTATION, 'TagLinkedServices.pushbutton',
                              'lib')
_FLOWARROW_BUNDLE = os.path.join(_PIPING, 'DrainageFlowArrows.pushbutton')

for _path in (_BUNDLE, _TAGLINKED_LIB, _FLOWARROW_BUNDLE):
    if _path not in sys.path:
        sys.path.insert(0, _path)
