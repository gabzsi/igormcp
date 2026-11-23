"""Standalone Markdown knowledge retrieval for the Igor Pro MCP package.

The module deliberately depends only on the Python standard library.  Documents
are enumerated by ``knowledge/manifest.json`` rather than by walking arbitrary
paths supplied by a caller.  This keeps document identifiers stable and makes
``read_document`` safe to expose through an MCP tool.
"""

from __future__ import annotations

from array import array
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
import hashlib
import json
import os
import re
import stat
import sys
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
_MAX_CACHED_DOCS = 64
_MAX_CACHED_BYTES = 1_000_000
_KNOWLEDGE_TIERS = frozenset({"core-reference", "on-demand"})
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_PARAGRAPH_SEPARATOR_RE = re.compile(r"(?:\r?\n)[ \t]*(?:\r?\n)+")
_COMMAND_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:/[A-Za-z0-9_.,=+*?()/-]+)?$"
)
_COMMAND_ROOT_RE = re.compile(r"[A-Za-z]+")
_TERM_RE = re.compile(r"[A-Za-z0-9_]+")
_POSTING_TOKEN_RE = re.compile(r"[a-z0-9_]{2,}")


class _IndexedSection(NamedTuple):
    heading: str
    heading_folded: str
    block: str
    block_folded: str
    read_offset: int


class _DocumentMeta(NamedTuple):
    doc_idx: int
    document_id: str
    document_id_folded: str
    path: str
    file_sig: tuple[int, int]
    expected_hash: str
    collection: Any
    knowledge_tier: str
    file_stem: str
    title: str
    title_folded: str
    source_path: Any
    topic_haystack: str


@dataclass(slots=True)
class _IndexedDocument:
    document_id: str
    path: str
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
        self._doc_count = len(documents)
        self._posting_typecode = "H" if self._doc_count <= 0xFFFF else "I"
        self._postings_shift = max(20, (self._doc_count + 1).bit_length())
        self._metas: list[_DocumentMeta | None] = [None] * self._doc_count
        self._postings_flat: array = array(self._posting_typecode)
        self._postings_spans: dict[str, int] = {}
        self._heading_postings_flat: array = array(self._posting_typecode)
        self._heading_postings_spans: dict[str, int] = {}
        self._updated_doc_indices: set[int] = set()
        self._collection_indices: dict[str, tuple[int, ...]] = {
            "all": tuple(range(self._doc_count)),
            "manuals": tuple(
                idx for idx, doc in enumerate(documents) if doc.get("collection") == "manuals"
            ),
            "guides": tuple(
                idx for idx, doc in enumerate(documents) if doc.get("collection") == "guides"
            ),
        }
        self._collection_sets: dict[str, frozenset[int]] = {
            "manuals": frozenset(self._collection_indices["manuals"]),
            "guides": frozenset(self._collection_indices["guides"]),
        }
        self.entries: OrderedDict[str, _IndexedDocument] = OrderedDict()
        self._cached_bytes = 0
        self.stats_hits = 0
        self.stats_misses = 0

    def normalize_collection(self, collection: object) -> str:
        if collection is None:
            return "all"
        if not isinstance(collection, str):
            raise ValueError("collection must be 'manuals', 'guides', or 'all'")
        normalized = collection.strip().lower()
        if normalized == "all":
            return "all"
        aliases = {"official": "manuals", "curated": "guides"}
        normalized = aliases.get(normalized, normalized)
        if normalized not in {"manuals", "guides"}:
            raise ValueError("collection must be 'manuals', 'guides', or 'all'")
        return normalized

    def collection_documents(self, collection: object) -> tuple[dict[str, Any], ...]:
        normalized = self.normalize_collection(collection)
        if normalized == "all":
            return self.documents
        return tuple(self.documents[idx] for idx in self._collection_indices[normalized])


_INDEX_LOCK = threading.RLock()
_INDEX_CACHE: dict[Path, _KnowledgeIndex] = {}


def _stat_signature(path: Path | str) -> tuple[int, int]:
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


def _expected_hash(document: dict[str, Any]) -> str:
    raw_expected_hash = document.get("bundle_sha256")
    return raw_expected_hash.upper() if isinstance(raw_expected_hash, str) and raw_expected_hash else ""


