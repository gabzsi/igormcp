---
type: reference
collection: guides
tags: [igor-pro, gotchas, pitfalls, bugs, troubleshooting, compatibility, conventions]
provenance: "Project-authored technical guide; consult WaveMetrics documentation for authoritative product details."
related:
  - "curated/Igor-Pro-10-Syntax-and-Commands.md"
  - "curated/Igor-Pro-10-Wave-Architecture.md"
  - "curated/Python-NumPy-to-Igor-Pro-Rosetta-Stone.md"
---

# Igor Pro Gotchas and Pitfalls

This reference catalogues subtle operational quirks, silent failure modes, and architectural pitfalls in WaveMetrics Igor Pro (with special emphasis on Igor Pro 10 and backward compatibility with Igor Pro 6).

## 1. Sub-Point Coordinate Truncation (`pnt2x`)

- **The Pitfall**: In Igor Pro, `pnt2x(w, p)` converts a point index $p$ to an x-coordinate. However, **`pnt2x()` silently truncates its point argument to an integer point index**.
- **Impact**: If a sub-point peak fitting algorithm (such as a 3-point parabolic vertex refinement $\Delta p = -\frac{y[p+1] - y[p-1]}{2(y[p+1] - 2y[p] + y[p-1])}$) calculates a fractional point $p_{\text{peak}} = p_0 + \Delta p$, calling `pnt2x(w, p0 + delta)` will truncate the fractional $\Delta p$ to zero. The parabolic refinement is silently discarded, reducing the alignment or measurement to coarse integer-point steps (e.g. 50 ps instead of sub-picosecond accuracy).
- **The Solution**: Always compute continuous coordinates directly from the wave's dimension scaling properties:
  ```text
  Variable subPointX = DimOffset(w, 0) + fractionalPoint * DimDelta(w, 0)
  ```

## 2. Unequal Wave Length Clamping in Vector Arithmetic

- **The Pitfall**: In NumPy, operations on arrays of differing sizes raise a `ValueError: operands could not be broadcast together`. In Igor Pro wave assignment (`wA = wB + wC`), if `wC` has fewer points than `wA` and `wB`, Igor **clamps `wC` to its last point value** for all remaining points rather than populating them with `NaN`.
- **Impact**: In automated trace averaging or subtraction, an unexpected short acquisition or truncated shot will contribute a flat DC baseline offset to the tail of the calculated average wave without throwing any warning or error.
- **The Solution**:
  - Always validate `numpnts(w1) == numpnts(w2)` before assignment.
  - Or use coordinate-based assignment with explicit limits:
    ```text
    wSum = wA(x) + (x <= rightx(wB) ? wB(x) : NaN)
    ```

## 3. Point Indexing `[p]` vs. Coordinate Evaluation `(x)`

- **The Pitfall**: Square brackets `w[p]` reference 0-based integer point index $p$. Parentheses `w(x)` evaluate the wave at physical continuous coordinate $x$ using its intrinsic `SetScale` calibration (interpolating linearly if $x$ falls between sample points).
- **Impact**: If a kinetic trace has time scaling from $-10\text{ ns}$ to $+40\text{ ns}$, `w[0]` evaluates the initial point at $-10\text{ ns}$. However, `w(0)` evaluates the wave at $t = 0.0\text{ ns}$, which corresponds to point index $p = 10\text{ ns} / \Delta t$! Mixing up `[]` and `()` in analysis loops leads to massive time-shift errors.
- **Rules**:
  - Use `w[p]` when iterating through memory buffers by point count.
  - Use `w(x)` when aligning physical peaks, setting fit limits, or evaluating calibrated time/wavelength.

## 4. Chained Semicolon Commands and Inline Comments (`//`)

- **The Pitfall**: Multiple Igor statements can be chained with semicolons on a single command line (`cmd1; cmd2; cmd3`). However, if an inline comment `//` is included in any segment, **Igor treats everything after `//` up to the physical newline as a comment**, silently discarding all subsequent semicolon-delimited commands!
- **Impact**: Commands sent over automation or batch scripts after a `//` are completely skipped without syntax errors.
- **The Solution**: Never use `//` inside semicolon-chained command lines. Either strip comments entirely or split into separate execution statements.

