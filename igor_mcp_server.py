"""MCP server for controlled Igor Pro access through a COM instance.

This standalone stdio server attaches to or starts Igor when configured.
It prefers Execute2 with a legacy capture path for dispatches without Execute2.
Markdown knowledge is bundled; session capture is explicit and never requires Git.
"""

import asyncio
import base64
import csv
import json
import math
import os
from pathlib import Path
from queue import Empty
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from ctypes import WINFUNCTYPE, byref, create_unicode_buffer, windll
from ctypes import wintypes
from typing import Annotated, Any, Iterable, Literal, TypedDict

from pydantic import Field
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
import igor_knowledge
import igor_pipe
import igor_reports
from igor_sessions import (
    append_tool_call_record,
    capture_markdown_session,
    read_session_memory as _read_session_memory,
    search_session_memory as _search_session_memory,
)

try:
    import pythoncom
    import win32com.client
    _PYWIN32_IMPORT_ERROR = None
except Exception as _e:  # pywin32 missing or broken; tools report it clearly.
    pythoncom = None
    win32com = None
    _PYWIN32_IMPORT_ERROR = str(_e)


_SERVER_VERSION = "0.3.0"
_PROGIDS = ("IgorPro.Application", "Igor.Application")
_MIN_IGOR_VERSION = 6.0
_WAVE_TYPE_SELECTOR_VERSION = 6.1
_DATA_FOLDER_WAVELIST_VERSION = 9.0
_MAX_COMMAND_BYTES = 2_400
_MAX_WAVE_POINTS = 10_000
_MAX_LISTED_WAVES = 10_000
_MAX_OUTPUT_BYTES = 1_000_000
_MAX_IGOR_OBJECT_NAME_LENGTH = 31
_HELPER_RANDOM_HEX_LENGTH = 8
_MAX_IMPORT_FILE_BYTES = 10_000_000
_MAX_IMPORT_COLUMNS = 256
_MAX_IMPORT_ROWS = 1_000_000
_MAX_IMPORT_PREFIX_LENGTH = (
    _MAX_IGOR_OBJECT_NAME_LENGTH - len(str(_MAX_IMPORT_COLUMNS - 1))
)
_PORTABLE_IGOR_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_IMPORT_DELIMITERS = {"comma": ",", "tab": "\t", "semicolon": ";"}
_IMPORT_EXTENSIONS = frozenset({".csv", ".tsv", ".txt"})
_TMP_READ_ATTEMPTS = 10
_TMP_READ_DELAY_SECONDS = 0.05
_EXECUTION_STATUS_MARKER = "<<IGOR-MCP-STATUS>>"
_WM_CLOSE = 0x0010
_SW_HIDE = 0
_SW_SHOWNOACTIVATE = 4
_GW_OWNER = 4
_GWL_EXSTYLE = -20
_WS_EX_DLGMODALFRAME = 0x00000001
_DIALOG_WINDOW_CLASSES = frozenset({"#32770"})
_DDE_HELPER_WINDOW_CLASSES = frozenset(
    {"OleDdeWndClass", "DDEMLEvent", "DDEMLMom"}
)
_DDE_HELPER_WINDOW_TITLES = frozenset({"dde server window"})
_NON_MAIN_HELPER_WINDOW_CLASSES = frozenset(
    {
        "GDI+ Hook Window Class",
        "IME",
        "MSCTFIME UI",
        "OleMainThreadWndClass",
        "CicMarshalWndClass",
        "tooltips_class32",
        "ComboLBox",
    }
)
_NON_MAIN_HELPER_TITLE_SUBSTRINGS = (
    "gdi+ window",
    "qtreeview",
    "default ime",
    "msctfime",
    "olemainthreadwnd",
)
_IGOR_EXECUTABLE_NAMES = frozenset({"igor.exe", "igor64.exe"})
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_UI_AUTOMATION_TIMEOUT_SECONDS = 4
_COMPILER_DIALOG_TITLES = frozenset({"Function Compilation Error", "Procedure Scan Error"})
_AUTOMATABLE_DIALOG_TITLES = frozenset({
    "Choose a procedure",
    "Looking for a Procedure File",
})
_SAFE_DIALOG_BUTTON_NAMES = ("Cancel", "Close")
_IGOR_EXECUTABLE_ENV = "IGOR_PRO_EXE"
_DEFAULT_IGOR_EXECUTABLE = Path(
    r"C:\Program Files\WaveMetrics\Igor Pro 10 Folder\IgorBinaries_x64\Igor64.exe"
)
_IGOR_STARTUP_TIMEOUT_SECONDS = 20.0
_IGOR_STARTUP_POLL_SECONDS = 0.25
_IGOR_COMMAND_TIMEOUT_SECONDS = 25.0
_IGOR_REPORT_TIMEOUT_SECONDS = 120.0
_COMMAND_WORKER_LOCK_WAIT_SECONDS = 1.0
_IGOR_TRANSPORT_ENV = "IGOR_TRANSPORT"
_VALID_TRANSPORTS = frozenset({"auto", "pipe", "com"})
_IGOR_TOOL_PROFILE_ENV = "IGOR_TOOL_PROFILE"
_VALID_TOOL_PROFILES = frozenset({"full", "core"})
_OPTIONAL_REPORT_TOOL_NAMES = frozenset(
    {"create_html_report", "open_report_graph", "apply_html_report"}
)
_MIN_USER_TIMEOUT_SECONDS = 1.0
_MAX_USER_TIMEOUT_SECONDS = 600.0
_PIPE_WORKER_TIMEOUT_GRACE_SECONDS = 3.0
_MAX_CAPTURE_IMAGE_BYTES = 20_000_000
_CAPTURE_WINDOW_RENDER_MODES = frozenset(
    {"auto", "screen", "screenGraphOnly", "export"}
)
_SAFE_WINDOW_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_#]+$")
_WINDOW_TYPE_BITS: dict[str, int] = {
    "graph": 1,
    "table": 2,
    "layout": 4,
    "notebook": 16,
    "panel": 64,
    "gizmo": 65536,
}
_ALL_WINDOW_TYPES_MASK = sum(_WINDOW_TYPE_BITS.values())  # 65623
_IGOR_LOAD_EXPERIMENT_FLAGS = 0
_IGOR_LOAD_TYPE_OPEN = 2
_EXPERIMENT_FILE_EXTENSIONS = frozenset({".pxp", ".pxt"})
_SOURCE_PATH = str(Path(__file__).resolve())

_lock = threading.Lock()
_command_worker_lock = threading.Lock()
_command_start_marker = threading.local()
_capture_mode = "temp-file capture"


def _is_error_result(tool_name: str, result: object) -> bool:
    """Recognize returned failures while retaining expected diagnostic status."""

    if isinstance(result, CallToolResult) and result.isError:
        return True
    if tool_name == "igor_status":
        return False
    structured = _structured_tool_result(result)
    if structured is None:
        return False
    if structured.get("ok") is False:
        return True
    error = structured.get("error")
    return isinstance(error, str) and bool(error)


def _structured_tool_result(result: object) -> dict[str, object] | None:
    if isinstance(result, CallToolResult):
        if isinstance(result.structuredContent, dict):
            return result.structuredContent
        return None
    if isinstance(result, dict):
        return result
    if (
        isinstance(result, tuple)
        and len(result) == 2
        and isinstance(result[1], dict)
    ):
        return result[1]
    return None


def _record_tool_call_best_effort(
    tool_name: str,
    arguments: dict[str, object],
    *,
    result: object = None,
    error: object = None,
    is_error: bool = False,
    status: str = "returned",
) -> None:
    """Write an observation without changing the outcome of the Igor call."""

    try:
        append_tool_call_record(
            tool_name,
            arguments,
            result=result,
            error=error,
            is_error=is_error,
            status=status,
        )
    except Exception as exc:
        try:
            print(
                f"igor-mcp: session memory write failed; tool outcome preserved: {exc}",
                file=sys.stderr,
                flush=True,
            )
        except Exception:
            pass


def _tool_profile() -> str:
    """Return configured tool profile: 'full' (default) or 'core'."""
    profile = os.environ.get(_IGOR_TOOL_PROFILE_ENV, "full").strip().lower()
    return profile if profile in _VALID_TOOL_PROFILES else "full"


class IgorFastMCP(FastMCP):
    """FastMCP wrapper for best-effort call memory, tool profiles, and structured tool errors."""

    async def list_tools(self):
        tools = await super().list_tools()
        if _tool_profile() == "core":
            return [
                tool for tool in tools if tool.name not in _OPTIONAL_REPORT_TOOL_NAMES
            ]
        return tools

    async def call_tool(self, name: str, arguments: dict[str, object]):
        try:
            result = await super().call_tool(name, arguments)
        except BaseException as exc:
            status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "raised"
            _record_tool_call_best_effort(
                name,
                arguments,
                error={"type": type(exc).__name__, "message": str(exc)},
                is_error=True,
                status=status,
            )
            raise

        structured_result = _structured_tool_result(result)
        is_error = _is_error_result(name, result)
        _record_tool_call_best_effort(
            name,
            arguments,
            result=structured_result if structured_result is not None else result,
            is_error=is_error,
        )
        if isinstance(result, CallToolResult):
            return result
        if is_error and structured_result is not None:
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps(
                            structured_result,
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                        ),
                    )
                ],
                structuredContent=structured_result,
                isError=True,
            )
        return result


mcp = IgorFastMCP(
    "igor-pro",
    instructions=(
        "Task routing: default to direct Igor commands for a simple one-off task. "
        "A short chain such as generating a wave and fitting it is still a command "
        "task; use execute_igor and do not generate an IPF procedure. Use an IPF "
        "procedure for ODEs, instrument control, substantial batch or loop-heavy "
        "processing across datasets, reusable routines, or an explicit request. "
        "A large wave handled by one built-in command does not need a procedure. "
        "Prefer call_function for direct typed return values over Print; use "
        "capture_window to visually verify plots, compile_procedures for silent "
        ".ipf reload/compile, and abort_igor to cancel hung server commands. "
        "For complex procedure work, inspect data first, write one bounded "
        "procedure, load/compile it, and verify outputs without blindly retrying "
        "after an unknown result. "
        "Never search the filesystem for Igor yourself: use ensure_igor to attach "
        "or start Igor via IGOR_PRO_EXE or the Igor Pro 10 default path. Do not "
        "use igor_status for startup; it is read-only and never starts Igor. "
        "Use open_igor_experiment for existing .pxp/.pxt files via normal Igor "
        "File > Open behavior; Igor must be running and unsaved-work prompts stay "
        "with the user. "
        "Preserve Igor's current window state. When starting a new Igor process, "
        "show its main window without activation. Never restore or foreground an "
        "existing window; if the user minimizes Igor, leave it minimized. Do not "
        "use DoWindow/F unless requested. Inspect dialogs with get_igor_dialogs "
        "and compiler diagnostics with get_igor_compilation_errors; the server "
        "does not dismiss dialogs automatically. Review diagnostics before "
        "dismissing. "
        "Queued COM compilation is asynchronous: use "
        'Execute/P/Q/Z "COMPILEPROCEDURES " (trailing space), then check '
        "get_igor_compilation_errors before dismissing, as dialogs can appear "
        "after execute_igor returns. "
        "Knowledge tiers: these initialize instructions are the compact everyday "
        "Igor core supplied on each MCP initialization. Use them directly for "
        "routine wave creation, assignment, scaling, folders, calculations, "
        "graphs, and styling; do not search manuals for familiar basic tasks. "
        "Official docs stay behind on-demand tools; core-reference pages are "
        "still read through a tool when needed. Use local query_igor_help or "
        "curated search/read tools only when syntax is uncertain, an operation "
        "unfamiliar, or diagnostics leave a gap. Do not enumerate or pre-read "
        "basic-reference pages as a preflight. Never use web search, a browser, "
        "HTTP fetch, forums, or any external source to look up Igor docs; if "
        "local knowledge lacks details, report the gap and ask the user. Run "
        "installer/updater downloads only for an explicitly requested "
        "installation or refresh, never task-time lookup. These instructions "
        "do not imply permanent model or host caching. Use read_igor_document "
        "to read a search result in full. "
        "Tool calls are logged as bounded local observations (bulk wave data "
        "omitted). Use search_session_memory and read_session_memory only for "
        "a nontrivial workflow being repeated or a relevant workaround; skip "
        "elementary one-offs. Use capture_session only for a Markdown summary "
        "worth preserving. Use execute_igor only for user-requested actions. "
        "Everyday Igor core: create a fresh, unique data folder and wave/graph "
        "names; omit /O so collisions fail, and preserve source waves. Waves "
        "hold point data plus dimension scales and units; p is point index and "
        "x is scaled X coordinate. For regular sampling, make the wave, set X "
        "origin/step/units, then assign wave = expression. Example (run in order "
        "and check success): "
        "NewDataFolder/S root:Run_20261020_01; "
        "Make/N=1000 signal; SetScale/P x, 0, 0.1, \"ms\", signal; "
        "signal = exp(-x/2); Display/N=RunPlot_20261020_01 signal; "
        "Label/W=RunPlot_20261020_01 left, \"Signal\"; "
        "Label/W=RunPlot_20261020_01 bottom, \"Time (ms)\"; "
        "ModifyGraph/W=RunPlot_20261020_01 rgb(signal)=(0,0,65535). "
        "NewDataFolder/S creates/selects the folder; Make/N creates points; "
        "SetScale/P sets X origin, step, units; Redimension/N=n resizes 1-D "
        "waves (check before shrinking). Display uses wave scaling; for "
        "irregular X use Display yWave vs xWave. Add traces with "
        "AppendToGraph/W=graphName waveName; label with Label/W=graphName "
        "left/bottom; set RGB with ModifyGraph/W=graphName rgb(trace)=(r,g,b), "
        "channels range 0-65535. "
        "Use import_delimited_text for numeric CSV/TSV/TXT; LoadWave for other "
        "formats. WaveStats/Q wave sets V_avg/V_sdev. CurveFit/Q line, wave "
        "writes W_coef; use a fresh folder and verify fit quality. Smooth, "
        "Integrate, Differentiate are built-ins; verify flags when needed. "
        "SaveExperiment as \"Run_20261020_01.pxp\" overwrites matching files, "
        "so pick a new name unless explicit. Consult local manuals for "
        "uncertain flags or version-specific syntax. HTML reports and "
        "apply-back require user requests."
    ),
)
mcp._mcp_server.version = _SERVER_VERSION

_READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_MUTATING_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)
_STARTUP_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


class VariableResult(TypedDict):
    name: str
    type: str
    value: float | str


class WaveDataResult(TypedDict, total=False):
    name: str
    npnts: int
    dimensions: int
    wave_type_code: int
    complex: bool
    truncated: bool
    values: list[float | str | dict[str, float | str]] | list[list[float | str]]
    # These fields apply to matrix waves.  FastMCP serializes omitted
    # TypedDict fields as null in some clients, so the public schema accepts
    # null explicitly for 1D results rather than advertising a string/number
    # that the server does not return for those waves.
    rows: int | None
    columns: int | None
    x_offset: float | None
    x_delta: float | None
    y_offset: float | None
    y_delta: float | None
    x_units: str | None
    y_units: str | None


class _IgorStatusRequired(TypedDict):
    com_reachable: bool
    compatible: bool
    igor_version: str | None
    minimum_igor_version: str
    current_experiment: str | None
    capture_mode: str
    source_path: str
    portable_object_name_limit: int
    error: str | None


class IgorStatusResult(_IgorStatusRequired, total=False):
    transport: str | None
    pipe_reachable: bool | None
    pipe_name: str | None
    pipe_capabilities: list[str] | None


class _ExecuteIgorRequired(TypedDict):
    ok: bool
    output: str
    error: str | None
    warning: str | None
    command_started: bool
    command_completed: bool
    outcome_known: bool
    preexisting_dialog_count: int
    dialog_detected: bool
    dialogs_open: int
    dialog_titles: list[str]
    dialog_details: list[dict[str, object]]
    compilation_errors: list[dict[str, object]]


class ExecuteIgorResult(_ExecuteIgorRequired, total=False):
    history: str | None
    results: str | None
    elapsed_seconds: float | None
    transport: str | None


class _EnsureIgorRequired(TypedDict):
    com_reachable: bool
    igor_version: str
    current_experiment: str
    executable_path: str | None
    source_path: str


class EnsureIgorResult(_EnsureIgorRequired, total=False):
    transport: str | None
    pipe_reachable: bool | None
    pipe_name: str | None


class OpenIgorExperimentResult(TypedDict):
    ok: bool
    file_path: str
    current_experiment: str | None
    error: str | None
    load_started: bool
    load_completed: bool
    outcome_known: bool
    dialogs_open: int
    dialog_titles: list[str]
    dialog_details: list[dict[str, object]]
    compilation_errors: list[dict[str, object]]


class DelimitedImportResult(TypedDict):
    source_path: str
    folder: str
    prefix: str
    rows: int
    columns: int
    waves: list[str]
    headers: list[str]
    portable_object_name_limit: int


class IgorError(Exception):
    """A tool-execution error that the MCP client can present to the model."""


def _window_class(hwnd: int) -> str:
    buffer = create_unicode_buffer(256)
    length = windll.user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value if length else ""


def _window_owner(hwnd: int) -> int:
    # GetWindow(GW_OWNER) works for native and Qt top-level modal windows.
    owner = windll.user32.GetWindow(hwnd, _GW_OWNER)
    if not owner:
        owner = windll.user32.GetParent(hwnd)
    return int(owner or 0)


def _window_process_id(hwnd: int) -> int:
    process_id = wintypes.DWORD()
    windll.user32.GetWindowThreadProcessId(hwnd, byref(process_id))
    return int(process_id.value)


def _process_image_path(process_id: int) -> str:
    if not process_id:
        return ""
    handle = windll.kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, process_id
    )
    if not handle:
        return ""
    try:
        buffer = create_unicode_buffer(1024)
        length = wintypes.DWORD(len(buffer))
        if not windll.kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, byref(length)
        ):
            return ""
        return buffer.value
    finally:
        windll.kernel32.CloseHandle(handle)


def _window_text(hwnd: int) -> str:
    """Return a Win32 window's title/control text without accessing other data."""
    length = windll.user32.GetWindowTextLengthW(hwnd)
    if length == 0:
        return ""
    buffer = create_unicode_buffer(length + 1)
    windll.user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value.strip()


def _igor_dialog_window(hwnd: int) -> dict[str, object] | None:
    """Return metadata for a visible Igor-owned modal/dialog window."""
    if not windll.user32.IsWindowVisible(hwnd):
        return None
    process_id = _window_process_id(hwnd)
    process_path = _process_image_path(process_id)
    if Path(process_path).name.lower() not in _IGOR_EXECUTABLE_NAMES:
        return None
    title = _window_text(hwnd)
    if not title:
        return None
    class_name = _window_class(hwnd)
    owner_hwnd = _window_owner(hwnd)
    exstyle = int(windll.user32.GetWindowLongW(hwnd, _GWL_EXSTYLE))
    is_dialog = (
        title in _COMPILER_DIALOG_TITLES
        or owner_hwnd != 0
        or class_name in _DIALOG_WINDOW_CLASSES
        or bool(exstyle & _WS_EX_DLGMODALFRAME)
    )
    if not is_dialog:
        return None
    return {
        "hwnd": int(hwnd),
        "title": title,
        "class_name": class_name,
        "owner_hwnd": owner_hwnd,
        "process_id": process_id,
        "process_path": process_path,
        "modal": bool(owner_hwnd or exstyle & _WS_EX_DLGMODALFRAME),
    }


def _igor_dialogs() -> list[dict[str, object]]:
    """Find visible modal/dialog windows belonging to Igor Pro."""
    dialogs: list[dict[str, object]] = []
    enum_proc_type = WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @enum_proc_type
    def top_level_callback(hwnd, _lparam):
        dialog = _igor_dialog_window(int(hwnd))
        if dialog is not None:
            dialogs.append(dialog)
        return True

    windll.user32.EnumWindows(top_level_callback, 0)
    return dialogs


def _uia_dismiss_automatable_dialog(hwnd: int, title: str) -> bool:
    """Invoke only a safe Cancel/Close button on a known Igor chooser."""
    if title not in _AUTOMATABLE_DIALOG_TITLES:
        return False
    script = f"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$dialog = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd})
if ($null -eq $dialog -or $dialog.Current.Name -ne '{title}') {{ exit 2 }}
$condition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)
$buttons = $dialog.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
foreach ($button in $buttons) {{
    if ($button.Current.Name -in @('Cancel', 'Close')) {{
        $pattern = $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        ([System.Windows.Automation.InvokePattern]$pattern).Invoke()
        exit 0
    }}
}}
exit 3
"""
    try:
        _run_ui_automation(script, purpose="closing an Igor chooser")
    except IgorError:
        return False
    return True


def _wait_for_dialog_to_close(hwnd: int) -> bool:
    """Wait at most 0.5 seconds for one dialog to disappear."""
    for _ in range(10):
        if not windll.user32.IsWindow(hwnd) or not windll.user32.IsWindowVisible(hwnd):
            return True
        time.sleep(0.05)
    return False


def _dismiss_igor_dialogs(dialogs: list[dict[str, object]]) -> dict[str, int]:
    """Close Igor dialogs using safe chooser buttons and WM_CLOSE fallback."""
    result = {
        "close_requested": 0,
        "dismissed": 0,
        "uia_requested": 0,
        "wm_close_requested": 0,
        "close_failed": 0,
    }
    for dialog in dialogs:
        hwnd = int(dialog["hwnd"])
        if not windll.user32.IsWindow(hwnd):
            continue
        requested = False
        if _uia_dismiss_automatable_dialog(hwnd, str(dialog.get("title", ""))):
            result["uia_requested"] += 1
            requested = True
            result["close_requested"] += 1
            if _wait_for_dialog_to_close(hwnd):
                result["dismissed"] += 1
                continue

        wm_close_requested = bool(_request_close_igor_dialogs([dialog]))
        if wm_close_requested:
            result["wm_close_requested"] += 1
            result["close_requested"] += 1
            requested = True
        if not requested:
            result["close_failed"] += 1
            continue
        if _wait_for_dialog_to_close(hwnd):
            result["dismissed"] += 1
        else:
            result["close_failed"] += 1
    return result


def _request_close_igor_dialogs(dialogs: list[dict[str, object]]) -> int:
    """Post a normal close request without waiting for Igor's UI thread."""
    requested = 0
    for dialog in dialogs:
        hwnd = int(dialog["hwnd"])
        if windll.user32.IsWindow(hwnd) and windll.user32.PostMessageW(
            hwnd, _WM_CLOSE, 0, 0
        ):
            requested += 1
    return requested


def _dialog_blocking_message(dialogs: list[dict[str, object]], phase: str) -> str:
    details = "; ".join(
        f"{dialog['title']!r} (hwnd={dialog['hwnd']})" for dialog in dialogs
    )
    return (
        f"Igor dialog(s) were detected {phase}: {details}. "
        "No dialog was dismissed. Inspect with get_igor_dialogs and, for compiler "
        "errors, get_igor_compilation_errors. After reviewing the result, use an "
        "explicit dismiss tool only when appropriate."
    )


