# Auto Tag → Align Tags formatting handoff

For the chat working on `CKR Tools.tab/Annotation.panel/TagAlignment.pushbutton`
(Auto Tag). Goal: tags that Auto Tag places come out formatted **exactly like
Align Tags at angle 0** with the user's approved defaults — same datum, same
leader shapes, same spacing — so a stack made by either tool is
indistinguishable.

The user has field-tested every rule in here on `AlignTags.pushbutton` through
dozens of Revit sessions. Where this doc and your instincts disagree, this doc
wins — most of these rules exist because the obvious approach failed on screen.

## 0 · Prime directive: import, don't reimplement

`AlignTags.pushbutton` (sibling folder) contains the tested implementation.
Your `engine_bridge.py` already appends it to `sys.path` and imports
`engine`. Extend that pattern to everything below — `engine.plan_ordered` for
layout, and the helpers in its `script.py` where marked. Reimplementing any of
it will diverge, and has (see §4).

## 1 · Settings: read the shared file, never hard-code

Read `%APPDATA%\CKR\tag_align_settings.json` at run time (via the sibling
`settings.py`'s `load()` if importable, else plain json). The user tunes ONE
dialog; both tools must follow. Fallback defaults (the user's approved
values) only when the file is missing:

| key | value | meaning — read carefully |
|---|---|---|
| `angle_deg` | **0** | straight-leader ruleset (§3). Not "horizontal only" — a full shape system |
| `landing_mm` | **1524** | horizontal run from text before a bend; also the anchor offset (§2) |
| `vertical_mm` | **100** | **CLEAR GAP between texts, not row pitch.** Row pitch = tallest drawn text height + this. Your bridge currently passes it as centre-to-centre pitch — that stacks rows overlapping |
| `horizontal_mm` | 50 | column offset for Intermittent only; inert at these defaults |
| `cluster_mm` | **1500** | max arrow separation ALONG the run within one bundle; 0 = never split |
| `learned_left_mm` | (grows at runtime, currently ≈815) | the family's drawn head→text-left-edge distance, measured by Align Tags' self-correction. **Free calibration — use it (§4)** |
| `attached_end` | true | free attached leader ends and PIN them so geometry is exact |
| `justification` | automatic | TextNotes only — irrelevant to tags |
| `switch_side` | true | **IGNORE in ordered layouts.** The side is geometric now (§2); this checkbox only affects the legacy non-ordered path |
| `mode` | LL | supplies Upper/Lower only; Left/Right comes from geometry (§2) |

## 2 · Anchor: where a stack sits (user decision, 2026-08-02)

No pick exists in Auto Tag, so the anchor derives from the pipes:

- **Side** — geometric, per cluster: stack goes on the side of the bundle
  where the tags/space are; compute like `script.side_mode` does (compare
  column u with the pipes' mean u — your `engine_bridge._mode` already does
  this correctly). Upper/Lower from the dialog `mode`; Left/Right never from it.
- **Horizontal** — the text block sits **one `landing_mm` clear of the
  bundle's nearest pipe**:
  - stack LEFT of pipes (leaders exit right): datum column =
    `leftmost_pipe_u − landing − widest_text_width`
  - stack RIGHT of pipes (leaders exit left): datum column =
    `rightmost_pipe_u + landing`
- **Vertical** — keep Auto Tag's existing row baseline logic (lowest row's
  bottom edge = its baseline).

**The datum is ALWAYS the text block's bottom-LEFT corner, both sides.**
Left-hand stacks are ragged on the right (leaders start at each text's own
right edge), right-hand stacks are ragged on the right too — the flush left
column faces the pipes and every landing leaves it at the same u. Do not
mirror the corner with the side. This is the single most user-litigated rule
in the tool.

## 3 · The angle-0 ruleset (what `engine.plan_ordered` gives you)

