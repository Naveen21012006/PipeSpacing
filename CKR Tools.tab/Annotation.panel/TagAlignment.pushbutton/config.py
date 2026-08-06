# -*- coding: utf-8 -*-
"""Auto Tag - Shift+click entry point (pyRevit convention).

pyRevit runs a bundle file named config.py when the button is SHIFT+CLICKED,
instead of script.py. This one opens the local spacing control: the clear gap
between two texts. (The tool's actual configuration constants live in
tool_config.py - this file must stay a thin runnable script.)
"""

import os
import sys

_BUNDLE_DIR = os.path.dirname(__file__)
if _BUNDLE_DIR not in sys.path:
    sys.path.append(_BUNDLE_DIR)

import spacing_control

if __name__ == '__main__':
    spacing_control.ask_text_gap(confirm=True)