## 5. Data Folder Colon Rules and Current Folder Scoping

- **The Pitfall**:
  - `root:Data:waveA`: Absolute path starting from root.
  - `:Data:waveA`: Relative path looking for subfolder `Data` inside the *current* data folder.
  - `::waveA`: Relative path moving up one folder level.
  - `Data:waveA`: **Ambiguous/Invalid** in many contexts if not explicitly relative (`:`) or absolute (`root:`).
- **Impact**: Functions that execute commands without explicitly restoring the original data folder (`SetDataFolder savedDF`) pollute the user's active folder or create waves in unexpected locations.
- **The Solution**: Always preserve and restore data folder state:
  ```text
  String savedDF = GetDataFolder(1) // 1 = returns full absolute path
  SetDataFolder root:MySubFolder
  // ... work ...
  SetDataFolder $savedDF
  ```

## 6. History Carriage Returns and Pilcrow (`¶`) Control Characters

- **The Pitfall**: Igor Pro's internal command history buffer historically uses Carriage Return (`\r`, ASCII 13) rather than Line Feed (`\n`, ASCII 10) as statement terminators, and uses the pilcrow character (`¶`, ASCII 182 / Unicode `\u00B6`) for multiline returns in certain string operations.
- **Impact**: Unsanitized output read back over COM or written to external tools can contain invisible `\r` characters that overwrite console lines or corrupt string comparisons.
- **The Solution**: Normalize all strings returned from Igor by replacing `\r\n` and `\r` with `\n` and stripping non-printable control characters.

## 7. Igor Pro 6 Backward Compatibility Traps

When authoring `.ipf` procedures that must compile on both Igor Pro 10 and legacy Igor Pro 6:

1. **The 31-Character Object Name Limit**:
   - Igor 10 allows object names up to 255 bytes.
   - Igor 6 will fail compilation or truncate any wave, variable, function, or data folder name exceeding **31 characters**.
2. **Free Waves (`/FREE`)**:
   - `Make/FREE` and `Duplicate/FREE` were introduced in Igor 7. In Igor 6, `/FREE` throws an unrecognized flag error.
   - For portable procedures, use a designated global scratch subfolder (`root:Packages:MyTool:Scratch:`) and call `KillWaves/Z` on exit.
3. **`WaveType` Output Differences**:
   - In Igor 7+, `WaveType(w, 1)` cleanly returns wave classification (1 = numeric, 2 = text, etc.).
   - In Igor 6, `WaveType(w)` returns 0 for text waves and a bitmask for numeric waves.
4. **Structure and Wave References**:
   - In Igor 6, dynamic wave referencing requires `Wave w = $wavePathStr` rather than direct object pointer assignment. Always check `WaveExists(w)` before dereferencing.

## 8. `FuncFit` Flag Order Sensitivity

- **The Pitfall**: In Igor Pro, `FuncFit` supports leading flags (like `/Q`, `/M=2`, `/H="hold"`) and trailing options (like `/X=xWave`, `/W=weightWave`, `/I=1`, `/C=constraints`). Placing trailing options like `/C=` or `/I=` in the leading flag position before the fit function name results in a compilation failure with the cryptic error: `"expected fitting function"`.
- **Impact**: Multi-flag fitting scripts fail to execute or compile even though all flags are syntactically valid in isolation.
- **The Solution**: Always structure `FuncFit` commands with function name, coefficient wave, and dependent variable wave immediately following the leading flags, followed by trailing flag options:
  ```text
  FuncFit/Q/M=2/H=holdStr MyFitFunc, coefWave, yWave /X=xWave /W=wWave /I=1 /C=constraints
  ```

## 9. Covariance Matrix Generation Requires `/M=2` and Explicit Scoping

- **The Pitfall**: By default or with `/M=0`, Igor's curve fitting does not produce the full parameter covariance matrix `M_Covar`; it only creates `W_sigma`. Specifying `/M=2` generates `M_Covar` in the current data folder. However, inside a user-defined compiled function or macro, referencing `M_Covar` without an explicit `Wave M_Covar = M_Covar` declaration produces a runtime or compilation error.
- **Impact**: Post-fit parameter correlation matrices or bootstrap confidence intervals cannot be evaluated automatically.
- **The Solution**: Always invoke fitting with `/M=2` and declare `Wave M_Covar = M_Covar` and `Wave W_sigma = W_sigma` inside the calling procedure.

