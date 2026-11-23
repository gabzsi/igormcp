# Igor Pro MCP

A fast, research-safe Windows MCP server for **WaveMetrics Igor Pro** (versions **6.0–10.5+**). It connects AI assistants (Claude Desktop, Codex, Cursor, Antigravity, etc.) directly to Igor Pro over local stdio—with **100% native JSON-RPC 2.0 named pipes on Igor Pro 10.04+** (no COM registration required) and automatic **COM fallback** for older Igor versions (6.0–10.03).

---

## Features — What This MCP Can Do

* **Direct Analysis, Curve Fitting & Stiff ODEs:** Run Igor commands (`execute_igor`) or invoke built-in and user-defined functions with typed JSON arguments/returns (`call_function`—numbers, strings, complex numbers, wave refs, data-folder refs, and `kwargs`).
* **Visual Self-Verification (`capture_window`):** Capture any graph, panel, table, layout, or Gizmo (even hidden windows) as an inline PNG so the AI can visually inspect axes, labels, legends, and trace colors and fix overlaps automatically.
* **Learn Your Lab's Workflow & House Style (`profile_workflow` + `read_procedure`):** Point the AI at an open `.pxp` and ask *"understand my workflow"*. In one read-only call, it maps your data-folder layout, user `.ipf` procedures (including instrument-control drivers, control panels, and custom `Menu` items), loaded hardware XOPs (`IgorInfo(10)`), and graph styling defaults (`WinRecreation`: font, size, ticks, mirror, width/height, 16-bit RGB palette). Save with `save_profile=True` to **incrementally build up** a local profile across multiple `.pxp` experiments.
* **Structured Data & Text-Wave Access:** Inspect nested folders in one shot (`inspect_experiment`), list waves/procedures, import CSV/TSV/TXT files (`import_delimited_text`), read globals (`read_variable`), and read 1D/2D numeric waves or **1D text waves** with `start` pagination (`get_wave_data`).
* **First-Class `.ipf` Procedure Development:** Open, read, edit on disk, silently hot-reload & compile (`compile_procedures`), inspect structured file/line compile errors, and close procedure files without blocking dialogs.
* **Fast, Low-RAM Offline Documentation & Curated Guides:** Includes 8 audited, MIT-licensed Igor Pro guides (curve fitting, publication figures, wave architecture, data folders, gotchas, Python/NumPy Rosetta Stone, syntax, COM) plus a local cache of WaveMetrics' Markdown reference—indexed in ~10 MB RAM with **11–18 ms** warm search and zero web calls during tasks.
* **Editable HTML Reports (Igor 9+):** Package selected graphs into a shareable HTML report—either linked to independent single-figure `.pxp` editors (click a figure to edit in Igor, `Ctrl+S` to refresh) or a self-contained offline browser editor whose axis/trace edits can be applied back to Igor (`apply_html_report`).
* **Built for Research Safety & Reliability:**
  * Persistent named-pipe pool (~10–25 ms warm calls) with lock-free `abort_igor` and dialog-resilient `get_igor_state`.
  * MCP progress heartbeats (`notifications/progress`) keep strict hosts from timing out during long ODE/fitting runs.
  * Serialized commands, strict no-replay-after-dispatch on timeouts, no auto-dismissed dialogs, no `quit_igor` tool, and focus-preserving startup (shows only the single main Igor window without stealing focus).

---

## How to Use

Once configured in your MCP host (Claude Desktop, Codex, Cursor, Antigravity), just talk to your assistant naturally:

| Goal | Example Prompt |
|---|---|
| **Start / connect** | *"Connect to Igor Pro and check status."* (`ensure_igor` / `igor_status`) |
| **Learn your workflow** | *"Understand my workflow from this experiment and save it under profile `my-lab`."* (`profile_workflow(save_profile=True)`) |
| **Explore an unfamiliar `.pxp`** | *"Inspect the folder structure, waves, and open windows in this experiment."* (`inspect_experiment`) |
| **Analyze & fit data** | *"Import `run01.csv` into `root:Exp1:`, fit a Gaussian to `y` vs `x`, and report the fit parameters."* (`import_delimited_text`, `execute_igor`, `call_function`) |
| **Make & verify a figure** | *"Plot the data and fit in ACS single-column width with inward ticks, then capture the graph and check for label overlaps."* (`execute_igor`, `capture_window`) |
| **Develop `.ipf` procedures** | *"Read `MyAnalysis.ipf`, add a baseline-subtraction function, recompile, and test it on `root:w_raw`."* (`read_procedure`, `compile_procedures`, `call_function`) |
| **Share an editable report** | *"Create an editable HTML report from `Graph0` and `Graph1`."* (`create_html_report`) |

