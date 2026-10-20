"""Standalone Markdown knowledge retrieval for the Igor Pro MCP package.

The module deliberately depends only on the Python standard library.  Documents
are enumerated by ``knowledge/manifest.json`` rather than by walking arbitrary
paths supplied by a caller.  This keeps document identifiers stable and makes
``read_document`` safe to expose through an MCP tool.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any


_PACKAGE_DIR = Path(__file__).resolve().parent
_KNOWLEDGE_DIR = Path(
    os.environ.get("IGOR_KNOWLEDGE_DIR", str(_PACKAGE_DIR / "knowledge"))
).expanduser().resolve()
_MANIFEST_PATH = _KNOWLEDGE_DIR / "manifest.json"

_MAX_QUERY_CHARS = 4_096
_MAX_RESULTS = 20
_DEFAULT_MAX_CHARS = 20_000
_MAX_READ_CHARS = 200_000
_MAX_EXCERPT_CHARS = 700
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_COMMAND_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:/[A-Za-z0-9_.,=+*?()/-]+)?$"
)


def _load_manifest() -> dict[str, Any]:
    try:
        value = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - bad install
        raise RuntimeError(f"Knowledge manifest cannot be loaded: {_MANIFEST_PATH}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("documents"), list):
        raise RuntimeError("Knowledge manifest has no document list")
    return value


_MANIFEST = _load_manifest()
_DOCUMENTS = tuple(
    document
    for document in _MANIFEST["documents"]
    if isinstance(document, dict)
    and isinstance(document.get("id"), str)
    and isinstance(document.get("path"), str)
)
_DOCUMENT_BY_ID = {document["id"]: document for document in _DOCUMENTS}


def _clean_query(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    cleaned = " ".join(value.split())
    if not cleaned:
        raise ValueError(f"{label} must be non-empty")
    if len(cleaned) > _MAX_QUERY_CHARS:
        raise ValueError(f"{label} exceeds {_MAX_QUERY_CHARS:,} characters")
    return cleaned


def _bounded_limit(value: object, *, label: str, default: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if value < 1:
        raise ValueError(f"{label} must be at least 1")
    return min(value, maximum)


def _collection_documents(collection: object) -> tuple[dict[str, Any], ...]:
    if collection is None:
        return _DOCUMENTS
    if not isinstance(collection, str):
        raise ValueError("collection must be 'manuals', 'guides', or 'all'")
    normalized = collection.strip().lower()
    if normalized == "all":
        return _DOCUMENTS
    aliases = {"official": "manuals", "curated": "guides"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"manuals", "guides"}:
        raise ValueError("collection must be 'manuals', 'guides', or 'all'")
    return tuple(document for document in _DOCUMENTS if document.get("collection") == normalized)


def _safe_document_path(document_id: str, document: dict[str, Any]) -> Path:
    """Resolve a manifest path while rejecting traversal and non-bundled files."""

    candidate_id = PurePosixPath(document_id)
    if (
        candidate_id.is_absolute()
        or "\\" in document_id
        or any(part in {"", ".", ".."} for part in candidate_id.parts)
        or ":" in document_id
    ):
        raise ValueError("document_id must be a relative bundled document identifier")

    relative_path = document.get("path")
    if not isinstance(relative_path, str):  # pragma: no cover - invalid manifest
        raise RuntimeError(f"Manifest entry has no path: {document_id}")
    relative = PurePosixPath(relative_path)
    if (
        relative.is_absolute()
        or "\\" in relative_path
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise RuntimeError(f"Manifest path is unsafe: {relative_path}")

    root = _KNOWLEDGE_DIR.resolve()
    path = (root / Path(*relative.parts)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:  # pragma: no cover - invalid manifest
        raise RuntimeError(f"Manifest path escapes knowledge directory: {relative_path}") from exc
    if not path.is_file():
        raise FileNotFoundError(f"Bundled knowledge document is missing: {document_id}")
    return path


@lru_cache(maxsize=128)
def _read_text(document_id: str) -> str:
    document = _DOCUMENT_BY_ID.get(document_id)
    if document is None:
        raise ValueError(f"Unknown document_id: {document_id}")
    path = _safe_document_path(document_id, document)
    raw = path.read_bytes()
    expected_hash = document.get("bundle_sha256")
    if isinstance(expected_hash, str) and expected_hash:
        actual_hash = hashlib.sha256(raw).hexdigest().upper()
        if actual_hash != expected_hash.upper():
            raise RuntimeError(f"Bundled knowledge hash mismatch: {document_id}")
    return raw.decode("utf-8")


def _document_title(document: dict[str, Any], text: str) -> str:
    title = document.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    for line in text.splitlines():
        match = _HEADING_RE.match(line.strip())
        if match and len(match.group(1)) == 1:
            return match.group(2).strip()
    return Path(str(document["path"])).stem


def _sections(text: str) -> tuple[tuple[str, str, int], ...]:
    """Return ``(heading, paragraph, character_offset)`` entries."""

    blocks: list[tuple[str, int, int]] = []
    heading = ""
    block_start = 0
    cursor = 0
    for line in text.splitlines(keepends=True):
        line_content = line.rstrip("\r\n")
        match = _HEADING_RE.match(line_content.strip())
        if match:
            if text[block_start:cursor].strip():
                blocks.append((heading, block_start, cursor))
            heading = match.group(2).strip()
            block_start = cursor + len(line)
        cursor += len(line)
    if text[block_start:len(text)].strip():
        blocks.append((heading, block_start, len(text)))

    paragraph_separator = re.compile(r"(?:\r?\n)[ \t]*(?:\r?\n)+")
    expanded: list[tuple[str, str, int]] = []
    for section_heading, start, end in blocks:
        section_text = text[start:end]
        paragraph_start = 0
        for separator in paragraph_separator.finditer(section_text):
            paragraph = section_text[paragraph_start:separator.start()]
            trimmed = paragraph.strip()
            if trimmed:
                leading_whitespace = len(paragraph) - len(paragraph.lstrip())
                expanded.append(
                    (section_heading, trimmed, start + paragraph_start + leading_whitespace)
                )
            paragraph_start = separator.end()
        paragraph = section_text[paragraph_start:]
        trimmed = paragraph.strip()
        if trimmed:
            leading_whitespace = len(paragraph) - len(paragraph.lstrip())
            expanded.append(
                (section_heading, trimmed, start + paragraph_start + leading_whitespace)
            )
    return tuple(expanded)


def _excerpt(text: str, query: str) -> str:
    compact = " ".join(text.split())
    folded = compact.casefold()
    position = folded.find(query.casefold())
    if position < 0:
        return compact[:_MAX_EXCERPT_CHARS]
    start = max(0, position - 180)
    end = min(len(compact), start + _MAX_EXCERPT_CHARS)
    result = compact[start:end]
    if start:
        result = "..." + result
    if end < len(compact):
        result += "..."
    return result


def _score(query: str, title: str, heading: str, block: str) -> int:
    query_folded = query.casefold()
    title_folded = title.casefold()
    heading_folded = heading.casefold()
    block_folded = block.casefold()
    terms = [term.casefold() for term in re.findall(r"[A-Za-z0-9_]+", query) if len(term) >= 2]
    haystack = title_folded + " " + heading_folded + " " + block_folded
    if query_folded not in haystack:
        matched_terms = sum(term in haystack for term in terms)
        required_terms = len(terms)
        if matched_terms < required_terms:
            return 0
    score = 0

    if query_folded == title_folded:
        score += 160
    elif query_folded in title_folded:
        score += 60
    if _COMMAND_RE.fullmatch(query):
        command_root = re.match(r"[A-Za-z]+", query)
        if command_root and command_root.group(0).casefold() in title_folded:
            score += 90
        if "reference" in title_folded:
            score += 130
        if "what's new" in title_folded or "what's changed" in title_folded:
            score -= 180
    if query_folded == heading_folded:
        score += 150
    elif query_folded in heading_folded:
        score += 85

    # Exact command/function matches outrank generic substring matches.  This
    # matters for queries such as CurveFit, WaveStats, SetScale/P, and pnt2x.
    if _COMMAND_RE.fullmatch(query):
        exact = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(query) + r"(?![A-Za-z0-9_])", re.IGNORECASE)
        score += 120 * min(1, len(exact.findall(block)))
    score += 45 * min(1, block_folded.count(query_folded))

    for term in terms:
        score += 12 * min(2, heading_folded.count(term))
        score += 3 * min(2, block_folded.count(term))
    return score


def search(
    query: str,
    topic: str | None = None,
    max_results: int = 3,
    collection: str | None = "all",
) -> dict[str, Any]:
    """Search bundled Markdown knowledge and return JSON-serializable results.

    ``collection`` is ``manuals``, ``guides``, or ``all`` (``official`` and
    ``curated`` are accepted aliases).  Search is
    intentionally deterministic and heading-aware; it does not use external
    services or an embedding index.
    """

    try:
        cleaned_query = _clean_query(query, "query")
        cleaned_topic = None if topic is None else _clean_query(topic, "topic")
        result_limit = _bounded_limit(
            max_results, label="max_results", default=3, maximum=_MAX_RESULTS
        )
        documents = _collection_documents(collection)
    except ValueError as exc:
        return {
            "query": query if isinstance(query, str) else "",
            "topic": topic if isinstance(topic, str) else None,
            "collection": collection if isinstance(collection, str) else "all",
            "results_count": 0,
            "error": str(exc),
            "results": [],
        }
    candidates: list[dict[str, Any]] = []

    for document in documents:
        document_id = str(document["id"])
        text = _read_text(document_id)
        title = _document_title(document, text)
        if cleaned_topic and cleaned_topic.casefold() not in (
            document_id + " " + title + " " + str(document.get("source_path", ""))
        ).casefold():
            continue
        for heading, block, read_offset in _sections(text):
            score = _score(cleaned_query, title, heading, block)
            if score <= 0:
                continue
            candidates.append(
                {
                    "document_id": document_id,
                    "collection": document.get("collection"),
                    "file": Path(str(document["path"])).stem,
                    "title": title,
                    "section": heading or title,
                    "score": score,
                    "excerpt": _excerpt(block, cleaned_query),
                    "read_offset": read_offset,
                    "source_path": document.get("source_path"),
                }
            )

    candidates.sort(
        key=lambda result: (
            -int(result["score"]),
            str(result["document_id"]).casefold(),
            str(result["section"]).casefold(),
        )
    )
    # Return one best excerpt per document so a long reference manual cannot
    # crowd all results with neighboring paragraphs from the same file.
    distinct_results: list[dict[str, Any]] = []
    seen_documents: set[str] = set()
    for candidate in candidates:
        document_id = str(candidate["document_id"])
        if document_id in seen_documents:
            continue
        seen_documents.add(document_id)
        distinct_results.append(candidate)
    top_results = distinct_results[:result_limit]
    canonical_collection = "all" if collection is None else str(collection).strip().lower()
    canonical_collection = {"official": "manuals", "curated": "guides"}.get(
        canonical_collection, canonical_collection
    )
    return {
        "query": cleaned_query,
        "topic": cleaned_topic,
        "collection": canonical_collection,
        "results_count": len(top_results),
        "error": None,
        "results": top_results,
    }


def read_document(
    document_id: str,
    max_chars: int = _DEFAULT_MAX_CHARS,
    offset: int = 0,
) -> dict[str, Any]:
    """Read a bounded character-offset page by a safe bundled document ID."""

    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id must be a non-empty string")
    if document_id not in _DOCUMENT_BY_ID:
        raise ValueError(f"Unknown document_id: {document_id}")
    limit = _bounded_limit(
        max_chars, label="max_chars", default=_DEFAULT_MAX_CHARS, maximum=_MAX_READ_CHARS
    )
    document = _DOCUMENT_BY_ID[document_id]
    text = _read_text(document_id)
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise ValueError("offset must be an integer")
    if offset < 0:
        raise ValueError("offset must be at least 0")
    if offset > len(text):
        raise ValueError(f"offset must not exceed document length ({len(text):,})")
    end_offset = min(offset + limit, len(text))
    content = text[offset:end_offset]
    return {
        "document_id": document_id,
        "collection": document.get("collection"),
        "title": _document_title(document, text),
        "content": content,
        "truncated": offset > 0 or end_offset < len(text),
        "max_chars": limit,
        "offset": offset,
        "total_chars": len(text),
        "next_offset": end_offset if end_offset < len(text) else None,
        "source_path": document.get("source_path"),
        "source_sha256": document.get("source_sha256"),
        "provenance": document.get("provenance"),
    }


__all__ = ["read_document", "search"]
