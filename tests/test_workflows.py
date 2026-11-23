import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import igor_sessions
from igor_workflows import (
    list_workflow_profiles,
    load_workflow_profile,
    parse_graph_style,
    parse_procedure_outline,
    save_or_merge_workflow_profile,
    summarize_style_conventions,
)


SAMPLE_IPF = """\
#pragma TextEncoding = "UTF-8"
#pragma rtGlobals=3
#pragma version=1.2
#include <Waves Average>
#include "SpectraHelpers", optional

Menu "Spectroscopy"
    "Load Batch...", /Q, LoadSpectraBatch()
    "-"
    SubMenu "Fitting"
        "Fit Voigt Peaks", FitVoigtAll()
    End
    "Export Summary", ExportSummaryTable()
End

// Normalize a spectrum to unit peak area.
// Preserves original wave scaling.
Static Function NormalizeSpectrum(WAVE w)
    WaveStats/Q w
    w /= V_max
End

// Build a canonical sample label string.
Override Function/S MakeSampleLabel(string prefix, variable idx)
    return prefix + "_" + num2str(idx)
End

ThreadSafe Function/WAVE GetWorkWave(string name)
    WAVE w = $name
    return w
End

Function/C ComplexResponse(variable f)
    return cmplx(f, -f)
End

Function/DF EnsureOutputDF()
    return root:
End

// Legacy batch runner macro
Macro RunAllFits(folderName)
    String folderName
    Print folderName
EndMacro

Proc QuickClean()
    KillWaves/Z tempWave
End

Window SpectrumPlot() : Graph
    PauseUpdate; Silent 1
    Display yWave vs xWave
EndMacro
"""

SAMPLE_GRAPH_A = """\
Window Graph0() : Graph
    PauseUpdate; Silent 1 // building window...
    Display /W=(35.25,41.75,430.5,250.25) intensity vs wavelength
    ModifyGraph mode=4,marker=19,lsize=1.5,rgb=(0,0,65535)
    ModifyGraph mode(fitWave)=0,lsize(fitWave)=2,rgb(fitWave)=(65535,0,0,32768)
    ModifyGraph font="Arial",fSize=12
    ModifyGraph tick=2,mirror=1,standoff=1,axThick=1.5
    ModifyGraph grid(left)=1,lblMargin(left)=10,lblMargin(bottom)=6
    ModifyGraph nticks=5,subtick=1,gridRGB=(50000,50000,50000)
    ModifyGraph log(bottom)=1,log(left)=0
    ModifyGraph width=283.5,height=198.25
    ModifyGraph width={Aspect,1.5}
    SetAxis left 0,100
    Label left "Intensity (a.u.)"
    Legend/C/N=text0/J/F=0/A=LT "\\\\s(intensity) Data"
    TextBox/C/N=text1/A=MC "Sample A"
EndMacro
"""

SAMPLE_GRAPH_B = """\
Window Graph1() : Graph
    Display signal vs timeWave
    ModifyGraph font="Arial",fSize=12,tick=2,mirror=1,standoff=1,axThick=1.5
    ModifyGraph font(bottom)="Helvetica",fSize(bottom)=14
    ModifyGraph width=283.5,height=210,lsize=1.5,marker=8,rgb=(0,0,65535)
    ModifyGraph rgb(signal)=(0,39321,0),grid=0
EndMacro
"""