> **Tip:** `ensure_igor` is the only startup step—let the MCP server attach to or launch Igor rather than launching Igor manually from a shell. On Igor Pro 10.04+, if Igor is already open, enable **Help > AI Tools > Allow AI Apps to Connect** so the native JSON-RPC pipe is published.

---

## Tool Overview (36 Tools in `full` / 33 in `core`)

| Category | Tools |
|---|---|
| **Control & Execution** | `ensure_igor`, `igor_status`, `execute_igor`, `call_function`, `abort_igor`, `open_igor_experiment`, `import_delimited_text` |
| **Inspect, Profile & Capture** | `inspect_experiment`, `profile_workflow`, `list_waves`, `get_wave_data`, `read_variable`, `list_windows`, `capture_window`, `get_experiment_info`, `get_system_information` |
| **Procedures (`.ipf`)** | `list_procedures`, `read_procedure`, `open_procedure`, `close_procedure`, `compile_procedures`, `get_compile_status` |
| **State, Dialogs & Diagnostics** | `get_igor_state`, `get_igor_dialogs`, `dismiss_igor_dialogs`, `get_igor_compilation_errors`, `dismiss_igor_compilation_errors` |
| **Offline Knowledge** | `query_igor_help`, `search_igor_knowledge`, `read_igor_document` |
| **Session Memory** | `search_session_memory`, `read_session_memory`, `capture_session` |
| **Optional HTML Reports** *(omitted in `core`)* | `create_html_report`, `open_report_graph`, `apply_html_report` |

---

## Installation

### Requirements
* **Windows** with **Python 3.10+**
* **WaveMetrics Igor Pro** (10.04+ recommended for native JSON-RPC named pipes; 6.0+ supported via COM; 9+ required for HTML reports)
* An MCP host supporting local stdio servers (Claude Desktop, Codex, Cursor, Antigravity, etc.)

### Recommended Setup (Clone + Local WaveMetrics Docs)

```powershell
git clone https://github.com/gabzsi/igormcp.git
Set-Location igormcp
.\setup.ps1
```

