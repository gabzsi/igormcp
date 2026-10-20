---
type: reference
collection: guides
tags: [igor-pro, python, numpy, scipy, matplotlib, rosetta-stone, syntax, compatibility]
provenance: "Project-authored technical guide; consult WaveMetrics documentation for authoritative product details."
related:
  - "curated/Igor-Pro-10-Syntax-and-Commands.md"
  - "curated/Igor-Pro-10-Wave-Architecture.md"
  - "curated/Igor-Pro-10-Curve-Fitting.md"
---

# Python NumPy to Igor Pro Rosetta Stone

This reference maps common scientific Python idioms (NumPy, SciPy, Matplotlib) directly to native WaveMetrics Igor Pro operations, detailing both modern Igor Pro 10 syntax and Igor Pro 6 backward-compatibility constraints.

## 1. Array and Wave Creation

In Python/NumPy, data arrays are dimensionless buffers that require separate X arrays for coordinates. In Igor Pro, waves intrinsically embed their physical dimension scaling, units, and memory allocation.

| Task | Scientific Python (NumPy) | Igor Pro (Native Command) | Igor 6 Compatibility Notes |
|---|---|---|---|
| **Allocating zeros** | `w = np.zeros(1000)` | `Make/O/N=1000 w` | Standard across all versions (`/O` overwrites). |
| **Allocating double precision** | `w = np.zeros(1000, dtype=np.float64)` | `Make/O/D/N=1000 w` | `/D` flag forces 64-bit double precision. |
| **Allocating text array** | `w = np.empty(100, dtype=object)` | `Make/O/T/N=100 str_wave` | `/T` flag creates text waves. |
| **2D Matrix allocation** | `m = np.zeros((100, 200))` | `Make/O/N=(100, 200) mat` | Igor dimensions are `(rows, columns)`. |
| **Linear spacing (`linspace`)** | `x = np.linspace(0, 10, 500)` | `Make/O/N=500 w; SetScale/I x 0, 10, "s", w` | `/I` sets inclusive range: $x_0 = 0, x_{N-1} = 10$. |
| **Step spacing (`arange`)** | `x = np.arange(0, 10, 0.02)` | `Make/O/N=500 w; SetScale/P x 0, 0.02, "s", w` | `/P` sets start and step: $x_0 = 0, \Delta x = 0.02$. |
| **Temporary / Free waves** | In-memory local variable | `Make/FREE/N=1000 temp_w` | **Igor 7+ only**. In Igor 6 procedures, allocate a prefixed global wave and `KillWaves/Z` on exit. |

## 2. Waveform Math and Indexing

| Task | Scientific Python (NumPy) | Igor Pro (Native Command) | Explanation |
|---|---|---|---|
| **Point-by-point formula** | `y = 2.5 * np.sin(2 * np.pi * 5 * x)` | `y = 2.5 * sin(2 * pi * 5 * x)` | Igor automatically evaluates formulas across all wave points in C++. |
| **Point index evaluation** | `val = y[15]` | `val = y[15]` | `w[p]` indexes by 0-based integer point index $p$. |
| **Coordinate evaluation** | `idx = np.argmin(np.abs(x - 0.05)); val = y[idx]` | `val = y(0.05)` | `w(x)` evaluates wave at continuous coordinate $x$ using intrinsic scaling. |
| **Gaussian noise** | `noise = np.random.normal(0, 0.1, len(y))` | `y += gnoise(0.1)` | `gnoise(sigma)` generates Gaussian noise with standard deviation $\sigma$. |
| **Uniform noise** | `noise = np.random.uniform(-1, 1, len(y))` | `y += enoise(1.0)` | `enoise(mag)` generates uniform noise in $[-mag, +mag]$. |
| **Subrange assignment** | `y[10:50] = 0` | `y[10, 49] = 0` | In Igor, subranges in brackets use inclusive point limits: `[startPoint, endPoint]`. |
| **Coordinate subrange** | `y[(x >= 1.2) & (x <= 3.5)] = 0` | `y(1.2, 3.5) = 0` | Parentheses `(x1, x2)` specify limits by scaled physical coordinate. |

## 3. Statistics and Signal Processing

