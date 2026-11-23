"""Explicit opt-in live smoke test, entirely through a separate MCP client.

Creates only uniquely named fixture waves and graphs, leaving them for review.
Never saves, opens, or replaces an experiment. Not part of unittest discovery.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main():
    output = ROOT / "artifacts"
    output.mkdir(exist_ok=True)
    run = "HtmlTest_" + uuid.uuid4().hex[:8]
    env = dict(os.environ)
    env["IGOR_SESSION_DIR"] = str(ROOT / "sessions")
    params = StdioServerParameters(command=sys.executable, args=["-B", "-m", "igor_mcp_server"], cwd=str(ROOT), env=env)
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as client:
            await client.initialize()

            async def call(name, args):
                result = await client.call_tool(name, args)
                value = result.structuredContent or {"content": [getattr(c, "text", "") for c in result.content]}
                if result.isError:
                    raise RuntimeError(json.dumps(value))
                print(name, json.dumps(value), flush=True)
                return value

            await call("ensure_igor", {})
            await call("execute_igor", {"command": f'NewDataFolder root:{run}; Make/N=121 root:{run}:signal,root:{run}:fit; SetScale/P x,0,0.05,"ms",root:{run}:signal,root:{run}:fit; root:{run}:signal=exp(-x/1.3)+0.025*sin(18*x); root:{run}:fit=exp(-x/1.3); Display/N={run} root:{run}:signal,root:{run}:fit; Label/W={run} bottom,"Time (ms)"; Label/W={run} left,"Absorbance"; ModifyGraph/W={run} rgb(signal)=(12000,22000,55000),mode(signal)=3,rgb(fit)=(55000,14000,8000),lSize(fit)=2'})
            path = output / f"{run}.html"
            result = await call("create_html_report", {"output_path": str(path), "graphs": [run], "editing_mode": "browser", "title": "Editable spectroscopy report", "notes": "Click the figure to edit axes, colors, or captions. Save edited HTML to send an editable copy."})
            print("REPORT_PATH", path, flush=True)
            # Apply only to our fixture graph, and only if source identity is available.
            if result.get("source_experiment"):
                import igor_reports
                data = igor_reports.read_report(path)
                data["figures"][0]["edited"] = True
                data["figures"][0]["traces"][0]["color"] = "#227744"
                edited = output / f"{run}_apply_test.html"
                igor_reports.write_report(edited, data)
                await call("apply_html_report", {"file_path": str(edited)})


if __name__ == "__main__":
    asyncio.run(main())
