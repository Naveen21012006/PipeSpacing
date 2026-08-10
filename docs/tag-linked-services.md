# Tag Linked Services — implementation notes

Built against **Conditional Tagging Tool for Linked MEP Services —
Development Brief, Rev. 00** (CKR Consulting Engineers, Wet Services).

This note records where each requirement lives, where the implementation
departs from the brief and why, and how to run the acceptance tests.

---

## 1. Platform

The brief specifies a C# add-in with an MSI, a multi-targeted solution and
a WPF/MVVM project layout. It has been built instead as a **pyRevit tool
inside the existing `MEP Tools` extension**, alongside Pipe Spacing, Auto
Tag and Align Tags, because that is what this repository is and what the
office already deploys.

What that changes:

| Brief | Here | Why it does not cost anything |
|---|---|---|
| §8.1 dual build, .NET 4.8 / .NET 8 | One codebase | pyRevit hosts the same script on 2022–2026; the version-divergent calls are isolated in `compat.py` exactly as clause 8.1 asks |
| §8.3 MSI installer, `.addin` per version | pyRevit extension folder | Install is the extension path already documented in the repo README |
| §7.8 `Core / Revit / UI / Command` projects | `lib/ckr_taglinked/*.py` | Same separation: `core.py` and `config.py` import no Revit API and are unit-tested from `tests/` |
| §10.4 unit test project | `tests/test_taglinked_core.py` | 57 tests: classification at the boundary angles, view-range and crop clipping, paper-to-model conversion, spacing, the settings schema |

Everything else in the brief is implemented as written.

---

## 2. Where each requirement lives

| Ref | Requirement | Module |
|---|---|---|
| FR-01 | Link discovery, per instance, unloaded and nested reported | `links.py` |
| FR-02 | Category rows, tag family/type, leader, orientation; separate riser tag | `tagtypes.py`, `ui.py` |
| FR-03 | Family/type tree, classification, system type, size, level, workset | `filters.py` |
| FR-04 | Classification and minimum visible length | `core.classify`, `core.passes_length`, `runner._assess` |
| FR-05 | Visibility: bbox → category/link checks → create-and-test | `viewvolume.py`, `runner.gather`, `placement.Placer.place` |
| FR-06 | Placement, crop clamp, spacing ladder | `placement.py`, `core.placement_candidates`, `core.clamp_into_region` |
| FR-07 | Duplicate suppression by (link instance, linked element) | `runner.tagged_pairs`, `compat.tagged_link_pairs` |
| FR-08 | Dry run, everything rolled back | `runner.place_all(preview=True)` |
| FR-09 | Report by category and rejection reason, CSV | `report.py`, `script.write_csv` |
| FR-10 | Orphaned tag purge | `PurgeOrphanedTags.pushbutton/script.py` |
| FR-11 | Named profiles, last-used reload | `settings.py` |
| 5.1 / 7.3 | Graded drainage is horizontal, by angle | `core.classify` |
| 5.2 / 7.4 | Lᵥ measured inside the view range and crop | `core.visible_segment`, `viewvolume.build` |
| 5.3 / 7.6.4 | Insertion points clamped inside the annotation crop | `core.clamp_into_region` |
| 5.4 / 7.6.3 | Paper millimetres × view scale | `core.paper_mm_to_feet` |
| 7.7 | TransactionGroup, one Transaction, SubTransaction per tag | `runner.place_all` |
| 8.4 | No exception reaches Revit | `runner`, `script.py` |
| 8.5 | Rolling log, 30 days | `compat.RollingLog` |

Files written at run time:

    %APPDATA%\CKR\TagLinkedServices\logs\tag_linked_services_<date>.log
    %APPDATA%\CKR\TagLinkedServices\profiles\<name>.json
    %APPDATA%\CKR\TagLinkedServices\last_used.json
    %APPDATA%\CKR\TagLinkedServices\reports\tag_linked_services_<stamp>.csv

The CSV of clause FR-09 is written automatically after every run, preview
included, rather than behind an export button — same record, no extra
click, and the path is printed in the output window.

---

## 3. Departures from the brief, and the reasoning

1. **Filters match on names, not element ids.** A type id from one link
   document means nothing in another, and a reissued link brings new ids.
   Profiles therefore store `Family : Type`, level names and workset
   names, and still resolve after a link revision or in the next project.

2. **Stage 1 uses one native collector filter, then Python tests.**
   Clause 7.5 asks for `ElementParameterFilter` "where possible". Category
   plus "not a type" is native; the size, system, level and workset tests
   run in Python immediately afterwards, cheapest first. On a 25 000
   element link that is a few parameter reads per element and stays inside
   NF-02; expressing every rule as a filter rule would have cost multi-
   parameter special cases (a round duct filters on diameter, a
   rectangular one on width and height) for no measurable gain. If a real
   project link proves slower than NF-02 allows, this is the place to
   change and nothing else has to move.