def _watch_for_igor_dialogs(
    stop_event: threading.Event,
    detected: dict[int, dict[str, object]],
    detected_lock: threading.Lock,
    compilation_errors: dict[int, dict[str, object]],
) -> None:
    """Record dialogs and compiler diagnostics without changing Igor's UI state."""
    while not stop_event.wait(0.05):
        try:
            dialogs = _igor_dialogs()
            if not dialogs:
                continue
            compiler_dialogs = [
                dialog for dialog in dialogs
                if str(dialog.get("title", "")) in _COMPILER_DIALOG_TITLES
            ]
            with detected_lock:
                unread_compiler_dialogs = [
                    dialog for dialog in compiler_dialogs
                    if int(dialog["hwnd"]) not in compilation_errors
                ]
            captured_errors = _capture_compilation_errors(
                unread_compiler_dialogs, phase="during_command"
            )
            with detected_lock:
                for dialog in dialogs:
                    detected[int(dialog["hwnd"])] = dialog
                for error in captured_errors:
                    compilation_errors[int(error["hwnd"])] = error
        except Exception:
            # Monitoring must never take down the command worker.
            continue


def _run_ui_automation(script: str, *, purpose: str = "Windows UI Automation") -> str:
    """Run a fixed PowerShell UI-Automation script with a short timeout."""
    encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded_script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_UI_AUTOMATION_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise IgorError(
            f"Timed out while {purpose}; Igor may be busy"
        ) from exc
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise IgorError(f"{purpose.capitalize()} failed: {detail}")
    return completed.stdout.strip()


def _uia_compilation_error_details(hwnd: int, title: str) -> dict[str, object]:
    """Read an Igor Qt compiler dialog through Microsoft UI Automation."""
    # Qt controls do not have child HWNDs, but Igor exposes stable automation IDs.
    script = f"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$dialog = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd})
if ($null -eq $dialog -or $dialog.Current.Name -ne '{title}') {{ exit 2 }}
$all = $dialog.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
$message = ''
$location = ''
$source = ''
$buttons = @()
foreach ($element in $all) {{
    $id = $element.Current.AutomationId
    if ($id -eq 'IgorAppObject.MacroErrorDialog.ErrorMessageLabel') {{ $message = $element.Current.Name }}
    elseif ($id -eq 'IgorAppObject.MacroErrorDialog.ErrorWhereLabel') {{ $location = $element.Current.Name }}
    elseif ($id -eq 'IgorAppObject.MacroErrorDialog.ErrorLineEdit') {{
        $pattern = $null
        if ($element.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$pattern)) {{
            $source = ([System.Windows.Automation.TextPattern]$pattern).DocumentRange.GetText(-1).Trim()
        }}
    }}
    elseif ($element.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button) {{
        if ($element.Current.Name) {{ $buttons += $element.Current.Name }}
    }}
}}
[PSCustomObject]@{{ hwnd = {hwnd}; title = $dialog.Current.Name; message = $message; location = $location; source_line = $source; buttons = $buttons }} | ConvertTo-Json -Compress
"""
    output = _run_ui_automation(script)
    if not output:
        raise IgorError("Windows accessibility returned no compilation-dialog details")
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise IgorError("Windows accessibility returned malformed dialog details") from exc


def _compilation_error_dialogs() -> list[dict[str, object]]:
    """Find visible Igor compiler dialogs and read their text."""
    dialogs: list[dict[str, object]] = []
    enum_proc_type = WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @enum_proc_type
    def top_level_callback(hwnd, _lparam):
        if not windll.user32.IsWindowVisible(hwnd):
            return True
        title = _window_text(hwnd)
        if title not in _COMPILER_DIALOG_TITLES:
            return True

        try:
            dialogs.append(_uia_compilation_error_details(int(hwnd), title))
        except IgorError as exc:
            # Retain a minimal, safe result if UI Automation is unavailable.
            dialogs.append({"hwnd": int(hwnd), "title": title, "read_error": str(exc)})
        return True

    windll.user32.EnumWindows(top_level_callback, 0)
    return dialogs


def _capture_compilation_errors(
    dialogs: list[dict[str, object]], *, phase: str
) -> list[dict[str, object]]:
    """Read compiler dialogs before a caller dismisses them."""
    errors: list[dict[str, object]] = []
    for dialog in dialogs:
        title = str(dialog.get("title", ""))
        if title not in _COMPILER_DIALOG_TITLES:
            continue
        hwnd = int(dialog["hwnd"])
        try:
            details = _uia_compilation_error_details(hwnd, title)
        except IgorError as exc:
            details = {"hwnd": hwnd, "title": title, "read_error": str(exc)}
        details["phase"] = phase
        errors.append(details)
    return errors


def _dismiss_compilation_error_dialog(dialog: dict[str, object]) -> bool:
    """Use the dialog's own Quit Compile button; fall back to closing it."""
    hwnd = int(dialog["hwnd"])
    script = f"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$dialog = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd})
if ($null -eq $dialog -or $dialog.Current.Name -notin @('Function Compilation Error', 'Procedure Scan Error')) {{ exit 2 }}
$condition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'IgorAppObject.MacroErrorDialog.QuitCompileButton')
$button = $dialog.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $condition)
if ($null -eq $button) {{ exit 3 }}
$pattern = $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
([System.Windows.Automation.InvokePattern]$pattern).Invoke()
"""
    try:
        _run_ui_automation(script)
    except IgorError:
        # PostMessage does not wait on a potentially unresponsive Igor UI thread.
        if (
            not windll.user32.IsWindow(hwnd)
            or _window_text(hwnd) not in _COMPILER_DIALOG_TITLES
        ):
            return False
        return bool(windll.user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0))
    return True


def _igor_escape(value: str) -> str:
    """Escape a Python string for an Igor string literal, rejecting control breaks."""
    if "\x00" in value or "\r" in value or "\n" in value:
        raise IgorError("Igor string literal cannot contain null bytes or newlines")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _helper_name(prefix: str) -> str:
    """Return a unique identifier that fits Igor Pro 6's 31-character limit."""
    max_prefix_length = (
        _MAX_IGOR_OBJECT_NAME_LENGTH - _HELPER_RANDOM_HEX_LENGTH - 1
    )
    if (
        not isinstance(prefix, str)
        or not _PORTABLE_IGOR_NAME_PATTERN.fullmatch(prefix)
        or len(prefix) > max_prefix_length
    ):
        raise ValueError(
            "helper prefix must be a portable Igor identifier no longer than "
            f"{max_prefix_length} characters"
        )
    name = f"{prefix}_{uuid.uuid4().hex[:_HELPER_RANDOM_HEX_LENGTH]}"
    if len(name) > _MAX_IGOR_OBJECT_NAME_LENGTH:  # Defensive invariant.
        raise AssertionError("generated Igor helper name exceeds the portable limit")
    return name


def _validate_portable_igor_name(
    name: str, *, label: str, max_length: int = _MAX_IGOR_OBJECT_NAME_LENGTH
) -> str:
    """Validate a conservative Igor identifier usable in Igor Pro 6 through 10."""
    if not isinstance(name, str) or not name:
        raise IgorError(f"{label} must be a non-empty string")
    if len(name) > max_length:
        raise IgorError(
            f"{label} must be no longer than {max_length} characters for "
            "Igor Pro 6 compatibility"
        )
    if not _PORTABLE_IGOR_NAME_PATTERN.fullmatch(name):
        raise IgorError(
            f"{label} must start with a letter and contain only letters, "
            "digits, and underscores for Igor Pro 6 compatibility"
        )
    return name


def _validate_portable_data_folder(folder: str) -> str:
    """Validate a fully qualified data-folder path with portable components."""
    if not isinstance(folder, str) or not folder.startswith("root:"):
        raise IgorError("folder must be a fully qualified Igor path starting with root:")
    if not folder.endswith(":"):
        raise IgorError("folder must end with a colon, for example root:imports:")
    components = [component for component in folder[5:-1].split(":") if component]
    reconstructed = "root:" + "".join(f"{component}:" for component in components)
    if reconstructed != folder:
        raise IgorError("folder contains an empty or malformed path component")
    for component in components:
        _validate_portable_igor_name(component, label="data-folder component")
    return folder


def _resolve_import_path(file_path: str) -> Path:
    """Resolve and bound a user-selected local delimited-text file."""
    if not isinstance(file_path, str) or not file_path.strip():
        raise IgorError("file_path must be a non-empty absolute path")
    candidate = Path(file_path)
    if not candidate.is_absolute():
        raise IgorError("file_path must be absolute")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise IgorError(f"Import file does not exist or is inaccessible: {file_path}") from exc
    if not resolved.is_file():
        raise IgorError(f"Import path is not a file: {resolved}")
    if resolved.suffix.lower() not in _IMPORT_EXTENSIONS:
        allowed = ", ".join(sorted(_IMPORT_EXTENSIONS))
        raise IgorError(f"Import file must use one of these extensions: {allowed}")
    size = resolved.stat().st_size
    if size > _MAX_IMPORT_FILE_BYTES:
        raise IgorError(
            f"Import file is {size:,} bytes; the limit is "
            f"{_MAX_IMPORT_FILE_BYTES:,} bytes"
        )
    return resolved


def _read_numeric_delimited_file(
    path: Path, *, delimiter: str, has_header: bool
) -> tuple[list[str], list[list[float]]]:
    """Read a bounded rectangular numeric table and return headers plus rows."""
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise IgorError(f"Could not open import file: {path}") from exc
    with handle:
        reader = csv.reader(handle, delimiter=delimiter)
        records = [
            (line_number, [cell.strip() for cell in row])
            for line_number, row in enumerate(reader, start=1)
            if row and any(cell.strip() for cell in row)
        ]
    if not records:
        raise IgorError("Import file contains no non-empty rows")

    headers: list[str] = []
    if has_header:
        _, headers = records.pop(0)
        if not records:
            raise IgorError("Import file contains a header but no data rows")

    column_count = len(records[0][1])
    if not 1 <= column_count <= _MAX_IMPORT_COLUMNS:
        raise IgorError(
            f"Import file has {column_count} columns; the supported range is "
            f"1 through {_MAX_IMPORT_COLUMNS}"
        )
    if has_header and len(headers) != column_count:
        raise IgorError(
            f"Header has {len(headers)} columns but data has {column_count}"
        )
    if len(records) > _MAX_IMPORT_ROWS:
        raise IgorError(
            f"Import file has {len(records):,} rows; the limit is "
            f"{_MAX_IMPORT_ROWS:,}"
        )

    numeric_rows: list[list[float]] = []
    for line_number, row in records:
        if len(row) != column_count:
            raise IgorError(
                f"Import row {line_number} has {len(row)} columns; expected "
                f"{column_count}"
            )
        numeric_row: list[float] = []
        for column_index, cell in enumerate(row, start=1):
            try:
                numeric_row.append(float(cell))
            except ValueError as exc:
                raise IgorError(
                    f"Import value at row {line_number}, column {column_index} "
                    f"is not numeric: {cell!r}"
                ) from exc
        numeric_rows.append(numeric_row)

    if not has_header:
        headers = [f"column_{index}" for index in range(column_count)]
    return headers, numeric_rows


@contextmanager
def _normalized_import_file(rows: list[list[float]], delimiter: str):
    """Write validated numeric rows to a short-lived headerless UTF-8 file."""
    descriptor, path = tempfile.mkstemp(prefix="igor_mcp_import_", suffix=".txt")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter=delimiter, lineterminator="\n")
            for row in rows:
                writer.writerow(
                    "NaN" if math.isnan(value) else
                    "Inf" if value == math.inf else
                    "-Inf" if value == -math.inf else
                    format(value, ".17g")
                    for value in row
                )
        yield path
    finally:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            pass


@contextmanager
def _temporary_output_file():
    """Yield an unpredictable per-call path and remove it after use."""
    path = os.path.join(
        tempfile.gettempdir(), f"igor_mcp_{uuid.uuid4().hex}.txt"
    )
    try:
        yield path
    finally:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            # Cleanup must not mask the result of an Igor command.
            pass


def _read_tmp_file(path: str) -> str:
    """Read a capture file, waiting briefly for Igor to close it."""
    for _ in range(_TMP_READ_ATTEMPTS):
        if os.path.exists(path):
            try:
                with open(path, "rb") as handle:
                    data = handle.read(_MAX_OUTPUT_BYTES + 1)
                if len(data) > _MAX_OUTPUT_BYTES:
                    raise IgorError(
                        f"Igor output exceeded the {_MAX_OUTPUT_BYTES:,}-byte limit"
                    )
                return data.decode("utf-8", errors="replace")
            except PermissionError:
                pass
        time.sleep(_TMP_READ_DELAY_SECONDS)
    raise IgorError(
        "Igor produced no output file; the command may have failed or Igor may be busy"
    )


def _normalize_newlines(text: str) -> str:
    """Normalize CRLF and lone CR without introducing blank lines."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _active_igor():
    """Return an active Igor COM object or raise with all attempted ProgIDs."""
    if _PYWIN32_IMPORT_ERROR:
        raise IgorError(f"pywin32 is not available: {_PYWIN32_IMPORT_ERROR}")

    failures = []
    for progid in _PROGIDS:
        try:
            # GetActiveObject cannot create a new Igor process.
            return win32com.client.GetActiveObject(progid)
        except Exception as exc:
            failures.append(f"{progid}: {type(exc).__name__}: {exc}")

    raise IgorError(
        "Igor not running (no active COM object for 'IgorPro.Application' or "
        "'Igor.Application'). " + " | ".join(failures)
    )


def _igor_executable_candidates() -> list[Path]:
    """Return configured and standard Igor executable candidates."""
    configured = os.environ.get(_IGOR_EXECUTABLE_ENV, "").strip()
    if configured:
        return [Path(configured)]

    # The personal Windows deployment has a stable Igor Pro 10 installation.
    # Try this exact path first so normal startup does not depend on a broad
    # filesystem search. The versioned-folder scan below remains a fallback for
    # other installed Igor versions or machines with a different layout.
    preferred = [_DEFAULT_IGOR_EXECUTABLE]

    roots = []
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        value = os.environ.get(variable, "").strip()
        if value:
            roots.append(Path(value) / "WaveMetrics")
    roots.extend((
        Path(r"C:\Program Files\WaveMetrics"),
        Path(r"C:\Program Files (x86)\WaveMetrics"),
    ))

    candidates: list[Path] = []
    seen: set[str] = set()
    for candidate in preferred:
        key = str(candidate).casefold()
        if key not in seen and candidate.is_file():
            seen.add(key)
            candidates.append(candidate)
    for root in roots:
        if not root.is_dir():
            continue
        for folder in root.glob("Igor Pro * Folder"):
            for relative in (
                Path("IgorBinaries_x64") / "Igor64.exe",
                Path("IgorBinaries") / "Igor.exe",
            ):
                candidate = folder / relative
                key = str(candidate).casefold()
                if key not in seen and candidate.is_file():
                    seen.add(key)
                    candidates.append(candidate)
    # Preserve the explicit default before fallback discovery results.
    return candidates


def _select_igor_executable() -> Path:
    """Select a validated Igor executable for automatic startup."""
    configured = os.environ.get(_IGOR_EXECUTABLE_ENV, "").strip()
    candidates = _igor_executable_candidates()
    if configured:
        if not candidates[0].is_file():
            raise IgorError(
                f"{_IGOR_EXECUTABLE_ENV} points to a missing file: {candidates[0]}"
            )
        if candidates[0].name.casefold() not in _IGOR_EXECUTABLE_NAMES:
            raise IgorError(
                f"{_IGOR_EXECUTABLE_ENV} must name Igor.exe or Igor64.exe: "
                f"{candidates[0]}"
            )
    if not candidates:
        raise IgorError(
            "Igor is not running and no Igor executable was found. Set "
            f"{_IGOR_EXECUTABLE_ENV} to the full path of Igor64.exe or Igor.exe."
        )
    return candidates[0]





def _is_igor_process_running() -> bool:
    """Return True if an Igor process is already running on Windows."""
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        kernel32 = ctypes.windll.kernel32

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == -1:
            return False
        try:
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            if kernel32.Process32First(snapshot, ctypes.byref(entry)):
                while True:
                    name = entry.szExeFile.decode("utf-8", errors="ignore").casefold()
                    if name in ("igor64.exe", "igor.exe"):
                        return True
                    if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                        break
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:
        pass
    return False


def _transport_mode() -> str:
    """Return configured transport selection mode: 'auto', 'pipe', or 'com'."""
    mode = os.environ.get(_IGOR_TRANSPORT_ENV, "auto").strip().lower()
    return mode if mode in _VALID_TRANSPORTS else "auto"


def _clamp_timeout(
    timeout_seconds: float | None,
    *,
    default: float = _IGOR_COMMAND_TIMEOUT_SECONDS,
) -> float:
    """Validate and clamp a caller-provided timeout to [1.0, 600.0] seconds."""
    if timeout_seconds is None:
        return float(default)
    if isinstance(timeout_seconds, bool) or not isinstance(
        timeout_seconds, (int, float)
    ):
        raise IgorError("timeout_seconds must be a number")
    if not math.isfinite(timeout_seconds):
        raise IgorError("timeout_seconds must be finite")
    return max(
        _MIN_USER_TIMEOUT_SECONDS,
        min(_MAX_USER_TIMEOUT_SECONDS, float(timeout_seconds)),
    )


def _executable_igor_version(executable: Path) -> str | None:
    """Return the Windows file version string for an Igor executable, if readable."""
    if sys.platform != "win32" or not executable.is_file():
        return None
    try:
        import win32api

        info = win32api.GetFileVersionInfo(str(executable), "\\")
        ms = info["FileVersionMS"]
        ls = info["FileVersionLS"]
        return (
            f"{win32api.HIWORD(ms)}.{win32api.LOWORD(ms)}."
            f"{win32api.HIWORD(ls)}.{win32api.LOWORD(ls)}"
        )
    except Exception:
        return None


def _try_pipe_client(
    *,
    auto_start: bool = False,
    timeout_seconds: float = 2.0,
) -> igor_pipe.IgorPipeClient | None:
    """Return a connected IgorPipeClient if a live native pipe is found, else None."""
    if _transport_mode() == "com":
        return None
    client = igor_pipe.IgorPipeClient(default_timeout=timeout_seconds)
    try:
        client.connect(timeout_seconds=timeout_seconds)
        return client
    except igor_pipe.PipeNotAvailableError:
        client.close()
    if not auto_start:
        return None
    try:
        with _igor_application(auto_start=True):
            pass
    except IgorError:
        return None
    client = igor_pipe.IgorPipeClient(default_timeout=timeout_seconds)
    try:
        client.connect(timeout_seconds=timeout_seconds)
        return client
    except igor_pipe.PipeNotAvailableError:
        client.close()
        return None


def _is_igor_main_window_candidate(class_name: str, title: str) -> bool:
    """Return True only for Igor Pro's actual top-level main application window."""
    if not title or not title.strip():
        return False
    lowered_title = title.casefold()
    if lowered_title in _DDE_HELPER_WINDOW_TITLES:
        return False
    if any(token in lowered_title for token in _NON_MAIN_HELPER_TITLE_SUBSTRINGS):
        return False
    if class_name in _DDE_HELPER_WINDOW_CLASSES:
        return False
    if class_name in _NON_MAIN_HELPER_WINDOW_CLASSES:
        return False
    if class_name in _DIALOG_WINDOW_CLASSES:
        return False
    if title in _COMPILER_DIALOG_TITLES:
        return False
    lowered_class = class_name.casefold()
    if any(
        token in lowered_class
        for token in ("popup", "tooltip", "toolsavebits", "gdi+", "ime")
    ):
        return False
    return "igor" in lowered_title


def _show_new_igor_window_without_activation(process_id: int) -> bool:
    """Show a newly started Igor main window without activating it."""
    if not process_id:
        return False

    windows: list[int] = []
    helper_windows: list[int] = []
    enum_proc_type = WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @enum_proc_type
    def top_level_callback(hwnd, _lparam):
        hwnd = int(hwnd)
        if _window_process_id(hwnd) != process_id:
            return True
        class_name = _window_class(hwnd)
        if class_name in _DDE_HELPER_WINDOW_CLASSES:
            helper_windows.append(hwnd)
            return True
        title = _window_text(hwnd)
        if title.casefold() in _DDE_HELPER_WINDOW_TITLES:
            # The title is an additional process-scoped exclusion only. Never
            # hide a window based on its title because Igor graphs are user data.
            return True
        if _window_owner(hwnd):
            return True
        if not _is_igor_main_window_candidate(class_name, title):
            return True
        windows.append(hwnd)
        return True

    # Igor may register its automation object just before creating its main
    # window, so allow a short interval for that top-level window to appear.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        windows.clear()
        helper_windows.clear()
        windll.user32.EnumWindows(top_level_callback, 0)
        for hwnd in helper_windows:
            if windll.user32.IsWindowVisible(hwnd):
                windll.user32.ShowWindow(wintypes.HWND(hwnd), _SW_HIDE)
        if windows:
            break
        time.sleep(0.05)

    # If Igor already has a visible main window (including minimized), leave it
    # alone and never un-hide any other hidden windows.
    if any(windll.user32.IsWindowVisible(hwnd) for hwnd in windows):
        return True

    # Otherwise show only the single primary Igor main window without activation.
    if windows:
        windll.user32.ShowWindow(wintypes.HWND(windows[0]), _SW_SHOWNOACTIVATE)

    return bool(windows)


