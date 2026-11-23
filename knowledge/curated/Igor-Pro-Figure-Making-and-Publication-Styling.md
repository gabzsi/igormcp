---
type: reference
collection: guides
tags: [igor-pro, figures, publication, plotting, styling, colors, export, graphics]
provenance: "Project-authored technical guide; consult WaveMetrics documentation for authoritative product details."
related:
  - "curated/Igor-Pro-10-Syntax-and-Commands.md"
  - "curated/Python-NumPy-to-Igor-Pro-Rosetta-Stone.md"
  - "curated/Igor-Pro-10-Wave-Architecture.md"
---

# Igor Pro Figure Making and Publication Styling

This reference provides a definitive cookbook for generating publication-quality, journal-compliant figures in WaveMetrics Igor Pro, including dimension standards, axis geometry, 16-bit color palettes, error bars, and high-DPI raster and vector export.

## 1. Standard Publication Dimensions

Major scientific publishers (Nature Portfolio, ACS, APS, Elsevier, Wiley) require figures dimensioned in standard column widths. Igor Pro graph sizing operates in typographic points (1 inch = 72 points).

| Layout Target | Width (Inches) | Width (mm) | Igor Width (`pt`) | Default Aspect | Igor Height (`pt`) |
|---|---|---|---|---|---|
| **ACS Single Column (1 col)** | 3.25 in | 82.5 mm | `width=234` | 4:3 or Square | `height=175` to `234` |
| **Single Column (Nature / APS / 85 mm)** | 3.33 in | 85 mm | `width=240` | 4:3 or Square | `height=180` to `240` |
| **Intermediate (1.5 col)**| 5.00 in | 127 mm| `width=360` | 16:10 | `height=225` |
| **Double Column (2 col)** | 7.00 in | 178 mm| `width=504` | 16:9 or 2:1 | `height=280` to `350` |

### Setting Graph Dimensions
```text
// Exact printable plot area in points (72 pt/in)
ModifyGraph width=240, height=180

// Or make square plot area
ModifyGraph width=220, height=220
```

## 2. Core Publication Axis Styling

Raw Igor graphs lack mirrored axes and have tight point clipping. A publication-grade graph requires inward ticks, mirrored frames, offset data margins, and clean sans-serif typography.

```text
// Universal journal styling chain:
ModifyGraph mirror=1, standoff=1, tick=2, axThick=1.0; ModifyGraph font="Arial", fSize=10; ModifyGraph lblMargin(left)=5, lblMargin(bottom)=5
```

| Flag | Recommended Setting | Purpose |
|---|---|---|
| **`mirror=1`** | `mirror=1` | Mirrors axes and ticks on top and right borders (box frame). |
| **`standoff=1`** | `standoff=1` | Prevents edge data points from touching axis frame borders. |
| **`tick=2`** | `tick=2` (inside) | Inward-facing tick marks (standard for physical sciences). `tick=0` is outside, `tick=1` is crossing, `tick=3` is none. |
| **`axThick=1.0`** | `1.0` to `1.2` pt | Line width of axis lines and tick marks. |
| **`font="Arial"`** | `"Arial"` / `"Helvetica"` | Standard clean sans-serif font accepted by all journals. |
| **`fSize=10`** | `9` to `11` pt | Legible tick and axis label text size when scaled in print. |
| **`lblMargin=5`** | `5` to `8` pt | Proper breathing room between tick labels and axis title. |

## 3. Curated 16-bit Color Palettes

Igor Pro specifies RGB color values on a **16-bit integer scale (0 to 65,535)** rather than the standard 8-bit (0 to 255) scale. To convert standard 8-bit hex/RGB:
$$\text{Igor RGB} = \text{RGB}_{8\text{-bit}} \times 257$$

### A. Okabe-Ito (Colorblind-Safe Standard)
Universally legible across all forms of color-vision deficiency.

| Color Name | Hex Code | Igor Pro 16-bit RGB `(r, g, b)` | ModifyGraph Snippet |
|---|---|---|---|
| **Black** | `#000000` | `(0, 0, 0)` | `ModifyGraph rgb(w)=(0, 0, 0)` |
| **Orange** | `#E69F00` | `(58982, 36700, 0)` | `ModifyGraph rgb(w)=(58982, 36700, 0)` |
| **Sky Blue** | `#56B4E9` | `(22016, 47104, 60160)` | `ModifyGraph rgb(w)=(22016, 47104, 60160)` |
| **Bluish Green** | `#009E73` | `(0, 40448, 30720)` | `ModifyGraph rgb(w)=(0, 40448, 30720)` |
| **Blue** | `#0072B2` | `(0, 28672, 53248)` | `ModifyGraph rgb(w)=(0, 28672, 53248)` |
| **Vermillion** | `#D55E00` | `(54528, 19712, 0)` | `ModifyGraph rgb(w)=(54528, 19712, 0)` |
| **Reddish Purple** | `#CC79A7` | `(52428, 31232, 43008)` | `ModifyGraph rgb(w)=(52428, 31232, 43008)` |

