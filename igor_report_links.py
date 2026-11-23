"""Build Windows links to independent single-graph Igor files.

The installed compiled C# URI handler starts normal Igor instances without
Python, MCP, AI, or COM. The legacy Python handler remains for diagnostics only.
"""
import argparse
import asyncio
import base64
from datetime import timedelta
import html
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
REGISTRY = ROOT / ".local" / "native-report-links"
MARKER = '<script id="igor-native-report" type="application/json">'


def encode(data):
    return json.dumps(data, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render(data, token):
    esc = html.escape
    panels = []
    for i, figure in enumerate(data["figures"]):
        uri = f"igor-report://edit/{token}/{i}"
        legend = " · ".join(esc(t["label"]) for t in figure.get("traces", []))
        image = "data:image/png;base64," + figure["original_png"]
        if figure.get("native_preview"):
            relative = Path(figure["native_preview"]).relative_to(Path(data["native_report"]).parent)
            image = quote(relative.as_posix())
        panels.append(f'<section><h2>{esc(figure["title"])}</h2><a class="figure" href="{uri}" title="Edit this graph in Igor"><img src="{image}" alt="{esc(figure["title"])}"></a><p><a class="button" href="{uri}">Edit in Igor</a></p><p class="legend">{legend}</p><p>{esc(figure["caption"])}</p></section>')
    summary = ""
    analysis = data.get("exercise", {}).get("analysis", {})
    if analysis.get("rows"):
        rows = "".join(f'<tr><td>{r["Q_uM"]:.0f}</td><td>{r["F0_over_F"]:.3f}</td><td>{r["tau_fast_ns"]:.3f}</td><td>{r["tau_slow_ns"]:.3f}</td><td>{r["tau_A_ns"]:.3f}</td></tr>' for r in analysis["rows"])
        summary = '<details><summary>Analysis summary · simulated data</summary><div class="scroll"><table><tr><th>Q / µM</th><th>F₀/F</th><th>τ_fast / ns</th><th>τ_slow / ns</th><th>τ_A / ns</th></tr>' + rows + '</table></div><p>Synthetic teaching example; the numerical fit results are fixed. Editing a graph does not rerun the fits.</p></details>'
    native = {"format": "igor-native-report-v1", "token": token, "experiment": data["source_experiment"], "graphs": [f["graph"] for f in data["figures"]]}
    document = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>''' + esc(data["title"]) + '''</title><style>
body{font:16px/1.55 system-ui,sans-serif;color:#20333f;background:#eef2f5;margin:0}main{max-width:980px;margin:25px auto;padding:30px;background:white;border-radius:10px}h1{line-height:1.2;font-size:30px}h2{font-size:22px}.button{display:inline-block;background:#174a68;color:white;padding:9px 15px;border-radius:5px;text-decoration:none}.toolbar{display:flex;gap:14px;align-items:center;flex-wrap:wrap;padding:15px 0;border-block:1px solid #d8e2e9}.hint,.legend{color:#586e7b;font-size:14px}.figure{display:block;border:1px solid #d5e0e8;cursor:pointer}.figure:hover{border-color:#174a68;box-shadow:0 0 0 2px #b6d4e5}.figure img{display:block;width:100%;height:auto}section{margin:35px 0 50px}details{background:#f4f7f9;padding:14px;margin-top:20px}summary{cursor:pointer;font-weight:600}table{border-collapse:collapse;width:100%}th,td{padding:9px;border-bottom:1px solid #cfdae2;text-align:right;white-space:nowrap}.scroll{overflow-x:auto}.source{overflow-wrap:anywhere;font-size:12px;color:#627582}@media(max-width:600px){main{margin:0;padding:16px}h1{font-size:25px}}@media print{.toolbar,.button,.hint{display:none}section{break-inside:avoid}}
</style></head><body><main><h1>''' + esc(data["title"]) + '''</h1>
<p><strong>Click any figure to edit the original graph in Igor.</strong> Save your changes in Igor with Ctrl+S.</p>
<div class="toolbar"><a class="button" href="igor-report://edit/''' + token + '''/0">Open experiment in Igor</a><a class="button" href="igor-report://refresh/''' + token + '''">Refresh figures</a><span class="hint">After refreshing, reload this page.</span></div>
<p class="hint">Windows may ask once whether to open Igor Report Links. This report uses the installed Windows link handler and Igor’s own editor. Keep the linked .pxp file available.</p>
''' + summary + "".join(panels) + '<p class="source">Linked experiment: ' + esc(data["source_experiment"]) + '</p>' + MARKER + encode(native) + '</script></main></body></html>'
    if data.get("native_single_figures"):
        document = document.replace("Click any figure to edit the original graph in Igor.", "Click a figure to open only that figure in Igor.")
        document = document.replace("Save your changes in Igor with Ctrl+S.", "Ctrl+S saves that figure and updates its preview. Reload this page to see the changes.")
        document = document.replace("Open experiment in Igor", "Open first figure")
        document = document.replace('<a class="button" href="igor-report://refresh/' + token + '">Refresh figures</a>', '<button class="button" onclick="location.reload()">Reload previews</button>')
        document = document.replace("After refreshing, reload this page.", "Each figure is an independent editable copy.")
        document = document.replace("This report uses the installed Windows link handler and Igor’s own editor. Keep the linked .pxp file available.", "This report uses a native Windows launcher and Igor’s own editor. No Python, MCP, or AI is involved when opening a figure. Keep the report and its figure folder together.")
        document = document.replace("Linked experiment: ", "Original full experiment (preserved): ")
    return document


def register_report(path, data):
    if not data.get("source_experiment") or not Path(data["source_experiment"]).is_file():
        raise ValueError("Save the experiment to a .pxp file before making a linked report")
    path = Path(path).resolve()
    token = secrets.token_urlsafe(24)
    REGISTRY.mkdir(parents=True, exist_ok=True)
    entry = {"path": str(path), "data": data}
    (REGISTRY / (token + ".json")).write_text(encode(entry), encoding="utf-8")
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(render(data, token))
    except Exception:
        (REGISTRY / (token + ".json")).unlink(missing_ok=True)
        raise
    return token


def prepare_native_assets(path, data, export_graph):
    """Create independent native graph files without replacing the source experiment."""
    path = Path(path).resolve()
    assets = path.with_name(path.stem + "_figures")
    assets.mkdir()
    hook = assets / "ReportPreview.ipf"
    shutil.copy2(ROOT / "native" / "ReportPreview.ipf", hook)
    data.update(native_single_figures=True, native_assets=str(assets), native_report=str(path),
                native_preview_procedure=str(hook), native_hooks_prepared=False)
    for figure in data["figures"]:
        experiment = assets / (figure["graph"] + ".pxp")
        preview = experiment.with_suffix(".png")
        export_graph(figure["graph"], experiment)
        preview.write_bytes(base64.b64decode(figure["original_png"]))
        figure.update(native_file=str(experiment), native_preview=str(preview))


def parse_link(uri):
    parsed = urlsplit(uri)
    if parsed.scheme != "igor-report" or parsed.netloc not in ("edit", "refresh") or parsed.query or parsed.fragment:
        raise ValueError("Invalid Igor report link")
    parts = parsed.path.strip("/").split("/")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32}", parts[0]):
        raise ValueError("Invalid report registration")
    if parsed.netloc == "edit":
        if len(parts) != 2 or not parts[1].isdigit():
            raise ValueError("Invalid graph index")
        return parsed.netloc, parts[0], int(parts[1])
    if len(parts) != 1:
        raise ValueError("Invalid refresh link")
    return parsed.netloc, parts[0], 0


async def handle(uri):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    import igor_reports
    action, token, index = parse_link(uri)
    entry_path = REGISTRY / (token + ".json")
    if not entry_path.is_file():
        raise ValueError("This report is not registered on this computer. Install the link handler and register the report first.")
    entry = json.loads(entry_path.read_text(encoding="utf-8"))
    data = igor_reports.validate(entry["data"])
    if index >= len(data["figures"]):
        raise ValueError("Unknown graph")
    # A separate local MCP process works even when Codex/ChatGPT is closed.
    executable = Path(sys.executable).with_name("python.exe")
    params = StdioServerParameters(command=str(executable), args=["-B", "-m", "igor_mcp_server"], cwd=str(ROOT), env=dict(os.environ))
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errors:
        async with stdio_client(params, errlog=errors) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()
                async def call(name, arguments):
                    result = await client.call_tool(name, arguments, read_timeout_seconds=timedelta(seconds=150))
                    value = result.structuredContent
                    if result.isError or (isinstance(value, dict) and value.get("ok") is False):
                        raise RuntimeError((value or {}).get("error", "Igor operation failed"))
                    return value
                await call("ensure_igor", {})
                selected = await call("open_report_graph", {"file_path": data["source_experiment"], "graph": data["figures"][index]["graph"], "activate": action == "edit"})
                if action == "edit" and not selected.get("foreground") and selected.get("window_handle"):
                    # The Windows URI handler itself holds the foreground grant
                    # from the browser click, independently of its MCP child.
                    from ctypes import windll, wintypes
                    hwnd = wintypes.HWND(selected["window_handle"])
                    selected["foreground"] = bool(windll.user32.SetForegroundWindow(hwnd)) or int(windll.user32.GetForegroundWindow() or 0) == selected["window_handle"]
                if action == "edit" and not selected.get("foreground") and selected.get("server_pid"):
                    # Windows grants focus rights to the URI handler after a user
                    # click. Pass those rights to this specific MCP child only.
                    from ctypes import windll
                    if windll.user32.AllowSetForegroundWindow(selected["server_pid"]):
                        selected = await call("open_report_graph", {"file_path": data["source_experiment"], "graph": data["figures"][index]["graph"], "activate": True})
                if action == "refresh":
                    with tempfile.TemporaryDirectory(prefix="igor-linked-report-") as folder:
                        exported = Path(folder, "preview.html")
                        await call("create_html_report", {"output_path": str(exported), "graphs": [f["graph"] for f in data["figures"]], "title": data["title"], "notes": data["notes"], "editing_mode": "browser"})
                        updated = igor_reports.read_report(exported)
                    for old, fresh in zip(data["figures"], updated["figures"]):
                        old["original_png"] = fresh["original_png"]
                    path = Path(entry["path"])
                    temporary = path.with_name(path.name + ".refreshing")
                    temporary.write_text(render(data, token), encoding="utf-8")
                    os.replace(temporary, path)
                    entry_path.write_text(encode(entry), encoding="utf-8")
                    return {"ok": True, "action": "refresh", "file_path": str(path)}
                return {"ok": True, "action": "edit", **selected}


def install_handler():
    import winreg
    executable = ROOT / ".local" / "IgorFigureLink.exe"
    executable.parent.mkdir(exist_ok=True)
    compiler = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    subprocess.run([str(compiler), "/nologo", "/target:winexe", "/out:" + str(executable),
                    "/reference:System.Web.Extensions.dll", "/reference:System.Windows.Forms.dll",
                    str(ROOT / "native/IgorFigureLink.cs")], check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    command = f'"{executable}" "{REGISTRY}" "%1"'
    key_path = r"Software\Classes\igor-report"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path + r"\shell\open\command") as key:
            previous = winreg.QueryValueEx(key, "")[0]
        if previous != command and str(Path(__file__).resolve()) not in previous:
            raise RuntimeError("An unrelated igor-report handler already exists; it was preserved")
    except FileNotFoundError:
        pass
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:Igor Report Links")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path + r"\shell\open\command") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)
    return command


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("uri", nargs="?")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--convert", type=Path)
    parser.add_argument("--experiment", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.install:
            print(install_handler())
        elif args.convert:
            raise RuntimeError("Use the create_html_report MCP tool to generate a native single-figure package. The earlier direct browser-report conversion did not export the necessary graph copies.")
        elif args.uri:
            result = asyncio.run(handle(args.uri))
            REGISTRY.mkdir(parents=True, exist_ok=True)
            (REGISTRY / "last-action.json").write_text(encode(result), encoding="utf-8")
            if Path(sys.executable).name.lower() != "pythonw.exe":
                print(json.dumps(result))
        else:
            parser.error("Specify a report link, --install, or --convert")
    except Exception as exc:
        if Path(sys.executable).name.lower() == "pythonw.exe":
            from ctypes import windll
            windll.user32.MessageBoxW(None, str(exc), "Igor Report Links", 0x10)
        else:
            raise


if __name__ == "__main__":
    main()
