import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.shared.memory import create_connected_server_and_client_session


SERVER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_DIR))

import igor_mcp_server as server


class StartupWindowTests(unittest.TestCase):
    def _run_window_scan(self, windows, process_id=1234):
        by_hwnd = {window["hwnd"]: window for window in windows}

        def hwnd_value(hwnd):
            value = getattr(hwnd, "value", hwnd)
            return int(value or 0)

        class FakeUser32:
            def __init__(self):
                self.show_calls = []

            def EnumWindows(self, callback, _lparam):
                for window in windows:
                    callback(window["hwnd"], 0)
                return True

            def IsWindowVisible(self, hwnd):
                return by_hwnd[hwnd_value(hwnd)]["visible"]

            def IsIconic(self, hwnd):
                return by_hwnd[hwnd_value(hwnd)]["minimized"]

            def ShowWindow(self, hwnd, command):
                window = by_hwnd[hwnd_value(hwnd)]
                self.show_calls.append((window["hwnd"], command))
                if command == server._SW_HIDE:
                    window["visible"] = False
                elif command == server._SW_SHOWNOACTIVATE:
                    window["visible"] = True
                    window["minimized"] = False
                return True

        user32 = FakeUser32()
        fake_windll = type("FakeWindll", (), {"user32": user32})()
        with (
            patch.object(
                server,
                "WINFUNCTYPE",
                side_effect=lambda *_args: (lambda callback: callback),
            ),
            patch.object(server, "windll", fake_windll),
            patch.object(
                server,
                "_window_process_id",
                side_effect=lambda hwnd: by_hwnd[hwnd_value(hwnd)]["pid"],
            ),
            patch.object(
                server,
                "_window_owner",
                side_effect=lambda hwnd: by_hwnd[hwnd_value(hwnd)]["owner"],
            ),
            patch.object(
                server,
                "_window_class",
                side_effect=lambda hwnd: by_hwnd[hwnd_value(hwnd)]["class"],
            ),
            patch.object(
                server,
                "_window_text",
                side_effect=lambda hwnd: by_hwnd[hwnd_value(hwnd)]["title"],
            ),
            patch.object(server.time, "sleep"),
        ):
            result = server._show_new_igor_window_without_activation(process_id)
        return result, user32.show_calls

    def test_new_process_hides_exact_dde_helpers_and_shows_hidden_main_noactivate(self):
        windows = [
            {
                "hwnd": 0x100,
                "pid": 1234,
                "owner": 0,
                "class": "OleDdeWndClass",
                "title": "DDE Server Window",
                "visible": True,
                "minimized": False,
            },
            {
                "hwnd": 0x101,
                "pid": 1234,
                "owner": 0,
                "class": "DDEMLEvent",
                "title": "",
                "visible": True,
                "minimized": False,
            },
            {
                "hwnd": 0x102,
                "pid": 1234,
                "owner": 0,
                "class": "DDEMLMom",
                "title": "",
                "visible": True,
                "minimized": False,
            },
            {
                "hwnd": 0x103,
                "pid": 1234,
                "owner": 0,
                "class": "UnknownOleClass",
                "title": "DDE Server Window",
                "visible": False,
                "minimized": False,
            },
            {
                "hwnd": 0x104,
                "pid": 1234,
                "owner": 0,
                "class": "IgorMainWindow",
                "title": "Igor Pro",
                "visible": False,
                "minimized": False,
            },
            {
                "hwnd": 0x105,
                "pid": 5678,
                "owner": 0,
                "class": "OleDdeWndClass",
                "title": "DDE Server Window",
                "visible": True,
                "minimized": False,
            },
        ]

        result, calls = self._run_window_scan(windows)

        self.assertTrue(result)
        self.assertEqual(
            calls,
            [
                (0x100, server._SW_HIDE),
                (0x101, server._SW_HIDE),
                (0x102, server._SW_HIDE),
                (0x104, server._SW_SHOWNOACTIVATE),
            ],
        )
        self.assertTrue(windows[5]["visible"], "foreign-process window must be untouched")

    def test_new_process_leaves_visible_or_minimized_main_window_unchanged(self):
        windows = [
            {
                "hwnd": 0x200,
                "pid": 1234,
                "owner": 0,
                "class": "IgorMainWindow",
                "title": "Igor Pro",
                "visible": True,
                "minimized": True,
            }
        ]

        result, calls = self._run_window_scan(windows)

        self.assertTrue(result)
        self.assertEqual(calls, [])
        self.assertTrue(windows[0]["visible"])
        self.assertTrue(windows[0]["minimized"])

    def test_new_process_ignores_hidden_qt_gdi_and_ime_helper_windows(self):
        windows = [
            {
                "hwnd": 0x300,
                "pid": 1234,
                "owner": 0,
                "class": "GDI+ Hook Window Class",
                "title": "GDI+ Window (Igor64.exe)",
                "visible": False,
                "minimized": False,
            },
            {
                "hwnd": 0x301,
                "pid": 1234,
                "owner": 0,
                "class": "Qt693QWindowIcon",
                "title": "QTreeView",
                "visible": False,
                "minimized": False,
            },
            {
                "hwnd": 0x302,
                "pid": 1234,
                "owner": 0,
                "class": "IME",
                "title": "Default IME",
                "visible": False,
                "minimized": False,
            },
            {
                "hwnd": 0x303,
                "pid": 1234,
                "owner": 0,
                "class": "Qt693QWindowToolSaveBits",
                "title": "",
                "visible": False,
                "minimized": False,
            },
            {
                "hwnd": 0x304,
                "pid": 1234,
                "owner": 0,
                "class": "Qt693QWindowIcon",
                "title": "Untitled - Igor Pro 10",
                "visible": False,
                "minimized": False,
            },
            {
                "hwnd": 0x305,
                "pid": 1234,
                "owner": 0,
                "class": "Qt693QWindowIcon",
                "title": "Igor Pro Secondary Hidden",
                "visible": False,
                "minimized": False,
            },
        ]

        result, calls = self._run_window_scan(windows)

        self.assertTrue(result)
        self.assertEqual(calls, [(0x304, server._SW_SHOWNOACTIVATE)])
        self.assertFalse(windows[0]["visible"])
        self.assertFalse(windows[1]["visible"])
        self.assertFalse(windows[2]["visible"])
        self.assertFalse(windows[3]["visible"])
        self.assertTrue(windows[4]["visible"])
        self.assertFalse(windows[5]["visible"])


class ValidationTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(server, "_try_pipe_client", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_command_limit_counts_utf8_bytes(self):
        server._validate_command("\u00e9" * 1_200)
        with self.assertRaisesRegex(server.IgorError, "UTF-8 bytes"):
            server._validate_command("\u00e9" * 1_201)

    def test_validate_command_normalizes_multiline_statements(self):
        cmd = "Make/O/N=100 test_w\r\nSetScale/P x 0, 0.05, \"s\", test_w;\ntest_w = sin(x)"
        normalized = server._validate_command(cmd)
        self.assertEqual(
            normalized,
            'Make/O/N=100 test_w; SetScale/P x 0, 0.05, "s", test_w; test_w = sin(x)',
        )

    def test_validate_command_rejects_comment_lines(self):
        cmd = "Make/O/N=100 test_w\n// comment line\nSetScale/P x 0, 0.05, \"s\", test_w"
        with self.assertRaisesRegex(server.IgorError, "cannot be safely chained"):
            server._validate_command(cmd)

    def test_experiment_path_validation_requires_an_existing_absolute_pxp_or_pxt(self):
        with tempfile.TemporaryDirectory() as directory:
            experiment = Path(directory) / "sample.pxp"
            experiment.write_bytes(b"test")
            self.assertEqual(
                server._validate_experiment_file_path(str(experiment)),
                experiment.resolve(),
            )

            template = Path(directory) / "template.pxt"
            template.write_bytes(b"test")
            self.assertEqual(
                server._validate_experiment_file_path(str(template)),
                template.resolve(),
            )

            with self.assertRaisesRegex(server.IgorError, "absolute path"):
                server._validate_experiment_file_path("sample.pxp")
            with self.assertRaisesRegex(server.IgorError, "does not exist"):
                server._validate_experiment_file_path(
                    str(Path(directory) / "missing.pxp")
                )
            with self.assertRaisesRegex(server.IgorError, r"\.pxp or \.pxt"):
                server._validate_experiment_file_path(
                    str(Path(directory) / "data.csv")
                )
            experiment_directory = Path(directory) / "folder.pxp"
            experiment_directory.mkdir()
            with self.assertRaisesRegex(server.IgorError, "not a file"):
                server._validate_experiment_file_path(str(experiment_directory))

    def test_open_experiment_calls_normal_com_open_without_starting_igor(self):
        class FakeApplication:
            def __init__(self):
                self.load_args = None

            def LoadExperiment(self, *args):
                self.load_args = args

        with tempfile.TemporaryDirectory() as directory:
            experiment = Path(directory) / "sample.pxp"
            experiment.write_bytes(b"test")
            app = FakeApplication()
            started = []

            def invoke(callback, **kwargs):
                self.assertFalse(kwargs["auto_start"])
                self.assertTrue(kwargs["check_dialogs"])
                return callback(app, 10.03)

            with (
                patch.object(server, "_with_igor", side_effect=invoke),
                patch.object(server, "_igor_dialogs", return_value=[]),
                patch.object(server, "_query", return_value="sample") as query,
            ):
                result = server._open_igor_experiment_direct(
                    experiment, started_callback=lambda: started.append(True)
                )

        self.assertEqual(app.load_args, (0, 2, "", str(experiment)))
        self.assertEqual(server._IGOR_LOAD_EXPERIMENT_FLAGS, 0)
        self.assertEqual(server._IGOR_LOAD_TYPE_OPEN, 2)
        self.assertEqual(started, [True])
        self.assertEqual(result["current_experiment"], "sample")
        self.assertTrue(result["load_completed"])
        query.assert_called_once_with(app, "%s", "IgorInfo(1)")

    def test_open_experiment_leaves_preexisting_dialogs_open(self):
        class FakeApplication:
            def LoadExperiment(self, *_args):
                raise AssertionError("open must not run while a dialog is present")

        dialog = {"hwnd": 123, "title": "Save Changes"}
        with tempfile.TemporaryDirectory() as directory:
            experiment = Path(directory) / "sample.pxp"
            experiment.write_bytes(b"test")

            with (
                patch.object(
                    server,
                    "_with_igor",
                    side_effect=lambda callback, **_kwargs: callback(FakeApplication(), 10.03),
                ),
                patch.object(server, "_igor_dialogs", return_value=[dialog]),
                patch.object(server, "_capture_compilation_errors", return_value=[]),
                patch.object(server, "_dismiss_igor_dialogs") as dismiss,
            ):
                result = server._open_igor_experiment_direct(experiment)

        self.assertFalse(result["ok"])
        self.assertFalse(result["load_started"])
        self.assertTrue(result["outcome_known"])
        self.assertEqual(result["dialog_details"], [dialog])
        dismiss.assert_not_called()

    def test_tool_schemas_contain_parameter_descriptions(self):
        execute_tool = server.mcp._tool_manager._tools["execute_igor"]
        self.assertIn("description", execute_tool.parameters["properties"]["command"])

        open_tool = server.mcp._tool_manager._tools["open_igor_experiment"]
        self.assertIn("description", open_tool.parameters["properties"]["file_path"])

        import_tool = server.mcp._tool_manager._tools["import_delimited_text"]
        self.assertIn("description", import_tool.parameters["properties"]["file_path"])
        self.assertIn("description", import_tool.parameters["properties"]["delimiter"])

        wave_tool = server.mcp._tool_manager._tools["get_wave_data"]
        self.assertIn("description", wave_tool.parameters["properties"]["wave_name"])
        self.assertIn("description", wave_tool.parameters["properties"]["max_points"])

    def test_newline_normalization_does_not_double_crlf(self):
        self.assertEqual(
            server._normalize_newlines("first\r\nsecond\rthird\n"),
            "first\nsecond\nthird\n",
        )

    def test_minimum_supported_igor_version_is_6_0(self):
        server._ensure_supported_igor_version(6.0)
        with self.assertRaisesRegex(server.IgorError, "6.0 or newer"):
            server._ensure_supported_igor_version(5.99)

    def test_connect_reuses_active_igor_without_starting_a_process(self):
        active = object()
        with (
            patch.object(server, "_active_igor", return_value=active),
            patch.object(server.subprocess, "Popen") as popen,
        ):
            result = server._connect()

        self.assertIs(result, active)
        popen.assert_not_called()

    def test_connect_preserves_minimized_existing_igor_visibility(self):
        class MinimizedApplication:
            def __init__(self):
                self.visible = False

            @property
            def Visible(self):
                return self.visible

            @Visible.setter
            def Visible(self, value):
                raise AssertionError("connection must not change Igor visibility")

        app = MinimizedApplication()
        with patch.object(server, "_active_igor", return_value=app):
            self.assertIs(server._connect(), app)
        self.assertFalse(app.Visible)

    def test_new_igor_window_is_shown_without_activation(self):
        source = Path(server.__file__).read_text(encoding="utf-8")
        self.assertIn("_show_new_igor_window_without_activation", source)
        self.assertIn("_SW_SHOWNOACTIVATE", source)
        startup = source.split("def _show_new_igor_window_without_activation", 1)[1].split("def _connect", 1)[0]
        self.assertNotIn("SetForegroundWindow", startup)
        self.assertNotIn("SW_RESTORE", startup)
        self.assertNotIn("_bring_igor_window_to_front", source)

    def test_configured_igor_path_is_used_before_discovery(self):
        executable = Path(r"C:\Configured\Igor64.exe")
        with patch.dict(
            server.os.environ,
            {server._IGOR_EXECUTABLE_ENV: str(executable)},
        ):
            self.assertEqual(server._igor_executable_candidates(), [executable])

    def test_task_routing_instructions_keep_short_work_on_commands(self):
        instructions = server.mcp.instructions
        self.assertIn("generating a wave and fitting it", instructions)
        self.assertIn("do not generate an IPF procedure", instructions)
        self.assertIn("ODEs, instrument control", instructions)
        self.assertIn("substantial batch or loop-heavy processing", instructions)
        self.assertIn("A large wave handled by one built-in command", instructions)
        self.assertIn("Never search the filesystem", instructions)
        self.assertIn("Do not use igor_status for startup", instructions)
        self.assertIn("Use open_igor_experiment", instructions)
        self.assertIn("normal Igor File > Open behavior", instructions)
        self.assertIn("show its main window without activation", instructions)
        self.assertIn("if the user minimizes Igor, leave it minimized", instructions)
        self.assertIn("DoWindow/F unless requested", instructions)
        self.assertIn("does not dismiss dialogs automatically", instructions)
        self.assertIn("Everyday Igor core", instructions)
        self.assertIn("compact everyday Igor core supplied on each MCP initialization", instructions)
        self.assertIn("do not search manuals for familiar basic tasks", instructions)
        self.assertIn("core-reference pages are still read through a tool", instructions)
        self.assertIn("Do not enumerate or pre-read basic-reference pages as a preflight", instructions)
        self.assertIn("local query_igor_help or curated search/read tools", instructions)
        self.assertIn("only when syntax is uncertain", instructions)
        self.assertIn("Use search_session_memory and read_session_memory only for", instructions)
        self.assertIn("skip elementary one-offs", instructions)
        self.assertIn("Never use web search, a browser, HTTP fetch, forums, or any external source", instructions)
        self.assertIn("report the gap and ask the user", instructions)
        self.assertIn("explicitly requested installation or refresh, never task-time lookup", instructions)
        self.assertIn("do not imply permanent model or host caching", instructions)
        self.assertIn("NewDataFolder/S root:Run_20261020_01", instructions)
        self.assertIn('SetScale/P x, 0, 0.1, "ms", signal', instructions)
        self.assertIn("signal = exp(-x/2)", instructions)
        self.assertIn("Display yWave vs xWave", instructions)
        self.assertIn("AppendToGraph/W=graphName", instructions)
        self.assertIn("Label/W=graphName", instructions)
        self.assertIn("channels range 0-65535", instructions)
        self.assertIn("omit /O", instructions)
        self.assertLessEqual(len(instructions), 6_000)

    def test_connect_starts_igor_and_waits_for_com_registration(self):
        active = object()
        process = type("Process", (), {"pid": 1234, "poll": lambda self: None})()
        executable = Path(r"C:\Igor\Igor64.exe")
        with (
            patch.object(server, "_is_igor_process_running", return_value=False),
            patch.object(
                server,
                "_active_igor",
                side_effect=[server.IgorError("not registered"), active],
            ),
            patch.object(server, "_select_igor_executable", return_value=executable),
            patch.object(server.subprocess, "Popen", return_value=process) as popen,
            patch.object(server, "_show_new_igor_window_without_activation", return_value=True) as show_window,
            patch.object(server.time, "sleep"),
        ):
            result = server._connect()

        self.assertIs(result, active)
        show_window.assert_called_once_with(1234)
        popen.assert_called_once_with(
            [str(executable)],
            cwd=str(executable.parent),
            stdin=server.subprocess.DEVNULL,
            stdout=server.subprocess.DEVNULL,
            stderr=server.subprocess.DEVNULL,
            creationflags=getattr(server.subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )

    def test_connect_reports_an_igor_process_that_exits_during_startup(self):
        process = type("Process", (), {"pid": 1234, "poll": lambda self: 7})()
        executable = Path(r"C:\Igor\Igor64.exe")
        with (
            patch.object(server, "_is_igor_process_running", return_value=False),
            patch.object(
                server,
                "_active_igor",
                side_effect=server.IgorError("not registered"),
            ),
            patch.object(server, "_select_igor_executable", return_value=executable),
            patch.object(server.subprocess, "Popen", return_value=process),
        ):
            with self.assertRaisesRegex(server.IgorError, "exited during startup with code 7"):
                server._connect()

    def test_with_igor_reports_dialogs_without_dismissing_them(self):
        dialog = {"hwnd": 1234, "title": "Choose a procedure"}

        class Application:
            def __enter__(self):
                return object()

            def __exit__(self, *_args):
                return False

        with (
            patch.object(server, "_igor_application", return_value=Application()),
            patch.object(server, "_igor_dialogs", return_value=[dialog]),
            patch.object(server, "_dismiss_igor_dialogs") as dismiss,
            patch.object(server, "_igor_version") as version,
        ):
            with self.assertRaisesRegex(server.IgorError, "No dialog was dismissed"):
                server._with_igor(lambda _app, current_version: current_version)

        dismiss.assert_not_called()
        version.assert_not_called()

    def test_dialog_diagnostics_and_explicit_recovery_work_while_com_lock_is_held(self):
        dialog = {"hwnd": 4321, "title": "Choose a procedure"}
        compiler_error = {"hwnd": 4321, "title": "Function Compilation Error"}
        with (
            patch.object(server, "_igor_dialogs", return_value=[dialog]),
            patch.object(server, "_compilation_error_dialogs", return_value=[compiler_error]),
            patch.object(
                server,
                "_dismiss_igor_dialogs",
                return_value={"close_requested": 1, "dismissed": 1},
            ) as dismiss_dialogs,
            patch.object(server, "_dismiss_compilation_error_dialog", return_value=True) as dismiss_compiler,
        ):
            with server._lock:
                self.assertEqual(server.get_igor_dialogs(), [dialog])
                self.assertEqual(server.get_igor_compilation_errors(), [compiler_error])
                self.assertEqual(server.dismiss_igor_dialogs()["dismissed"], 1)
                self.assertEqual(server.dismiss_igor_compilation_errors()["dismissed"], 1)

        dismiss_dialogs.assert_called_once_with([dialog])
        dismiss_compiler.assert_called_once_with(compiler_error)

    def test_command_worker_returns_worker_result(self):
        expected = {"ok": True, "output": "done"}
        with patch.object(server, "_execute_igor_direct", return_value=expected):
            result = server._execute_igor_in_worker("Print 1")
        self.assertEqual(result, expected)

    def test_open_worker_keeps_shared_admission_until_timed_out_com_call_exits(self):
        import threading
        import time

        allow_exit = threading.Event()
        finished = threading.Event()

        def slow_open(_path):
            started = getattr(server._command_start_marker, "event", None)
            if started is not None:
                started.set()
            allow_exit.wait(1)
            finished.set()
            return {"ok": True}

        with tempfile.TemporaryDirectory() as directory:
            experiment = Path(directory) / "sample.pxp"
            experiment.write_bytes(b"test")
            with (
                patch.object(server, "_open_igor_experiment_direct", side_effect=slow_open),
                patch.object(server, "_IGOR_COMMAND_TIMEOUT_SECONDS", 0.05),
                patch.object(server, "_igor_dialogs", return_value=[]),
                patch.object(server, "_capture_compilation_errors", return_value=[]),
            ):
                result = server._open_igor_experiment_in_worker(experiment.resolve())
                allow_exit.set()
                self.assertTrue(finished.wait(1))

        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if server._command_worker_lock.acquire(blocking=False):
                server._command_worker_lock.release()
                break
            time.sleep(0.005)
        else:
            self.fail("timed-out open worker did not release shared command admission")

        self.assertFalse(result["ok"])
        self.assertTrue(result["load_started"])
        self.assertFalse(result["outcome_known"])
        self.assertIn("response deadline", result["error"])

    def test_command_worker_reports_timeout_until_worker_exits(self):
        import threading
        import time

        allow_exit = threading.Event()
        finished = threading.Event()

        def slow_run(_cmd):
            allow_exit.wait(1)
            finished.set()
            return {"ok": True, "output": "late"}

        dialog = {"hwnd": 9876, "title": "Function Compilation Error"}
        compiler_error = {"hwnd": 9876, "title": "Function Compilation Error", "phase": "command_timeout"}
        with (
            patch.object(server, "_execute_igor_direct", side_effect=slow_run),
            patch.object(server, "_IGOR_COMMAND_TIMEOUT_SECONDS", 0.05),
            patch.object(server, "_igor_dialogs", return_value=[dialog]),
            patch.object(server, "_capture_compilation_errors", return_value=[compiler_error]),
        ):
            result = server._execute_igor_in_worker("Sleep/T 100")
            allow_exit.set()
            self.assertTrue(finished.wait(1))

        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if server._command_worker_lock.acquire(blocking=False):
                server._command_worker_lock.release()
                break
            time.sleep(0.005)
        else:
            self.fail("timed-out worker did not release command admission")

        self.assertIn("response deadline", result["error"])
        self.assertIn("get_igor_dialogs", result["error"])
        self.assertFalse(result["outcome_known"])
        self.assertTrue(result["dialog_detected"])
        self.assertEqual(result["dialog_details"], [dialog])
        self.assertEqual(result["compilation_errors"], [compiler_error])

    def test_command_worker_fails_fast_when_another_command_is_active(self):
        self.assertTrue(server._command_worker_lock.acquire())
        try:
            with (
                patch.object(server, "_COMMAND_WORKER_LOCK_WAIT_SECONDS", 0),
                patch.object(server, "_execute_igor_in_worker_locked") as run,
            ):
                result = server._execute_igor_in_worker("Print 1")
        finally:
            server._command_worker_lock.release()

        self.assertFalse(result["ok"])
        self.assertFalse(result["command_started"])
        self.assertTrue(result["outcome_known"])
        self.assertIn("already running", result["error"])
        run.assert_not_called()

    def test_helper_names_are_unique_and_fit_igor_6_limit(self):
        names = {server._helper_name("igorMcpHistory") for _ in range(20)}

        self.assertEqual(len(names), 20)
        self.assertTrue(
            all(len(name) <= server._MAX_IGOR_OBJECT_NAME_LENGTH for name in names)
        )
        with self.assertRaisesRegex(ValueError, "portable Igor identifier"):
            server._helper_name("prefix-is-not-portable")

    def test_import_prefix_leaves_room_for_largest_column_suffix(self):
        prefix = "a" * server._MAX_IMPORT_PREFIX_LENGTH
        server._validate_portable_igor_name(
            prefix,
            label="prefix",
            max_length=server._MAX_IMPORT_PREFIX_LENGTH,
        )

        largest_name = f"{prefix}{server._MAX_IMPORT_COLUMNS - 1}"
        self.assertEqual(len(largest_name), server._MAX_IGOR_OBJECT_NAME_LENGTH)
        with self.assertRaisesRegex(server.IgorError, "Igor Pro 6"):
            server._validate_portable_igor_name(
                prefix + "x",
                label="prefix",
                max_length=server._MAX_IMPORT_PREFIX_LENGTH,
            )

    def test_all_tools_declare_safety_annotations(self):
        tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}

        self.assertEqual(set(tools), {
            "open_report_graph",
            "create_html_report",
            "apply_html_report",
            "execute_igor",
            "import_delimited_text",
            "read_variable",
            "get_wave_data",
            "list_waves",
            "list_procedures",
            "ensure_igor",
            "open_igor_experiment",
            "get_igor_dialogs",
            "dismiss_igor_dialogs",
            "get_igor_compilation_errors",
            "dismiss_igor_compilation_errors",
            "igor_status",
            "query_igor_help",
            "capture_session",
            "search_igor_knowledge",
            "read_igor_document",
            "search_session_memory",
            "read_session_memory",
            "capture_window",
            "call_function",
            "abort_igor",
            "get_igor_state",
            "open_procedure",
            "close_procedure",
            "compile_procedures",
            "get_compile_status",
            "list_windows",
            "get_experiment_info",
            "get_system_information",
            "inspect_experiment",
            "read_procedure",
            "profile_workflow",
        })
        self.assertTrue(tools["query_igor_help"].annotations.readOnlyHint)
        self.assertFalse(tools["query_igor_help"].annotations.destructiveHint)
        self.assertTrue(tools["igor_status"].annotations.readOnlyHint)
        self.assertFalse(tools["ensure_igor"].annotations.readOnlyHint)
        self.assertFalse(tools["ensure_igor"].annotations.destructiveHint)
        self.assertFalse(tools["open_igor_experiment"].annotations.readOnlyHint)
        self.assertTrue(tools["open_igor_experiment"].annotations.destructiveHint)
        self.assertTrue(tools["get_igor_dialogs"].annotations.readOnlyHint)
        self.assertFalse(tools["dismiss_igor_dialogs"].annotations.readOnlyHint)
        self.assertTrue(tools["dismiss_igor_dialogs"].annotations.destructiveHint)
        for name in ("read_variable", "get_wave_data", "list_waves", "list_procedures"):
            self.assertTrue(tools[name].annotations.readOnlyHint)
            self.assertFalse(tools[name].annotations.destructiveHint)
        self.assertTrue(tools["search_session_memory"].annotations.readOnlyHint)
        self.assertTrue(tools["read_session_memory"].annotations.readOnlyHint)
        capture_description = tools["capture_session"].description.casefold()
        self.assertIn("caller-written", capture_description)
        self.assertIn("automatic mcp call observations", capture_description)
        self.assertNotIn("explicitly requested", capture_description)
        summary_description = server.mcp._tool_manager._tools["capture_session"].parameters["properties"]["summary"]["description"].casefold()
        self.assertIn("preserve in session memory", summary_description)
        self.assertFalse(tools["igor_status"].annotations.destructiveHint)
        self.assertFalse(tools["execute_igor"].annotations.readOnlyHint)
        self.assertTrue(tools["execute_igor"].annotations.destructiveHint)
        self.assertFalse(tools["import_delimited_text"].annotations.readOnlyHint)
        self.assertTrue(tools["import_delimited_text"].annotations.destructiveHint)
        for name in (
            "capture_window",
            "get_igor_state",
            "get_compile_status",
            "list_windows",
            "get_experiment_info",
            "get_system_information",
            "inspect_experiment",
            "read_procedure",
            "profile_workflow",
        ):
            self.assertTrue(tools[name].annotations.readOnlyHint)
            self.assertFalse(tools[name].annotations.destructiveHint)
        for name in (
            "call_function",
            "abort_igor",
            "open_procedure",
            "close_procedure",
            "compile_procedures",
        ):
            self.assertFalse(tools[name].annotations.readOnlyHint)
            self.assertTrue(tools[name].annotations.destructiveHint)

    def test_query_igor_help_returns_documentation_excerpts(self):
        result = server.query_igor_help(query="CurveFit", max_results=3)
        self.assertIsNone(result.get("error"))
        self.assertEqual(result["results_count"], 0)
        self.assertEqual(result["results"], [])

    def test_com_initialization_is_owned_and_balanced_by_application_context(self):
        calls = []

        class FakeCom:
            @staticmethod
            def CoInitialize():
                calls.append("init")

            @staticmethod
            def CoUninitialize():
                calls.append("uninit")

        with (
            patch.object(server, "pythoncom", FakeCom),
            patch.object(server, "_PYWIN32_IMPORT_ERROR", None),
            patch.object(server, "_connect", return_value=type("App", (), {"Visible": True})()),
        ):
            with server._igor_application():
                self.assertEqual(calls, ["init"])
        self.assertEqual(calls, ["init", "uninit"])

        calls.clear()
        with (
            patch.object(server, "pythoncom", FakeCom),
            patch.object(server, "_PYWIN32_IMPORT_ERROR", None),
            patch.object(server, "_connect", side_effect=server.IgorError("not running")),
        ):
            with self.assertRaisesRegex(server.IgorError, "not running"):
                with server._igor_application():
                    pass
        self.assertEqual(calls, ["init", "uninit"])

    def test_mcp_error_results_preserve_structured_failure_and_logger_is_best_effort(self):
        successful = {
            "ok": True,
            "output": "done",
            "error": None,
            "warning": None,
            "command_started": True,
            "command_completed": True,
            "outcome_known": True,
            "preexisting_dialog_count": 0,
            "dialog_detected": False,
            "dialogs_open": 0,
            "dialog_titles": [],
            "dialog_details": [],
            "compilation_errors": [],
        }
        failed = server._failed_execute_result(
            "COM call failed", command_started=False, outcome_known=True
        )

        async def invoke(tool_name, arguments):
            async with create_connected_server_and_client_session(server.mcp) as client:
                return await client.call_tool(tool_name, arguments)

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"IGOR_SESSION_DIR": directory}
        ):
            with (
                patch.object(server, "_execute_igor_in_worker", return_value=successful) as execute,
                patch.object(server, "append_tool_call_record", side_effect=OSError("disk full")),
                patch("sys.stderr", new_callable=io.StringIO) as stderr,
            ):
                success_result = asyncio.run(invoke("execute_igor", {"command": "Print 1"}))
            self.assertFalse(success_result.isError)
            self.assertEqual(success_result.structuredContent["output"], "done")
            execute.assert_called_once_with("Print 1")
            self.assertIn("tool outcome preserved", stderr.getvalue())

            with patch.object(server, "_execute_igor_in_worker", return_value=failed):
                failed_result = asyncio.run(invoke("execute_igor", {"command": "Print 2"}))
            self.assertTrue(failed_result.isError)
            self.assertFalse(failed_result.structuredContent["ok"])
            self.assertEqual(json.loads(failed_result.content[0].text)["error"], "COM call failed")

            for tool_name in ("query_igor_help", "search_igor_knowledge"):
                invalid_result = asyncio.run(invoke(tool_name, {"query": ""}))
                self.assertTrue(invalid_result.isError)
                self.assertEqual(
                    invalid_result.structuredContent["error"], "query must be non-empty"
                )

            saved = asyncio.run(
                invoke(
                    "capture_session",
                    {"title": "wire memory test", "summary": "Saved CurveFit outcome"},
                )
            )
            self.assertFalse(saved.isError)
            record_id = saved.structuredContent["session_id"]
            found = asyncio.run(
                invoke("search_session_memory", {"query": "CurveFit outcome", "record_type": "summary"})
            )
            self.assertEqual(found.structuredContent["results"][0]["record_id"], record_id)
            read = asyncio.run(invoke("read_session_memory", {"record_id": record_id}))
            self.assertIn("Saved CurveFit outcome", read.structuredContent["content"])

            raised_result = asyncio.run(
                invoke("read_igor_document", {"document_id": "unknown.md"})
            )
            self.assertTrue(raised_result.isError)

            help_result = asyncio.run(invoke("query_igor_help", {"query": "CurveFit"}))
            self.assertFalse(help_result.isError)
            help_results = help_result.structuredContent["results"]
            self.assertEqual(help_results, [])

    def test_execute_igor_reports_preexisting_dialog_without_running_or_dismissing(self):
        dialog = {"hwnd": 1234, "title": "Choose a procedure"}
        app = object()
        with (
            patch.object(
                server,
                "_with_igor",
                side_effect=lambda fn, **_kwargs: fn(app, 10.0),
            ),
            patch.object(server, "_igor_dialogs", return_value=[dialog]),
            patch.object(server, "_dismiss_igor_dialogs") as dismiss,
            patch.object(server, "_execute_via_tempfile") as execute,
        ):
            result = server._execute_igor_direct("Print 1")

        self.assertFalse(result["ok"])
        self.assertFalse(result["command_started"])
        self.assertTrue(result["outcome_known"])
        self.assertEqual(result["preexisting_dialog_count"], 1)
        self.assertTrue(result["dialog_detected"])
        self.assertEqual(result["dialog_details"], [dialog])
        self.assertIn("No dialog was dismissed", result["error"])
        dismiss.assert_not_called()
        execute.assert_not_called()

    def test_known_chooser_uses_only_safe_ui_automation_buttons(self):
        with patch.object(server, "_run_ui_automation", return_value="") as run:
            self.assertTrue(
                server._uia_dismiss_automatable_dialog(1234, "Choose a procedure")
            )

        script = run.call_args.args[0]
        self.assertIn("'Cancel', 'Close'", script)
        self.assertNotIn("'Open'", script)

    def test_unknown_dialog_does_not_use_ui_automation(self):
        with patch.object(server, "_run_ui_automation") as run:
            self.assertFalse(
                server._uia_dismiss_automatable_dialog(1234, "Unexpected dialog")
            )

        run.assert_not_called()

    def test_execute_igor_reports_but_does_not_dismiss_a_new_dialog(self):
        dialog = {"hwnd": 5678, "title": "Choose a procedure"}
        with (
            patch.object(
                server,
                "_with_igor",
                side_effect=lambda fn, **_kwargs: fn(object(), 10.0),
            ),
            patch.object(server, "_igor_dialogs", side_effect=[[], [dialog]]),
            patch.object(server, "_capture_compilation_errors", return_value=[]),
            patch.object(server, "_dismiss_igor_dialogs") as dismiss,
            patch.object(server, "_execute_via_tempfile", return_value="ok"),
        ):
            result = server._execute_igor_direct("Print 1")

        self.assertFalse(result["ok"])
        self.assertTrue(result["command_started"])
        self.assertTrue(result["command_completed"])
        self.assertTrue(result["outcome_known"])
        self.assertTrue(result["dialog_detected"])
        self.assertEqual(result["dialog_titles"], ["Choose a procedure"])
        self.assertEqual(result["dialog_details"], [dialog])
        self.assertEqual(result["dialogs_open"], 1)
        self.assertIn("No dialog was dismissed", result["error"])
        dismiss.assert_not_called()

    def test_execute_igor_returns_unknown_outcome_for_command_error(self):
        app = object()
        with (
            patch.object(
                server,
                "_with_igor",
                side_effect=lambda fn, **_kwargs: fn(app, 10.0),
            ),
            patch.object(server, "_igor_dialogs", return_value=[]),
            patch.object(
                server,
                "_execute_via_tempfile",
                side_effect=server.IgorError("COM call failed"),
            ),
        ):
            result = server._execute_igor_direct("DVA2_RunAutomatic()")

        self.assertFalse(result["ok"])
        self.assertTrue(result["command_started"])
        self.assertFalse(result["command_completed"])
        self.assertFalse(result["outcome_known"])
        self.assertEqual(result["error"], "COM call failed")


class ToolBehaviorTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(server, "_try_pipe_client", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_status_reports_source_even_when_igor_is_unavailable(self):
        with patch.object(
            server, "_with_igor", side_effect=server.IgorError("not running")
        ):
            result = server.igor_status()

        self.assertFalse(result["com_reachable"])
        self.assertFalse(result["compatible"])
        self.assertEqual(result["minimum_igor_version"], "6.0")
        self.assertEqual(result["portable_object_name_limit"], 31)
        self.assertEqual(Path(result["source_path"]), Path(server.__file__).resolve())
        self.assertEqual(result["error"], "not running")

    def test_ensure_igor_attaches_and_reports_startup_configuration(self):
        with (
            patch.object(
                server,
                "_with_igor",
                side_effect=lambda fn, **_kwargs: fn(object(), 10.03),
            ) as with_igor,
            patch.object(server, "_query", return_value="Untitled"),
            patch.object(
                server,
                "_igor_executable_candidates",
                return_value=[Path(r"C:\Program Files\WaveMetrics\Igor Pro 10 Folder\IgorBinaries_x64\Igor64.exe")],
            ),
        ):
            result = server.ensure_igor()

        self.assertTrue(result["com_reachable"])
        self.assertEqual(result["igor_version"], "10.03")
        self.assertEqual(result["current_experiment"], "Untitled")
        self.assertTrue(result["executable_path"].endswith("Igor64.exe"))
        self.assertEqual(Path(result["source_path"]), Path(server.__file__).resolve())
        self.assertTrue(with_igor.call_args.kwargs.get("check_dialogs", True))

    def test_status_reports_reachable_but_unsupported_igor(self):
        with (
            patch.object(
                server,
                "_with_igor",
                side_effect=lambda fn, **_kwargs: fn(object(), 5.0),
            ),
            patch.object(server, "_query", return_value="legacy.pxp"),
        ):
            result = server.igor_status()

        self.assertTrue(result["com_reachable"])
        self.assertFalse(result["compatible"])
        self.assertEqual(result["igor_version"], "5.00")
        self.assertIn("6.0 or newer", result["error"])

    def test_delimited_import_uses_short_names_and_moves_verified_waves(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.csv"
            source.write_text("time,signal\n0,1\n1,3\n", encoding="utf-8")
            state = {"loaded": False}

            def query(_app, _fmt, expression):
                if "DataFolderExists" in expression:
                    return "1"
                if 'WaveList("*"' in expression:
                    return ""
                if "WaveList" in expression:
                    temporary_prefix = expression.split('"', 2)[1].rstrip("*")
                    return (
                        f"{temporary_prefix}0;{temporary_prefix}1;"
                        if state["loaded"] else ""
                    )
                self.fail(f"Unexpected query: {expression}")

            def load(_app, command):
                self.assertRegex(command, r"LoadWave/J/D/A=im_[0-9a-f]{8}/O/K=0")
                state["loaded"] = True
                return "loaded"

            with (
                patch.object(
                    server, "_with_igor", side_effect=lambda fn: fn(object(), 10.0)
                ),
                patch.object(server, "_query", side_effect=query),
                patch.object(server, "_execute_via_tempfile", side_effect=load),
                patch.object(server, "_raw_execute") as raw_execute,
            ):
                result = server.import_delimited_text(
                    str(source), prefix="safe", folder="root:imports:"
                )

        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["columns"], 2)
        self.assertEqual(result["headers"], ["time", "signal"])
        self.assertEqual(result["waves"], ["safe0", "safe1"])
        self.assertEqual(result["portable_object_name_limit"], 31)
        move_commands = [call.args[1] for call in raw_execute.call_args_list]
        self.assertEqual(len(move_commands), 2)
        self.assertTrue(all(command.startswith("MoveWave im_") for command in move_commands))
        self.assertTrue(move_commands[0].endswith(", root:imports:safe0"))
        self.assertTrue(move_commands[1].endswith(", root:imports:safe1"))

    def test_delimited_import_rejects_existing_destination_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.csv"
            source.write_text("x\n1\n", encoding="utf-8")

            def query(_app, _fmt, expression):
                if "DataFolderExists" in expression:
                    return "1"
                if 'WaveList("*"' in expression:
                    return "safe0;"
                self.fail(f"Unexpected query: {expression}")

            with (
                patch.object(
                    server, "_with_igor", side_effect=lambda fn: fn(object(), 10.0)
                ),
                patch.object(server, "_query", side_effect=query),
                patch.object(server, "_execute_via_tempfile") as execute,
            ):
                with self.assertRaisesRegex(server.IgorError, "overwrite=true"):
                    server.import_delimited_text(str(source), prefix="safe")

        execute.assert_not_called()

    def test_delimited_import_overwrite_is_explicit_and_precedes_moves(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.csv"
            source.write_text("x,y\n1,2\n", encoding="utf-8")
            state = {"loaded": False}

            def query(_app, _fmt, expression):
                if "DataFolderExists" in expression:
                    return "1"
                if 'WaveList("*"' in expression:
                    return "safe0;safe1;"
                if "WaveList" in expression:
                    temporary_prefix = expression.split('"', 2)[1].rstrip("*")
                    return (
                        f"{temporary_prefix}0;{temporary_prefix}1;"
                        if state["loaded"] else ""
                    )
                self.fail(f"Unexpected query: {expression}")

            def load(_app, _command):
                state["loaded"] = True
                return "loaded"

            with (
                patch.object(
                    server, "_with_igor", side_effect=lambda fn: fn(object(), 10.0)
                ),
                patch.object(server, "_query", side_effect=query),
                patch.object(server, "_execute_via_tempfile", side_effect=load),
                patch.object(server, "_raw_execute") as raw_execute,
            ):
                result = server.import_delimited_text(
                    str(source), prefix="safe", overwrite=True
                )

        self.assertEqual(result["waves"], ["safe0", "safe1"])
        commands = [call.args[1] for call in raw_execute.call_args_list]
        self.assertEqual(commands[:2], [
            "KillWaves/Z root:safe0",
            "KillWaves/Z root:safe1",
        ])
        self.assertTrue(commands[2].endswith(", root:safe0"))
        self.assertTrue(commands[3].endswith(", root:safe1"))

    def test_delimited_import_reports_non_numeric_cell_location(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.csv"
            source.write_text("x,y\n1,bad\n", encoding="utf-8")

            with self.assertRaisesRegex(server.IgorError, "row 2, column 2"):
                server.import_delimited_text(str(source), prefix="safe")

    def test_delimited_import_rejects_prefix_too_long_for_igor_6(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.csv"
            source.write_text("x\n1\n", encoding="utf-8")

            with self.assertRaisesRegex(server.IgorError, "Igor Pro 6"):
                server.import_delimited_text(
                    str(source), prefix="a" * (server._MAX_IMPORT_PREFIX_LENGTH + 1)
                )

    def test_real_wave_returns_structured_values_and_truncation(self):
        query_results = iter(["1", "4", "1", "3", "1"])
        with (
            patch.object(server, "_with_igor", side_effect=lambda fn: fn(object(), 10.0)),
            patch.object(server, "_query", side_effect=lambda *_: next(query_results)),
            patch.object(server, "_raw_execute") as execute,
            patch.object(server, "_read_tmp_file", return_value="1\r\nNaN\r\n"),
        ):
            result = server.get_wave_data("root:test", max_points=2)

        self.assertEqual(result["values"], [1.0, "NaN"])
        self.assertTrue(result["truncated"])
        self.assertFalse(result["complex"])
        self.assertIn('"%.17g\\n"', execute.call_args.args[1])

    def test_complex_wave_returns_explicit_components(self):
        query_results = iter(["1", "5", "1", "1", "1"])
        with (
            patch.object(server, "_with_igor", side_effect=lambda fn: fn(object(), 10.0)),
            patch.object(server, "_query", side_effect=lambda *_: next(query_results)),
            patch.object(server, "_raw_execute"),
            patch.object(server, "_read_tmp_file", return_value="2.5,-3\r\n"),
        ):
            result = server.get_wave_data("root:complexWave")

        self.assertEqual(result["values"], [{"real": 2.5, "imag": -3.0}])
        self.assertTrue(result["complex"])

    def test_multidimensional_wave_is_rejected(self):
        query_results = iter(["1", "4", "1", "10", "3"])
        with (
            patch.object(server, "_with_igor", side_effect=lambda fn: fn(object(), 10.0)),
            patch.object(server, "_query", side_effect=lambda *_: next(query_results)),
        ):
            with self.assertRaisesRegex(server.IgorError, "only 1D and 2D"):
                server.get_wave_data("root:image3d")

    def test_2d_numeric_matrix_is_supported(self):
        # WaveExists=1, WaveType=4, WaveType(1)=1, numpnts=6, WaveDims=2
        # DimSize(0)=2 (rows), DimSize(1)=3 (cols)
        # DimOffset(0)=0, DimDelta(0)=1, DimOffset(1)=0, DimDelta(1)=1
        # WaveUnits(0)="s", WaveUnits(1)="nm"
        query_results = iter([
            "1", "4", "1", "6", "2",
            "2", "3", "0", "1", "0", "1", "s", "nm"
        ])
        matrix_text = "1.0\t2.0\t3.0\n4.0\t5.0\t6.0\n"
        with (
            patch.object(server, "_with_igor", side_effect=lambda fn: fn(object(), 10.0)),
            patch.object(server, "_query", side_effect=lambda *_: next(query_results)),
            patch.object(server, "_raw_execute"),
            patch.object(server, "_read_tmp_file", return_value=matrix_text),
        ):
            result = server.get_wave_data("root:matrix2d")

        self.assertEqual(result["dimensions"], 2)
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["columns"], 3)
        self.assertEqual(result["x_units"], "s")
        self.assertEqual(result["y_units"], "nm")
        self.assertEqual(result["values"], [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
        self.assertFalse(result["truncated"])

    def test_empty_numeric_wave_is_supported_with_igor_6_wavedims_result(self):
        query_results = iter(["1", "4", "1", "0", "0"])
        with (
            patch.object(server, "_with_igor", side_effect=lambda fn: fn(object(), 6.3)),
            patch.object(server, "_query", side_effect=lambda *_: next(query_results)),
            patch.object(server, "_raw_execute") as execute,
        ):
            result = server.get_wave_data("root:empty")

        self.assertEqual(result["npnts"], 0)
        self.assertEqual(result["dimensions"], 0)
        self.assertEqual(result["values"], [])
        execute.assert_not_called()

    def test_igor_6_0_numeric_wave_uses_legacy_type_check(self):
        query_results = iter(["1", "4", "1", "1"])
        with (
            patch.object(server, "_with_igor", side_effect=lambda fn: fn(object(), 6.0)),
            patch.object(server, "_query", side_effect=lambda *_: next(query_results)) as query,
            patch.object(server, "_raw_execute"),
            patch.object(server, "_read_tmp_file", return_value="7\r\n"),
        ):
            result = server.get_wave_data("root:legacy")

        self.assertEqual(result["values"], [7.0])
        expressions = [call.args[2] for call in query.call_args_list]
        self.assertFalse(any(expression.endswith(", 1)") for expression in expressions))

    def test_list_waves_does_not_change_active_folder(self):
        query_results = iter(["1", "a;b;"])
        with (
            patch.object(server, "_with_igor", side_effect=lambda fn: fn(object(), 10.0)),
            patch.object(server, "_query", side_effect=lambda *_: next(query_results)) as query,
            patch.object(server, "_raw_execute") as execute,
        ):
            result = server.list_waves("root:data")

        self.assertEqual(result, {"folder": "root:data", "waves": ["a", "b"]})
        self.assertIn("WaveList", query.call_args_list[1].args[2])
        execute.assert_not_called()

    def test_list_waves_igor_6_uses_path_aware_object_functions(self):
        query_results = iter(["1", "2", "a", "b"])
        with (
            patch.object(server, "_with_igor", side_effect=lambda fn: fn(object(), 6.3)),
            patch.object(server, "_query", side_effect=lambda *_: next(query_results)) as query,
            patch.object(server, "_raw_execute") as execute,
        ):
            result = server.list_waves("root:data:")

        self.assertEqual(result, {"folder": "root:data:", "waves": ["a", "b"]})
        self.assertIn("CountObjects", query.call_args_list[1].args[2])
        self.assertIn("GetIndexedObjName", query.call_args_list[2].args[2])
        self.assertIn("GetIndexedObjName", query.call_args_list[3].args[2])
        execute.assert_not_called()

    def test_list_waves_igor_6_rejects_excessive_object_count(self):
        query_results = iter(["1", str(server._MAX_LISTED_WAVES + 1)])
        with (
            patch.object(server, "_with_igor", side_effect=lambda fn: fn(object(), 6.3)),
            patch.object(server, "_query", side_effect=lambda *_: next(query_results)),
            patch.object(server, "_raw_execute") as execute,
        ):
            with self.assertRaisesRegex(server.IgorError, "listing limit"):
                server.list_waves("root:data:")

        execute.assert_not_called()


class _FakePipeClient:
    """Minimal in-memory IgorPipeClient stand-in for offline server tests."""

    def __init__(self, pipe_name: str = r"\\.\pipe\IgorPro_1004_1"):
        self.pipe_name = pipe_name
        self.closed = False
        self.calls: list[tuple[str, object]] = []
        self.responses: dict[str, object] = {}

    def close(self) -> None:
        self.closed = True

    def _dispatch(self, key: str, payload: object):
        self.calls.append((key, payload))
        value = self.responses.get(key)
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return value(payload)
        return value if value is not None else {}

    def capabilities(self, timeout_seconds: float = 5.0):
        return self._dispatch("capabilities", {"timeout": timeout_seconds})

    def execute_command(self, command: str, timeout_seconds: float = 30.0):
        return self._dispatch(
            "execute_command", {"command": command, "timeout": timeout_seconds}
        )

    def call_function(
        self,
        name: str,
        args=None,
        kwargs=None,
        timeout_seconds: float = 30.0,
    ):
        return self._dispatch(
            "call_function",
            {"name": name, "args": args, "kwargs": kwargs, "timeout": timeout_seconds},
        )

    def capture_window(
        self,
        window=None,
        render: str = "auto",
        max_dimension: int = 1200,
        fmt: str = "png",
        timeout_seconds: float = 30.0,
    ):
        return self._dispatch(
            "capture_window",
            {
                "window": window,
                "render": render,
                "max_dimension": max_dimension,
                "fmt": fmt,
                "timeout": timeout_seconds,
            },
        )

    def get_state(self, timeout_seconds: float = 5.0):
        return self._dispatch("get_state", {"timeout": timeout_seconds})

    def compile_procedures(self, timeout_seconds: float = 30.0):
        return self._dispatch("compile_procedures", {"timeout": timeout_seconds})

    def get_compile_status(self, timeout_seconds: float = 10.0):
        return self._dispatch("get_compile_status", {"timeout": timeout_seconds})

    def close_procedures(self, file_paths, timeout_seconds: float = 30.0):
        return self._dispatch(
            "close_procedures",
            {"file_paths": list(file_paths), "timeout": timeout_seconds},
        )


class HybridPipeAndNewToolsTests(unittest.TestCase):
    def test_transport_mode_selection(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IGOR_TRANSPORT", None)
            self.assertEqual(server._transport_mode(), "auto")
        for val, expected in (
            ("pipe", "pipe"),
            ("COM", "com"),
            ("auto", "auto"),
            ("invalid", "auto"),
        ):
            with patch.dict(os.environ, {"IGOR_TRANSPORT": val}):
                self.assertEqual(server._transport_mode(), expected)

    def test_connect_passes_jsonrpc_for_igor_10_04_plus_when_not_com_only(self):
        active = object()
        process = type("Process", (), {"pid": 4321, "poll": lambda self: None})()
        executable = Path(r"C:\Igor10\Igor64.exe")

        with (
            patch.dict(os.environ, {"IGOR_TRANSPORT": "auto"}),
            patch.object(server, "_is_igor_process_running", return_value=False),
            patch.object(
                server,
                "_active_igor",
                side_effect=[server.IgorError("not running"), active],
            ),
            patch.object(server, "_select_igor_executable", return_value=executable),
            patch.object(server, "_executable_igor_version", return_value="10.04.0.0"),
            patch.object(server.subprocess, "Popen", return_value=process) as popen,
            patch.object(server, "_show_new_igor_window_without_activation", return_value=True),
            patch.object(server.time, "sleep"),
        ):
            result = server._connect()
        self.assertIs(result, active)
        self.assertEqual(popen.call_args.args[0], [str(executable), "/JSONRPC"])

        with (
            patch.dict(os.environ, {"IGOR_TRANSPORT": "com"}),
            patch.object(server, "_is_igor_process_running", return_value=False),
            patch.object(
                server,
                "_active_igor",
                side_effect=[server.IgorError("not running"), active],
            ),
            patch.object(server, "_select_igor_executable", return_value=executable),
            patch.object(server, "_executable_igor_version", return_value="10.04.0.0"),
            patch.object(server.subprocess, "Popen", return_value=process) as popen,
            patch.object(server, "_show_new_igor_window_without_activation", return_value=True),
            patch.object(server.time, "sleep"),
        ):
            server._connect()
        self.assertEqual(popen.call_args.args[0], [str(executable)])

    def test_igor_status_and_ensure_igor_report_pipe_transport(self):
        fake = _FakePipeClient(r"\\.\pipe\IgorPro_test")
        fake.responses["capabilities"] = {
            "igorVersion": "10.05",
            "methods": ["igor.command.execute", "igor.fn.call"],
        }
        fake.responses["call_function"] = lambda p: {
            "value": "MyExp" if p["name"] == "IgorInfo" else 10.05
        }
        with (
            patch.object(server, "_try_pipe_client", return_value=fake),
            patch.object(server, "_probe_com_reachable", return_value=True),
        ):
            status = server.igor_status()
            ensured = server.ensure_igor()

        self.assertEqual(status["transport"], "pipe")
        self.assertTrue(status["pipe_reachable"])
        self.assertEqual(status["pipe_name"], r"\\.\pipe\IgorPro_test")
        self.assertEqual(
            status["pipe_capabilities"], ["igor.command.execute", "igor.fn.call"]
        )
        self.assertTrue(status["compatible"])
        self.assertEqual(status["current_experiment"], "MyExp")
        self.assertEqual(ensured["transport"], "pipe")
        self.assertTrue(ensured["pipe_reachable"])
        self.assertEqual(ensured["pipe_name"], r"\\.\pipe\IgorPro_test")
        self.assertTrue(fake.closed)

    def test_execute_igor_pipe_success_and_timeout_clamping(self):
        fake = _FakePipeClient()
        fake.responses["execute_command"] = {
            "history": "  42\n",
            "results": "res42",
            "elapsedSeconds": 0.12,
        }
        with (
            patch.object(server, "_try_pipe_client", return_value=fake),
            patch.object(server, "_igor_dialogs", return_value=[]),
        ):
            res = server.execute_igor("Print 42", timeout_seconds=9999.0)

        self.assertTrue(res["ok"])
        self.assertEqual(res["output"], "42")
        self.assertEqual(res["history"], "  42\n")
        self.assertEqual(res["results"], "res42")
        self.assertEqual(res["elapsed_seconds"], 0.12)
        self.assertEqual(res["transport"], "pipe")
        self.assertEqual(fake.calls[0][1]["timeout"], 600.0)

    def test_execute_igor_no_replay_after_post_dispatch_pipe_error(self):
        for exc in (
            server.igor_pipe.PipeTimeoutError("timed out after dispatch"),
            server.igor_pipe.PipeDispatchError("broken pipe after dispatch"),
        ):
            fake = _FakePipeClient()
            fake.responses["execute_command"] = exc
            with (
                patch.object(server, "_try_pipe_client", return_value=fake),
                patch.object(server, "_igor_dialogs", return_value=[]),
                patch.object(server, "_with_igor") as com_call,
            ):
                res = server._execute_igor_direct("KillWaves/Z w0")

            self.assertFalse(res["ok"])
            self.assertTrue(res["command_started"])
            self.assertFalse(res["outcome_known"])
            self.assertEqual(res["transport"], "pipe")
            com_call.assert_not_called()

    def test_execute_igor_falls_back_to_com_on_pre_dispatch_pipe_unavailable(self):
        fake = _FakePipeClient()
        fake.responses["execute_command"] = server.igor_pipe.PipeNotAvailableError(
            "pipe disconnected before send"
        )
        with (
            patch.object(server, "_try_pipe_client", return_value=fake),
            patch.object(
                server,
                "_with_igor",
                side_effect=lambda fn, **_kwargs: fn(object(), 10.0),
            ) as com_call,
            patch.object(server, "_igor_dialogs", return_value=[]),
            patch.object(server, "_execute_command", return_value="from com"),
        ):
            res = server._execute_igor_direct("Print 1")

        self.assertTrue(res["ok"])
        self.assertEqual(res["output"], "from com")
        self.assertEqual(res["transport"], "com")
        com_call.assert_called_once()

    def test_capture_window_pipe_and_com_fallback_and_wire_passthrough(self):
        png_bytes = b"\x89PNG\r\n\x1a\nfake-png-bytes"
        b64_png = server.base64.b64encode(png_bytes).decode("ascii")
        fake = _FakePipeClient()
        fake.responses["capture_window"] = {
            "window": "Graph0",
            "width": 640,
            "height": 480,
            "data": b64_png,
        }

        async def invoke():
            async with create_connected_server_and_client_session(server.mcp) as client:
                return await client.call_tool("capture_window", {"window": "Graph0"})

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"IGOR_SESSION_DIR": directory}
        ):
            with patch.object(server, "_try_pipe_client", return_value=fake):
                wire_res = asyncio.run(invoke())

        self.assertFalse(wire_res.isError)
        self.assertEqual(wire_res.structuredContent["transport"], "pipe")
        self.assertEqual(wire_res.structuredContent["window"], "Graph0")
        self.assertEqual(len(wire_res.content), 2)
        self.assertEqual(wire_res.content[1].type, "image")
        self.assertEqual(wire_res.content[1].data, b64_png)
        self.assertEqual(wire_res.content[1].mimeType, "image/png")

        # COM fallback path
        def fake_save_pict(_app, command):
            match = server.re.search(r'as "([^"]+)"', command)
            self.assertIsNotNone(match)
            path = match.group(1).replace("\\\\", "\\")
            Path(path).write_bytes(png_bytes)
            return "(command executed; no output)"

        with (
            patch.object(server, "_try_pipe_client", return_value=None),
            patch.object(
                server,
                "_with_igor",
                side_effect=lambda fn, **_kwargs: fn(object(), 10.0),
            ),
            patch.object(server, "_query", return_value="Graph0"),
            patch.object(server, "_execute_command", side_effect=fake_save_pict),
        ):
            com_res = server.capture_window()

        self.assertFalse(com_res.isError)
        self.assertEqual(com_res.structuredContent["transport"], "com")
        self.assertEqual(com_res.structuredContent["window"], "Graph0")
        self.assertEqual(com_res.content[1].data, b64_png)

    def test_call_function_pipe_and_unavailable_and_no_replay(self):
        fake = _FakePipeClient()
        fake.responses["call_function"] = {
            "type": "number",
            "value": 10.05,
            "history": "",
            "results": "",
        }
        with (
            patch.object(server, "_try_pipe_client", return_value=fake),
            patch.object(server, "_igor_dialogs", return_value=[]),
        ):
            res = server.call_function("IgorVersion")
        self.assertTrue(res["ok"])
        self.assertEqual(res["value"], 10.05)
        self.assertEqual(res["transport"], "pipe")

        with patch.object(server, "_try_pipe_client", return_value=None):
            missing = server.call_function("IgorVersion")
        self.assertFalse(missing["ok"])
        self.assertIn("10.04+", missing["error"])

        fake_timeout = _FakePipeClient()
        fake_timeout.responses["call_function"] = server.igor_pipe.PipeTimeoutError(
            "fn timed out"
        )
        with (
            patch.object(server, "_try_pipe_client", return_value=fake_timeout),
            patch.object(server, "_igor_dialogs", return_value=[]),
        ):
            timed_out = server.call_function("SlowFunc")
        self.assertFalse(timed_out["ok"])
        self.assertTrue(timed_out["command_started"])
        self.assertFalse(timed_out["outcome_known"])

    def test_abort_igor_and_get_igor_state_never_acquire_locks(self):
        fake = _FakePipeClient()
        fake.responses["get_state"] = {"busy": True, "modalDialog": True}
        dialog = {"hwnd": 77, "title": "Choose a procedure"}
        with (
            patch.object(
                server.igor_pipe,
                "cancel_igor_request",
                return_value={"cancelled": True},
            ) as cancel_mock,
            patch.object(server, "_try_pipe_client", return_value=fake),
            patch.object(server, "_igor_dialogs", return_value=[dialog]),
            patch.object(server, "_capture_compilation_errors", return_value=[]),
        ):
            with server._lock, server._command_worker_lock, server._pipe_pool_lock:
                abort_res = server.abort_igor(timeout_seconds=5.0)
                state_res = server.get_igor_state()

        cancel_mock.assert_called_once_with(timeout_seconds=5.0)
        self.assertTrue(abort_res["ok"])
        self.assertTrue(abort_res["cancelled"])
        self.assertTrue(state_res["ok"])
        self.assertEqual(state_res["transport"], "pipe")
        self.assertTrue(state_res["busy"])
        self.assertEqual(state_res["dialogs_open"], 1)

    def test_open_and_close_procedure(self):
        with self.assertRaisesRegex(server.IgorError, "absolute"):
            server.open_procedure("relative.ipf")
        with self.assertRaisesRegex(server.IgorError, "does not exist"):
            server.open_procedure(r"C:\nonexistent_igor_mcp_test_file.ipf")

        with tempfile.TemporaryDirectory() as directory:
            ipf = Path(directory) / "my_proc.ipf"
            ipf.write_text("#pragma rtGlobals=3\n", encoding="utf-8")
            with patch.object(
                server,
                "_execute_igor_in_worker",
                return_value={
                    "ok": True,
                    "output": "",
                    "error": None,
                    "dialogs_open": 0,
                    "dialog_details": [],
                    "compilation_errors": [],
                    "transport": "pipe",
                },
            ) as exec_worker:
                res = server.open_procedure(str(ipf))
            self.assertTrue(res["ok"])
            self.assertIn('OpenProc "', exec_worker.call_args.args[0])

        fake = _FakePipeClient()
        fake.responses["close_procedures"] = {
            "closed": ["a.ipf"],
            "reopened": [],
            "notClosed": [],
            "compile": {"compiled": True},
        }
        with patch.object(server, "_try_pipe_client", return_value=fake):
            closed = server.close_procedure(["a.ipf"])
        self.assertTrue(closed["ok"])
        self.assertEqual(closed["closed"], ["a.ipf"])

        with patch.object(server, "_try_pipe_client", return_value=None):
            no_pipe = server.close_procedure(["a.ipf"])
        self.assertFalse(no_pipe["ok"])
        self.assertIn("10.04+", no_pipe["error"])

    def test_compile_procedures_and_get_compile_status(self):
        fake = _FakePipeClient()
        fake.responses["compile_procedures"] = {
            "compiled": True,
            "reloaded": ["a.ipf"],
            "reloadFailed": [],
            "errors": [],
        }
        fake.responses["get_compile_status"] = {"compiled": True, "needsCompile": False}
        with patch.object(server, "_try_pipe_client", return_value=fake):
            comp = server.compile_procedures()
            status = server.get_compile_status()
        self.assertTrue(comp["ok"])
        self.assertEqual(comp["reloaded"], ["a.ipf"])
        self.assertTrue(status["compiled"])

        with (
            patch.object(server, "_try_pipe_client", return_value=None),
            patch.object(server, "_compilation_error_dialogs", return_value=[]),
        ):
            com_status = server.get_compile_status()
        self.assertEqual(com_status["transport"], "com")
        self.assertIn("native", com_status["note"])

    def test_list_windows_experiment_info_and_system_information(self):
        fake = _FakePipeClient()
        fake.responses["call_function"] = lambda p: {
            "value": (
                "Graph0;Panel0;"
                if p["name"] == "WinList"
                else "MyExp"
                if (p["name"] == "IgorInfo" and p["args"] == [1])
                else "MyExp.pxp"
                if (p["name"] == "IgorInfo" and p["args"] == [12])
                else "IGORVERS:10.05"
                if (p["name"] == "IgorInfo" and p["args"] == [0])
                else "OS:Windows"
                if (p["name"] == "IgorInfo" and p["args"] == [3])
                else "6.7.2"
                if (p["name"] == "IgorInfo" and p["args"] == [6])
                else "XOP1;XOP2;"
                if (p["name"] == "IgorInfo" and p["args"] == [10])
                else 16
            )
        }
        fake.responses["execute_command"] = {"results": r"C:\Data\\"}

        with patch.object(server, "_try_pipe_client", return_value=fake):
            wins = server.list_windows(types=["graph", "panel"], match="*0")
            exp = server.get_experiment_info()
            sys_info = server.get_system_information()

        self.assertEqual(wins["windows"], ["Graph0", "Panel0"])
        self.assertEqual(wins["count"], 2)
        self.assertEqual(fake.calls[0][1]["args"], ["*0", ";", "WIN:65"])
        self.assertEqual(exp["name"], "MyExp")
        self.assertEqual(exp["file_name"], "MyExp.pxp")
        self.assertTrue(exp["saved"])
        self.assertEqual(sys_info["qt_version"], "6.7.2")
        self.assertEqual(sys_info["loaded_xops"], ["XOP1", "XOP2"])
        self.assertEqual(sys_info["thread_processor_count"], 16)

    def test_capture_window_and_list_windows_reject_newline_and_unsafe_injection(self):
        # Newline / control-char injection rejected before touching COM or pipe
        bad_capture = server.capture_window(window='Graph0"\nKillWaves/A/Z')
        self.assertTrue(bad_capture.isError)
        self.assertIn("newlines", bad_capture.structuredContent["error"])

        # Unsafe window name rejected in COM SavePICT fallback
        with (
            patch.object(server, "_try_pipe_client", return_value=None),
            patch.object(
                server,
                "_with_igor",
                side_effect=lambda fn, **_kwargs: fn(object(), 10.0),
            ),
            patch.object(server, "_execute_command") as com_exec,
        ):
            unsafe_com = server.capture_window(window="Graph0; KillWaves/A/Z")
        self.assertTrue(unsafe_com.isError)
        self.assertIn("unsupported characters", unsafe_com.structuredContent["error"])
        com_exec.assert_not_called()

        # Render enum matches Igor Pro 10.04+
        bad_render = server.capture_window(render="bitmap")
        self.assertTrue(bad_render.isError)
        self.assertIn("screenGraphOnly", bad_render.structuredContent["error"])

        # list_windows rejects newline injection
        with self.assertRaisesRegex(server.IgorError, "newlines"):
            server.list_windows(match='*"\nKillWaves/A/Z')

    def test_open_procedure_enforces_ipf_extension_and_rejects_newlines(self):
        with self.assertRaisesRegex(server.IgorError, "newlines"):
            server.open_procedure("C:\\tmp\\foo.ipf\nKillWaves/A/Z")

        with tempfile.TemporaryDirectory() as directory:
            non_ipf = Path(directory) / "notes.txt"
            non_ipf.write_text("hello\n", encoding="utf-8")
            with self.assertRaisesRegex(server.IgorError, r"\.ipf"):
                server.open_procedure(str(non_ipf))

        with self.assertRaisesRegex(server.IgorError, "newlines"):
            server.close_procedure(["C:\\tmp\\foo.ipf\nbar"])

    def test_broken_pipe_after_connect_falls_back_in_ensure_igor_and_not_reachable_in_status(self):
        broken = _FakePipeClient(r"\\.\pipe\IgorPro_dead")
        broken.responses["capabilities"] = server.igor_pipe.PipeDispatchError(
            "pipe broke on capabilities"
        )
        with (
            patch.dict(os.environ, {"IGOR_TRANSPORT": "auto"}),
            patch.object(server, "_try_pipe_client", return_value=broken),
            patch.object(
                server,
                "_with_igor",
                side_effect=lambda fn, **_kwargs: fn(object(), 10.05),
            ),
            patch.object(server, "_query", return_value="FallbackExp"),
        ):
            status = server.igor_status()
            ensured = server.ensure_igor()

        self.assertFalse(status["pipe_reachable"])
        self.assertEqual(status["transport"], "com")
        self.assertTrue(status["com_reachable"])
        self.assertFalse(ensured["pipe_reachable"])
        self.assertEqual(ensured["transport"], "com")
        self.assertEqual(ensured["current_experiment"], "FallbackExp")

    def test_capture_window_session_jsonl_omits_base64_png_data(self):
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"A" * 128
        b64_png = server.base64.b64encode(png_bytes).decode("ascii")
        fake = _FakePipeClient()
        fake.responses["capture_window"] = {
            "window": "Graph0",
            "width": 320,
            "height": 240,
            "data": b64_png,
        }

        async def invoke():
            async with create_connected_server_and_client_session(server.mcp) as client:
                return await client.call_tool(
                    "capture_window", {"window": "Graph0", "render": "screenGraphOnly"}
                )

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"IGOR_SESSION_DIR": directory}
        ):
            with patch.object(server, "_try_pipe_client", return_value=fake):
                wire_res = asyncio.run(invoke())
            self.assertFalse(wire_res.isError)
            jsonl_files = list(Path(directory).rglob("*.jsonl"))
            self.assertTrue(jsonl_files)
            log_text = "\n".join(
                f.read_text(encoding="utf-8") for f in jsonl_files
            )
            self.assertIn("capture_window", log_text)
            self.assertNotIn(b64_png, log_text)

    def test_igor_transport_pipe_mode_fails_closed_without_com_fallback(self):
        with (
            patch.dict(os.environ, {"IGOR_TRANSPORT": "pipe"}),
            patch.object(server, "_try_pipe_client", return_value=None),
            patch.object(server, "_with_igor") as com_call,
        ):
            exec_res = server._execute_igor_direct("Print 1")
            status_res = server.igor_status()
            cap_res = server.capture_window()
            comp_res = server.compile_procedures()
            cstat_res = server.get_compile_status()
            with self.assertRaisesRegex(server.IgorError, "IGOR_TRANSPORT=pipe"):
                server.list_windows()

        com_call.assert_not_called()
        self.assertFalse(exec_res["ok"])
        self.assertEqual(exec_res["transport"], "pipe")
        self.assertFalse(status_res["pipe_reachable"])
        self.assertEqual(status_res["transport"], "none")
        self.assertTrue(cap_res.isError)
        self.assertFalse(comp_res["ok"])
        self.assertFalse(cstat_res["ok"])

    def test_tool_profile_core_omits_optional_report_tools(self):
        async def _list_names():
            return {tool.name for tool in await server.mcp.list_tools()}

        with patch.dict(os.environ, {"IGOR_TOOL_PROFILE": "full"}):
            full_names = asyncio.run(_list_names())
        with patch.dict(os.environ, {"IGOR_TOOL_PROFILE": "core"}):
            core_names = asyncio.run(_list_names())

        self.assertEqual(len(full_names), 36)
        self.assertEqual(len(core_names), 33)
        for report_tool in ("create_html_report", "open_report_graph", "apply_html_report"):
            self.assertIn(report_tool, full_names)
            self.assertNotIn(report_tool, core_names)

    def test_pipe_first_list_waves_read_variable_and_list_procedures(self):
        class _DataPipeClient(_FakePipeClient):
            def call_function(self, name, args=None, kwargs=None, timeout_seconds=30.0):
                self.calls.append(("call_function", name, args, kwargs, timeout_seconds))
                if name == "DataFolderExists":
                    return {"type": "number", "value": 1}
                if name == "exists":
                    return {"type": "number", "value": 2}
                if name == "StrVarOrDefault":
                    if args and args[0] == "root:s_label":
                        return {"type": "string", "value": "sample_a"}
                    return {"type": "string", "value": args[1] if args and len(args) > 1 else ""}
                if name == "NumVarOrDefault":
                    return {"type": "number", "value": 3.5}
                if name == "WinList":
                    return {"type": "string", "value": "Procedure;MyProcs.ipf;"}
                if name == "FunctionList":
                    if args and args[2] == "WIN:MyProcs.ipf":
                        return {"type": "string", "value": "MyFunc;"}
                    return {"type": "string", "value": ""}
                return {"type": "string", "value": ""}

            def execute_command(self, command, timeout_seconds=30.0):
                self.calls.append(("execute_command", command, timeout_seconds))
                return {"results": "w_x;w_y;", "history": "", "elapsedSeconds": 0.001}

        fake = _DataPipeClient()
        with (
            patch.object(server, "_try_pipe_client", return_value=fake),
            patch.object(server, "_with_igor") as com_call,
        ):
            waves = server.list_waves("root:TestDF")
            str_var = server.read_variable("root:s_label")
            num_var = server.read_variable("root:v_gain")
            procs = server.list_procedures()

        com_call.assert_not_called()
        self.assertEqual(waves, {"folder": "root:TestDF", "waves": ["w_x", "w_y"]})
        self.assertEqual(str_var, {"name": "root:s_label", "type": "string", "value": "sample_a"})
        self.assertEqual(num_var, {"name": "root:v_gain", "type": "numeric", "value": 3.5})
        self.assertIn("MyProcs.ipf", procs)
        self.assertIn("MyFunc", procs)

    def test_quick_polish_png_ihdr_dimensions_and_max_dimension_bounds(self):
        import struct

        png_bytes = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + struct.pack(">II", 640, 480)
            + b"\x08\x02\x00\x00\x00"
        )
        b64_png = server.base64.b64encode(png_bytes).decode("ascii")
        fake = _FakePipeClient()
        # Pipe response intentionally omits width & height
        fake.responses["capture_window"] = {
            "window": "Graph0",
            "data": b64_png,
        }
        with patch.object(server, "_try_pipe_client", return_value=fake):
            res = server.capture_window(window="Graph0")
        self.assertFalse(res.isError)
        self.assertEqual(res.structuredContent["width"], 640)
        self.assertEqual(res.structuredContent["height"], 480)

        # COM fallback also populates width & height from PNG IHDR
        def fake_com_exec(_app, cmd):
            match = server.re.search(r'as "([^"]+)"', cmd)
            self.assertIsNotNone(match)
            out_path = match.group(1).replace("\\\\", "\\")
            Path(out_path).write_bytes(png_bytes)
            return ""

        with (
            patch.dict(os.environ, {"IGOR_TRANSPORT": "com"}),
            patch.object(
                server,
                "_with_igor",
                side_effect=lambda fn, **_kwargs: fn(object(), 10.05),
            ),
            patch.object(server, "_query", side_effect=["1"]),
            patch.object(server, "_execute_command", side_effect=fake_com_exec),
        ):
            com_res = server.capture_window(window="Graph0")
        self.assertFalse(com_res.isError)
        self.assertEqual(com_res.structuredContent["width"], 640)
        self.assertEqual(com_res.structuredContent["height"], 480)

        # Direct Python calls enforce max_dimension bounds
        for bad_dim in (0, 63, 8193, True, False):
            err_res = server.capture_window(max_dimension=bad_dim)
            self.assertTrue(err_res.isError)
            self.assertIn("64 and 8192", err_res.structuredContent["error"])

    def test_quick_polish_compile_errors_list_derived_state_and_list_windows_raw_newline(self):
        # 1. compile_procedures populates errors list from singular error object
        fake = _FakePipeClient()
        singular_err = {"file": "MyProc.ipf", "line": 42, "message": "syntax error"}
        fake.responses["compile_procedures"] = {
            "compiled": False,
            "error": singular_err,
        }
        with patch.object(server, "_try_pipe_client", return_value=fake):
            comp = server.compile_procedures()
        self.assertFalse(comp["ok"])
        self.assertFalse(comp["compiled"])
        self.assertEqual(comp["error"], singular_err)
        self.assertEqual(comp["errors"], [singular_err])

        # 2. get_igor_state derives busy from pipe_state fields when busy is omitted
        fake_busy = _FakePipeClient()
        fake_busy.responses["get_state"] = {
            "procedureRunning": True,
            "loadingExperiment": False,
            "runningRequest": False,
            "queuedRequests": 0,
            "otherQueuedRequests": 0,
        }
        fake_idle = _FakePipeClient()
        fake_idle.responses["get_state"] = {
            "procedureRunning": False,
            "loadingExperiment": False,
            "runningRequest": False,
            "queuedRequests": 0,
            "otherQueuedRequests": 0,
        }
        with (
            patch.object(server, "_igor_dialogs", return_value=[]),
            patch.object(server, "_try_pipe_client", return_value=fake_busy),
        ):
            st_busy = server.get_igor_state()
        with (
            patch.object(server, "_igor_dialogs", return_value=[]),
            patch.object(server, "_try_pipe_client", return_value=fake_idle),
        ):
            st_idle = server.get_igor_state()
        self.assertTrue(st_busy["busy"])
        self.assertFalse(st_idle["busy"])

        # 3. get_compile_status derives needs_compile from compiled bool
        fake_cs_ok = _FakePipeClient()
        fake_cs_ok.responses["get_compile_status"] = {"compiled": True}
        fake_cs_dirty = _FakePipeClient()
        fake_cs_dirty.responses["get_compile_status"] = {"compiled": False}
        with patch.object(server, "_try_pipe_client", return_value=fake_cs_ok):
            cs_ok = server.get_compile_status()
        with patch.object(server, "_try_pipe_client", return_value=fake_cs_dirty):
            cs_dirty = server.get_compile_status()
        self.assertIs(cs_ok["needs_compile"], False)
        self.assertIs(cs_dirty["needs_compile"], True)

        # 4. list_windows rejects leading/trailing newlines before strip()
        for bad_match in ("\nGraph*", "Graph*\n", "Graph*\r\n", "\x00Graph*"):
            with self.assertRaisesRegex(server.IgorError, "newlines"):
                server.list_windows(match=bad_match)

    def test_quick_polish_server_info_version_and_call_function_schema_docs(self):
        self.assertEqual(server.mcp._mcp_server.version, "0.5.0")

        async def _check_wire():
            async with create_connected_server_and_client_session(server.mcp) as client:
                init = await client.initialize()
                tools = await client.list_tools()
                return init, tools

        init_result, tools_result = asyncio.run(_check_wire())
        self.assertEqual(init_result.serverInfo.version, "0.5.0")

        call_fn_tool = next(t for t in tools_result.tools if t.name == "call_function")
        args_desc = call_fn_tool.inputSchema["properties"]["args"]["description"]
        self.assertIn("wave", args_desc)
        self.assertIn("datafolder", args_desc)
        self.assertIn("real", args_desc)
        self.assertIn("NaN", args_desc)
        self.assertIn("Infinity", args_desc)

    def test_pipe_first_get_wave_data_1d_complex_and_2d(self):
        class _WavePipeClient(_FakePipeClient):
            def __init__(self, mode_kind: str):
                super().__init__()
                self.mode_kind = mode_kind

            def call_function(self, name, args=None, kwargs=None, timeout_seconds=30.0):
                self.calls.append(("call_function", name, args, kwargs, timeout_seconds))
                if name == "WaveExists":
                    return {"type": "number", "value": 1}
                if name == "WaveType":
                    if args and len(args) > 1 and args[1] == 1:
                        return {"type": "number", "value": 1}  # numeric
                    if self.mode_kind == "1d_complex":
                        return {"type": "number", "value": 5}  # 4 (double) | 1 (complex)
                    return {"type": "number", "value": 4}
                if name == "numpnts":
                    return {"type": "number", "value": 4 if self.mode_kind == "2d" else 3}
                if name == "WaveDims":
                    return {"type": "number", "value": 2 if self.mode_kind == "2d" else 1}
                if name == "DimSize":
                    return {"type": "number", "value": 2}
                if name == "DimOffset":
                    return {"type": "number", "value": 0.0 if args[1] == 0 else 10.0}
                if name == "DimDelta":
                    return {"type": "number", "value": 0.5 if args[1] == 0 else 2.0}
                if name == "WaveUnits":
                    return {"type": "string", "value": "s" if args[1] == 0 else "m"}
                return {"type": "number", "value": 0}

            def execute_command(self, command, timeout_seconds=30.0):
                self.calls.append(("execute_command", command, timeout_seconds))
                if self.mode_kind == "1d_real":
                    payload = "1.5\nNaN\nInf\n"
                elif self.mode_kind == "1d_complex":
                    payload = "1,2\n3,-4\nNaN,Inf\n"
                else:
                    payload = "1\t2\n3\t4\n"
                match = server.re.search(r'Open \S+ as "([^"]+)"', command)
                if match:
                    out_path = match.group(1).replace("\\\\", "\\")
                    Path(out_path).write_text(payload, encoding="utf-8")
                return {"results": "", "history": ""}

        with patch.object(server, "_with_igor") as com_call:
            with patch.object(server, "_try_pipe_client", return_value=_WavePipeClient("1d_real")):
                w1 = server.get_wave_data("root:w_real", max_points=10)
            with patch.object(server, "_try_pipe_client", return_value=_WavePipeClient("1d_complex")):
                wc = server.get_wave_data("root:w_cplx", max_points=10)
            with patch.object(server, "_try_pipe_client", return_value=_WavePipeClient("2d")):
                w2 = server.get_wave_data("root:w_mat", max_points=10)

        com_call.assert_not_called()
        self.assertEqual(
            w1,
            {
                "name": "root:w_real",
                "npnts": 3,
                "dimensions": 1,
                "wave_type_code": 4,
                "complex": False,
                "start": 0,
                "truncated": False,
                "values": [1.5, "NaN", "Inf"],
            },
        )
        self.assertEqual(
            wc["values"],
            [
                {"real": 1.0, "imag": 2.0},
                {"real": 3.0, "imag": -4.0},
                {"real": "NaN", "imag": "Inf"},
            ],
        )
        self.assertTrue(wc["complex"])
        self.assertEqual(
            w2,
            {
                "name": "root:w_mat",
                "npnts": 4,
                "dimensions": 2,
                "rows": 2,
                "columns": 2,
                "x_offset": 0.0,
                "x_delta": 0.5,
                "y_offset": 10.0,
                "y_delta": 2.0,
                "x_units": "s",
                "y_units": "m",
                "wave_type_code": 4,
                "complex": False,
                "start": 0,
                "truncated": False,
                "values": [[1.0, 2.0], [3.0, 4.0]],
            },
        )

        # IGOR_TRANSPORT=pipe fails closed without COM fallback
        with (
            patch.dict(os.environ, {"IGOR_TRANSPORT": "pipe"}),
            patch.object(server, "_try_pipe_client", return_value=None),
            patch.object(server, "_with_igor") as com_call_closed,
        ):
            with self.assertRaisesRegex(server.IgorError, "IGOR_TRANSPORT=pipe"):
                server.get_wave_data("root:w_real")
        com_call_closed.assert_not_called()

    def test_pipe_first_import_delimited_text(self):
        class _ImportPipeClient(_FakePipeClient):
            def __init__(self):
                super().__init__()
                self.temp_prefix = None

            def call_function(self, name, args=None, kwargs=None, timeout_seconds=30.0):
                self.calls.append(("call_function", name, args, kwargs, timeout_seconds))
                if name == "DataFolderExists":
                    return {"type": "number", "value": 1}
                if name == "WaveList":
                    pattern = args[0] if args else ""
                    if self.temp_prefix and pattern.startswith(self.temp_prefix):
                        return {"type": "string", "value": f"{self.temp_prefix}0;{self.temp_prefix}1;"}
                    return {"type": "string", "value": ""}
                return {"type": "string", "value": ""}

            def execute_command(self, command, timeout_seconds=30.0):
                self.calls.append(("execute_command", command, timeout_seconds))
                if command.startswith('fprintf 0, "%s", WaveList'):
                    return {"results": "", "history": ""}
                if command.startswith("LoadWave/J/D/A="):
                    match = server.re.search(r"/A=([A-Za-z0-9_]+)/O", command)
                    if match:
                        self.temp_prefix = match.group(1)
                    return {"results": "", "history": ""}
                return {"results": "", "history": ""}

        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "sample.csv"
            csv_path.write_text("x,y\n1.0,2.5\n3.0,4.5\n", encoding="utf-8")

            fake = _ImportPipeClient()
            with (
                patch.object(server, "_try_pipe_client", return_value=fake),
                patch.object(server, "_with_igor") as com_call,
            ):
                res = server.import_delimited_text(
                    str(csv_path), prefix="imp", folder="root:"
                )

            com_call.assert_not_called()
            self.assertEqual(
                res,
                {
                    "source_path": str(csv_path.resolve()),
                    "folder": "root:",
                    "prefix": "imp",
                    "rows": 2,
                    "columns": 2,
                    "waves": ["imp0", "imp1"],
                    "headers": ["x", "y"],
                    "portable_object_name_limit": server._MAX_IGOR_OBJECT_NAME_LENGTH,
                },
            )

            # IGOR_TRANSPORT=pipe fails closed without COM fallback
            with (
                patch.dict(os.environ, {"IGOR_TRANSPORT": "pipe"}),
                patch.object(server, "_try_pipe_client", return_value=None),
                patch.object(server, "_with_igor") as com_call_closed,
            ):
                with self.assertRaisesRegex(server.IgorError, "IGOR_TRANSPORT=pipe"):
                    server.import_delimited_text(str(csv_path), prefix="imp", folder="root:")
            com_call_closed.assert_not_called()

    def test_pipe_first_open_igor_experiment_and_no_replay_on_dispatch_error(self):
        class _OpenPipeClient(_FakePipeClient):
            def call_function(self, name, args=None, kwargs=None, timeout_seconds=30.0):
                self.calls.append(("call_function", name, args, kwargs, timeout_seconds))
                if name == "IgorInfo":
                    return {"type": "string", "value": "MySavedExp"}
                return {"type": "string", "value": ""}

        with tempfile.TemporaryDirectory() as directory:
            pxp_path = Path(directory) / "MySavedExp.pxp"
            pxp_path.write_bytes(b"IGOR")

            fake = _OpenPipeClient()
            with (
                patch.object(server, "_igor_dialogs", return_value=[]),
                patch.object(server, "_try_pipe_client", return_value=fake),
                patch.object(server, "_with_igor") as com_call,
            ):
                res = server.open_igor_experiment(str(pxp_path))

            com_call.assert_not_called()
            self.assertEqual(
                res,
                {
                    "ok": True,
                    "file_path": str(pxp_path.resolve()),
                    "current_experiment": "MySavedExp",
                    "error": None,
                    "load_started": True,
                    "load_completed": True,
                    "outcome_known": True,
                    "dialogs_open": 0,
                    "dialog_titles": [],
                    "dialog_details": [],
                    "compilation_errors": [],
                },
            )
            exec_cmds = [c[1]["command"] for c in fake.calls if c[0] == "execute_command"]
            self.assertTrue(any('Execute/P/Q "LOADFILE ' in cmd for cmd in exec_cmds))

            # Post-dispatch PipeDispatchError must NOT replay via COM
            broken = _FakePipeClient()
            broken.responses["execute_command"] = server.igor_pipe.PipeDispatchError(
                "pipe disconnected after LOADFILE"
            )
            with (
                patch.object(server, "_igor_dialogs", return_value=[]),
                patch.object(server, "_try_pipe_client", return_value=broken),
                patch.object(server, "_with_igor") as com_call_no_replay,
            ):
                fail_res = server.open_igor_experiment(str(pxp_path))
            com_call_no_replay.assert_not_called()
            self.assertFalse(fail_res["ok"])
            self.assertTrue(fail_res["load_started"])
            self.assertFalse(fail_res["outcome_known"])

            # IGOR_TRANSPORT=pipe fails closed without COM fallback
            with (
                patch.dict(os.environ, {"IGOR_TRANSPORT": "pipe"}),
                patch.object(server, "_igor_dialogs", return_value=[]),
                patch.object(server, "_try_pipe_client", return_value=None),
                patch.object(server, "_with_igor") as com_call_closed,
            ):
                closed_res = server.open_igor_experiment(str(pxp_path))
            com_call_closed.assert_not_called()
            self.assertFalse(closed_res["ok"])
            self.assertFalse(closed_res["load_started"])
            self.assertTrue(closed_res["outcome_known"])
            self.assertIn("IGOR_TRANSPORT=pipe", closed_res["error"])

    def test_inspect_experiment_pipe_path_and_truncation(self):
        tree = {
            "root:": {
                "waves": [f"w{i}" for i in range(25)],
                "vars": 2,
                "strings": 1,
                "subs": ["RunA", "RunB"],
            },
            "root:RunA:": {
                "waves": ["signal", "fit_signal"],
                "vars": 1,
                "strings": 0,
                "subs": ["Nested"],
            },
            "root:RunB:": {
                "waves": ["baseline"],
                "vars": 0,
                "strings": 0,
                "subs": [],
            },
            "root:RunA:Nested:": {
                "waves": ["deepWave"],
                "vars": 0,
                "strings": 0,
                "subs": [],
            },
        }
        windows_by_bit = {
            "WIN:1": "Graph0;Graph1;",
            "WIN:2": "Table0;",
            "WIN:4": "",
            "WIN:16": "Notebook0;",
            "WIN:64": "Panel0;",
            "WIN:65536": "",
        }

        class _InspectPipeClient(_FakePipeClient):
            def call_function(self, name, args=None, kwargs=None, timeout_seconds=30.0):
                self.calls.append(("call_function", name, args, kwargs, timeout_seconds))
                if name == "DataFolderExists":
                    return {"type": "number", "value": 1 if args[0] in tree else 0}
                if name == "IgorInfo":
                    if args == [1]:
                        return {"type": "string", "value": "LabExp"}
                    if args == [12]:
                        return {"type": "string", "value": "LabExp.pxp"}
                if name == "CountObjects":
                    node = tree[args[0]]
                    obj_type = args[1]
                    counts = {
                        1: len(node["waves"]),
                        2: node["vars"],
                        3: node["strings"],
                        4: len(node["subs"]),
                    }
                    return {"type": "number", "value": counts[obj_type]}
                if name == "GetIndexedObjName":
                    node = tree[args[0]]
                    obj_type, idx = args[1], args[2]
                    items = node["waves"] if obj_type == 1 else node["subs"]
                    return {"type": "string", "value": items[idx]}
                if name == "WinList":
                    return {"type": "string", "value": windows_by_bit.get(args[2], "")}
                return {"type": "string", "value": ""}

            def execute_command(self, command, timeout_seconds=30.0):
                self.calls.append(("execute_command", command, timeout_seconds))
                if "PathInfo home" in command:
                    return {"results": r"C:\Data\Experiments", "history": ""}
                return {"results": "", "history": ""}

        # Full inspection over pipe with root_folder normalized ("root" -> "root:")
        fake = _InspectPipeClient()
        with (
            patch.object(server, "_try_pipe_client", return_value=fake),
            patch.object(server, "_with_igor") as com_call,
        ):
            res = server.inspect_experiment(root_folder="root", max_depth=4, max_folders=50)

        com_call.assert_not_called()
        self.assertTrue(res["ok"])
        self.assertEqual(res["transport"], "pipe")
        self.assertEqual(
            res["experiment"],
            {
                "name": "LabExp",
                "file_name": "LabExp.pxp",
                "path": r"C:\Data\Experiments",
                "saved": True,
            },
        )
        self.assertEqual(res["root_folder"], "root:")
        self.assertEqual(res["folder_count"], 4)
        self.assertEqual(res["total_waves"], 29)
        self.assertFalse(res["truncated"])
        self.assertEqual(
            [f["path"] for f in res["folders"]],
            ["root:", "root:RunA:", "root:RunB:", "root:RunA:Nested:"],
        )
        # First folder had 25 waves -> capped at 20 with waves_truncated=True
        self.assertEqual(len(res["folders"][0]["waves"]), 20)
        self.assertTrue(res["folders"][0]["waves_truncated"])
        self.assertEqual(res["folders"][1]["waves"], ["signal", "fit_signal"])
        self.assertFalse(res["folders"][1]["waves_truncated"])
        self.assertEqual(
            res["windows"],
            {
                "graph": ["Graph0", "Graph1"],
                "table": ["Table0"],
                "layout": [],
                "panel": ["Panel0"],
                "notebook": ["Notebook0"],
                "gizmo": [],
            },
        )
        # Never calls SetDataFolder
        exec_cmds = [c[1] for c in fake.calls if c[0] == "execute_command"]
        self.assertFalse(any("SetDataFolder" in cmd for cmd in exec_cmds))

        # max_depth=2 truncates root:RunA:Nested: (depth 2)
        fake_depth = _InspectPipeClient()
        with patch.object(server, "_try_pipe_client", return_value=fake_depth):
            depth_res = server.inspect_experiment(max_depth=2, include_wave_names=False)
        self.assertEqual(depth_res["folder_count"], 3)
        self.assertTrue(depth_res["truncated"])
        self.assertTrue(all(f["waves"] == [] and not f["waves_truncated"] for f in depth_res["folders"]))

        # max_folders=2 truncates remaining folders
        fake_max = _InspectPipeClient()
        with patch.object(server, "_try_pipe_client", return_value=fake_max):
            max_res = server.inspect_experiment(max_folders=2)
        self.assertEqual(max_res["folder_count"], 2)
        self.assertTrue(max_res["truncated"])

        # Missing folder raises IgorError
        fake_missing = _InspectPipeClient()
        with patch.object(server, "_try_pipe_client", return_value=fake_missing):
            with self.assertRaisesRegex(server.IgorError, "does not exist"):
                server.inspect_experiment(root_folder="root:Nope:")

        # Invalid root_folder syntax raises IgorError
        with self.assertRaisesRegex(server.IgorError, "root:"):
            server.inspect_experiment(root_folder="notroot:")
        with self.assertRaisesRegex(server.IgorError, "empty path component"):
            server.inspect_experiment(root_folder="root::bad:")

    def test_inspect_experiment_com_fallback_and_pipe_fail_closed(self):
        def fake_query(_app, _fmt, expr):
            if expr == 'DataFolderExists("root:")':
                return "1"
            if expr == "IgorInfo(1)":
                return "ComExp"
            if expr == "IgorInfo(12)":
                return ""
            if 'CountObjects("root:", 1)' in expr:
                return "2|1|0|1"
            if 'CountObjects("root:Sub:", 1)' in expr:
                return "1|0|0|0"
            if expr == 'GetIndexedObjName("root:", 1, 0)':
                return "w0"
            if expr == 'GetIndexedObjName("root:", 1, 1)':
                return "w1"
            if expr == 'GetIndexedObjName("root:", 4, 0)':
                return "Sub"
            if expr == 'GetIndexedObjName("root:Sub:", 1, 0)':
                return "subWave"
            if expr == 'WinList("*", ";", "WIN:1")':
                return "Graph0;"
            if expr.startswith('WinList("*", ";", "WIN:'):
                return ""
            self.fail(f"Unexpected COM query: {expr}")

        with (
            patch.object(server, "_try_pipe_client", return_value=None),
            patch.object(
                server, "_with_igor", side_effect=lambda fn, **kw: fn(object(), 10.0)
            ),
            patch.object(server, "_query", side_effect=fake_query),
        ):
            res = server.inspect_experiment()

        self.assertTrue(res["ok"])
        self.assertEqual(res["transport"], "com")
        self.assertEqual(
            res["experiment"],
            {"name": "ComExp", "file_name": "", "path": "", "saved": False},
        )
        self.assertEqual(res["folder_count"], 2)
        self.assertEqual(res["total_waves"], 3)
        self.assertFalse(res["truncated"])
        self.assertEqual(res["folders"][0]["waves"], ["w0", "w1"])
        self.assertEqual(res["folders"][1]["path"], "root:Sub:")
        self.assertEqual(res["folders"][1]["waves"], ["subWave"])
        self.assertEqual(res["windows"]["graph"], ["Graph0"])
        self.assertEqual(set(res["windows"]), {"graph", "table", "layout", "panel", "notebook", "gizmo"})

        # IGOR_TRANSPORT=pipe fails closed without COM fallback
        with (
            patch.dict(os.environ, {"IGOR_TRANSPORT": "pipe"}),
            patch.object(server, "_try_pipe_client", return_value=None),
            patch.object(server, "_with_igor") as com_call_closed,
        ):
            with self.assertRaisesRegex(server.IgorError, "IGOR_TRANSPORT=pipe"):
                server.inspect_experiment()
        com_call_closed.assert_not_called()

    def test_progress_heartbeat_emitted_over_real_mcp_session(self):
        progress_events: list[tuple[float, float | None, str | None]] = []

        async def _progress_cb(
            progress: float, total: float | None, message: str | None
        ) -> None:
            progress_events.append((progress, total, message))

        def _slow_execute(_command, timeout_seconds=None):
            server.time.sleep(0.18)
            return {
                "ok": True,
                "output": "done",
                "error": None,
                "warning": None,
                "command_started": True,
                "command_completed": True,
                "outcome_known": True,
                "preexisting_dialog_count": 0,
                "dialog_detected": False,
                "dialogs_open": 0,
                "dialog_titles": [],
                "dialog_details": [],
                "compilation_errors": [],
                "history": "done",
                "results": "",
                "elapsed_seconds": 0.18,
                "transport": "pipe",
            }

        async def _run_slow():
            async with create_connected_server_and_client_session(
                server.mcp._mcp_server
            ) as session:
                return await session.call_tool(
                    "execute_igor",
                    {"command": "Print 1"},
                    progress_callback=_progress_cb,
                )

        with (
            patch.object(server, "_PROGRESS_HEARTBEAT_INITIAL_DELAY_SECONDS", 0.04),
            patch.object(server, "_PROGRESS_HEARTBEAT_INTERVAL_SECONDS", 0.04),
            patch.object(server, "_execute_igor_in_worker", side_effect=_slow_execute),
            patch.object(server, "_record_tool_call_best_effort"),
        ):
            result = asyncio.run(_run_slow())

        self.assertFalse(result.isError)
        self.assertTrue(result.structuredContent["ok"])
        self.assertGreaterEqual(len(progress_events), 2)
        for progress, total, message in progress_events:
            self.assertGreater(progress, 0.0)
            self.assertIsNone(total)
            self.assertIn("execute_igor running", message or "")
        self.assertGreater(progress_events[-1][0], progress_events[0][0])

        # Fast tool call (< initial delay) emits zero heartbeats
        fast_events: list[tuple[float, float | None, str | None]] = []

        async def _fast_cb(
            progress: float, total: float | None, message: str | None
        ) -> None:
            fast_events.append((progress, total, message))

        async def _run_fast():
            async with create_connected_server_and_client_session(
                server.mcp._mcp_server
            ) as session:
                return await session.call_tool(
                    "execute_igor",
                    {"command": "Print 1"},
                    progress_callback=_fast_cb,
                )

        with (
            patch.object(server, "_PROGRESS_HEARTBEAT_INITIAL_DELAY_SECONDS", 5.0),
            patch.object(server, "_PROGRESS_HEARTBEAT_INTERVAL_SECONDS", 5.0),
            patch.object(
                server,
                "_execute_igor_in_worker",
                return_value={
                    "ok": True,
                    "output": "fast",
                    "error": None,
                    "warning": None,
                    "command_started": True,
                    "command_completed": True,
                    "outcome_known": True,
                    "preexisting_dialog_count": 0,
                    "dialog_detected": False,
                    "dialogs_open": 0,
                    "dialog_titles": [],
                    "dialog_details": [],
                    "compilation_errors": [],
                    "history": "fast",
                    "results": "",
                    "elapsed_seconds": 0.001,
                    "transport": "pipe",
                },
            ),
            patch.object(server, "_record_tool_call_best_effort"),
        ):
            fast_result = asyncio.run(_run_fast())

        self.assertFalse(fast_result.isError)
        self.assertEqual(fast_events, [])


class PersistentPipePoolTests(unittest.TestCase):
    def setUp(self):
        server._reset_pipe_pool()

    def tearDown(self):
        server._reset_pipe_pool()

    def _make_poolable_client_cls(self, instances_out: list):
        class _PoolablePipeClient(_FakePipeClient):
            def __init__(self, *args, **kwargs):
                super().__init__()
                self._connected = False
                self.connect_calls = 0
                self.pid = 4242
                instances_out.append(self)

            @property
            def is_connected(self) -> bool:
                return self._connected and not self.closed

            def connect(self, timeout_seconds=None):
                self.connect_calls += 1
                self._connected = True

            def close(self) -> None:
                self._connected = False
                super().close()

        return _PoolablePipeClient

    def test_persistent_pipe_reused_across_consecutive_calls_and_reset(self):
        created: list = []
        client_cls = self._make_poolable_client_cls(created)

        def _factory(*args, **kwargs):
            inst = client_cls(*args, **kwargs)
            inst.responses["call_function"] = lambda p: (
                {"type": "string", "value": "Graph0;"}
                if p["name"] == "WinList"
                else {"type": "string", "value": "MyExp" if p["args"] == [1] else ""}
            )
            return inst

        with (
            patch.object(server.igor_pipe, "IgorPipeClient", side_effect=_factory),
            patch.object(server.igor_pipe, "is_pid_alive", return_value=True),
        ):
            r1 = server.list_windows()
            r2 = server.get_experiment_info()

        self.assertEqual(r1["windows"], ["Graph0"])
        self.assertEqual(r2["name"], "MyExp")
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].connect_calls, 1)
        self.assertFalse(created[0].closed)
        self.assertTrue(created[0].is_connected)

        server._reset_pipe_pool()
        self.assertTrue(created[0].closed)

    def test_stale_pooled_pipe_reconnects_once_on_pre_dispatch_error(self):
        created: list = []
        client_cls = self._make_poolable_client_cls(created)

        def _factory(*args, **kwargs):
            inst = client_cls(*args, **kwargs)
            inst.responses["call_function"] = {"type": "string", "value": "Graph0;"}
            return inst

        with (
            patch.object(server.igor_pipe, "IgorPipeClient", side_effect=_factory),
            patch.object(server.igor_pipe, "is_pid_alive", return_value=True),
        ):
            r1 = server.list_windows()
            self.assertEqual(r1["windows"], ["Graph0"])
            self.assertEqual(len(created), 1)

            # Simulate Igor restarting so the cached handle breaks before bytes are sent.
            created[0].responses["call_function"] = server.igor_pipe.PipeNotAvailableError(
                "broken pipe pre-dispatch"
            )
            r2 = server.list_windows()

        self.assertEqual(r2["windows"], ["Graph0"])
        self.assertEqual(len(created), 2)
        self.assertTrue(created[0].closed)
        self.assertFalse(created[1].closed)

    def test_stale_pid_detected_on_checkout_triggers_clean_reconnect(self):
        created: list = []
        client_cls = self._make_poolable_client_cls(created)

        def _factory(*args, **kwargs):
            inst = client_cls(*args, **kwargs)
            inst.responses["call_function"] = {"type": "string", "value": "Graph0;"}
            return inst

        alive_state = {"alive": True}
        with (
            patch.object(server.igor_pipe, "IgorPipeClient", side_effect=_factory),
            patch.object(
                server.igor_pipe,
                "is_pid_alive",
                side_effect=lambda _pid: alive_state["alive"],
            ),
        ):
            server.list_windows()
            self.assertEqual(len(created), 1)
            alive_state["alive"] = False
            server.list_windows()

        self.assertEqual(len(created), 2)
        self.assertTrue(created[0].closed)
        self.assertFalse(created[1].closed)

    def test_post_dispatch_error_discards_pooled_client_without_replay(self):
        created: list = []
        client_cls = self._make_poolable_client_cls(created)

        def _factory(*args, **kwargs):
            inst = client_cls(*args, **kwargs)
            inst.responses["call_function"] = {"type": "string", "value": "Graph0;"}
            return inst

        with (
            patch.object(server.igor_pipe, "IgorPipeClient", side_effect=_factory),
            patch.object(server.igor_pipe, "is_pid_alive", return_value=True),
            patch.object(server, "_igor_dialogs", return_value=[]),
            patch.object(server, "_capture_compilation_errors", return_value=[]),
            patch.object(server, "_watch_for_igor_dialogs"),
            patch.object(server, "_with_igor") as com_call,
        ):
            # Warm the pool.
            server.list_windows()
            self.assertEqual(len(created), 1)

            # Simulate a post-dispatch timeout on the pooled client.
            created[0].responses["execute_command"] = server.igor_pipe.PipeTimeoutError(
                "timed out after dispatch"
            )
            res = server.execute_igor("Display w0")

        com_call.assert_not_called()
        self.assertFalse(res["ok"])
        self.assertTrue(res["command_started"])
        self.assertFalse(res["outcome_known"])
        self.assertTrue(created[0].closed)
        # Must NEVER reconnect/replay after a post-dispatch error.
        self.assertEqual(len(created), 1)
        self.assertIsNone(server._pipe_pool._client)


