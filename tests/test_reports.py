import asyncio
import copy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import igor_reports as reports


def fixture():
    x, y = [0.0, 1.0, 2.0], [1.0, None, 3.0]
    fingerprint = reports.data_fingerprint(x, y)
    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j8S8AAAAASUVORK5CYII="
    return {"format": reports.FORMAT, "title": "Test report", "notes": "Notes", "source_experiment": "C:\\test\\source.pxp",
            "figures": [{"graph": "Graph1", "title": "Signal", "caption": "Caption", "edited": False, "legend": True,
                         "original_png": png, "x": {"label": "Time", "min": 0, "max": 2, "log": False},
                         "y": {"label": "Signal", "min": 0, "max": 4, "log": False},
                         "traces": [{"name": "signal", "label": "Signal", "y_wave": "root:signal", "x_wave": "",
                                     "x": x, "y": y, "data_sha256": fingerprint, "color": "#123456", "width": 1, "mode": 0}]}]}


class PortableReportTests(unittest.TestCase):
    def test_data_fingerprint_survives_javascript_number_serialization(self):
        self.assertEqual(reports.data_fingerprint([0.0, -0.0, 1.0, 1e20], [None, 2.0]),
                         reports.data_fingerprint([0, 0, 1, 100000000000000000000], [None, 2]))

    def test_round_trip_and_never_overwrites(self):
        data = fixture()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "report.html")
            reports.write_report(path, data)
            self.assertEqual(reports.read_report(path), data)
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                reports.write_report(path, data)
            self.assertEqual(path.read_bytes(), before)

    def test_script_injection_stays_in_data(self):
        data = fixture()
        data["title"] = '</script><script>alert("bad")</script>'
        data["notes"] = '<img src=x onerror="alert(1)">'
        data["figures"][0]["caption"] = reports.MARKER
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "report.html")
            reports.write_report(path, data)
            self.assertEqual(reports.read_report(path), data)
            source = path.read_text(encoding="utf-8")
            self.assertEqual(source.count(reports.MARKER), 1)
            self.assertNotIn('<script>alert("bad")', source)
            self.assertNotIn('fetch(', source)
            self.assertNotIn('<script src=', source)

    def test_rejects_invalid_axes_and_command_names(self):
        for mutate in (
            lambda d: d["figures"][0]["x"].update(min=2),
            lambda d: d["figures"][0]["x"].update(log=True),
            lambda d: d["figures"][0]["x"].update(min=float("nan")),
            lambda d: d["figures"][0].update(graph="Graph1;KillWaves signal"),
            lambda d: d["figures"][0]["traces"][0].update(name="signal)"),
            lambda d: d["figures"][0]["traces"][0].update(width=-1),
            lambda d: d["figures"][0]["traces"][0].update(color="red;bad"),
            lambda d: d["figures"][0]["x"].update(label="line\nKillWaves signal"),
            lambda d: d["figures"][0]["traces"][0].update(x=[1]),
        ):
            data = fixture()
            mutate(data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                reports.validate(data)


class ReportApplyTests(unittest.TestCase):
    def setUp(self):
        import igor_mcp_server as server
        self.server = server
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name, "edited.html")

    def run_apply(self, data, current=None, source=None):
        reports.write_report(self.path, data)
        server = self.server
        self.commands = []
        def immediate(operation):
            return operation(object(), 10.0)
        with patch.object(server, "_run_report_operation", side_effect=immediate), \
             patch.object(server, "_report_source", return_value=source or data["source_experiment"]), \
             patch.object(server, "_report_graph", return_value=current or fixture()["figures"][0]), \
             patch.object(server, "_execute_command", side_effect=lambda app, command: self.commands.append(command)):
            return server.apply_html_report(str(self.path))

    def test_apply_uses_only_validated_styles_and_does_not_save_experiment(self):
        data = fixture()
        data["figures"][0]["edited"] = True
        data["figures"][0]["x"].update(label='Time "quoted"', min=-2)
        data["figures"][0]["traces"][0].update(color="#ff0000", width=2)
        result = self.run_apply(data)
        self.assertTrue(result["ok"])
        self.assertFalse(result["experiment_saved"])
        self.assertEqual(len(self.commands), 7)
        self.assertTrue(any("rgb(signal)=(65535,0,0)" in c for c in self.commands))
        self.assertFalse(any("SaveExperiment" in c or "DoWindow" in c for c in self.commands))

    def test_data_change_or_wrong_experiment_prevents_all_styling(self):
        for kind in ("source", "data", "forged_data", "second_figure"):
            data = fixture()
            data["figures"][0]["edited"] = True
            current = copy.deepcopy(data["figures"][0])
            source = data["source_experiment"]
            if kind == "source":
                source = "C:\\other\\source.pxp"
            elif kind == "data":
                current["traces"][0]["data_sha256"] = "0" * 64
            elif kind == "forged_data":
                data["figures"][0]["traces"][0]["y"][0] = 500
            else:
                second = copy.deepcopy(data["figures"][0])
                second["graph"] = "Graph2"
                second["traces"][0]["y_wave"] = "root:other"
                data["figures"].append(second)
            with self.subTest(kind=kind), self.assertRaises(self.server.IgorError):
                self.run_apply(data, current, source)
            self.assertEqual(self.commands, [])
            self.path.unlink()

    def test_no_edits_is_a_noop(self):
        self.assertEqual(self.run_apply(fixture())["graphs"], [])
        self.assertEqual(self.commands, [])

    def test_tool_schemas_and_annotations(self):
        tools = {t.name: t for t in asyncio.run(self.server.mcp.list_tools())}
        self.assertFalse(tools["create_html_report"].annotations.destructiveHint)
        self.assertTrue(tools["apply_html_report"].annotations.destructiveHint)
        self.assertEqual(tools["create_html_report"].inputSchema["properties"]["graphs"]["maxItems"], 20)

    def test_report_uses_longer_bounded_wait(self):
        server = self.server
        def worker(operation, **kwargs):
            self.assertEqual(kwargs["timeout_seconds"], server._IGOR_REPORT_TIMEOUT_SECONDS)
            self.assertGreater(kwargs["timeout_seconds"], server._IGOR_COMMAND_TIMEOUT_SECONDS)
            return {"ok": True}
        with patch.object(server, "_run_igor_operation_in_worker_locked", side_effect=worker):
            self.assertTrue(server._run_report_operation(lambda app, version: None)["ok"])

    def test_custom_wait_allows_work_beyond_command_deadline(self):
        server = self.server
        def work():
            time.sleep(.04)
            return {"ok": True}
        with patch.object(server, "_IGOR_COMMAND_TIMEOUT_SECONDS", .001):
            result = server._run_igor_operation_in_worker_locked(
                work, failure_factory=lambda error, **details: {"ok": False, "error": error},
                action_description="test report", timeout_seconds=1,
            )
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