def _connect(*, auto_start: bool = True):
    """Attach to Igor; caller must initialize COM on this worker thread."""
    if _PYWIN32_IMPORT_ERROR:
        raise IgorError(f"pywin32 is not available: {_PYWIN32_IMPORT_ERROR}")
    try:
        # Keep an attached Igor instance in its existing window state.
        return _active_igor()
    except IgorError as initial_error:
        if not auto_start:
            raise initial_error

    executable = _select_igor_executable()
    process = None
    if not _is_igor_process_running():
        launch_args = [str(executable)]
        if _transport_mode() != "com" and igor_pipe.supports_native_pipe(
            _executable_igor_version(executable)
        ):
            launch_args.append("/JSONRPC")
        try:
            process = subprocess.Popen(
                launch_args,
                cwd=str(executable.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except OSError as exc:
            raise IgorError(
                f"Could not start Igor executable '{executable}': {exc}"
            ) from exc

    deadline = time.monotonic() + _IGOR_STARTUP_TIMEOUT_SECONDS
    last_error = "COM registration has not completed"
    while time.monotonic() < deadline:
        if process is not None:
            return_code = process.poll()
            if return_code is not None:
                raise IgorError(
                    f"Igor exited during startup with code {return_code}; "
                    f"executable was '{executable}'"
                )
        try:
            # Preserve an existing Igor window exactly. On first launch, show
            # the new main window without activation so the user can inspect it
            # or bring it forward later.
            try:
                app = _active_igor()
                if process is not None:
                    _show_new_igor_window_without_activation(process.pid)
                return app
            except IgorError as exc:
                last_error = str(exc)
                time.sleep(_IGOR_STARTUP_POLL_SECONDS)
        except IgorError as exc:
            last_error = str(exc)
            time.sleep(_IGOR_STARTUP_POLL_SECONDS)

    proc_info = f"process {process.pid}" if process is not None else "existing process"
    raise IgorError(
        f"Igor {proc_info} started from '{executable}', but no active "
        f"COM object appeared within {_IGOR_STARTUP_TIMEOUT_SECONDS:.0f} seconds. "
        f"Last connection error: {last_error}"
    )


@contextmanager
def _igor_application(*, auto_start: bool = True):
    """Initialize COM for this call and balance it before the worker returns."""
    if _PYWIN32_IMPORT_ERROR:
        raise IgorError(f"pywin32 is not available: {_PYWIN32_IMPORT_ERROR}")
    pythoncom.CoInitialize()
    try:
        app = _connect(auto_start=auto_start)
        yield app
    finally:
        pythoncom.CoUninitialize()


def _raw_execute(app, line: str) -> None:
    """Execute server-generated Igor code and surface an Igor/COM failure."""
    try:
        app.Execute(line)
    except Exception as exc:
        raise IgorError(f"COM error while executing Igor command: {exc}") from exc


def _query(app, fmt: str, args: str) -> str:
    """Evaluate Igor expressions and return their formatted text safely."""
    file_ref = _helper_name("igorMcpFile")
    with _temporary_output_file() as path:
        line = (
            f"Variable {file_ref}; "
            f'Open {file_ref} as "{_igor_escape(path)}"; '
            f'fprintf {file_ref}, "{fmt}", {args}; '
            f"Close {file_ref}; KillVariables/Z {file_ref}"
        )
        _raw_execute(app, line)
        return _read_tmp_file(path)


def _execute_via_tempfile(app, command: str) -> str:
    """Capture one command's history and any Igor error in an isolated file."""
    capture_ref = _helper_name("igorMcpCapture")
    error_var = _helper_name("igorMcpError")
    history_var = _helper_name("igorMcpHistory")
    file_ref = _helper_name("igorMcpFile")

    with _temporary_output_file() as path:
        line = (
            f"Variable {capture_ref} = CaptureHistoryStart(); "
            f'Execute/Z "{_igor_escape(command)}"; '
            f"Variable {error_var} = V_Flag; "
            f"String {history_var} = CaptureHistory({capture_ref}, 1); "
            f"Variable {file_ref}; "
            f'Open {file_ref} as "{_igor_escape(path)}"; '
            f'fprintf {file_ref}, "%s", {history_var}; '
            f'fprintf {file_ref}, "\\n{_EXECUTION_STATUS_MARKER}%d|%s", '
            f'{error_var}, GetErrMessage({error_var}); '
            f"Close {file_ref}; "
            f"KillVariables/Z {capture_ref}, {error_var}, {file_ref}; "
            f"KillStrings/Z {history_var}"
        )
        _raw_execute(app, line)
        captured = _normalize_newlines(_read_tmp_file(path))

    try:
        output, status = captured.rsplit(_EXECUTION_STATUS_MARKER, 1)
        error_code, error_message = status.split("|", 1)
        error_code = int(error_code.strip())
    except (ValueError, TypeError) as exc:
        raise IgorError("Igor returned malformed command-capture status") from exc

    output = output.rstrip("\n")
    if error_code:
        message = error_message.strip() or "Igor did not provide an error message"
        raise IgorError(f"IGOR ERROR {error_code}: {message}")
    return output or "(command executed; no output)"


def _execute_command(app, command: str) -> str:
    """Capture one command's history and any Igor error.

    The temporary-file path is used only when the COM object does not expose
    ``Execute2``. Once ``Execute2`` has been looked up and invoked, an
    exception is reported as an unknown command outcome instead of replaying
    a potentially mutating command through the fallback path.
    """
    try:
        execute2 = getattr(app, "Execute2")
    except AttributeError:
        execute2 = None

    if execute2 is not None:
        try:
            # Execute2(flags1, flags2, cmd, errorCode, errorMsg, history, results)
            # In win32com, out parameters are returned as a tuple: (errorCode, errorMsg, history, results)
            ret = execute2(0, 0, command, 0, "", "", "")
            error_code, error_message, history, results = ret
            if error_code != 0:
                message = error_message.strip() or "Igor did not provide an error message"
                raise IgorError(f"IGOR ERROR {error_code}: {message}")
            cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(history)).lstrip("¶\r\n").strip()
            return cleaned or str(results).strip() or "(command executed; no output)"
        except IgorError:
            raise
        except Exception as exc:
            raise IgorError(
                "Igor Execute2 failed after dispatch; command outcome is unknown. "
                "Do not retry the command until Igor state has been inspected."
            ) from exc
    return _execute_via_tempfile(app, command)


def _validate_command(command: str) -> str:
    if not isinstance(command, str) or not command.strip():
        raise IgorError("command must be a non-empty string")
    if "\x00" in command:
        raise IgorError("command cannot contain null bytes")
    if "\r" in command or "\n" in command:
        lines = [
            line.strip()
            for line in command.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        ]
        non_empty = [line for line in lines if line]
        for line in non_empty:
            if line.startswith("//"):
                raise IgorError(
                    "command lines starting with '//' cannot be safely chained with semicolons"
                )
        command = "; ".join(line.rstrip(";") for line in non_empty)
    command_bytes = len(command.encode("utf-8"))
    if command_bytes > _MAX_COMMAND_BYTES:
        raise IgorError(
            f"command is {command_bytes:,} UTF-8 bytes and exceeds the "
            f"{_MAX_COMMAND_BYTES:,}-byte server limit"
        )
    return command


def _validate_experiment_file_path(file_path: str) -> Path:
    """Require an existing absolute Igor experiment file path."""
    if not isinstance(file_path, str) or not file_path.strip():
        raise IgorError("file_path must be a non-empty absolute path")
    if "\x00" in file_path:
        raise IgorError("file_path cannot contain null bytes")

    path = Path(file_path)
    if not path.is_absolute():
        raise IgorError("file_path must be an absolute path")
    if path.suffix.casefold() not in _EXPERIMENT_FILE_EXTENSIONS:
        raise IgorError("file_path must name an Igor .pxp or .pxt experiment")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise IgorError(f"Igor experiment file does not exist: {file_path}") from exc
    if not resolved.is_file():
        raise IgorError(f"Igor experiment path is not a file: {file_path}")
    return resolved


def _validate_max_points(max_points: int) -> int:
    if isinstance(max_points, bool) or not isinstance(max_points, int):
        raise IgorError("max_points must be an integer")
    if not 0 <= max_points <= _MAX_WAVE_POINTS:
        raise IgorError(
            f"max_points must be between 0 and {_MAX_WAVE_POINTS:,}"
        )
    return max_points




def _json_numeric_value(text: str):
    """Return a finite Igor number as JSON numeric data; preserve special text."""
    try:
        value = float(text)
    except ValueError:
        return text
    return value if math.isfinite(value) else text


def _igor_version(app) -> float:
    """Return the connected Igor version using syntax available in Igor 6."""
    text = _query(app, "%.2f", "IgorVersion()").strip()
    try:
        return float(text)
    except ValueError as exc:
        raise IgorError(f"Igor returned an invalid version value: {text!r}") from exc


def _ensure_supported_igor_version(version: float) -> None:
    """Reject Igor versions older than the supported Igor Pro 6 baseline."""
    if version < _MIN_IGOR_VERSION:
        raise IgorError(
            f"Igor Pro {_MIN_IGOR_VERSION:.1f} or newer is required; "
            f"connected version is {version:.2f}"
        )


def _with_igor(
    callback,
    *,
    require_supported: bool = True,
    auto_start: bool = True,
    check_dialogs: bool = True,
):
    """Serialize Igor access, preserve dialogs, and balance COM teardown."""
    with _lock:
        with _igor_application(auto_start=auto_start) as app:
            if check_dialogs:
                dialogs = _igor_dialogs()
                if dialogs:
                    raise IgorError(
                        _dialog_blocking_message(dialogs, "before the tool operation")
                    )
            version = _igor_version(app)
            if require_supported:
                _ensure_supported_igor_version(version)
            return callback(app, version)


def _list_wave_names_in_folder(app, version: float, folder: str) -> list[str]:
    """List wave names through the fastest read-only path supported by Igor."""
    escaped_folder = _igor_escape(folder)
    if version >= _DATA_FOLDER_WAVELIST_VERSION:
        wave_list = _query(
            app,
            "%s",
            f'WaveList("*", ";", "", $"{escaped_folder}")',
        )
        return [name for name in wave_list.split(";") if name.strip()]

    wave_count = int(
        _query(
            app,
            "%d",
            f'CountObjects("{escaped_folder}", 1)',
        ).strip()
    )
    if wave_count > _MAX_LISTED_WAVES:
        raise IgorError(
            f"Data folder '{folder}' contains {wave_count:,} waves; "
            f"the Igor 6-8 listing limit is {_MAX_LISTED_WAVES:,}"
        )
    names = []
    output_bytes = 0
    for index in range(wave_count):
        name = _query(
            app,
            "%s",
            f'GetIndexedObjName("{escaped_folder}", 1, {index})',
        ).strip()
        if not name:
            raise IgorError(
                f"Igor returned no name for wave index {index} in '{folder}'"
            )
        output_bytes += len(name.encode("utf-8")) + 1
        if output_bytes > _MAX_OUTPUT_BYTES:
            raise IgorError("wave listing exceeded the server output limit")
        names.append(name)
    return names


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


def _execute_igor_via_pipe(
    client: igor_pipe.IgorPipeClient,
    command: str,
    *,
    timeout_seconds: float,
    started_callback=None,
) -> ExecuteIgorResult:
    """Execute one Igor command over the native JSON-RPC named pipe.

    Raises PipeNotAvailableError only before request dispatch so the caller can
    safely fall back to COM. Any post-dispatch failure (PipeTimeoutError,
    PipeDispatchError) returns a conservative unknown-outcome result without
    replaying the command.
    """
    compilation_errors: dict[int, dict[str, object]] = {}
    before = _igor_dialogs()
    if before:
        for error in _capture_compilation_errors(before, phase="before_command"):
            compilation_errors.setdefault(int(error["hwnd"]), error)
        return _failed_execute_result(
            _dialog_blocking_message(before, "before command execution"),
            command_started=False,
            outcome_known=True,
            preexisting_dialog_count=len(before),
            dialog_details=before,
            compilation_errors=list(compilation_errors.values()),
            transport="pipe",
        )

    stop_event = threading.Event()
    detected: dict[int, dict[str, object]] = {}
    detected_lock = threading.Lock()
    watcher = threading.Thread(
        target=_watch_for_igor_dialogs,
        args=(stop_event, detected, detected_lock, compilation_errors),
        name="igor-mcp-dialog-watch",
        daemon=True,
    )
    watcher.start()
    t0 = time.monotonic()
    resp: dict[str, Any] | None = None
    rpc_error: igor_pipe.PipeRPCError | None = None
    dispatch_error: igor_pipe.PipeDispatchError | None = None
    marker = getattr(_command_start_marker, "event", None)
    try:
        if started_callback is not None:
            started_callback()
        if marker is not None:
            marker.set()
        resp = client.execute_command(command, timeout_seconds=timeout_seconds)
    except igor_pipe.PipeNotAvailableError:
        # Pre-dispatch only: clear started marker so COM fallback does not
        # misreport command_started=True if COM subsequently fails pre-dispatch.
        if marker is not None:
            marker.clear()
        raise
    except igor_pipe.PipeRPCError as exc:
        rpc_error = exc
    except igor_pipe.PipeDispatchError as exc:
        # Covers PipeTimeoutError (subclass of PipeDispatchError) and broken pipe
        # post-dispatch: NEVER fall back or replay over COM.
        dispatch_error = exc
    finally:
        stop_event.set()
        watcher.join(timeout=1.0)

    elapsed = round(time.monotonic() - t0, 4)
    after = _igor_dialogs()
    with detected_lock:
        for dialog in after:
            detected[int(dialog["hwnd"])] = dialog
        detected_dialogs = list(detected.values())
    unread_compiler_dialogs = [
        dialog
        for dialog in detected_dialogs
        if str(dialog.get("title", "")) in _COMPILER_DIALOG_TITLES
        and int(dialog["hwnd"]) not in compilation_errors
    ]
    for error in _capture_compilation_errors(
        unread_compiler_dialogs, phase="after_command"
    ):
        compilation_errors.setdefault(int(error["hwnd"]), error)

    dialog_detected = bool(detected_dialogs)
    comp_errors = list(compilation_errors.values())

    if dispatch_error is not None:
        err_msg = str(dispatch_error)
        if dialog_detected:
            err_msg = (
                f"{err_msg} "
                f"{_dialog_blocking_message(detected_dialogs, 'during or after command execution')}"
            )
        return _failed_execute_result(
            err_msg,
            command_started=True,
            outcome_known=False,
            dialog_details=detected_dialogs,
            compilation_errors=comp_errors,
            elapsed_seconds=elapsed,
            transport="pipe",
        )

    if rpc_error is not None:
        err_msg = str(rpc_error)
        if dialog_detected:
            err_msg = (
                f"{err_msg} "
                f"{_dialog_blocking_message(detected_dialogs, 'during or after command execution')}"
            )
        return {
            "ok": False,
            "output": "",
            "error": err_msg,
            "warning": None,
            "command_started": True,
            "command_completed": False,
            "outcome_known": True,
            "preexisting_dialog_count": 0,
            "dialog_detected": dialog_detected,
            "dialogs_open": len(after),
            "dialog_titles": [str(dialog["title"]) for dialog in detected_dialogs],
            "dialog_details": detected_dialogs,
            "compilation_errors": comp_errors,
            "history": "",
            "results": "",
            "elapsed_seconds": elapsed,
            "transport": "pipe",
        }

    assert resp is not None
    history = str(resp.get("history", "") or "")
    results = str(resp.get("results", "") or "")
    elapsed_value = resp.get("elapsedSeconds", elapsed)
    error_code = resp.get("errorCode", 0)
    raw_error = resp.get("errorMessage") or resp.get("error")
    command_failed = bool(error_code) or resp.get("ok") is False
    if command_failed:
        if error_code:
            msg = str(raw_error).strip() if raw_error else "Igor did not provide an error message"
            error: str | None = f"IGOR ERROR {error_code}: {msg}"
        else:
            error = str(raw_error or "Igor command failed")
    else:
        error = None

    cleaned_history = re.sub(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", history
    ).lstrip("¶\r\n").strip()
    output = cleaned_history or results.strip() or "(command executed; no output)"
    command_completed = not command_failed
    if dialog_detected:
        dialog_error = _dialog_blocking_message(
            detected_dialogs, "during or after command execution"
        )
        error = f"{error} {dialog_error}" if error else dialog_error

    return {
        "ok": command_completed and not dialog_detected,
        "output": output if command_completed else cleaned_history or results.strip(),
        "error": error,
        "warning": None,
        "command_started": True,
        "command_completed": command_completed,
        "outcome_known": True,
        "preexisting_dialog_count": 0,
        "dialog_detected": dialog_detected,
        "dialogs_open": len(after),
        "dialog_titles": [str(dialog["title"]) for dialog in detected_dialogs],
        "dialog_details": detected_dialogs,
        "compilation_errors": comp_errors,
        "history": history,
        "results": results,
        "elapsed_seconds": elapsed_value,
        "transport": "pipe",
    }


def _execute_igor_direct(
    command: str, *, started_callback=None, timeout_seconds: float | None = None
) -> ExecuteIgorResult:
    """Execute one Igor command and preserve any visible dialog diagnostics."""
    command = _validate_command(command)
    effective_timeout = _clamp_timeout(timeout_seconds)
    callback_fired = False

    def _fire_started_once() -> None:
        nonlocal callback_fired
        if started_callback is not None and not callback_fired:
            callback_fired = True
            started_callback()

    mode = _transport_mode()
    if mode != "com":
        client = _try_pipe_client(auto_start=False, timeout_seconds=2.0)
        if client is not None:
            try:
                return _execute_igor_via_pipe(
                    client,
                    command,
                    timeout_seconds=effective_timeout,
                    started_callback=_fire_started_once if started_callback is not None else None,
                )
            except igor_pipe.PipeNotAvailableError as exc:
                if mode == "pipe":
                    return _failed_execute_result(
                        str(exc),
                        command_started=False,
                        outcome_known=True,
                        transport="pipe",
                    )
                # Pre-dispatch failure in 'auto' mode: safe to fall back to COM.
                callback_fired = False
            finally:
                client.close()
        elif mode == "pipe":
            return _failed_execute_result(
                "No active Igor Pro named-pipe instance found (IGOR_TRANSPORT=pipe).",
                command_started=False,
                outcome_known=True,
                transport="pipe",
            )

    compilation_errors: dict[int, dict[str, object]] = {}

    def operation(app, _version):
        before = _igor_dialogs()
        if before:
            for error in _capture_compilation_errors(before, phase="before_command"):
                compilation_errors.setdefault(int(error["hwnd"]), error)
            return _failed_execute_result(
                _dialog_blocking_message(before, "before command execution"),
                command_started=False,
                outcome_known=True,
                preexisting_dialog_count=len(before),
                dialog_details=before,
                compilation_errors=list(compilation_errors.values()),
                transport="com",
            )

        stop_event = threading.Event()
        detected: dict[int, dict[str, object]] = {}
        detected_lock = threading.Lock()
        watcher = threading.Thread(
            target=_watch_for_igor_dialogs,
            args=(stop_event, detected, detected_lock, compilation_errors),
            name="igor-mcp-dialog-watch",
            daemon=True,
        )
        watcher.start()
        command_error: IgorError | None = None
        result: str | None = None
        t0 = time.monotonic()
        try:
            if started_callback is not None:
                _fire_started_once()
            marker = getattr(_command_start_marker, "event", None)
            if marker is not None:
                marker.set()
            result = _execute_command(app, command)
        except IgorError as exc:
            command_error = exc
        finally:
            stop_event.set()
            watcher.join(timeout=1.0)
        elapsed = round(time.monotonic() - t0, 4)

        after = _igor_dialogs()
        with detected_lock:
            for dialog in after:
                detected[int(dialog["hwnd"])] = dialog
            detected_dialogs = list(detected.values())
        unread_compiler_dialogs = [
            dialog for dialog in detected_dialogs
            if str(dialog.get("title", "")) in _COMPILER_DIALOG_TITLES
            and int(dialog["hwnd"]) not in compilation_errors
        ]
        for error in _capture_compilation_errors(
            unread_compiler_dialogs, phase="after_command"
        ):
            compilation_errors.setdefault(int(error["hwnd"]), error)

        command_completed = result is not None and command_error is None
        dialog_detected = bool(detected_dialogs)
        error = str(command_error) if command_error else None
        if dialog_detected:
            dialog_error = _dialog_blocking_message(
                detected_dialogs, "during or after command execution"
            )
            error = f"{error} {dialog_error}" if error else dialog_error
        return {
            "ok": command_completed and not dialog_detected,
            "output": result or "",
            "error": error,
            "warning": None,
            "command_started": True,
            "command_completed": command_completed,
            "outcome_known": command_completed,
            "preexisting_dialog_count": 0,
            "dialog_detected": dialog_detected,
            "dialogs_open": len(after),
            "dialog_titles": [str(dialog["title"]) for dialog in detected_dialogs],
            "dialog_details": detected_dialogs,
            "compilation_errors": list(compilation_errors.values()),
            "history": result or "",
            "results": "",
            "elapsed_seconds": elapsed,
            "transport": "com",
        }

    return _with_igor(operation, check_dialogs=False)


def _failed_execute_result(
    error: str,
    *,
    command_started: bool,
    outcome_known: bool = False,
    preexisting_dialog_count: int = 0,
    dialog_details: list[dict[str, object]] | None = None,
    compilation_errors: list[dict[str, object]] | None = None,
    elapsed_seconds: float | None = None,
    transport: str = "com",
) -> ExecuteIgorResult:
    """Create a conservative result for a worker or connection failure."""
    details = list(dialog_details or [])
    return {
        "ok": False,
        "output": "",
        "error": error,
        "warning": None,
        "command_started": command_started,
        "command_completed": False,
        "outcome_known": outcome_known,
        "preexisting_dialog_count": preexisting_dialog_count,
        "dialog_detected": bool(details),
        "dialogs_open": len(details),
        "dialog_titles": [str(dialog.get("title", "")) for dialog in details],
        "dialog_details": details,
        "compilation_errors": list(compilation_errors or []),
        "history": "",
        "results": "",
        "elapsed_seconds": elapsed_seconds,
        "transport": transport,
    }


def _run_igor_operation_in_worker_locked(
    operation,
    *,
    failure_factory,
    action_description: str,
    on_timeout=None,
    on_complete=None,
    timeout_seconds=None,
):
    """Run one admitted Igor operation with the shared bounded worker policy."""
    response_timeout = _IGOR_COMMAND_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    result_holder: list[object] = []
    exception_holder: list[Exception] = []
    operation_started = threading.Event()

    def runner():
        _command_start_marker.event = operation_started
        try:
            result_holder.append(operation())
        except Exception as exc:
            exception_holder.append(exc)
        finally:
            try:
                del _command_start_marker.event
            except AttributeError:
                pass

    thread = threading.Thread(
        target=runner,
        name="igor-mcp-command-runner",
        daemon=True,
    )
    thread.start()
    thread.join(timeout=response_timeout)

    if thread.is_alive():
        if on_timeout is not None:
            on_timeout()
        if on_complete is not None:
            def release_after_worker() -> None:
                try:
                    thread.join()
                finally:
                    on_complete()

            threading.Thread(
                target=release_after_worker,
                name="igor-mcp-command-reaper",
                daemon=True,
            ).start()
        error = (
            f"Igor {action_description} exceeded the "
            f"{response_timeout:.0f}-second "
            "response deadline; the worker may still be running and operation outcome is unknown. "
            "Do not retry. Inspect get_igor_dialogs and get_igor_compilation_errors; "
            "explicitly dismiss a blocking dialog only after reviewing it."
        )
        try:
            dialogs = _igor_dialogs()
            compilation_errors = _capture_compilation_errors(
                dialogs, phase="command_timeout"
            )
        except Exception:
            dialogs = []
            compilation_errors = []
        if dialogs:
            error = f"{error} {_dialog_blocking_message(dialogs, 'while the operation outcome is unknown')}"
        return failure_factory(
            error,
            command_started=operation_started.is_set(),
            outcome_known=False,
            dialog_details=dialogs,
            compilation_errors=compilation_errors,
        )

    if exception_holder:
        started = operation_started.is_set()
        return failure_factory(
            str(exception_holder[0]),
            command_started=started,
            outcome_known=not started,
        )

    if result_holder:
        return result_holder[0]

    return failure_factory(
        "Igor operation failed without a result",
        command_started=operation_started.is_set(),
        outcome_known=not operation_started.is_set(),
    )


def _execute_igor_in_worker_locked(
    command: str,
    *,
    on_timeout=None,
    on_complete=None,
    timeout_seconds: float | None = None,
) -> ExecuteIgorResult:
    """Run one admitted command in a bounded worker thread."""
    if timeout_seconds is None:
        operation = lambda: _execute_igor_direct(command)
        effective_timeout = float(_IGOR_COMMAND_TIMEOUT_SECONDS)
    else:
        operation = lambda: _execute_igor_direct(
            command, timeout_seconds=timeout_seconds
        )
        effective_timeout = float(timeout_seconds)
    worker_grace = (
        _PIPE_WORKER_TIMEOUT_GRACE_SECONDS
        if _transport_mode() != "com" and effective_timeout >= _MIN_USER_TIMEOUT_SECONDS
        else 0.0
    )
    outer_timeout = effective_timeout + worker_grace
    return _run_igor_operation_in_worker_locked(
        operation,
        failure_factory=_failed_execute_result,
        action_description="command execution",
        on_timeout=on_timeout,
        on_complete=on_complete,
        timeout_seconds=outer_timeout,
    )


def _execute_igor_in_worker(
    command: str, *, timeout_seconds: float | None = None
) -> ExecuteIgorResult:
    """Admit one Igor command at a time and bound the response wait.

    If the worker outlives the response deadline, the admission remains held
    until the worker exits so a later command cannot overlap an unknown COM
    operation.
    """
    if not _command_worker_lock.acquire(timeout=_COMMAND_WORKER_LOCK_WAIT_SECONDS):
        return _failed_execute_result(
            "Another Igor command is already running; no new command was started. "
            "Inspect Igor state and retry after the active request finishes.",
            command_started=False,
            outcome_known=True,
        )
    release_deferred = False

    def defer_release() -> None:
        nonlocal release_deferred
        release_deferred = True

    def release_after_worker() -> None:
        _command_worker_lock.release()

    try:
        return _execute_igor_in_worker_locked(
            command,
            on_timeout=defer_release,
            on_complete=release_after_worker,
            timeout_seconds=timeout_seconds,
        )
    finally:
        if not release_deferred:
            _command_worker_lock.release()


@mcp.tool(annotations=_MUTATING_ANNOTATIONS, structured_output=True)
def execute_igor(
    command: Annotated[
        str,
        Field(
            description=(
                "Single-line Igor command string (max 2400 bytes). Separate multiple statements "
                "with semicolons ';'. Do NOT include newlines. Use /O, /Q, and /Z flags for automation."
            )
        ),
    ],
    timeout_seconds: Annotated[
        float | None,
        Field(
            description=(
                "Optional command timeout in seconds (clamped to 1.0..600.0). "
                "Defaults to the server's 25-second response deadline."
            )
        ),
    ] = None,
) -> ExecuteIgorResult:
    """Execute one user-requested Igor command through a bounded worker thread.

    Use direct commands for simple calculations, plots, fits, inspection, and
    short sequences. Use a bounded IPF procedure for ODEs, instrument control,
    substantial batch or loop-heavy processing across datasets, reusable
    routines, or when requested. A large wave handled by one built-in command
    does not need a procedure. A response timeout does not cancel Igor work;
    the server keeps command admission held
    until the worker exits and reports the command outcome as unknown. Visible dialogs are left open and reported with available compiler diagnostics. Compilation queued with
    ``Execute/P/Q/Z \"COMPILEPROCEDURES \"`` is asynchronous; after it returns,
    call ``get_igor_compilation_errors`` before dismissing a visible diagnostic.
    An empty ``compilation_errors`` list does not establish that queued
    compilation succeeded.
    """
    normalized_command = _validate_command(command)
    if timeout_seconds is None:
        return _execute_igor_in_worker(normalized_command)
    clamped = _clamp_timeout(timeout_seconds)
    return _execute_igor_in_worker(normalized_command, timeout_seconds=clamped)


def _failed_open_igor_experiment_result(
    file_path: str,
    error: str,
    *,
    command_started: bool,
    outcome_known: bool = False,
    dialog_details: list[dict[str, object]] | None = None,
    compilation_errors: list[dict[str, object]] | None = None,
) -> OpenIgorExperimentResult:
    """Create a conservative result for an open worker or COM failure."""
    details = list(dialog_details or [])
    return {
        "ok": False,
        "file_path": file_path,
        "current_experiment": None,
        "error": error,
        "load_started": command_started,
        "load_completed": False,
        "outcome_known": outcome_known,
        "dialogs_open": len(details),
        "dialog_titles": [str(dialog.get("title", "")) for dialog in details],
        "dialog_details": details,
        "compilation_errors": list(compilation_errors or []),
    }


def _open_igor_experiment_direct(
    path: Path, *, started_callback=None
) -> OpenIgorExperimentResult:
    """Open a saved experiment via native JSON-RPC pipe or COM LoadExperiment."""
    file_path = str(path)
    before = _igor_dialogs()
    if before:
        compilation_errors = _capture_compilation_errors(
            before, phase="before_experiment_open"
        )
        return _failed_open_igor_experiment_result(
            file_path,
            _dialog_blocking_message(before, "before experiment open"),
            command_started=False,
            outcome_known=True,
            dialog_details=before,
            compilation_errors=compilation_errors,
        )

    mode = _transport_mode()
    if mode != "com":
        client = _try_pipe_client(auto_start=False, timeout_seconds=2.0)
        if client is not None:
            pipe_unavailable = False
            try:
                before = _igor_dialogs()
                if before:
                    compilation_errors = _capture_compilation_errors(
                        before, phase="before_experiment_open"
                    )
                    return _failed_open_igor_experiment_result(
                        file_path,
                        _dialog_blocking_message(before, "before experiment open"),
                        command_started=False,
                        outcome_known=True,
                        dialog_details=before,
                        compilation_errors=compilation_errors,
                    )

                if started_callback is not None:
                    started_callback()
                marker = getattr(_command_start_marker, "event", None)
                if marker is not None:
                    marker.set()

                cmd = f'Execute/P/Q "LOADFILE {_igor_escape(file_path)}"'
                try:
                    resp = client.execute_command(
                        cmd, timeout_seconds=_IGOR_COMMAND_TIMEOUT_SECONDS
                    )
                except igor_pipe.PipeNotAvailableError as exc:
                    if marker is not None:
                        marker.clear()
                    if mode == "pipe":
                        return _failed_open_igor_experiment_result(
                            file_path,
                            str(exc),
                            command_started=False,
                            outcome_known=True,
                        )
                    pipe_unavailable = True
                except igor_pipe.PipeDispatchError as exc:
                    # Post-dispatch failure: never replay over COM.
                    after = _igor_dialogs()
                    compilation_errors = _capture_compilation_errors(
                        after, phase="after_experiment_open"
                    )
                    return _failed_open_igor_experiment_result(
                        file_path,
                        str(exc),
                        command_started=True,
                        outcome_known=False,
                        dialog_details=after,
                        compilation_errors=compilation_errors,
                    )
                except igor_pipe.PipeRPCError as exc:
                    after = _igor_dialogs()
                    compilation_errors = _capture_compilation_errors(
                        after, phase="after_experiment_open"
                    )
                    return _failed_open_igor_experiment_result(
                        file_path,
                        str(exc),
                        command_started=True,
                        outcome_known=True,
                        dialog_details=after,
                        compilation_errors=compilation_errors,
                    )

                if not pipe_unavailable:
                    error_code = resp.get("errorCode", 0)
                    raw_error = resp.get("errorMessage") or resp.get("error")
                    if error_code or resp.get("ok") is False:
                        err_msg = (
                            f"IGOR ERROR {error_code}: {raw_error}"
                            if error_code
                            else str(raw_error or "Igor failed to open experiment")
                        )
                        after = _igor_dialogs()
                        compilation_errors = _capture_compilation_errors(
                            after, phase="after_experiment_open"
                        )
                        return _failed_open_igor_experiment_result(
                            file_path,
                            err_msg,
                            command_started=True,
                            outcome_known=True,
                            dialog_details=after,
                            compilation_errors=compilation_errors,
                        )

                    after = _igor_dialogs()
                    if after:
                        compilation_errors = _capture_compilation_errors(
                            after, phase="after_experiment_open"
                        )
                        return _failed_open_igor_experiment_result(
                            file_path,
                            _dialog_blocking_message(
                                after, "during or after experiment open"
                            ),
                            command_started=True,
                            outcome_known=False,
                            dialog_details=after,
                            compilation_errors=compilation_errors,
                        )

                    experiment_name: str | None = None
                    try:
                        info_resp = client.call_function(
                            "IgorInfo", [1], timeout_seconds=10.0
                        )
                        raw_exp = str(info_resp.get("value", "") or "").strip()
                        experiment_name = raw_exp or None
                    except igor_pipe.IgorPipeError:
                        experiment_name = None

                    return {
                        "ok": True,
                        "file_path": file_path,
                        "current_experiment": experiment_name,
                        "error": None,
                        "load_started": True,
                        "load_completed": True,
                        "outcome_known": True,
                        "dialogs_open": 0,
                        "dialog_titles": [],
                        "dialog_details": [],
                        "compilation_errors": [],
                    }
            finally:
                client.close()
        elif mode == "pipe":
            return _failed_open_igor_experiment_result(
                file_path,
                "No active Igor Pro named-pipe instance found (IGOR_TRANSPORT=pipe).",
                command_started=False,
                outcome_known=True,
            )

    def operation(app, _version):
        before = _igor_dialogs()
        if before:
            compilation_errors = _capture_compilation_errors(
                before, phase="before_experiment_open"
            )
            return _failed_open_igor_experiment_result(
                file_path,
                _dialog_blocking_message(before, "before experiment open"),
                command_started=False,
                outcome_known=True,
                dialog_details=before,
                compilation_errors=compilation_errors,
            )

        if started_callback is not None:
            started_callback()
        marker = getattr(_command_start_marker, "event", None)
        if marker is not None:
            marker.set()
        try:
            # IgorProLoadType.ipLoadTypeOpen is 2 in IgorPro.tlb. It follows
            # Igor's normal File > Open behavior, including save prompts.
            app.LoadExperiment(
                _IGOR_LOAD_EXPERIMENT_FLAGS,
                _IGOR_LOAD_TYPE_OPEN,
                "",
                file_path,
            )
        except Exception as exc:
            raise IgorError(
                f"COM error while opening Igor experiment '{file_path}': {exc}"
            ) from exc

        after = _igor_dialogs()
        if after:
            compilation_errors = _capture_compilation_errors(
                after, phase="after_experiment_open"
            )
            return _failed_open_igor_experiment_result(
                file_path,
                _dialog_blocking_message(after, "during or after experiment open"),
                command_started=True,
                outcome_known=False,
                dialog_details=after,
                compilation_errors=compilation_errors,
            )

        experiment_name = _query(app, "%s", "IgorInfo(1)").strip()
        return {
            "ok": True,
            "file_path": file_path,
            "current_experiment": experiment_name or None,
            "error": None,
            "load_started": True,
            "load_completed": True,
            "outcome_known": True,
            "dialogs_open": 0,
            "dialog_titles": [],
            "dialog_details": [],
            "compilation_errors": [],
        }

    try:
        return _with_igor(operation, auto_start=False, check_dialogs=True)
    except IgorError as exc:
        dialogs = _igor_dialogs()
        if dialogs:
            compilation_errors = _capture_compilation_errors(
                dialogs, phase="before_experiment_open"
            )
            marker = getattr(_command_start_marker, "event", None)
            return _failed_open_igor_experiment_result(
                file_path,
                _dialog_blocking_message(dialogs, "during experiment open"),
                command_started=bool(marker and marker.is_set()),
                outcome_known=not bool(marker and marker.is_set()),
                dialog_details=dialogs,
                compilation_errors=compilation_errors,
            )
        raise


def _open_igor_experiment_in_worker(path: Path) -> OpenIgorExperimentResult:
    """Serialize and bound an experiment open without cancelling timed-out COM."""
    file_path = str(path)

    def failure_factory(error, *, command_started, outcome_known=False,
                        dialog_details=None, compilation_errors=None):
        return _failed_open_igor_experiment_result(
            file_path,
            error,
            command_started=command_started,
            outcome_known=outcome_known,
            dialog_details=dialog_details,
            compilation_errors=compilation_errors,
        )

    if not _command_worker_lock.acquire(timeout=_COMMAND_WORKER_LOCK_WAIT_SECONDS):
        return _failed_open_igor_experiment_result(
            file_path,
            "Another Igor command is already running; no experiment open was started. "
            "Inspect Igor state and retry after the active request finishes.",
            command_started=False,
            outcome_known=True,
        )

    release_deferred = False

    def defer_release() -> None:
        nonlocal release_deferred
        release_deferred = True

    def release_after_worker() -> None:
        _command_worker_lock.release()

    try:
        return _run_igor_operation_in_worker_locked(
            lambda: _open_igor_experiment_direct(path),
            failure_factory=failure_factory,
            action_description="experiment open",
            on_timeout=defer_release,
            on_complete=release_after_worker,
        )
    finally:
        if not release_deferred:
            _command_worker_lock.release()


@mcp.tool(annotations=_MUTATING_ANNOTATIONS, structured_output=True)
def open_igor_experiment(
    file_path: Annotated[
        str,
        Field(
            description=(
                "Absolute path to an existing Igor .pxp or .pxt experiment file. "
                "Uses Igor's normal Open behavior; any unsaved-work prompt remains "
                "under Igor's control and is never dismissed automatically."
            )
        ),
    ],
) -> OpenIgorExperimentResult:
    """Open an existing experiment through COM on the serialized worker.

    Igor must already be running; call ensure_igor separately when startup is
    needed. Normal Igor Open behavior governs unsaved-work prompts. This tool
    does not save, discard, or dismiss dialogs, and it leaves Igor's window
    state unchanged.
    """
    path = _validate_experiment_file_path(file_path)
    return _open_igor_experiment_in_worker(path)


def _kill_waves_quietly_pipe(
    client: igor_pipe.IgorPipeClient, wave_names: Iterable[str]
) -> None:
    """Best-effort cleanup of waves over the native pipe without raising."""
    for name in wave_names:
        try:
            client.execute_command(f"KillWaves/Z {name}", timeout_seconds=10.0)
        except (igor_pipe.IgorPipeError, IgorError):
            pass


def _reserve_import_wave_names_pipe(
    client: igor_pipe.IgorPipeClient, count: int, max_attempts: int = 5
) -> tuple[str, list[str]]:
    """Find a temporary prefix that does not collide with existing waves over pipe."""
    for _ in range(max_attempts):
        prefix = _helper_name("im")
        names = [f"{prefix}{index}" for index in range(count)]
        resp = client.call_function(
            "WaveList", [f"{prefix}*", ";", ""], timeout_seconds=10.0
        )
        matches = str(resp.get("value", "") or "").strip(";")
        if not matches:
            return prefix, names
    raise IgorError("Could not reserve collision-free temporary wave names")


def _kill_waves_quietly(app, wave_names: Iterable[str]) -> None:
    """Best-effort cleanup of waves without raising on error."""
    for name in wave_names:
        try:
            _raw_execute(app, f"KillWaves/Z {name}")
        except IgorError:
            pass


def _reserve_import_wave_names(
    app, count: int, max_attempts: int = 5
) -> tuple[str, list[str]]:
    """Find a temporary prefix that does not collide with existing waves."""
    for _ in range(max_attempts):
        prefix = _helper_name("im")
        names = [f"{prefix}{index}" for index in range(count)]
        matches = _query(app, "%s", f'WaveList("{prefix}*", ";", "")').strip(";")
        if not matches:
            return prefix, names
    raise IgorError("Could not reserve collision-free temporary wave names")


@mcp.tool(annotations=_MUTATING_ANNOTATIONS, structured_output=True)
def import_delimited_text(
    file_path: Annotated[
        str,
        Field(description="Absolute or repo-relative path to a CSV, TSV, or TXT file."),
    ],
    prefix: Annotated[
        str,
        Field(description="Prefix for imported wave names (max 28 chars, e.g. 'imp')."),
    ] = "imported",
    folder: Annotated[
        str,
        Field(description="Target data folder ending in a colon (e.g. 'root:' or 'root:data:')."),
    ] = "root:",
    delimiter: Annotated[
        str,
        Field(description="Column delimiter: must be exactly 'comma', 'tab', or 'semicolon'."),
    ] = "comma",
    has_header: Annotated[
        bool,
        Field(description="Whether row 0 contains column header names."),
    ] = True,
    overwrite: Annotated[
        bool,
        Field(description="Whether to overwrite existing destination waves."),
    ] = False,
) -> DelimitedImportResult:
    """Import a bounded numeric CSV/TSV/TXT file using short Igor wave names.

    The tool validates and normalizes the file before calling Igor. Imported
    waves are named ``prefix0``, ``prefix1``, and so on; the prefix is limited
    so every generated name fits Igor Pro 6's 31-character object-name limit.
    The target data folder must already exist and use portable path components.
    Existing destination waves are rejected unless ``overwrite`` is true.
    """
    resolved_path = _resolve_import_path(file_path)
    prefix = _validate_portable_igor_name(
        prefix, label="prefix", max_length=_MAX_IMPORT_PREFIX_LENGTH
    )
    folder = _validate_portable_data_folder(folder)
    if delimiter not in _IMPORT_DELIMITERS:
        choices = ", ".join(sorted(_IMPORT_DELIMITERS))
        raise IgorError(f"delimiter must be one of: {choices}")
    if not isinstance(has_header, bool):
        raise IgorError("has_header must be true or false")
    if not isinstance(overwrite, bool):
        raise IgorError("overwrite must be true or false")

    delimiter_character = _IMPORT_DELIMITERS[delimiter]
    headers, rows = _read_numeric_delimited_file(
        resolved_path,
        delimiter=delimiter_character,
        has_header=has_header,
    )
    wave_names = [f"{prefix}{index}" for index in range(len(headers))]
    if any(len(name) > _MAX_IGOR_OBJECT_NAME_LENGTH for name in wave_names):
        raise IgorError(
            "Generated wave name exceeds Igor Pro 6's 31-character limit; "
            "use a shorter prefix"
        )

    mode = _transport_mode()
    if mode != "com":
        client = _try_pipe_client(auto_start=False, timeout_seconds=2.0)
        if client is not None:
            mutating_started = False
            try:
                exists_resp = client.call_function(
                    "DataFolderExists", [folder], timeout_seconds=10.0
                )
                try:
                    folder_exists = int(exists_resp.get("value")) == 1
                except (TypeError, ValueError):
                    folder_exists = False
                if not folder_exists:
                    raise IgorError(f"Data folder '{folder}' does not exist")

                target_paths = [f"{folder}{name}" for name in wave_names]
                escaped_folder = _igor_escape(folder)
                list_resp = client.execute_command(
                    f'fprintf 0, "%s", WaveList("*", ";", "", $"{escaped_folder}")',
                    timeout_seconds=10.0,
                )
                if list_resp.get("errorCode"):
                    raise IgorError(
                        f"Failed to list waves in '{folder}': "
                        f"{list_resp.get('errorMessage') or list_resp.get('errorCode')}"
                    )
                existing_names = {
                    name
                    for name in str(list_resp.get("results", "") or "").split(";")
                    if name.strip()
                }
                collisions = [
                    target
                    for name, target in zip(wave_names, target_paths)
                    if name in existing_names
                ]
                if collisions and not overwrite:
                    joined = ", ".join(collisions[:5])
                    extra = (
                        ""
                        if len(collisions) <= 5
                        else f" and {len(collisions) - 5} more"
                    )
                    raise IgorError(
                        f"Destination wave already exists: {joined}{extra}. "
                        "Choose another prefix or set overwrite=true."
                    )

                temporary_prefix, temporary_names = _reserve_import_wave_names_pipe(
                    client, len(headers)
                )
                moved_targets: list[str] = []
                mutating_started = True
                try:
                    with _normalized_import_file(
                        rows, delimiter_character
                    ) as normalized_path:
                        command = (
                            f'LoadWave/J/D/A={temporary_prefix}/O/K=0 '
                            f'"{normalized_path}"'
                        )
                        load_resp = client.execute_command(
                            command, timeout_seconds=25.0
                        )
                        if load_resp.get("errorCode"):
                            raise IgorError(
                                f"LoadWave failed: "
                                f"{load_resp.get('errorMessage') or load_resp.get('errorCode')}"
                            )

                    imported_resp = client.call_function(
                        "WaveList",
                        [f"{temporary_prefix}*", ";", ""],
                        timeout_seconds=10.0,
                    )
                    imported_matches = {
                        name
                        for name in str(imported_resp.get("value", "") or "").split(";")
                        if name
                    }
                    missing = [
                        name
                        for name in temporary_names
                        if name not in imported_matches
                    ]
                    if missing:
                        raise IgorError(
                            "Igor did not create the expected temporary import waves: "
                            + ", ".join(missing)
                        )

                    if overwrite:
                        for target in collisions:
                            kill_resp = client.execute_command(
                                f"KillWaves/Z {target}", timeout_seconds=10.0
                            )
                            if kill_resp.get("errorCode"):
                                raise IgorError(
                                    f"KillWaves failed for {target}: "
                                    f"{kill_resp.get('errorMessage') or kill_resp.get('errorCode')}"
                                )
                    for temporary_name, target in zip(temporary_names, target_paths):
                        move_resp = client.execute_command(
                            f"MoveWave {temporary_name}, {target}",
                            timeout_seconds=10.0,
                        )
                        if move_resp.get("errorCode"):
                            raise IgorError(
                                f"MoveWave failed for {target}: "
                                f"{move_resp.get('errorMessage') or move_resp.get('errorCode')}"
                            )
                        moved_targets.append(target)
                except Exception:
                    _kill_waves_quietly_pipe(client, temporary_names)
                    _kill_waves_quietly_pipe(client, moved_targets)
                    raise

                return {
                    "source_path": str(resolved_path),
                    "folder": folder,
                    "prefix": prefix,
                    "rows": len(rows),
                    "columns": len(headers),
                    "waves": wave_names,
                    "headers": headers,
                    "portable_object_name_limit": _MAX_IGOR_OBJECT_NAME_LENGTH,
                }
            except igor_pipe.PipeNotAvailableError as exc:
                if mode == "pipe" or mutating_started:
                    raise IgorError(str(exc)) from exc
            except igor_pipe.IgorPipeError as exc:
                raise IgorError(str(exc)) from exc
            finally:
                client.close()
        elif mode == "pipe":
            raise IgorError(
                "No active Igor Pro named-pipe instance found (IGOR_TRANSPORT=pipe)."
            )

    def operation(app, version):
        escaped_folder = _igor_escape(folder)
        if _query(app, "%d", f'DataFolderExists("{escaped_folder}")').strip() != "1":
            raise IgorError(f"Data folder '{folder}' does not exist")

        target_paths = [f"{folder}{name}" for name in wave_names]
        existing_names = set(_list_wave_names_in_folder(app, version, folder))
        collisions = [
            target
            for name, target in zip(wave_names, target_paths)
            if name in existing_names
        ]
        if collisions and not overwrite:
            joined = ", ".join(collisions[:5])
            extra = "" if len(collisions) <= 5 else f" and {len(collisions) - 5} more"
            raise IgorError(
                f"Destination wave already exists: {joined}{extra}. "
                "Choose another prefix or set overwrite=true."
            )

        temporary_prefix, temporary_names = _reserve_import_wave_names(app, len(headers))
        moved_targets: list[str] = []
        try:
            with _normalized_import_file(rows, delimiter_character) as normalized_path:
                command = (
                    f'LoadWave/J/D/A={temporary_prefix}/O/K=0 '
                    f'"{normalized_path}"'
                )
                _execute_command(app, command)

            imported_matches = set(
                name
                for name in _query(
                    app,
                    "%s",
                    f'WaveList("{temporary_prefix}*", ";", "")',
                ).split(";")
                if name
            )
            missing = [name for name in temporary_names if name not in imported_matches]
            if missing:
                raise IgorError(
                    "Igor did not create the expected temporary import waves: "
                    + ", ".join(missing)
                )

            if overwrite:
                for target in collisions:
                    _raw_execute(app, f"KillWaves/Z {target}")
            for temporary_name, target in zip(temporary_names, target_paths):
                _raw_execute(app, f"MoveWave {temporary_name}, {target}")
                moved_targets.append(target)
        except Exception:
            _kill_waves_quietly(app, temporary_names)
            _kill_waves_quietly(app, moved_targets)
            raise

        return {
            "source_path": str(resolved_path),
            "folder": folder,
            "prefix": prefix,
            "rows": len(rows),
            "columns": len(headers),
            "waves": wave_names,
            "headers": headers,
            "portable_object_name_limit": _MAX_IGOR_OBJECT_NAME_LENGTH,
        }

    return _with_igor(operation)


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS, structured_output=True)
def read_variable(
    name: Annotated[
        str,
        Field(description="Global variable name or full path (e.g. 'V_avg', 'V_r2', or 'root:my_var')."),
    ],
) -> VariableResult:
    """Read a global numeric or string Igor variable without modifying it."""
    if not isinstance(name, str) or not name.strip():
        raise IgorError("name must be a non-empty variable name or full path")

    mode = _transport_mode()
    if mode != "com":
        client = _try_pipe_client(auto_start=False, timeout_seconds=2.0)
        if client is not None:
            try:
                exists_resp = client.call_function("exists", [name], timeout_seconds=10.0)
                raw_exists = exists_resp.get("value")
                try:
                    exists_code = int(raw_exists)
                except (TypeError, ValueError):
                    exists_code = -1
                if exists_code != 2:
                    raise IgorError(
                        f"'{name}' is not a global variable "
                        f"(Igor exists() code {raw_exists})"
                    )
                not_a_string = f"IGOR_MCP_{uuid.uuid4().hex}"
                str_resp = client.call_function(
                    "StrVarOrDefault", [name, not_a_string], timeout_seconds=10.0
                )
                str_val = str_resp.get("value")
                if isinstance(str_val, str) and str_val != not_a_string:
                    return {"name": name, "type": "string", "value": str_val}
                num_resp = client.call_function(
                    "NumVarOrDefault", [name, 0.0], timeout_seconds=10.0
                )
                num_val = num_resp.get("value")
                if isinstance(num_val, (int, float)) and not isinstance(num_val, bool):
                    if math.isfinite(num_val):
                        value: float | str = float(num_val)
                    elif math.isnan(num_val):
                        value = "NaN"
                    else:
                        value = "Inf" if num_val > 0 else "-Inf"
                elif num_val is None:
                    value = "NaN"
                else:
                    value = _json_numeric_value(str(num_val))
                return {"name": name, "type": "numeric", "value": value}
            except igor_pipe.PipeNotAvailableError as exc:
                if mode == "pipe":
                    raise IgorError(str(exc)) from exc
            except igor_pipe.IgorPipeError as exc:
                raise IgorError(str(exc)) from exc
            finally:
                client.close()
        elif mode == "pipe":
            raise IgorError(
                "No active Igor Pro named-pipe instance found (IGOR_TRANSPORT=pipe)."
            )

    def operation(app, _version):
        escaped_name = _igor_escape(name)
        not_a_string = f"IGOR_MCP_{uuid.uuid4().hex}"
        output = _query(
            app,
            "%d|%.15g|%s",
            f'exists("{escaped_name}"), NumVarOrDefault("{escaped_name}", NaN), '
            f'StrVarOrDefault("{escaped_name}", "{not_a_string}")',
        )
        exists_code, numeric_value, string_value = output.split("|", 2)
        if exists_code.strip() != "2":
            raise IgorError(
                f"'{name}' is not a global variable "
                f"(Igor exists() code {exists_code.strip()})"
            )
        if string_value != not_a_string:
            return {"name": name, "type": "string", "value": string_value}
        return {
            "name": name,
            "type": "numeric",
            "value": _json_numeric_value(numeric_value),
        }

    return _with_igor(operation)


