# MEP Tools — pyRevit Extension

A small pyRevit extension of MEP tools. It adds an **MEP Tools** ribbon
tab with a **Piping** panel:

- **Pipe Spacing** — re-space parallel pipe runs around a fixed reference.
- **Pipe Insulation** — auto-apply / update pipe insulation to company standards.
- **Level Adjustment** — find the piping in a plan view that belongs to
  another level, and leave only that on screen.
- **Workset Filter** — tick the worksets to hide in the active view;
  the rest stay on screen. Click again to restore.
- **2D Elements Filter** — hide the 3D model in the active view and leave
  only the 2D annotation and detail elements for review. Click again to
  restore.

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
- **Tag Linked Services** — tag pipes, ducts and cable trays that live in a
  **linked** model, in the active floor plan, and only the runs that earn a
  tag. See `docs/tag-linked-services.md`.
- **Purge Orphaned Tags** — delete the tags left pointing at nothing after a
  link is reissued.

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

5. A new **MEP Tools** tab appears with a **Piping** panel and the tool buttons.

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

### Workset Filter

Temporarily hides chosen worksets in the active view, leaving the rest on
screen. Needs a workshared model.

1. Open any graphical model view (plan, section, elevation or 3D).
2. Click **Workset Filter** and tick the worksets to hide — the list shows
   every user workset, sorted, with a search box and Check All / Uncheck All.
3. Click **Hide Selected Worksets**. A summary reports the workset counts and
   how many elements were hidden.

The hide is Revit's Temporary Hide/Isolate, applied per view — workset
settings, view visibility settings and the model itself are never modified,
and the view control bar's **Reset Temporary Hide/Isolate** clears it like
any other. The button toggles too: click it again on a view it filtered and
the hidden worksets come back; click once more to choose a new set. Each
view remembers its own filter for the Revit session.

A linked model is one element on one workset, so ticking a link's workset
hides or shows the whole link; elements *inside* a link are not filtered
individually.

### 2D Elements Filter

One click strips the model out of the active view so the annotations can be
reviewed on their own — text notes, detail lines, detail components, filled
regions, tags, revision clouds, spot dimensions, generic annotations,
detail groups and every other view-specific element stay on screen; every
visible 3D model element is hidden, and so are the datum and reference
marks — grids, levels, reference planes, dimensions and the section /
elevation / callout marks of other views — which would only clutter the
review.

1. Open the view to review (plan, ceiling plan, section, elevation,
   drafting or 3D view).
2. Click **2D Elements Filter**. A summary reports the active view, how
   many 2D elements stayed visible — broken down by category — and how
   many 3D elements were hidden.
3. Click it again to restore the view.

An element counts as 2D when it is view-specific, or when its category is
an annotation category — except the datum and reference marks above, which
are hidden by default. The hide is Revit's Temporary Hide/Isolate, applied
per view —
view settings and the model itself are never modified, and the view control
bar's **Reset Temporary Hide/Isolate** clears it like any other. The button
toggles like Level Adjustment and Workset Filter, and each view remembers
its own state for the Revit session; an isolate that came from somewhere
else is cleared before scanning so the counts are always taken against the
full view. A drafting view is reported as already all-2D, and a view with
no annotations at all is left untouched rather than emptied.

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

### Tag Linked Services

Revit's **Tag All Not Tagged** cannot annotate elements inside a linked
model, and has no conditions. This does both: it reproduces that workflow
for link content in the active floor plan, and tags only the runs worth
annotating.

1. Open the floor plan and click **Tag Linked Services**.
2. **Links and tags** — tick the link instances to read, the categories to
   tag, and the tag family:type for each. Risers can have their own tag
   family.
3. **Filters** — narrow by family/type, system classification, system type,
   size range, reference level or workset. Ticking nothing in a list means
   that filter is off, never that nothing passes.
4. **Rules** — the angle that separates horizontal from vertical, and the
   minimum length a run must show before it earns a tag.
5. **Preview** reports the counts without leaving a single tag behind, so
   the thresholds can be tuned against real numbers. **Place Tags** commits,
   as one undo step.

The tags it just created are left **selected**, so **Align Tags** can stack
them straight away without picking them again. A preview selects nothing —
its tags never existed.

Four things it gets right that a first attempt usually does not:

- **Graded drainage still counts as horizontal.** Soil and waste run at
  1:100 to 1:40, so runs are classified by *angle* (15° by default), not by
  a flat test — otherwise every drainage run in the model is missed.
- **Length is measured in this view, not on the element.** A 30 m riser
  drawn as one element is judged by the part inside *this* plan's view
  range, so it is not tagged identically on eight floors.
- **Tags land inside the annotation crop.** A tag head outside it is not
  drawn on the sheet however visible its pipe is, so insertion points are
  clamped, and a long run whose midpoint falls outside the crop is tagged
  at the middle of the part you can see.
- **Offsets are millimetres on the sheet.** They are multiplied by the view
  scale, so the same settings measure identically at 1:50 and 1:200.

Every run reports what it did — placed, already tagged, and each reason for
rejection — in the output window, with a CSV copy written alongside the log.

Settings profiles, logs and CSV reports live under
`%APPDATA%\CKR\TagLinkedServices\`. Implementation notes, the deviations
from the development brief and the acceptance test list are in
`docs/tag-linked-services.md`.

Phase 1 covers floor plans, and pipes, ducts and cable trays. Fittings,
sections, nested links and multi-view batch runs are not included.

### Purge Orphaned Tags

Tags on linked elements are stored in *this* model. When the link is
reissued with elements deleted or regenerated, those tags orphan — they stay
on the sheet, empty, pointing at nothing, and the drawing degrades quietly
between issues.

Open the view, click **Purge Orphaned Tags**: it lists what it found in the
output window, asks once, and deletes them in a single undo step. Nothing
else is touched, and a view with no orphans says so.

Author: Naveen