What `.\setup.ps1` does:
1. Creates `.venv` and installs the package and its dependencies.
2. Downloads and validates the official WaveMetrics Markdown pages listed by [docs.wavemetrics.com/llms.txt](https://docs.wavemetrics.com/llms.txt) into the Git-ignored `.local/knowledge/` cache (requires internet access).
3. Writes path-resolved MCP config snippets under `.local/mcp-config.example.json` (Claude Desktop / Cursor / Antigravity) and `.local/mcp-config.codex.toml` (Codex).

Copy the generated snippet into your MCP host configuration and restart/reconnect the host. `setup.ps1` never edits your host config or starts Igor automatically.

* **Custom Python path:** `.\setup.ps1 -Python 'C:\Path\to\python.exe'`
* **Merge an existing local knowledge bundle:** `.\setup.ps1 -IncludeExistingKnowledge 'D:\path\to\knowledge'`
* **Skip online doc download (using an existing valid bundle):** `.\setup.ps1 -SkipKnowledgeDownload`

### Updating an Existing Install

```powershell
git pull
.\setup.ps1 -SkipKnowledgeDownload   # fast code-only update (omit flag to also refresh WaveMetrics docs)
```

Then restart your MCP host. Your local `.local/knowledge/` cache and `sessions/` memory are preserved across updates.

To refresh only the local WaveMetrics documentation cache later:

```powershell
.\.venv\Scripts\python.exe -B scripts\update_knowledge.py --output .local\knowledge
```

### Alternative: Direct Install Without Cloning

```powershell
# Run on demand with uvx
uvx --from git+https://github.com/gabzsi/igormcp.git igor-mcp-server

# Or install globally with pipx / pip
pipx install git+https://github.com/gabzsi/igormcp.git
pip install git+https://github.com/gabzsi/igormcp.git
```

*(Direct installs include the 8 curated guides immediately; set `IGOR_KNOWLEDGE_DIR` to a local bundle if you also want `query_igor_help` to search WaveMetrics' full Markdown reference.)*

---

## Configuration & Environment Variables

| Variable | Default | Description |
|---|---|---|
| `IGOR_TRANSPORT` | `auto` | `auto` (prefer Igor 10.04+ native JSON-RPC pipe, fall back to COM), `pipe` (require native pipe, fail closed), or `com` (force COM). |
| `IGOR_PIPE_NAME` | _(auto-discovered)_ | Explicit Windows named pipe path (e.g. `\\.\pipe\IgorPro-...`) to skip `IgorProInstances.json` discovery. |
| `IGOR_VERSION_PREFIX` | _(none)_ | Prefer a specific Igor Pro version prefix when multiple instances are running (e.g. `10.5`). |
| `IGOR_TOOL_PROFILE` | `full` | `full` (all 36 tools) or `core` (33 tools, omitting the 3 HTML report tools to save context tokens in eager-loading hosts like Codex/Claude Desktop). |
| `IGOR_PRO_EXE` | `C:\Program Files\WaveMetrics\Igor Pro 10 Folder\IgorBinaries_x64\Igor64.exe` | Path to `Igor64.exe` / `Igor.exe` used by `ensure_igor` when launching Igor. |
| `IGOR_KNOWLEDGE_DIR` | _(bundled guides only)_ | Directory containing the validated local WaveMetrics Markdown bundle used by `query_igor_help`. |
| `IGOR_SESSION_DIR` | `%LOCALAPPDATA%\igor-mcp\sessions` | Directory where local JSONL tool logs, `capture_session` notes, and saved `profile_workflow` profiles are stored. |

---

## Editable HTML Reports

Ask for an editable HTML report separately, naming the graphs to include (requires Igor Pro 9+):

* **Native single-figure mode (default):** `create_html_report` exports independent single-graph `.pxp` copies (`SaveGraphCopy`), PNG previews, and a save hook. Install the compiled Windows link handler once with `.venv\Scripts\python.exe -B igor_report_links.py --install` (compiles `native/IgorFigureLink.cs`). Clicking a figure opens only that graph in Igor; `Ctrl+S` updates the PNG preview without touching the original full experiment. Keep the HTML and its `_figures` folder together. Handler registration is stored locally under `.local/native-report-links/`.
* **Self-contained browser mode (`editing_mode="browser"`):** Embeds original PNG previews and numerical XY trace data in a single offline HTML file (no Igor, MCP, or internet required to edit axis labels/limits, log scales, colors, line widths, legends, notes, or export SVG). Supports top-level 1D XY graphs up to 20 figures, 20 traces/figure, 10,000 points/trace, and 100,000 points total; data are never silently truncated.
* **Apply browser edits back to Igor (`apply_html_report`):** Verifies the original saved experiment is open at its original path and checks wave/data fingerprints before applying axis and trace styling back to Igor (never executes HTML/JavaScript).

---

## Knowledge, Session Memory & Research Data Privacy

* **Local-only documentation:** The server sends a compact operational Igor core (~4.8k chars) on `initialize` and searches only local files during tasks—it **never** makes outbound web requests while working.
* **Local-only session & workflow memory:** Automatic JSONL logs, `capture_session` summaries, and `profile_workflow` files live locally under `IGOR_SESSION_DIR` (Git-ignored, never committed). Logs omit bulk wave arrays (`data`, `values`, `points`, `samples`, `matrix`, `array`), strip directory paths to filenames, and redact secret-like keys.
* **What goes to your AI provider:** Like any MCP server, tool inputs and outputs returned in conversation (commands, wave/folder names, `get_wave_data` slices, procedure text, and `capture_window` PNGs) are sent by your MCP host to its configured AI provider. For unpublished or restricted research data, use a university/enterprise AI subscription with contractual no-training / Zero-Data-Retention (ZDR) terms—or a local LLM—in accordance with your institution's data policy.
* **Optional agent skill:** [.agents/skills/igor-pro/SKILL.md](.agents/skills/igor-pro/SKILL.md) provides additional task-routing and data-safety guidance for skill-aware agents (not required by the server).

---

## Development & Testing

Run the full offline test suite (174 tests, including a real local Windows named-pipe mock server—no Igor installation required):

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
```

GitHub Actions CI ([.github/workflows/tests.yml](.github/workflows/tests.yml)) runs the full offline suite on `windows-latest` across Python 3.10 and 3.12 on every push and pull request.

## License & Third-Party Material

The server and 8 project-authored guides are released under the [MIT License](LICENSE). This license does not grant rights to Igor Pro, WaveMetrics documentation, or other third-party material. WaveMetrics' official manuals are excluded from this repository and downloaded locally by users during setup.
