# Igor Pro MCP project instructions

This is a standalone Windows MCP server for Igor Pro. Bundled Markdown knowledge and local session memory are runtime data; no wiki or Git history is required to run the server.

## Igor tasks

- Route Igor operations through the `igor-pro` MCP server. Start or attach through `ensure_igor`; never launch Igor from a shell.
- Prefer direct `execute_igor` commands for simple calculations, plots, fitting, inspection, and short command sequences. Use an IPF procedure for ODE simulations, instrument control, batch or loop-heavy processing across multiple datasets, reusable routines, or an explicit user request. Data size alone does not call for an IPF when a built-in command handles the work simply.
- For elementary tasks, use the initialize guidance and current Igor context directly; do not enumerate or pre-read basic-reference pages. Use local MCP knowledge tools for uncertain syntax, unfamiliar operations, or unresolved diagnostics. Never use web search, a browser, HTTP fetch, or external sites to look up Igor documentation. If local knowledge lacks needed details, report the gap and ask the user. The installer/updater download is a separate installation workflow, not task-time lookup. Search session memory only for nontrivial workflows being repeated or relevant workarounds, not elementary one-offs.
- Preserve the user-chosen window state; do not force Igor into the foreground or minimize it automatically. Review command results and diagnostics before retrying. Inspect remaining dialogs with `get_igor_dialogs` or `get_igor_compilation_errors`; use explicit dismissal tools only when appropriate.

## Session memory

- MCP tool calls are written automatically as bounded JSONL logs under `sessions/`.
- Use `capture_session` to write a concise Markdown summary when a result, preference, or workaround is likely to help future work. The automatic log is call history; a summary records the useful outcome.
- Source host configurations set `IGOR_SESSION_DIR` to this repository's `sessions/`. Installed deployments default to `%LOCALAPPDATA%\igor-mcp\sessions`.
- Session files are local, Git-ignored, and never committed automatically. Do not create wiki pages or development reports unless the user asks.

## Server changes

- Run offline tests with `.venv\Scripts\python.exe -B -m unittest discover -s tests`.
- Preserve Igor experiment data and unrelated changes. Bound any requested live test to a uniquely named data folder.
