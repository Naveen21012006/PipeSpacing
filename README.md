# Pipe Spacing — pyRevit Extension

Adjusts the clear spacing between parallel, non-sloped pipe runs around a
chosen reference pipe. Connected segments (pipes + fittings + accessories)
move as one run, and crossing connector pipes are reshaped to follow.

## Requirements

- **Autodesk Revit** (built/tested for Revit 2024).
- **pyRevit** installed — https://github.com/pyrevitlabs/pyRevit/releases

No other downloads are needed; the tool uses only the Revit API and pyRevit.

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

5. A new **PipeSpacing** tab appears with a **Tools** panel and the
   **Pipe Spacing** button.

> CLI alternative (instead of steps 2–4):
> `pyrevit extensions paths add "C:\PyRevitExtensions"` then `pyrevit reload`

## Use

1. Select the pipes to space (one or more parallel runs; include any
   crossing connectors you want carried along).
2. Click **Pipe Spacing**, then graphically pick the **reference** pipe
   (its run stays fixed).
3. Enter the required **clear distance** between adjacent pipe surfaces (mm).

All selected pipes must be straight, non-sloped, and at the same elevation.

Author: Naveen