def _load_document_payload(
    index: _KnowledgeIndex,
    document: dict[str, Any],
) -> tuple[str, tuple[int, int], str, str, str]:
    document_id = str(document["id"])
    expected_hash = _expected_hash(document)
    path = _safe_document_path(document_id, document, root=index.root)
    path_str = os.fspath(path)
    file_sig = _stat_signature(path_str)
    raw = path.read_bytes()
    if expected_hash:
        actual_hash = hashlib.sha256(raw).hexdigest().upper()
        if actual_hash != expected_hash:
            raise RuntimeError(f"Bundled knowledge hash mismatch: {document_id}")
    text = raw.decode("utf-8")
    title = _document_title(document, text)
    return path_str, file_sig, expected_hash, text, title


def _build_entry_and_meta(
    doc_idx: int,
    document: dict[str, Any],
    path_str: str,
    file_sig: tuple[int, int],
    expected_hash: str,
    text: str,
    title: str,
) -> tuple[_DocumentMeta, _IndexedDocument]:
    document_id = str(document["id"])
    title_folded = title.casefold()
    source_path = document.get("source_path")
    collection = document.get("collection")
    tier = _knowledge_tier(document)
    file_stem = Path(str(document["path"])).stem
    topic_haystack = (document_id + " " + title + " " + str(source_path or "")).casefold()
    meta = _DocumentMeta(
        doc_idx=doc_idx,
        document_id=document_id,
        document_id_folded=document_id.casefold(),
        path=path_str,
        file_sig=file_sig,
        expected_hash=expected_hash,
        collection=collection,
        knowledge_tier=tier,
        file_stem=file_stem,
        title=title,
        title_folded=title_folded,
        source_path=source_path,
        topic_haystack=topic_haystack,
    )
    entry = _IndexedDocument(
        document_id=document_id,
        path=path_str,
        file_sig=file_sig,
        expected_hash=expected_hash,
        collection=collection,
        knowledge_tier=tier,
        file_stem=file_stem,
        title=title,
        title_folded=title_folded,
        source_path=source_path,
        topic_haystack=topic_haystack,
        text=text,
    )
    return meta, entry


def _put_entry_locked(index: _KnowledgeIndex, entry: _IndexedDocument) -> None:
    existing = index.entries.pop(entry.document_id, None)
    if existing is not None:
        index._cached_bytes -= sys.getsizeof(existing.text)
    index.entries[entry.document_id] = entry
    index._cached_bytes += sys.getsizeof(entry.text)
    while len(index.entries) > _MAX_CACHED_DOCS or (
        len(index.entries) > 1 and index._cached_bytes > _MAX_CACHED_BYTES
    ):
        _evicted_id, evicted = index.entries.popitem(last=False)
        index._cached_bytes -= sys.getsizeof(evicted.text)


def _merge_postings_arrays(
    old_flat: array,
    old_spans: dict[str, int],
    new_postings: dict[str, array],
    *,
    typecode: str,
    shift: int,
) -> tuple[array, dict[str, int]]:
    if not new_postings:
        return old_flat, old_spans
    if not old_spans:
        flat = array(typecode)
        spans: dict[str, int] = {}
        for tok in list(new_postings.keys()):
            docs = new_postings.pop(tok)
            start = len(flat)
            flat.extend(docs)
            spans[tok] = (start << shift) | len(docs)
        return flat, spans

    mask = (1 << shift) - 1
    merged_flat = array(typecode)
    merged_spans: dict[str, int] = {}
    for tok, packed in old_spans.items():
        start = packed >> shift
        count = packed & mask
        new_start = len(merged_flat)
        merged_flat.extend(old_flat[start : start + count])
        extra = new_postings.pop(tok, None)
        if extra is not None:
            merged_flat.extend(extra)
            count += len(extra)
        merged_spans[tok] = (new_start << shift) | count
    for tok in list(new_postings.keys()):
        docs = new_postings.pop(tok)
        new_start = len(merged_flat)
        merged_flat.extend(docs)
        merged_spans[tok] = (new_start << shift) | len(docs)
    return merged_flat, merged_spans


