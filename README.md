# Igor Pro MCP

A Windows-only [Model Context Protocol](https://modelcontextprotocol.io/) server for WaveMetrics Igor Pro. It uses a local stdio connection and Igor's COM automation interface; it does not open a network listener or require a wiki service.

## Requirements

- Windows with Igor Pro installed and available to the current user through COM. Igor Pro 10 is the recommended target; the server currently enforces a minimum Igor version of 6.0, though older releases may differ in supported commands.
- Python 3.10 or newer.
- An MCP host that supports launching a local stdio server, such as Codex or another compatible client.

## Install

Clone the repository and create an environment with the project's dependencies. Use the Python launcher selector for an installed Python 3.10+ version (`py -3.10` below; adjust the minor version if needed):

```powershell
git clone https://github.com/gabzsi/igormcp.git
Set-Location igormcp
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
```

The package declares its dependencies in `pyproject.toml` (`mcp` 1.x and `pywin32` on Windows) and installs the `igor-pro-mcp` entry point. The MCP host should launch it as a local stdio process using the environment's Python; the server is not intended to be opened in a browser or exposed as a network service.

## Configure an MCP host

Use absolute paths in the host configuration. Replace `C:\path\to\igormcp` with the checkout location. The server uses `-B` to avoid writing Python bytecode into the checkout.

For Codex, add an entry like this to `config.toml`:

```toml
[mcp_servers.igor-pro]
command = "C:\\path\\to\\igormcp\\.venv\\Scripts\\python.exe"
args = ["-B", "-m", "igor_mcp_server"]
cwd = "C:\\path\\to\\igormcp"
env = { IGOR_SESSION_DIR = "C:\\path\\to\\igormcp\\sessions", PYTHONDONTWRITEBYTECODE = "1" }
```

For hosts that use the common JSON stdio configuration shape:

```json
{
  "mcpServers": {
    "igor-pro": {
      "command": "C:\\path\\to\\igormcp\\.venv\\Scripts\\python.exe",
      "args": ["-B", "-m", "igor_mcp_server"],
      "cwd": "C:\\path\\to\\igormcp",
      "env": {
        "IGOR_SESSION_DIR": "C:\\path\\to\\igormcp\\sessions",
        "PYTHONDONTWRITEBYTECODE": "1"
      }
    }
  }
}
```

Configuration locations and field names vary by host; use its documentation if its format differs. Set `IGOR_PRO_EXE` to the full path of `Igor64.exe` or `Igor.exe` only when automatic discovery does not find the intended installation. `ensure_igor` uses that setting or the maintained Igor Pro 10 default path. `IGOR_SESSION_DIR` is optional; without it, session data goes under `%LOCALAPPDATA%\igor-mcp\sessions`. `IGOR_KNOWLEDGE_DIR` can point to an existing compatible knowledge directory containing `manifest.json` and the referenced Markdown files. The project does not provide an importer for external manuals.

Restart or reconnect the MCP host after editing its configuration or updating the server. A new host initialization is needed to receive changed server instructions.

## Available tools

| Area | Tools |
|---|---|
| Igor control | `ensure_igor`, `igor_status`, `execute_igor`, `open_igor_experiment`, `import_delimited_text` |
| Data inspection | `list_waves`, `get_wave_data`, `read_variable`, `list_procedures` |
| Dialogs and compiler diagnostics | `get_igor_dialogs`, `dismiss_igor_dialogs`, `get_igor_compilation_errors`, `dismiss_igor_compilation_errors` |
| Knowledge | `query_igor_help`, `search_igor_knowledge`, `read_igor_document` |
| Session memory | `search_session_memory`, `read_session_memory`, `capture_session` |

`igor_status` is a read-only diagnostic and does not start Igor. Use `ensure_igor` to attach or start Igor. `open_igor_experiment` requires an already-running Igor instance, accepts an existing `.pxp` or `.pxt`, and follows Igor's normal Open behavior; it never saves, discards, or dismisses unsaved-work prompts. Review dialog or compiler diagnostics before using a dismissal tool.

## Working safely

- Prefer direct `execute_igor` commands for ordinary calculations, plots, fitting, inspection, and short command sequences. Use an IPF procedure for ODE simulations, instrument control, repeated loops, substantial multi-dataset work, or reusable routines.
- Preserve source data. Create uniquely named run folders and derived waves; omit Igor overwrite flags such as `/O` unless the user explicitly asks to replace data.
- The server serializes commands and bounds its response wait. A timeout means the command outcome may be unknown; inspect Igor state and diagnostics before retrying.
- The server requests that a newly started Igor window be shown without activation and does not intentionally restore or foreground an existing window. Igor, Windows, or another application may still affect focus according to their own behavior.
- The MCP host can invoke Igor commands through this server. Connect it only to a host and workspace you trust; do not pass passwords, tokens, or other secrets as command text.

The server's startup instructions include a compact guide to common waves, scaling, plotting, labels, colors, and folder patterns. For task-specific procedures or prior work, search `search_session_memory` and read relevant matches. Use `query_igor_help` for exact manual syntax and `search_igor_knowledge` for curated workflows; use `read_igor_document` to read a search result.

## Knowledge and third-party materials

The public source distribution contains the eight curated project guides. It does not include WaveMetrics' official manual corpus or its image assets because no redistribution permission is included with this project. As a result, `search_igor_knowledge` works with the bundled guides, while `query_igor_help` returns no manual results unless an authorized compatible knowledge directory is configured with `IGOR_KNOWLEDGE_DIR`. This repository does not provide a manual importer. Official Igor documentation is available from [WaveMetrics](https://www.wavemetrics.com/products/igorpro/manual) and its [online documentation](https://docs.wavemetrics.com/igorpro/).

An authorized local deployment may keep a compatible Igor help export outside this public snapshot. Do not add WaveMetrics manuals or their image assets without separate redistribution permission. See [knowledge/README.md](knowledge/README.md) for the bundled-guide inventory.

The server code and project-authored guides are released under the MIT License; see [LICENSE](LICENSE). That license does not apply to WaveMetrics documentation, Igor Pro, or other third-party materials, which remain subject to their owners' rights.

## Session memory and privacy

Automatic MCP tool-call observations are stored as bounded JSONL files under `IGOR_SESSION_DIR`. `capture_session` writes a separate concise Markdown summary when a useful outcome, preference, or workaround should persist. Logs omit bulk wave arrays and redact values under fields named like secrets, but command text and results can still contain sensitive information. Keep the session directory private and do not commit it.

The storage is local and append-only: each JSONL file is limited to 1 MB, up to 64 log files are retained, and summaries stop at 1,000 files or 50 MB. Records are not deleted automatically. When a limit is reached, archive or remove old local session files yourself before continuing.

## Troubleshooting

- **The MCP tools do not appear:** verify the host's command, absolute Python path, `cwd`, and environment; then restart/reconnect the host.
- **Igor cannot be reached:** call `ensure_igor`. If startup cannot find Igor, set `IGOR_PRO_EXE` to the installed `Igor64.exe` or `Igor.exe`. `igor_status` reports availability but does not start the application.
- **An experiment will not open:** confirm Igor is already running and the `.pxp` or `.pxt` path exists. Resolve any unsaved-work prompt in Igor itself.
- **A command times out or a dialog remains:** treat a timeout as an unknown outcome. Inspect Igor state with `list_waves`, `get_igor_dialogs`, or `get_igor_compilation_errors` before retrying or dismissing a dialog.
- **Official help search has no results:** the public distribution contains curated guides only. Configure `IGOR_KNOWLEDGE_DIR` to an existing compatible bundle that you are authorized to use; this project does not convert native Igor help files.
- **Session writes fail:** check that the configured session directory is writable and below its storage limits. Existing records are preserved when the cap is reached.

## Development checks

Run the offline test suite from the repository root:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
```

The suite uses mocks and local fixtures; live Igor testing is separate and is not needed for installation verification.