| Task | Scientific Python (NumPy / SciPy) | Igor Pro (Native Command) | Igor Result Variables |
|---|---|---|---|
| **Descriptive statistics** | `mean = np.mean(y); std = np.std(y)` | `WaveStats/Q y` | `V_avg` (mean), `V_sdev` (std dev), `V_min`, `V_max`, `V_npnts`, `V_sem`. |
| **Fast Fourier Transform** | `fy = np.fft.fft(y)` | `FFT/DEST=y_fft y` | Computes complex FFT with automatic frequency axis scaling. |
| **Inverse FFT** | `y_rec = np.fft.ifft(fy)` | `IFFT/DEST=y_rec y_fft` | Inverse transform restoring time-domain coordinates. |
| **Integration** | `cum = np.cumsum(y) * dx` | `Integrate y /D=y_int` | Cumulative trapezoidal integration preserving dimension scaling. |
| **Differentiation** | `dy = np.gradient(y, dx)` | `Differentiate y /D=y_diff` | Central-difference numerical derivative with respect to scaled X. |
| **Smoothing (Box / Savitzky-Golay)** | `scipy.signal.savgol_filter(y, 11, 2)` | `Smooth/S=2 11, y` | `/S=degree` applies Savitzky-Golay polynomial filtering. |
| **Peak detection** | `scipy.signal.find_peaks(y, height=0.5)` | `FindPeak/Q/M=0.5 y` | `V_PeakLoc` (x-coord), `V_PeakVal` (y-val), `V_LeadingEdgeLoc`. |

## 4. Curve Fitting (Optimization)

In SciPy, curve fitting requires defining lambda functions, packaging initial guess arrays, and inspecting covariance matrices. In Igor Pro, `CurveFit` natively handles Levenberg-Marquardt optimization with built-in models and output waves.

```text
// Python:
// popt, pcov = scipy.optimize.curve_fit(lambda x, y0, A, w, phi: y0 + A*np.sin(w*x + phi), x, y)

// Igor Pro equivalent:
CurveFit/Q/M=0/W=0 sin, yWave /D
```

| Output Variable / Wave | Python Equivalent | Purpose |
|---|---|---|
| **`W_coef`** | `popt` | 1D parameter array: `W_coef[0]=y0`, `[1]=A`, `[2]=omega`, `[3]=phi` |
| **`W_sigma`** | `np.sqrt(np.diag(pcov))` | 1D standard error array for each coefficient |
| **`V_chisq`** | $\chi^2 / \text{dof}$ | Reduced Chi-Square goodness of fit |
| **`fit_yWave`** | `y_fit = model(x_fine, *popt)` | Generated model curve added to active graph by `/D` flag |
| **`V_fitError`** | Fit convergence flag | `0` = converged; non-zero = aborted or singular matrix |

## 5. Visualization (Matplotlib vs Igor Pro)

| Task | Matplotlib (Python) | Igor Pro (Native Command) | Notes |
|---|---|---|---|
| **Create new plot** | `fig, ax = plt.subplots(); ax.plot(x, y)` | `Display y vs x` | If `y` is scaled with `SetScale`, `Display y` is sufficient. |
| **Add trace to graph** | `ax.plot(x, y2)` | `AppendToGraph y2 vs x` | Appends data to topmost graph window. |
| **Style trace color** | `ax.plot(x, y, color='red', lw=2)` | `ModifyGraph rgb(y)=(65535,0,0), lsize(y)=2` | Igor Pro colors use 16-bit RGB `(0 - 65535)`. |
| **Scatter markers** | `ax.scatter(x, y, marker='o')` | `ModifyGraph mode(y)=3, marker(y)=19` | `mode=3` enables markers; `marker=19` is filled circle. |
| **Axis labels & title** | `ax.set_xlabel("Time (s)"); ax.set_ylabel("Voltage (V)")` | `Label bottom "Time (s)"; Label left "Voltage (V)"` | Sets primary coordinate axis titles. |
| **Legend** | `ax.legend()` | `Legend/C/N=text0/J` | Automatically populates legend from graph trace names. |

## 6. Backward Compatibility Strategy: Igor 10 vs Igor 6

When writing procedures intended to run on both modern systems (Igor Pro 10) and legacy deployments (Igor Pro 6):

1. **Object Name Length**:
   - **Modern Igor 10**: Names support up to 255 bytes.
   - **Igor Pro 6**: Names are strictly limited to **31 characters**. For dual-target procedures, keep all wave, variable, data folder, and function names $\le 31$ characters.
2. **Local / Free Waves**:
   - Do NOT use `Make/FREE` in shared `.ipf` files if Igor 6 support is needed (compiler error in Igor 6).
   - Use uniquely prefixed global waves in a dedicated data folder (e.g. `root:Packages:MyTool:temp_w`) and clean up with `KillWaves/Z`.
3. **Conditional Compilation**:
   Use Igor's preprocessor to branch on feature availability:
   ```text
   #if IgorVersion() >= 7.00
       Make/FREE/N=500 tempWave
   #else
       Make/O/N=500 root:Packages:tempWave
       Wave tempWave = root:Packages:tempWave
   #endif
   ```
4. **WaveType Inspection**:
   - In Igor 7+, `WaveType(w, 1)` cleanly returns type codes (numeric, text, reference).
   - In Igor 6, `WaveType(w)` returns 0 for text waves and non-zero for numeric precision types.

See also: [Syntax and Commands](Igor-Pro-10-Syntax-and-Commands.md), [Wave Architecture](Igor-Pro-10-Wave-Architecture.md), [Curve Fitting](Igor-Pro-10-Curve-Fitting.md)