class WavePaginationAndTextWaveTests(unittest.TestCase):
    def setUp(self):
        server._reset_pipe_pool()

    def tearDown(self):
        server._reset_pipe_pool()

    def test_get_wave_data_rejects_negative_or_non_int_start(self):
        with self.assertRaisesRegex(server.IgorError, "non-negative integer"):
            server.get_wave_data("root:w0", start=-1)
        with self.assertRaisesRegex(server.IgorError, "non-negative integer"):
            server.get_wave_data("root:w0", start=True)

    def test_get_wave_data_1d_and_2d_numeric_pagination_pipe(self):
        class _PaginatedPipeClient(_FakePipeClient):
            def __init__(self, kind: str):
                super().__init__()
                self.kind = kind
                self.last_command = ""

            def call_function(self, name, args=None, kwargs=None, timeout_seconds=30.0):
                self.calls.append(("call_function", name, args))
                if name == "WaveExists":
                    return {"type": "number", "value": 1}
                if name == "WaveType":
                    if args and len(args) > 1 and args[1] == 1:
                        return {"type": "number", "value": 1}
                    return {"type": "number", "value": 4}
                if name == "numpnts":
                    return {"type": "number", "value": 6 if self.kind == "2d" else 5}
                if name == "WaveDims":
                    return {"type": "number", "value": 2 if self.kind == "2d" else 1}
                if name == "DimSize":
                    return {"type": "number", "value": 3 if args[1] == 0 else 2}
                if name in ("DimOffset", "DimDelta"):
                    return {"type": "number", "value": 0.0 if name == "DimOffset" else 1.0}
                if name == "WaveUnits":
                    return {"type": "string", "value": ""}
                return {"type": "number", "value": 0}

            def execute_command(self, command, timeout_seconds=30.0):
                self.last_command = command
                self.calls.append(("execute_command", command))
                m_range = server.re.search(r"/R=\[(\d+),(\d+)\]", command)
                first_row, last_row = int(m_range.group(1)), int(m_range.group(2))
                if self.kind == "1d":
                    data = [10.0, 20.0, 30.0, 40.0, 50.0]
                    payload = "\n".join(str(v) for v in data[first_row : last_row + 1]) + "\n"
                else:
                    rows = [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]
                    payload = (
                        "\n".join(
                            "\t".join(str(v) for v in row)
                            for row in rows[first_row : last_row + 1]
                        )
                        + "\n"
                    )
                m_out = server.re.search(r'Open \S+ as "([^"]+)"', command)
                if m_out:
                    Path(m_out.group(1).replace("\\\\", "\\")).write_text(
                        payload, encoding="utf-8"
                    )
                return {"results": "", "history": ""}

        c1 = _PaginatedPipeClient("1d")
        with patch.object(server, "_try_pipe_client", return_value=c1):
            p1 = server.get_wave_data("root:w1", max_points=2, start=2)
        self.assertEqual(p1["values"], [30.0, 40.0])
        self.assertEqual(p1["start"], 2)
        self.assertTrue(p1["truncated"])
        self.assertIn("/R=[2,3]", c1.last_command)

        c1_end = _PaginatedPipeClient("1d")
        with patch.object(server, "_try_pipe_client", return_value=c1_end):
            p2 = server.get_wave_data("root:w1", max_points=2, start=4)
        self.assertEqual(p2["values"], [50.0])
        self.assertEqual(p2["start"], 4)
        self.assertFalse(p2["truncated"])
        self.assertIn("/R=[4,4]", c1_end.last_command)

        c1_past = _PaginatedPipeClient("1d")
        with patch.object(server, "_try_pipe_client", return_value=c1_past):
            p3 = server.get_wave_data("root:w1", max_points=2, start=5)
        self.assertEqual(p3["values"], [])
        self.assertEqual(p3["start"], 5)
        self.assertFalse(p3["truncated"])
        self.assertEqual(c1_past.last_command, "")

        c2 = _PaginatedPipeClient("2d")
        with patch.object(server, "_try_pipe_client", return_value=c2):
            m1 = server.get_wave_data("root:mat", max_points=4, start=1)
        self.assertEqual(m1["values"], [[3.0, 4.0], [5.0, 6.0]])
        self.assertEqual(m1["start"], 1)
        self.assertFalse(m1["truncated"])
        self.assertIn("/R=[1,2]", c2.last_command)

        c2_past = _PaginatedPipeClient("2d")
        with patch.object(server, "_try_pipe_client", return_value=c2_past):
            m2 = server.get_wave_data("root:mat", max_points=4, start=3)
        self.assertEqual(m2["values"], [])
        self.assertEqual(m2["start"], 3)
        self.assertFalse(m2["truncated"])
        self.assertEqual(c2_past.last_command, "")

    def test_get_wave_data_1d_and_2d_numeric_pagination_com(self):
        query_results_1d = iter(["1", "4", "1", "5", "1"])
        with (
            patch.object(server, "_try_pipe_client", return_value=None),
            patch.object(server, "_with_igor", side_effect=lambda fn: fn(object(), 10.0)),
            patch.object(server, "_query", side_effect=lambda *_: next(query_results_1d)),
            patch.object(server, "_raw_execute") as execute_1d,
            patch.object(server, "_read_tmp_file", return_value="30\r\n40\r\n"),
        ):
            r1 = server.get_wave_data("root:w1", max_points=2, start=2)
        self.assertEqual(r1["values"], [30.0, 40.0])
        self.assertEqual(r1["start"], 2)
        self.assertTrue(r1["truncated"])
        self.assertIn("/R=[2,3]", execute_1d.call_args.args[1])

        query_results_2d = iter([
            "1", "4", "1", "6", "2",
            "3", "2", "0", "1", "0", "1", "s", "nm",
        ])
        with (
            patch.object(server, "_try_pipe_client", return_value=None),
            patch.object(server, "_with_igor", side_effect=lambda fn: fn(object(), 10.0)),
            patch.object(server, "_query", side_effect=lambda *_: next(query_results_2d)),
            patch.object(server, "_raw_execute") as execute_2d,
            patch.object(server, "_read_tmp_file", return_value="3.0\t4.0\n"),
        ):
            r2 = server.get_wave_data("root:mat", max_points=2, start=1)
        self.assertEqual(r2["values"], [[3.0, 4.0]])
        self.assertEqual(r2["start"], 1)
        self.assertTrue(r2["truncated"])
        self.assertIn("/R=[1,1]", execute_2d.call_args.args[1])

    def test_get_wave_data_1d_text_wave_pipe_and_com(self):
        elements = [
            'alpha "quoted"',
            "line1\nline2",
            "",
            "tab\there",
        ]

        class _TextWavePipeClient(_FakePipeClient):
            def __init__(self, dims: int = 1, wave_class: int = 2):
                super().__init__()
                self.dims = dims
                self.wave_class = wave_class

            def call_function(self, name, args=None, kwargs=None, timeout_seconds=30.0):
                if name == "WaveExists":
                    return {"type": "number", "value": 1}
                if name == "WaveType":
                    if args and len(args) > 1 and args[1] == 1:
                        return {"type": "number", "value": self.wave_class}
                    return {"type": "number", "value": 0}
                if name == "numpnts":
                    return {"type": "number", "value": len(elements)}
                if name == "WaveDims":
                    return {"type": "number", "value": self.dims}
                return {"type": "number", "value": 0}

            def execute_command(self, command, timeout_seconds=30.0):
                m_sep = server.re.search(r'"%s(<<IGOR_SEP_[0-9a-f]+>>)"', command)
                assert m_sep is not None
                sep = m_sep.group(1)
                m_range = server.re.search(r"/R=\[(\d+),(\d+)\]", command)
                first_row, last_row = int(m_range.group(1)), int(m_range.group(2))
                sliced = elements[first_row : last_row + 1]
                payload = "".join(item.replace("\n", "\r\n") + sep for item in sliced)
                m_out = server.re.search(r'Open \S+ as "([^"]+)"', command)
                Path(m_out.group(1).replace("\\\\", "\\")).write_bytes(
                    payload.encode("utf-8")
                )
                return {"results": "", "history": ""}

        # Pipe full read
        with patch.object(server, "_try_pipe_client", return_value=_TextWavePipeClient()):
            full = server.get_wave_data("root:labels", max_points=10)
        self.assertEqual(full["values"], elements)
        self.assertEqual(full["wave_type_code"], 0)
        self.assertFalse(full["complex"])
        self.assertTrue(full["text"])
        self.assertEqual(full["start"], 0)
        self.assertFalse(full["truncated"])
        self.assertEqual(full["npnts"], 4)
        self.assertEqual(full["dimensions"], 1)

        # Pipe paginated read
        with patch.object(server, "_try_pipe_client", return_value=_TextWavePipeClient()):
            paged = server.get_wave_data("root:labels", max_points=2, start=1)
        self.assertEqual(paged["values"], ["line1\nline2", ""])
        self.assertEqual(paged["start"], 1)
        self.assertTrue(paged["truncated"])
        self.assertTrue(paged["text"])

        # Pipe rejects 2D text wave and wave-reference wave
        with patch.object(server, "_try_pipe_client", return_value=_TextWavePipeClient(dims=2)):
            with self.assertRaisesRegex(server.IgorError, "only 1D text waves"):
                server.get_wave_data("root:text2d")
        with patch.object(
            server, "_try_pipe_client", return_value=_TextWavePipeClient(wave_class=4)
        ):
            with self.assertRaisesRegex(server.IgorError, "wave-reference"):
                server.get_wave_data("root:wref")

        # COM fallback 1D text wave (including pagination)
        captured_cmd: list[str] = []

        def _fake_raw_execute(_app, cmd):
            captured_cmd.append(cmd)
            m_sep = server.re.search(r'"%s(<<IGOR_SEP_[0-9a-f]+>>)"', cmd)
            sep = m_sep.group(1)
            m_range = server.re.search(r"/R=\[(\d+),(\d+)\]", cmd)
            first_row, last_row = int(m_range.group(1)), int(m_range.group(2))
            sliced = elements[first_row : last_row + 1]
            payload = "".join(item + sep for item in sliced)
            m_out = server.re.search(r'Open \S+ as "([^"]+)"', cmd)
            Path(m_out.group(1).replace("\\\\", "\\")).write_text(
                payload, encoding="utf-8"
            )

        query_results = iter(["1", "0", "2", "4", "1"])
        with (
            patch.object(server, "_try_pipe_client", return_value=None),
            patch.object(server, "_with_igor", side_effect=lambda fn: fn(object(), 10.0)),
            patch.object(server, "_query", side_effect=lambda *_: next(query_results)),
            patch.object(server, "_raw_execute", side_effect=_fake_raw_execute),
        ):
            com_res = server.get_wave_data("root:labels", max_points=2, start=1)

        self.assertEqual(com_res["values"], ["line1\nline2", ""])
        self.assertTrue(com_res["text"])
        self.assertEqual(com_res["wave_type_code"], 0)
        self.assertFalse(com_res["complex"])
        self.assertEqual(com_res["start"], 1)
        self.assertTrue(com_res["truncated"])
        self.assertIn("/R=[1,2]", captured_cmd[0])

        # COM rejects DFREF wave (class 3)
        query_dfref = iter(["1", "0", "3"])
        with (
            patch.object(server, "_try_pipe_client", return_value=None),
            patch.object(server, "_with_igor", side_effect=lambda fn: fn(object(), 10.0)),
            patch.object(server, "_query", side_effect=lambda *_: next(query_dfref)),
        ):
            with self.assertRaisesRegex(server.IgorError, "data-folder-reference"):
                server.get_wave_data("root:dfref")

    def test_read_procedure_pipe_com_pagination_and_validation(self):
        proc_sources = {
            "MyAnalysis.ipf": "line0\r\nline1\r\nline2\rline3\nline4",
            "EmptyProc.ipf": "",
        }

        class _ProcPipeClient(_FakePipeClient):
            def call_function(self, name, args=None, kwargs=None, timeout_seconds=30.0):
                self.calls.append(("call_function", name, args, kwargs, timeout_seconds))
                if name == "ProcedureText":
                    return {"type": "string", "value": proc_sources.get(args[2], "")}
                if name == "WinList":
                    return {
                        "type": "string",
                        "value": ";".join(proc_sources.keys()) + ";",
                    }
                return {"type": "string", "value": ""}

        # Pipe full read + pagination
        with (
            patch.object(server, "_try_pipe_client", return_value=_ProcPipeClient()),
            patch.object(server, "_with_igor") as com_call,
        ):
            first = server.read_procedure("MyAnalysis.ipf", start_line=0, max_lines=2)
            second = server.read_procedure("MyAnalysis.ipf", start_line=2, max_lines=10)
            empty = server.read_procedure("EmptyProc.ipf")

        com_call.assert_not_called()
        self.assertTrue(first["ok"])
        self.assertEqual(first["transport"], "pipe")
        self.assertEqual(first["name"], "MyAnalysis.ipf")
        self.assertEqual(first["total_lines"], 5)
        self.assertEqual(first["start_line"], 0)
        self.assertEqual(first["returned_lines"], 2)
        self.assertTrue(first["truncated"])
        self.assertEqual(first["content"], "line0\nline1")

        self.assertEqual(second["start_line"], 2)
        self.assertEqual(second["returned_lines"], 3)
        self.assertFalse(second["truncated"])
        self.assertEqual(second["content"], "line2\nline3\nline4")

        self.assertEqual(empty["total_lines"], 0)
        self.assertEqual(empty["returned_lines"], 0)
        self.assertFalse(empty["truncated"])
        self.assertEqual(empty["content"], "")

        # Missing procedure raises IgorError on pipe
        with patch.object(server, "_try_pipe_client", return_value=_ProcPipeClient()):
            with self.assertRaisesRegex(server.IgorError, "not found"):
                server.read_procedure("Missing.ipf")

        # COM fallback + pagination + missing procedure
        def fake_query(_app, _fmt, expr):
            if expr == 'ProcedureText("", 0, "MyAnalysis.ipf")':
                return proc_sources["MyAnalysis.ipf"]
            if expr == 'ProcedureText("", 0, "Ghost.ipf")':
                return ""
            if expr == 'WinList("*", ";", "WIN:128")':
                return "MyAnalysis.ipf;EmptyProc.ipf;"
            self.fail(f"Unexpected COM query: {expr}")

        with (
            patch.object(server, "_try_pipe_client", return_value=None),
            patch.object(
                server, "_with_igor", side_effect=lambda fn, **kw: fn(object(), 10.0)
            ),
            patch.object(server, "_query", side_effect=fake_query),
        ):
            com_res = server.read_procedure("MyAnalysis.ipf", start_line=1, max_lines=2)
            with self.assertRaisesRegex(server.IgorError, "not found"):
                server.read_procedure("Ghost.ipf")

        self.assertTrue(com_res["ok"])
        self.assertEqual(com_res["transport"], "com")
        self.assertEqual(com_res["total_lines"], 5)
        self.assertEqual(com_res["returned_lines"], 2)
        self.assertTrue(com_res["truncated"])
        self.assertEqual(com_res["content"], "line1\nline2")

        # IGOR_TRANSPORT=pipe fails closed without COM fallback
        with (
            patch.dict(os.environ, {"IGOR_TRANSPORT": "pipe"}),
            patch.object(server, "_try_pipe_client", return_value=None),
            patch.object(server, "_with_igor") as com_closed,
        ):
            with self.assertRaisesRegex(server.IgorError, "IGOR_TRANSPORT=pipe"):
                server.read_procedure("MyAnalysis.ipf")
        com_closed.assert_not_called()

        # Input validation
        for bad_name in ("", "   ", "bad\nname.ipf", "bad\rname.ipf", "bad\x00name"):
            with self.assertRaises(server.IgorError):
                server.read_procedure(bad_name)
        for bad_start in (-1, True, "0"):
            with self.assertRaises(server.IgorError):
                server.read_procedure("MyAnalysis.ipf", start_line=bad_start)
        for bad_max in (0, 1001, False, "10"):
            with self.assertRaises(server.IgorError):
                server.read_procedure("MyAnalysis.ipf", max_lines=bad_max)

    def test_profile_workflow_pipe_com_and_incremental_merge(self):
        proc_texts = {
            "WMGraphHelper.ipf": "#pragma rtGlobals=3\nFunction WM_Internal()\nEnd\n",
            "LabFit.ipf": (
                "#pragma rtGlobals=3\n"
                "Function FitDecay(wave w)\n"
                "\tCurveFit/Q exp_XOffset w\n"
                "End\n"
            ),
            "Procedure": (
                "#pragma rtGlobals=3\n"
                "Menu \"Lab\"\n"
                "\t\"Run Batch\", /Q, RunBatch()\n"
                "End\n"
                "Function RunBatch()\n"
                "\tPrint \"done\"\n"
                "End\n"
            ),
        }
        graph_recreation = (
            "Window Graph0() : Graph\n"
            "\tPauseUpdate; Silent 1\n"
            "\tDisplay /W=(35,53,430,261) signal\n"
            '\tModifyGraph font="Arial",fSize=12,mirror=1,tick=2\n'
            "\tModifyGraph rgb(signal)=(0,0,65535),lsize=1.5\n"
            '\tLabel left "Current (nA)"\n'
            '\tLabel bottom "Time (ms)"\n'
            "EndMacro\n"
        )

        def _make_pipe_client(exp_name: str, extra_proc: tuple[str, str] | None = None):
            local_procs = dict(proc_texts)
            if extra_proc is not None:
                local_procs[extra_proc[0]] = extra_proc[1]

            class _WorkflowPipeClient(_FakePipeClient):
                def call_function(self, name, args=None, kwargs=None, timeout_seconds=30.0):
                    self.calls.append(("call_function", name, args, kwargs, timeout_seconds))
                    if name == "DataFolderExists":
                        return {"type": "number", "value": 1 if args[0] == "root:" else 0}
                    if name == "IgorInfo":
                        if args == [1]:
                            return {"type": "string", "value": exp_name}
                        if args == [12]:
                            return {"type": "string", "value": f"{exp_name}.pxp"}
                    if name == "CountObjects":
                        return {"type": "number", "value": 1 if args[1] == 1 else 0}
                    if name == "GetIndexedObjName":
                        return {"type": "string", "value": "signal"}
                    if name == "WinList":
                        if args[2] == "WIN:1":
                            return {"type": "string", "value": "Graph0;"}
                        if args[2] == "WIN:128":
                            return {
                                "type": "string",
                                "value": ";".join(local_procs.keys()) + ";",
                            }
                        return {"type": "string", "value": ""}
                    if name == "ProcedureText":
                        return {"type": "string", "value": local_procs.get(args[2], "")}
                    if name == "WinRecreation":
                        return {"type": "string", "value": graph_recreation}
                    return {"type": "string", "value": ""}

                def execute_command(self, command, timeout_seconds=30.0):
                    self.calls.append(("execute_command", command, timeout_seconds))
                    if "PathInfo home" in command:
                        return {"results": r"C:\Lab\Data", "history": ""}
                    return {"results": "", "history": ""}

            return _WorkflowPipeClient()

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"IGOR_SESSION_DIR": tmpdir}):
                # First experiment over pipe, with max_procedures=2 (prioritizes Procedure, LabFit.ipf over WMGraphHelper.ipf)
                with (
                    patch.object(
                        server,
                        "_try_pipe_client",
                        return_value=_make_pipe_client("ExpOne"),
                    ),
                    patch.object(server, "_with_igor") as com_call,
                ):
                    res1 = server.profile_workflow(
                        max_procedures=2,
                        save_profile=True,
                        profile_name="gabor-lab",
                        user_notes="Prefer Arial 12pt and mirror axes.",
                    )

                com_call.assert_not_called()
                self.assertTrue(res1["ok"])
                self.assertEqual(res1["transport"], "pipe")
                self.assertEqual(res1["experiment"]["name"], "ExpOne")
                self.assertEqual(res1["folder_count"], 1)
                self.assertEqual(res1["total_waves"], 1)
                self.assertTrue(res1["procedures_truncated"])
                self.assertEqual(
                    [p["name"] for p in res1["procedures"]],
                    ["Procedure", "LabFit.ipf"],
                )
                self.assertEqual(len(res1["graph_styles"]), 1)
                self.assertIsInstance(res1["style_defaults"], dict)
                self.assertIsNotNone(res1["saved_profile"])

                # Second experiment incrementally merges into the same workflow profile
                with patch.object(
                    server,
                    "_try_pipe_client",
                    return_value=_make_pipe_client(
                        "ExpTwo",
                        extra_proc=(
                            "ExtraAnalysis.ipf",
                            "#pragma rtGlobals=3\nFunction ComputeSNR(wave w)\nEnd\n",
                        ),
                    ),
                ):
                    res2 = server.profile_workflow(
                        max_procedures=10,
                        include_window_styles=False,
                        save_profile=True,
                        profile_name="gabor-lab",
                        user_notes="Store fits in root:Fits:",
                    )

                self.assertEqual(res2["graph_styles"], [])
                self.assertIsNotNone(res2["saved_profile"])
                self.assertTrue(res2["saved_profile"]["merged"])
                self.assertEqual(res2["saved_profile"]["observations_count"], 2)
                # Verify saved profile files exist and merged both experiments/notes
                md_path = Path(res2["saved_profile"]["markdown_path"])
                json_path = Path(res2["saved_profile"]["json_path"])
                self.assertTrue(md_path.exists())
                self.assertTrue(json_path.exists())
                saved_text = md_path.read_text(encoding="utf-8")
                self.assertIn("ExpOne", saved_text)
                self.assertIn("ExpTwo", saved_text)
                self.assertIn("Prefer Arial 12pt", saved_text)
                self.assertIn("Store fits in root:Fits:", saved_text)

        # COM fallback path (save_profile=False)
        def fake_com_query(_app, _fmt, expr):
            if expr == 'DataFolderExists("root:")':
                return "1"
            if expr == "IgorInfo(1)":
                return "ComExp"
            if expr == "IgorInfo(12)":
                return ""
            if 'CountObjects("root:", 1)' in expr:
                return "1|0|0|0"
            if expr == 'GetIndexedObjName("root:", 1, 0)':
                return "signal"
            if expr == 'WinList("*", ";", "WIN:1")':
                return "Graph0;"
            if expr == 'WinList("*", ";", "WIN:128")':
                return "Procedure;LabFit.ipf;"
            if expr.startswith('WinList("*", ";", "WIN:'):
                return ""
            if expr == 'ProcedureText("", 0, "Procedure")':
                return proc_texts["Procedure"]
            if expr == 'ProcedureText("", 0, "LabFit.ipf")':
                return proc_texts["LabFit.ipf"]
            if expr == 'WinRecreation("Graph0", 0)':
                return graph_recreation
            if expr == "IgorInfo(10)":
                return "VDT2-64;"
            self.fail(f"Unexpected COM query: {expr}")

        with (
            patch.object(server, "_try_pipe_client", return_value=None),
            patch.object(
                server, "_with_igor", side_effect=lambda fn, **kw: fn(object(), 10.0)
            ),
            patch.object(server, "_query", side_effect=fake_com_query),
        ):
            com_res = server.profile_workflow(save_profile=False)

        self.assertTrue(com_res["ok"])
        self.assertEqual(com_res["transport"], "com")
        self.assertEqual(com_res["experiment"]["name"], "ComExp")
        self.assertEqual(
            [p["name"] for p in com_res["procedures"]],
            ["Procedure", "LabFit.ipf"],
        )
        self.assertEqual(len(com_res["graph_styles"]), 1)
        self.assertEqual(com_res["loaded_xops"], ["VDT2-64"])
        self.assertIsNone(com_res["saved_profile"])

        # IGOR_TRANSPORT=pipe fails closed without COM fallback
        with (
            patch.dict(os.environ, {"IGOR_TRANSPORT": "pipe"}),
            patch.object(server, "_try_pipe_client", return_value=None),
            patch.object(server, "_with_igor") as com_closed,
        ):
            with self.assertRaisesRegex(server.IgorError, "IGOR_TRANSPORT=pipe"):
                server.profile_workflow()
        com_closed.assert_not_called()


if __name__ == "__main__":
    unittest.main()

