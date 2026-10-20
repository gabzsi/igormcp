"""Bounded local session summaries and automatic MCP tool-call memory."""

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import uuid
from typing import Any


_MAX_SUMMARY_BYTES = 100_000
_MAX_SUMMARY_FILES = 1_000
_MAX_SUMMARY_TOTAL_BYTES = 50_000_000
_MAX_QUERY_CHARS = 4_096
_MAX_RESULTS = 20
_MAX_READ_CHARS = 50_000
_MAX_TOOL_LOG_FILES = 64
_MAX_TOOL_LOG_FILE_BYTES = 1_000_000
_MAX_TOOL_LOG_RECORD_BYTES = 16_384
_MAX_LOG_STRING_CHARS = 1_200
_MAX_LOG_DEPTH = 5
_MAX_LOG_MAPPING_ITEMS = 24
_MAX_LOG_SEQUENCE_PREVIEW = 3
_MAX_SEARCH_EXCERPT_CHARS = 600
_RECORD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SENSITIVE_KEY_RE = re.compile(
    r"(?:password|secret|token|authorization|credential|api[_-]?key)", re.IGNORECASE
)
_BULK_DATA_KEYS = frozenset(
    {"data", "wave_data", "values", "points", "samples", "matrix", "array"}
)
_WRITE_LOCK = threading.Lock()
_PROCESS_LOG_ID = uuid.uuid4().hex[:12]
_PROCESS_LOG_STARTED = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
_PROCESS_LOG_SEGMENT = 1


def _session_directory(*, create: bool = False) -> Path:
    configured = os.environ.get("IGOR_SESSION_DIR")
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    directory = (
        Path(configured).expanduser().resolve()
        if configured
        else base / "igor-mcp" / "sessions"
    )
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory


def _clip_text(value: str, limit: int = _MAX_LOG_STRING_CHARS) -> str:
    if len(value) <= limit:
        return value
    omitted = len(value) - limit
    return f"{value[:limit]}… [truncated {omitted} characters]"


def _value_count(value: Any) -> int | None:
    try:
        return len(value)
    except (TypeError, OverflowError):
        return None


