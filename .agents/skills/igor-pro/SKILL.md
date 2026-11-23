---
name: igor-pro
description: >-
  Automate, inspect, query, and execute commands in WaveMetrics Igor Pro through
  the igor-pro MCP server. Use for Igor startup, data inspection, analysis,
  knowledge search, and reuse of prior Igor session experience.
---

# Igor Pro MCP

Use this skill whenever a task involves Igor Pro. Route all Igor operations through the connected `igor-pro` MCP server. Never launch Igor from PowerShell, CMD, or another shell.

## Task routing

- Search prior session memory only when repeating a nontrivial workflow or looking for a relevant workaround; skip elementary one-offs. Read useful matches with `read_session_memory`.
- Prefer direct `execute_igor` commands for simple calculations, plots, fitting, inspection, and short command sequences. Use an IPF procedure for ODE simulations, instrument control, batch or loop-heavy processing across multiple datasets, reusable routines, or an explicit user request. Data size alone does not call for an IPF when a built-in command handles the work simply.
- Prefer `call_function` for direct typed return values over `Print`; use `inspect_experiment` for a single-shot overview of folders, waves, and windows, `capture_window` to visually verify plots, `compile_procedures` for silent `.ipf` reload/compile, and `abort_igor` to cancel hung server commands.
- When the user asks to understand their workflow or opens a `.pxp` with custom procedures or styled graphs, call `profile_workflow` (set `save_profile=True` to build up and incrementally merge a reusable workflow profile across experiments) and use `read_procedure` to inspect specific procedure functions.
- Use initialize guidance for elementary commands; do not enumerate or pre-read basic-reference pages as a task preflight. Use local MCP knowledge tools for uncertain syntax, unfamiliar operations, or unresolved diagnostics. Never use web search, a browser, HTTP fetch, or external sites to look up Igor documentation. If the local knowledge bundle lacks needed details, report the gap and ask the user rather than falling back online. The installer/updater download is a separate installation workflow, not task-time lookup. Search curated guides when a project-specific workflow is relevant, then read the useful match with `read_igor_document`.
- Combine short, dependent commands when that keeps one requested operation clear. Avoid unrelated mutations. Do not export figures unless the user requests files.

## Everyday Igor core

Waves hold point data plus dimension scaling and units. In pointwise assignments, `p` is the point index and `x` is the scaled X coordinate. Prefer direct wave assignments and built-in operations for ordinary arithmetic, fitting, and analysis; use an IPF procedure for ODEs, instrument control, repeated loops, or substantial multi-dataset workflows.

For uniformly sampled data, this is a safe starting pattern. Replace the example folder and graph names with fresh unique names, run setup in order, and check each result before continuing:

```igorpro
NewDataFolder/S root:Run_20261020_01
Make/N=1000 signal
SetScale/P x, 0, 0.1, "ms", signal
signal = exp(-x/2)
Display/N=RunPlot_20261020_01 signal
Label/W=RunPlot_20261020_01 left, "Signal"
Label/W=RunPlot_20261020_01 bottom, "Time (ms)"
ModifyGraph/W=RunPlot_20261020_01 rgb(signal)=(0,0,65535)
```

`NewDataFolder/S` creates and selects the folder; `Make/N` creates points; `SetScale/P` sets the first X value, step, and units. Omit `/O` and use new run folders so name collisions fail without replacing prior data. `Display` uses wave X scaling; use `Display yWave vs xWave` for irregular X values. Add traces with `AppendToGraph/W=graphName waveName`, label axes with `Label/W=graphName left` or `bottom`, and set trace RGB with `ModifyGraph/W=graphName rgb(traceName)=(r,g,b)`; RGB channels range from 0 to 65535. Keep original waves intact by deriving results in a new run folder.

These patterns are startup guidance, not a substitute for exact advanced/version-specific syntax. Use the bundled manuals when syntax is uncertain, an operation is unfamiliar, or advanced/version-specific behavior needs verification. Search session memory only for a repeated nontrivial workflow or relevant workaround; skip elementary one-offs.

Use `query_igor_help` for local details on Make, NewDataFolder, SetScale, Waves, Display, AppendToGraph, Label, or ModifyGraph when they are uncertain; do not follow external documentation links for task-time lookup.

## Tools

| Tool | Use |
|---|---|
| `ensure_igor` | Attach to Igor or start the configured executable through the server. |
| `open_igor_experiment` | Open an existing `.pxp` or `.pxt` through Igor's normal Open behavior; Igor must already be running. Unsaved-work prompts remain under Igor's control. |
| `igor_status` | Check server identity and pipe/COM availability without starting Igor. |
| `execute_igor` | Run requested Igor commands. |
| `call_function` | Call an Igor function directly for typed return values over the native pipe (preferred over `Print`). |
| `abort_igor` | Cancel a hung or long-running server command. |
| `capture_window` | Capture an Igor window image to visually verify plots. |
| `inspect_experiment` | Single-shot read-only overview of experiment metadata, bounded data-folder tree, wave names, and open windows without changing the active folder. |
| `profile_workflow` | Read-only workflow profile of folders, procedure outlines, and graph style conventions; set `save_profile=True` to persist and incrementally merge across experiments. |
| `list_waves`, `get_wave_data`, `read_variable`, `list_windows`, `get_experiment_info`, `get_system_information`, `get_igor_state` | Inspect open experiment data, windows, system info, and live Igor state. |
| `list_procedures`, `read_procedure`, `open_procedure`, `close_procedure`, `compile_procedures`, `get_compile_status` | List, read paginated source lines, open, close, and silently compile `.ipf` procedures. |
| `import_delimited_text` | Import supported numeric CSV, TSV, or TXT data. |
| `get_igor_dialogs`, `get_igor_compilation_errors` | Inspect visible dialogs and compiler diagnostics. |
| `dismiss_igor_dialogs`, `dismiss_igor_compilation_errors` | Close dialogs after reviewing the relevant result. |
| `query_igor_help` | Search official local manuals. |
| `search_igor_knowledge`, `read_igor_document` | Search and read bundled curated guides. |
| `search_session_memory`, `read_session_memory` | Find and read previous Igor session experience. |
| `capture_session` | Save a concise Markdown summary of useful outcomes. |

## Dialogs and retries

When the server starts Igor, it shows the new main window without activating it, so it remains available behind your current window. Later MCP actions preserve its current state and do not foreground it; if you minimize Igor, it stays minimized. Review the result of `execute_igor` and any returned diagnostics before taking another action. Inspect remaining dialogs with `get_igor_dialogs` or `get_igor_compilation_errors`; use an explicit dismissal tool only when appropriate. A timeout leaves the command outcome unknown; inspect Igor state before any retry.

## Session memory

MCP calls are automatically written to bounded JSONL logs in the configured session directory. This log preserves call history; it does not replace a concise summary of the useful outcome. After meaningful work, use `capture_session` when a result, preference, failure, or workaround could help future tasks. Search prior sessions only when repeating a nontrivial workflow or when a task may depend on a relevant prior result or workaround. Session files are local; do not create wiki pages or development reports, and do not commit the logs automatically.

For source checkouts, the host sets `IGOR_SESSION_DIR` to the repository `sessions/` directory. Installed deployments use `%LOCALAPPDATA%\igor-mcp\sessions` unless overridden.
