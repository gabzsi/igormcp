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
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from ctypes import WINFUNCTYPE, byref, create_unicode_buffer, windll
from ctypes import wintypes
from typing import Annotated, Iterable, TypedDict

from pydantic import Field
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
import igor_knowledge
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
_COMMAND_WORKER_LOCK_WAIT_SECONDS = 1.0
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


class IgorFastMCP(FastMCP):
    """FastMCP wrapper for best-effort call memory and structured tool errors."""

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
        "For complex procedure work, inspect the data first, write one bounded "
        "procedure, then load/compile it and verify its outputs without blindly "
        "retrying after an unknown result. "
        "Never search the filesystem for Igor yourself: use ensure_igor when the "
        "user asks to open Igor or when a session needs an explicit attach/start "
        "preflight. ensure_igor uses the IGOR_PRO_EXE host setting and the "
        "maintained Igor Pro 10 default path. Do not use igor_status for startup; "
        "it is a read-only diagnostic and intentionally never starts Igor. "
        "Use open_igor_experiment to open an existing .pxp or .pxt through the "
        "normal Igor File > Open behavior; it requires an already-running Igor "
        "instance and never dismisses an unsaved-work prompt. "
        "Preserve Igor's current window state. When starting a new Igor process, "
        "show its main window without activation so the user can inspect or bring "
        "it forward later. Never restore or foreground an existing window during "
        "an MCP action; if the user minimizes Igor, leave it minimized. Do not use "
        "explicit activation commands such as DoWindow/F unless requested. Inspect "
        "visible dialogs with get_igor_dialogs and compiler "
        "diagnostics with get_igor_compilation_errors; "
        "the server does not dismiss dialogs automatically. Review diagnostics before "
        "using an explicit dismiss tool. "
        "Procedure compilation through the operation queue is asynchronous: use "
        'Execute/P/Q/Z "COMPILEPROCEDURES " (including the trailing space), then '
        "read get_igor_compilation_errors before dismissing the dialog. An empty "
        "execute_igor.compilation_errors field does not mean queued compilation "
        "succeeded because the dialog can appear after execute_igor returns. "
        "Knowledge tiers: these initialize instructions are the compact everyday "
        "Igor core supplied on each MCP initialization. Use them directly for "
        "routine wave creation, assignment, scaling, folders, calculations, "
        "graph creation, and styling; do not search manuals for familiar basic "
        "tasks. Official documents stay behind on-demand reference tools. Their "
        "knowledge_tier labels are reference hints only: core-reference pages are "
        "still read through a tool when verification is needed; on-demand pages "
        "provide broader detail. For gaps, use local query_igor_help or curated "
        "search/read tools only when syntax is uncertain, an operation unfamiliar, "
        "or diagnostics leave an unresolved gap. Do not enumerate or pre-read "
        "basic-reference pages as a preflight. Never use web search, a browser, "
        "HTTP fetch, forums, or any external source to look up Igor commands, "
        "examples, errors, or docs. If "
        "local knowledge lacks needed syntax or details, report the gap and ask the "
        "user; do not fall back online. Run installer/updater downloads only for an "
        "explicitly requested installation or refresh, never task-time lookup. Use "
        "curated guides when a "
        "project-specific workflow is relevant. These instructions are sent at "
        "initialize; "
        "they do not imply permanent model or host caching. Use read_igor_document "
        "to read a search result in full. "
        "Tool calls are automatically recorded as bounded local observations in "
        "session memory. These records summarize arguments and outcomes; bulk wave "
        "data is omitted. Use search_session_memory and read_session_memory only for "
        "a nontrivial workflow being repeated or a relevant workaround; skip "
        "elementary one-offs. Use capture_session only "
        "when a human/assistant-written Markdown summary should be preserved. "
        "Use execute_igor only for an action explicitly requested by the user."
        " Everyday Igor core: create a fresh, unique data folder and unique wave/graph "
        "names; omit /O so collisions fail, and preserve source waves by deriving results "
        "in new waves. Waves are arrays with dimension scales and units; p is the point "
        "index and x is the scaled X coordinate in pointwise assignments. For regular "
        "sampling, make the wave, set its X origin/step/units, then assign with wave = "
        "expression. Example (run each setup command in order and check success): "
        "NewDataFolder/S root:Run_20261020_01; "
        "Make/N=1000 signal; SetScale/P x, 0, 0.1, \"ms\", signal; "
        "signal = exp(-x/2); Display/N=RunPlot_20261020_01 signal; "
        "Label/W=RunPlot_20261020_01 left, \"Signal\"; "
        "Label/W=RunPlot_20261020_01 bottom, \"Time (ms)\"; "
        "ModifyGraph/W=RunPlot_20261020_01 rgb(signal)=(0,0,65535). "
        "NewDataFolder/S creates/selects the folder; Make/N creates points; SetScale/P "
        "sets the first X value, step, and units; Redimension/N=n changes a 1-D wave's "
        "point count. Check before shrinking because points can be discarded. "
        "Display uses wave scaling; for irregular "
        "X values use Display yWave vs xWave. Add a trace with AppendToGraph/W=graphName "
        "waveName; label with Label/W=graphName left or bottom; set trace RGB with "
        "ModifyGraph/W=graphName rgb(traceName)=(r,g,b), where channels range 0-65535. "
        "For supported numeric CSV/TSV/TXT files use import_delimited_text; use LoadWave "
        "for other formats or Igor files. WaveStats/Q wave reports values such as V_avg "
        "and V_sdev. CurveFit/Q line, wave is a common built-in fit and writes W_coef by "
        "default; use a fresh analysis folder and inspect coefficients and compare the "
        "fit with the data before relying on it. "
        "Smooth, Integrate, and Differentiate are common built-ins; verify flags if their "
        "specific behavior matters. SaveExperiment as \"Run_20261020_01.pxp\" saves the "
        "current experiment and overwrites a matching file, so choose a new filename "
        "unless overwrite is explicit. Use an IPF procedure for ODEs, instrument control, "
        "repeated loops, or substantial multi-dataset workflows. Consult manuals for "
        "uncertain flags, advanced behavior, or version-specific syntax."
    ),
)

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