def _ensure_indexed(index: _KnowledgeIndex, doc_indices: tuple[int, ...]) -> set[int]:
    """Ensure metadata and posting index are built and fresh for ``doc_indices``."""

    loaded_this_call: set[int] = set()
    if not doc_indices:
        return loaded_this_call

    with _INDEX_LOCK:
        metas = index._metas
        unindexed: list[int] = []
        stale: list[int] = []
        for idx in doc_indices:
            meta = metas[idx]
            if meta is None:
                unindexed.append(idx)
                continue
            try:
                current_sig = _stat_signature(meta.path)
            except OSError:
                current_sig = None
            if current_sig != meta.file_sig:
                stale.append(idx)

        if unindexed:
            typecode = index._posting_typecode
            new_postings: dict[str, array] = defaultdict(lambda: array(typecode))
            new_heading_postings: dict[str, array] = defaultdict(lambda: array(typecode))
            cache_during_scan = len(unindexed) <= _MAX_CACHED_DOCS
            for idx in unindexed:
                document = index.documents[idx]
                path_str, file_sig, expected_hash, text, title = _load_document_payload(
                    index, document
                )
                meta, entry = _build_entry_and_meta(
                    idx, document, path_str, file_sig, expected_hash, text, title
                )
                metas[idx] = meta
                folded = meta.title_folded + " " + text.casefold()
                for tok in set(_POSTING_TOKEN_RE.findall(folded)):
                    new_postings[tok].append(idx)
                heading_parts: list[str] = []
                for line in text.splitlines():
                    match = _HEADING_RE.match(line.strip())
                    if match:
                        heading_parts.append(match.group(2).strip())
                if heading_parts:
                    headings_folded = " ".join(heading_parts).casefold()
                    for tok in set(_POSTING_TOKEN_RE.findall(headings_folded)):
                        new_heading_postings[tok].append(idx)
                index.stats_misses += 1
                if cache_during_scan:
                    _put_entry_locked(index, entry)
                    loaded_this_call.add(idx)
            index._postings_flat, index._postings_spans = _merge_postings_arrays(
                index._postings_flat,
                index._postings_spans,
                new_postings,
                typecode=typecode,
                shift=index._postings_shift,
            )
            index._heading_postings_flat, index._heading_postings_spans = _merge_postings_arrays(
                index._heading_postings_flat,
                index._heading_postings_spans,
                new_heading_postings,
                typecode=typecode,
                shift=index._postings_shift,
            )

        for idx in stale:
            document = index.documents[idx]
            path_str, file_sig, expected_hash, text, title = _load_document_payload(index, document)
            meta, entry = _build_entry_and_meta(
                idx, document, path_str, file_sig, expected_hash, text, title
            )
            metas[idx] = meta
            index._updated_doc_indices.add(idx)
            _put_entry_locked(index, entry)
            index.stats_misses += 1
            loaded_this_call.add(idx)

    return loaded_this_call


def _get_entry(
    index: _KnowledgeIndex,
    document: dict[str, Any],
    *,
    count_hit: bool = True,
) -> _IndexedDocument:
    document_id = str(document["id"])
    expected_hash = _expected_hash(document)

    with _INDEX_LOCK:
        cached = index.entries.get(document_id)

    if cached is not None and cached.expected_hash == expected_hash:
        try:
            current_sig = _stat_signature(cached.path)
        except OSError:
            current_sig = None
        if current_sig == cached.file_sig:
            with _INDEX_LOCK:
                if document_id in index.entries:
                    index.entries.move_to_end(document_id)
                if count_hit:
                    index.stats_hits += 1
            return cached

    path, file_sig, expected_hash, text, title = _load_document_payload(index, document)
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
    )
    with _INDEX_LOCK:
        _put_entry_locked(index, entry)
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


def _postings_lookup_locked(
    spans: dict[str, int],
    flat: array,
    shift: int,
    term: str,
) -> set[int]:
    mask = (1 << shift) - 1
    docs: set[int] = set()
    for tok, packed in spans.items():
        if term in tok:
            start = packed >> shift
            count = packed & mask
            docs.update(flat[start : start + count])
    return docs