def _export_wave_slice(
    app,
    escaped_name: str,
    format_string: str,
    last_row: int,
) -> list[str]:
    """Export a wave slice to a temporary file via wfprintf and return non-empty lines."""
    file_ref = _helper_name("igorMcpFile")
    with _temporary_output_file() as path:
        line = (
            f"Variable {file_ref}; "
            f'Open {file_ref} as "{_igor_escape(path)}"; '
            f'wfprintf {file_ref}, "{format_string}" '
            f'/R=[0,{last_row}] $"{escaped_name}"; '
            f"Close {file_ref}; KillVariables/Z {file_ref}"
        )
        _raw_execute(app, line)
        text = _normalize_newlines(_read_tmp_file(path))
    return [l for l in text.splitlines() if l.strip()]


def _validate_numeric_wave(
    app, version: float, wave_name: str, escaped_name: str
) -> tuple[int, int, int]:
    """Verify that a wave exists, is numeric, and has <= 2 dimensions.

    Returns (wave_type, point_count, dimensions).
    """
    if _query(app, "%d", f'WaveExists($"{escaped_name}")').strip() != "1":
        raise IgorError(f"Wave '{wave_name}' not found")
    wave_type = int(_query(app, "%d", f'WaveType($"{escaped_name}")').strip())
    if version >= _WAVE_TYPE_SELECTOR_VERSION:
        wave_class = int(
            _query(app, "%d", f'WaveType($"{escaped_name}", 1)').strip()
        )
        class_names = {
            0: "invalid",
            2: "text",
            3: "data-folder-reference",
            4: "wave-reference",
        }
        if wave_class != 1:
            class_name = class_names.get(wave_class, f"unknown class {wave_class}")
            raise IgorError(
                f"Wave '{wave_name}' is a {class_name} wave; "
                "only numeric waves are supported"
            )
    elif wave_type == 0:
        # In Igor 6.0, plain WaveType returns zero for a text wave or a
        # missing wave. WaveExists above has already excluded the latter.
        raise IgorError(
            f"Wave '{wave_name}' is a text wave; only numeric waves are supported"
        )
    point_count = int(_query(app, "%d", f'numpnts($"{escaped_name}")').strip())
    dimensions = int(_query(app, "%d", f'WaveDims($"{escaped_name}")').strip())
    # Igor 6 reports WaveDims=0 both for a null reference and for a valid
    # zero-row wave. WaveExists above distinguishes those cases.
    if dimensions > 2 or (dimensions == 0 and point_count > 0):
        raise IgorError(
            f"Wave '{wave_name}' has {dimensions} dimensions; only 1D and 2D waves are supported"
        )
    return wave_type, point_count, dimensions