class WorkflowExtractionTests(unittest.TestCase):
    def test_parse_procedure_outline_extracts_structure_and_docs(self):
        outline = parse_procedure_outline("SpectraTools.ipf", SAMPLE_IPF)

        self.assertEqual(outline["name"], "SpectraTools.ipf")
        self.assertGreater(outline["line_count"], 40)
        self.assertEqual(
            outline["pragmas"],
            [
                '#pragma TextEncoding = "UTF-8"',
                "#pragma rtGlobals=3",
                "#pragma version=1.2",
            ],
        )
        self.assertEqual(outline["includes"], ["Waves Average", "SpectraHelpers"])
        self.assertEqual(
            outline["menus"],
            [
                {
                    "menu": "Spectroscopy",
                    "items": ["Load Batch...", "Fit Voigt Peaks", "Export Summary"],
                }
            ],
        )
        self.assertFalse(outline["definitions_truncated"])

        by_name = {d["name"]: d for d in outline["definitions"]}
        self.assertEqual(
            set(by_name),
            {
                "NormalizeSpectrum",
                "MakeSampleLabel",
                "GetWorkWave",
                "ComplexResponse",
                "EnsureOutputDF",
                "RunAllFits",
                "QuickClean",
                "SpectrumPlot",
            },
        )
        self.assertEqual(by_name["NormalizeSpectrum"]["kind"], "Function")
        self.assertEqual(
            by_name["NormalizeSpectrum"]["doc"],
            "Normalize a spectrum to unit peak area. Preserves original wave scaling.",
        )
        self.assertEqual(by_name["MakeSampleLabel"]["kind"], "Function")
        self.assertIn("Override Function/S MakeSampleLabel", by_name["MakeSampleLabel"]["signature"])
        self.assertEqual(by_name["GetWorkWave"]["kind"], "Function")
        self.assertEqual(by_name["ComplexResponse"]["kind"], "Function")
        self.assertEqual(by_name["EnsureOutputDF"]["kind"], "Function")
        self.assertEqual(by_name["RunAllFits"]["kind"], "Macro")
        self.assertEqual(by_name["RunAllFits"]["doc"], "Legacy batch runner macro")
        self.assertEqual(by_name["QuickClean"]["kind"], "Proc")
        self.assertEqual(by_name["SpectrumPlot"]["kind"], "Window")
        self.assertGreater(by_name["SpectrumPlot"]["line"], by_name["QuickClean"]["line"])

    def test_parse_procedure_outline_caps_pragmas_and_definitions(self):
        lines = [f"#pragma custom{i}=1" for i in range(25)]
        for i in range(110):
            lines.append(f"Function Fn_{i}()")
            lines.append("End")
        outline = parse_procedure_outline("Huge.ipf", "\n".join(lines))

        self.assertEqual(len(outline["pragmas"]), 20)
        self.assertEqual(len(outline["definitions"]), 100)
        self.assertTrue(outline["definitions_truncated"])
        self.assertEqual(outline["definitions"][0]["name"], "Fn_0")
        self.assertEqual(outline["definitions"][-1]["name"], "Fn_99")

    def test_parse_graph_style_and_summarize_conventions(self):
        style_a = parse_graph_style("Graph0", SAMPLE_GRAPH_A)
        self.assertEqual(style_a["window"], "Graph0")
        self.assertEqual(style_a["fonts"], ["Arial"])
        self.assertEqual(style_a["font_sizes"], [12])
        self.assertEqual(style_a["ticks"], [2])
        self.assertEqual(style_a["mirrors"], [1])
        self.assertEqual(style_a["standoffs"], [1])
        self.assertEqual(style_a["ax_thicks"], [1.5])
        self.assertEqual(style_a["widths"], [283.5])
        self.assertEqual(style_a["heights"], [198.25])
        self.assertEqual(style_a["grids"], [1])
        self.assertEqual(style_a["lbl_margins"], [10.0, 6.0])
        self.assertEqual(style_a["log_axes"], ["bottom"])
        self.assertEqual(style_a["trace_modes"], [4, 0])
        self.assertEqual(style_a["trace_lsizes"], [1.5, 2.0])
        self.assertEqual(style_a["markers"], [19])
        self.assertEqual(style_a["rgb_colors"], [[0, 0, 65535], [65535, 0, 0]])
        self.assertTrue(style_a["has_legend"])
        self.assertTrue(style_a["has_textbox"])

        style_b = parse_graph_style("Graph1", SAMPLE_GRAPH_B)
        self.assertFalse(style_b["has_legend"])
        self.assertFalse(style_b["has_textbox"])
        self.assertEqual(style_b["grids"], [0])

        summary = summarize_style_conventions([style_a, style_b])
        self.assertEqual(summary["graphs_analyzed"], 2)
        self.assertEqual(summary["preferred_font"], "Arial")
        self.assertEqual(summary["preferred_font_size"], 12)
        self.assertEqual(summary["preferred_tick"], 2)
        self.assertEqual(summary["preferred_mirror"], 1)
        self.assertEqual(summary["preferred_standoff"], 1)
        self.assertEqual(summary["preferred_ax_thick"], 1.5)
        self.assertEqual(summary["common_widths_pt"], [283.5])
        self.assertEqual(set(summary["common_heights_pt"]), {198.25, 210.0})
        self.assertEqual(summary["common_trace_lsizes"][0], 1.5)
        self.assertEqual(set(summary["common_markers"]), {19, 8})
        self.assertEqual(summary["palette_rgb"][0], [0, 0, 65535])
        self.assertEqual(len(summary["palette_rgb"]), 3)
        self.assertTrue(summary["uses_grid"])
        self.assertTrue(summary["uses_legend"])

    def test_summarize_style_conventions_empty_and_grid_off(self):
        empty = summarize_style_conventions([])
        self.assertEqual(empty["graphs_analyzed"], 0)
        self.assertIsNone(empty["preferred_font"])
        self.assertFalse(empty["uses_grid"])
        self.assertFalse(empty["uses_legend"])
        self.assertEqual(empty["palette_rgb"], [])

        grid_off = summarize_style_conventions([parse_graph_style("G", SAMPLE_GRAPH_B)])
        self.assertFalse(grid_off["uses_grid"])
        self.assertFalse(grid_off["uses_legend"])


