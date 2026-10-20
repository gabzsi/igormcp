#!/usr/bin/env python3
"""Download WaveMetrics' indexed Markdown into a validated local bundle."""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import hashlib
import html
import json
import os
import re
import shutil
import socket
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


INDEX_URL = "https://docs.wavemetrics.com/llms.txt"
ALLOWED_HOST = "docs.wavemetrics.com"
CORE_REFERENCE_URLS = frozenset(
    {
        f"https://{ALLOWED_HOST}/igorpro/commands/{name}.md"
        for name in (
            "make",
            "duplicate",
            "setscale",
            "display",
            "appendtograph",
            "modifygraph",
            "label",
            "wavestats",
            "curvefit",
        )
    }
    | {
        f"https://{ALLOWED_HOST}/igorpro/igor-basics/waves.md",
        f"https://{ALLOWED_HOST}/igorpro/igor-basics/data-folders.md",
    }
)
MAX_INDEX_BYTES = 5 * 1024 * 1024
MAX_PAGE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_DOCUMENTS = 5000
TIMEOUT_SECONDS = 25
RETRIES = 3
WORKERS = 4
MAX_PRESERVED_BUNDLE_BYTES = 2 * 1024 * 1024 * 1024
_ENTRY_RE = re.compile(r"^\s*[-*+]\s+\[([^\]]+)\]\((https://[^)\s]+)\)\s*(?::\s*(.*))?$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_IMAGE_RE = re.compile(
    r"!\[(?P<alt>(?:\\.|[^\]])*)\]\(\s*(?:<(?P<angle>[^>]+)>|(?P<url>[^)\s]+))(?P<tail>[^)]*)\)"
)
_HTML_IMAGE_RE = re.compile(r"(?P<prefix><img\b[^>]*?\bsrc\s*=\s*)(?P<q>['\"])(?P<url>.*?)(?P=q)", re.I)


def classify_knowledge_tier(source_url: str | None) -> str:
    """Classify only the explicit small core-reference URL allowlist."""
    return "core-reference" if source_url in CORE_REFERENCE_URLS else "on-demand"


def apply_knowledge_tiers(manifest: dict[str, Any]) -> dict[str, Any]:
    """Add deterministic retrieval-tier metadata without changing document IDs or paths."""
    documents = manifest.get("documents")
    if not isinstance(documents, list):
        raise ValueError("Knowledge manifest has no documents list")
    online_counts = {"core-reference": 0, "on-demand": 0}
    total_counts = {"core-reference": 0, "on-demand": 0}
    for document in documents:
        if not isinstance(document, dict):
            raise ValueError("Knowledge manifest contains an invalid document record")
        if document.get("collection") == "manuals" and document.get("source_url"):
            tier = classify_knowledge_tier(str(document["source_url"]))
            online_counts[tier] += 1
        else:
            tier = "on-demand"
        document["knowledge_tier"] = tier
        total_counts[tier] += 1
    bundle = manifest.setdefault("bundle", {})
    bundle["knowledge_tier_policy"] = (
        "Only pages in the explicit core-reference allowlist are labeled core-reference; "
        "all other official pages default to on-demand. The compact everyday core is "
        "supplied in server initialization instructions."
    )
    source_scope = bundle.setdefault("source_scope", {})
    source_scope["official_online_knowledge_tier_counts"] = online_counts
    source_scope["all_document_knowledge_tier_counts"] = total_counts
    return manifest


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def _safe_url(url: str) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"Invalid URL port: {url}") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != ALLOWED_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"URL is outside the WaveMetrics documentation allowlist: {url}")
    return parsed


