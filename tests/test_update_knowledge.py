from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from scripts import update_knowledge as updater


class ParseIndexTests(unittest.TestCase):
    def test_only_unique_allowlisted_markdown_links_are_selected(self):
        index = """# Igor Pro
## Basics
- [Waves](https://docs.wavemetrics.com/igorpro/waves.md): Wave manual.
- [Waves again](https://docs.wavemetrics.com/igorpro/waves.md): duplicate.
- [Forum](https://www.wavemetrics.com/forum): external link.
- [Not markdown](https://docs.wavemetrics.com/igorpro/page.html): HTML page.
"""
        pages = updater.parse_index(index)
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["title"], "Waves")
        self.assertEqual(pages[0]["relative_path"], "igorpro/waves.md")
        self.assertEqual(pages[0]["source_group"], "Igor Pro > Basics")

    def test_rejects_unsafe_first_party_path(self):
        index = "- [Escape](https://docs.wavemetrics.com/igorpro/%2e%2e/private.md): x\n"
        with self.assertRaises(ValueError):
            updater.parse_index(index)

    def test_rejects_non_allowlisted_or_non_https_urls(self):
        for url in (
            "http://docs.wavemetrics.com/igorpro/a.md",
            "https://example.org/a.md",
            "https://docs.wavemetrics.com.evil.test/a.md",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                updater._safe_url(url)


class RewriteImagesTests(unittest.TestCase):
    def test_resolves_relative_image_urls_but_preserves_fenced_code(self):
        markdown = """![Graph](../assets/graph.png)
<img src="/assets/table.png">
```markdown
![literal](../assets/example.png)
```
"""
        converted = updater._rewrite_image_urls(markdown, "https://docs.wavemetrics.com/igorpro/graphing/page.md")
        self.assertIn("![Graph](https://docs.wavemetrics.com/igorpro/assets/graph.png)", converted)
        self.assertIn('src="https://docs.wavemetrics.com/assets/table.png"', converted)
        self.assertIn("![literal](../assets/example.png)", converted)

    def test_image_inventory_ignores_fenced_examples(self):
        markdown = "![real](./real.png)\n```md\n![example](./not-real.png)\n```\n"
        self.assertEqual(updater._image_urls(markdown), ["./real.png"])


class KnowledgeTierTests(unittest.TestCase):
    def test_core_sections_and_bounded_commands_are_deterministic(self):
        cases = {
            "https://docs.wavemetrics.com/igorpro/igor-basics/multidimensional-waves.md": ("core-reference", "igor-basics"),
            "https://docs.wavemetrics.com/igorpro/analysis/image-processing.md": ("core-reference", "analysis"),
            "https://docs.wavemetrics.com/igorpro/graphing/3d-graphics.md": ("core-reference", "graphing"),
            "https://docs.wavemetrics.com/igorpro/data/igor-hdf5-guide.md": ("core-reference", "data"),
            "https://docs.wavemetrics.com/igorpro/user-interface/menus.md": ("core-reference", "user-interface"),
            "https://docs.wavemetrics.com/igorpro/programming/structures/point.md": ("core-reference", "programming"),
            "https://docs.wavemetrics.com/igorpro/commands/make.md": ("core-reference", "common-commands"),
            "https://docs.wavemetrics.com/igorpro/commands/savepict.md": ("core-reference", "common-commands"),
            "https://docs.wavemetrics.com/igorpro/commands/unknowncommand.md": ("on-demand", "command-catalog"),
            "https://docs.wavemetrics.com/igorpro/extensions/igor-filter-design-laboratory.md": ("on-demand", "specialist-reference"),
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(updater.classify_knowledge_page(url), expected)
        self.assertEqual(
            updater.classify_knowledge_page(
                "https://docs.wavemetrics.com/igorpro/commands/local-helper.md",
                "Igor Pro > Programming > Keywords",
            ),
            ("core-reference", "programming"),
        )
        self.assertEqual(updater.classify_knowledge_tier(None), "on-demand")

    def test_tiering_changes_only_metadata_and_reports_counts(self):
        docs = [
            {"id": "manuals/make", "path": "official/make.md", "collection": "manuals", "source_url": "https://docs.wavemetrics.com/igorpro/commands/make.md"},
            {"id": "manuals/other", "path": "official/other.md", "collection": "manuals", "source_url": "https://docs.wavemetrics.com/igorpro/commands/other.md"},
            {"id": "manuals/local", "path": "official/local.md", "collection": "manuals"},
            {"id": "guides/waves", "path": "curated/waves.md", "collection": "guides"},
        ]
        original_identity = [(item["id"], item["path"]) for item in docs]
        manifest = {"bundle": {}, "documents": docs}
        updater.apply_knowledge_tiers(manifest)
        self.assertEqual([(item["id"], item["path"]) for item in docs], original_identity)
        self.assertEqual([item["knowledge_tier"] for item in docs], ["core-reference", "on-demand", "on-demand", "on-demand"])
        self.assertEqual([item["knowledge_category"] for item in docs], ["common-commands", "command-catalog", "local-reference", "project-guide"])
        self.assertEqual(manifest["bundle"]["source_scope"]["official_online_knowledge_tier_counts"], {"core-reference": 1, "on-demand": 1})
        self.assertEqual(manifest["bundle"]["source_scope"]["all_document_knowledge_tier_counts"], {"core-reference": 1, "on-demand": 3})
        self.assertIn("not preloaded", manifest["bundle"]["knowledge_tier_policy"])

    def test_offline_retier_updates_manifest_only_after_validating_bundle(self):
        make_url = "https://docs.wavemetrics.com/igorpro/commands/make.md"
        page = b"# Make\nCreate a wave.\n"
        guide = b"# Guide\nAuthored content.\n"
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / "official").mkdir()
            (root / "curated").mkdir()
            (root / "official" / "make.md").write_bytes(page)
            (root / "curated" / "guide.md").write_bytes(guide)
            records = [
                {"id": "manuals/make", "path": "official/make.md", "collection": "manuals", "source_url": make_url, "bundle_sha256": hashlib.sha256(page).hexdigest().upper()},
                {"id": "guides/guide", "path": "curated/guide.md", "collection": "guides", "bundle_sha256": hashlib.sha256(guide).hexdigest().upper()},
            ]
            before = {record["path"]: (root / record["path"]).read_bytes() for record in records}
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({"bundle": {}, "documents": records}), encoding="utf-8")
            counts, _manifest_hash = updater.retier_bundle_manifest(root)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(counts, {"core-reference": 1, "on-demand": 1})
            self.assertEqual([item["knowledge_tier"] for item in manifest["documents"]], ["core-reference", "on-demand"])
            self.assertEqual([(item["id"], item["path"]) for item in manifest["documents"]], [(record["id"], record["path"]) for record in records])
            self.assertEqual({path: (root / path).read_bytes() for path in before}, before)


