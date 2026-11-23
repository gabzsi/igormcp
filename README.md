# Igor Pro MCP

A fast, research-safe Windows MCP server for **WaveMetrics Igor Pro** (6.0–10.5+).  
Uses **native JSON-RPC named pipes** on Igor Pro 10.04+ (no COM required) with automatic **COM fallback** for Igor 6.0–10.03. Works with Claude Desktop, Codex, Cursor, and Antigravity.

## What It Can Do

* **Learns Your Lab's Workflow Over Time:**
  * **From existing `.pxp` files (`profile_workflow`):** Ask *"understand my workflow"* and it reads your folder structure, `.ipf` / instrument-control procedures, custom menus, loaded XOPs, and graph house style (`WinRecreation`)—then **incrementally merges** new experiments into a growing local profile (`save_profile=True`).
  * **From your sessions (`capture_session` + `search_session_memory`):** Automatically logs tool runs locally (redacted, no bulk wave arrays) and lets you save analysis recipes, fit settings, and workarounds so future sessions pick up right where you left off instead of re-explaining your lab's conventions.
* **Analyze & Fit:** Run commands (`execute_igor`), call Igor functions with typed JSON args/returns (`call_function`), import CSV/TSV/TXT, and read 1D/2D numeric or 1D text waves with pagination (`get_wave_data`).
* **See & Fix Plots:** Capture graphs, panels, tables, layouts, or Gizmos as PNGs (`capture_window`) so the AI can visually catch clipped labels, overlaps, or wrong colors.
* **Develop `.ipf` Procedures:** Read embedded `Procedure` or `.ipf` windows (`read_procedure`), hot-reload edits silently from disk (`compile_procedures`), and inspect structured file/line compile errors.
* **Offline Docs & Curated Guides:** 8 audited Igor guides + local WaveMetrics Markdown cache (~10 MB RAM, ~15 ms search, zero web calls during work).
* **Editable HTML Reports (Igor 9+):** Export graphs to linked single-figure `.pxp` editors or a self-contained offline browser editor (`create_html_report`, `apply_html_report`).
* **Research-Safe Defaults:** Serialized commands, no-replay-after-dispatch on timeouts, MCP progress heartbeats for long ODE/fit runs, lock-free `abort_igor` & `get_igor_state`, no auto-dismissed dialogs, no `quit_igor`.

## Example Prompts

* *"Understand my workflow from this `.pxp` and save my lab's procedures and plotting style under `my-lab`."*
* *"Remember this fitting recipe and baseline workaround for next time."* (`capture_session`)
* *"Check our past sessions for how we aligned and fitted the kinetics traces, then run it on `root:Run02`."* (`search_session_memory`)
* *"Import `run01.csv` into `root:Exp1:`, fit a Gaussian, plot in ACS single-column width with inward ticks, and capture the graph to check formatting."*
* *"Read `MyAnalysis.ipf`, add a baseline subtraction function, compile, and test it on `root:w_raw`."*

> On Igor 10.04+, enable **Help > AI Tools > Allow AI Apps to Connect** if Igor is already open (or let `ensure_igor` launch Igor automatically).

## Installation

**Requirements:** Windows, Python 3.10+, Igor Pro 6.0+ (10.04+ recommended).

```powershell
git clone https://github.com/gabzsi/igormcp.git
Set-Location igormcp
.\setup.ps1
```

`setup.ps1` creates `.venv`, downloads WaveMetrics' Markdown docs into `.local/knowledge/` (Git-ignored), and writes ready-to-copy MCP configs in `.local/mcp-config.example.json` and `.local/mcp-config.codex.toml`. Copy the snippet into your MCP host and restart it.

**Update later:**
```powershell
git pull
.\setup.ps1 -SkipKnowledgeDownload   # omit flag to also refresh WaveMetrics docs
```

**Or run without cloning:**
```powershell
uvx --from git+https://github.com/gabzsi/igormcp.git igor-mcp-server
# or: pipx install git+https://github.com/gabzsi/igormcp.git
```

## Tools (36 in `full` / 33 in `core`)

| Group | Tools |
|---|---|
| **Execution** | `ensure_igor`, `igor_status`, `execute_igor`, `call_function`, `abort_igor`, `open_igor_experiment`, `import_delimited_text` |
| **Inspect & Visuals** | `inspect_experiment`, `profile_workflow`, `list_waves`, `get_wave_data`, `read_variable`, `list_windows`, `capture_window`, `get_experiment_info`, `get_system_information` |
| **Procedures** | `list_procedures`, `read_procedure`, `open_procedure`, `close_procedure`, `compile_procedures`, `get_compile_status` |
| **State & Dialogs** | `get_igor_state`, `get_igor_dialogs`, `dismiss_igor_dialogs`, `get_igor_compilation_errors`, `dismiss_igor_compilation_errors` |
| **Docs & Memory** | `query_igor_help`, `search_igor_knowledge`, `read_igor_document`, `search_session_memory`, `read_session_memory`, `capture_session` |
| **HTML Reports** *(optional)* | `create_html_report`, `open_report_graph`, `apply_html_report` *(omitted when `IGOR_TOOL_PROFILE=core`)* |

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `IGOR_TRANSPORT` | `auto` | `auto` (pipe first, COM fallback), `pipe` (pipe only), or `com` (COM only). |
| `IGOR_TOOL_PROFILE` | `full` | `full` (36 tools) or `core` (33 tools, omits HTML reports for leaner contexts). |
| `IGOR_PIPE_NAME` / `IGOR_VERSION_PREFIX` | _(auto)_ | Target a specific Igor named pipe or version prefix (e.g. `10.5`). |
| `IGOR_PRO_EXE` | Igor Pro 10 default | Path to `Igor64.exe` / `Igor.exe`. |
| `IGOR_KNOWLEDGE_DIR` | _(curated guides)_ | Path to local WaveMetrics Markdown bundle (`.local/knowledge`). |
| `IGOR_SESSION_DIR` | `%LOCALAPPDATA%\igor-mcp\sessions` | Local folder for redacted JSONL logs, session notes, and saved workflow profiles. |

## Privacy & Research Data

* **Everything in this server is local:** Zero outbound web calls during work; logs and workflow profiles stay on disk (Git-ignored, bulk wave arrays omitted, paths stripped to filenames).
* **AI provider boundary:** Commands, wave slices, procedure text, and `capture_window` PNGs sent in chat go to your MCP host's AI provider. For restricted/unpublished lab data, use a university/enterprise AI plan (no-training / ZDR) or a local LLM.

## Tests & License

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests   # 174 offline tests, no Igor needed
```

Code and 8 curated guides are [MIT Licensed](LICENSE). WaveMetrics manuals are not bundled and are downloaded locally during setup.