class IgorStatusResult(TypedDict):
    com_reachable: bool
    compatible: bool
    igor_version: str | None
    minimum_igor_version: str
    current_experiment: str | None
    capture_mode: str
    source_path: str
    portable_object_name_limit: int
    error: str | None


class ExecuteIgorResult(TypedDict):
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


class EnsureIgorResult(TypedDict):
    com_reachable: bool
    igor_version: str
    current_experiment: str
    executable_path: str | None
    source_path: str


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
    """Escape a Python string for an Igor string literal."""
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
        if class_name in _DIALOG_WINDOW_CLASSES:
            return True
        if title in _COMPILER_DIALOG_TITLES:
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

    for hwnd in windows:
        # A minimized window is visible in the Win32 sense. Leave it minimized
        # if Igor or the user chose that state during startup.
        if not windll.user32.IsWindowVisible(hwnd):
            windll.user32.ShowWindow(wintypes.HWND(hwnd), _SW_SHOWNOACTIVATE)

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
        try:
            process = subprocess.Popen(
                [str(executable)],
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


def _execute_igor_direct(
    command: str, *, started_callback=None
) -> ExecuteIgorResult:
    """Execute one Igor command and preserve any visible dialog diagnostics."""
    _validate_command(command)
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
        try:
            if started_callback is not None:
                started_callback()
            marker = getattr(_command_start_marker, "event", None)
            if marker is not None:
                marker.set()
            result = _execute_command(app, command)
        except IgorError as exc:
            command_error = exc
        finally:
            stop_event.set()
            watcher.join(timeout=1.0)

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
    }


def _run_igor_operation_in_worker_locked(
    operation,
    *,
    failure_factory,
    action_description: str,
    on_timeout=None,
    on_complete=None,
):
    """Run one admitted Igor operation with the shared bounded worker policy."""
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
    thread.join(timeout=_IGOR_COMMAND_TIMEOUT_SECONDS)

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
            f"{_IGOR_COMMAND_TIMEOUT_SECONDS:.0f}-second "
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
    command: str, *, on_timeout=None, on_complete=None
) -> ExecuteIgorResult:
    """Run one admitted command in a bounded worker thread."""
    return _run_igor_operation_in_worker_locked(
        lambda: _execute_igor_direct(command),
        failure_factory=_failed_execute_result,
        action_description="command execution",
        on_timeout=on_timeout,
        on_complete=on_complete,
    )


def _execute_igor_in_worker(command: str) -> ExecuteIgorResult:
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
    return _execute_igor_in_worker(normalized_command)


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
    """Open a saved experiment through Igor's normal Automation Server method."""
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


@mcp.tool(annotations=_STARTUP_ANNOTATIONS, structured_output=True)
def ensure_igor() -> EnsureIgorResult:
    """Attach to Igor or start the configured Igor executable if needed.

    This is the explicit startup/preflight operation. Call it instead of
    searching the filesystem for Igor. It reuses an active Igor COM instance
    and otherwise starts ``IGOR_PRO_EXE`` or the maintained default Igor Pro 10
    path. It does not open an experiment, load a procedure, or run a user
    command.
    """
    def operation(app, version):
        candidates = _igor_executable_candidates()
        experiment = _query(app, "%s", "IgorInfo(1)").strip()
        return {
            "com_reachable": True,
            "igor_version": f"{version:.2f}",
            "current_experiment": experiment or "(untitled)",
            "executable_path": str(candidates[0]) if candidates else None,
            "source_path": _SOURCE_PATH,
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
    """Report source identity, COM availability, and Igor compatibility.

    This is a read-only diagnostic only; it does not start Igor. Use
    ``ensure_igor`` when Igor must be attached or launched.
    """
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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
