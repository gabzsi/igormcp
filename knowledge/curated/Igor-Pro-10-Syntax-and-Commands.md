---
type: reference
collection: guides
tags: [igor-pro, syntax, commands, execution, flags, operations]
provenance: "Project-authored technical guide; consult WaveMetrics documentation for authoritative product details."
related:
  - "curated/Igor-Pro-10-Wave-Architecture.md"
  - "curated/Igor-Pro-10-Curve-Fitting.md"
  - "curated/Igor-Pro-10-Automation-and-COM.md"
---

# Igor Pro 10 Syntax and Commands

Igor Pro 10 command syntax governs operation invocation, execution flags, command chaining, and error suppression across interactive and automated workflows.

## Command Line Structure

Igor commands follow the general structure:
```text
Operation[/flags] [parameters]
```
Multiple operations can be chained on a single command line using semicolons (`;`):
```text
Make/O/N=500 w; SetScale/P x 0, 0.01, "s", w; w = sin(2*pi*x); Display w
```

## Universal Command Flags

Standard execution flags modify operation behavior across built-in Igor commands:

| Flag | Purpose | Examples |
|---|---|---|
| `/O` | Overwrite existing object without prompting or error | `Make/O/N=100 wave0`, `Duplicate/O w, w_copy`, `NewDataFolder/O/S root:Data` |
| `/Q` | Quiet mode; suppresses console history output and interactive alerts | `CurveFit/Q`, `WaveStats/Q my_wave`, `LoadWave/Q` |
| `/Z` | Zero/Quiet error mode; suppresses modal error dialogs and returns status | `KillWaves/Z temp_wave`, `CloseProc/Z`, `Execute/Z cmd` |
| `/D` | Display/Destination flag; plots fit curves or specifies destination waves | `CurveFit/Q sin, my_wave /D` |
| `/N` | Size or count parameter; sets point count or dimension allocation | `Make/O/N=1000 my_wave`, `Make/O/N=(100, 100) mat` |
| `/P` | Path or scaling parameter; sets wave X-scaling or folder path | `SetScale/P x 0, 1e-3, "s", w`, `LoadWave/P=myPath` |
| `/S` | Selection or subrange parameter | `NewDataFolder/O/S root:Exp` |

## Naming and Quoting Rules

1. **Standard Names**:
   - Must begin with a letter.
   - May contain alphanumeric characters and underscores (`_`).
   - Case-insensitive (e.g. `MyWave` and `mywave` reference the same object).
   - Maximum length in Igor Pro 10: 255 bytes (legacy Igor 6 limited names to 31 bytes).
2. **Liberal Names**:
   - Objects containing spaces, periods, hyphens, or special characters must be quoted with single quotes (`'`):
     ```text
     Display 'run-01.data', 'run-02.data'
     SetDataFolder root:'Experimental Runs':'Batch-A'
     ```
3. **Literal Strings**:
   - Literal string arguments must be enclosed in double quotes (`"`):
     ```text
     Print "Execution finished"
     SetScale/P x 0, 0.01, "seconds", my_wave
     ```

## Safe Automated Command Conventions

When driving Igor Pro via automation or MCP:
- Always include `/O` on mutating creation commands (`Make/O`, `Duplicate/O`, `NewDataFolder/O`) to prevent modal overwrite prompts.
- Always include `/Q` on analysis operations (`WaveStats/Q`, `CurveFit/Q`) to prevent dialogs and excessive history buffer growth.
- Always include `/Z` on cleanup commands (`KillWaves/Z`, `CloseProc/Z`) so missing objects do not raise fatal errors.
- Never exceed 2,400 UTF-8 bytes per single command execution string.

See also: [Wave Architecture](Igor-Pro-10-Wave-Architecture.md), [Curve Fitting](Igor-Pro-10-Curve-Fitting.md), [Automation and COM](Igor-Pro-10-Automation-and-COM.md), [Data Folders](Igor-Pro-10-Data-Folders.md)
