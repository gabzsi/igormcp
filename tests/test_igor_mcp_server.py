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


class ValidationTests(unittest.TestCase):
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
        self.assertNotIn("SetForegroundWindow", source)
        self.assertNotIn("SW_RESTORE", source)
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
        self.assertIn("NewDataFolder/S root:Run_20261020_01", instructions)
        self.assertIn('SetScale/P x, 0, 0.1, "ms", signal', instructions)
        self.assertIn("signal = exp(-x/2)", instructions)
        self.assertIn("Display yWave vs xWave", instructions)
        self.assertIn("AppendToGraph/W=graphName", instructions)
        self.assertIn("Label/W=graphName", instructions)
        self.assertIn("RGB channels range from 0 to 65535", instructions)
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
            self.assertEqual(help_result.structuredContent["results"], [])

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


if __name__ == "__main__":
    unittest.main()