def _get_2d_wave_data(
    app,
    wave_name: str,
    escaped_name: str,
    wave_type: int,
    point_count: int,
    is_complex: bool,
    max_points: int,
) -> WaveDataResult:
    """Read metadata and matrix values from a 2D numeric wave."""
    num_rows = int(_query(app, "%d", f'DimSize($"{escaped_name}", 0)').strip())
    num_cols = int(_query(app, "%d", f'DimSize($"{escaped_name}", 1)').strip())
    x_offset = float(_query(app, "%.15g", f'DimOffset($"{escaped_name}", 0)').strip())
    x_delta = float(_query(app, "%.15g", f'DimDelta($"{escaped_name}", 0)').strip())
    y_offset = float(_query(app, "%.15g", f'DimOffset($"{escaped_name}", 1)').strip())
    y_delta = float(_query(app, "%.15g", f'DimDelta($"{escaped_name}", 1)').strip())
    x_units = _query(app, "%s", f'WaveUnits($"{escaped_name}", 0)').strip()
    y_units = _query(app, "%s", f'WaveUnits($"{escaped_name}", 1)').strip()

    max_rows = max(1, max_points // max(1, num_cols)) if num_cols > 0 else 0
    rows_to_read = min(num_rows, max_rows)
    matrix_values: list[list[float | str]] = []

    if rows_to_read > 0 and num_cols > 0:
        format_string = "\\t".join(["%.17g"] * num_cols) + "\\n"
        lines = _export_wave_slice(app, escaped_name, format_string, rows_to_read - 1)
        for line_str in lines:
            tokens = [t.strip() for t in line_str.split("\t") if t.strip()]
            matrix_values.append([_json_numeric_value(t) for t in tokens])

    return {
        "name": wave_name,
        "npnts": point_count,
        "dimensions": 2,
        "rows": num_rows,
        "columns": num_cols,
        "x_offset": x_offset,
        "x_delta": x_delta,
        "y_offset": y_offset,
        "y_delta": y_delta,
        "x_units": x_units,
        "y_units": y_units,
        "wave_type_code": wave_type,
        "complex": is_complex,
        "truncated": rows_to_read < num_rows,
        "values": matrix_values,
    }


def _get_1d_wave_data(
    app,
    wave_name: str,
    escaped_name: str,
    wave_type: int,
    point_count: int,
    dimensions: int,
    is_complex: bool,
    max_points: int,
) -> WaveDataResult:
    """Read metadata and values from a 1D (or empty 0D) numeric wave."""
    count = min(point_count, max_points)
    values: list[float | str | dict[str, float | str]] = []
    if count:
        format_string = "%.17g,%.17g\\n" if is_complex else "%.17g\\n"
        rows = _export_wave_slice(app, escaped_name, format_string, count - 1)
        if len(rows) != count:
            raise IgorError(
                f"Igor returned {len(rows)} wave rows; expected {count}"
            )
        for row in rows:
            if is_complex:
                components = row.split(",", 1)
                if len(components) != 2:
                    raise IgorError("Igor returned malformed complex-wave data")
                values.append(
                    {
                        "real": _json_numeric_value(components[0]),
                        "imag": _json_numeric_value(components[1]),
                    }
                )
            else:
                values.append(_json_numeric_value(row))

    return {
        "name": wave_name,
        "npnts": point_count,
        "dimensions": dimensions,
        "wave_type_code": wave_type,
        "complex": is_complex,
        "truncated": count < point_count,
        "values": values,
    }


def _pipe_call_int(
    client: igor_pipe.IgorPipeClient,
    func_name: str,
    args: list[Any],
    *,
    timeout_seconds: float = 10.0,
) -> int:
    resp = client.call_function(func_name, args, timeout_seconds=timeout_seconds)
    val = resp.get("value")
    try:
        return int(val)
    except (TypeError, ValueError) as exc:
        raise IgorError(
            f"Igor {func_name}() returned non-integer value: {val!r}"
        ) from exc


def _pipe_call_float(
    client: igor_pipe.IgorPipeClient,
    func_name: str,
    args: list[Any],
    *,
    timeout_seconds: float = 10.0,
) -> float:
    resp = client.call_function(func_name, args, timeout_seconds=timeout_seconds)
    val = resp.get("value")
    try:
        return float(val)
    except (TypeError, ValueError) as exc:
        raise IgorError(
            f"Igor {func_name}() returned non-numeric value: {val!r}"
        ) from exc


def _export_wave_slice_pipe(
    client: igor_pipe.IgorPipeClient,
    escaped_name: str,
    format_string: str,
    last_row: int,
) -> list[str]:
    """Export a wave slice over the native pipe to a temporary file via wfprintf."""
    file_ref = _helper_name("igorMcpFile")
    with _temporary_output_file() as path:
        cmd = (
            f"Variable {file_ref}; "
            f'Open {file_ref} as "{_igor_escape(path)}"; '
            f'wfprintf {file_ref}, "{format_string}" '
            f'/R=[0,{last_row}] $"{escaped_name}"; '
            f"Close {file_ref}; KillVariables/Z {file_ref}"
        )
        resp = client.execute_command(cmd, timeout_seconds=25.0)
        if resp.get("errorCode"):
            raise IgorError(
                f"Failed to export wave slice: "
                f"{resp.get('errorMessage') or resp.get('errorCode')}"
            )
        text = _normalize_newlines(_read_tmp_file(path))
    return [line for line in text.splitlines() if line.strip()]


def _get_wave_data_via_pipe(
    client: igor_pipe.IgorPipeClient,
    wave_name: str,
    max_points: int,
) -> WaveDataResult:
    """Read 1D or 2D numeric wave metadata and values over the native pipe."""
    wave_ref = {"wave": wave_name}
    try:
        exists = _pipe_call_int(client, "WaveExists", [wave_ref])
    except igor_pipe.PipeRPCError:
        exists = 0
    if exists != 1:
        raise IgorError(f"Wave '{wave_name}' not found")

    wave_type = _pipe_call_int(client, "WaveType", [wave_ref])
    wave_class = _pipe_call_int(client, "WaveType", [wave_ref, 1])
    class_names = {
        0: "invalid",
        2: "text",
        3: "data-folder-reference",
        4: "wave-reference",
    }
    if wave_class != 1:
        class_name = class_names.get(wave_class, f"unknown class {wave_class}")
        raise IgorError(
            f"Wave '{wave_name}' is a {class_name} wave; "
            "only numeric waves are supported"
        )
    point_count = _pipe_call_int(client, "numpnts", [wave_ref])
    dimensions = _pipe_call_int(client, "WaveDims", [wave_ref])
    if dimensions > 2 or (dimensions == 0 and point_count > 0):
        raise IgorError(
            f"Wave '{wave_name}' has {dimensions} dimensions; only 1D and 2D waves are supported"
        )

    is_complex = bool(wave_type & 0x01)
    escaped_name = _igor_escape(wave_name)

    if dimensions == 2:
        num_rows = _pipe_call_int(client, "DimSize", [wave_ref, 0])
        num_cols = _pipe_call_int(client, "DimSize", [wave_ref, 1])
        x_offset = _pipe_call_float(client, "DimOffset", [wave_ref, 0])
        x_delta = _pipe_call_float(client, "DimDelta", [wave_ref, 0])
        y_offset = _pipe_call_float(client, "DimOffset", [wave_ref, 1])
        y_delta = _pipe_call_float(client, "DimDelta", [wave_ref, 1])
        x_units = str(
            client.call_function("WaveUnits", [wave_ref, 0], timeout_seconds=10.0).get(
                "value", ""
            )
            or ""
        ).strip()
        y_units = str(
            client.call_function("WaveUnits", [wave_ref, 1], timeout_seconds=10.0).get(
                "value", ""
            )
            or ""
        ).strip()

        max_rows = max(1, max_points // max(1, num_cols)) if num_cols > 0 else 0
        rows_to_read = min(num_rows, max_rows)
        matrix_values: list[list[float | str]] = []

        if rows_to_read > 0 and num_cols > 0:
            format_string = "\\t".join(["%.17g"] * num_cols) + "\\n"
            lines = _export_wave_slice_pipe(
                client, escaped_name, format_string, rows_to_read - 1
            )
            for line_str in lines:
                tokens = [t.strip() for t in line_str.split("\t") if t.strip()]
                matrix_values.append([_json_numeric_value(t) for t in tokens])

        return {
            "name": wave_name,
            "npnts": point_count,
            "dimensions": 2,
            "rows": num_rows,
            "columns": num_cols,
            "x_offset": x_offset,
            "x_delta": x_delta,
            "y_offset": y_offset,
            "y_delta": y_delta,
            "x_units": x_units,
            "y_units": y_units,
            "wave_type_code": wave_type,
            "complex": is_complex,
            "truncated": rows_to_read < num_rows,
            "values": matrix_values,
        }

    count = min(point_count, max_points)
    values: list[float | str | dict[str, float | str]] = []
    if count:
        format_string = "%.17g,%.17g\\n" if is_complex else "%.17g\\n"
        rows = _export_wave_slice_pipe(client, escaped_name, format_string, count - 1)
        if len(rows) != count:
            raise IgorError(
                f"Igor returned {len(rows)} wave rows; expected {count}"
            )
        for row in rows:
            if is_complex:
                components = row.split(",", 1)
                if len(components) != 2:
                    raise IgorError("Igor returned malformed complex-wave data")
                values.append(
                    {
                        "real": _json_numeric_value(components[0]),
                        "imag": _json_numeric_value(components[1]),
                    }
                )
            else:
                values.append(_json_numeric_value(row))

    return {
        "name": wave_name,
        "npnts": point_count,
        "dimensions": dimensions,
        "wave_type_code": wave_type,
        "complex": is_complex,
        "truncated": count < point_count,
        "values": values,
    }


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS, structured_output=True)
def get_wave_data(
    wave_name: Annotated[
        str,
        Field(description="Name or full path of a 1D or 2D numeric wave (e.g. 'root:Run_001', 'matrix0')."),
    ],
    max_points: Annotated[
        int,
        Field(description="Maximum points (or matrix cells) to return (0 to 10,000). Defaults to 1000."),
    ] = 1000,
) -> WaveDataResult:
    """Return values and metadata from a 1D or 2D numeric wave, up to 10,000 points.

    The result includes the wave's full `npnts`, dimensionality, numeric type
    code, complex flag, scaling properties, and whether the returned values were truncated.
    Higher-dimensional (>2D), text, wave-reference, and data-folder-reference waves
    are rejected explicitly.
    """
    if not isinstance(wave_name, str) or not wave_name.strip():
        raise IgorError("wave_name must be a non-empty wave name or full path")
    max_points = _validate_max_points(max_points)

    mode = _transport_mode()
    if mode != "com":
        client = _try_pipe_client(auto_start=False, timeout_seconds=2.0)
        if client is not None:
            try:
                return _get_wave_data_via_pipe(client, wave_name, max_points)
            except igor_pipe.PipeNotAvailableError as exc:
                if mode == "pipe":
                    raise IgorError(str(exc)) from exc
            except igor_pipe.IgorPipeError as exc:
                raise IgorError(str(exc)) from exc
            finally:
                client.close()
        elif mode == "pipe":
            raise IgorError(
                "No active Igor Pro named-pipe instance found (IGOR_TRANSPORT=pipe)."
            )

    def operation(app, version):
        escaped_name = _igor_escape(wave_name)
        wave_type, point_count, dimensions = _validate_numeric_wave(
            app, version, wave_name, escaped_name
        )
        is_complex = bool(wave_type & 0x01)

        if dimensions == 2:
            return _get_2d_wave_data(
                app, wave_name, escaped_name, wave_type, point_count, is_complex, max_points
            )
        return _get_1d_wave_data(
            app, wave_name, escaped_name, wave_type, point_count, dimensions, is_complex, max_points
        )

    return _with_igor(operation)


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS, structured_output=True)
def list_waves(
    folder: Annotated[
        str,
        Field(description="Igor data folder path ending in a colon (default 'root:')."),
    ] = "root:",
) -> dict[str, object]:
    """List waves in a folder and leave Igor's active data folder unchanged.

    Igor 9 and later accept a data-folder reference in WaveList. Igor 6-8 use
    the path-aware CountObjects and GetIndexedObjName functions.
    """
    if not isinstance(folder, str) or not folder.strip():
        raise IgorError("folder must be a non-empty Igor data-folder path")

    mode = _transport_mode()
    if mode != "com":
        client = _try_pipe_client(auto_start=False, timeout_seconds=2.0)
        if client is not None:
            try:
                exists_resp = client.call_function(
                    "DataFolderExists", [folder], timeout_seconds=10.0
                )
                raw_exists = exists_resp.get("value")
                try:
                    folder_exists = int(raw_exists) == 1
                except (TypeError, ValueError):
                    folder_exists = False
                if not folder_exists:
                    raise IgorError(f"Data folder '{folder}' does not exist")
                escaped_folder = _igor_escape(folder)
                list_resp = client.execute_command(
                    f'fprintf 0, "%s", WaveList("*", ";", "", $"{escaped_folder}")',
                    timeout_seconds=10.0,
                )
                if list_resp.get("errorCode"):
                    raise IgorError(
                        f"Failed to list waves in '{folder}': "
                        f"{list_resp.get('errorMessage') or list_resp.get('errorCode')}"
                    )
                raw_list = str(list_resp.get("results", "") or "")
                if len(raw_list.encode("utf-8")) > _MAX_OUTPUT_BYTES:
                    raise IgorError("wave listing exceeded the server output limit")
                names = [name for name in raw_list.split(";") if name.strip()]
                return {"folder": folder, "waves": names}
            except igor_pipe.PipeNotAvailableError as exc:
                if mode == "pipe":
                    raise IgorError(str(exc)) from exc
            except igor_pipe.IgorPipeError as exc:
                raise IgorError(str(exc)) from exc
            finally:
                client.close()
        elif mode == "pipe":
            raise IgorError(
                "No active Igor Pro named-pipe instance found (IGOR_TRANSPORT=pipe)."
            )

    def operation(app, version):
        escaped_folder = _igor_escape(folder)
        if _query(app, "%d", f'DataFolderExists("{escaped_folder}")').strip() != "1":
            raise IgorError(f"Data folder '{folder}' does not exist")
        names = _list_wave_names_in_folder(app, version, folder)
        return {"folder": folder, "waves": names}

    return _with_igor(operation)


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS)
def list_procedures() -> str:
    """List loaded procedure files and their user-defined functions."""
    mode = _transport_mode()
    if mode != "com":
        client = _try_pipe_client(auto_start=False, timeout_seconds=2.0)
        if client is not None:
            try:
                proc_resp = client.call_function(
                    "WinList", ["*", ";", "WIN:128"], timeout_seconds=10.0
                )
                procedure_list = str(proc_resp.get("value", "") or "")
                procedures = [
                    procedure
                    for procedure in procedure_list.split(";")
                    if procedure.strip()
                ]
                if not procedures:
                    return "(no procedure windows found)"
                lines = []
                for procedure in procedures:
                    lines.append(f"{procedure}:")
                    fn_resp = client.call_function(
                        "FunctionList",
                        ["*", ";", f"WIN:{procedure}"],
                        timeout_seconds=10.0,
                    )
                    functions = str(fn_resp.get("value", "") or "")
                    names = [name for name in functions.split(";") if name.strip()]
                    if names:
                        lines.extend(f"  {name}" for name in names)
                    else:
                        lines.append("  (no user-defined functions)")
                result = "\n".join(lines)
                if len(result.encode("utf-8")) > _MAX_OUTPUT_BYTES:
                    raise IgorError("procedure listing exceeded the server output limit")
                return result
            except igor_pipe.PipeNotAvailableError as exc:
                if mode == "pipe":
                    raise IgorError(str(exc)) from exc
            except igor_pipe.IgorPipeError as exc:
                raise IgorError(str(exc)) from exc
            finally:
                client.close()
        elif mode == "pipe":
            raise IgorError(
                "No active Igor Pro named-pipe instance found (IGOR_TRANSPORT=pipe)."
            )

    def operation(app, _version):
        procedure_list = _query(app, "%s", 'WinList("*", ";", "WIN:128")')
        procedures = [procedure for procedure in procedure_list.split(";") if procedure.strip()]
        if not procedures:
            return "(no procedure windows found)"
        lines = []
        for procedure in procedures:
            lines.append(f"{procedure}:")
            escaped_procedure = _igor_escape(procedure)
            functions = _query(
                app, "%s", f'FunctionList("*", ";", "WIN:{escaped_procedure}")'
            )
            names = [name for name in functions.split(";") if name.strip()]
            lines.extend(f"  {name}" for name in names) if names else lines.append(
                "  (no user-defined functions)"
            )
        result = "\n".join(lines)
        if len(result.encode("utf-8")) > _MAX_OUTPUT_BYTES:
            raise IgorError("procedure listing exceeded the server output limit")
        return result

    return _with_igor(operation)