class BundleUpdateTests(unittest.TestCase):
    def test_builds_complete_hashed_bundle_and_preserves_curated_ids(self):
        page_url = "https://docs.wavemetrics.com/igorpro/basics/waves.md"
        index = f"# Igor Pro\n## Basics\n- [Waves]({page_url}): waves.\n".encode()
        source = b"# Waves\n![figure](./waves.png)\n"
        old_manifest = json.loads((Path(updater.__file__).resolve().parents[1] / "knowledge" / "manifest.json").read_text())
        expected_guides = {item["id"]: item["path"] for item in old_manifest["documents"]}

        def fake_fetch(url, _limit):
            return index if url == updater.INDEX_URL else source

        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch) / "local" / "knowledge"
            with patch.object(updater, "fetch_url", side_effect=fake_fetch):
                count, _manifest_hash = updater.update_bundle(output)
            self.assertEqual(count, 1)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual({d["id"]: d["path"] for d in manifest["documents"] if d["collection"] == "guides"}, expected_guides)
            manual = next(d for d in manifest["documents"] if d["collection"] == "manuals")
            self.assertEqual(manual["source_url"], page_url)
            self.assertEqual(manual["id"], "manuals/online/igorpro/basics/waves.md")
            self.assertEqual(manual["source_sha256"], hashlib.sha256(source).hexdigest().upper())
            self.assertEqual(manual["knowledge_tier"], "on-demand")
            self.assertEqual(manual["path"], "official/basics/waves.md")
            content = (output / manual["path"]).read_text(encoding="utf-8")
            self.assertIn("https://docs.wavemetrics.com/igorpro/basics/waves.png", content)
            self.assertEqual(manual["bundle_sha256"], hashlib.sha256(content.encode()).hexdigest().upper())

    def test_failed_download_leaves_existing_bundle_unchanged(self):
        page_url = "https://docs.wavemetrics.com/igorpro/waves.md"
        index = f"# Igor Pro\n- [Waves]({page_url}): waves.\n".encode()

        def failing_fetch(url, _limit):
            if url == updater.INDEX_URL:
                return index
            raise RuntimeError("simulated page failure")

        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch) / "knowledge"
            output.mkdir()
            sentinel = output / "keep.txt"
            sentinel.write_text("old bundle", encoding="utf-8")
            with patch.object(updater, "fetch_url", side_effect=failing_fetch):
                with self.assertRaisesRegex(RuntimeError, "simulated page failure"):
                    updater.update_bundle(output)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "old bundle")
            self.assertEqual(sorted(p.name for p in output.iterdir()), ["keep.txt"])

    def test_refuses_output_inside_tracked_knowledge(self):
        project = Path(updater.__file__).resolve().parents[1]
        with self.assertRaisesRegex(ValueError, "outside the tracked knowledge"):
            updater._ensure_output_safe(project, project / "knowledge" / "official")

    def test_include_existing_preserves_local_manual_and_referenced_asset(self):
        page_url = "https://docs.wavemetrics.com/igorpro/waves.md"
        index = f"# Igor Pro\n- [Waves]({page_url}): waves.\n".encode()
        page = b"# Waves\nNew online content.\n"
        legacy_md = b"# Legacy Automation\n![diagram](./Legacy_assets/diagram.png)\n"
        asset = b"fake-png-data"

        def fake_fetch(url, _limit):
            return index if url == updater.INDEX_URL else page

        with tempfile.TemporaryDirectory() as scratch:
            existing = Path(scratch) / "installed"
            (existing / "official" / "Legacy_assets").mkdir(parents=True)
            (existing / "official" / "Legacy.md").write_bytes(legacy_md)
            (existing / "official" / "Legacy_assets" / "diagram.png").write_bytes(asset)
            legacy_record = {
                "id": "manuals/Legacy.md",
                "path": "official/Legacy.md",
                "collection": "manuals",
                "title": "Legacy Automation",
                "source_version": "10.03",
                "bundle_sha256": hashlib.sha256(legacy_md).hexdigest().upper(),
            }
            (existing / "manifest.json").write_text(
                json.dumps({"schema_version": 1, "documents": [legacy_record]}), encoding="utf-8"
            )
            output = Path(scratch) / "local" / "knowledge"
            with patch.object(updater, "fetch_url", side_effect=fake_fetch):
                updater.update_bundle(output, existing)

            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            legacy = next(d for d in manifest["documents"] if d["id"] == legacy_record["id"])
            online = [d for d in manifest["documents"] if d.get("source_url")]
            self.assertEqual(len(online), 1)
            self.assertEqual(manifest["bundle"]["collections"]["manuals"]["document_count"], 2)
            self.assertEqual(legacy["source_version"], "10.03")
            self.assertEqual((output / legacy["path"]).read_bytes(), legacy_md)
            copied_asset = output / "official" / "Legacy_assets" / "diagram.png"
            self.assertEqual(copied_asset.read_bytes(), asset)
            self.assertEqual(legacy["bundle_assets"], [{
                "path": "official/Legacy_assets/diagram.png",
                "bundle_sha256": hashlib.sha256(asset).hexdigest().upper(),
                "bundle_bytes": len(asset),
            }])

    def test_refresh_preserves_local_manual_and_replaces_online_without_duplicates(self):
        page_url = "https://docs.wavemetrics.com/igorpro/waves.md"
        index = f"# Igor Pro\n- [Waves]({page_url}): waves.\n".encode()
        legacy_md = b"# Old installed page\n"
        legacy_hash = hashlib.sha256(legacy_md).hexdigest().upper()

        def fake_fetch(url, _limit):
            return index if url == updater.INDEX_URL else b"# Refreshed online page\n"

        with tempfile.TemporaryDirectory() as scratch:
            existing = Path(scratch) / "installed"
            (existing / "official").mkdir(parents=True)
            (existing / "official" / "legacy.md").write_bytes(legacy_md)
            legacy_record = {
                "id": "manuals/legacy.md", "path": "official/legacy.md", "collection": "manuals",
                "title": "Old installed page", "bundle_sha256": legacy_hash,
            }
            (existing / "manifest.json").write_text(json.dumps({"documents": [legacy_record]}), encoding="utf-8")
            output = Path(scratch) / "knowledge"
            with patch.object(updater, "fetch_url", side_effect=fake_fetch):
                updater.update_bundle(output, existing)
                updater.update_bundle(output)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            ids = [d["id"] for d in manifest["documents"]]
            self.assertEqual(ids.count("manuals/legacy.md"), 1)
            online = [d for d in manifest["documents"] if d.get("source_index_url") == updater.INDEX_URL]
            self.assertEqual(len(online), 1)
            self.assertEqual(online[0]["knowledge_tier"], "on-demand")
            self.assertEqual(next(d for d in manifest["documents"] if d["id"] == "manuals/legacy.md")["knowledge_tier"], "on-demand")
            self.assertEqual((output / online[0]["path"]).read_bytes(), b"# Refreshed online page\n")

    def test_update_emits_core_tier_for_allowlisted_page(self):
        page_url = "https://docs.wavemetrics.com/igorpro/commands/make.md"
        index = f"# Igor Pro\n- [Make]({page_url}): command reference.\n".encode()

        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch) / "knowledge"
            with patch.object(updater, "fetch_url", side_effect=lambda url, _limit: index if url == updater.INDEX_URL else b"# Make\nCreates waves.\n"):
                updater.update_bundle(output)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            make = next(item for item in manifest["documents"] if item.get("source_url") == page_url)
            self.assertEqual(make["knowledge_tier"], "core-reference")

    def test_offline_merge_reuses_downloaded_pages_and_preserves_installed_extras(self):
        page_url = "https://docs.wavemetrics.com/igorpro/waves.md"
        index = f"# Igor Pro\n- [Waves]({page_url}): waves.\n".encode()
        online_page = b"# Waves\nOnline page.\n"
        legacy_page = b"# Legacy Local Manual\nlocal-preservation-fixture-token\n"
        legacy_hash = hashlib.sha256(legacy_page).hexdigest().upper()

        def fake_fetch(url, _limit):
            return index if url == updater.INDEX_URL else online_page

        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            downloaded = root / "downloaded"
            with patch.object(updater, "fetch_url", side_effect=fake_fetch):
                updater.update_bundle(downloaded)
            existing = root / "installed"
            (existing / "official").mkdir(parents=True)
            (existing / "official" / "legacy-local.md").write_bytes(legacy_page)
            old_record = {
                "id": "manuals/Legacy Local Manual.md", "path": "official/legacy-local.md", "collection": "manuals",
                "title": "Legacy Local Manual", "source_version": "10.03", "bundle_sha256": legacy_hash,
            }
            (existing / "manifest.json").write_text(json.dumps({"documents": [old_record]}), encoding="utf-8")

            merged = root / "merged"
            count, _digest = updater.merge_downloaded_bundle(downloaded, merged, existing)
            self.assertEqual(count, 1)
            manifest = json.loads((merged / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["documents"]), 10)  # one online + one installed + eight guides
            retained = next(d for d in manifest["documents"] if d["id"] == old_record["id"])
            self.assertEqual(retained["source_version"], "10.03")
            self.assertEqual((merged / retained["path"]).read_bytes(), legacy_page)
            online = [d for d in manifest["documents"] if d.get("source_index_url") == updater.INDEX_URL]
            self.assertEqual(len(online), 1)
            self.assertEqual((merged / online[0]["path"]).read_bytes(), online_page)

    def test_offline_merge_rejects_unexpected_local_source_url(self):
        page_url = "https://docs.wavemetrics.com/igorpro/waves.md"
        index = f"# Igor Pro\n- [Waves]({page_url}): waves.\n".encode()
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            downloaded = root / "downloaded"
            with patch.object(updater, "fetch_url", side_effect=lambda url, _limit: index if url == updater.INDEX_URL else b"# Waves\n"):
                updater.update_bundle(downloaded)
            existing = root / "installed"
            (existing / "official" / "online" / "igorpro").mkdir(parents=True)
            local_page = b"# Bad replacement\n"
            (existing / "official" / "online" / "igorpro" / "waves.md").write_bytes(local_page)
            record = {
                "id": "manuals/online/igorpro/waves.md", "path": "official/online/igorpro/waves.md",
                "collection": "manuals", "title": "Bad replacement", "bundle_sha256": hashlib.sha256(local_page).hexdigest().upper(),
            }
            (existing / "manifest.json").write_text(json.dumps({"documents": [record]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "collide"):
                updater.merge_downloaded_bundle(downloaded, root / "merged", existing)


class CompactLayoutTests(unittest.TestCase):
    def test_official_url_layout_strips_only_the_redundant_igorpro_prefix(self):
        self.assertEqual(
            updater._official_bundle_path("https://docs.wavemetrics.com/igorpro/igor-basics/waves.md"),
            "official/igor-basics/waves.md",
        )
        self.assertEqual(
            updater._official_bundle_path("https://docs.wavemetrics.com/release-notes/10.03.md"),
            "official/release-notes/10.03.md",
        )
        with self.assertRaises(ValueError):
            updater._official_bundle_path("https://docs.wavemetrics.com/igorpro/%2e%2e/private.md")

    def test_offline_restructure_preserves_ids_content_guides_and_relative_assets(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            source = root / "source"
            old_page = "official/online/igorpro/graphing/graphs.md"
            page_data = b"# Graphs\n![sample](./media/plot.png)\n"
            asset_data = b"fixture image bytes"
            page = source / Path(*PurePosixPath(old_page).parts)
            page.parent.mkdir(parents=True)
            (page.parent / "media").mkdir()
            page.write_bytes(page_data)
            (page.parent / "media" / "plot.png").write_bytes(asset_data)
            guide_data = b"# Curated\n"
            (source / "curated").mkdir()
            (source / "curated" / "guide.md").write_bytes(guide_data)
            original_id = "manuals/online/igorpro/graphing/graphs.md"
            source_url = "https://docs.wavemetrics.com/igorpro/graphing/graphs.md"
            manifest = {
                "schema_version": 1,
                "bundle": {"source_scope": {"source_index_url": updater.INDEX_URL}},
                "documents": [
                    {
                        "id": original_id, "path": old_page, "collection": "manuals", "title": "Graphs",
                        "source_url": source_url, "source_index_url": updater.INDEX_URL,
                        "bundle_sha256": hashlib.sha256(page_data).hexdigest().upper(),
                    },
                    {
                        "id": "guides/curated", "path": "curated/guide.md", "collection": "guides",
                        "bundle_sha256": hashlib.sha256(guide_data).hexdigest().upper(),
                    },
                ],
            }
            (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            output = root / "restructured"
            remapped, _digest = updater.restructure_bundle(source, output)
            result = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            manual = next(record for record in result["documents"] if record["id"] == original_id)
            self.assertEqual(remapped, 1)
            self.assertEqual(manual["path"], "official/graphing/graphs.md")
            self.assertEqual(manual["id"], original_id)
            self.assertEqual(manual["bundle_sha256"], hashlib.sha256(page_data).hexdigest().upper())
            self.assertEqual((output / manual["path"]).read_bytes(), page_data)
            self.assertEqual((output / "official" / "graphing" / "media" / "plot.png").read_bytes(), asset_data)
            self.assertEqual(manual["bundle_assets"][0]["path"], "official/graphing/media/plot.png")
            self.assertEqual((output / "curated" / "guide.md").read_bytes(), guide_data)
            self.assertTrue((source / old_page).is_file())

    def test_offline_restructure_refuses_case_insensitive_path_collisions(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            source = root / "source"
            docs = []
            for identifier, old_path, url in (
                ("manuals/a", "old/a.md", "https://docs.wavemetrics.com/igorpro/data/shared.md"),
                ("manuals/b", "old/b.md", "https://docs.wavemetrics.com/data/shared.md"),
            ):
                data = f"# {identifier}\n".encode()
                path = source / old_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                docs.append({
                    "id": identifier, "path": old_path, "collection": "manuals", "source_url": url,
                    "source_index_url": updater.INDEX_URL,
                    "bundle_sha256": hashlib.sha256(data).hexdigest().upper(),
                })
            (source / "manifest.json").write_text(json.dumps({"bundle": {}, "documents": docs}), encoding="utf-8")
            output = root / "output"
            with self.assertRaisesRegex(ValueError, "collide"):
                updater.restructure_bundle(source, output)
            self.assertFalse(output.exists())
            self.assertTrue((source / "old" / "a.md").is_file())


if __name__ == "__main__":
    unittest.main()
