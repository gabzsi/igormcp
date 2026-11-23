# Igor Pro MCP

A Windows MCP server for WaveMetrics Igor Pro. It runs as a local stdio process and controls Igor through a hybrid transport: **Igor Pro 10.04+ runs 100% natively over JSON-RPC 2.0 named pipes without requiring COM registration** (controlled via `IGOR_TRANSPORT=auto|pipe|com`), with automatic COM fallback for older Igor versions (6.0–10.03). The server works independently of an agent skill, wiki, or Git history.

## Quick start

You need Windows, Python 3.10 or newer, an installed Igor Pro (version 10 is recommended; the server currently accepts 6.0+), and an MCP host that supports local stdio servers.

```powershell
git clone https://github.com/gabzsi/igormcp.git
Set-Location igormcp
.\setup.ps1
```

The setup script creates `.venv`, installs this package and its dependencies, downloads and validates the official Markdown pages listed by WaveMetrics' documentation index, and writes two path-resolved configuration examples under `.local/`. The download requires internet access. The downloaded pages stay in the Git-ignored `.local/knowledge/`; they are not included in this repository or its package. If an existing `.local/knowledge/` bundle is present, updates retain compatible manual documents that are no longer in the current online index. Use `-IncludeExistingKnowledge 'D:\path\to\knowledge'` to merge another validated local bundle during setup. If setup cannot find Python, pass `-Python` with the path to a Python 3.10+ executable.

Setup does not edit your MCP host configuration or start Igor. Copy the matching entry from `.local/mcp-config.codex.toml` or `.local/mcp-config.example.json` into your host's configuration, then restart or reconnect the host. A successful download activates the validated local bundle. If refresh fails but a previously validated local or explicitly supplied bundle exists, setup warns that it is using that older bundle. If no complete bundle is available, setup stops without writing MCP config files; retry with the command shown by setup. To skip the network download, use `.\setup.ps1 -SkipKnowledgeDownload` only when a valid local bundle already exists or pass one with `-IncludeExistingKnowledge`.

When the MCP server is connected, call `ensure_igor` to attach to or start Igor. This is the only startup step; do not launch Igor from a shell. To verify the code without Igor, run the offline suite:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
```

You can also install or run the server directly without cloning the repository:

```powershell
# Run on demand with uvx (from GitHub)
uvx --from git+https://github.com/gabzsi/igormcp.git igor-mcp-server

