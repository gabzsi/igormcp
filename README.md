# Igor Pro MCP

A Windows MCP server for WaveMetrics Igor Pro. It runs as a local stdio process and controls Igor through COM. The server works independently of an agent skill, wiki, or Git history.

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

## What it does

The server exposes 19 tools over MCP stdio:

- **Control:** `ensure_igor`, `igor_status`, `execute_igor`, `open_igor_experiment`, `import_delimited_text`
- **Inspect:** `list_waves`, `get_wave_data`, `read_variable`, `list_procedures`
- **Dialogs and diagnostics:** `get_igor_dialogs`, `dismiss_igor_dialogs`, `get_igor_compilation_errors`, `dismiss_igor_compilation_errors`
- **Knowledge:** `query_igor_help`, `search_igor_knowledge`, `read_igor_document`
- **Session memory:** `search_session_memory`, `read_session_memory`, `capture_session`

Use direct `execute_igor` commands for ordinary calculations, plotting, fitting, inspection, and short command sequences. Use an IPF procedure for ODE simulations, instrument control, repeated loops, substantial batch work, or reusable routines. `igor_status` only checks connectivity; it does not start Igor. `open_igor_experiment` requires Igor to be running and follows normal Igor Open behavior. Unsaved-work prompts remain under the user's control.

The server serializes Igor commands and bounds response waits. A timeout means the outcome may be unknown: inspect Igor state and diagnostics before retrying. Review dialogs or compiler errors before using an explicit dismissal tool.

When starting a new Igor process, the server requests that its main window be shown without activation. It does not intentionally restore or foreground an existing window. Igor or Windows may still affect focus independently.

## Knowledge and the optional skill

The server sends a compact operational Igor core in each MCP initialization: safe data-folder and wave patterns; point indexes versus scaled coordinates; wave scaling and units; supported text import and experiment saving; routine statistics and built-in fits; simple 2-D graphs, labels, and colors; and direct-command versus procedure guidance. Use these patterns for familiar work without searching a manual page. This is prompt context sent on connection, not a promise of permanent model or host caching.

The downloaded reference bundle is organized by task. Its broad foundation spans Igor Basics, Data, Graphing, Analysis, User Interface, and Programming, including common wave, import/export, experiment-file, graph, statistics, fitting, and routine command topics. These pages are searchable and readable on demand; the server does not preload whole pages into its initialize instructions. The `knowledge_tier` field distinguishes common `core-reference` pages from other `on-demand` references, but both tiers require a knowledge-tool call to retrieve page content. Specialized command details, extensions, instrument and XOP workflows, ODEs, Gizmo, and advanced or multidimensional plotting remain reference lookups when needed.

The repository and Python package include eight curated project guides, but no WaveMetrics manuals or extracted manual images. By default, setup downloads the Markdown pages listed by WaveMetrics' `llms.txt` documentation index into a local, ignored cache. A fresh install gets the pages in that index at download time; its coverage can change and may omit older or otherwise unlisted Igor Pro material. Subsequent updates preserve compatible non-indexed manuals already in the local bundle. This is not a claim that every Igor Pro manual, installed extension, or help topic is included. `query_igor_help` searches a validated local bundle when the generated MCP entry sets `IGOR_KNOWLEDGE_DIR`. `-SkipKnowledgeDownload` uses an existing valid bundle and stops with an error if none is available. The curated guides remain available through `search_igor_knowledge` in either case.

To refresh the local cache after setup, run:

```powershell
.\.venv\Scripts\python.exe -B scripts\update_knowledge.py --output .local\knowledge
```

If no valid bundle was available when setup generated the MCP configuration, rerun `.\setup.ps1` after a successful refresh. The downloader reads the official Markdown index at [docs.wavemetrics.com/llms.txt](https://docs.wavemetrics.com/llms.txt); this project does not import native Igor help files. For product documentation and terms, see [WaveMetrics' manual page](https://www.wavemetrics.com/products/igorpro/manual) and [Igor Pro documentation](https://docs.wavemetrics.com/igorpro/).

The optional [igor-pro skill](.agents/skills/igor-pro/SKILL.md) adds task routing and data-safety guidance for compatible agents. It is not required by the server and does not configure an MCP host.

## Session memory and privacy

MCP tool calls are recorded automatically as bounded JSONL observations. Use `search_session_memory` and `read_session_memory` to find task-specific prior work; `capture_session` writes a separate concise summary when a result or workaround should persist. Session data defaults to `%LOCALAPPDATA%\igor-mcp\sessions`; set `IGOR_SESSION_DIR` to use another location. The setup script leaves this setting out so the server uses its private per-user default.

Logs omit bulk wave arrays and redact values under fields named like secrets, but command text and results can still contain sensitive information. Keep session files private. They are not committed automatically, are append-only, and are not deleted when the configured storage cap is reached.

## License and third-party material

The server and project-authored guides are released under the [MIT License](LICENSE). The license does not grant rights to Igor Pro, WaveMetrics documentation, or other third-party material. The local documentation bundle is excluded from this public repository; do not add local manual exports or their image assets without redistribution permission.