def _pipe_capabilities_list(caps_resp: dict[str, Any]) -> list[str]:
    """Normalize the capabilities response from igor.capabilities to a list of strings."""
    if not isinstance(caps_resp, dict):
        return []
    for key in ("methods", "capabilities"):
        raw = caps_resp.get(key)
        if isinstance(raw, list):
            return [str(item) for item in raw if item is not None]
        if isinstance(raw, dict):
            return sorted(str(k) for k, v in raw.items() if v)
    return sorted(str(k) for k in caps_resp.keys())


def _pipe_version_and_experiment(
    client: igor_pipe.IgorPipeClient,
    caps_resp: dict[str, Any] | None = None,
) -> tuple[float | None, str, str]:
    """Query Igor version and current experiment name over a connected pipe client."""
    version_num: float | None = None
    version_str = ""
    if isinstance(caps_resp, dict):
        raw_ver = caps_resp.get("igorVersion") or caps_resp.get("version")
        if raw_ver is not None:
            version_str = str(raw_ver).strip()
    if not version_str:
        try:
            ver_resp = client.call_function("IgorVersion", [], timeout_seconds=5.0)
            raw_val = ver_resp.get("value")
            if raw_val is not None:
                version_num = float(raw_val)
                version_str = f"{version_num:.2f}"
        except (igor_pipe.IgorPipeError, ValueError, TypeError):
            pass
    if version_num is None and version_str:
        parsed = igor_pipe._parse_version_tuple(version_str)
        if parsed is not None:
            version_num = float(f"{parsed[0]}.{parsed[1]:02d}")
        else:
            try:
                version_num = float(version_str)
            except ValueError:
                version_num = None

    experiment = ""
    try:
        exp_resp = client.call_function("IgorInfo", [1], timeout_seconds=5.0)
        experiment = str(exp_resp.get("value", "") or "").strip()
    except igor_pipe.IgorPipeError:
        pass
    return version_num, (version_str or (f"{version_num:.2f}" if version_num is not None else "")), (experiment or "(untitled)")


def _probe_com_reachable() -> bool:
    """Return True if an active Igor COM object is currently reachable without starting Igor."""
    try:
        with _igor_application(auto_start=False):
            return True
    except IgorError:
        return False


@mcp.tool(annotations=_STARTUP_ANNOTATIONS, structured_output=True)
def ensure_igor() -> EnsureIgorResult:
    """Attach to Igor or start the configured Igor executable if needed.

    This is the explicit startup/preflight operation. Call it instead of
    searching the filesystem for Igor. It reuses an active Igor pipe or COM
    instance and otherwise starts ``IGOR_PRO_EXE`` or the maintained default
    Igor Pro 10 path (with ``/JSONRPC`` on Igor 10.04+ when transport is not
    forced to COM). It does not open an experiment, load a procedure, or run
    a user command.
    """
    candidates = _igor_executable_candidates()
    executable_path = str(candidates[0]) if candidates else None
    mode = _transport_mode()

    if mode != "com":
        client = _try_pipe_client(auto_start=False, timeout_seconds=1.5)
        if client is not None:
            try:
                pipe_name = client.pipe_name
                # Require the initial RPC probe to succeed before treating the pipe as live.
                caps_resp = client.capabilities(timeout_seconds=3.0)
                version_num, version_str, experiment = _pipe_version_and_experiment(
                    client, caps_resp
                )
                if version_num is not None:
                    _ensure_supported_igor_version(version_num)
                return {
                    "com_reachable": _probe_com_reachable() if mode == "auto" else False,
                    "igor_version": version_str or "10.04",
                    "current_experiment": experiment,
                    "executable_path": executable_path,
                    "source_path": _SOURCE_PATH,
                    "transport": "pipe",
                    "pipe_reachable": True,
                    "pipe_name": pipe_name,
                }
            except igor_pipe.IgorPipeError as exc:
                if mode == "pipe":
                    raise IgorError(
                        f"Igor Pro named-pipe probe failed (IGOR_TRANSPORT=pipe): {exc}"
                    ) from exc
            finally:
                client.close()

    def operation(app, version):
        experiment = _query(app, "%s", "IgorInfo(1)").strip()
        pipe_reachable = False
        pipe_name: str | None = None
        if mode != "com":
            pipe_client = _try_pipe_client(auto_start=False, timeout_seconds=1.0)
            if pipe_client is not None:
                try:
                    pipe_client.capabilities(timeout_seconds=2.0)
                    pipe_reachable = True
                    pipe_name = pipe_client.pipe_name
                except igor_pipe.IgorPipeError:
                    pipe_reachable = False
                    pipe_name = None
                finally:
                    pipe_client.close()
        return {
            "com_reachable": True,
            "igor_version": f"{version:.2f}",
            "current_experiment": experiment or "(untitled)",
            "executable_path": executable_path,
            "source_path": _SOURCE_PATH,
            "transport": "pipe" if pipe_reachable else "com",
            "pipe_reachable": pipe_reachable,
            "pipe_name": pipe_name,
        }

    return _with_igor(operation)


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS, structured_output=True)
def get_igor_dialogs() -> list[dict[str, object]]:
    """List visible modal/dialog windows owned by Igor Pro.

    This includes compiler dialogs, file choosers, procedure choosers, and
    other native or Qt dialogs. It does not click buttons or dismiss dialogs.
    """
    return _igor_dialogs()


@mcp.tool(annotations=_MUTATING_ANNOTATIONS, structured_output=True)
def dismiss_igor_dialogs() -> dict[str, int]:
    """Close visible Igor modal/dialog windows using their window close action.

    This explicit recovery tool requests a normal close after review and never
    clicks destructive buttons such as Save, Delete, or Quit Igor.
    """
    dialogs = _igor_dialogs()
    result = _dismiss_igor_dialogs(dialogs)
    return {"found": len(dialogs), **result}


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS, structured_output=True)
def get_igor_compilation_errors() -> list[dict[str, object]]:
    """Read visible Igor compiler dialogs without closing them.

    Supports Igor's "Function Compilation Error" and "Procedure Scan Error"
    dialogs. Returns each diagnostic, highlighted source line, and button labels.
    It deliberately ignores other application windows and remains callable
    while a COM command is blocked.
    """
    return _compilation_error_dialogs()


@mcp.tool(annotations=_MUTATING_ANNOTATIONS, structured_output=True)
def dismiss_igor_compilation_errors() -> dict[str, int]:
    """Click the Quit button in each visible Igor compiler dialog.

    This dismisses only "Function Compilation Error" and "Procedure Scan Error"
    dialogs; it does not close Igor or interact with other dialog types. It remains
    callable while a COM command is blocked.
    """
    dialogs = _compilation_error_dialogs()
    dismissed = sum(_dismiss_compilation_error_dialog(dialog) for dialog in dialogs)
    return {"found": len(dialogs), "dismissed": dismissed}


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS, structured_output=True)
def igor_status() -> IgorStatusResult:
    """Report source identity, Pipe/COM availability, and Igor compatibility.

    This is a read-only diagnostic only; it does not start Igor. Use
    ``ensure_igor`` when Igor must be attached or launched.
    """
    mode = _transport_mode()
    if mode != "com":
        client = _try_pipe_client(auto_start=False, timeout_seconds=1.5)
        if client is not None:
            try:
                pipe_name = client.pipe_name
                # Require the initial RPC probe to succeed before reporting pipe_reachable=True.
                caps_resp = client.capabilities(timeout_seconds=3.0)
                pipe_capabilities = _pipe_capabilities_list(caps_resp)
                version_num, version_str, experiment = _pipe_version_and_experiment(
                    client, caps_resp
                )
                compatible = (
                    version_num >= _MIN_IGOR_VERSION
                    if version_num is not None
                    else True
                )
                return {
                    "com_reachable": _probe_com_reachable() if mode == "auto" else False,
                    "compatible": compatible,
                    "igor_version": version_str or None,
                    "minimum_igor_version": f"{_MIN_IGOR_VERSION:.1f}",
                    "current_experiment": experiment,
                    "capture_mode": "native JSON-RPC pipe",
                    "source_path": _SOURCE_PATH,
                    "portable_object_name_limit": _MAX_IGOR_OBJECT_NAME_LENGTH,
                    "error": None if compatible else (
                        f"Igor Pro {_MIN_IGOR_VERSION:.1f} or newer is required"
                    ),
                    "transport": "pipe",
                    "pipe_reachable": True,
                    "pipe_name": pipe_name,
                    "pipe_capabilities": pipe_capabilities,
                }
            except igor_pipe.IgorPipeError as exc:
                if mode == "pipe":
                    return {
                        "com_reachable": False,
                        "compatible": False,
                        "igor_version": None,
                        "minimum_igor_version": f"{_MIN_IGOR_VERSION:.1f}",
                        "current_experiment": None,
                        "capture_mode": _capture_mode,
                        "source_path": _SOURCE_PATH,
                        "portable_object_name_limit": _MAX_IGOR_OBJECT_NAME_LENGTH,
                        "error": str(exc),
                        "transport": "none",
                        "pipe_reachable": False,
                        "pipe_name": None,
                        "pipe_capabilities": None,
                    }
            finally:
                client.close()
        elif mode == "pipe":
            return {
                "com_reachable": False,
                "compatible": False,
                "igor_version": None,
                "minimum_igor_version": f"{_MIN_IGOR_VERSION:.1f}",
                "current_experiment": None,
                "capture_mode": _capture_mode,
                "source_path": _SOURCE_PATH,
                "portable_object_name_limit": _MAX_IGOR_OBJECT_NAME_LENGTH,
                "error": "No active Igor Pro named-pipe instance found (IGOR_TRANSPORT=pipe).",
                "transport": "none",
                "pipe_reachable": False,
                "pipe_name": None,
                "pipe_capabilities": None,
            }

    def operation(app, version):
        experiment = _query(app, "%s", "IgorInfo(1)").strip()
        compatible = version >= _MIN_IGOR_VERSION
        return {
            "com_reachable": True,
            "compatible": compatible,
            "igor_version": f"{version:.2f}",
            "minimum_igor_version": f"{_MIN_IGOR_VERSION:.1f}",
            "current_experiment": experiment or "(untitled)",
            "capture_mode": _capture_mode,
            "source_path": _SOURCE_PATH,
            "portable_object_name_limit": _MAX_IGOR_OBJECT_NAME_LENGTH,
            "error": None if compatible else (
                f"Igor Pro {_MIN_IGOR_VERSION:.1f} or newer is required"
            ),
            "transport": "com",
            "pipe_reachable": False,
            "pipe_name": None,
            "pipe_capabilities": None,
        }

    try:
        return _with_igor(
            operation,
            require_supported=False,
            auto_start=False,
            check_dialogs=False,
        )
    except IgorError as exc:
        return {
            "com_reachable": False,
            "compatible": False,
            "igor_version": None,
            "minimum_igor_version": f"{_MIN_IGOR_VERSION:.1f}",
            "current_experiment": None,
            "capture_mode": _capture_mode,
            "source_path": _SOURCE_PATH,
            "portable_object_name_limit": _MAX_IGOR_OBJECT_NAME_LENGTH,
            "error": str(exc),
            "transport": "none",
            "pipe_reachable": False,
            "pipe_name": None,
            "pipe_capabilities": None,
        }


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS, structured_output=True)
def query_igor_help(
    query: Annotated[str, Field(description="Official Igor command or syntax to search.")],
    topic: Annotated[str | None, Field(description="Optional manual filename/topic filter.")] = None,
    max_results: Annotated[int, Field(ge=1, le=5, description="Number of excerpts, 1 to 5.")] = 3,
) -> dict[str, object]:
    """Search bundled official Igor documentation for syntax and operation flags."""
    return igor_knowledge.search(query, topic=topic, max_results=max_results, collection="manuals")


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS, structured_output=True)
def search_igor_knowledge(
    query: Annotated[str, Field(description="Workflow, fitting, plotting, NumPy translation, or gotcha to look up.")],
    max_results: Annotated[int, Field(ge=1, le=5, description="Number of excerpts, 1 to 5.")] = 3,
) -> dict[str, object]:
    """Search bundled curated Igor guides, independent of any workspace or wiki."""
    return igor_knowledge.search(query, max_results=max_results, collection="guides")


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS, structured_output=True)
def read_igor_document(
    document_id: Annotated[str, Field(description="Exact document_id from a knowledge search result.")],
    max_chars: Annotated[int, Field(ge=500, le=50000, description="Maximum characters to return, 500 to 50000.")] = 12000,
    offset: Annotated[int, Field(ge=0, description="Character offset from the start of the document, usually read_offset from a search result.")] = 0,
) -> dict[str, object]:
    """Read a bounded page from a bundled manual or curated guide."""
    return igor_knowledge.read_document(document_id, max_chars=max_chars, offset=offset)


@mcp.tool(annotations=_MUTATING_ANNOTATIONS, structured_output=True)
def capture_session(
    title: Annotated[str, Field(description="Title for a caller-written Markdown session summary.")] = "",
    summary: Annotated[str, Field(description="Caller-written summary to preserve in session memory.")] = "",
) -> dict[str, object]:
    """Save a caller-written human/assistant Markdown summary.

    Automatic MCP call observations are stored separately in the same local
    session-memory directory. This tool stores the supplied summary as a new
    Markdown file and never replaces prior records.
    """
    return capture_markdown_session(title, summary, source_path=_SOURCE_PATH)


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS, structured_output=True)
def search_session_memory(
    query: Annotated[str, Field(min_length=1, max_length=4096, description="Text to search in saved summaries and tool-call observations.")],
    record_type: Annotated[str, Field(description="Which records to search: 'all', 'summary', or 'tool_call'.")] = "all",
    max_results: Annotated[int, Field(ge=1, le=20, description="Maximum number of matching records to return.")] = 5,
) -> dict[str, object]:
    """Search saved summaries and bounded automatic tool-call observations."""
    return _search_session_memory(query, record_type=record_type, max_results=max_results)


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS, structured_output=True)
def read_session_memory(
    record_id: Annotated[str, Field(min_length=32, max_length=32, description="Opaque 32-character record_id returned by search_session_memory.")],
    max_chars: Annotated[int, Field(ge=500, le=50000, description="Maximum characters to return, 500 to 50000.")] = 12000,
) -> dict[str, object]:
    """Read one saved summary or tool-call observation by its opaque record ID."""
    return _read_session_memory(record_id, max_chars=max_chars)


def _report_source(app) -> str:
    filename = _query(app, "%s", "IgorInfo(12)").strip()
    if not filename:
        return ""
    _execute_command(app, "PathInfo home")
    directory = _query(app, "%s", 'ParseFilePath(5, S_path, "*", 0, 0)').strip()
    if not directory:
        raise IgorError("Cannot identify the saved experiment directory")
    return str(Path(directory, filename).resolve())


def _report_number(info: str, key: str, default: float) -> float:
    match = re.search(r"(?:^|;)" + re.escape(key) + r"\(x\)=([^;]+)", info)
    return float(match.group(1)) if match else default


def _report_key(info: str, key: str) -> str:
    match = re.search(r"(?:^|;)" + re.escape(key) + r":([^;]*)", info)
    return match.group(1) if match else ""


def _report_data_hash(x, y) -> str:
    return igor_reports.data_fingerprint(x, y)


def _report_wave(app, version, path: str):
    escaped = _igor_escape(path)
    kind, count, dimensions = _validate_numeric_wave(app, version, path, escaped)
    if dimensions != 1 or kind & 1 or not 1 <= count <= igor_reports.MAX_POINTS:
        raise IgorError(f"'{path}': reports require real 1D waves with 1 to 10,000 points; data are never truncated")
    result = _get_1d_wave_data(app, path, escaped, kind, count, dimensions, False, count)
    return [value if isinstance(value, (float, int)) and math.isfinite(value) else None for value in result["values"]]


def _report_graph(app, version, graph: str) -> dict:
    """Export a deliberately bounded XY subset; never silently flatten complex plots."""
    if not igor_reports.NAME.fullmatch(graph):
        raise IgorError("Reports require portable top-level graph names")
    g = _igor_escape(graph)
    if _query(app, "%d", f'WinType("{g}")').strip() != "1":
        raise IgorError(f"Graph '{graph}' does not exist")
    for function in ("ImageNameList", "ContourNameList"):
        if _query(app, "%s", f'{function}("{g}", ";")').strip():
            raise IgorError(f"'{graph}': image and contour plots are not supported by the portable editor")
    names = [name for name in _query(app, "%s", f'TraceNameList("{g}", ";", 27)').split(";") if name]
    if not 1 <= len(names) <= 20:
        raise IgorError(f"'{graph}': reports support 1 to 20 XY traces")
    figure = {"graph": graph, "title": graph, "caption": "", "edited": False, "legend": True, "traces": []}
    for name in names:
        if not igor_reports.TRACE.fullmatch(name):
            raise IgorError(f"'{graph}': trace '{name}' needs a portable trace name")
        n = _igor_escape(name)
        info = _query(app, "%s", f'TraceInfo("{g}", "{n}", 0)')
        if (_report_key(info, "XAXIS") != "bottom" or _report_key(info, "YAXIS") != "left"
                or _report_key(info, "TYPE") not in ("", "0")
                or _report_key(info, "YRANGE") != "[*]"
                or _report_key(info, "XRANGE") not in ("", "[*]")
                or _report_key(info, "ERRORBARS")):
            raise IgorError(f"'{graph}/{name}': only full-wave XY traces on bottom/left axes without error bars are supported")
        for key, default in (("offset", None), ("muloffset", None), ("zColor", 0), ("hideTrace", 0)):
            if default is None:
                match = re.search(r"(?:^|;)" + key + r"\(x\)=\(([^)]+)\)", info)
                if match:
                    values = [float(v) for v in match.group(1).split(",")]
                    expected = 0 if key == "offset" else 1
                    if any(value != expected for value in values):
                        raise IgorError(f"'{graph}/{name}': transformed traces are not supported")
            elif _report_number(info, key, default) != default:
                raise IgorError(f"'{graph}/{name}': hidden or variable-color traces are not supported")
        mode = int(_report_number(info, "mode", 0))
        if mode not in (0, 2, 3, 4):
            raise IgorError(f"'{graph}/{name}': unsupported trace display mode {mode}")
        yref = f'TraceNameToWaveRef("{g}", "{n}")'
        xref = f'XWaveRefFromTrace("{g}", "{n}")'
        ypath = _query(app, "%s", f'GetWavesDataFolder({yref}, 2)').strip()
        y = _report_wave(app, version, ypath)
        xpath = ""
        if _query(app, "%d", f"WaveExists({xref})").strip() == "1":
            xpath = _query(app, "%s", f'GetWavesDataFolder({xref}, 2)').strip()
            x = _report_wave(app, version, xpath)
        else:
            offset = float(_query(app, "%.17g", f"DimOffset({yref}, 0)").strip())
            delta = float(_query(app, "%.17g", f"DimDelta({yref}, 0)").strip())
            x = [offset + index * delta for index in range(len(y))]
        if len(x) != len(y):
            raise IgorError(f"'{graph}/{name}': X and Y waves must have equal lengths")
        rgb = re.search(r"(?:^|;)rgb\(x\)=\((\d+),(\d+),(\d+)\)", info)
        color = "#" + "".join(f"{round(int(v) * 255 / 65535):02x}" for v in rgb.groups()) if rgb else "#000000"
        figure["traces"].append({"name": name, "label": name, "y_wave": ypath, "x_wave": xpath,
                                  "x": x, "y": y, "data_sha256": _report_data_hash(x, y),
                                  "color": color, "width": max(.1, _report_number(info, "lSize", 1)), "mode": mode})
    for key, axis in (("x", "bottom"), ("y", "left")):
        info = _query(app, "%s", f'AxisInfo("{g}", "{axis}")')
        if _report_key(info, "ISCAT") not in ("", "0"):
            raise IgorError(f"'{graph}': category axes are not supported")
        _execute_command(app, f"GetAxis/W={graph}/Q {axis}")
        lo, hi = _query(app, "%.17g|%.17g", "V_min, V_max").strip().split("|")
        figure[key] = {"label": _query(app, "%s", f'AxisLabel("{g}", "{axis}")').strip(),
                       "min": float(lo), "max": float(hi), "log": bool(_report_number(info, "log", 0))}
    with tempfile.TemporaryDirectory(prefix="igor-report-") as directory:
        path = Path(directory, "preview.png")
        _execute_command(app, f'SavePICT/WIN={graph}/E=-5/B=144 as "{_igor_escape(str(path))}"')
        if not path.is_file() or path.stat().st_size > 10_000_000:
            raise IgorError("Igor did not produce a bounded PNG preview")
        figure["original_png"] = base64.b64encode(path.read_bytes()).decode("ascii")
    return figure


def _run_report_operation(operation):
    """Use the same admission, deadline, and unknown-outcome policy as commands."""
    if not _command_worker_lock.acquire(timeout=_COMMAND_WORKER_LOCK_WAIT_SECONDS):
        return {"ok": False, "error": "Another Igor operation is running; no report operation was started", "outcome_known": True}
    deferred = False

    def defer():
        nonlocal deferred
        deferred = True

    def failure(error, **details):
        return {"ok": False, "error": error, **details}

    def direct():
        def callback(app, version):
            if version < 9:
                raise IgorError("Editable HTML reports require Igor Pro 9 or newer")
            _command_start_marker.event.set()
            return operation(app, version)
        return _with_igor(callback, auto_start=False)

    try:
        return _run_igor_operation_in_worker_locked(
            direct, failure_factory=failure, action_description="HTML report operation",
            on_timeout=defer, on_complete=_command_worker_lock.release,
            timeout_seconds=_IGOR_REPORT_TIMEOUT_SECONDS,
        )
    finally:
        if not deferred:
            _command_worker_lock.release()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False), structured_output=True)
