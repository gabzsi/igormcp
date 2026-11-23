"""Offline unit tests for igor_pipe (Igor Pro 10.04+ native JSON-RPC pipe client)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

import igor_pipe
from igor_pipe import (
    IgorPipeClient,
    IgorPipeError,
    PipeDispatchError,
    PipeInstance,
    PipeNotAvailableError,
    PipeRPCError,
    PipeTimeoutError,
    cancel_igor_request,
    discover_instances,
    is_pid_alive,
    select_instance,
    supports_native_pipe,
)

if sys.platform == "win32":
    import pywintypes
    import win32event
    import win32file
    import win32pipe
    import winerror


class _MockNamedPipeServer:
    """Real Windows named-pipe JSON-RPC mock server with clean overlapped shutdown."""

    def __init__(self, handler) -> None:
        self.pipe_name = rf"\\.\pipe\igormcp_test_{uuid.uuid4().hex}"
        self._handler = handler
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._accept_thread: threading.Thread | None = None
        self.requests: list[dict] = []
        self._req_lock = threading.Lock()

    def start(self) -> None:
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        if not self._ready_event.wait(timeout=3.0):
            raise RuntimeError("Mock pipe server failed to start in time.")

    def stop(self) -> None:
        self._stop_event.set()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=3.0)
        for thread in self._threads:
            thread.join(timeout=2.0)

    def __enter__(self) -> _MockNamedPipeServer:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def _create_pipe_instance(self):
        return win32pipe.CreateNamedPipe(
            self.pipe_name,
            win32pipe.PIPE_ACCESS_DUPLEX | win32file.FILE_FLAG_OVERLAPPED,
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
            win32pipe.PIPE_UNLIMITED_INSTANCES,
            65536,
            65536,
            0,
            None,
        )

    def _accept_loop(self) -> None:
        while not self._stop_event.is_set():
            pipe = self._create_pipe_instance()
            self._ready_event.set()

            ov = pywintypes.OVERLAPPED()
            ev = win32event.CreateEvent(None, True, False, None)
            ov.hEvent = ev
            connected = False
            try:
                try:
                    rc = win32pipe.ConnectNamedPipe(pipe, ov)
                except pywintypes.error as exc:
                    rc = exc.winerror

                if rc in (0, winerror.ERROR_PIPE_CONNECTED):
                    connected = True
                elif rc == winerror.ERROR_IO_PENDING:
                    while not self._stop_event.is_set():
                        wait_rc = win32event.WaitForSingleObject(ev, 50)
                        if wait_rc == win32event.WAIT_OBJECT_0:
                            win32file.GetOverlappedResult(pipe, ov, False)
                            connected = True
                            break
                    if not connected:
                        try:
                            win32file.CancelIo(pipe)
                            win32file.GetOverlappedResult(pipe, ov, True)
                        except pywintypes.error:
                            pass
                else:
                    raise OSError(f"ConnectNamedPipe failed with {rc}")
            finally:
                win32file.CloseHandle(ev)

            if not connected:
                win32file.CloseHandle(pipe)
                break

            worker = threading.Thread(
                target=self._serve_client, args=(pipe,), daemon=True
            )
            self._threads.append(worker)
            worker.start()

    def _read_chunk(self, pipe) -> bytes | None:
        buf = win32file.AllocateReadBuffer(65536)
        ov = pywintypes.OVERLAPPED()
        ev = win32event.CreateEvent(None, True, False, None)
        ov.hEvent = ev
        try:
            try:
                rc, view = win32file.ReadFile(pipe, buf, ov)
            except pywintypes.error:
                return None

            if rc == winerror.ERROR_IO_PENDING:
                while not self._stop_event.is_set():
                    wait_rc = win32event.WaitForSingleObject(ev, 50)
                    if wait_rc == win32event.WAIT_OBJECT_0:
                        try:
                            n = win32file.GetOverlappedResult(pipe, ov, False)
                        except pywintypes.error:
                            return None
                        return bytes(view[:n])
                try:
                    win32file.CancelIo(pipe)
                    win32file.GetOverlappedResult(pipe, ov, True)
                except pywintypes.error:
                    pass
                return None

            try:
                n = win32file.GetOverlappedResult(pipe, ov, False)
            except pywintypes.error:
                return None
            return bytes(view[:n])
        finally:
            win32file.CloseHandle(ev)

    def _write_bytes(self, pipe, data: bytes) -> None:
        ov = pywintypes.OVERLAPPED()
        ev = win32event.CreateEvent(None, True, False, None)
        ov.hEvent = ev
        try:
            rc, _ = win32file.WriteFile(pipe, data, ov)
            if rc == winerror.ERROR_IO_PENDING:
                win32event.WaitForSingleObject(ev, 2000)
                win32file.GetOverlappedResult(pipe, ov, False)
        finally:
            win32file.CloseHandle(ev)

    def _serve_client(self, pipe) -> None:
        buffer = bytearray()
        try:
            while not self._stop_event.is_set():
                chunk = self._read_chunk(pipe)
                if not chunk:
                    break
                buffer.extend(chunk)
                while b"\n" in buffer:
                    idx = buffer.find(b"\n")
                    line = bytes(buffer[: idx + 1])
                    del buffer[: idx + 1]
                    req = json.loads(line.decode("utf-8"))
                    with self._req_lock:
                        self.requests.append(req)
                    action = self._handler(req, self._stop_event)
                    if action is None:
                        # Close pipe immediately without replying.
                        return
                    if isinstance(action, bytes):
                        self._write_bytes(pipe, action)
                    elif isinstance(action, list):
                        for item in action:
                            raw = (
                                item
                                if isinstance(item, bytes)
                                else (json.dumps(item) + "\n").encode("utf-8")
                            )
                            self._write_bytes(pipe, raw)
                    else:
                        payload = (json.dumps(action) + "\n").encode("utf-8")
                        self._write_bytes(pipe, payload)
        except Exception:
            pass
        finally:
            try:
                win32pipe.DisconnectNamedPipe(pipe)
            except pywintypes.error:
                pass
            try:
                win32file.CloseHandle(pipe)
            except pywintypes.error:
                pass


class VersionHelperTests(unittest.TestCase):
    def test_supports_native_pipe(self) -> None:
        self.assertFalse(supports_native_pipe("10.03"))
        self.assertTrue(supports_native_pipe("10.04"))
        self.assertTrue(supports_native_pipe("10.04B01"))
        self.assertTrue(supports_native_pipe("10.5.1"))
        self.assertFalse(supports_native_pipe("9.05"))
        self.assertTrue(supports_native_pipe("11.0"))
        self.assertFalse(supports_native_pipe(None))
        self.assertFalse(supports_native_pipe(""))
        self.assertFalse(supports_native_pipe("not-a-version"))


class InstanceDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = self._tmp.name
        self.instances_file = os.path.join(self.tmp_path, "IgorProInstances.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_is_pid_alive_real_process_and_invalid_pid(self) -> None:
        self.assertTrue(is_pid_alive(os.getpid()))
        self.assertFalse(is_pid_alive(0))
        self.assertFalse(is_pid_alive(-1))
        self.assertFalse(is_pid_alive(999_999_999))

    def test_discover_jsonl_filters_stale_pids(self) -> None:
        live_pid = os.getpid()
        lines = [
            json.dumps(
                {
                    "pid": 999_999_999,
                    "pipe": r"\\.\pipe\igor_stale",
                    "igorVersion": "10.5.1",
                }
            ),
            "",
            "{corrupt json line",
            json.dumps(
                {
                    "pid": live_pid,
                    "pipe": r"\\.\pipe\igor_live",
                    "igorVersion": "10.5.1",
                }
            ),
        ]
        with open(self.instances_file, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

        found = discover_instances(self.instances_file)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].pid, live_pid)
        self.assertEqual(found[0].pipe, r"\\.\pipe\igor_live")
        self.assertEqual(found[0].igor_version, "10.5.1")
        self.assertEqual(found[0].raw["pid"], live_pid)

    def test_discover_json_array_and_instances_dict(self) -> None:
        live_pid = os.getpid()
        # JSON array
        with open(self.instances_file, "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {
                        "pid": live_pid,
                        "pipe": r"\\.\pipe\igor_a",
                        "igorVersion": "10.04",
                    },
                    {
                        "pid": live_pid,
                        "pipe": r"\\.\pipe\igor_b",
                        "igorVersion": "10.5.1",
                    },
                ],
                fh,
            )

        found = discover_instances(self.instances_file)
        self.assertEqual([i.pipe for i in found], [r"\\.\pipe\igor_a", r"\\.\pipe\igor_b"])

        # Dict with "instances" key
        with open(self.instances_file, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "instances": [
                        {
                            "pid": live_pid,
                            "pipe": r"\\.\pipe\igor_wrapped",
                            "igorVersion": "10.6.0",
                        }
                    ]
                },
                fh,
            )
        found_wrapped = discover_instances(self.instances_file)
        self.assertEqual(len(found_wrapped), 1)
        self.assertEqual(found_wrapped[0].pipe, r"\\.\pipe\igor_wrapped")

    def test_missing_or_empty_file(self) -> None:
        missing = os.path.join(self.tmp_path, "does_not_exist.json")
        self.assertEqual(discover_instances(missing), [])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IGOR_PIPE_NAME", None)
            with self.assertRaises(PipeNotAvailableError):
                select_instance(missing)

        with open(self.instances_file, "w", encoding="utf-8") as fh:
            fh.write("   \n\n  ")
        self.assertEqual(discover_instances(self.instances_file), [])

    def test_select_instance_version_prefix_and_env_overrides(self) -> None:
        live_pid = os.getpid()
        with open(self.instances_file, "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {
                        "pid": live_pid,
                        "pipe": r"\\.\pipe\igor_10_4",
                        "igorVersion": "10.04",
                    },
                    {
                        "pid": live_pid,
                        "pipe": r"\\.\pipe\igor_10_5",
                        "igorVersion": "10.5.1",
                    },
                ],
                fh,
            )

        with mock.patch.dict(
            os.environ,
            {
                "IGOR_INSTANCES_PATH": self.instances_file,
                "IGOR_VERSION_PREFIX": "10.04",
            },
            clear=False,
        ):
            os.environ.pop("IGOR_PIPE_NAME", None)
            selected = select_instance()
            self.assertEqual(selected.pipe, r"\\.\pipe\igor_10_4")

        with mock.patch.dict(
            os.environ,
            {
                "IGOR_INSTANCES_PATH": self.instances_file,
                "IGOR_VERSION_PREFIX": "10.5",
            },
            clear=False,
        ):
            os.environ.pop("IGOR_PIPE_NAME", None)
            selected = select_instance()
            self.assertEqual(selected.pipe, r"\\.\pipe\igor_10_5")

        with mock.patch.dict(
            os.environ,
            {
                "IGOR_INSTANCES_PATH": self.instances_file,
                "IGOR_VERSION_PREFIX": "11.0",
            },
            clear=False,
        ):
            os.environ.pop("IGOR_PIPE_NAME", None)
            with self.assertRaises(PipeNotAvailableError):
                select_instance()

        # IGOR_PIPE_NAME bypasses discovery entirely even if file is missing.
        with mock.patch.dict(
            os.environ,
            {
                "IGOR_PIPE_NAME": r"\\.\pipe\explicit_override",
                "IGOR_INSTANCES_PATH": os.path.join(self.tmp_path, "missing.json"),
            },
            clear=False,
        ):
            selected = select_instance()
            self.assertIsInstance(selected, PipeInstance)
            self.assertEqual(selected.pipe, r"\\.\pipe\explicit_override")


@unittest.skipUnless(sys.platform == "win32", "Windows named-pipe tests require Windows")
class NamedPipeClientTests(unittest.TestCase):
    def test_all_high_level_methods(self) -> None:
        def handler(req: dict, _stop: threading.Event):
            method = req["method"]
            params = req.get("params")
            return {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {"echoMethod": method, "echoParams": params},
            }

        with _MockNamedPipeServer(handler) as server:
            with IgorPipeClient(server.pipe_name) as client:
                self.assertTrue(client.is_connected)

                res = client.capabilities()
                self.assertEqual(res["echoMethod"], "igor.capabilities")
                self.assertIsNone(res["echoParams"])

                res = client.execute_command("Print 2+2")
                self.assertEqual(res["echoMethod"], "igor.command.execute")
                self.assertEqual(res["echoParams"], {"cmd": "Print 2+2"})

                res = client.call_function("WaveStats", [1, "w0"], {"Q": True})
                self.assertEqual(res["echoMethod"], "igor.fn.call")
                self.assertEqual(
                    res["echoParams"],
                    {"name": "WaveStats", "args": [1, "w0"], "kwargs": {"Q": True}},
                )

                res = client.call_function("SimpleFn")
                self.assertEqual(
                    res["echoParams"], {"name": "SimpleFn", "args": []}
                )
                self.assertNotIn("kwargs", res["echoParams"])

                for render_mode in ("auto", "screen", "screenGraphOnly", "export"):
                    res = client.capture_window(
                        window="Graph0",
                        render=render_mode,
                        max_dimension=800,
                        fmt="png",
                    )
                    self.assertEqual(res["echoMethod"], "igor.window.capture")
                    self.assertEqual(
                        res["echoParams"],
                        {
                            "window": "Graph0",
                            "render": render_mode,
                            "maxDimension": 800,
                            "format": "png",
                        },
                    )

                res = client.capture_window()
                self.assertNotIn("window", res["echoParams"])
                self.assertEqual(res["echoParams"]["render"], "auto")
                self.assertEqual(res["echoParams"]["maxDimension"], 1200)
                self.assertEqual(res["echoParams"]["format"], "png")

                res = client.get_state()
                self.assertEqual(res["echoMethod"], "igor.app.state")

                res = client.cancel_request()
                self.assertEqual(res["echoMethod"], "igor.request.cancel")

                res = client.compile_procedures()
                self.assertEqual(res["echoMethod"], "igor.procedures.compile")

                res = client.get_compile_status()
                self.assertEqual(res["echoMethod"], "igor.procedures.compileStatus")

                res = client.close_procedures([r"C:\tmp\test.ipf"])
                self.assertEqual(res["echoMethod"], "igor.procedures.close")
                self.assertEqual(
                    res["echoParams"], {"filePaths": [r"C:\tmp\test.ipf"]}
                )

            self.assertFalse(client.is_connected)
            # Verify monotonically incrementing JSON-RPC request IDs.
            ids = [r["id"] for r in server.requests]
            self.assertEqual(ids, list(range(1, len(ids) + 1)))

    def test_cancel_igor_request_independent_connection(self) -> None:
        cancel_seen = threading.Event()

        def handler(req: dict, stop: threading.Event):
            if req["method"] == "igor.command.execute":
                # Wait until cancel arrives on a second pipe connection.
                cancel_seen.wait(timeout=3.0)
                return {
                    "jsonrpc": "2.0",
                    "id": req["id"],
                    "result": {"cancelled": cancel_seen.is_set()},
                }
            if req["method"] == "igor.request.cancel":
                cancel_seen.set()
                return {
                    "jsonrpc": "2.0",
                    "id": req["id"],
                    "result": {"ok": True},
                }
            return {"jsonrpc": "2.0", "id": req["id"], "result": {}}

        with _MockNamedPipeServer(handler) as server:
            exec_result: dict = {}
            exec_errors: list[BaseException] = []

            def run_exec():
                try:
                    with IgorPipeClient(server.pipe_name) as c:
                        exec_result.update(c.execute_command("LongOp", timeout_seconds=5.0))
                except BaseException as exc:
                    exec_errors.append(exc)

            worker = threading.Thread(target=run_exec, daemon=True)
            worker.start()
            time.sleep(0.05)

            cancel_res = cancel_igor_request(server.pipe_name, timeout_seconds=3.0)
            self.assertEqual(cancel_res, {"ok": True})

            worker.join(timeout=3.0)
            self.assertFalse(worker.is_alive())
            self.assertEqual(exec_errors, [])
            self.assertEqual(exec_result, {"cancelled": True})

    def test_rpc_error_propagation(self) -> None:
        def handler(req: dict, _stop: threading.Event):
            if req["method"] == "igor.command.execute":
                return {
                    "jsonrpc": "2.0",
                    "id": req["id"],
                    "error": {
                        "code": -32001,
                        "message": "Syntax error in command",
                        "data": {"line": 1, "token": "???"},
                    },
                }
            return {"jsonrpc": "2.0", "id": req["id"], "result": {"ok": True}}

        with _MockNamedPipeServer(handler) as server:
            with IgorPipeClient(server.pipe_name) as client:
                with self.assertRaises(PipeRPCError) as ctx:
                    client.execute_command("???")
                err = ctx.exception
                self.assertEqual(err.code, -32001)
                self.assertEqual(err.message, "Syntax error in command")
                self.assertEqual(err.data, {"line": 1, "token": "???"})
                self.assertIsInstance(err, IgorPipeError)
                self.assertNotIsInstance(err, PipeDispatchError)
                self.assertNotIsInstance(err, PipeNotAvailableError)

                # Connection should still be healthy after an application-level RPC error.
                self.assertEqual(client.get_state(), {"ok": True})

    def test_nonexistent_pipe_raises_not_available(self) -> None:
        missing_pipe = rf"\\.\pipe\igormcp_missing_{uuid.uuid4().hex}"
        client = IgorPipeClient(missing_pipe)
        with self.assertRaises(PipeNotAvailableError):
            client.capabilities(timeout_seconds=1.0)
        self.assertFalse(client.is_connected)

    def test_timeout_when_server_stalls_does_not_hang(self) -> None:
        def stalling_handler(req: dict, stop: threading.Event):
            # Stall until the test server stops.
            stop.wait(timeout=5.0)
            return None

        with _MockNamedPipeServer(stalling_handler) as server:
            client = IgorPipeClient(server.pipe_name)
            start = time.monotonic()
            with self.assertRaises(PipeTimeoutError) as ctx:
                client.execute_command("Sleep 10", timeout_seconds=0.25)
            elapsed = time.monotonic() - start
            self.assertIsInstance(ctx.exception, PipeDispatchError)
            self.assertLess(elapsed, 2.0)
            self.assertFalse(client.is_connected)

    def test_pipe_closed_mid_response_raises_dispatch_error(self) -> None:
        def drop_handler(req: dict, _stop: threading.Event):
            # Drop connection immediately after receiving request.
            return None

        with _MockNamedPipeServer(drop_handler) as server:
            client = IgorPipeClient(server.pipe_name)
            with self.assertRaises(PipeDispatchError) as ctx:
                client.execute_command("Make/O w0")
            self.assertNotIsInstance(ctx.exception, PipeNotAvailableError)
            self.assertFalse(client.is_connected)

    def test_malformed_json_response_raises_dispatch_error(self) -> None:
        def bad_json_handler(req: dict, _stop: threading.Event):
            return b"{not valid json\n"

        with _MockNamedPipeServer(bad_json_handler) as server:
            client = IgorPipeClient(server.pipe_name)
            with self.assertRaises(PipeDispatchError):
                client.get_state(timeout_seconds=2.0)
            self.assertFalse(client.is_connected)

    def test_skips_unsolicited_notification_before_response(self) -> None:
        def notify_then_reply(req: dict, _stop: threading.Event):
            return [
                {"jsonrpc": "2.0", "method": "igor.progress", "params": {"pct": 50}},
                {"jsonrpc": "2.0", "id": req["id"], "result": {"done": True}},
            ]

        with _MockNamedPipeServer(notify_then_reply) as server:
            with IgorPipeClient(server.pipe_name) as client:
                self.assertEqual(client.get_state(), {"done": True})

    def test_mismatched_response_id_on_result_and_error(self) -> None:
        def wrong_result_id(req: dict, _stop: threading.Event):
            return {
                "jsonrpc": "2.0",
                "id": req["id"] + 99,
                "result": {"ok": True},
            }

        with _MockNamedPipeServer(wrong_result_id) as server:
            client = IgorPipeClient(server.pipe_name)
            with self.assertRaises(PipeDispatchError):
                client.get_state(timeout_seconds=2.0)
            self.assertFalse(client.is_connected)

        def wrong_error_id(req: dict, _stop: threading.Event):
            return {
                "jsonrpc": "2.0",
                "id": req["id"] + 42,
                "error": {"code": -32001, "message": "Wrong id error"},
            }

        with _MockNamedPipeServer(wrong_error_id) as server:
            client = IgorPipeClient(server.pipe_name)
            with self.assertRaises(PipeDispatchError) as ctx:
                client.get_state(timeout_seconds=2.0)
            self.assertNotIsInstance(ctx.exception, PipeRPCError)
            self.assertFalse(client.is_connected)

    def test_malformed_error_code_and_non_dict_result(self) -> None:
        def bad_error_code(req: dict, _stop: threading.Event):
            return {
                "jsonrpc": "2.0",
                "id": req["id"],
                "error": {"code": "not-an-int", "message": "Weird error"},
            }

        with _MockNamedPipeServer(bad_error_code) as server:
            with IgorPipeClient(server.pipe_name) as client:
                with self.assertRaises(PipeRPCError) as ctx:
                    client.get_state(timeout_seconds=2.0)
                self.assertEqual(ctx.exception.code, -32000)
                self.assertEqual(ctx.exception.message, "Weird error")

        def non_dict_result(req: dict, _stop: threading.Event):
            return {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": ["unexpected", "list"],
            }

        with _MockNamedPipeServer(non_dict_result) as server:
            client = IgorPipeClient(server.pipe_name)
            with self.assertRaises(PipeDispatchError) as ctx:
                client.get_state(timeout_seconds=2.0)
            self.assertIn("non-object", str(ctx.exception))
            self.assertFalse(client.is_connected)

    def test_large_multi_chunk_and_coalesced_responses(self) -> None:
        large_blob = "A" * 150_000  # > 64 KiB read chunk size

        def handler(req: dict, _stop: threading.Event):
            if req["method"] == "igor.window.capture":
                return {
                    "jsonrpc": "2.0",
                    "id": req["id"],
                    "result": {"data": large_blob},
                }
            # Coalesce notification + response into a single pipe write.
            line1 = json.dumps({"jsonrpc": "2.0", "method": "igor.note", "params": {}})
            line2 = json.dumps(
                {"jsonrpc": "2.0", "id": req["id"], "result": {"coalesced": True}}
            )
            return (line1 + "\n" + line2 + "\n").encode("utf-8")

        with _MockNamedPipeServer(handler) as server:
            with IgorPipeClient(server.pipe_name) as client:
                res_large = client.capture_window()
                self.assertEqual(len(res_large["data"]), 150_000)
                self.assertEqual(res_large["data"], large_blob)

                res_coalesced = client.get_state()
                self.assertEqual(res_coalesced, {"coalesced": True})

    def test_pre_dispatch_write_broken_pipe_raises_not_available(self) -> None:
        def ok_handler(req: dict, _stop: threading.Event):
            return {"jsonrpc": "2.0", "id": req["id"], "result": {"ok": True}}

        with _MockNamedPipeServer(ok_handler) as server:
            client = IgorPipeClient(server.pipe_name)
            client.connect(timeout_seconds=2.0)
            self.assertTrue(client.is_connected)

            broken = pywintypes.error(
                winerror.ERROR_BROKEN_PIPE, "WriteFile", "The pipe has been ended."
            )
            with mock.patch.object(igor_pipe.win32file, "WriteFile", side_effect=broken):
                with self.assertRaises(PipeNotAvailableError):
                    client.execute_command("Print 1", timeout_seconds=2.0)
            self.assertFalse(client.is_connected)

    def test_read_line_max_bytes_guard(self) -> None:
        def oversized_handler(req: dict, _stop: threading.Event):
            return (
                json.dumps(
                    {"jsonrpc": "2.0", "id": req["id"], "result": {"blob": "X" * 2048}}
                )
                + "\n"
            ).encode("utf-8")

        with _MockNamedPipeServer(oversized_handler) as server:
            client = IgorPipeClient(server.pipe_name)
            orig_read_line = client._read_line
            with mock.patch.object(
                client,
                "_read_line",
                side_effect=lambda deadline, method, timeout: orig_read_line(
                    deadline, method, timeout, max_line_bytes=256
                ),
            ):
                with self.assertRaises(PipeDispatchError):
                    client.get_state(timeout_seconds=2.0)
            self.assertFalse(client.is_connected)

    def test_connect_populates_discovered_pid_and_clears_on_close(self) -> None:
        def ok_handler(req: dict, _stop: threading.Event):
            return {"ok": True}

        with _MockNamedPipeServer(ok_handler) as server:
            with tempfile.TemporaryDirectory() as tmpdir:
                reg = os.path.join(tmpdir, "IgorProInstances.json")
                with open(reg, "w", encoding="utf-8") as fh:
                    json.dump(
                        [{"pid": os.getpid(), "pipe": server.pipe_name, "igorVersion": "10.04"}],
                        fh,
                    )
                client = IgorPipeClient(instances_path=reg)
                self.assertIsNone(client.pid)
                client.connect(timeout_seconds=2.0)
                self.assertEqual(client.pid, os.getpid())
                client.close()
                self.assertIsNone(client.pid)


if __name__ == "__main__":
    unittest.main()
