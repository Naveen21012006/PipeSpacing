# Align Tags — Test Plan

Maps each spec item to its verification step. The geometry engine is verified
automatically by the pytest suite (run `python -m pytest tests/ --cov=engine
--cov-branch` at the repo root — currently 157 tests, 100% branch coverage);
everything touching a live Revit document is verified manually with the
checklists below.

Suggested test model: the Revit MEP sample (`rme_basic_sample`), Toilet Room
plan — or any plan view with a handful of tagged pipes/fixtures, a room tag
and a leadered text note.

## Phase A — Align Tags command + configuration dialog

Setup once: pyRevit → Reload; confirm the **MEP Tools** tab → **Annotation**
panel shows the new **Align Tags** button beside **Auto Tag**.

| # | Spec item | Steps | Pass when |
|---|-----------|-------|-----------|
| A1 | Preselection honored | Select 4+ tags, click Align Tags | No selection prompt; dialog opens directly |
| A2 | Filtered selection | Click Align Tags with nothing selected | Only tags/text notes are pickable; Finish continues |
| A3 | Mixed selection | Select element tags + a room tag + a leadered text note | All are processed in one run |
| A4 | Keynote tags | Include a keynote tag | Aligned like any other tag |
| A5 | Pick loop | Proceed, then pick several points in sequence | Each pick re-aligns the whole set to the new point |
| A6 | Esc finishes | Press Esc after some picks | Loop ends quietly; the LAST alignment persists |
| A7 | One undo step per pick | Make 3 picks, press Ctrl+Z 3 times | Each undo reverts exactly one pick ("Align Tags" entries) |
| A8 | Anchor = lowest head | Pick a point | Lowest tag head lands on the picked point; stack grows upward |
| A9 | Vertical Spacing | Set 500 (mm project) and re-pick | Clear gap between stacked tag texts measures 500 mm in the model (row pitch = tag height + 500) |
| A10 | Leaders parallel at Angle | Set Angle 30, 45, 60 | All slanted segments parallel at the set angle; landings horizontal and straight |
| A11 | Leaders never cross | Tag a scattered cluster, align | No two leaders intersect |
| A12 | Four modes | Run each quadrant button once | Stack sits in the chosen quadrant; leaders exit the correct side (see note 1) |
| A13 | Constant Landing | Enable, set Landing 1000 | Every landing measures exactly 1000 mm; heads follow the leaders instead of a straight column |
| A14 | Intermittent Alignment | Enable, Horizontal Spacing 2000 | Two staggered columns at half the row height; odd rows offset AWAY from the elements |
| A15 | Switch Pick Point Side | Toggle on, re-pick, with **Order by pipe position OFF** | Leader exit side mirrors (see note 1) |
| A15b | Switch Side is inert in ordered mode | Toggle it on and off with **Order by pipe position ON**, picking the same spot each time | The result is identical both ways — with ordering on, the side comes from the click, not the dialog |
| A16 | Attached End Tags off (default) | Include a tag with Attached leader end | Tag is aligned; arrowhead stays glued to the element; output notes the slant angle is approximate for it |
| A17 | Attached End Tags on | Enable checkbox, repeat | Leader end becomes Free, pinned at the element's nearest point; slant angle exact; arrowhead may shift slightly |
| A18 | Tags without leaders | Include one | Skipped + reported, never moved |
| A19 | Pinned tags | Pin one tag | Skipped + reported |
| A20 | Multi-leader tag | Tag with two tagged references | Both leaders get elbows at the angle |
| A21 | Text note justification | Set Left / Right / Automatic on a leadered note | Justification applied (tooltip warns the arrowhead may move); Unchanged leaves it alone |
| A22 | Keep Selection After Use | On (default) | Tags still selected after Esc |
| A23 | Keep Selection off | Toggle off | Selection cleared after Esc |
| A24 | Turn Snaps Off | Toggle on | Pick point does not snap to geometry |
| A25 | Section/elevation | Repeat A8 in a section view | Alignment works in the view plane, not world XY |
| A26 | Wrong-quadrant pick (ordering OFF) | With Upper-Left selected and **Order by pipe position OFF**, pick above-RIGHT of the tagged elements | Quadrant auto-switches for that pick; output explains and names the button to press for a permanent change |
| A26b | **Side follows the click (ordering ON)** | With ordering ON, pick once LEFT of a bundle and once RIGHT of it, whatever the dialog quadrant says | Both land correctly first time. Leaders exit right from a left-hand pick, left from a right-hand one. **No "Quadrant switched" message** — the side was never wrong to begin with |
| A26c | Cold start, both sides | Fresh Revit session, no prior placement. One right-hand pick; then repeat after a left-hand pick; then two right-hand picks in a row | All three right-hand results identical, and P sits on the bottom-left corner of the text block every time. The log's `bottom-left residual` line reads ~0 mm |
| A26d | **Self-correcting pick** | Align a below-left stack (leaders exit right), then RERUN on the same tags several times, picking a marked point each time | The drawn bottom-left corner lands ON the mark every run — no drift. Log shows `Drawn corner missed the pick by …; re-planning` then `Drawn corner verified on the pick` (or nothing when the first pass already landed) |
| A26e | **Upper stacks self-correct too** | Repeat A26d picking ABOVE the pipes (leaders descend) | Vertical lands on the pick as well: the tool derives the text bottom from the drawn landing height and top edge (bottom = 2·mid − top). For an Upper-Right stack the horizontal stays as placed (the left edge carries the leader, so u cannot be verified there) — if its u drifts, that is the one remaining known gap |
| A27 | Order by pipe position | Tag a vertical riser bundle, align with the checkbox on (default) | Stack reads left-to-right: leftmost pipe = top tag; arrows re-placed on their pipes along the leader line. Horizontal runs: topmost pipe = top tag |
| A28 | Straight leaders (angle 0) | Set Angle 0, align a riser bundle | Leaders are horizontal, arrow at each tag's own height; the elbow grip sits at the MIDPOINT of the line (drag-friendly); a tag whose pipe doesn't reach its height gets a small individual slant (output reports the count) |
| A29 | Pick anchors the corner | Watch where the stack lands relative to the click | The click is the BOTTOM-LEFT corner of the lowest tag's text, **on either side of the pipes**; text left edges align up the column |
| A29b | **Left-edge column both sides** | Tag a bundle from the LEFT, then tag another from the RIGHT, mixing short and long tag texts | Both stacks line their text up on the LEFT edge, ragged on the right. For the left-hand stack that is the OUTSIDE edge; for the right-hand stack it is the INSIDE edge, so every landing leaves the column at the same u and the leaders comb out level instead of staggering |
| A30 | Auto-split clusters | Select BOTH riser groups at once, align | Output: "Selection split into 2 clusters"; prompt reads "Cluster 1 of 2"; active cluster highlighted; one pick per group |
| A31 | Skip a cluster | Press Esc before picking for cluster 1 | Cluster 1 untouched; prompt moves to cluster 2 |
| A32 | Cluster Distance 0 | Set 0, repeat A30 | Never splits — one stack for everything (old behavior) |
| A33 | Mixed pipe directions | One cluster containing risers + a horizontal branch tag | Risers get pure straight lines; the branch tag gets an L-leader (horizontal landing + 90° turn onto the pipe at its arrow position); no scissoring leaders |
| A34 | L-bends on horizontal pipes | Angle 0, align a cluster of stacked horizontal pipes | Each leader: horizontal landing, then a TRUE 90° vertical bend onto its pipe; top tag = top pipe; the TILT_DEG tilt (15° since 2026-09-03) applies only to climbs onto VERTICAL pipes |
| A35 | Short-pipe arrow fan | Repeat A34 where the pipes are short (a corner stub) | Arrows spaced equidistantly within the pipes' extent, nearest row first — clean stepped look |
| A37 | Vertical drops from below | Angle 0, tags below a group of vertical drop pipes | Tilted climbs (TILT_DEG, 15°) onto each drop, arrows fanned UP the pipes with clearance from the ends — the leader is never drawn along the pipe line; at a mixed corner, level tags stay straight while climb tags tilt |
| A38 | Clusters = pipe bundles | Select tags across a corner (riser bundle + runs), plus two separate risers | Each PARALLEL side-by-side rack is its own cluster: perpendicular pipes never mix; two racks apart laterally never mix; the same rack tagged at two distant stations gives two clusters. Mixed stacks only with Cluster Distance 0 |
| A39 | Fan follows real pipe side | Mode Lower-Left, pipes BELOW the stack | Bottom row turns nearest, upper rows step outward — no climb crosses a landing (the case-1 rework) |
| A40 | Clearance + near window | Angle 0 at a corner into other pipework | Arrows keep ~250 mm from bends/ends and the fan stays in the near 60% of the run — no climb hugs the far bundle |
| A41 | Auto chain cap | Select a whole corridor (20+ tags), Cluster Distance 2000 | Chains auto-split at their widest gaps (max 10 tags / ~4 m span per cluster) — no mega-stack; Cluster Distance 0 still means never split |
| A42 | Active-cluster markers | Multi-cluster run | Orange ring markers sit on the active cluster's tags THROUGH the pick prompt, jump to the next group on Esc, and vanish when the command ends |
| A43 | Final arrangement | Place two clusters so their stacks/leaders overlap, Esc to finish | The later-placed cluster auto-moves to clear the overlap (250 mm margin); output reports what moved; ONE Ctrl+Z reverts the whole cleanup; earlier cluster never moves |
| A45 | Obstacles from earlier runs | Align a cluster onto a stack placed in a PREVIOUS run, Esc | Final arrangement moves the new cluster off the old one (the old never moves) — the margin-band collision case |
| A46 | Ground-truth audit | Any run | If Revit puts a head >10 mm off-plan, the output warns and the log names the tag; silent runs mean everything landed as planned |
| A47 | Auto snapshot | Any run with picks | `%APPDATA%\CKR\logs\align_check*.png` holds the drawn result for review without screenshots |
| A48 | Mid-span bends on long runs | Angle 0, tag a LONG horizontal run, pick at its middle | Landing + 90°-style tilted bend appear right at the pick (no "Pick skipped"); corner stubs behave exactly as before |
| A36 | Re-align refreshes elbows | Align, then re-align the same tags to a new spot; select one | Elbow bubble sits at the NEW midpoint (not the old one); dragging the tag toward the pipe works freely |

