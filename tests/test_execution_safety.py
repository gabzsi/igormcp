import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import ANY, patch


SERVER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_DIR))

import igor_mcp_server as server


class ExecuteDispatchSafetyTests(unittest.TestCase):
    def test_execute2_failure_does_not_replay_through_capture_fallback(self):
        class Application:
            def Execute2(self, *_args):
                raise RuntimeError("dispatch failed after Igor accepted the command")

        with patch.object(server, "_execute_via_tempfile") as fallback:
            with self.assertRaisesRegex(server.IgorError, "outcome is unknown"):
                server._execute_command(Application(), "KillWaves/Z root:wave")

        fallback.assert_not_called()

    def test_missing_execute2_uses_legacy_capture_path(self):
        with patch.object(
            server, "_execute_via_tempfile", return_value="captured"
        ) as fallback:
            result = server._execute_command(object(), "Print 1")

        self.assertEqual(result, "captured")
        fallback.assert_called_once_with(ANY, "Print 1")


class WorkerAdmissionSafetyTests(unittest.TestCase):
    def test_prestart_exception_is_known_without_command_started(self):
        with patch.object(
            server, "_execute_igor_direct", side_effect=RuntimeError("connect failed")
        ):
            result = server._execute_igor_in_worker("Print 1")

        self.assertFalse(result["command_started"])
        self.assertTrue(result["outcome_known"])

    def test_post_dispatch_exception_is_unknown(self):
        def fail_after_dispatch(_command):
            server._command_start_marker.event.set()
            raise RuntimeError("COM returned no result")

        with patch.object(server, "_execute_igor_direct", side_effect=fail_after_dispatch):
            result = server._execute_igor_in_worker("KillWaves/Z root:wave")

        self.assertTrue(result["command_started"])
        self.assertFalse(result["outcome_known"])

    def test_timed_out_worker_keeps_admission_until_it_exits(self):
        finished = threading.Event()
        allow_exit = threading.Event()

        def slow_run(_command):
            allow_exit.wait(2)
            finished.set()
            return {"ok": True, "output": "late"}

        with (
            patch.object(server, "_execute_igor_direct", side_effect=slow_run),
            patch.object(server, "_IGOR_COMMAND_TIMEOUT_SECONDS", 0.02),
            patch.object(server, "_COMMAND_WORKER_LOCK_WAIT_SECONDS", 0),
        ):
            first = server._execute_igor_in_worker("Sleep/T 100")
            second = server._execute_igor_in_worker("Print 1")
            self.assertFalse(first["outcome_known"])
            self.assertTrue(first["error"] and "response deadline" in first["error"])
            self.assertFalse(second["command_started"])
            self.assertTrue(second["outcome_known"])
            self.assertIn("already running", second["error"])

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


if __name__ == "__main__":
    unittest.main()