Call `engine.plan_ordered(anchor, items, mode, 0.0, pitch, landing,
horizontal, bundle, clearance=mm_to_feet(250))` with per-item `head_offset`,
`line_offset`, `exit_edge` (§4). You then get, per tag: `head`, `elbow`,
`end` — apply all three (`end` re-places the arrow ON the pipe). The rules it
implements, so you can verify output rather than trust it:

- **Ordering**: vertical bundles read left pipe → top tag; horizontal bundles
  top pipe → top tag. Sort is side-invariant.
- **Level with a vertical pipe** → one straight horizontal leader, elbow grip
  parked at the line's midpoint.
- **Reaching a horizontal pipe** → horizontal landing + TRUE 90° bend onto
  the pipe. Turns fan equidistantly, anchored ahead of the text (never at the
  pipe's far end), confined to the near 60% of the run.
- **Climbing to a vertical pipe** → landing + climb leaning 7.5°
  (`engine.TILT_DEG`) off vertical, so a leader never draws along a pipe.
- **Arrows** keep 250 mm clearance from bends/ends/fittings and from each
  other on short runs.
- **Clusters are physical pipe bundles**: parallel within 10°, side-by-side
  within 600 mm (`BUNDLE_LATERAL_MM`), tagged within `cluster_mm` along the
  run; never mix perpendicular pipes; auto-split at 10 tags / 4 m span
  (`clusters.bundle_clusters` — import it).
- **Row pitch** = tallest drawn text height + `vertical_mm`.

## 4 · The family lies — do not trust what you measure beforehand

Hard-won facts about the tag family (`QIC-MEP-Tag-Pipe-Tag`), all
log-proven; your bridge currently trips on the first one:

1. **`TagHeadPosition` is NOT the text corner.** The drawn text's left edge
   sits `learned_left_mm` (≈815 mm) LEFT of the head. Your bridge passes no
   `head_offset`, so it aligns heads — every stack lands ~815 mm right of
   where the datum should be, ragged on both sides.
2. **Bounding boxes are unreliable in every state.** They include leaders
   (`HasLeader=False` is accepted but still draws), and the family
   re-anchors its text around the head depending on leader state — a box
   measured with leaders collapsed describes a tag that is never drawn.
   Median-guard anything box-derived (see `ordered_plan`'s outlier snap).
3. **Verification beats prediction.** Align Tags places, measures the DRAWN
   corner, re-plans once with the residual, all in one TransactionGroup
   (`Assimilate` → one undo). Reuse `script.correction_from_spans` +
   `_drawn_correction` logic: box sides are only clean where the leader is
   not; leader direction comes from the drawn plan (arrow vs landing), never
   from the mode; exit-left corners derive from `head − learned_left_mm`.
4. **Practical recipe for new tags**: place the head at
   `datum_u + mm_to_feet(learned_left_mm)`; `line_offset` = half drawn text
   height; `exit_edge` = text width (estimate: longest line ×
   `TEXT_SIZE` × view scale × 0.6 — see `script._text_width_hint`). Then
   verify-and-correct as in (3). If `learned_left_mm` is 0 (never taught),
   fall back to the width estimate and log it.
5. Python's `logging` module is silenced under this pyRevit host — use
   `common.get_file_logger()` (direct-write) for anything you want to see.

## 5 · Acceptance (mirror of the user's sign-off on Align Tags)

1. A stack Auto Tag places and a stack Align Tags places on the same bundle
   are visually identical: same datum edge, shapes, pitch, fans.
2. Text left edges flush on one column, both sides of the pipes; ragged
   edges never face a flush requirement.
3. No leader drawn along a pipe; horizontal runs get true 90° bends;
   vertical climbs lean 7.5°.
4. Rows never overlap: pitch = drawn text height + 100 mm clear.
5. Re-running Auto Tag over its own output does not shift anything
   (verify-correct loop converges; nothing measured from stale leaders).
6. One undo step per cluster.

Questions → the Align Tags chat (this doc's author) arbitrates formatting;
`docs/align-tags-test-plan.md` rows A26b–A26e document the correction
behaviour these rules come from.
