---
type: reference
collection: guides
tags: [igor-pro, curve-fitting, regression, models, optimization]
provenance: "Project-authored technical guide; consult WaveMetrics documentation for authoritative product details."
related:
  - "curated/Igor-Pro-10-Wave-Architecture.md"
  - "curated/Igor-Pro-10-Syntax-and-Commands.md"
---

# Igor Pro 10 Curve Fitting

Curve fitting in Igor Pro uses the Levenberg-Marquardt non-linear least-squares algorithm to fit waveform data to analytical mathematical models or user-defined functions.

## Built-in Fit Types

| Function Name | Model Equation | Fitted Coefficients (`W_coef`) |
|---|---|---|
| `line` | $y = k_0 + k_1 x$ | `W_coef[0]=y0`, `[1]=slope` |
| `poly n` | $y = \sum_{i=0}^n k_i x^i$ | `W_coef[0...n]` |
| `sin` | $y = y_0 + A \sin(\omega x + \phi)$ | `W_coef[0]=y0`, `[1]=A`, `[2]=omega` (rad/s), `[3]=phi` |
| `gauss` | $y = y_0 + A \exp(-((x - x_0) / \text{width})^2)$ | `W_coef[0]=y0`, `[1]=A`, `[2]=x0`, `[3]=width` |
| `lor` | $y = y_0 + A / ((x - x_0)^2 + B)$ | `W_coef[0]=y0`, `[1]=A`, `[2]=x0`, `[3]=B` |
| `exp` | $y = y_0 + A \exp(-\text{invTau} \cdot x)$ | `W_coef[0]=y0`, `[1]=A`, `[2]=invTau` |
| `dblexp` | $y = y_0 + A_1 e^{-\text{invTau}_1 x} + A_2 e^{-\text{invTau}_2 x}$ | `W_coef[0]=y0`, `[1]=A1`, `[2]=invTau1`, `[3]=A2`, `[4]=invTau2` |

## Standard Command Invocation

```text
// Fit single wave using intrinsic X scaling
CurveFit/Q/M=0/W=0 sin, yWave /D

// Fit explicit XY data
CurveFit/Q/M=0/W=0 gauss, yWave /X=xWave /D

// Fit a restricted X subrange
CurveFit/Q sin, yWave(0.002, 0.045) /D
```

### Key Execution Flags
- `/Q`: Quiet mode; suppresses interactive fitting alerts and console printing.
- `/D`: Destination / Display; automatically generates a high-resolution fitted wave named `fit_<yWave>` and adds it to the active graph.
- `/X=xWave`: Specifies independent variable wave for non-uniformly spaced data.
- `/W=weightWave`: Specifies error weights ($1/\sigma$ or $1/\sigma^2$).
- `/M=0`: Standard non-matrix weighting mode.
- `/M=2`: Matrix weighting mode; calculates and outputs the full parameter covariance matrix `M_Covar`.
- `/H="<holdString>"`: Bitmask string indicating held parameters (`"1"` to hold, `"0"` to optimize, e.g. `/H="00010"`).
- `/I=1`: Interprets `/W` weights as inverse standard deviation ($1/\sigma$).

## Output Variables and Waves

A successful `CurveFit` or `FuncFit` operation creates or updates several objects in the active data folder:

1. **`W_coef` (1D Wave)**: Contains optimized parameter values in standard order.
2. **`W_sigma` (1D Wave)**: Contains estimated standard errors for each coefficient.
3. **`M_Covar` (2D Wave)**: Full parameter covariance matrix (only generated when `/M=2` is specified). Inside compiled user functions, must be declared via `Wave M_Covar = M_Covar`.
4. **`V_chisq` (Scalar Variable)**: Reduced Chi-Square ($\chi^2$) goodness of fit.
5. **`V_npnts` (Scalar Variable)**: Number of data points included in the fit.
6. **`V_fitError` (Scalar Variable)**: Error code (`0` = normal convergence; non-zero = singular matrix, max iterations exceeded, or math error).
7. **`V_fitOptions` (Scalar Variable)**: Bitmask of options used during fitting.

## User-Defined Fit Functions

Custom mathematical models are implemented using thread-safe Igor functions:
```text
Function MyModel(w, x) : FitFunc
    Wave w          // Parameter coefficients
    Variable x      // Independent coordinate
    return w[0] + w[1]*x + w[2]*x^2
End

// Fitting to custom function:
// FuncFit/Q/M=2/H=holdStr MyModel, coefWave, yWave /X=xWave /W=weightWave /I=1 /C=constraints
```

> **Syntax Note on Flag Ordering**: Flags `/C=constraints` and `/I=1` must be placed as trailing options *after* the Y wave argument. Placing `/C=` before the function name triggers an "expected fitting function" syntax error.

See also: [Syntax and Commands](Igor-Pro-10-Syntax-and-Commands.md), [Wave Architecture](Igor-Pro-10-Wave-Architecture.md), [Automation and COM](Igor-Pro-10-Automation-and-COM.md), [Gotchas and Pitfalls](Igor-Pro-Gotchas-and-Pitfalls.md)
