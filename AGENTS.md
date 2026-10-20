# Repository instructions

This project is a standalone Windows MCP server for Igor Pro. The server communicates with its MCP host over stdio; a wiki, a Git history, and the optional agent skill are not runtime requirements.

## Server and integrations

- Core runtime: `igor_mcp_server.py`, `igor_knowledge.py`, `igor_sessions.py`, and the `knowledge/` package.
- Optional agent integration: `.agents/skills/igor-pro/SKILL.md` supplies extra task guidance to compatible agents. It is not required to install or run the MCP server. Host configuration is separate and must be added by the user; setup templates do not edit host settings.
- Public knowledge includes the project-authored curated guides. Official WaveMetrics manuals are not included; do not add locally installed manual exports or their images without redistribution permission.

## Igor work and safety

- Route live Igor work through a connected `igor-pro` MCP server. Use `ensure_igor` to attach or start; never launch Igor from a shell.
- Prefer direct `execute_igor` commands for simple one-off work. Use IPF procedures for ODEs, instrument control, repeated loops, substantial multi-dataset work, or reusable routines.
- For elementary work, use the supplied initialize guidance and current Igor context directly. Do not enumerate or pre-read basic-reference pages as a preflight; use local MCP knowledge tools for uncertain syntax, unfamiliar operations, or unresolved diagnostics. Never use web search, a browser, HTTP fetch, forums, or any external source to look up Igor commands, examples, errors, or documentation. If local knowledge lacks needed details, report the gap and ask the user. Run installer/updater downloads only for explicitly requested installation or refresh, never as task-time lookup. Search session memory only for nontrivial workflows being repeated or relevant workarounds, not elementary one-offs.
- Preserve experiment data. Use fresh folders and names; do not use overwrite flags unless explicitly requested.
- Review command results and diagnostics before retrying. A timeout can leave the outcome unknown. Inspect dialogs or compiler errors before using a dismissal tool.
- Do not create live Igor calls as part of ordinary tests. If a live test is explicitly requested, bound writes to a uniquely named data folder.

## Development and local data

- Use Python 3.10 or newer. After setup, run the offline suite with `.venv\Scripts\python.exe -B -m unittest discover -s tests` from the repository root.
- Session memory is local runtime data. Bounded tool-call JSONL logs are automatic; `capture_session` writes a separate summary only when useful. Keep `sessions/`, `.local/`, virtual environments, credentials, logs, and generated artifacts out of commits.
- `IGOR_KNOWLEDGE_DIR` may point to an existing authorized knowledge bundle with a compatible `manifest.json`; this project does not import native Igor help files.