def create_html_report(
    output_path: Annotated[str, Field(description="Absolute path for a new .html file; existing files are never overwritten.")],
    graphs: Annotated[list[str], Field(min_length=1, max_length=20, description="Explicit list of existing top-level XY graph names to include.")],
    title: Annotated[str, Field(max_length=100000, description="Report title.")] = "Igor graph report",
    notes: Annotated[str, Field(max_length=100000, description="Plain-text report notes.")] = "",
    editing_mode: Annotated[Literal["igor", "browser"], Field(description="igor: click a preview to edit an independent single-graph PXP copy in Igor (requires the compiled Windows launcher). browser: use the standalone simplified browser editor.")] = "igor",
) -> dict[str, object]:
    """Create an HTML report with native Igor editing by default, only on request.

    Default igor mode requires a saved experiment and one-time compiled Windows
    link handler installation (igor_report_links.py --install). Each figure is
    exported as a separate SaveGraphCopy PXP. Clicking it opens only that copy
    in a normal independent Igor instance; no Python, MCP, or AI runs on clicks.
    Ctrl+S saves the copy and updates its adjacent PNG via a native Igor hook;
    reload the HTML for updated previews. The full source experiment is unchanged.
    Recipients need Igor, the handler, and the registered HTML/figure package.

    Explicit browser mode is self-contained: recipients edit axes, styles, titles,
    notes, and captions, and download a new editable HTML copy. No Igor, MCP,
    AI, helper installation, or network is needed to edit the report. Original
    Igor PNGs and full numerical trace data are embedded. Browser rendering is
    simplified, not a pixel-exact Igor graph editor. Supports real 1D XY traces
    with bottom/left axes, up to 10,000 points per trace and 100,000 total.
    Unsupported plot types fail explicitly. Does not save the Igor experiment.
    """
    path = Path(output_path)
    if not path.is_absolute() or path.suffix.lower() != ".html":
        raise IgorError("output_path must be an absolute .html path")
    if path.exists() or not path.parent.is_dir():
        raise IgorError("Choose a new report filename in an existing directory")
    if not 1 <= len(graphs) <= 20 or len(set(graphs)) != len(graphs) or any(not igor_reports.NAME.fullmatch(g) for g in graphs):
        raise IgorError("Provide 1 to 20 unique portable graph names")
    igor_reports.text(title, "title", 100000)
    igor_reports.text(notes, "notes", 100000)

    def operation(app, version):
        source = _report_source(app)
        if editing_mode == "igor" and not source:
            raise IgorError("Save the Igor experiment before creating a native linked report, or explicitly choose editing_mode='browser' for a snapshot")
        data = {"format": igor_reports.FORMAT, "title": title, "notes": notes,
                "source_experiment": source, "figures": [_report_graph(app, version, graph) for graph in graphs]}
        if editing_mode == "igor":
            import igor_report_links
            data["igor_executable"] = str(_igor_executable_candidates()[0])
            def export_graph(graph, destination):
                _execute_command(app, f"GetWindow {graph} hide")
                was_hidden = _query(app, "%d", "V_value").strip() == "1"
                try:
                    _execute_command(app, f"DoWindow/HIDE=0 {graph}")
                    _execute_command(app, f'SaveGraphCopy/W={graph} as "{_igor_escape(str(destination))}"')
                finally:
                    if was_hidden:
                        _execute_command(app, f"DoWindow/HIDE=1 {graph}")
            igor_report_links.prepare_native_assets(path, data, export_graph)
            igor_report_links.register_report(path, data)
        else:
            igor_reports.write_report(path, data)
        return {"ok": True, "file_path": str(path.resolve()), "graphs": graphs,
                "editing_mode": editing_mode,
                "recipient_requirements": "Igor and the compiled Windows figure launcher; keep the HTML and its _figures folder together. Click opens one independent figure copy; Ctrl+S saves it and updates its preview." if editing_mode == "igor" else "A modern browser only. Save edited HTML downloads a new editable copy.",
                "source_experiment": data["source_experiment"]}

    return _run_report_operation(operation)


@mcp.tool(annotations=_MUTATING_ANNOTATIONS, structured_output=True)
def apply_html_report(
    file_path: Annotated[str, Field(description="Absolute path to an edited HTML report. Apply only when explicitly requested.")],
) -> dict[str, object]:
    """Apply browser-edited axes and trace styles to their original Igor graphs.

    Requires the original saved experiment already open in Igor. Verifies its
    path, graph/trace identities, wave paths, and unchanged numeric data before
    applying any changes. Does not run HTML or JavaScript, change wave data,
    activate windows, or save the experiment. Titles, captions, legend labels,
    and report notes remain report-only. Save the experiment in Igor afterwards.
    """
    path = Path(file_path)
    if not path.is_absolute() or path.suffix.lower() != ".html":
        raise IgorError("file_path must be an absolute .html path")
    data = igor_reports.read_report(path)
    edited = [figure for figure in data["figures"] if figure["edited"]]
    if not edited:
        return {"ok": True, "graphs": [], "message": "No browser-edited figures to apply"}
    if not data["source_experiment"]:
        raise IgorError("This snapshot came from an unsaved experiment and cannot be applied back safely")

    def operation(app, version):
        if os.path.normcase(_report_source(app)) != os.path.normcase(data["source_experiment"]):
            raise IgorError("Open the original saved experiment before applying this report")
        commands = []
        for figure in edited:
            graph = figure["graph"]
            current = _report_graph(app, version, graph)
            old = [(t["name"], t["y_wave"], t["x_wave"], t["data_sha256"]) for t in current["traces"]]
            supplied = [(t["name"], t["y_wave"], t["x_wave"], t["data_sha256"]) for t in figure["traces"]]
            if old != supplied or any(_report_data_hash(t["x"], t["y"]) != t["data_sha256"] for t in figure["traces"]):
                raise IgorError(f"'{graph}': source traces or data differ; no graph styling was applied")
            for key, axis in (("x", "bottom"), ("y", "left")):
                settings = figure[key]
                commands.extend([f'Label/W={graph} {axis}, "{_igor_escape(settings["label"])}"',
                                 f'ModifyGraph/W={graph} log({axis})={int(settings["log"])}',
                                 f'SetAxis/W={graph} {axis}, {settings["min"]:.17g}, {settings["max"]:.17g}'])
            for trace in figure["traces"]:
                name = trace["name"]
                rgb = tuple(int(trace["color"][i:i+2], 16) * 257 for i in (1, 3, 5))
                commands.append(f'ModifyGraph/W={graph} rgb({name})=({rgb[0]},{rgb[1]},{rgb[2]}),lSize({name})={trace["width"]:.17g},mode({name})={trace["mode"]}')
        commands = [_validate_command(command) for command in commands]
        applied = 0
        try:
            for command in commands:
                _execute_command(app, command)
                applied += 1
        except Exception as exc:
            raise IgorError(f"Apply stopped after {applied} styling commands; changes may be partial. Inspect Igor before retrying. {exc}") from exc
        return {"ok": True, "graphs": [f["graph"] for f in edited], "experiment_saved": False,
                "message": "Applied axes and trace styles. Save the experiment in Igor to retain changes on disk. Report titles, captions and legend text stay in HTML."}

    return _run_report_operation(operation)


def _activate_report_window(experiment_name: str) -> bool:
    """Foreground Igor only for an explicit native-report edit action."""
    windows = []
    matching = []
    _command_start_marker.report_hwnd = None
    enum_type = WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @enum_type
    def callback(hwnd, _lparam):
        hwnd = int(hwnd)
        image = _process_image_path(_window_process_id(hwnd))
        if Path(image).name.casefold() in _IGOR_EXECUTABLE_NAMES:
            if _window_owner(hwnd) or _window_class(hwnd) in _DDE_HELPER_WINDOW_CLASSES:
                return True
            windows.append(hwnd)
            if experiment_name and experiment_name.casefold() in _window_text(hwnd).casefold():
                matching.append(hwnd)
        return True

    windll.user32.EnumWindows(callback, 0)
    if len(matching) == 1:
        windows = matching
    if len(windows) != 1:
        return False
    hwnd = wintypes.HWND(windows[0])
    _command_start_marker.report_hwnd = windows[0]
    if windll.user32.IsIconic(hwnd):
        windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE; explicitly requested by the click.
    elif not windll.user32.IsWindowVisible(hwnd):
        windll.user32.ShowWindow(hwnd, 5)  # SW_SHOW.
    requested = bool(windll.user32.SetForegroundWindow(hwnd))
    return requested or int(windll.user32.GetForegroundWindow() or 0) == windows[0]


@mcp.tool(annotations=_MUTATING_ANNOTATIONS, structured_output=True)
def open_report_graph(
    file_path: Annotated[str, Field(description="Absolute saved .pxp/.pxt path linked by the clicked report.")],
    graph: Annotated[str, Field(description="Exact portable graph name selected by the user.")],
    activate: Annotated[bool, Field(description="True for a user click to edit; false for refreshing previews.")] = True,
) -> dict[str, object]:
    """Open a report's experiment and select its graph in Igor's native editor.

    Only use for an explicit request or native report click. Unlike ordinary
    automation, an edit click requests foreground activation. Start/attach with
    ensure_igor first. If a different experiment is open, normal Igor Open
    behavior preserves unsaved-work prompts; no dialog is dismissed and no
    experiment is saved automatically. A registered local link handler can call
    this tool without an AI or external MCP host.
    """
    path = Path(file_path)
    if not path.is_absolute() or path.suffix.lower() not in _EXPERIMENT_FILE_EXTENSIONS or not path.is_file():
        raise IgorError("The linked .pxp/.pxt experiment must exist at an absolute path")
    if not igor_reports.NAME.fullmatch(graph):
        raise IgorError("Invalid portable graph name")
    path = path.resolve()

    def operation(app, _version):
        if os.path.normcase(_report_source(app)) != os.path.normcase(str(path)):
            app.LoadExperiment(_IGOR_LOAD_EXPERIMENT_FLAGS, _IGOR_LOAD_TYPE_OPEN, "", str(path))
            dialogs = _igor_dialogs()
            if dialogs:
                if activate:
                    _activate_report_window(_query(app, "%s", "IgorInfo(1)").strip())
                raise IgorError(_dialog_blocking_message(dialogs, "opening the linked experiment"))
            if os.path.normcase(_report_source(app)) != os.path.normcase(str(path)):
                raise IgorError("The linked experiment has not opened. Inspect Igor before retrying.")
        if _query(app, "%d", f'WinType("{graph}")').strip() != "1":
            raise IgorError(f"Graph '{graph}' is not present in the linked experiment")
        foreground = False
        if activate:
            _execute_command(app, f"DoWindow/F {graph}")
            foreground = _activate_report_window(path.stem)
        return {"ok": True, "file_path": str(path), "graph": graph, "activated": activate,
                "foreground": foreground, "window_handle": getattr(_command_start_marker, "report_hwnd", None), "server_pid": os.getpid(), "experiment_saved": False}

    return _run_report_operation(operation)


# ---------------------------------------------------------------------------
# Native Pipe + Hybrid MCP tools (Igor Pro 10.04+ with COM fallbacks)
# ---------------------------------------------------------------------------


def _png_dimensions(png_bytes: bytes) -> tuple[int | None, int | None]:
    """Extract (width, height) from a PNG IHDR chunk, or (None, None)."""
    if len(png_bytes) >= 24 and png_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        width, height = struct.unpack(">II", png_bytes[16:24])
        return int(width), int(height)
    return None, None


def _capture_window_error(error: str, *, transport: str | None = None) -> CallToolResult:
    payload: dict[str, object] = {"ok": False, "error": error}
    if transport is not None:
        payload["transport"] = transport
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))],
        structuredContent=payload,
        isError=True,
    )


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS, structured_output=False)
def capture_window(
    window: Annotated[
        str | None,
        Field(
            description=(
                "Igor window name to capture (graph, table, layout, panel, etc.), "
                "or omit to capture the frontmost window."
            )
        ),
    ] = None,
    render: Annotated[
        str,
        Field(
            description=(
                "Native pipe rendering mode: 'auto', 'screen', 'screenGraphOnly', or 'export'."
            )
        ),
    ] = "auto",
    max_dimension: Annotated[
        int,
        Field(
            ge=64,
            le=8192,
            description="Maximum width or height in pixels (64 to 8192, default 1200).",
        ),
    ] = 1200,
    format: Annotated[
        str,
        Field(description="Image format to return (currently 'png')."),
    ] = "png",
) -> CallToolResult:
    """Capture an Igor Pro window as a PNG image.

    Uses Igor Pro 10.04+ native JSON-RPC ``igor.window.capture`` when available,
    or falls back to COM ``SavePICT/E=-5/B=144`` into a temporary file.
    """
    fmt = format.strip().lower() if isinstance(format, str) else ""
    if fmt != "png":
        return _capture_window_error(f"Unsupported image format {format!r}; only 'png' is supported.")
    if render not in _CAPTURE_WINDOW_RENDER_MODES:
        allowed = ", ".join(sorted(_CAPTURE_WINDOW_RENDER_MODES))
        return _capture_window_error(
            f"Unsupported render mode {render!r}; use one of: {allowed}."
        )
    if (
        not isinstance(max_dimension, int)
        or isinstance(max_dimension, bool)
        or not (64 <= max_dimension <= 8192)
    ):
        return _capture_window_error(
            "max_dimension must be an integer between 64 and 8192."
        )
    if window is not None and (
        not isinstance(window, str)
        or not window.strip()
        or "\x00" in window
        or "\r" in window
        or "\n" in window
    ):
        return _capture_window_error(
            "window must be a non-empty string without null bytes or newlines."
        )

    mode = _transport_mode()
    if mode != "com":
        client = _try_pipe_client(auto_start=False, timeout_seconds=2.0)
        if client is not None:
            try:
                resp = client.capture_window(
                    window=window,
                    render=render,
                    max_dimension=max_dimension,
                    fmt=fmt,
                )
                b64_png = resp.get("data") or resp.get("image") or resp.get("png")
                if not isinstance(b64_png, str) or not b64_png.strip():
                    return _capture_window_error(
                        "Igor pipe window capture returned no base64 PNG data.",
                        transport="pipe",
                    )
                b64_png = b64_png.strip()
                try:
                    raw_bytes = base64.b64decode(b64_png, validate=True)
                except Exception as exc:
                    return _capture_window_error(
                        f"Igor pipe window capture returned invalid base64 data: {exc}",
                        transport="pipe",
                    )
                if not raw_bytes or len(raw_bytes) > _MAX_CAPTURE_IMAGE_BYTES:
                    return _capture_window_error(
                        "Captured PNG image is empty or exceeds the 20 MB limit.",
                        transport="pipe",
                    )
                png_width, png_height = _png_dimensions(raw_bytes)
                width = resp.get("width")
                if width is None:
                    width = png_width
                height = resp.get("height")
                if height is None:
                    height = png_height
                meta: dict[str, object] = {
                    "ok": True,
                    "window": resp.get("window", window),
                    "width": width,
                    "height": height,
                    "format": fmt,
                    "render": resp.get("render", render),
                    "byte_length": len(raw_bytes),
                    "transport": "pipe",
                }
                return CallToolResult(
                    content=[
                        TextContent(type="text", text=json.dumps(meta)),
                        ImageContent(type="image", data=b64_png, mimeType="image/png"),
                    ],
                    structuredContent=meta,
                )
            except igor_pipe.PipeNotAvailableError as exc:
                if mode == "pipe":
                    return _capture_window_error(str(exc), transport="pipe")
            except igor_pipe.IgorPipeError as exc:
                return _capture_window_error(str(exc), transport="pipe")
            finally:
                client.close()
        elif mode == "pipe":
            return _capture_window_error(
                "No active Igor Pro named-pipe instance found (IGOR_TRANSPORT=pipe).",
                transport="pipe",
            )

    def com_operation(app, _version):
        target_win = window.strip() if isinstance(window, str) and window.strip() else ""
        if not target_win:
            # WinName(0, 7): topmost graph (1), table (2), or layout (4)
            target_win = _query(app, "%s", "WinName(0, 7)").strip()
            if not target_win:
                raise IgorError("No graph, table, or layout window is open to capture.")
        else:
            if not _SAFE_WINDOW_NAME_PATTERN.fullmatch(target_win):
                raise IgorError(
                    f"Window name '{target_win}' contains unsupported characters for COM SavePICT."
                )
            win_type = _query(app, "%d", f'WinType("{_igor_escape(target_win)}")').strip()
            if win_type == "0":
                raise IgorError(f"Window '{target_win}' does not exist.")
        if not _SAFE_WINDOW_NAME_PATTERN.fullmatch(target_win):
            raise IgorError(
                f"Window name '{target_win}' contains unsupported characters for COM SavePICT."
            )

        with tempfile.TemporaryDirectory(prefix="igor-capture-") as directory:
            png_path = Path(directory, "capture.png")
            _execute_command(
                app,
                f'SavePICT/E=-5/B=144/WIN={target_win} as "{_igor_escape(str(png_path))}"',
            )
            if not png_path.is_file():
                raise IgorError(f"SavePICT did not produce a PNG file for window '{target_win}'.")
            raw_bytes = png_path.read_bytes()
            if not raw_bytes or len(raw_bytes) > _MAX_CAPTURE_IMAGE_BYTES:
                raise IgorError("Captured PNG image is empty or exceeds the 20 MB limit.")
            b64_png = base64.b64encode(raw_bytes).decode("ascii")

        png_width, png_height = _png_dimensions(raw_bytes)
        meta: dict[str, object] = {
            "ok": True,
            "window": target_win,
            "width": png_width,
            "height": png_height,
            "format": fmt,
            "byte_length": len(raw_bytes),
            "transport": "com",
        }
        return CallToolResult(
            content=[
                TextContent(type="text", text=json.dumps(meta)),
                ImageContent(type="image", data=b64_png, mimeType="image/png"),
            ],
            structuredContent=meta,
        )

    try:
        return _with_igor(com_operation, auto_start=False)
    except (IgorError, OSError) as exc:
        return _capture_window_error(str(exc), transport="com")


def _failed_call_function_result(
    error: str,
    *,
    command_started: bool,
    outcome_known: bool = False,
    dialog_details: list[dict[str, object]] | None = None,
    compilation_errors: list[dict[str, object]] | None = None,
    elapsed_seconds: float | None = None,
    transport: str = "pipe",
) -> dict[str, object]:
    details = list(dialog_details or [])
    result: dict[str, object] = {
        "ok": False,
        "type": None,
        "value": None,
        "history": "",
        "results": "",
        "elapsed_seconds": elapsed_seconds,
        "transport": transport,
        "command_started": command_started,
        "outcome_known": outcome_known,
        "error": error,
    }
    if details:
        result["dialogs_open"] = len(details)
        result["dialog_details"] = details
    if compilation_errors:
        result["compilation_errors"] = list(compilation_errors)
    return result


@mcp.tool(annotations=_MUTATING_ANNOTATIONS, structured_output=True)
def call_function(
    name: Annotated[
        str,
        Field(description="Igor built-in or user-defined function name to invoke (e.g. 'IgorVersion', 'MyFunc')."),
    ],
    args: Annotated[
        list[Any] | None,
        Field(
            description=(
                "Optional list of positional arguments to pass to the Igor function. "
                "Typed objects: wave ref {'wave': 'root:myWave'}, "
                "data folder ref {'datafolder': 'root:myFolder'}, "
                "complex number {'real': 1.5, 'imag': -2.5}, "
                "and special floats 'NaN', 'Infinity', '-Infinity'."
            )
        ),
    ] = None,
    kwargs: Annotated[
        dict[str, Any] | None,
        Field(description="Optional dict of keyword arguments to pass to the Igor function."),
    ] = None,
    timeout_seconds: Annotated[
        float,
        Field(description="Maximum seconds to wait for the function call (1.0 to 600.0, default 30.0)."),
    ] = 30.0,
) -> dict[str, object]:
    """Invoke an Igor Pro function directly over the native JSON-RPC pipe.

    Requires Igor Pro 10.04+ native JSON-RPC named-pipe transport. Runs on the
    serialized command worker and never replays after dispatch.

    Argument encoding in ``args`` / ``kwargs``:
    - Wave reference: ``{"wave": "root:myWave"}``
    - Data folder reference: ``{"datafolder": "root:myFolder"}``
    - Complex number: ``{"real": 1.5, "imag": -2.5}``
    - Special floats: ``"NaN"``, ``"Infinity"``, ``"-Infinity"``
    """
    if not isinstance(name, str) or not name.strip() or "\x00" in name:
        raise IgorError("name must be a non-empty string without null bytes")
    clamped_timeout = _clamp_timeout(timeout_seconds, default=30.0)
    if _transport_mode() == "com":
        return _failed_call_function_result(
            "call_function requires Igor Pro 10.04+ native JSON-RPC pipe transport "
            "(IGOR_TRANSPORT is currently set to 'com').",
            command_started=False,
            outcome_known=True,
            transport="com",
        )

    if not _command_worker_lock.acquire(timeout=_COMMAND_WORKER_LOCK_WAIT_SECONDS):
        return _failed_call_function_result(
            "Another Igor command is already running; no function call was started. "
            "Inspect Igor state and retry after the active request finishes.",
            command_started=False,
            outcome_known=True,
            transport="pipe",
        )

    release_deferred = False

    def defer_release() -> None:
        nonlocal release_deferred
        release_deferred = True

    def release_after_worker() -> None:
        _command_worker_lock.release()

    def direct() -> dict[str, object]:
        client = _try_pipe_client(auto_start=False, timeout_seconds=2.0)
        if client is None:
            return _failed_call_function_result(
                "call_function requires an active Igor Pro 10.04+ native JSON-RPC named-pipe connection.",
                command_started=False,
                outcome_known=True,
                transport="none",
            )
        try:
            before = _igor_dialogs()
            if before:
                comp_errors = _capture_compilation_errors(before, phase="before_command")
                return _failed_call_function_result(
                    _dialog_blocking_message(before, "before function call"),
                    command_started=False,
                    outcome_known=True,
                    dialog_details=before,
                    compilation_errors=comp_errors,
                    transport="pipe",
                )
            marker = getattr(_command_start_marker, "event", None)
            if marker is not None:
                marker.set()
            t0 = time.monotonic()
            try:
                resp = client.call_function(
                    name.strip(),
                    args=args,
                    kwargs=kwargs,
                    timeout_seconds=clamped_timeout,
                )
            except igor_pipe.PipeNotAvailableError as exc:
                return _failed_call_function_result(
                    str(exc),
                    command_started=False,
                    outcome_known=True,
                    transport="pipe",
                )
            except igor_pipe.PipeDispatchError as exc:
                # Covers PipeTimeoutError and post-dispatch disconnect: never replay.
                elapsed = round(time.monotonic() - t0, 4)
                dialogs = _igor_dialogs()
                comp_errors = _capture_compilation_errors(dialogs, phase="after_command")
                return _failed_call_function_result(
                    str(exc),
                    command_started=True,
                    outcome_known=False,
                    dialog_details=dialogs,
                    compilation_errors=comp_errors,
                    elapsed_seconds=elapsed,
                    transport="pipe",
                )
            except igor_pipe.PipeRPCError as exc:
                elapsed = round(time.monotonic() - t0, 4)
                return _failed_call_function_result(
                    str(exc),
                    command_started=True,
                    outcome_known=True,
                    elapsed_seconds=elapsed,
                    transport="pipe",
                )

            elapsed = round(time.monotonic() - t0, 4)
            return {
                "ok": True,
                "type": resp.get("type"),
                "value": resp.get("value"),
                "history": str(resp.get("history", "") or ""),
                "results": str(resp.get("results", "") or ""),
                "elapsed_seconds": resp.get("elapsedSeconds", elapsed),
                "transport": "pipe",
                "command_started": True,
                "outcome_known": True,
                "error": None,
            }
        finally:
            client.close()

    try:
        return _run_igor_operation_in_worker_locked(
            direct,
            failure_factory=_failed_call_function_result,
            action_description=f"function call '{name}'",
            on_timeout=defer_release,
            on_complete=release_after_worker,
            timeout_seconds=clamped_timeout + _PIPE_WORKER_TIMEOUT_GRACE_SECONDS,
        )
    finally:
        if not release_deferred:
            _command_worker_lock.release()


