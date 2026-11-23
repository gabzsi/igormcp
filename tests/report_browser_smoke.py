"""Opt-in headless browser test for edit, download, reopen, and SVG export.

Run with an environment providing playwright and an installed Microsoft Edge.
"""
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import igor_reports
from playwright.sync_api import sync_playwright


def main():
    output = ROOT / "artifacts"
    source = Path(sys.argv[1])
    data = igor_reports.read_report(source)
    # Use current renderer even when a saved fixture predates UI changes.
    working = output / "Editable_HTML_Report_Demo.html"
    working.write_text(igor_reports.render_report(data), encoding="utf-8")
    errors, requests = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(accept_downloads=True, viewport={"width": 1200, "height": 900})
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: requests.append(request.url))
        page.goto(working.as_uri())
        assert page.locator(".figure img").count() == 1
        page.get_by_role("button", name="Edit report text", exact=True).click()
        page.locator("#reportTitle").fill('Edited <report> "quoted"')
        page.locator("#reportNotes").fill('Text with </script><script>window.injected=true</script> & Unicode: αβγ')
        page.get_by_role("button", name="Apply text", exact=True).click()
        page.get_by_role("button", name="Edit figure", exact=True).click()
        editor = page.locator(".figure .editor")
        editor.get_by_label("Figure title", exact=True).fill("Transient absorption")
        editor.get_by_label("Caption", exact=True).fill("Recipient edited this figure locally.")
        axes = editor.locator(".fields").nth(1)
        axes.get_by_label("Label", exact=True).fill("Time after pulse (ms)")
        axes.get_by_label("Minimum", exact=True).fill("8")
        axes.get_by_label("Maximum", exact=True).fill("8")
        page.get_by_role("button", name="Apply figure changes", exact=True).click()
        assert "must be finite and different" in page.locator(".error").inner_text()
        axes.get_by_label("Minimum", exact=True).fill("0.1")
        axes.get_by_label("Maximum", exact=True).fill("6")
        axes.get_by_label("Logarithmic", exact=True).check()
        trace = editor.locator(".trace").first
        trace.get_by_label("Color", exact=True).fill("#007744")
        trace.get_by_label("Line width", exact=True).fill("2.5")
        page.get_by_role("button", name="Save edited HTML", exact=True).click()
        assert "Apply your pending" in page.locator("#status").inner_text()
        page.get_by_role("button", name="Apply figure changes", exact=True).click()
        assert page.locator(".plot svg").count() == 1
        assert page.locator(".error").inner_text() == ""
        with page.expect_download() as download:
            page.get_by_role("button", name="Save edited HTML", exact=True).click()
        saved = output / "Edited_HTML_Report_browser_test.html"
        download.value.save_as(saved)
        parsed = igor_reports.read_report(saved)
        assert parsed["title"] == 'Edited <report> "quoted"'
        assert "</script><script>" in parsed["notes"]
        assert parsed["figures"][0]["x"]["log"] is True
        assert parsed["figures"][0]["traces"][0]["color"] == "#007744"
        for figure in parsed["figures"]:
            for trace in figure["traces"]:
                assert igor_reports.data_fingerprint(trace["x"], trace["y"]) == trace["data_sha256"]
        page.goto(saved.as_uri())
        assert page.locator("h1").inner_text() == parsed["title"]
        assert page.locator(".plot svg").count() == 1
        assert page.evaluate("window.injected") is None
        assert page.locator(".plot svg text").all_text_contents().count("Time after pulse (ms)") == 1
        with page.expect_download() as download:
            page.get_by_role("button", name="Download editable rendering as SVG", exact=True).click()
        svg_path = output / "Report_browser_test.svg"
        download.value.save_as(svg_path)
        assert ET.parse(svg_path).getroot().tag.endswith("svg")
        with page.expect_download() as download:
            page.get_by_role("button", name="Save edited HTML", exact=True).click()
        again = output / "Edited_HTML_Report_second_save.html"
        download.value.save_as(again)
        assert igor_reports.read_report(again) == parsed
        page.evaluate("scrollTo(0,0)")
        page.screenshot(path=str(output / "report_desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.evaluate("scrollTo(0,0)")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(output / "report_mobile.png"), full_page=True)
        assert not errors, errors
        assert all(url.startswith(("file:", "data:", "blob:")) for url in requests), requests
        context.close()
        browser.close()
    print(json.dumps({"ok": True, "browser_errors": errors, "external_requests": 0,
                      "checks": ["edit", "invalid limits", "unsaved form guard", "HTML download", "reopen", "script escaping", "SVG export", "second save", "mobile width"],
                      "demo_path": str(working)}, indent=2))


if __name__ == "__main__":
    main()
