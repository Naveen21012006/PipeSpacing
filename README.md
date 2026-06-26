# CKR Tools — pyRevit Extension

A small pyRevit extension of MEP piping tools. It adds a **CKR Tools** ribbon
tab with a **Piping** panel containing:

- **Pipe Spacing** — re-space parallel pipe runs around a fixed reference.
- **Pipe Insulation** — auto-apply / update pipe insulation to company standards.

## Requirements

- **Autodesk Revit** (works with 2022, 2023, 2024 and 2025; the Insulation tool
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

Author: Naveen
