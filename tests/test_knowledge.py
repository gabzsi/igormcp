import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SERVER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_DIR))

import igor_knowledge as knowledge


EXPECTED_GUIDE_IDS = frozenset(
    {
        "guides/Igor-Pro-10-Automation-and-COM.md",
        "guides/Igor-Pro-10-Curve-Fitting.md",
        "guides/Igor-Pro-10-Data-Folders.md",
        "guides/Igor-Pro-10-Syntax-and-Commands.md",
        "guides/Igor-Pro-10-Wave-Architecture.md",
        "guides/Igor-Pro-Figure-Making-and-Publication-Styling.md",
        "guides/Igor-Pro-Gotchas-and-Pitfalls.md",
        "guides/Python-NumPy-to-Igor-Pro-Rosetta-Stone.md",
    }
)


class PublicKnowledgeBundleTests(unittest.TestCase):
    def test_public_manifest_contains_only_stable_curated_guides(self):
        documents = knowledge._DOCUMENTS
        self.assertEqual({document["id"] for document in documents}, EXPECTED_GUIDE_IDS)
        self.assertTrue(all(document["collection"] == "guides" for document in documents))
        self.assertFalse(any(document["collection"] == "manuals" for document in documents))

        for document in documents:
            with self.subTest(document_id=document["id"]):
                self.assertRegex(document["bundle_sha256"], r"^[0-9A-F]{64}$")
                self.assertNotIn("source_path", document)
                self.assertNotIn("source_sha256", document)
                bundled = knowledge._safe_document_path(document["id"], document)
                digest = hashlib.sha256(bundled.read_bytes()).hexdigest().upper()
                self.assertEqual(digest, document["bundle_sha256"])

    def test_curated_search_finds_guides_and_manual_search_is_empty(self):
        guide_result = knowledge.search("pnt2x", collection="guides", max_results=3)
        self.assertIsNone(guide_result["error"])
        self.assertEqual(
            guide_result["results"][0]["document_id"],
            "guides/Igor-Pro-Gotchas-and-Pitfalls.md",
        )
        self.assertEqual(guide_result["results"][0]["knowledge_tier"], "on-demand")

        manual_result = knowledge.search("CurveFit", collection="manuals", max_results=3)
        self.assertIsNone(manual_result["error"])
        self.assertEqual(manual_result["results_count"], 0)
        self.assertEqual(manual_result["results"], [])

    def test_read_document_is_json_serializable_and_bounded(self):
        result = knowledge.read_document(
            "guides/Igor-Pro-Gotchas-and-Pitfalls.md", max_chars=500
        )
        self.assertEqual(
            result["document_id"], "guides/Igor-Pro-Gotchas-and-Pitfalls.md"
        )
        self.assertEqual(len(result["content"]), 500)
        self.assertEqual(result["knowledge_tier"], "on-demand")
        self.assertTrue(result["truncated"])
        json.dumps(result)

    def test_read_document_pages_unicode_text_by_character_offset(self):
        document_id = "guides/Igor-Pro-Gotchas-and-Pitfalls.md"
        text = knowledge._read_text(document_id)
        unicode_offset = next(index for index, char in enumerate(text) if ord(char) > 127)
        start = max(0, unicode_offset - 4)
        first = knowledge.read_document(document_id, max_chars=8, offset=start)
        second = knowledge.read_document(
            document_id, max_chars=8, offset=first["next_offset"]
        )
        self.assertEqual(first["content"], text[start : start + 8])
        self.assertEqual(first["total_chars"], len(text))
        self.assertEqual(first["next_offset"], start + 8)
        self.assertIn(text[unicode_offset], first["content"] + second["content"])

    def test_search_result_offset_reads_matching_guide_content(self):
        result = knowledge.search("pnt2x", collection="guides", max_results=1)
        match = result["results"][0]
        text = knowledge._read_text(match["document_id"])
        self.assertIsInstance(match["read_offset"], int)
        self.assertIn("pnt2x", text[match["read_offset"] : match["read_offset"] + 1200])

    def test_document_reader_rejects_traversal_and_bad_offsets(self):
        for document_id in ("../README.md", "guides/../README.md", r"guides\x.md"):
            with self.subTest(document_id=document_id), self.assertRaises(ValueError):
                knowledge.read_document(document_id)

        document_id = "guides/Igor-Pro-Gotchas-and-Pitfalls.md"
        text = knowledge._read_text(document_id)
        for invalid in (-1, True, 1.5, "1"):
            with self.subTest(offset=invalid), self.assertRaises(ValueError):
                knowledge.read_document(document_id, offset=invalid)
        with self.assertRaisesRegex(ValueError, "document length"):
            knowledge.read_document(document_id, offset=len(text) + 1)

    def test_curated_guides_have_no_private_source_paths_or_wiki_links(self):
        for document in knowledge._DOCUMENTS:
            text = knowledge._read_text(document["id"])
            with self.subTest(document_id=document["id"]):
                self.assertIn("collection: guides", text)
                self.assertNotIn("raw/igor-pro-10-docs", text)
                self.assertNotIn("wiki/igor-mcp.wiki", text)
                self.assertNotIn("[[", text)
                self.assertNotIn("file://", text)

    def test_external_compatible_manual_bundle_loads_from_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            manual_dir = bundle / "manuals"
            manual_dir.mkdir()
            manual_path = manual_dir / "External Fixture.md"
            manual_text = "# External Fixture\n\nexternal-bundle-fixture-token\n"
            manual_path.write_text(manual_text, encoding="utf-8")
            digest = hashlib.sha256(manual_path.read_bytes()).hexdigest().upper()
            manifest = {
                "documents": [
                    {
                        "id": "manuals/External Fixture.md",
                        "path": "manuals/External Fixture.md",
                        "collection": "manuals",
                        "title": "External Fixture",
                        "knowledge_tier": "core-reference",
                        "bundle_sha256": digest,
                    }
                ]
            }
            (bundle / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            program = (
                "import json, igor_knowledge as k; "
                "r=k.search('external-bundle-fixture-token', collection='manuals'); "
                "assert r['results_count']==1, r; "
                "d=k.read_document(r['results'][0]['document_id']); "
                "print(json.dumps({'id':d['document_id'],'content':d['content'],"
                "'search_tier':r['results'][0]['knowledge_tier'],"
                "'read_tier':d['knowledge_tier']}))"
            )
            environment = os.environ.copy()
            environment["IGOR_KNOWLEDGE_DIR"] = str(bundle)
            environment["PYTHONPATH"] = str(SERVER_DIR)
            result = subprocess.run(
                [sys.executable, "-B", "-c", program],
                cwd=SERVER_DIR,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            loaded = json.loads(result.stdout)
            self.assertEqual(loaded["id"], "manuals/External Fixture.md")
            self.assertIn("external-bundle-fixture-token", loaded["content"])
            self.assertEqual(loaded["search_tier"], "core-reference")
            self.assertEqual(loaded["read_tier"], "core-reference")


if __name__ == "__main__":
    unittest.main()