def _candidate_doc_indices(
    index: _KnowledgeIndex,
    canonical_collection: str,
    terms: tuple[str, ...],
) -> tuple[tuple[int, ...] | set[int], dict[str, set[int]]]:
    """Return ``(candidate_indices, heading_docs_by_term)`` for ``terms``."""

    if not terms:
        return index._collection_indices[canonical_collection], {}

    unique_terms = tuple(dict.fromkeys(terms))
    with _INDEX_LOCK:
        spans = index._postings_spans
        flat = index._postings_flat
        h_spans = index._heading_postings_spans
        h_flat = index._heading_postings_flat
        shift = index._postings_shift
        updated = set(index._updated_doc_indices)

        matched: set[int] | None = None
        for term in unique_terms:
            term_docs = _postings_lookup_locked(spans, flat, shift, term)
            if matched is None:
                matched = term_docs
            else:
                matched &= term_docs
            if not matched:
                break

        heading_docs_by_term: dict[str, set[int]] = {}
        if matched or updated:
            for term in unique_terms:
                heading_docs_by_term[term] = _postings_lookup_locked(h_spans, h_flat, shift, term)

    result: set[int] = matched if matched is not None else set()
    if updated:
        result |= updated
    if canonical_collection != "all":
        result &= index._collection_sets[canonical_collection]
    return result, heading_docs_by_term