### B. Nature Editorial / Crimson & Navy
High-impact, subdued academic palette.

| Role | Color Name | Igor Pro 16-bit RGB `(r, g, b)` |
|---|---|---|
| **Primary Data** | Navy Blue | `(12800, 28672, 53248)` |
| **Fit / Highlight** | Crimson Red | `(58112, 9728, 9728)` |
| **Secondary Trace** | Forest Green | `(0, 38400, 20480)` |
| **Reference / Tail** | Amber Yellow | `(62464, 41216, 5120)` |

## 4. Markers, Error Bars, and Line Styling

```text
// Style trace as open markers with fit line:
ModifyGraph mode(rawData)=3, marker(rawData)=19, msize(rawData)=2.5
ModifyGraph mode(fitData)=0, lsize(fitData)=1.5, lstyle(fitData)=0

// Add symmetric error bars from standard error wave (/L=line thickness, /T=cap thickness)
ErrorBars/L=1.0/T=1.0 rawData Y, wave=(sigmaWave, sigmaWave)
```

- **Modes (`mode(w)=...`)**:
  - `0`: Continuous line.
  - `3`: Markers only (scatter).
  - `4`: Line and markers.
- **Markers (`marker(w)=...`)**:
  - `19`: Filled circle.
  - `16`: Filled square.
  - `8`: Open circle.
  - `5`: Open square (`0` = plus `+`, `1` = cross `×`).

## 5. Axis Labels, Math Typography, and Legends

Igor Pro uses backslash escape codes for formatting axis labels and text boxes:
- Superscript: `\Stext\M`
- Subscript: `\Btext\M`
- Greek letters: native UTF-8 characters or Unicode escape sequences `\u03BC` ($\mu$), `\u03C4` ($\tau$), `\u03BB` ($\lambda$) (note: `\Znn` sets font size in points, not Greek letters)

```text
// Chemical and physical units formatting:
Label bottom "Time (\\F'Symbol'm\\F'Arial's)"   // Microseconds: µs
Label left "Absorbance (OD \\F'Symbol'\\44\\F'Arial'10\\S-3\\M)" // OD x 10^-3
TextBox/C/N=panelLabel/F=0/A=LT/X=4/Y=4 "\\Z12\\f01(a)" // Panel tag (a) bold
```

## 6. Publication Export Commands

Export figures directly without GUI dialogs using `SavePICT`:

```text
// 1. High-Resolution 300 DPI PNG (for manuscripts & slides)
SavePICT/E=-5/RES=300 as "C:\\Users\\...\\Figure1.png"

// 2. Ultra-High Resolution 600 DPI TIFF (for final print submission)
SavePICT/E=-7/RES=600 as "C:\\Users\\...\\Figure1.tif"

// 3. Vector PDF (resolution-independent; native text & paths preserved)
SavePICT/E=-8 as "C:\\Users\\...\\Figure1.pdf"
```

| Flag | Description |
|---|---|
| `/E=-5` | PNG format (lossless compression). |
| `/E=-7` | TIFF format (lossless print master). |
| `/E=-8` | Vector PDF format (`/E=-2` is Windows Enhanced Metafile EMF). |
| `/RES=dpi` | Exact raster output resolution in DPI (e.g. `/RES=300` or `/RES=600`; `/B=dpi` only accepts `n*72` for `n=1..8`). |

## 7. Reusable Procedure Module

For automated one-line styling and palette application in any experiment, load [`procedures/FigureStyles.ipf`](procedures/FigureStyles.ipf):

```text
#include "FigureStyles"

// One-line styling commands:
Pub_StyleGraph("", fontName="Arial", fontSize=10, traceWidth=1.5)
Pub_SetGraphSize("", 3.33, 2.50)
Pub_ApplyPalette("", "OkabeIto")
Pub_ExportFigure("", "C:\\path\\Figure1.png", dpi=300)
```

See also: [Python NumPy to Igor Pro Rosetta Stone](Python-NumPy-to-Igor-Pro-Rosetta-Stone.md), [Syntax and Commands](Igor-Pro-10-Syntax-and-Commands.md), [Wave Architecture](Igor-Pro-10-Wave-Architecture.md)