def _json_safe(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Convert tool arguments/results to bounded strict-JSON observations."""

    normalized_key = key.casefold()
    if _SENSITIVE_KEY_RE.search(key):
        return "[redacted]"
    if normalized_key in _BULK_DATA_KEYS:
        return {"omitted": "bulk data", "item_count": _value_count(value)}
    if depth >= _MAX_LOG_DEPTH:
        return {"omitted": "maximum nesting depth", "type": type(value).__name__}
    if isinstance(value, str):
        if normalized_key in {"path", "file_path", "source_path"}:
            return Path(value).name
        return _clip_text(value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Path):
        return value.name if normalized_key in {"path", "file_path", "source_path"} else str(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= _MAX_LOG_MAPPING_ITEMS:
                result["_omitted_keys"] = len(value) - index
                break
            safe_key = _clip_text(str(raw_key), 100)
            result[safe_key] = _json_safe(item, key=safe_key, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        sequence = list(value)
        return {
            "type": "array",
            "count": len(sequence),
            "items": [
                _json_safe(item, depth=depth + 1)
                for item in sequence[:_MAX_LOG_SEQUENCE_PREVIEW]
            ],
            "items_omitted": max(0, len(sequence) - _MAX_LOG_SEQUENCE_PREVIEW),
        }
    if hasattr(value, "model_dump"):
        try:
            return _json_safe(value.model_dump(mode="json"), key=key, depth=depth + 1)
        except Exception:
            pass
    return {"type": type(value).__name__, "repr": _clip_text(repr(value), 300)}


def _log_arguments(tool_name: str, arguments: dict[str, Any]) -> Any:
    safe = _json_safe(arguments, key="arguments")
    if tool_name == "capture_session" and isinstance(safe, dict):
        summary = arguments.get("summary")
        if isinstance(summary, str):
            safe["summary"] = {
                "omitted": "explicit summary is saved as a separate memory record",
                "characters": len(summary),
                "utf8_bytes": len(summary.encode("utf-8")),
            }
    return safe


def _log_result(tool_name: str, result: Any) -> Any:
    if tool_name == "search_session_memory" and isinstance(result, dict):
        rows = result.get("results")
        return {
            "query": result.get("query"),
            "record_type": result.get("record_type"),
            "results_count": result.get("results_count"),
            "results": [
                {
                    key: row.get(key)
                    for key in ("record_id", "record_type", "title", "timestamp_utc", "tool_name")
                    if key in row
                }
                for row in rows[:_MAX_RESULTS]
                if isinstance(row, dict)
            ]
            if isinstance(rows, list)
            else [],
        }
    if tool_name == "read_session_memory" and isinstance(result, dict):
        content = result.get("content")
        return {
            "record_id": result.get("record_id"),
            "record_type": result.get("record_type"),
            "content_characters": len(content) if isinstance(content, str) else None,
            "truncated": result.get("truncated"),
        }
    if tool_name == "capture_session" and isinstance(result, dict):
        return {
            "ok": result.get("ok"),
            "title": result.get("title"),
            "session_id": result.get("session_id"),
        }
    return _json_safe(result, key="result")


def _bounded_event_json(event: dict[str, Any]) -> bytes:
    """Serialize a complete JSON object, shrinking values before any clipping."""

    original_arguments = event["arguments"]
    original_result = event.get("result")
    original_error = event.get("error")
    initial = json.dumps(
        event, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    if len(initial) <= _MAX_TOOL_LOG_RECORD_BYTES:
        return initial
    for string_limit, mapping_limit, sequence_limit in (
        (600, 16, 2),
        (250, 8, 1),
        (80, 4, 0),
    ):
        candidate = dict(event)
        candidate["arguments"] = _shrink_json(
            original_arguments,
            string_limit=string_limit,
            mapping_limit=mapping_limit,
            sequence_limit=sequence_limit,
        )
        candidate["result"] = _shrink_json(
            original_result,
            string_limit=string_limit,
            mapping_limit=mapping_limit,
            sequence_limit=sequence_limit,
        )
        candidate["error"] = _shrink_json(
            original_error,
            string_limit=string_limit,
            mapping_limit=mapping_limit,
            sequence_limit=sequence_limit,
        )
        candidate["truncated"] = True
        encoded = json.dumps(
            candidate, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) <= _MAX_TOOL_LOG_RECORD_BYTES:
            return encoded

    fallback = {
        key: event.get(key)
        for key in (
            "record_type",
            "record_id",
            "timestamp_utc",
            "tool_name",
            "status",
            "is_error",
        )
    }
    fallback.update(
        {
            "arguments": {"omitted": "record size limit"},
            "result": {"omitted": "record size limit"},
            "error": _clip_text(str(original_error), 500) if original_error else None,
            "truncated": True,
        }
    )
    encoded = json.dumps(
        fallback, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > _MAX_TOOL_LOG_RECORD_BYTES:  # pragma: no cover - fixed fallback
        raise ValueError("bounded tool-call record exceeded its fixed JSON limit")
    return encoded


def _contains_truncation(value: Any) -> bool:
    if isinstance(value, str):
        return "[truncated " in value
    if isinstance(value, dict):
        return (
            "omitted" in value
            or bool(value.get("_omitted_keys"))
            or bool(value.get("items_omitted"))
            or any(_contains_truncation(item) for item in value.values())
        )
    if isinstance(value, list):
        return any(_contains_truncation(item) for item in value)
    return False


def _shrink_json(
    value: Any,
    *,
    string_limit: int,
    mapping_limit: int,
    sequence_limit: int,
    depth: int = 0,
) -> Any:
    if isinstance(value, str):
        return _clip_text(value, string_limit)
    if isinstance(value, dict):
        result = {
            key: _shrink_json(
                item,
                string_limit=string_limit,
                mapping_limit=mapping_limit,
                sequence_limit=sequence_limit,
                depth=depth + 1,
            )
            for key, item in list(value.items())[:mapping_limit]
        }
        if len(value) > mapping_limit:
            result["_omitted_keys"] = len(value) - mapping_limit
        return result
    if isinstance(value, list):
        return [
            _shrink_json(
                item,
                string_limit=string_limit,
                mapping_limit=mapping_limit,
                sequence_limit=sequence_limit,
                depth=depth + 1,
            )
            for item in value[:sequence_limit]
        ]
    if depth >= 6:
        return "[omitted: maximum nesting depth]"
    return value


def _safe_record_path(directory: Path, path: Path) -> Path | None:
    try:
        root = directory.resolve()
        resolved = path.resolve(strict=True)
        if resolved.parent != root or not resolved.is_file():
            return None
        return resolved
    except OSError:
        return None


def _summary_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.md"), key=lambda path: path.name, reverse=True)


def _tool_log_files(directory: Path) -> list[Path]:
    return sorted(
        directory.glob("tool-calls-*.jsonl"),
        key=lambda path: path.name,
        reverse=True,
    )[:_MAX_TOOL_LOG_FILES]


def capture_markdown_session(title: str, summary: str, *, source_path: str) -> dict:
    """Append one explicitly supplied summary, without replacing prior records."""

    if not isinstance(title, str) or not isinstance(summary, str):
        raise ValueError("title and summary must be strings")
    title = " ".join(title.split()) or "Session record"
    summary_bytes = summary.encode("utf-8")
    if len(title) > 200 or len(summary_bytes) > _MAX_SUMMARY_BYTES:
        raise ValueError(
            f"title must be at most 200 characters; summary at most {_MAX_SUMMARY_BYTES} UTF-8 bytes"
        )
    if not summary.strip():
        raise ValueError("summary is required")
    directory = _session_directory(create=True)
    timestamp = datetime.now(timezone.utc)
    identifier = uuid.uuid4().hex
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")[:60] or "session"
    target = directory / f"{timestamp:%Y%m%dT%H%M%SZ}-{slug}-{identifier[:12]}.md"
    digest = hashlib.sha256(Path(source_path).read_bytes()).hexdigest()
    content = (
        f"# {title}\n\n"
        f"- Recorded UTC: {timestamp.isoformat()}\n"
        f"- Session ID: {identifier}\n"
        f"- Server SHA-256: {digest}\n"
        "- Record type: explicit caller-supplied human/assistant summary\n\n"
        f"## Summary\n\n{summary.strip()}\n"
    )
    content_bytes = content.encode("utf-8")
    with _WRITE_LOCK:
        existing = _summary_files(directory)
        total_bytes = 0
        for path in existing:
            try:
                total_bytes += path.stat().st_size
            except OSError:
                continue
        if len(existing) >= _MAX_SUMMARY_FILES or total_bytes + len(content_bytes) > _MAX_SUMMARY_TOTAL_BYTES:
            raise ValueError(
                "session summary storage limit reached; archive old summaries before adding more"
            )
        with target.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    return {
        "ok": True,
        "title": title,
        "path": str(target),
        "session_id": identifier,
        "summary_source": "caller_supplied",
    }


def append_tool_call_record(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    result: Any = None,
    error: Any = None,
    is_error: bool = False,
    status: str = "returned",
) -> str:
    """Append a bounded observation of one MCP call and return its opaque ID."""

    global _PROCESS_LOG_SEGMENT
    if not isinstance(tool_name, str) or not tool_name:
        raise ValueError("tool_name must be a non-empty string")
    directory = _session_directory(create=True)
    record_id = uuid.uuid4().hex
    event: dict[str, Any] = {
        "record_type": "tool_call_observation",
        "observation_source": "MCP tool execution",
        "record_id": record_id,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "tool_name": _clip_text(tool_name, 100),
        "status": status,
        "is_error": bool(is_error),
        "arguments": _log_arguments(tool_name, arguments),
        "result": _log_result(tool_name, result) if status == "returned" else None,
        "error": _json_safe(error, key="error") if error is not None else None,
    }
    event["truncated"] = any(
        _contains_truncation(event.get(key))
        for key in ("arguments", "result", "error")
    )
    encoded = _bounded_event_json(event) + b"\n"
    with _WRITE_LOCK:
        log_path = directory / (
            f"tool-calls-{_PROCESS_LOG_STARTED}-{_PROCESS_LOG_ID}-"
            f"{_PROCESS_LOG_SEGMENT:03d}.jsonl"
        )
        try:
            current_size = log_path.stat().st_size
        except FileNotFoundError:
            current_size = 0
        if current_size + len(encoded) > _MAX_TOOL_LOG_FILE_BYTES:
            _PROCESS_LOG_SEGMENT += 1
            log_path = directory / (
                f"tool-calls-{_PROCESS_LOG_STARTED}-{_PROCESS_LOG_ID}-"
                f"{_PROCESS_LOG_SEGMENT:03d}.jsonl"
            )
            if len(list(directory.glob("tool-calls-*.jsonl"))) >= _MAX_TOOL_LOG_FILES:
                raise ValueError(
                    "automatic session memory storage limit reached; existing logs were preserved"
                )
        elif not log_path.exists() and len(list(directory.glob("tool-calls-*.jsonl"))) >= _MAX_TOOL_LOG_FILES:
            raise ValueError(
                "automatic session memory storage limit reached; existing logs were preserved"
            )
        if len(encoded) > _MAX_TOOL_LOG_FILE_BYTES:  # pragma: no cover - record cap is lower
            raise ValueError("tool-call record exceeds the log-file limit")
        with log_path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
    return record_id


def _read_summary(path: Path, directory: Path) -> dict[str, Any] | None:
    safe_path = _safe_record_path(directory, path)
    if safe_path is None:
        return None
    try:
        raw = safe_path.read_bytes()[: _MAX_SUMMARY_BYTES + 4_096]
        content = raw.decode("utf-8")
    except (OSError, UnicodeError):
        return None
    match = re.search(r"^- Session ID: ([0-9a-f]{32})$", content, re.MULTILINE)
    if not match:
        return None
    timestamp_match = re.search(r"^- Recorded UTC: (.+)$", content, re.MULTILINE)
    title_match = re.match(r"^#\s+(.+)$", content)
    return {
        "record_type": "summary",
        "record_id": match.group(1),
        "timestamp_utc": timestamp_match.group(1) if timestamp_match else "",
        "title": title_match.group(1).strip() if title_match else safe_path.stem,
        "content": content,
    }


def _iter_records(directory: Path, record_type: str):
    if record_type in {"all", "summary"}:
        for path in _summary_files(directory)[:_MAX_SUMMARY_FILES]:
            record = _read_summary(path, directory)
            if record is not None:
                yield record
    if record_type in {"all", "tool_call"}:
        for path in _tool_log_files(directory):
            safe_path = _safe_record_path(directory, path)
            if safe_path is None:
                continue
            try:
                if safe_path.stat().st_size > _MAX_TOOL_LOG_FILE_BYTES:
                    continue
                with safe_path.open("rb") as handle:
                    while True:
                        line = handle.readline(_MAX_TOOL_LOG_RECORD_BYTES + 2)
                        if not line:
                            break
                        if len(line) > _MAX_TOOL_LOG_RECORD_BYTES + 1 or not line.endswith(b"\n"):
                            while line and not line.endswith(b"\n"):
                                line = handle.readline(4_096)
                            continue
                        try:
                            record = json.loads(line)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        if (
                            isinstance(record, dict)
                            and record.get("record_type") == "tool_call_observation"
                            and isinstance(record.get("record_id"), str)
                            and _RECORD_ID_RE.fullmatch(record["record_id"])
                        ):
                            yield record
            except (OSError, UnicodeError):
                continue
def _record_text(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, allow_nan=False, sort_keys=True)


def _excerpt(text: str, query: str) -> str:
    compact = " ".join(text.split())
    position = compact.casefold().find(query.casefold())
    if position < 0:
        return _clip_text(compact, _MAX_SEARCH_EXCERPT_CHARS)
    start = max(0, position - 180)
    end = min(len(compact), start + _MAX_SEARCH_EXCERPT_CHARS)
    excerpt = compact[start:end]
    if start:
        excerpt = "…" + excerpt
    if end < len(compact):
        excerpt += "…"
    return excerpt


def search_session_memory(
    query: str, *, record_type: str = "all", max_results: int = 5
) -> dict[str, Any]:
    """Search local explicit summaries and bounded tool-call observations."""

    if not isinstance(query, str):
        raise ValueError("query must be a string")
    query = " ".join(query.split())
    if not query:
        raise ValueError("query must be non-empty")
    if len(query) > _MAX_QUERY_CHARS:
        raise ValueError(f"query exceeds {_MAX_QUERY_CHARS:,} characters")
    if record_type not in {"all", "summary", "tool_call"}:
        raise ValueError("record_type must be 'all', 'summary', or 'tool_call'")
    if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1:
        raise ValueError("max_results must be a positive integer")
    max_results = min(max_results, _MAX_RESULTS)

    directory = _session_directory(create=False)
    if not directory.is_dir():
        return {
            "query": query,
            "record_type": record_type,
            "results_count": 0,
            "error": None,
            "results": [],
        }
    matches = []
    for record in _iter_records(directory, record_type):
        searchable = _record_text(record)
        if query.casefold() not in searchable.casefold():
            continue
        matches.append(
            {
                "record_id": record["record_id"],
                "record_type": record["record_type"],
                "timestamp_utc": record.get("timestamp_utc", ""),
                "title": record.get("title", record.get("tool_name", "Tool call")),
                "tool_name": record.get("tool_name"),
                "excerpt": _excerpt(searchable, query),
            }
        )
    matches.sort(
        key=lambda item: (str(item.get("timestamp_utc", "")), item["record_id"]),
        reverse=True,
    )
    return {
        "query": query,
        "record_type": record_type,
        "results_count": min(len(matches), max_results),
        "error": None,
        "results": matches[:max_results],
    }


def read_session_memory(record_id: str, *, max_chars: int = 20_000) -> dict[str, Any]:
    """Read one stored record by opaque ID; arbitrary paths are never accepted."""

    if not isinstance(record_id, str) or not _RECORD_ID_RE.fullmatch(record_id):
        raise ValueError("record_id must be a 32-character lowercase hexadecimal ID")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")
    max_chars = min(max_chars, _MAX_READ_CHARS)
    directory = _session_directory(create=False)
    if not directory.is_dir():
        raise ValueError(f"Session memory record not found: {record_id}")
    for record in _iter_records(directory, "all"):
        if record.get("record_id") != record_id:
            continue
        record_type = str(record["record_type"])
        if record_type == "summary":
            content = str(record["content"])
        else:
            content = json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False)
        return {
            "record_id": record_id,
            "record_type": record_type,
            "title": record.get("title", record.get("tool_name", "Tool call")),
            "timestamp_utc": record.get("timestamp_utc", ""),
            "content": content[:max_chars],
            "truncated": len(content) > max_chars,
            "max_chars": max_chars,
        }
    raise ValueError(f"Session memory record not found: {record_id}")


__all__ = [
    "append_tool_call_record",
    "capture_markdown_session",
    "read_session_memory",
    "search_session_memory",
]
