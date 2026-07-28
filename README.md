# CKR Tools — pyRevit Extension

A small pyRevit extension of MEP tools. It adds a **CKR Tools** ribbon
tab with a **Piping** panel:

- **Pipe Spacing** — re-space parallel pipe runs around a fixed reference.
- **Pipe Insulation** — auto-apply / update pipe insulation to company standards.
- **Level Adjustment** — find the piping in a plan view that belongs to
  another level, and leave only that on screen.

and an **Annotation** panel:

- **Auto Tag** — create missing tags for selected MEP elements, align or
  distribute the heads, square off the leaders.
- **Align Tags** — align selected tags / leadered text notes to a picked
  point: heads stack at a set spacing, leaders become parallel at a set
  angle. Configurable quadrant, constant landing and intermittent layout;
  settings persist per user. See `docs/align-tags-test-plan.md`.
- **Annotation Dashboard** — an always-on-top palette that applies the same
  leader rules without picking: **Process Visible** tidies every supported
  tag in the active view (or just your selection), **Dynamic Tagging**
  applies them to each new tag as you place it. Tags are never moved — only
  their leaders are rebuilt.

## Requirements

- **Autodesk Revit** (works with 2022 through 2026; the Insulation tool
  targets 2024).
- **pyRevit** installed — https://github.com/pyrevitlabs/pyRevit/releases

No other downloads are needed; the tools use only the Revit API and pyRevit.

## Install

1. **Extract** the zip to a permanent folder you won't delete, e.g.
   `C:\PyRevitExtensions\`. Keep the folder name **`PipeSpacing.extension`**
   exactly as-is (the `.extension` suffix is required) and keep the inner
   structure intact.

2. Open **Revit**. On the **pyRevit** ribbon tab, click
   **pyRevit → Settings**.

3. Under **Custom Extension Directories**, add the **parent** folder that
   contains `PipeSpacing.extension` (e.g. `C:\PyRevitExtensions`), then
   **Save Settings**.

4. Click **pyRevit → Reload**.

5. A new **CKR Tools** tab appears with a **Piping** panel and the tool buttons.

> CLI alternative (instead of steps 2–4):
> `pyrevit extensions paths add "C:\PyRevitExtensions"` then `pyrevit reload`

## Tools

### Pipe Spacing

Adjusts the clear spacing between parallel pipe runs around a chosen reference
pipe. Connected segments (pipes + fittings + accessories) move as one run, and
crossing connector pipes are reshaped to follow. Spacing works in the active
view's plane, so horizontal pipes in a plan **and** vertical risers in a
section/elevation are supported.

1. Select the pipes to space (one or more parallel runs; include any crossing
   connectors you want carried along).
2. Click **Pipe Spacing**, then graphically pick the **reference** pipe
   (its run stays fixed).
3. Enter the required **clear distance** between adjacent pipe surfaces (mm).

Selected run pipes must be straight, in the view plane, and share one plane.

### Pipe Insulation

Creates or updates pipe insulation for every visible pipe — and its connected
fittings and valves — in the active floor plan, based on company standards.

1. Open the floor plan view.
2. Click **Pipe Insulation** and confirm the count shown.
3. Each pipe's system (CCWS / CCWR / HWS / HWR / Condensate Drain) and Nominal
   Diameter decide the thickness; fittings/valves inherit the connected pipe's
   value. A completion report is shown at the end.

All insulation standards live in the `INSULATION_STANDARDS` config block at the
top of that tool's `script.py` — edit there to change the rules.

### Level Adjustment

A read-only QA check: which piping drawn in this plan actually belongs to
another level?

1. Open the plan view you want to check (floor, MEP/engineering or ceiling
   plan — anything with an associated level).
2. Click **Level Adjustment** to check the view.
3. Review what is left on screen, then press **Esc** to restore it — or click
   an element to keep the isolation and carry on working.

Every visible **pipe, flex pipe, fitting and valve** is compared with the
view's level. The ones that match are hidden with Temporary Hide/Isolate —
along with any insulation they host, so nothing is left floating — and what
remains on screen is the review list. The rest of the model stays visible as
context.

Fittings and valves are family instances, and the parameter holding their
level differs from family to family. So the level is resolved in two passes:
the element's own level parameters first, and failing those, the level of the
run it connects to — a valve with no level of its own belongs to its pipes.
Only a genuinely orphaned element is reported as unverified, which counts as
an issue too and stays on screen. A fitting joining two levels is credited to
the view's level when it touches it, so riser transitions are not reported as
defects.

After the summary, the view is held open for review. **Esc** restores it —
Revit is in a command state at that point, so Esc cancels exactly as it does
for any other tool, and zooming and panning work throughout. Clicking an
element instead ends the review with the isolation left in place and that
element selected, ready to have its level corrected.

The button toggles as well, so you never need the eyeglasses icon on the view
control bar: click it again on a view it isolated and what it hid comes back;
click once more and the view is checked afresh. Each view remembers its own
state for the Revit session. An isolate that came from somewhere else is
cleared before scanning instead, so the count is always taken against the
full view.

Nothing is modified — no parameter is written, only the view state.

The summary breaks the results down by category; the output window adds a
full table and lists the offending elements grouped by the level they
actually belong to, each group with a **Select** link.

Phase 1 covers piping in the host model. Ducts, cable trays, conduits and
linked models are not checked.

### Align Tags

Arranges selected tags into tidy stacks with parallel leaders.

1. Select the tags / leadered text notes (or preselect them first).
2. Click **Align Tags**. The first run opens the settings dialog; after that
   a plain click reuses your last settings and goes straight to picking —
   **Shift+click** whenever you want the dialog back.
3. Pick where the stack should sit. Each pick re-aligns the set and is
   exactly one undo step. Press **Esc** to finish.

Tags group themselves into *pipe bundles*: only pipes that run parallel,
sit side by side within the Rack Width, and are tagged at the same station
along the run share a stack — so a perpendicular pipe never gets dragged
into someone else's column. With the angle set to 0 the leaders come out
straight where they can, turn a true 90° onto horizontal runs, and take a
slight tilt onto vertical ones so a leader is never drawn along its pipe.
Once every cluster is placed, overlapping stacks and crossing leaders are
resolved automatically in one further undo step.

Click **Help** in the dialog for the full option reference.

### Annotation Dashboard

The same leader rules, applied without picking anything. The palette stays
on top and Revit stays usable while it is open.

- **Process Visible** rebuilds the leaders of every supported tag in the
  active view — or just the current selection, if there is one.
- **Dynamic Tagging** keeps applying them to each new tag as you place it,
  until you untick it or close the palette. It always starts off.

Tags are never moved: each keeps the position you gave it and only its
leader changes. Use Align Tags when you want stacks arranged.

Settings for both tools live in `%APPDATA%\CKR\tag_align_settings.json`, and
anything that goes wrong is logged to `%APPDATA%\CKR\logs\`.

Author: Naveen