def parse_index(text: str) -> list[dict[str, str]]:
    """Parse first-party Markdown links and their section headings."""
    headings: list[str] = []
    pages: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            depth = len(heading.group(1))
            headings = headings[: depth - 1] + [heading.group(2).strip()]
            continue
        entry = _ENTRY_RE.match(line)
        if not entry:
            if line.lstrip().startswith(("- [", "* [", "+ [")) and ALLOWED_HOST in line and ".md" in line:
                raise ValueError(f"Could not parse an index Markdown link: {line[:160]}")
            continue
        title, url, _description = entry.groups()
        parsed = urllib.parse.urlsplit(url)
        if parsed.hostname != ALLOWED_HOST:
            continue
        _safe_url(url)
        if not parsed.path.endswith(".md"):
            continue
        path_parts = [urllib.parse.unquote(part) for part in parsed.path.split("/")[1:]]
        if not path_parts or any(
            part in {"", ".", ".."} or "/" in part or "\\" in part or ":" in part or "\x00" in part
            for part in path_parts
        ):
            raise ValueError(f"Unsafe documentation path in index: {url}")
        pages.setdefault(
            url,
            {
                "title": title.strip(),
                "url": url,
                "relative_path": "/".join(path_parts),
                "source_group": " > ".join(headings),
            },
        )
    if not pages:
        raise ValueError("The WaveMetrics index contained no first-party Markdown pages")
    if len(pages) > MAX_DOCUMENTS:
        raise ValueError(f"Index lists more than the safety limit of {MAX_DOCUMENTS} pages")
    paths = [page["relative_path"] for page in pages.values()]
    if len(set(paths)) != len(paths):
        raise ValueError("Different index URLs resolve to the same local Markdown path")
    return list(pages.values())


def _rewrite_image_urls(text: str, source_url: str) -> str:
    """Make inline image links absolute while leaving fenced examples untouched."""
    output: list[str] = []
    fence_char = ""
    fence_length = 0
    for line in text.splitlines(keepends=True):
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            if not fence_char:
                fence_char, fence_length = marker[0], len(marker)
                output.append(line)
                continue
            if marker[0] == fence_char and len(marker) >= fence_length:
                fence_char, fence_length = "", 0
            output.append(line)
            continue
        if fence_char:
            output.append(line)
            continue

        def absolute(url: str) -> str:
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme or parsed.netloc or url.startswith("#"):
                return url
            return urllib.parse.urljoin(source_url, url)

        def image(match: re.Match[str]) -> str:
            url = match.group("angle") or match.group("url")
            if not url:
                return match.group(0)
            resolved = absolute(url)
            rendered = f"<{resolved}>" if match.group("angle") else resolved
            return f"![{match.group('alt')}]({rendered}{match.group('tail')})"

        line = _IMAGE_RE.sub(image, line)

        def html_image(match: re.Match[str]) -> str:
            return match.group("prefix") + match.group("q") + absolute(match.group("url")) + match.group("q")

        output.append(_HTML_IMAGE_RE.sub(html_image, line))
    return "".join(output)


def _image_urls(text: str) -> list[str]:
    """Return Markdown and HTML image targets outside fenced code blocks."""
    urls: list[str] = []
    fence_char = ""
    fence_length = 0
    for line in text.splitlines():
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            if not fence_char:
                fence_char, fence_length = marker[0], len(marker)
                continue
            if marker[0] == fence_char and len(marker) >= fence_length:
                fence_char, fence_length = "", 0
            continue
        if fence_char:
            continue
        for match in _IMAGE_RE.finditer(line):
            url = match.group("angle") or match.group("url")
            if url:
                urls.append(html.unescape(url.strip()))
        for match in _HTML_IMAGE_RE.finditer(line):
            urls.append(html.unescape(match.group("url").strip()))
    return urls


class _AllowlistRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        _safe_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_AllowlistRedirect())