## Dialog + persistence

| # | Spec item | Steps | Pass when |
|---|-----------|-------|-----------|
| B1 | Defaults | Delete `%APPDATA%\CKR\tag_align_settings.json`, open dialog | Angle 45, Vertical 60.96 mm, Landing 1524 mm, Horizontal 3048 mm, Keep Selection ON, everything else OFF, justification Unchanged |
| B2 | Quadrant highlight | Click each quadrant button | Clicked one highlights, previous un-highlights |
| B3 | Slider ↔ textbox | Drag slider; type in box | Both stay in sync; typing junk doesn't crash |
| B4 | Angle clamp | Type 0 or 90, Proceed | Value clamps into 1–89 |
| B5 | Project units | Open dialog in a project set to inches | Fields show inch values with the right unit label; typed values round-trip |
| B6 | Persistence | Change several settings, Proceed, close Revit, reopen dialog | All values restored (`%APPDATA%\CKR\tag_align_settings.json` exists) |
| B7 | Corrupt settings | Put garbage in the JSON file, start tool | Tool starts with defaults, no error |
| B8 | Help | Click Help | Help text appears |
| B9 | Cancel | Click Cancel / press Esc in dialog | Nothing modified, no pick prompt |

## Quality gates (spot checks)

| # | Gate | Check |
|---|------|-------|
| Q1 | No unhandled exceptions | Try hostile inputs (empty view, cancel everything, weird units) — never a Revit error dialog with a stack trace |
| Q2 | Errors logged | Force an error (e.g. read-only settings folder) — `%APPDATA%\CKR\logs\align_tags.log` records it |
| Q3 | Transactions | Steps A5–A7 above prove pick = transaction = undo step |

