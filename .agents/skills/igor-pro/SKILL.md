---
name: igor-pro
description: >-
  Use the connected igor-pro MCP server for Igor Pro startup, data inspection,
  calculations, plotting, analysis, knowledge lookup, and prior-session recall.
---

# Igor Pro through MCP

This optional skill adds agent guidance for the `igor-pro` MCP server. It is not part of the server installation or a substitute for configuring an MCP host. Route live Igor operations through the connected server; never launch Igor from a shell.

## Choose the right operation

- Use `ensure_igor` to attach to Igor or start it when needed. `igor_status` is diagnostic only and does not start Igor.
- Use `execute_igor` for simple calculations, plots, fitting, inspection, and short command sequences. Create an IPF procedure for ODE simulations, instrument control, substantial batch/loop work, reusable routines, or when requested.
- Use `open_igor_experiment` only for an existing `.pxp` or `.pxt` when Igor is already running. Normal Igor prompts remain under Igor's control; do not save, discard, or dismiss unsaved-work prompts automatically.
- Inspect with `list_waves`, `get_wave_data`, `read_variable`, and `list_procedures`. Use `import_delimited_text` for supported numeric CSV, TSV, or TXT input.
- Use the initialize guidance for elementary commands; do not enumerate or pre-read basic-reference pages as a task preflight. Query a specific manual page only when syntax is uncertain, an operation is unfamiliar, or diagnostics leave an unresolved question. Search curated guides when a project-specific workflow is relevant, then read the useful match with `read_igor_document`. Public installs may not have official manuals locally; link to WaveMetrics' [online Igor documentation](https://docs.wavemetrics.com/igorpro/) when the local manual bundle is unavailable.
- Search prior session memory only when repeating a nontrivial workflow or looking for a relevant workaround; skip elementary one-offs. Read useful matches with `read_session_memory`. Use `capture_session` to save a concise outcome or workaround when it is likely to help again.

## Everyday patterns and data safety

Waves contain point data plus scaling and units. In pointwise assignments, `p` is the point index and `x` is the scaled X coordinate. For uniform sampling, create a fresh run folder, assign the X scale, then calculate and plot. Replace the example names with fresh unused identifiers before running; omit `/O` so collisions fail instead of overwriting existing data.

```igorpro
NewDataFolder/S root:Run_Example_01
Make/N=1000 signal
SetScale/P x, 0, 0.1, "ms", signal
signal = exp(-x/2)
Display/N=RunPlot_Example_01 signal
Label/W=RunPlot_Example_01 left, "Signal"
Label/W=RunPlot_Example_01 bottom, "Time (ms)"
ModifyGraph/W=RunPlot_Example_01 rgb(signal)=(0,0,65535)
```

`SetScale/P` sets the first X value, step, and units. `Display` uses wave scaling; use `Display yWave vs xWave` for irregular X values. Append a trace with `AppendToGraph/W=graphName waveName`, label axes with `Label/W=graphName left` or `bottom`, and use `ModifyGraph/W=graphName rgb(traceName)=(r,g,b)` for trace color (RGB channels range from 0 to 65535). Preserve originals by deriving results in a new folder.

## Inspect results and recover carefully

Review the result and diagnostics after each mutation. A timed-out command may still be running or may have completed; inspect Igor state before retrying. If dialogs remain, inspect them with `get_igor_dialogs` or `get_igor_compilation_errors` before using an explicit dismissal tool. The server does not intentionally foreground an existing Igor window; Windows or Igor may still affect focus independently.

Tool calls are recorded as bounded local JSONL observations under `IGOR_SESSION_DIR`; they omit bulk wave arrays, but command text and results may still contain sensitive material. Keep session files private. Summaries are caller-written; logs do not create a wiki or automatically preserve an analysis conclusion.