## 10. Pre-Pulse Baseline Window Encroachment & Tail Leverage

- **The Pitfall**: In transient spectroscopy with pulsed excitation, an instrument response function (IRF) with Gaussian width $\sigma$ and time-zero $t_0$ causes transient signals to begin rising at $t_0 - 3\sigma$. If the pre-pulse baseline region is chosen too close to zero (e.g. up to $-0.05\ \mu\text{s}$ when $t_0 - 3\sigma = -0.28\ \mu\text{s}$), the linear baseline fit will include a portion of the rising edge.
- **Impact**: Extrapolating a linear baseline fitted on a short interval ($0.2\ \mu\text{s}$) over a long kinetic trace ($80\ \mu\text{s}$) involves an extreme leverage factor ($\sim 400$). A minute slope error caused by pulse encroachment or noise is amplified into a massive artificial offset at long times ($\approx 1.0$ OD), completely corrupting slow decay fits.
- **The Solution**: Strictly terminate baseline fitting windows before $t_0 - 3\sigma$, and apply regularization limits to linear subtraction slopes to prevent runaway noise amplification across extended time windows.

## 11. Wave Reference Declaration Order (IGOR ERROR 330)

- **The Pitfall**: In Igor Pro user functions, local wave declarations like `Wave w = root:foo` or `Wave w = $strPath` are evaluated at runtime when the statement executes. If `root:foo` does not exist in memory at that exact moment, Igor immediately halts execution with:
  ```text
  IGOR ERROR 330: Attempt to operate on a null (missing) wave
  ```
- **Impact**: Functions that create data structures or folders (`InitData()`, `Make/O root:foo`) must execute *before* any local wave variable declarations that bind to those waves. Placing wave declarations at the top of a function before the creation routine executes causes reproducible Error 330 failures. Similarly, export or diagnostic functions that bind to optional waves (such as bootstrap results) will crash if the optional step was skipped.
- **The Solution**:
  - Always call wave creation or initialization routines before binding local `Wave` variables.
  - In conditional or export functions, always check existence first:
    ```text
    if (!WaveExists(root:'Fit Results':boot_params))
        return 0
    endif
    Wave bp = root:'Fit Results':boot_params
    ```

## 12. Font-Escape Syntax in Graph Labels (`\F'Symbol'`) vs. Native UTF-8

- **The Pitfall**: Legacy Igor Pro procedures frequently used font-switching escape codes like `\F'Symbol'm\Fs` to print Greek letters like $\mu\text{s}$. However, in Igor Pro, `\Fs` is an **invalid escape code** (returning to the default font requires `\F0` or `\M`). When Igor encounters `\Fs`, the font switch never closes, leaving all subsequent characters rendered in Symbol font. For example, `s` is rendered as Greek $\sigma$ and `)` remains in Symbol font, corrupting axis labels into unreadable glyphs.
- **Impact**: Microsecond axes (`µs`) and standard deviation labels (`σ`) appear severely mangled in GUI graphs and publication exports.
- **The Solution**: In modern Igor Pro (Igor 7 through 10), native UTF-8 encoding is fully supported. Avoid escape-code font switching entirely and pass standard UTF-8 string literals directly:
  ```text
  Label bottom "Time (µs)"
  Label bottom "Normalized Residual (σ)"
  ```
  Additionally, always use `ModifyGraph standoff=1, fSize=11` to prevent label/tick collisions, and note that physical quantities like absorbance ($A = -\log_{10}(I/I_0)$) are strictly dimensionless and should not be labeled with arbitrary unit tags like `(AU)`.

See also: [Syntax and Commands](Igor-Pro-10-Syntax-and-Commands.md), [Wave Architecture](Igor-Pro-10-Wave-Architecture.md), [Curve Fitting](Igor-Pro-10-Curve-Fitting.md), [Python NumPy to Igor Pro Rosetta Stone](Python-NumPy-to-Igor-Pro-Rosetta-Stone.md), [Figure Making and Publication Styling](Igor-Pro-Figure-Making-and-Publication-Styling.md)