### Notes / known interpretation choices (flag anything that feels wrong)

1. **Mode names** describe where the TAG STACK sits relative to the elements
   (Upper-Left = tags above-left, leaders slant down-right). **Switch Pick
   Point Side** currently mirrors the leader exit side (UL⇄UR, LL⇄LR).
2. **Vertical Spacing is the clear gap between stacked tags**: the tallest
   tag's text height is measured per view (leaders suppressed inside a
   rolled-back transaction) and rows stack at tag-height + gap, so the
   default 60.96 mm gap works at any view scale. Landing Distance and
   Horizontal Spacing remain plain model distances in the view plane.
3. **Intermittent** offsets odd rows AWAY from the elements and halves the
   row step (same-column tags keep the full Vertical Spacing).
4. With **Constant Landing**, the picked point sets the stack's height and
   first row; head u-positions derive from the leaders (spec trade-off: you
   cannot fix ends + angle + landing + a straight head column simultaneously).
5. **Constant Landing takes precedence over Intermittent**: with a fixed
   landing each head's horizontal position is dictated by its leader, so
   there is no column to stagger — Intermittent is ignored (full row step)
   while Constant Landing is on. Tooltips say so.
6. Tags whose leader points at a **linked model** element resolve the end
   through the link transform; an unloaded link puts the tag in the
   "leader end could not be read" skip report. Attached-end tags into the
   host model anchor at the nearest point on the element to the tag head.
7. **Order by pipe position** requires every selected tag to point at a
   curve element (pipe/duct/tray); otherwise the tool says so once and
   uses standard alignment. In ordered mode the arrowheads are re-placed
   on the pipes (free ends moved; attached ends steered by elbow, or
   freed+pinned when "Attached End Tags" is on).

## Phase B (deferred): Annotation Dashboard, Dynamic Tagging, help HTML

Test plan lands with Phase B.