class WorkflowPersistenceTests(unittest.TestCase):
    def test_incremental_merge_and_session_memory_discovery(self):
        proc_a = parse_procedure_outline(
            "SpectraTools.ipf",
            '#include "BaseHelpers"\nMenu "Spectroscopy"\n"Load Batch..."\nEnd\nFunction LoadBatch()\nEnd\n',
        )
        proc_b_update = parse_procedure_outline(
            "SpectraTools.ipf",
            '#include "FitHelpers"\nMenu "Spectroscopy"\n"Fit Voigt"\nEnd\nFunction FitVoigt()\nEnd\n',
        )
        proc_c = parse_procedure_outline(
            "ExportTools.ipf",
            "Function ExportCSV()\nEnd\n",
        )
        style_a = parse_graph_style("Graph0", SAMPLE_GRAPH_A)
        style_b = parse_graph_style("Graph1", SAMPLE_GRAPH_B)

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"IGOR_SESSION_DIR": directory}
        ):
            first = save_or_merge_workflow_profile(
                "Spectroscopy Lab",
                {
                    "experiment": r"C:\Users\alice\private_project\ExpA.pxp",
                    "folder_patterns": ["root:RawData:", "root:Calibration:"],
                    "procedures": [proc_a],
                    "graph_styles": [style_a],
                    # Sensitive & bulk keys must never be persisted
                    "api_key": "super-secret-token",
                    "wave_data": [1.0, 2.0, 3.0, 4.0],
                },
                user_notes="Always subtract dark background before fitting.",
            )
            self.assertFalse(first["merged"])
            self.assertEqual(first["slug"], "spectroscopy-lab")
            self.assertEqual(first["observations_count"], 1)
            self.assertEqual(first["experiments_count"], 1)
            self.assertTrue(Path(first["json_path"]).is_file())
            self.assertTrue(Path(first["markdown_path"]).is_file())

            second = save_or_merge_workflow_profile(
                "Spectroscopy Lab",
                {
                    "experiment_name": "/home/bob/sensitive/ExpB.pxp",
                    "folder_patterns": ["root:RawData:", "root:Fits:"],
                    "procedures": [proc_b_update, proc_c],
                    "graph_styles": [style_b],
                },
                user_notes="Use Arial 12pt with inside ticks for publication plots.",
            )
            self.assertTrue(second["merged"])
            self.assertEqual(second["observations_count"], 2)
            self.assertEqual(second["experiments_count"], 2)
            self.assertEqual(second["style_defaults"]["graphs_analyzed"], 2)
            self.assertEqual(second["style_defaults"]["preferred_font"], "Arial")

            loaded = load_workflow_profile("Spectroscopy Lab")
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded["experiments_seen"], ["ExpA.pxp", "ExpB.pxp"])
            self.assertEqual(
                loaded["folder_patterns"],
                ["root:RawData:", "root:Calibration:", "root:Fits:"],
            )
            self.assertEqual(set(loaded["procedures"]), {"SpectraTools.ipf", "ExportTools.ipf"})
            merged_spectra = loaded["procedures"]["SpectraTools.ipf"]
            self.assertEqual(merged_spectra["includes"], ["BaseHelpers", "FitHelpers"])
            self.assertEqual(
                merged_spectra["menus"],
                [{"menu": "Spectroscopy", "items": ["Load Batch...", "Fit Voigt"]}],
            )
            self.assertEqual(
                [d["name"] for d in merged_spectra["definitions"]],
                ["LoadBatch", "FitVoigt"],
            )
            self.assertEqual(
                loaded["user_notes"],
                [
                    "Always subtract dark background before fitting.",
                    "Use Arial 12pt with inside ticks for publication plots.",
                ],
            )

            # Verify no full local directory paths or sensitive/bulk data leaked
            json_text = Path(second["json_path"]).read_text(encoding="utf-8")
            md_text = Path(second["markdown_path"]).read_text(encoding="utf-8")
            for leaked in ("alice", "private_project", "bob", "sensitive", "super-secret-token", "wave_data"):
                self.assertNotIn(leaked, json_text)
                self.assertNotIn(leaked, md_text)

            # Verify list_workflow_profiles
            listed = list_workflow_profiles()
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["slug"], "spectroscopy-lab")
            self.assertEqual(listed[0]["experiments_count"], 2)
            self.assertEqual(listed[0]["observations_count"], 2)
            self.assertEqual(listed[0]["procedures_count"], 2)
            self.assertEqual(listed[0]["graphs_analyzed"], 2)

            # Verify igor_sessions.search_session_memory & read_session_memory find it automatically
            search_res = igor_sessions.search_session_memory(
                "FitVoigt", record_type="summary", max_results=5
            )
            self.assertEqual(search_res["results_count"], 1)
            record_id = search_res["results"][0]["record_id"]
            self.assertEqual(record_id, loaded["record_id"])
            read_res = igor_sessions.read_session_memory(record_id)
            self.assertEqual(read_res["record_type"], "summary")
            self.assertIn("Workflow Profile: Spectroscopy Lab", read_res["content"])
            self.assertIn("Always subtract dark background before fitting.", read_res["content"])

    def test_slugification_and_style_observation_bound(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"IGOR_SESSION_DIR": directory}
        ):
            res_default = save_or_merge_workflow_profile("   ", {})
            self.assertEqual(res_default["slug"], "default")

            res_traversal = save_or_merge_workflow_profile("../../My Lab/Profile!!", {})
            self.assertEqual(res_traversal["slug"], "my-lab-profile")
            self.assertEqual(
                Path(res_traversal["json_path"]).parent, Path(directory).resolve()
            )

            many_styles = [parse_graph_style(f"G{i}", SAMPLE_GRAPH_A) for i in range(60)]
            save_or_merge_workflow_profile("bounded", {"graph_styles": many_styles})
            loaded = load_workflow_profile("bounded")
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(len(loaded["style_observations"]), 50)
            self.assertEqual(loaded["style_defaults"]["graphs_analyzed"], 50)
            self.assertIsNone(load_workflow_profile("nonexistent-profile"))


if __name__ == "__main__":
    unittest.main()
