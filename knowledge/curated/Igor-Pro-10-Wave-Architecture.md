---
type: concept
collection: guides
tags: [igor-pro, waves, waveforms, data-types, scaling, arrays]
provenance: "Project-authored technical guide; consult WaveMetrics documentation for authoritative product details."
related:
  - "curated/Igor-Pro-10-Syntax-and-Commands.md"
  - "curated/Igor-Pro-10-Curve-Fitting.md"
  - "curated/Igor-Pro-10-Data-Folders.md"
---

# Igor Pro 10 Wave Architecture

Waves are the fundamental data structures in WaveMetrics Igor Pro, representing dimensioned, typed numerical or textual arrays with intrinsic coordinate scaling.

## Wave Properties and Types

In Igor Pro 10, a wave consists of:
- **Data payload**: An array of 1D, 2D, 3D, or 4D points.
- **Data type**:
  - Double-precision floating point (default with `/D` flag, standard in modern Igor).
  - Single-precision floating point (`/S`).
  - Signed or unsigned integer (8, 16, 32, or 64 bits: `/B`, `/W`, `/I`, `/L`).
  - Text wave (`/T` flag).
  - Complex numeric wave (`/C` flag).
- **Dimension scaling**: Intrinsic mapping from point indices ($p, q, r, s$) to physical coordinates ($x, y, z, t$).
- **Data Units**: String labels associated with data values and dimension coordinates (e.g. `"V"`, `"s"`, `"nm"`).

## Wave Creation and Allocation

```text
Make/O/N=1000 my_wave                    // 1D wave of 1000 double points
Make/O/D/N=500 precision_wave            // Explicit double precision
Make/O/T/N=50 labels                     // 1D text wave with 50 elements
Make/O/N=(100, 200) image_matrix         // 2D wave: 100 rows by 200 columns
```

## Intrinsic X-Scaling

Unlike generic numerical arrays in Python or C, Igor waves embed continuous coordinate information. There is often no need to store a separate X wave when points are uniformly sampled:

```text
// SetScale/P <dim> <startValue>, <stepValue>, "<units>", <waveName>
SetScale/P x 0, 0.001, "s", my_wave      // 1 ms per point starting at t=0
SetScale/P x -10, 0.05, "V", volt_wave   // 50 mV per point starting at -10 V
```

## Waveform Arithmetic and Indexing

Igor provides high-performance vector operations evaluated natively in C++:
```text
// Point-by-point formula assignment
my_wave = sin(2 * pi * 50 * x) + gnoise(0.05)

// Wave indexing:
// w[p] accesses point by integer index (0-based)
// w(x) accesses point by scaled X coordinate (interpolated or closest)
Print my_wave[0]                         // First point
Print my_wave(0.05)                      // Value at x = 0.05 seconds
```

## Statistical Analysis and Inspection

To compute descriptive statistics without copying wave buffers into memory:
```text
WaveStats/Q my_wave
```
Sets global scalar variables in the active data folder:
- `V_avg`: Arithmetic mean
- `V_sdev`: Standard deviation
- `V_min`, `V_max`: Minimum and maximum values
- `V_minRowLoc`, `V_maxRowLoc`: Point indices of min and max
- `V_npnts`: Total valid (non-NaN) points count
- `V_sem`: Standard error of the mean

See also: [Syntax and Commands](Igor-Pro-10-Syntax-and-Commands.md), [Curve Fitting](Igor-Pro-10-Curve-Fitting.md), [Data Folders](Igor-Pro-10-Data-Folders.md)