@mcp.tool(annotations=_MUTATING_ANNOTATIONS, structured_output=True)
def abort_igor(
    timeout_seconds: Annotated[
        float,
        Field(description="Maximum seconds to wait for cancellation response (1.0 to 600.0, default 10.0)."),
    ] = 10.0,
) -> dict[str, object]:
    """Request cancellation of the currently running Igor Pro operation.

    Connects over an independent named-pipe connection without acquiring the
    COM or command-worker locks, so it remains callable while a long command
    is in flight.
    """
    clamped_timeout = _clamp_timeout(timeout_seconds, default=10.0)
    if _transport_mode() == "com":
        return {
            "ok": False,
            "cancelled": False,
            "transport": "com",
            "error": "abort_igor requires Igor Pro 10.04+ native JSON-RPC pipe transport (IGOR_TRANSPORT is 'com').",
        }
    try:
        resp = igor_pipe.cancel_igor_request(timeout_seconds=clamped_timeout)
        return {
            "ok": True,
            "cancelled": bool(resp.get("cancelled", True)),
            "details": resp,
            "transport": "pipe",
            "error": None,
        }
    except igor_pipe.IgorPipeError as exc:
        return {
            "ok": False,
            "cancelled": False,
            "details": None,
            "transport": "pipe",
            "error": str(exc),
        }


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS, structured_output=True)
def get_igor_state() -> dict[str, object]:
    """Inspect Igor Pro's current busy/dialog/compilation state without locking.

    Never acquires the COM or command-worker locks. Queries Igor Pro 10.04+
    ``igor.app.state`` on an independent pipe connection (which responds even
    when a modal dialog is open) and merges/falls back to Win32 dialog and
    compiler-error inspection.
    """
    pipe_state: dict[str, Any] | None = None
    transport = "win32"
    if _transport_mode() != "com":
        client = _try_pipe_client(auto_start=False, timeout_seconds=2.0)
        if client is not None:
            try:
                pipe_state = client.get_state(timeout_seconds=5.0)
                transport = "pipe"
            except igor_pipe.IgorPipeError:
                pipe_state = None
            finally:
                client.close()

    dialogs = _igor_dialogs()
    compilation_errors = _capture_compilation_errors(dialogs, phase="state_check")
    busy = pipe_state.get("busy") if isinstance(pipe_state, dict) else None
    if busy is None and isinstance(pipe_state, dict):
        busy = bool(
            pipe_state.get("procedureRunning")
            or pipe_state.get("loadingExperiment")
            or pipe_state.get("runningRequest")
            or pipe_state.get("queuedRequests")
            or pipe_state.get("otherQueuedRequests")
        )
    modal_dialog = bool(
        isinstance(pipe_state, dict)
        and (pipe_state.get("modalDialog") or pipe_state.get("dialog"))
    ) or bool(dialogs)
    return {
        "ok": True,
        "transport": transport,
        "pipe_state": pipe_state,
        "busy": busy,
        "modal_dialog": modal_dialog,
        "dialogs_open": len(dialogs),
        "dialog_titles": [str(dialog.get("title", "")) for dialog in dialogs],
        "dialog_details": dialogs,
        "compilation_errors": compilation_errors,
        "error": None,
    }


@mcp.tool(annotations=_MUTATING_ANNOTATIONS, structured_output=True)
def open_procedure(
    file_path: Annotated[
        str,
        Field(description="Absolute path to an existing Igor procedure file (.ipf)."),
    ],
) -> dict[str, object]:
    """Open an existing procedure file in Igor Pro without triggering a file chooser.

    Validates that ``file_path`` is an absolute path to an existing ``.ipf`` file
    on disk before executing ``OpenProc "<path>"`` via pipe or COM.
    """
    if (
        not isinstance(file_path, str)
        or not file_path.strip()
        or "\x00" in file_path
        or "\r" in file_path
        or "\n" in file_path
    ):
        raise IgorError(
            "file_path must be a non-empty absolute path without null bytes or newlines"
        )
    if not os.path.isabs(file_path):
        raise IgorError("file_path must be an absolute path")
    if not os.path.isfile(file_path):
        raise IgorError(f"Procedure file does not exist: {file_path}")

    resolved_path = Path(file_path).resolve()
    if resolved_path.suffix.casefold() != ".ipf":
        raise IgorError("file_path must name an Igor .ipf procedure file")
    resolved = str(resolved_path)
    cmd = f'OpenProc "{_igor_escape(resolved)}"'
    res = _execute_igor_in_worker(cmd)
    return {
        "ok": res["ok"],
        "file_path": resolved,
        "output": res["output"],
        "transport": res.get("transport", "com"),
        "dialogs_open": res["dialogs_open"],
        "dialog_details": res["dialog_details"],
        "compilation_errors": res["compilation_errors"],
        "error": res["error"],
    }


@mcp.tool(annotations=_MUTATING_ANNOTATIONS, structured_output=True)
def close_procedure(
    file_paths: Annotated[
        list[str],
        Field(
            min_length=1,
            description="List of procedure file paths to close in Igor Pro.",
        ),
    ],
) -> dict[str, object]:
    """Close one or more open procedure windows over the native JSON-RPC pipe.

    Requires Igor Pro 10.04+ native JSON-RPC named-pipe transport.
    """
    if not isinstance(file_paths, list) or not file_paths or any(
        not isinstance(p, str)
        or not p.strip()
        or "\x00" in p
        or "\r" in p
        or "\n" in p
        for p in file_paths
    ):
        raise IgorError(
            "file_paths must be a non-empty list of path strings without null bytes or newlines"
        )

    if _transport_mode() == "com":
        return {
            "ok": False,
            "closed": [],
            "reopened": [],
            "not_closed": list(file_paths),
            "compile": None,
            "transport": "com",
            "error": "close_procedure requires Igor Pro 10.04+ native JSON-RPC pipe transport (IGOR_TRANSPORT is 'com').",
        }

    def failure_factory(error: str, **_details) -> dict[str, object]:
        return {
            "ok": False,
            "closed": [],
            "reopened": [],
            "not_closed": list(file_paths),
            "compile": None,
            "transport": "pipe",
            "error": error,
        }

    if not _command_worker_lock.acquire(timeout=_COMMAND_WORKER_LOCK_WAIT_SECONDS):
        return failure_factory(
            "Another Igor command is already running; no procedures were closed. "
            "Inspect Igor state and retry after the active request finishes."
        )

    release_deferred = False

    def defer_release() -> None:
        nonlocal release_deferred
        release_deferred = True

    def release_after_worker() -> None:
        _command_worker_lock.release()

    def direct() -> dict[str, object]:
        client = _try_pipe_client(auto_start=False, timeout_seconds=2.0)
        if client is None:
            return {
                "ok": False,
                "closed": [],
                "reopened": [],
                "not_closed": list(file_paths),
                "compile": None,
                "transport": "none",
                "error": "close_procedure requires an active Igor Pro 10.04+ native JSON-RPC named-pipe connection.",
            }
        try:
            marker = getattr(_command_start_marker, "event", None)
            if marker is not None:
                marker.set()
            resp = client.close_procedures(file_paths, timeout_seconds=30.0)
            return {
                "ok": True,
                "closed": resp.get("closed", []),
                "reopened": resp.get("reopened", []),
                "not_closed": resp.get("notClosed", resp.get("not_closed", [])),
                "compile": resp.get("compile"),
                "transport": "pipe",
                "error": None,
            }
        except igor_pipe.IgorPipeError as exc:
            return failure_factory(str(exc))
        finally:
            client.close()

    try:
        return _run_igor_operation_in_worker_locked(
            direct,
            failure_factory=failure_factory,
            action_description="close_procedure",
            on_timeout=defer_release,
            on_complete=release_after_worker,
            timeout_seconds=30.0 + _PIPE_WORKER_TIMEOUT_GRACE_SECONDS,
        )
    finally:
        if not release_deferred:
            _command_worker_lock.release()


@mcp.tool(annotations=_MUTATING_ANNOTATIONS, structured_output=True)
def compile_procedures() -> dict[str, object]:
    """Compile all open Igor Pro procedure files and report structured diagnostics.

    Over Igor Pro 10.04+ native JSON-RPC pipe, compiles synchronously and returns
    structured reload/error details. Over COM fallback, queues
    ``Execute/P/Q/Z "COMPILEPROCEDURES "`` and reports any visible compiler dialogs.
    """
    mode = _transport_mode()
    if mode != "com":
        def failure_factory(error: str, **_details) -> dict[str, object]:
            return {
                "ok": False,
                "compiled": False,
                "reloaded": [],
                "reload_failed": [],
                "errors": [],
                "transport": "pipe",
                "error": error,
            }

        if not _command_worker_lock.acquire(timeout=_COMMAND_WORKER_LOCK_WAIT_SECONDS):
            return failure_factory(
                "Another Igor command is already running; procedure compilation was not started. "
                "Inspect Igor state and retry after the active request finishes."
            )

        release_deferred = False

        def defer_release() -> None:
            nonlocal release_deferred
            release_deferred = True

        def release_after_worker() -> None:
            _command_worker_lock.release()

        pipe_unavailable = False

        def direct_pipe() -> dict[str, object] | None:
            nonlocal pipe_unavailable
            client = _try_pipe_client(auto_start=False, timeout_seconds=2.0)
            if client is None:
                pipe_unavailable = True
                return None
            try:
                marker = getattr(_command_start_marker, "event", None)
                if marker is not None:
                    marker.set()
                resp = client.compile_procedures(timeout_seconds=30.0)
                raw_errors = resp.get("errors")
                errors = list(raw_errors) if isinstance(raw_errors, list) else []
                singular_error = resp.get("error")
                if singular_error and not errors:
                    errors = [singular_error]
                compiled = bool(resp.get("compiled", not errors))
                return {
                    "ok": compiled and not errors,
                    "compiled": compiled,
                    "reloaded": resp.get("reloaded", []),
                    "reload_failed": resp.get(
                        "reloadFailed", resp.get("reload_failed", [])
                    ),
                    "errors": errors,
                    "details": resp,
                    "transport": "pipe",
                    "error": singular_error,
                }
            except igor_pipe.PipeNotAvailableError as exc:
                marker = getattr(_command_start_marker, "event", None)
                if marker is not None:
                    marker.clear()
                if mode == "pipe":
                    return failure_factory(str(exc))
                pipe_unavailable = True
                return None
            except igor_pipe.IgorPipeError as exc:
                return failure_factory(str(exc))
            finally:
                client.close()

        try:
            pipe_res = _run_igor_operation_in_worker_locked(
                direct_pipe,
                failure_factory=failure_factory,
                action_description="compile_procedures",
                on_timeout=defer_release,
                on_complete=release_after_worker,
                timeout_seconds=30.0 + _PIPE_WORKER_TIMEOUT_GRACE_SECONDS,
            )
        finally:
            if not release_deferred:
                _command_worker_lock.release()

        if pipe_res is not None:
            return pipe_res
        if pipe_unavailable and mode == "pipe":
            return failure_factory(
                "No active Igor Pro named-pipe instance found (IGOR_TRANSPORT=pipe)."
            )

    res = _execute_igor_in_worker('Execute/P/Q/Z "COMPILEPROCEDURES "')
    dialogs = _igor_dialogs()
    comp_errors = _capture_compilation_errors(dialogs, phase="compile_procedures")
    if not comp_errors:
        comp_errors = list(res.get("compilation_errors", []))
    return {
        "ok": res["ok"] and not comp_errors,
        "compiled": res["ok"] and not comp_errors,
        "reloaded": [],
        "reload_failed": [],
        "errors": comp_errors,
        "transport": "com",
        "error": res.get("error"),
        "note": (
            "COM queued compilation is asynchronous; call get_igor_compilation_errors "
            "if a compiler dialog appears after this call returns."
        ),
    }


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS, structured_output=True)
def get_compile_status() -> dict[str, object]:
    """Query whether Igor Pro procedures are currently compiled.

    Uses Igor Pro 10.04+ native JSON-RPC ``igor.procedures.compileStatus`` when
    available. Over COM fallback, checks visible Win32 compiler error dialogs and
    notes that exact side-effect-free compile status requires the native pipe.
    """
    mode = _transport_mode()
    if mode != "com":
        client = _try_pipe_client(auto_start=False, timeout_seconds=2.0)
        if client is not None:
            try:
                resp = client.get_compile_status(timeout_seconds=10.0)
                compiled = resp.get("compiled")
                needs_compile = resp.get(
                    "needsCompile", resp.get("needs_compile")
                )
                if needs_compile is None and isinstance(compiled, bool):
                    needs_compile = not compiled
                return {
                    "ok": True,
                    "compiled": compiled,
                    "needs_compile": needs_compile,
                    "compilation_errors": resp.get("errors", []),
                    "details": resp,
                    "transport": "pipe",
                    "error": None,
                }
            except igor_pipe.PipeNotAvailableError as exc:
                if mode == "pipe":
                    return {
                        "ok": False,
                        "compiled": None,
                        "needs_compile": None,
                        "compilation_errors": [],
                        "details": None,
                        "transport": "pipe",
                        "error": str(exc),
                    }
            except igor_pipe.IgorPipeError as exc:
                return {
                    "ok": False,
                    "compiled": None,
                    "needs_compile": None,
                    "compilation_errors": [],
                    "details": None,
                    "transport": "pipe",
                    "error": str(exc),
                }
            finally:
                client.close()
        elif mode == "pipe":
            return {
                "ok": False,
                "compiled": None,
                "needs_compile": None,
                "compilation_errors": [],
                "details": None,
                "transport": "pipe",
                "error": "No active Igor Pro named-pipe instance found (IGOR_TRANSPORT=pipe).",
            }

    compiler_dialogs = _compilation_error_dialogs()
    compiled = False if compiler_dialogs else None
    return {
        "ok": True,
        "compiled": compiled,
        "needs_compile": (not compiled) if isinstance(compiled, bool) else None,
        "compilation_errors": compiler_dialogs,
        "details": None,
        "transport": "com",
        "note": (
            "Exact side-effect-free compile status requires Igor Pro 10.04+ native "
            "JSON-RPC pipe transport; reporting visible Win32 compiler dialogs only."
        ),
        "error": None,
    }


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS, structured_output=True)
def list_windows(
    types: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional list of Igor window types to include: 'graph', 'table', "
                "'layout', 'notebook', 'panel', 'gizmo'. Defaults to all 6."
            )
        ),
    ] = None,
    match: Annotated[
        str | None,
        Field(description="Optional Igor wildcard match pattern (e.g. 'Graph*'). Defaults to '*'."),
    ] = None,
) -> dict[str, object]:
    """List open Igor Pro windows filtered by type and wildcard pattern."""
    if types is None:
        mask = _ALL_WINDOW_TYPES_MASK
    else:
        if not isinstance(types, list) or not types:
            raise IgorError("types must be a non-empty list of window type names or omitted")
        mask = 0
        for item in types:
            if not isinstance(item, str):
                raise IgorError(f"Invalid window type: {item!r}")
            bit = _WINDOW_TYPE_BITS.get(item.strip().lower())
            if bit is None:
                valid = ", ".join(sorted(_WINDOW_TYPE_BITS))
                raise IgorError(f"Unknown window type {item!r}; valid types are: {valid}")
            mask |= bit

    if match is not None:
        if not isinstance(match, str):
            raise IgorError("match pattern must be a string or omitted")
        if "\x00" in match or "\r" in match or "\n" in match:
            raise IgorError("match pattern cannot contain null bytes or newlines")
        pattern = match.strip() or "*"
    else:
        pattern = "*"
    options = f"WIN:{mask}"

    mode = _transport_mode()
    if mode != "com":
        client = _try_pipe_client(auto_start=False, timeout_seconds=2.0)
        if client is not None:
            try:
                resp = client.call_function(
                    "WinList", [pattern, ";", options], timeout_seconds=10.0
                )
                raw_list = str(resp.get("value", "") or "")
                windows = [w for w in raw_list.split(";") if w.strip()]
                return {
                    "ok": True,
                    "windows": windows,
                    "count": len(windows),
                    "transport": "pipe",
                }
            except igor_pipe.PipeNotAvailableError as exc:
                if mode == "pipe":
                    raise IgorError(str(exc)) from exc
            except igor_pipe.IgorPipeError as exc:
                raise IgorError(str(exc)) from exc
            finally:
                client.close()
        elif mode == "pipe":
            raise IgorError("No active Igor Pro named-pipe instance found (IGOR_TRANSPORT=pipe).")

    def operation(app, _version):
        raw_list = _query(
            app, "%s", f'WinList("{_igor_escape(pattern)}", ";", "{options}")'
        )
        windows = [w for w in raw_list.split(";") if w.strip()]
        return {
            "ok": True,
            "windows": windows,
            "count": len(windows),
            "transport": "com",
        }

    return _with_igor(operation, auto_start=False)


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS, structured_output=True)
def get_experiment_info() -> dict[str, object]:
    """Return the current Igor Pro experiment name, filename, folder path, and saved flag."""
    mode = _transport_mode()
    if mode != "com":
        client = _try_pipe_client(auto_start=False, timeout_seconds=2.0)
        if client is not None:
            try:
                name = str(
                    client.call_function("IgorInfo", [1], timeout_seconds=10.0).get(
                        "value", ""
                    )
                    or ""
                ).strip()
                file_name = str(
                    client.call_function("IgorInfo", [12], timeout_seconds=10.0).get(
                        "value", ""
                    )
                    or ""
                ).strip()
                folder_path = ""
                if file_name:
                    path_resp = client.execute_command(
                        'PathInfo home; fprintf 0, "%s", ParseFilePath(5, S_path, "*", 0, 0)',
                        timeout_seconds=10.0,
                    )
                    if path_resp.get("errorCode"):
                        raise IgorError(
                            f"Failed to resolve experiment home path: "
                            f"{path_resp.get('errorMessage') or path_resp.get('errorCode')}"
                        )
                    folder_path = str(path_resp.get("results", "") or "").strip()
                return {
                    "ok": True,
                    "name": name or "(untitled)",
                    "file_name": file_name,
                    "path": folder_path,
                    "saved": bool(file_name),
                    "transport": "pipe",
                }
            except igor_pipe.PipeNotAvailableError as exc:
                if mode == "pipe":
                    raise IgorError(str(exc)) from exc
            except igor_pipe.IgorPipeError as exc:
                raise IgorError(str(exc)) from exc
            finally:
                client.close()
        elif mode == "pipe":
            raise IgorError("No active Igor Pro named-pipe instance found (IGOR_TRANSPORT=pipe).")

    def operation(app, _version):
        name = _query(app, "%s", "IgorInfo(1)").strip()
        file_name = _query(app, "%s", "IgorInfo(12)").strip()
        folder_path = ""
        if file_name:
            _execute_command(app, "PathInfo home")
            folder_path = _query(
                app, "%s", 'ParseFilePath(5, S_path, "*", 0, 0)'
            ).strip()
        return {
            "ok": True,
            "name": name or "(untitled)",
            "file_name": file_name,
            "path": folder_path,
            "saved": bool(file_name),
            "transport": "com",
        }

    return _with_igor(operation, auto_start=False)


@mcp.tool(annotations=_READ_ONLY_ANNOTATIONS, structured_output=True)
def get_system_information() -> dict[str, object]:
    """Return Igor Pro version/platform info, OS/CPU info, Qt version, loaded XOPs, and thread count."""
    mode = _transport_mode()
    if mode != "com":
        client = _try_pipe_client(auto_start=False, timeout_seconds=2.0)
        if client is not None:
            try:
                info0 = str(
                    client.call_function("IgorInfo", [0], timeout_seconds=10.0).get(
                        "value", ""
                    )
                    or ""
                ).strip()
                info3 = str(
                    client.call_function("IgorInfo", [3], timeout_seconds=10.0).get(
                        "value", ""
                    )
                    or ""
                ).strip()
                qt_version = str(
                    client.call_function("IgorInfo", [6], timeout_seconds=10.0).get(
                        "value", ""
                    )
                    or ""
                ).strip()
                xops_raw = str(
                    client.call_function("IgorInfo", [10], timeout_seconds=10.0).get(
                        "value", ""
                    )
                    or ""
                ).strip()
                try:
                    proc_val = client.call_function(
                        "ThreadProcessorCount", [], timeout_seconds=10.0
                    ).get("value")
                except igor_pipe.PipeRPCError:
                    proc_resp = client.execute_command(
                        'fprintf 0, "%d", ThreadProcessorCount',
                        timeout_seconds=10.0,
                    )
                    if proc_resp.get("errorCode"):
                        raise IgorError(
                            f"Failed to query ThreadProcessorCount: "
                            f"{proc_resp.get('errorMessage') or proc_resp.get('errorCode')}"
                        )
                    proc_val = str(proc_resp.get("results", "") or "").strip()
                proc_count: int | str | None
                if isinstance(proc_val, (int, float)) and not isinstance(proc_val, bool):
                    proc_count = int(proc_val)
                elif isinstance(proc_val, str) and proc_val.strip().isdigit():
                    proc_count = int(proc_val.strip())
                else:
                    proc_count = proc_val
                loaded_xops = [x.strip() for x in xops_raw.split(";") if x.strip()]
                return {
                    "ok": True,
                    "igor_info": info0,
                    "system_info": info3,
                    "qt_version": qt_version,
                    "loaded_xops": loaded_xops,
                    "thread_processor_count": proc_count,
                    "transport": "pipe",
                }
            except igor_pipe.PipeNotAvailableError as exc:
                if mode == "pipe":
                    raise IgorError(str(exc)) from exc
            except igor_pipe.IgorPipeError as exc:
                raise IgorError(str(exc)) from exc
            finally:
                client.close()
        elif mode == "pipe":
            raise IgorError("No active Igor Pro named-pipe instance found (IGOR_TRANSPORT=pipe).")

    def operation(app, _version):
        info0 = _query(app, "%s", "IgorInfo(0)").strip()
        info3 = _query(app, "%s", "IgorInfo(3)").strip()
        qt_version = _query(app, "%s", "IgorInfo(6)").strip()
        xops_raw = _query(app, "%s", "IgorInfo(10)").strip()
        proc_raw = _query(app, "%d", "ThreadProcessorCount").strip()
        loaded_xops = [x.strip() for x in xops_raw.split(";") if x.strip()]
        proc_count: int | str = int(proc_raw) if proc_raw.isdigit() else proc_raw
        return {
            "ok": True,
            "igor_info": info0,
            "system_info": info3,
            "qt_version": qt_version,
            "loaded_xops": loaded_xops,
            "thread_processor_count": proc_count,
            "transport": "com",
        }

    return _with_igor(operation, auto_start=False)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
