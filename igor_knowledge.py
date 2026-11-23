"""Standalone Markdown knowledge retrieval for the Igor Pro MCP package.

The module deliberately depends only on the Python standard library.  Documents
are enumerated by ``knowledge/manifest.json`` rather than by walking arbitrary
paths supplied by a caller.  This keeps document identifiers stable and makes
``read_document`` safe to expose through an MCP tool.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
import stat
import threading
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple


_PACKAGE_DIR = Path(__file__).resolve().parent
_KNOWLEDGE_DIR = Path(
    os.environ.get("IGOR_KNOWLEDGE_DIR", str(_PACKAGE_DIR / "knowledge"))
).expanduser().resolve()
_MANIFEST_PATH = _KNOWLEDGE_DIR / "manifest.json"
_INITIAL_KNOWLEDGE_DIR = _KNOWLEDGE_DIR
_INITIAL_MANIFEST_PATH = _MANIFEST_PATH

_MAX_QUERY_CHARS = 4_096
_MAX_RESULTS = 20
_DEFAULT_MAX_CHARS = 20_000
_MAX_READ_CHARS = 200_000
_MAX_EXCERPT_CHARS = 700
_MAX_CACHED_ROOTS = 8
_KNOWLEDGE_TIERS = frozenset({"core-reference", "on-demand"})
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_PARAGRAPH_SEPARATOR_RE = re.compile(r"(?:\r?\n)[ \t]*(?:\r?\n)+")
_COMMAND_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:/[A-Za-z0-9_.,=+*?()/-]+)?$"
)
_COMMAND_ROOT_RE = re.compile(r"[A-Za-z]+")
_TERM_RE = re.compile(r"[A-Za-z0-9_]+")


class _IndexedSection(NamedTuple):
    heading: str
    heading_folded: str
    block: str
    block_folded: str
    read_offset: int


@dataclass
class _IndexedDocument:
    document_id: str
    path: Path
    file_sig: tuple[int, int]
    expected_hash: str
    collection: Any
    knowledge_tier: str
    file_stem: str
    title: str
    title_folded: str
    source_path: Any
    topic_haystack: str
    text: str
    full_folded: str
    sections: tuple[_IndexedSection, ...] | None = None


class _KnowledgeIndex:
    """In-memory index for a single resolved knowledge root."""

    def __init__(
        self,
        *,
        root: Path,
        manifest_path: Path,
        manifest_sig: tuple[int, int],
        manifest: dict[str, Any],
        documents: tuple[dict[str, Any], ...],
    ) -> None:
        self.root = root
        self.manifest_path = manifest_path
        self.manifest_sig = manifest_sig
        self.manifest = manifest
        self.documents = documents
        self.document_by_id = {document["id"]: document for document in documents}
        self.entries: dict[str, _IndexedDocument] = {}
        self.stats_hits = 0
        self.stats_misses = 0

    def collection_documents(self, collection: object) -> tuple[dict[str, Any], ...]:
        if collection is None:
            return self.documents
        if not isinstance(collection, str):
            raise ValueError("collection must be 'manuals', 'guides', or 'all'")
        normalized = collection.strip().lower()
        if normalized == "all":
            return self.documents
        aliases = {"official": "manuals", "curated": "guides"}
        normalized = aliases.get(normalized, normalized)
        if normalized not in {"manuals", "guides"}:
            raise ValueError("collection must be 'manuals', 'guides', or 'all'")
        return tuple(
            document for document in self.documents if document.get("collection") == normalized
        )


_INDEX_LOCK = threading.RLock()
_INDEX_CACHE: dict[Path, _KnowledgeIndex] = {}


def _stat_signature(path: Path) -> tuple[int, int]:
    st = os.stat(path)
    return (st.st_mtime_ns, st.st_size)


def _load_manifest_from(manifest_path: Path) -> dict[str, Any]:
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - bad install
        raise RuntimeError(f"Knowledge manifest cannot be loaded: {manifest_path}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("documents"), list):
        raise RuntimeError("Knowledge manifest has no document list")
    return value


def _load_manifest() -> dict[str, Any]:
    return _load_manifest_from(_MANIFEST_PATH)


def _parse_manifest_documents(manifest: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    return tuple(
        document
        for document in manifest["documents"]
        if isinstance(document, dict)
        and isinstance(document.get("id"), str)
        and isinstance(document.get("path"), str)
    )


_MANIFEST = _load_manifest()
_DOCUMENTS = _parse_manifest_documents(_MANIFEST)
_DOCUMENT_BY_ID = {document["id"]: document for document in _DOCUMENTS}
_INITIAL_MANIFEST_SIG = _stat_signature(_MANIFEST_PATH)


def _resolve_root_and_manifest() -> tuple[Path, Path]:
    root = _KNOWLEDGE_DIR.resolve()
    if _MANIFEST_PATH != _INITIAL_MANIFEST_PATH:
        manifest_path = _MANIFEST_PATH.resolve()
    else:
        manifest_path = (root / "manifest.json").resolve()
    return root, manifest_path


def _get_index() -> _KnowledgeIndex:
    root, manifest_path = _resolve_root_and_manifest()
    try:
        manifest_sig = _stat_signature(manifest_path)
    except OSError as exc:  # pragma: no cover - bad install
        raise RuntimeError(f"Knowledge manifest cannot be loaded: {manifest_path}") from exc

    with _INDEX_LOCK:
        cached = _INDEX_CACHE.get(root)
        if (
            cached is not None
            and cached.manifest_path == manifest_path
            and cached.manifest_sig == manifest_sig
        ):
            return cached

        if (
            root == _INITIAL_KNOWLEDGE_DIR
            and manifest_path == _INITIAL_MANIFEST_PATH
            and manifest_sig == _INITIAL_MANIFEST_SIG
        ):
            manifest = _MANIFEST
            documents = _DOCUMENTS
        else:
            manifest = _load_manifest_from(manifest_path)
            documents = _parse_manifest_documents(manifest)

        index = _KnowledgeIndex(
            root=root,
            manifest_path=manifest_path,
            manifest_sig=manifest_sig,
            manifest=manifest,
            documents=documents,
        )
        if len(_INDEX_CACHE) >= _MAX_CACHED_ROOTS and root not in _INDEX_CACHE:
            oldest_key = next(iter(_INDEX_CACHE))
            _INDEX_CACHE.pop(oldest_key, None)
        _INDEX_CACHE[root] = index
        return index


def clear_cache() -> None:
    """Clear all cached knowledge indexes and document entries."""

    with _INDEX_LOCK:
        _INDEX_CACHE.clear()


def _knowledge_tier(document: dict[str, Any]) -> str:
    """Return a recognized reference tier, defaulting older bundles to on-demand."""

    value = document.get("knowledge_tier")
    return value if isinstance(value, str) and value in _KNOWLEDGE_TIERS else "on-demand"


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


def _safe_document_path(
    document_id: str,
    document: dict[str, Any],
    *,
    root: Path | None = None,
) -> Path:
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

    resolved_root = _KNOWLEDGE_DIR.resolve() if root is None else root
    path = (resolved_root / Path(*relative.parts)).resolve()
    try:
        path.relative_to(resolved_root)
    except ValueError as exc:  # pragma: no cover - invalid manifest
        raise RuntimeError(f"Manifest path escapes knowledge directory: {relative_path}") from exc
    try:
        st = os.stat(path)
    except OSError as exc:
        raise FileNotFoundError(f"Bundled knowledge document is missing: {document_id}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise FileNotFoundError(f"Bundled knowledge document is missing: {document_id}")
    return path


def _resolve_document_record(index: _KnowledgeIndex, document_id: str) -> dict[str, Any] | None:
    if (
        index.root == _INITIAL_KNOWLEDGE_DIR
        and index.manifest_path == _INITIAL_MANIFEST_PATH
        and index.manifest_sig == _INITIAL_MANIFEST_SIG
    ):
        return _DOCUMENT_BY_ID.get(document_id)
    return index.document_by_id.get(document_id)


def _get_entry(index: _KnowledgeIndex, document: dict[str, Any]) -> _IndexedDocument:
    document_id = str(document["id"])
    raw_expected_hash = document.get("bundle_sha256")
    expected_hash = raw_expected_hash.upper() if isinstance(raw_expected_hash, str) and raw_expected_hash else ""

    with _INDEX_LOCK:
        cached = index.entries.get(document_id)

    if cached is not None and cached.expected_hash == expected_hash:
        try:
            current_sig = _stat_signature(cached.path)
        except OSError:
            current_sig = None
        if current_sig == cached.file_sig:
            with _INDEX_LOCK:
                index.stats_hits += 1
            return cached

    path = _safe_document_path(document_id, document, root=index.root)
    file_sig = _stat_signature(path)
    raw = path.read_bytes()
    if expected_hash:
        actual_hash = hashlib.sha256(raw).hexdigest().upper()
        if actual_hash != expected_hash:
            raise RuntimeError(f"Bundled knowledge hash mismatch: {document_id}")
    text = raw.decode("utf-8")
    title = _document_title(document, text)
    title_folded = title.casefold()
    source_path = document.get("source_path")
    entry = _IndexedDocument(
        document_id=document_id,
        path=path,
        file_sig=file_sig,
        expected_hash=expected_hash,
        collection=document.get("collection"),
        knowledge_tier=_knowledge_tier(document),
        file_stem=Path(str(document["path"])).stem,
        title=title,
        title_folded=title_folded,
        source_path=source_path,
        topic_haystack=(document_id + " " + title + " " + str(source_path or "")).casefold(),
        text=text,
        full_folded=title_folded + " " + text.casefold(),
    )
    with _INDEX_LOCK:
        index.entries[document_id] = entry
        index.stats_misses += 1
    return entry


def _get_sections(entry: _IndexedDocument) -> tuple[_IndexedSection, ...]:
    sections = entry.sections
    if sections is not None:
        return sections
    computed = tuple(
        _IndexedSection(
            heading=heading,
            heading_folded=heading.casefold(),
            block=block,
            block_folded=block.casefold(),
            read_offset=read_offset,
        )
        for heading, block, read_offset in _sections(entry.text)
    )
    with _INDEX_LOCK:
        if entry.sections is None:
            entry.sections = computed
        return entry.sections


def _read_text(document_id: str) -> str:
    index = _get_index()
    document = _resolve_document_record(index, document_id)
    if document is None:
        raise ValueError(f"Unknown document_id: {document_id}")
    return _get_entry(index, document).text


_read_text.cache_clear = clear_cache  # type: ignore[attr-defined]


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

    expanded: list[tuple[str, str, int]] = []
    for section_heading, start, end in blocks:
        section_text = text[start:end]
        paragraph_start = 0
        for separator in _PARAGRAPH_SEPARATOR_RE.finditer(section_text):
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
    terms = [term.casefold() for term in _TERM_RE.findall(query) if len(term) >= 2]
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
        command_root = _COMMAND_ROOT_RE.match(query)
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
        score += 120 * (1 if exact.search(block) is not None else 0)
    if query_folded in block_folded:
        score += 45

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
        index = _get_index()
        documents = index.collection_documents(collection)
    except ValueError as exc:
        return {
            "query": query if isinstance(query, str) else "",
            "topic": topic if isinstance(topic, str) else None,
            "collection": collection if isinstance(collection, str) else "all",
            "results_count": 0,
            "error": str(exc),
            "results": [],
        }

    query_folded = cleaned_query.casefold()
    topic_folded = cleaned_topic.casefold() if cleaned_topic else None
    terms = tuple(term.casefold() for term in _TERM_RE.findall(cleaned_query) if len(term) >= 2)
    is_command = bool(_COMMAND_RE.fullmatch(cleaned_query))
    command_root_match = _COMMAND_ROOT_RE.match(cleaned_query) if is_command else None
    command_root_folded = command_root_match.group(0).casefold() if command_root_match else None
    exact_re = (
        re.compile(
            r"(?<![A-Za-z0-9_])" + re.escape(cleaned_query) + r"(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        if is_command
        else None
    )

    best_per_document: list[tuple[int, str, str, _IndexedDocument, _IndexedSection]] = []

    for document in documents:
        entry = _get_entry(index, document)
        if topic_folded and topic_folded not in entry.topic_haystack:
            continue

        if terms:
            if any(term not in entry.full_folded for term in terms):
                continue
        elif not is_command and query_folded not in entry.full_folded:
            continue

        title_folded = entry.title_folded
        title_score = 0
        if query_folded == title_folded:
            title_score += 160
        elif query_folded in title_folded:
            title_score += 60
        if is_command:
            if command_root_folded and command_root_folded in title_folded:
                title_score += 90
            if "reference" in title_folded:
                title_score += 130
            if "what's new" in title_folded or "what's changed" in title_folded:
                title_score -= 180

        remaining_terms = tuple(term for term in terms if term not in title_folded)
        best_score = 0
        best_section_folded = ""
        best_section: _IndexedSection | None = None

        for section in _get_sections(entry):
            heading_folded = section.heading_folded
            block_folded = section.block_folded
            if remaining_terms and any(
                term not in heading_folded and term not in block_folded
                for term in remaining_terms
            ):
                continue

            score = title_score
            if query_folded == heading_folded:
                score += 150
            elif query_folded in heading_folded:
                score += 85
            if exact_re is not None and exact_re.search(section.block) is not None:
                score += 120
            if query_folded in block_folded:
                score += 45
            for term in terms:
                if term in heading_folded:
                    heading_count = heading_folded.count(term)
                    score += 12 * (2 if heading_count >= 2 else heading_count)
                if term in block_folded:
                    block_count = block_folded.count(term)
                    score += 3 * (2 if block_count >= 2 else block_count)
            if score <= 0:
                continue

            section_folded = heading_folded or title_folded
            if best_section is None or score > best_score or (
                score == best_score and section_folded < best_section_folded
            ):
                best_score = score
                best_section_folded = section_folded
                best_section = section

        if best_section is not None:
            best_per_document.append(
                (
                    best_score,
                    entry.document_id.casefold(),
                    best_section_folded,
                    entry,
                    best_section,
                )
            )

    best_per_document.sort(key=lambda item: (-item[0], item[1], item[2]))
    top_results = [
        {
            "document_id": entry.document_id,
            "collection": entry.collection,
            "knowledge_tier": entry.knowledge_tier,
            "file": entry.file_stem,
            "title": entry.title,
            "section": section.heading or entry.title,
            "score": score,
            "excerpt": _excerpt(section.block, cleaned_query),
            "read_offset": section.read_offset,
            "source_path": entry.source_path,
        }
        for score, _doc_id_folded, _section_folded, entry, section in best_per_document[:result_limit]
    ]
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
    index = _get_index()
    document = _resolve_document_record(index, document_id)
    if document is None:
        raise ValueError(f"Unknown document_id: {document_id}")
    limit = _bounded_limit(
        max_chars, label="max_chars", default=_DEFAULT_MAX_CHARS, maximum=_MAX_READ_CHARS
    )
    entry = _get_entry(index, document)
    text = entry.text
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
        "knowledge_tier": _knowledge_tier(document),
        "title": entry.title,
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


__all__ = ["clear_cache", "read_document", "search"]