# Or install globally with pipx / pip
pipx install git+https://github.com/gabzsi/igormcp.git
pip install git+https://github.com/gabzsi/igormcp.git
```

From a local checkout, install with `pip install .` (or `pip install -e .`) and launch the server via the `igor-mcp-server` (or `igor-pro-mcp`) console script entry point. Note that Igor Pro 10.04+ runs 100% natively over JSON-RPC named pipes without requiring COM registration.

## Environment Variables & Configuration

| Variable | Default | Description |
|---|---|---|
| `IGOR_TRANSPORT` | `auto` | Transport selection: `auto` (prefer Igor Pro 10.04+ native JSON-RPC named pipe, fall back to COM), `pipe` (require native pipe), or `com` (force COM). |
| `IGOR_PIPE_NAME` | _(auto-discovered)_ | Explicit Windows named pipe path (e.g. `\\.\pipe\IgorPro-...`) to bypass `IgorProInstances.json` discovery. |
| `IGOR_VERSION_PREFIX` | _(none)_ | Filter auto-discovered native-pipe instances by Igor Pro version prefix (e.g. `10.5`). |
| `IGOR_TOOL_PROFILE` | `full` | Exposed MCP tool set: `full` (all 34 tools) or `core` (31 tools, omitting the 3 optional HTML report tools for leaner eager-host contexts). |
| `IGOR_PRO_EXE` | `C:\Program Files\WaveMetrics\Igor Pro 10 Folder\IgorBinaries_x64\Igor64.exe` | Path to `Igor64.exe` / `Igor.exe` used by `ensure_igor` when starting Igor. |
| `IGOR_KNOWLEDGE_DIR` | _(bundled guides only)_ | Directory containing the validated local WaveMetrics Markdown bundle used by `query_igor_help`. |
| `IGOR_SESSION_DIR` | `%LOCALAPPDATA%\igor-mcp\sessions` | Directory where automatic JSONL tool logs and `capture_session` summaries are stored. |

## What it does

The server exposes 34 tools over MCP stdio:

- **Control & execution:** `ensure_igor`, `igor_status`, `execute_igor`, `call_function`, `abort_igor`, `open_igor_experiment`, `import_delimited_text`
- **Inspect & capture:** `inspect_experiment`, `list_waves`, `get_wave_data`, `read_variable`, `list_windows`, `capture_window`, `get_experiment_info`, `get_system_information`
- **Procedures:** `list_procedures`, `open_procedure`, `close_procedure`, `compile_procedures`, `get_compile_status`
- **Optional HTML reports:** `create_html_report`, `open_report_graph`, `apply_html_report`
- **State, dialogs, and diagnostics:** `get_igor_state`, `get_igor_dialogs`, `dismiss_igor_dialogs`, `get_igor_compilation_errors`, `dismiss_igor_compilation_errors`
- **Knowledge:** `query_igor_help`, `search_igor_knowledge`, `read_igor_document`
- **Session memory:** `search_session_memory`, `read_session_memory`, `capture_session`

Use direct `execute_igor` commands for ordinary calculations, plotting, fitting, inspection, and short command sequences. Use an IPF procedure for ODE simulations, instrument control, repeated loops, substantial batch work, or reusable routines. `inspect_experiment` returns a single-shot read-only summary of experiment identity, bounded data-folder hierarchy (object counts and optional wave names), and open windows without changing the active folder. `get_wave_data` reads 1D/2D numeric waves and 1D text waves with 0-based `start` offset pagination (up to 10,000 points per call). `igor_status` only checks connectivity; it does not start Igor. `open_igor_experiment` requires Igor to be running and follows normal Igor Open behavior. Unsaved-work prompts remain under the user's control.

The server serializes Igor commands and bounds response waits. For long-running operations (`execute_igor`, `call_function`, `compile_procedures`, `create_html_report`, `apply_html_report`), the server emits periodic MCP progress heartbeats when the client supplies a `progressToken` so strict MCP hosts do not time out prematurely. A timeout means the outcome may be unknown: inspect Igor state and diagnostics before retrying. Review dialogs or compiler errors before using an explicit dismissal tool.

When starting a new Igor process, the server requests that its main window be shown without activation. It does not intentionally restore or foreground an existing window. Igor or Windows may still affect focus independently.

## Editable HTML reports

Ask for an editable HTML report separately, naming the graphs to include. By default, `create_html_report` creates independent single-graph `.pxp` copies with Igor's `SaveGraphCopy`, adjacent PNG previews, and a native Igor save hook. Click a preview to open only that graph in a normal independent Igor instance. Ctrl+S saves the graph copy and updates its PNG; reload the HTML to see the change. The original full experiment stays unchanged. This is a linked single-figure workflow, not native OLE embedded inside a browser.

Install the compiled Windows link handler once with `.venv\Scripts\python.exe -B igor_report_links.py --install`. Setup compiles the small C# source in `native/` with the Windows .NET Framework compiler. Figure clicks then run `IgorFigureLink.exe` directly: no Python, MCP, or AI is launched. The launcher starts Igor with `/I` in normal mode, never `/Automation` or `/N`, and holds no COM reference that could terminate the editor on release. Repeat clicks reuse that figure's running editor when its experiment title still matches. Keep the HTML and its `_figures` folder together. Recipients need Igor, the launcher, and local report registration; plain HTML cannot host Windows OLE by itself. URI arguments select only locally registered files, not arbitrary commands.

The handler registry is stored under `.local/native-report-links/` and should remain private. Installation preserves an unrelated existing handler and can upgrade this project's earlier Python handler. To remove it, remove only `HKCU\Software\Classes\igor-report` if it still points to this installation. Standalone browser-report conversion was part of the earlier experimental Python-link prototype; use `create_html_report` to generate the native single-figure package.

Explicit `editing_mode="browser"` creates the self-contained browser editor described below. It writes a new `.html` file with original Igor PNG previews and full numerical trace data. Neither mode overwrites an existing report or saves the experiment. Igor Pro 9 or newer is required to create reports.

Report operations have a bounded 120-second response wait to accommodate multi-graph exports; ordinary commands retain their 25-second wait. A timeout still leaves the operation outcome unknown and admission held until the worker finishes. Inspect state before retrying.

Recipients simply open the HTML in a modern browser. They need no Igor, MCP, AI, installation, or internet connection. Click a figure or **Edit figure**, change axis labels/limits, logarithmic scaling, colors, line widths, and display modes, then **Apply figure changes**. Titles, notes, captions, and legend labels can also be edited. **Save edited HTML** downloads a new editable copy; changes do not overwrite the original file automatically. Use the downloaded file for sharing or further edits. SVG export is also available. Numerical data are embedded in the HTML, so sharing the report shares those data.

The browser uses a simplified XY rendering, retaining the original Igor preview for comparison. It does not reproduce Igor's full native editor, rich text, annotations, fonts, custom ticks, or symbol shapes. The initial view is the original preview; editing switches to the browser rendering. This first version supports top-level graphs with portable graph/trace names, real 1D full-wave XY traces on bottom/left axes, line/dot/marker modes, and linear/logarithmic scales. Limits are 20 figures, 20 traces per figure, 10,000 points per trace, and 100,000 points total. Unsupported image/contour/box/violin plots, error bars, subranges, transformed/hidden/variable-color traces, and category or additional axes are rejected. Data are never silently truncated.

To transfer an edited report's axis and trace styling back to Igor, explicitly ask to apply it with `apply_html_report`. The original saved experiment must already be open at its original path; graphs, trace/wave identities, and numerical data are checked before styling changes start. The HTML and its JavaScript are never executed by this tool. It applies axis labels, limits, log settings, colors, widths, and display modes. Report titles, captions, legend labels, and notes stay in HTML. Save the experiment in Igor afterwards. Reports created from unsaved experiments are portable snapshots and cannot be applied back. A runtime error during application may leave partial styling changes, which the tool reports; inspect Igor before retrying.

## Knowledge and the optional skill

The server sends a compact operational Igor core in each MCP initialization: safe data-folder and wave patterns; point indexes versus scaled coordinates; wave scaling and units; supported text import and experiment saving; routine statistics and built-in fits; simple 2-D graphs, labels, and colors; and direct-command versus procedure guidance. Use these patterns for familiar work without searching a manual page. Do not enumerate or pre-read basic-reference pages as a task preflight. Retrieve a specific local page only when syntax is uncertain, an operation is unfamiliar, or diagnostics leave an unresolved question. Never use web search, a browser, HTTP fetch, forums, or external sites to look up Igor documentation during task execution. If the local bundle lacks needed details, report the gap and ask the user rather than falling back online or starting a download. This is prompt context sent on connection, not a promise of permanent model or host caching.

The downloaded reference bundle is organized by task. Its broad foundation spans Igor Basics, Data, Graphing, Analysis, User Interface, and Programming, including common wave, import/export, experiment-file, graph, statistics, fitting, and routine command topics. These pages are searchable and readable on demand; the server does not preload whole pages into its initialize instructions. The `knowledge_tier` field distinguishes common `core-reference` pages from other `on-demand` references, but both tiers require a knowledge-tool call to retrieve page content. Specialized command details, extensions, instrument and XOP workflows, ODEs, Gizmo, and advanced or multidimensional plotting remain reference lookups when needed.

The repository and Python package include eight curated project guides, but no WaveMetrics manuals or extracted manual images. By default, setup downloads the Markdown pages listed by WaveMetrics' `llms.txt` documentation index into a local, ignored cache. A fresh install gets the pages in that index at download time; its coverage can change and may omit older or otherwise unlisted Igor Pro material. Subsequent updates preserve compatible non-indexed manuals already in the local bundle. This is not a claim that every Igor Pro manual, installed extension, or help topic is included. `query_igor_help` searches a validated local bundle when the generated MCP entry sets `IGOR_KNOWLEDGE_DIR`. `-SkipKnowledgeDownload` uses an existing valid bundle and stops with an error if none is available. The updater downloads are permitted only for explicitly requested installation or refresh; agents use the local bundle for task-time lookups and report gaps. The curated guides remain available through `search_igor_knowledge` in either case.

To refresh the local cache after setup, run:

```powershell
.\.venv\Scripts\python.exe -B scripts\update_knowledge.py --output .local\knowledge
```

If no valid bundle was available when setup generated the MCP configuration, rerun `.\setup.ps1` after a successful refresh. The downloader reads the official Markdown index at [docs.wavemetrics.com/llms.txt](https://docs.wavemetrics.com/llms.txt); this project does not import native Igor help files. The Markdown index link documents the setup/refresh source only, not an agent's task-time lookup path.

The optional [igor-pro skill](.agents/skills/igor-pro/SKILL.md) adds task routing and data-safety guidance for compatible agents. It is not required by the server and does not configure an MCP host.

## Session memory and privacy

MCP tool calls are recorded automatically as bounded JSONL observations. Search and read prior session memory when repeating a nontrivial workflow or when the current task may depend on a relevant result or workaround; skip elementary one-off commands. `capture_session` writes a separate concise summary when a result or workaround should persist. Session data defaults to `%LOCALAPPDATA%\igor-mcp\sessions`; set `IGOR_SESSION_DIR` to use another location. The setup script leaves this setting out so the server uses its private per-user default.

Logs omit bulk wave arrays and redact values under fields named like secrets, but command text and results can still contain sensitive information. Keep session files private. They are not committed automatically, are append-only, and are not deleted when the configured storage cap is reached.

## License and third-party material

The server and project-authored guides are released under the [MIT License](LICENSE). The license does not grant rights to Igor Pro, WaveMetrics documentation, or other third-party material. The local documentation bundle is excluded from this public repository; do not add local manual exports or their image assets without redistribution permission.