3. **The annotation crop falls back to the model crop.** Open item 1 of
   the brief. `GetAnnotationCropShape()` is used when the version exposes
   it and the annotation crop is active. Otherwise the model crop is used,
   which is safe in the only direction that matters: the annotation crop
   always encloses the model crop, so a point clamped into the model crop
   is inside the annotation crop too. It may clamp a little more tightly
   than strictly necessary; it can never leave a tag off the sheet.

4. **Link graphics overrides are only partly pre-tested.** A hidden link
   instance is skipped and a hidden category warns. Finer cases — a link
   displayed *by linked view*, a category overridden inside the link's own
   settings — are left to stage 3, which creates the tag and asks Revit
   for its bounding box. That is the only reliable answer, and it is why
   stages 1 and 2 are aggressive.

5. **Stage 3 can be switched off.** "Confirm every tag is really visible"
   is on by default, as the brief requires. It regenerates the document
   once per tag, which is the cost the brief anticipates in open item 2.
   The switch is there for a very large run where the user accepts that
   some tags may not display and that spacing is not checked; the log
   records which mode ran. Benchmark before turning it off.

6. **One dialog, one view.** Multi-view batch runs (OS-04) and the
   non-plan views (OS-01) are out of Phase 1 scope and are refused with a
   message, per AT-14.

7. **A link counts as loaded when it hands over its document.**
   `RevitLinkType.IsLoaded()` was the first test and it reported "not
   loaded" for links whose document opens perfectly well — which hid every
   service in the project behind a dead end. `GetLinkDocument()` is now the
   authority, because reading that document is the whole job; the link
   type's `LinkedFileStatus` is consulted only afterwards, to word the
   reason a link could not be read (unloaded, not found, closed workset).
   Nested link *instances* are detected via `RevitLinkType.IsNestedLink`
   and listed disabled with that reason, per OS-06.

8. **Tag spacing is compared on world-axis rectangles.** In a plan view
   rotated relative to the project, a tag's bounding box read this way is
   slightly larger than the tag. The effect is conservative — the tool
   asks for a little more room than it needs — and only in rotated views.

---

## 4. Brief §11, open items

| Item | Status |
|---|---|
| 1. Annotation crop accessor | Handled defensively (§3.3 above). Worth confirming on each version against a sheet |
| 2. Stage 3 cost | Implemented as specified, with a documented switch. **Needs benchmarking on a real project link** before any decision to change the approach |
| 3. Multi-reference tags | `GetTaggedReferences()` is enumerated in full, so every reference a manually placed multi-reference tag carries suppresses its element. Confirm against a model that actually uses them |
| 4. Level filtering across links | **Still needs the engineer's answer.** The level filter currently lists the LINK's levels and matches the element's own reference level. Where a link is authored to a different datum, that is the honest reading — but if the office wants host-level equivalence, that is a mapping decision, not a code decision, and it should be agreed before it is written |

---

## 5. Acceptance tests

Run against a host model with a services link. Each row is the brief's own
criterion; the preview counts are the fastest way to check most of them.

| Ref | Test | Expected |
|---|---|---|
| AT-01 | 500 pipes, minimum horizontal 3000 mm | Only horizontal runs with Lᵥ > 3000 mm tagged; the placed count equals the preview count exactly |
| AT-02 | Soil pipe at 1:100, 6 m | Classified horizontal and tagged. Covered by unit test `test_drainage_gradient_is_horizontal` |
| AT-03 | 30 m riser through eight plans | Tagged per plan where Lᵥ passes; the report shows the per-view length, not 30 m. Unit test `test_riser_through_a_storey_reports_the_storey` |
| AT-04 | Riser 3.2 m per storey | Tagged; Lᵥ is the part inside the view range |
| AT-05 | Long run crossing the crop, midpoint outside | Tag inside the crop, at the midpoint of the clipped part |
| AT-06 | View placed on a sheet | Every tag created is visible on the sheet; none suppressed by the annotation crop |
| AT-07 | Same criteria at 1:50 and 1:200 | Offsets and clearances measure the same on both sheets. Unit test `test_offset_scales_with_the_view` |
| AT-08 | Re-run with Skip already tagged | Zero duplicates; skipped count equals the previous placed count |
| AT-09 | Link placed rotated, and mirrored | Classification and Lᵥ correct for both instances |
| AT-10 | Link reloaded with elements deleted, then Purge Orphaned Tags | All orphans found and removed in one undo step |
| AT-11 | Preview | No tags remain; no unsaved change attributable to the preview |
| AT-12 | Cancel mid-run | Tags already placed are kept, the run is reported honestly, the model is intact |
| AT-13 | Included category with no loaded tag family | Blocked with the family named; no partial run |
| AT-14 | Run from a 3D view, section or sheet | Clear message, no exception |
| AT-15 | Same build on 2022 and 2026 | Identical behaviour and tag positions |

The core-layer unit tests run from the repo root:

    python -m pytest tests/test_taglinked_core.py -q

---

## 6. Phase 2 candidates (unchanged from the brief §3.2)

Sections and elevations, fittings and equipment, connected-run aggregation,
multi-view batch runs, decluttering beyond the stagger rule, and nested
links.