def _best_section_in_text(
    text: str,
    *,
    title_folded: str,
    title_score: int,
    query_folded: str,
    terms: tuple[str, ...],
    remaining_terms: tuple[str, ...],
    exact_re: re.Pattern[str] | None,
) -> tuple[int, str, str, str, int] | None:
    """Find the highest-scoring paragraph in ``text`` without allocating all sections."""

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

    best_score = 0
    best_section_folded = ""
    best_heading = ""
    best_block: str | None = None
    best_read_offset = 0

    for section_heading, start, end in blocks:
        heading_folded = section_heading.casefold()
        section_text = text[start:end]
        block_terms_needed = tuple(term for term in remaining_terms if term not in heading_folded)
        if block_terms_needed:
            section_text_folded = section_text.casefold()
            if any(term not in section_text_folded for term in block_terms_needed):
                continue

        heading_score = title_score
        if query_folded == heading_folded:
            heading_score += 150
        elif query_folded in heading_folded:
            heading_score += 85
        for term in terms:
            if term in heading_folded:
                heading_count = heading_folded.count(term)
                heading_score += 12 * (2 if heading_count >= 2 else heading_count)

        section_folded = heading_folded or title_folded
        paragraph_start = 0
        for separator in _PARAGRAPH_SEPARATOR_RE.finditer(section_text):
            paragraph = section_text[paragraph_start:separator.start()]
            trimmed = paragraph.strip()
            if trimmed:
                block_folded = trimmed.casefold()
                if not block_terms_needed or not any(
                    term not in block_folded for term in block_terms_needed
                ):
                    score = heading_score
                    if exact_re is not None and exact_re.search(trimmed) is not None:
                        score += 120
                    if query_folded in block_folded:
                        score += 45
                    for term in terms:
                        if term in block_folded:
                            block_count = block_folded.count(term)
                            score += 3 * (2 if block_count >= 2 else block_count)
                    if score > 0 and (
                        best_block is None
                        or score > best_score
                        or (score == best_score and section_folded < best_section_folded)
                    ):
                        leading_whitespace = len(paragraph) - len(paragraph.lstrip())
                        best_score = score
                        best_section_folded = section_folded
                        best_heading = section_heading
                        best_block = trimmed
                        best_read_offset = start + paragraph_start + leading_whitespace
            paragraph_start = separator.end()

        paragraph = section_text[paragraph_start:]
        trimmed = paragraph.strip()
        if trimmed:
            block_folded = trimmed.casefold()
            if not block_terms_needed or not any(
                term not in block_folded for term in block_terms_needed
            ):
                score = heading_score
                if exact_re is not None and exact_re.search(trimmed) is not None:
                    score += 120
                if query_folded in block_folded:
                    score += 45
                for term in terms:
                    if term in block_folded:
                        block_count = block_folded.count(term)
                        score += 3 * (2 if block_count >= 2 else block_count)
                if score > 0 and (
                    best_block is None
                    or score > best_score
                    or (score == best_score and section_folded < best_section_folded)
                ):
                    leading_whitespace = len(paragraph) - len(paragraph.lstrip())
                    best_score = score
                    best_section_folded = section_folded
                    best_heading = section_heading
                    best_block = trimmed
                    best_read_offset = start + paragraph_start + leading_whitespace

    if best_block is None:
        return None
    return (best_score, best_section_folded, best_heading, best_block, best_read_offset)


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
        canonical_collection = index.normalize_collection(collection)
    except ValueError as exc:
        return {
            "query": query if isinstance(query, str) else "",
            "topic": topic if isinstance(topic, str) else None,
            "collection": collection if isinstance(collection, str) else "all",
            "results_count": 0,
            "error": str(exc),
            "results": [],
        }

    doc_indices = index._collection_indices[canonical_collection]
    loaded_this_call = _ensure_indexed(index, doc_indices)

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

    candidate_indices, heading_docs_by_term = _candidate_doc_indices(
        index, canonical_collection, terms
    )
    metas = index._metas
    updated_indices = index._updated_doc_indices
    max_block_bonus = (120 if exact_re is not None else 0) + 45 + 6 * len(terms)
    full_heading_bonus = 150 + 24 * len(terms)

    scored_candidates: list[tuple[int, int, str, int]] = []
    for doc_idx in candidate_indices:
        meta = metas[doc_idx]
        if meta is None:
            continue
        if topic_folded and topic_folded not in meta.topic_haystack:
            continue

        title_folded = meta.title_folded
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

        if not terms or doc_idx in updated_indices:
            heading_bonus = full_heading_bonus
        else:
            all_terms_in_heading = True
            heading_term_bonus = 0
            for term in terms:
                if doc_idx in heading_docs_by_term[term]:
                    heading_term_bonus += 24
                else:
                    all_terms_in_heading = False
            heading_bonus = (150 if all_terms_in_heading else 0) + heading_term_bonus

        max_possible_score = title_score + heading_bonus + max_block_bonus
        if max_possible_score <= 0:
            continue
        scored_candidates.append((max_possible_score, title_score, meta.document_id_folded, doc_idx))

    scored_candidates.sort(key=lambda item: (-item[0], item[2]))

    best_per_document: list[tuple[int, str, str, _IndexedDocument, str, str, int]] = []
    top_keys: list[tuple[int, str]] = []

    for max_possible_score, title_score, doc_id_folded, doc_idx in scored_candidates:
        if len(top_keys) >= result_limit:
            kth_score, kth_doc_id = top_keys[-1]
            if max_possible_score < kth_score or (
                max_possible_score == kth_score and doc_id_folded > kth_doc_id
            ):
                break

        document = index.documents[doc_idx]
        entry = _get_entry(index, document, count_hit=(doc_idx not in loaded_this_call))

        if doc_idx in updated_indices or not terms:
            full_folded = entry.title_folded + " " + entry.text.casefold()
            if terms:
                if any(term not in full_folded for term in terms):
                    continue
            elif not is_command and query_folded not in full_folded:
                continue

        title_folded = entry.title_folded
        remaining_terms = tuple(term for term in terms if term not in title_folded)
        match_result = _best_section_in_text(
            entry.text,
            title_folded=title_folded,
            title_score=title_score,
            query_folded=query_folded,
            terms=terms,
            remaining_terms=remaining_terms,
            exact_re=exact_re,
        )
        if match_result is not None:
            best_score, best_section_folded, best_heading, best_block, best_read_offset = (
                match_result
            )
            best_per_document.append(
                (
                    best_score,
                    doc_id_folded,
                    best_section_folded,
                    entry,
                    best_heading,
                    best_block,
                    best_read_offset,
                )
            )
            top_keys.append((best_score, doc_id_folded))
            top_keys.sort(key=lambda item: (-item[0], item[1]))
            if len(top_keys) > result_limit:
                top_keys.pop()

    best_per_document.sort(key=lambda item: (-item[0], item[1], item[2]))
    top_results = [
        {
            "document_id": entry.document_id,
            "collection": entry.collection,
            "knowledge_tier": entry.knowledge_tier,
            "file": entry.file_stem,
            "title": entry.title,
            "section": best_heading or entry.title,
            "score": score,
            "excerpt": _excerpt(best_block, cleaned_query),
            "read_offset": best_read_offset,
            "source_path": entry.source_path,
        }
        for (
            score,
            _doc_id_folded,
            _section_folded,
            entry,
            best_heading,
            best_block,
            best_read_offset,
        ) in best_per_document[:result_limit]
    ]
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
