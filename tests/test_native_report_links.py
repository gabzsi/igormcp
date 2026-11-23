import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import igor_report_links as links
from test_reports import fixture


class NativeLinkTests(unittest.TestCase):
    def test_parse_only_registered_action_shapes(self):
        token = "a" * 32
        self.assertEqual(links.parse_link(f"igor-report://edit/{token}/12"), ("edit", token, 12))
        self.assertEqual(links.parse_link(f"igor-report://refresh/{token}"), ("refresh", token, 0))
        for uri in (f"https://edit/{token}/0", "igor-report://edit/../../0", f"igor-report://edit/{token}/-1", f"igor-report://refresh/{token}/anything", f"igor-report://edit/{token}/0?command=bad"):
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                links.parse_link(uri)

    def test_native_report_has_direct_links_and_no_browser_editor(self):
        data = fixture()
        data["title"] = '<script>alert("x")</script>'
        source = links.render(data, "a" * 32)
        self.assertIn('igor-report://edit/' + "a" * 32 + '/0', source)
        self.assertIn('igor-report://refresh/' + "a" * 32, source)
        self.assertNotIn("Apply figure changes", source)
        self.assertNotIn('<script>alert("x")</script>', source)
        self.assertEqual(source.count(links.MARKER), 1)

    def test_registration_is_local_and_never_overwrites_report(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            experiment = root / "test.pxp"
            experiment.write_bytes(b"fixture")
            data = fixture()
            data["source_experiment"] = str(experiment)
            output = root / "report.html"
            with patch.object(links, "REGISTRY", root / "registry"):
                token = links.register_report(output, data)
                entry = json.loads((root / "registry" / (token + ".json")).read_text())
                self.assertEqual(entry["path"], str(output))
                with self.assertRaises(FileExistsError):
                    links.register_report(output, data)
                self.assertEqual(len(list((root / "registry").glob("*.json"))), 1)


class NativeGraphToolTests(unittest.TestCase):
    def test_hidden_source_is_exported_visible_then_restored(self):
        import igor_mcp_server as server
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.pxp"
            source.write_bytes(b"source preserved")
            output = root / "report.html"
            commands = []
            with patch.object(server, "_run_report_operation", side_effect=lambda operation: operation(Mock(), 10)), \
                 patch.object(server, "_report_source", return_value=str(source)), \
                 patch.object(server, "_report_graph", return_value=fixture()["figures"][0]), \
                 patch.object(server, "_query", return_value="1"), \
                 patch.object(server, "_execute_command", side_effect=lambda app, command: commands.append(command)), \
                 patch.object(links, "REGISTRY", root / "registry"):
                self.assertTrue(server.create_html_report(str(output), ["Graph1"])["ok"])
            self.assertEqual(commands[0], "GetWindow Graph1 hide")
            self.assertEqual(commands[1], "DoWindow/HIDE=0 Graph1")
            self.assertTrue(commands[2].startswith("SaveGraphCopy/W=Graph1 as "))
            self.assertEqual(commands[3], "DoWindow/HIDE=1 Graph1")
            self.assertEqual(source.read_bytes(), b"source preserved")

    def test_same_experiment_selects_graph_without_reload_or_save(self):
        import igor_mcp_server as server
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "source.pxp")
            path.write_bytes(b"fixture")
            app = Mock()
            with patch.object(server, "_run_report_operation", side_effect=lambda operation: operation(app, 10)), \
                 patch.object(server, "_report_source", return_value=str(path)), \
                 patch.object(server, "_query", return_value="1"), \
                 patch.object(server, "_execute_command") as command, \
                 patch.object(server, "_activate_report_window", return_value=True):
                result = server.open_report_graph(str(path), "Graph1")
            self.assertTrue(result["ok"])
            app.LoadExperiment.assert_not_called()
            command.assert_called_once_with(app, "DoWindow/F Graph1")
            self.assertFalse(result["experiment_saved"])

    def test_refresh_does_not_activate(self):
        import igor_mcp_server as server
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "source.pxp")
            path.write_bytes(b"fixture")
            with patch.object(server, "_run_report_operation", side_effect=lambda operation: operation(Mock(), 10)), \
                 patch.object(server, "_report_source", return_value=str(path)), \
                 patch.object(server, "_query", return_value="1"), \
                 patch.object(server, "_execute_command") as command, \
                 patch.object(server, "_activate_report_window") as activate:
                self.assertTrue(server.open_report_graph(str(path), "Graph1", False)["ok"])
            command.assert_not_called()
            activate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
