import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import igor_sessions as sessions
from igor_sessions import (
    append_tool_call_record,
    capture_markdown_session,
    read_session_memory,
    search_session_memory,
)


class SessionTests(unittest.TestCase):
    def test_explicit_capture_without_git_and_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"IGOR_SESSION_DIR": directory}):
                first = capture_markdown_session("../../sample", "Measured result", source_path=__file__)
                second = capture_markdown_session("../../sample", "Other result", source_path=__file__)
            self.assertNotEqual(first["path"], second["path"])
            self.assertEqual(Path(first["path"]).parent, Path(directory))
            text = Path(first["path"]).read_text(encoding="utf-8")
            self.assertIn("Measured result", text)
            self.assertIn("caller-supplied human/assistant summary", text)
            self.assertEqual(len(list(Path(directory).iterdir())), 2)

    def test_empty_summary_is_not_presented_as_a_trace(self):
        with self.assertRaisesRegex(ValueError, "summary is required"):
            capture_markdown_session("Test", "", source_path=__file__)

    def test_summary_and_tool_observations_are_searchable_and_readable_by_id(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"IGOR_SESSION_DIR": directory}
        ):
            summary = capture_markdown_session(
                "CurveFit run", "Successful fit with 5 parameters", source_path=__file__
            )
            call_id = append_tool_call_record(
                "execute_igor",
                {"command": "CurveFit wave"},
                result={"ok": True, "output": "fit complete"},
            )
            summaries = search_session_memory(
                "CurveFit", record_type="summary", max_results=5
            )
            calls = search_session_memory(
                "CurveFit", record_type="tool_call", max_results=5
            )
            self.assertEqual(summaries["results"][0]["record_id"], summary["session_id"])
            self.assertEqual(calls["results"][0]["record_id"], call_id)
            self.assertEqual(read_session_memory(summary["session_id"])["record_type"], "summary")
            tool_record = read_session_memory(call_id)
            self.assertEqual(tool_record["record_type"], "tool_call_observation")
            self.assertIn("fit complete", tool_record["content"])

    def test_tool_call_records_are_strict_json_bounded_and_omit_bulk_data(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"IGOR_SESSION_DIR": directory}
        ):
            record_id = append_tool_call_record(
                "read_variable",
                {"name": "V_test", "secret_token": "private", "extra": "x" * 50_000},
                result={
                    "value": float("nan"),
                    "data": [float("inf")] * 100_000,
                },
            )
            log_path = next(Path(directory).glob("tool-calls-*.jsonl"))
            line = log_path.read_bytes().rstrip(b"\n")
            self.assertLessEqual(len(line), sessions._MAX_TOOL_LOG_RECORD_BYTES)
            record = json.loads(
                line,
                parse_constant=lambda token: self.fail(f"non-standard JSON constant: {token}"),
            )
            self.assertEqual(record["record_id"], record_id)
            self.assertTrue(record["truncated"])
            self.assertEqual(record["arguments"]["secret_token"], "[redacted]")
            self.assertEqual(record["result"]["data"]["omitted"], "bulk data")
            self.assertTrue(read_session_memory(record_id)["content"].startswith("{"))

    def test_record_ids_do_not_accept_paths_and_summary_quota_preserves_history(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"IGOR_SESSION_DIR": directory}
        ), patch.object(sessions, "_MAX_SUMMARY_FILES", 1):
            first = capture_markdown_session("First", "Saved", source_path=__file__)
            original = Path(first["path"]).read_bytes()
            with self.assertRaisesRegex(ValueError, "storage limit"):
                capture_markdown_session("Second", "Also saved", source_path=__file__)
            self.assertEqual(Path(first["path"]).read_bytes(), original)
            with self.assertRaisesRegex(ValueError, "32-character"):
                read_session_memory("../outside.md")

    def test_search_skips_oversized_external_jsonl_files_and_lines(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"IGOR_SESSION_DIR": directory}
        ):
            oversized_file = Path(directory) / "tool-calls-99999999T999999Z-external.jsonl"
            oversized_file.write_bytes(b"x" * (sessions._MAX_TOOL_LOG_FILE_BYTES + 1))
            record_id = append_tool_call_record(
                "query_igor_help", {"query": "bounded-reader-marker"}, result={"results_count": 1}
            )
            log_path = next(
                path for path in Path(directory).glob("tool-calls-*.jsonl")
                if path != oversized_file
            )
            valid_line = log_path.read_bytes()
            log_path.write_bytes(
                b"x" * (sessions._MAX_TOOL_LOG_RECORD_BYTES + 100) + b"\n" + valid_line
            )
            matches = search_session_memory("bounded-reader-marker", record_type="tool_call")
            self.assertEqual(matches["results_count"], 1)
            self.assertEqual(matches["results"][0]["record_id"], record_id)
            self.assertEqual(read_session_memory(record_id)["record_type"], "tool_call_observation")

    def test_tool_log_quota_preserves_existing_append_only_history(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"IGOR_SESSION_DIR": directory}
        ), patch.object(sessions, "_MAX_TOOL_LOG_FILES", 1):
            append_tool_call_record("list_waves", {}, result={"ok": True})
            log_path = next(Path(directory).glob("tool-calls-*.jsonl"))
            original = log_path.read_bytes()
            with patch.object(sessions, "_MAX_TOOL_LOG_FILE_BYTES", len(original)):
                with self.assertRaisesRegex(ValueError, "existing logs were preserved"):
                    append_tool_call_record("list_waves", {}, result={"ok": True})
            self.assertEqual(log_path.read_bytes(), original)
            self.assertEqual(len(list(Path(directory).glob("tool-calls-*.jsonl"))), 1)