def fetch_url(url: str, maximum_bytes: int) -> bytes:
    _safe_url(url)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "igor-pro-mcp-knowledge/0.2", "Accept": "text/markdown, text/plain, */*"},
    )
    for attempt in range(RETRIES):
        try:
            with _OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
                data = response.read(maximum_bytes + 1)
            if len(data) > maximum_bytes:
                raise ValueError(f"Response exceeds the {maximum_bytes:,}-byte limit: {url}")
            return data
        except urllib.error.HTTPError as exc:
            if exc.code not in {408, 425, 429} and not 500 <= exc.code <= 599:
                raise
            error: Exception = exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            error = exc
        if attempt + 1 == RETRIES:
            raise RuntimeError(f"Download failed after {RETRIES} attempts: {url}") from error
        time.sleep(0.5 * (2**attempt))
    raise AssertionError("unreachable")


def _safe_relative(root: Path, relative: str) -> Path:
    posix = PurePosixPath(relative)
    if posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts) or "\\" in relative:
        raise ValueError(f"Unsafe bundle path: {relative}")
    result = (root / Path(*posix.parts)).resolve()
    result.relative_to(root.resolve())
    return result


def _guide_records(project_root: Path) -> list[dict[str, Any]]:
    manifest = json.loads((project_root / "knowledge" / "manifest.json").read_text(encoding="utf-8"))
    guides: list[dict[str, Any]] = []
    for item in manifest.get("documents", []):
        if item.get("collection") != "guides":
            continue
        source = _safe_relative(project_root / "knowledge", item["path"])
        data = source.read_bytes()
        if _sha256(data) != item.get("bundle_sha256", "").upper():
            raise ValueError(f"Bundled guide hash mismatch: {item.get('id')}")
        record = copy.deepcopy(item)
        record["knowledge_tier"] = "on-demand"
        record["_content"] = data
        guides.append(record)
    if len(guides) != 8:
        raise ValueError(f"Expected eight bundled curated guides; found {len(guides)}")
    return guides


def _validate_source_record(root: Path, record: dict[str, Any]) -> tuple[Path, bytes]:
    if not isinstance(record.get("id"), str) or not record["id"]:
        raise ValueError("Existing bundle has a document without a valid ID")
    if not isinstance(record.get("path"), str):
        raise ValueError(f"Existing document has no relative path: {record.get('id')}")
    path = _safe_relative(root, record["path"])
    if not path.is_file():
        raise ValueError(f"Existing document is missing: {record['id']} ({record['path']})")
    data = path.read_bytes()
    expected = record.get("bundle_sha256")
    if not isinstance(expected, str) or _sha256(data) != expected.upper():
        raise ValueError(f"Existing document hash mismatch: {record['id']}")
    return path, data


def _copy_preserved_file(stage: Path, relative_path: str, data: bytes, occupied: dict[str, str]) -> str:
    destination = _safe_relative(stage, relative_path)
    digest = _sha256(data)
    normalized = PurePosixPath(relative_path).as_posix().casefold()
    previous = occupied.get(normalized)
    if previous is not None and previous != digest:
        raise ValueError(f"Preserved bundle paths collide with different content: {relative_path}")
    if previous is None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        occupied[normalized] = digest
    return digest


def _merge_existing_bundles(
    sources: list[Path], stage: Path, package_guides: list[dict[str, Any]], occupied: dict[str, str] | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Preserve non-index manuals/assets and matching guides from local bundles."""
    guide_paths = {item["id"]: item["path"] for item in package_guides}
    preserved_guides: dict[str, dict[str, Any]] = {}
    manuals: dict[str, dict[str, Any]] = {}
    occupied = occupied if occupied is not None else {}
    copied_assets: set[str] = set()
    copied_bytes = 0
    visited: set[Path] = set()

    for source in sources:
        source = source.expanduser().resolve()
        if source in visited:
            continue
        visited.add(source)
        if not source.is_dir():
            raise ValueError(f"Existing knowledge bundle is not a directory: {source}")
        manifest_path = source / "manifest.json"
        if not manifest_path.is_file():
            raise ValueError(f"Existing knowledge bundle has no manifest.json: {source}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot read existing bundle manifest: {manifest_path}") from exc
        records = manifest.get("documents") if isinstance(manifest, dict) else None
        if not isinstance(records, list):
            raise ValueError(f"Existing bundle manifest has no documents list: {manifest_path}")
        ids: set[str] = set()
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str):
                raise ValueError(f"Invalid document record in existing bundle: {manifest_path}")
            if record["id"] in ids:
                raise ValueError(f"Duplicate document ID in existing bundle: {record['id']}")
            ids.add(record["id"])

            collection = record.get("collection")
            is_matching_guide = collection == "guides" and record["id"] in guide_paths and record.get("path") == guide_paths[record["id"]]
            is_local_manual = collection == "manuals" and not record.get("source_url") and not record.get("source_index_url")
            if not is_matching_guide and not is_local_manual:
                continue

            source_doc, data = _validate_source_record(source, record)
            if is_matching_guide and record["id"] in preserved_guides:
                continue
            if is_local_manual and record["id"] in manuals:
                old = manuals[record["id"]]
                if old["path"] != record["path"] or old["bundle_sha256"].upper() != record["bundle_sha256"].upper():
                    raise ValueError(f"Conflicting local manual IDs in existing bundles: {record['id']}")
                continue

            copied_bytes += len(data)
            if copied_bytes > MAX_PRESERVED_BUNDLE_BYTES:
                raise ValueError(f"Preserved local bundle exceeds {MAX_PRESERVED_BUNDLE_BYTES:,} bytes")
            _copy_preserved_file(stage, record["path"], data, occupied)
            copied = copy.deepcopy(record)
            if is_local_manual:
                copied["knowledge_tier"] = "on-demand"
                asset_records: list[dict[str, Any]] = []
                document_asset_paths: set[str] = set()
                for raw_url in _image_urls(data.decode("utf-8", errors="strict")):
                    parsed = urllib.parse.urlsplit(raw_url)
                    if parsed.scheme or parsed.netloc or not parsed.path:
                        continue
                    relative = urllib.parse.unquote(parsed.path)
                    if relative.startswith(("/", "\\")) or "\\" in relative:
                        raise ValueError(f"Unsafe local image path in {record['id']}: {raw_url}")
                    asset = (source_doc.parent / Path(*PurePosixPath(relative).parts)).resolve()
                    try:
                        asset_relative = asset.relative_to(source.resolve()).as_posix()
                    except ValueError as exc:
                        raise ValueError(f"Local image escapes its bundle in {record['id']}: {raw_url}") from exc
                    if not asset.is_file():
                        raise ValueError(f"Local image is missing in {record['id']}: {raw_url}")
                    asset_data = asset.read_bytes()
                    asset_digest = _copy_preserved_file(stage, asset_relative, asset_data, occupied)
                    asset_key = asset_relative.casefold()
                    if asset_key not in document_asset_paths:
                        document_asset_paths.add(asset_key)
                        asset_records.append({"path": asset_relative, "bundle_sha256": asset_digest, "bundle_bytes": len(asset_data)})
                    if asset_key not in copied_assets:
                        copied_assets.add(asset_key)
                        copied_bytes += len(asset_data)
                        if copied_bytes > MAX_PRESERVED_BUNDLE_BYTES:
                            raise ValueError(f"Preserved local bundle exceeds {MAX_PRESERVED_BUNDLE_BYTES:,} bytes")
                if asset_records:
                    copied["bundle_assets"] = asset_records
                manuals[copied["id"]] = copied
            else:
                copied["knowledge_tier"] = "on-demand"
                preserved_guides[copied["id"]] = copied

    for record in package_guides:
        guide_id = record["id"]
        if guide_id not in preserved_guides:
            data = record.pop("_content")
            _copy_preserved_file(stage, record["path"], data, occupied)
            preserved_guides[guide_id] = record
        else:
            record.pop("_content", None)
    guide_records = [preserved_guides[item["id"]] for item in package_guides]
    return guide_records, list(manuals.values()), len(copied_assets)


def _ensure_output_safe(project_root: Path, output: Path) -> Path:
    candidate = output.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = Path(os.path.abspath(candidate))
    for parent in (candidate, *candidate.parents):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Output path contains a symbolic link or reparse point: {parent}")
    target = candidate.resolve()
    package_knowledge = (project_root / "knowledge").resolve()
    try:
        target.relative_to(package_knowledge)
    except ValueError:
        pass
    else:
        raise ValueError("Downloaded manuals must stay outside the tracked knowledge/ package")
    try:
        project_root.resolve().relative_to(target)
    except ValueError:
        pass
    else:
        raise ValueError("Output cannot replace the project root or one of its parent directories")
    return target


def update_bundle(output: Path, include_existing: Path | None = None) -> tuple[int, str]:
    project_root = Path(__file__).resolve().parents[1]
    target = _ensure_output_safe(project_root, output)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and (target.is_symlink() or not target.is_dir()):
        raise ValueError(f"Output must be a normal directory, not a link or file: {target}")

    existing_sources: list[Path] = []
    if include_existing is not None:
        existing_sources.append(include_existing)
    if target.exists() and (target / "manifest.json").is_file():
        existing_sources.append(target)
    package_guides = _guide_records(project_root)

    index_bytes = fetch_url(INDEX_URL, MAX_INDEX_BYTES)
    try:
        index_text = index_bytes.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("WaveMetrics llms.txt is not valid UTF-8") from exc
    pages = parse_index(index_text)
    retrieved = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=target.parent))
    total_bytes = 0
    total_lock = __import__("threading").Lock()
    records: list[dict[str, Any] | None] = [None] * len(pages)

    def download(index_page: int, page: dict[str, str]) -> tuple[int, dict[str, Any]]:
        nonlocal total_bytes
        raw = fetch_url(page["url"], MAX_PAGE_BYTES)
        try:
            text = raw.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Page is not valid UTF-8: {page['url']}") from exc
        if not text.strip() or re.match(r"\s*<(?:!doctype\s+html|html)\b", text, re.I):
            raise ValueError(f"Expected a Markdown page, not empty content or an HTML response: {page['url']}")
        text = _rewrite_image_urls(text, page["url"])
        bundled = text.encode("utf-8")
        with total_lock:
            if total_bytes + len(raw) > MAX_TOTAL_BYTES:
                raise ValueError(f"Downloaded pages exceed the {MAX_TOTAL_BYTES:,}-byte total limit")
            total_bytes += len(raw)
        relative = "official/online/" + page["relative_path"]
        destination = _safe_relative(stage, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(bundled)
        doc_id = "manuals/online/" + page["relative_path"]
        return index_page, {
            "id": doc_id,
            "path": relative,
            "collection": "manuals",
            "title": page["title"],
            "source_url": page["url"],
            "source_sha256": _sha256(raw),
            "source_bytes": len(raw),
            "source_version": "current-index",
            "source_index_group": page["source_group"],
            "source_index_url": INDEX_URL,
            "source_index_sha256": _sha256(index_bytes),
            "retrieved_at": retrieved,
            "knowledge_tier": classify_knowledge_tier(page["url"]),
            "bundle_sha256": _sha256(bundled),
            "bundle_bytes": len(bundled),
        }

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = [executor.submit(download, index, page) for index, page in enumerate(pages)]
            completed = 0
            for future in concurrent.futures.as_completed(futures):
                index, record = future.result()
                records[index] = record
                completed += 1
                if completed % 100 == 0:
                    print(f"Downloaded {completed}/{len(pages)} official Markdown pages...", flush=True)

        guide_records, preserved_manuals, preserved_asset_count = _merge_existing_bundles(
            existing_sources, stage, package_guides
        )
        online_records = [record for record in records if record is not None]
        online_ids = {record["id"] for record in online_records}
        collisions = online_ids.intersection(record["id"] for record in preserved_manuals)
        if collisions:
            raise ValueError(f"Local manual IDs collide with refreshed online documents: {sorted(collisions)[:5]}")
        document_records = guide_records + preserved_manuals + online_records
        manifest = {
            "schema_version": 1,
            "bundle": {
                "name": "Igor Pro local WaveMetrics documentation",
                "description": "Index-listed WaveMetrics Markdown downloaded for local use, plus the eight bundled project guides.",
                "source_scope": {
                    "source_index_url": INDEX_URL,
                    "source_index_sha256": _sha256(index_bytes),
                    "retrieved_at": retrieved,
                    "source_version": "current-index",
                    "official_document_count": len(pages),
                    "preserved_local_document_count": len(preserved_manuals),
                    "preserved_local_asset_count": preserved_asset_count,
                    "curated_guide_count": len(guide_records),
                    "official_scope": "Markdown pages linked by the WaveMetrics llms.txt index; excludes images and unlisted site content.",
                },
                "collections": {
                    "manuals": {"description": "Official WaveMetrics documentation, including refreshed index pages and retained local manuals.", "document_count": len(online_records) + len(preserved_manuals)},
                "local_manuals": {"description": "Previously installed local manuals retained for coverage not present in llms.txt.", "document_count": len(preserved_manuals)},
                    "guides": {"description": "Project-authored technical guides.", "document_count": len(guide_records)},
                },
            },
            "documents": document_records,
        }
        apply_knowledge_tiers(manifest)
        manifest_path = stage / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _validate_manifest(stage, manifest)
        manifest_hash = _sha256(manifest_path.read_bytes())

        previous = None
        if target.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            previous = target.with_name(f"{target.name}.previous-{stamp}-{uuid.uuid4().hex[:8]}")
            target.replace(previous)
        try:
            stage.replace(target)
        except Exception:
            if previous is not None and not target.exists():
                previous.replace(target)
            raise
        return len(pages), manifest_hash
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def merge_downloaded_bundle(downloaded_bundle: Path, output: Path, include_existing: Path) -> tuple[int, str]:
    """Offline merge of a verified online download with an installed local bundle."""
    project_root = Path(__file__).resolve().parents[1]
    target = _ensure_output_safe(project_root, output)
    downloaded = downloaded_bundle.expanduser().resolve()
    local = include_existing.expanduser().resolve()
    if downloaded == target or target.is_relative_to(downloaded) or downloaded.is_relative_to(target):
        raise ValueError("Output must not replace or be inside the downloaded source bundle")
    if downloaded == local:
        raise ValueError("Downloaded source and local preservation bundle must be different directories")
    if local == target or local.is_relative_to(target):
        raise ValueError("Output must not replace or contain the local preservation bundle")
    if not downloaded.is_dir():
        raise ValueError(f"Downloaded source bundle is not a directory: {downloaded}")
    source_manifest_path = downloaded / "manifest.json"
    if not source_manifest_path.is_file():
        raise ValueError(f"Downloaded source bundle has no manifest.json: {downloaded}")
    try:
        source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read downloaded bundle manifest: {source_manifest_path}") from exc
    source_scope = source_manifest.get("bundle", {}).get("source_scope", {})
    if source_scope.get("source_index_url") != INDEX_URL:
        raise ValueError("Downloaded source bundle does not identify the WaveMetrics llms.txt index")
    documents = source_manifest.get("documents")
    if not isinstance(documents, list):
        raise ValueError("Downloaded source bundle has no documents list")
    guides: list[dict[str, Any]] = []
    online_docs: list[tuple[dict[str, Any], bytes]] = []
    seen_ids: set[str] = set()
    for record in documents:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            raise ValueError("Downloaded bundle has an invalid document record")
        if record["id"] in seen_ids:
            raise ValueError(f"Duplicate document ID in downloaded bundle: {record['id']}")
        seen_ids.add(record["id"])
        if record.get("collection") == "guides":
            path, data = _validate_source_record(downloaded, record)
            copied = copy.deepcopy(record)
            copied["_content"] = data
            guides.append(copied)
        elif record.get("collection") == "manuals" and record.get("source_index_url") == INDEX_URL:
            source_url = record.get("source_url", "")
            _safe_url(source_url)
            _path, data = _validate_source_record(downloaded, record)
            online_docs.append((copy.deepcopy(record), data))
        else:
            raise ValueError(f"Unexpected document in downloaded-only bundle: {record['id']}")
    expected_online = source_scope.get("official_document_count")
    if expected_online != len(online_docs):
        raise ValueError(f"Downloaded bundle count mismatch: expected {expected_online}, found {len(online_docs)}")
    if len(guides) != 8:
        raise ValueError(f"Downloaded bundle must contain eight curated guides; found {len(guides)}")

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and (target.is_symlink() or not target.is_dir()):
        raise ValueError(f"Output must be a normal directory, not a link or file: {target}")
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=target.parent))
    try:
        occupied: dict[str, str] = {}
        for record, data in online_docs:
            _copy_preserved_file(stage, record["path"], data, occupied)

        guide_records, preserved_manuals, preserved_asset_count = _merge_existing_bundles(
            [local], stage, guides, occupied
        )
        online_ids = {record["id"] for record, _data in online_docs}
        collisions = online_ids.intersection(record["id"] for record in preserved_manuals)
        if collisions:
            raise ValueError(f"Local manual IDs collide with online documents: {sorted(collisions)[:5]}")
        source_scope = copy.deepcopy(source_scope)
        source_scope["preserved_local_document_count"] = len(preserved_manuals)
        source_scope["preserved_local_asset_count"] = preserved_asset_count
        manifest = copy.deepcopy(source_manifest)
        manifest["bundle"]["description"] = "Index-listed WaveMetrics Markdown plus project-authored guides and preserved local manuals."
        manifest["bundle"]["source_scope"] = source_scope
        manifest["bundle"].setdefault("collections", {})["local_manuals"] = {
            "description": "Previously installed local manuals retained for coverage not present in llms.txt.",
            "document_count": len(preserved_manuals),
        }
        manifest["bundle"]["collections"]["manuals"]["document_count"] = len(online_docs) + len(preserved_manuals)
        manifest["documents"] = guide_records + preserved_manuals + [record for record, _data in online_docs]
        apply_knowledge_tiers(manifest)
        manifest_path = stage / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _validate_manifest(stage, manifest)
        manifest_hash = _sha256(manifest_path.read_bytes())

        previous = None
        if target.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            previous = target.with_name(f"{target.name}.previous-{stamp}-{uuid.uuid4().hex[:8]}")
            target.replace(previous)
        try:
            stage.replace(target)
        except Exception:
            if previous is not None and not target.exists():
                previous.replace(target)
            raise
        return len(online_docs), manifest_hash
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _validate_manifest(root: Path, manifest: dict[str, Any]) -> None:
    documents = manifest.get("documents")
    if not isinstance(documents, list) or not documents:
        raise ValueError("Generated manifest has no documents")
    ids: set[str] = set()
    for document in documents:
        if not isinstance(document, dict) or not isinstance(document.get("id"), str):
            raise ValueError("Generated manifest has an invalid document record")
        if document["id"] in ids:
            raise ValueError(f"Duplicate document ID: {document['id']}")
        ids.add(document["id"])
        path = _safe_relative(root, document["path"])
        data = path.read_bytes()
        if _sha256(data) != document.get("bundle_sha256", "").upper():
            raise ValueError(f"Generated bundle hash mismatch: {document['id']}")
        for asset_record in document.get("bundle_assets", []):
            asset_path = _safe_relative(root, asset_record["path"])
            asset_data = asset_path.read_bytes()
            if len(asset_data) != asset_record.get("bundle_bytes") or _sha256(asset_data) != asset_record.get("bundle_sha256", "").upper():
                raise ValueError(f"Generated asset hash mismatch: {document['id']} ({asset_record['path']})")
        if document.get("collection") == "manuals":
            for raw_url in _image_urls(data.decode("utf-8", errors="strict")):
                parsed = urllib.parse.urlsplit(raw_url)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                relative = urllib.parse.unquote(parsed.path)
                if relative.startswith(("/", "\\")) or "\\" in relative:
                    raise ValueError(f"Unsafe relative image URL in {document['id']}: {raw_url}")
                image_path = (path.parent / Path(*PurePosixPath(relative).parts)).resolve()
                try:
                    image_path.relative_to(root.resolve())
                except ValueError as exc:
                    raise ValueError(f"Relative image URL escapes output bundle in {document['id']}: {raw_url}") from exc
                if not image_path.is_file():
                    raise ValueError(f"Relative image URL is missing in {document['id']}: {raw_url}")


def retier_bundle_manifest(bundle_root: Path) -> tuple[dict[str, int], str]:
    """Validate a bundle, then atomically update only its manifest tier metadata."""
    project_root = Path(__file__).resolve().parents[1]
    root = _ensure_output_safe(project_root, bundle_root)
    if not root.is_dir():
        raise ValueError(f"Knowledge bundle is not a directory: {root}")
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError(f"Knowledge bundle has no regular manifest.json: {root}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read bundle manifest: {manifest_path}") from exc
    _validate_manifest(root, manifest)
    apply_knowledge_tiers(manifest)
    temporary = manifest_path.with_name(f".manifest-{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(manifest_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    counts = manifest["bundle"]["source_scope"]["all_document_knowledge_tier_counts"]
    return counts, _sha256(manifest_path.read_bytes())
    if len(ids) != len(documents):
        raise ValueError("Generated manifest contains duplicate IDs")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default = Path(__file__).resolve().parents[1] / ".local" / "knowledge"
    parser.add_argument("--output", type=Path, default=default, help="local bundle directory (default: .local/knowledge)")
    parser.add_argument("--include-existing", type=Path, help="preserve local manuals/assets and matching guides from an existing compatible bundle")
    parser.add_argument("--merge-downloaded", type=Path, help="offline-merge this validated downloaded bundle instead of fetching the index")
    parser.add_argument("--retier-bundle", type=Path, help="validate a local bundle and atomically update only its tier metadata; performs no network request")
    args = parser.parse_args(argv)
    try:
        if args.retier_bundle is not None:
            if args.merge_downloaded is not None or args.include_existing is not None:
                parser.error("--retier-bundle cannot be combined with merge options")
            counts, manifest_hash = retier_bundle_manifest(args.retier_bundle)
            print(f"Knowledge tiers updated: {args.retier_bundle.resolve()}")
            print(f"Core-reference: {counts['core-reference']}; on-demand: {counts['on-demand']}")
            print(f"Manifest SHA-256: {manifest_hash}")
            return 0
        if args.merge_downloaded is not None:
            if args.include_existing is None:
                parser.error("--merge-downloaded requires --include-existing")
            count, manifest_hash = merge_downloaded_bundle(args.merge_downloaded, args.output, args.include_existing)
        else:
            count, manifest_hash = update_bundle(args.output, args.include_existing)
    except Exception as exc:
        print(f"Knowledge update failed: {exc}", file=sys.stderr)
        return 1
    output = _ensure_output_safe(Path(__file__).resolve().parents[1], args.output)
    print(f"Knowledge bundle ready: {output}")
    output_root = _ensure_output_safe(Path(__file__).resolve().parents[1], args.output)
    manifest = json.loads((output_root / "manifest.json").read_text(encoding="utf-8"))
    local_count = manifest.get("bundle", {}).get("source_scope", {}).get("preserved_local_document_count", 0)
    print(f"Official Markdown pages: {count}; preserved local manuals: {local_count}; curated guides: 8")
    print(f"Manifest SHA-256: {manifest_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
