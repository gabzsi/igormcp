"""Pure-Python Igor Pro workflow extraction and incremental persistence."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
from typing import Any

import igor_sessions


_MAX_PRAGMAS = 20
_MAX_DEFINITIONS = 100
_MAX_MENU_ITEMS = 50
_MAX_RGB_COLORS = 24
_MAX_PALETTE_RGB = 16
_MAX_FOLDER_PATTERNS = 100
_MAX_EXPERIMENTS = 200
_MAX_STYLE_OBSERVATIONS = 50
_MAX_USER_NOTES = 50
_MAX_NOTE_CHARS = 1_000
_MAX_SLUG_CHARS = 48
_MAX_MARKDOWN_BYTES = 90_000

_PROFILE_LOCK = threading.Lock()

_PRAGMA_RE = re.compile(r"^\s*#pragma\b.*$", re.IGNORECASE)
_INCLUDE_RE = re.compile(r'^\s*#include\s+(?:"([^"]+)"|<([^>]+)>)', re.IGNORECASE)
_MENU_START_RE = re.compile(r'^\s*Menu\s+"([^"]+)"(?:\s*,.*)?$', re.IGNORECASE)
_SUBMENU_START_RE = re.compile(r'^\s*SubMenu\s+"([^"]+)"(?:\s*,.*)?$', re.IGNORECASE)
_END_RE = re.compile(r"^\s*End\b", re.IGNORECASE)
_MENU_ITEM_RE = re.compile(r'^\s*"([^"]+)"')

_DEF_RE = re.compile(
    r"^\s*(?:(?:Static|ThreadSafe|Override)\s+)*"
    r"(Function(?:/[A-Za-z]+)?|Macro|Proc|Window)\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\s*\(|\s*:\s*[A-Za-z_])",
    re.IGNORECASE,
)

_MODIFY_GRAPH_RE = re.compile(r"^\s*ModifyGraph(?:/[A-Za-z0-9_=]+)*\s+(.+)$", re.IGNORECASE)
_LEGEND_CMD_RE = re.compile(r"^\s*Legend(?:\s|/|$)", re.IGNORECASE)
_TEXTBOX_CMD_RE = re.compile(r"^\s*TextBox(?:\s|/|$)", re.IGNORECASE)

_OPT_TARGET = r"(?:\([^)]*\))?"
_FONT_RE = re.compile(rf'\bfont{_OPT_TARGET}\s*=\s*"([^"]+)"', re.IGNORECASE)
_FSIZE_RE = re.compile(rf"\bfSize{_OPT_TARGET}\s*=\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
_TICK_RE = re.compile(rf"\btick{_OPT_TARGET}\s*=\s*([0-9]+)", re.IGNORECASE)
_MIRROR_RE = re.compile(rf"\bmirror{_OPT_TARGET}\s*=\s*([0-9]+)", re.IGNORECASE)
_STANDOFF_RE = re.compile(rf"\bstandoff{_OPT_TARGET}\s*=\s*([0-9]+)", re.IGNORECASE)
_AXTHICK_RE = re.compile(rf"\baxThick{_OPT_TARGET}\s*=\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
_WIDTH_RE = re.compile(rf"\bwidth{_OPT_TARGET}\s*=\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
_HEIGHT_RE = re.compile(rf"\bheight{_OPT_TARGET}\s*=\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
_GRID_RE = re.compile(rf"\bgrid{_OPT_TARGET}\s*=\s*([0-9]+)", re.IGNORECASE)
_LBLMARGIN_RE = re.compile(rf"\blblMargin{_OPT_TARGET}\s*=\s*(-?[0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
_LOG_AXIS_RE = re.compile(r"\blog\(\s*([^)]+?)\s*\)\s*=\s*1\b", re.IGNORECASE)
_MODE_RE = re.compile(rf"\bmode{_OPT_TARGET}\s*=\s*([0-9]+)", re.IGNORECASE)
_LSIZE_RE = re.compile(rf"\blsize{_OPT_TARGET}\s*=\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
_MARKER_RE = re.compile(rf"\bmarker{_OPT_TARGET}\s*=\s*([0-9]+)", re.IGNORECASE)
_RGB_RE = re.compile(
    rf"\brgb{_OPT_TARGET}\s*=\s*\(\s*([0-9]+)\s*,\s*([0-9]+)\s*,\s*([0-9]+)(?:\s*,\s*[0-9]+)?\s*\)",
    re.IGNORECASE,
)


def _append_unique(items: list[Any], value: Any, *, limit: int | None = None) -> None:
    if value in items:
        return
    if limit is not None and len(items) >= limit:
        return
    items.append(value)


def _strip_inline_comment(line: str) -> str:
    """Strip an Igor Pro `//` comment outside of double-quoted strings."""

    in_quotes = False
    escaped = False
    for index in range(len(line) - 1):
        char = line[index]
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_quotes:
            escaped = True
            continue
        if char == '"':
            in_quotes = not in_quotes
            continue
        if not in_quotes and char == "/" and line[index + 1] == "/":
            return line[:index].rstrip()
    return line.rstrip()


def _split_igor_commands(line: str) -> list[str]:
    """Split a single Igor line on semicolons outside double-quoted strings."""

    code = _strip_inline_comment(line)
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    escaped = False
    for char in code:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and in_quotes:
            current.append(char)
            escaped = True
            continue
        if char == '"':
            in_quotes = not in_quotes
            current.append(char)
            continue
        if char == ";" and not in_quotes:
            segment = "".join(current).strip()
            if segment:
                parts.append(segment)
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _parse_number(raw: str) -> int | float:
    value = float(raw)
    if value.is_integer():
        return int(value)
    return value


def _normalize_kind(raw_keyword: str) -> str:
    lowered = raw_keyword.casefold()
    if lowered.startswith("function"):
        return "Function"
    if lowered == "macro":
        return "Macro"
    if lowered == "proc":
        return "Proc"
    return "Window"


def _extract_preceding_doc(lines: list[str], index: int) -> str:
    comments: list[str] = []
    cursor = index - 1
    while cursor >= 0 and len(comments) < 2:
        stripped = lines[cursor].strip()
        if not stripped.startswith("//"):
            break
        text = stripped[2:].strip()
        if text:
            comments.append(text)
        cursor -= 1
    comments.reverse()
    return " ".join(comments)


def parse_procedure_outline(name: str, text: str) -> dict[str, Any]:
    """Parse an Igor Pro .ipf / Procedure window into a bounded structural outline."""

    if not isinstance(name, str):
        raise ValueError("name must be a string")
    if not isinstance(text, str):
        raise ValueError("text must be a string")

    lines = text.splitlines()
    pragmas: list[str] = []
    includes: list[str] = []
    menus: list[dict[str, Any]] = []
    definitions: list[dict[str, Any]] = []
    definitions_truncated = False

    current_menu: dict[str, Any] | None = None
    submenu_depth = 0

    for idx, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("//"):
            continue

        if current_menu is not None:
            if _SUBMENU_START_RE.match(raw_line):
                submenu_depth += 1
                continue
            if _END_RE.match(raw_line):
                if submenu_depth > 0:
                    submenu_depth -= 1
                else:
                    menus.append(current_menu)
                    current_menu = None
                continue
            item_match = _MENU_ITEM_RE.match(raw_line)
            if item_match:
                label = item_match.group(1).strip()
                if label and label != "-":
                    _append_unique(current_menu["items"], label, limit=_MAX_MENU_ITEMS)
            continue

        if _PRAGMA_RE.match(raw_line):
            _append_unique(pragmas, _strip_inline_comment(raw_line).strip(), limit=_MAX_PRAGMAS)
            continue

        include_match = _INCLUDE_RE.match(raw_line)
        if include_match:
            target = (include_match.group(1) or include_match.group(2) or "").strip()
            if target:
                _append_unique(includes, target)
            continue

        menu_match = _MENU_START_RE.match(raw_line)
        if menu_match:
            current_menu = {
                "menu": menu_match.group(1).strip(),
                "items": [],
            }
            submenu_depth = 0
            continue

        def_match = _DEF_RE.match(raw_line)
        if def_match:
            if len(definitions) >= _MAX_DEFINITIONS:
                definitions_truncated = True
                continue
            kind = _normalize_kind(def_match.group(1))
            def_name = def_match.group(2)
            signature = _strip_inline_comment(raw_line).strip()
            doc = _extract_preceding_doc(lines, idx)
            definitions.append(
                {
                    "kind": kind,
                    "name": def_name,
                    "signature": signature,
                    "line": idx + 1,
                    "doc": doc,
                }
            )

    if current_menu is not None:
        menus.append(current_menu)

    return {
        "name": name,
        "line_count": len(lines),
        "pragmas": pragmas,
        "includes": includes,
        "menus": menus,
        "definitions": definitions,
        "definitions_truncated": definitions_truncated,
    }


def parse_graph_style(window_name: str, recreation_text: str) -> dict[str, Any]:
    """Extract styling parameters from an Igor Pro WinRecreation(win, 0) script."""

    if not isinstance(window_name, str):
        raise ValueError("window_name must be a string")
    if not isinstance(recreation_text, str):
        raise ValueError("recreation_text must be a string")

    fonts: list[str] = []
    font_sizes: list[int | float] = []
    ticks: list[int] = []
    mirrors: list[int] = []
    standoffs: list[int] = []
    ax_thicks: list[float] = []
    widths: list[float] = []
    heights: list[float] = []
    grids: list[int] = []
    lbl_margins: list[float] = []
    log_axes: list[str] = []
    trace_modes: list[int] = []
    trace_lsizes: list[float] = []
    markers: list[int] = []
    rgb_colors: list[list[int]] = []
    has_legend = False
    has_textbox = False

    for raw_line in recreation_text.splitlines():
        for command in _split_igor_commands(raw_line):
            if _LEGEND_CMD_RE.match(command):
                has_legend = True
                continue
            if _TEXTBOX_CMD_RE.match(command):
                has_textbox = True
                continue

            mg_match = _MODIFY_GRAPH_RE.match(command)
            if not mg_match:
                continue
            body = mg_match.group(1)

            for match in _FONT_RE.finditer(body):
                font_name = match.group(1).strip()
                if font_name:
                    _append_unique(fonts, font_name)
            for match in _FSIZE_RE.finditer(body):
                _append_unique(font_sizes, _parse_number(match.group(1)))
            for match in _TICK_RE.finditer(body):
                _append_unique(ticks, int(match.group(1)))
            for match in _MIRROR_RE.finditer(body):
                _append_unique(mirrors, int(match.group(1)))
            for match in _STANDOFF_RE.finditer(body):
                _append_unique(standoffs, int(match.group(1)))
            for match in _AXTHICK_RE.finditer(body):
                _append_unique(ax_thicks, float(match.group(1)))
            for match in _WIDTH_RE.finditer(body):
                _append_unique(widths, float(match.group(1)))
            for match in _HEIGHT_RE.finditer(body):
                _append_unique(heights, float(match.group(1)))
            for match in _GRID_RE.finditer(body):
                _append_unique(grids, int(match.group(1)))
            for match in _LBLMARGIN_RE.finditer(body):
                _append_unique(lbl_margins, float(match.group(1)))
            for match in _LOG_AXIS_RE.finditer(body):
                axis_name = match.group(1).strip().strip("'\"")
                if axis_name:
                    _append_unique(log_axes, axis_name)
            for match in _MODE_RE.finditer(body):
                _append_unique(trace_modes, int(match.group(1)))
            for match in _LSIZE_RE.finditer(body):
                _append_unique(trace_lsizes, float(match.group(1)))
            for match in _MARKER_RE.finditer(body):
                _append_unique(markers, int(match.group(1)))
            for match in _RGB_RE.finditer(body):
                color = [int(match.group(1)), int(match.group(2)), int(match.group(3))]
                _append_unique(rgb_colors, color, limit=_MAX_RGB_COLORS)

    return {
        "window": window_name,
        "fonts": fonts,
        "font_sizes": font_sizes,
        "ticks": ticks,
        "mirrors": mirrors,
        "standoffs": standoffs,
        "ax_thicks": ax_thicks,
        "widths": widths,
        "heights": heights,
        "grids": grids,
        "lbl_margins": lbl_margins,
        "log_axes": log_axes,
        "trace_modes": trace_modes,
        "trace_lsizes": trace_lsizes,
        "markers": markers,
        "rgb_colors": rgb_colors,
        "has_legend": has_legend,
        "has_textbox": has_textbox,
    }


def _most_common(counter: Counter[Any]) -> Any:
    if not counter:
        return None
    return counter.most_common(1)[0][0]


def _distinct_by_frequency(counter: Counter[Any], *, limit: int | None = None) -> list[Any]:
    ordered = [item for item, _count in counter.most_common()]
    if limit is not None:
        return ordered[:limit]
    return ordered


def summarize_style_conventions(graph_styles: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate parsed graph styles into a compact consensus summary."""

    valid_styles = [style for style in graph_styles if isinstance(style, dict)]
    font_counter: Counter[str] = Counter()
    fsize_counter: Counter[int | float] = Counter()
    tick_counter: Counter[int] = Counter()
    mirror_counter: Counter[int] = Counter()
    standoff_counter: Counter[int] = Counter()
    ax_thick_counter: Counter[float] = Counter()
    width_counter: Counter[float] = Counter()
    height_counter: Counter[float] = Counter()
    lsize_counter: Counter[float] = Counter()
    marker_counter: Counter[int] = Counter()
    rgb_counter: Counter[tuple[int, int, int]] = Counter()
    uses_grid = False
    uses_legend = False

    for style in valid_styles:
        for font in style.get("fonts", ()):
            if isinstance(font, str) and font:
                font_counter[font] += 1
        for size in style.get("font_sizes", ()):
            if isinstance(size, (int, float)) and not isinstance(size, bool) and math.isfinite(size):
                fsize_counter[size] += 1
        for tick in style.get("ticks", ()):
            if isinstance(tick, int) and not isinstance(tick, bool):
                tick_counter[tick] += 1
        for mirror in style.get("mirrors", ()):
            if isinstance(mirror, int) and not isinstance(mirror, bool):
                mirror_counter[mirror] += 1
        for standoff in style.get("standoffs", ()):
            if isinstance(standoff, int) and not isinstance(standoff, bool):
                standoff_counter[standoff] += 1
        for ax_thick in style.get("ax_thicks", ()):
            if isinstance(ax_thick, (int, float)) and not isinstance(ax_thick, bool) and math.isfinite(ax_thick):
                ax_thick_counter[float(ax_thick)] += 1
        for width in style.get("widths", ()):
            if isinstance(width, (int, float)) and not isinstance(width, bool) and math.isfinite(width):
                width_counter[float(width)] += 1
        for height in style.get("heights", ()):
            if isinstance(height, (int, float)) and not isinstance(height, bool) and math.isfinite(height):
                height_counter[float(height)] += 1
        for lsize in style.get("trace_lsizes", ()):
            if isinstance(lsize, (int, float)) and not isinstance(lsize, bool) and math.isfinite(lsize):
                lsize_counter[float(lsize)] += 1
        for marker in style.get("markers", ()):
            if isinstance(marker, int) and not isinstance(marker, bool):
                marker_counter[marker] += 1
        for color in style.get("rgb_colors", ()):
            if (
                isinstance(color, (list, tuple))
                and len(color) == 3
                and all(isinstance(c, int) and not isinstance(c, bool) for c in color)
            ):
                rgb_counter[(color[0], color[1], color[2])] += 1
        if any(isinstance(g, int) and not isinstance(g, bool) and g > 0 for g in style.get("grids", ())):
            uses_grid = True
        if style.get("has_legend"):
            uses_legend = True

    palette_rgb = [list(rgb) for rgb in _distinct_by_frequency(rgb_counter, limit=_MAX_PALETTE_RGB)]

    return {
        "graphs_analyzed": len(valid_styles),
        "preferred_font": _most_common(font_counter),
        "preferred_font_size": _most_common(fsize_counter),
        "preferred_tick": _most_common(tick_counter),
        "preferred_mirror": _most_common(mirror_counter),
        "preferred_standoff": _most_common(standoff_counter),
        "preferred_ax_thick": _most_common(ax_thick_counter),
        "common_widths_pt": _distinct_by_frequency(width_counter),
        "common_heights_pt": _distinct_by_frequency(height_counter),
        "common_trace_lsizes": _distinct_by_frequency(lsize_counter),
        "common_markers": _distinct_by_frequency(marker_counter),
        "palette_rgb": palette_rgb,
        "uses_grid": uses_grid,
        "uses_legend": uses_legend,
    }


def _slugify_profile_name(profile_name: str) -> str:
    if not isinstance(profile_name, str):
        return "default"
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", profile_name.strip().lower())
    cleaned = cleaned.strip("-_")[:_MAX_SLUG_CHARS].strip("-_")
    return cleaned or "default"


def _profile_record_id(slug: str) -> str:
    return hashlib.sha256(f"workflow-profile:{slug}".encode("utf-8")).hexdigest()[:32]


def _sanitize_experiment_name(raw: Any) -> str | None:
    """Extract only the experiment filename/basename, never local directory paths."""

    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    segments = [part for part in re.split(r"[\\/]+", stripped) if part]
    if not segments:
        return None
    name = segments[-1].strip()
    if not name or name in {".", ".."}:
        return None
    return name[:160]


def _sanitize_folder_pattern(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    return cleaned[:200]


def _sanitize_style_observation(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    window = raw.get("window")
    window_name = str(window).strip()[:120] if isinstance(window, str) and window.strip() else "Graph"
    return {
        "window": window_name,
        "fonts": [f for f in raw.get("fonts", ()) if isinstance(f, str) and f][:16],
        "font_sizes": [
            s for s in raw.get("font_sizes", ())
            if isinstance(s, (int, float)) and not isinstance(s, bool) and math.isfinite(s)
        ][:16],
        "ticks": [
            t for t in raw.get("ticks", ()) if isinstance(t, int) and not isinstance(t, bool)
        ][:8],
        "mirrors": [
            m for m in raw.get("mirrors", ()) if isinstance(m, int) and not isinstance(m, bool)
        ][:8],
        "standoffs": [
            s for s in raw.get("standoffs", ()) if isinstance(s, int) and not isinstance(s, bool)
        ][:8],
        "ax_thicks": [
            float(a) for a in raw.get("ax_thicks", ())
            if isinstance(a, (int, float)) and not isinstance(a, bool) and math.isfinite(a)
        ][:8],
        "widths": [
            float(w) for w in raw.get("widths", ())
            if isinstance(w, (int, float)) and not isinstance(w, bool) and math.isfinite(w)
        ][:8],
        "heights": [
            float(h) for h in raw.get("heights", ())
            if isinstance(h, (int, float)) and not isinstance(h, bool) and math.isfinite(h)
        ][:8],
        "grids": [
            g for g in raw.get("grids", ()) if isinstance(g, int) and not isinstance(g, bool)
        ][:8],
        "lbl_margins": [
            float(m) for m in raw.get("lbl_margins", ())
            if isinstance(m, (int, float)) and not isinstance(m, bool) and math.isfinite(m)
        ][:8],
        "log_axes": [
            a for a in raw.get("log_axes", ()) if isinstance(a, str) and a
        ][:16],
        "trace_modes": [
            m for m in raw.get("trace_modes", ()) if isinstance(m, int) and not isinstance(m, bool)
        ][:16],
        "trace_lsizes": [
            float(s) for s in raw.get("trace_lsizes", ())
            if isinstance(s, (int, float)) and not isinstance(s, bool) and math.isfinite(s)
        ][:16],
        "markers": [
            m for m in raw.get("markers", ()) if isinstance(m, int) and not isinstance(m, bool)
        ][:16],
        "rgb_colors": [
            [int(c[0]), int(c[1]), int(c[2])]
            for c in raw.get("rgb_colors", ())
            if isinstance(c, (list, tuple))
            and len(c) == 3
            and all(isinstance(v, int) and not isinstance(v, bool) for v in c)
        ][:_MAX_RGB_COLORS],
        "has_legend": bool(raw.get("has_legend", False)),
        "has_textbox": bool(raw.get("has_textbox", False)),
    }


def _normalize_procedure_outline(name: str, outline: dict[str, Any]) -> dict[str, Any]:
    pragmas = [
        p.strip() for p in outline.get("pragmas", ()) if isinstance(p, str) and p.strip()
    ][:_MAX_PRAGMAS]
    includes: list[str] = []
    for inc in outline.get("includes", ()):
        if isinstance(inc, str) and inc.strip():
            _append_unique(includes, inc.strip())

    menus: list[dict[str, Any]] = []
    for menu in outline.get("menus", ()):
        if not isinstance(menu, dict):
            continue
        menu_name = menu.get("menu")
        if not isinstance(menu_name, str) or not menu_name.strip():
            continue
        items: list[str] = []
        for item in menu.get("items", ()):
            if isinstance(item, str) and item.strip():
                _append_unique(items, item.strip(), limit=_MAX_MENU_ITEMS)
        menus.append({"menu": menu_name.strip(), "items": items})

    definitions: list[dict[str, Any]] = []
    for defn in outline.get("definitions", ()):
        if not isinstance(defn, dict):
            continue
        d_kind = defn.get("kind")
        d_name = defn.get("name")
        if not isinstance(d_kind, str) or not isinstance(d_name, str) or not d_name.strip():
            continue
        definitions.append(
            {
                "kind": d_kind,
                "name": d_name.strip(),
                "signature": str(defn.get("signature", "")).strip()[:300],
                "line": int(defn["line"]) if isinstance(defn.get("line"), int) else 0,
                "doc": str(defn.get("doc", "")).strip()[:300],
            }
        )
        if len(definitions) >= _MAX_DEFINITIONS:
            break

    line_count = outline.get("line_count", 0)
    return {
        "name": name,
        "line_count": int(line_count) if isinstance(line_count, int) else 0,
        "pragmas": pragmas,
        "includes": includes,
        "menus": menus,
        "definitions": definitions,
        "definitions_truncated": bool(outline.get("definitions_truncated", False)),
    }


def _merge_procedure_outlines(
    existing: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    pragmas = list(existing.get("pragmas", ()))
    for pragma in incoming.get("pragmas", ()):
        _append_unique(pragmas, pragma, limit=_MAX_PRAGMAS)

    includes = list(existing.get("includes", ()))
    for inc in incoming.get("includes", ()):
        _append_unique(includes, inc)

    menus_by_name: dict[str, list[str]] = {}
    menu_order: list[str] = []
    for source in (existing.get("menus", ()), incoming.get("menus", ())):
        for menu in source:
            menu_name = menu["menu"]
            if menu_name not in menus_by_name:
                menus_by_name[menu_name] = []
                menu_order.append(menu_name)
            for item in menu.get("items", ()):
                _append_unique(menus_by_name[menu_name], item, limit=_MAX_MENU_ITEMS)
    merged_menus = [{"menu": m, "items": menus_by_name[m]} for m in menu_order]

    defs_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    def_order: list[tuple[str, str]] = []
    truncated = bool(existing.get("definitions_truncated", False)) or bool(
        incoming.get("definitions_truncated", False)
    )
    for source in (existing.get("definitions", ()), incoming.get("definitions", ())):
        for defn in source:
            key = (defn["kind"], defn["name"])
            if key not in defs_by_key:
                if len(def_order) >= _MAX_DEFINITIONS:
                    truncated = True
                    continue
                def_order.append(key)
            defs_by_key[key] = defn
    merged_defs = [defs_by_key[key] for key in def_order]

    return {
        "name": incoming["name"],
        "line_count": incoming.get("line_count", existing.get("line_count", 0)),
        "pragmas": pragmas,
        "includes": includes,
        "menus": merged_menus,
        "definitions": merged_defs,
        "definitions_truncated": truncated,
    }


def _extract_snapshot_procedures(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_procs = snapshot.get("procedures")
    result: dict[str, dict[str, Any]] = {}
    if isinstance(raw_procs, dict):
        for raw_name, raw_val in raw_procs.items():
            proc_name = _sanitize_experiment_name(str(raw_name)) or str(raw_name).strip()
            if not proc_name:
                continue
            if isinstance(raw_val, str):
                outline = parse_procedure_outline(proc_name, raw_val)
                result[proc_name] = _normalize_procedure_outline(proc_name, outline)
            elif isinstance(raw_val, dict):
                result[proc_name] = _normalize_procedure_outline(proc_name, raw_val)
    elif isinstance(raw_procs, list):
        for item in raw_procs:
            if not isinstance(item, dict):
                continue
            raw_name = item.get("name")
            if not isinstance(raw_name, str) or not raw_name.strip():
                continue
            proc_name = _sanitize_experiment_name(raw_name) or raw_name.strip()
            result[proc_name] = _normalize_procedure_outline(proc_name, item)
    return result


def _extract_snapshot_experiments(snapshot: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for key in ("experiment", "experiment_name"):
        val = snapshot.get(key)
        clean = _sanitize_experiment_name(val)
        if clean:
            _append_unique(names, clean)
    for key in ("experiments", "experiments_seen"):
        val = snapshot.get(key)
        if isinstance(val, (list, tuple)):
            for item in val:
                clean = _sanitize_experiment_name(item)
                if clean:
                    _append_unique(names, clean)
    return names


def _extract_snapshot_folders(snapshot: dict[str, Any]) -> list[str]:
    folders: list[str] = []
    for key in ("folder_patterns", "folders", "data_folders"):
        val = snapshot.get(key)
        if isinstance(val, (list, tuple)):
            for item in val:
                clean = _sanitize_folder_pattern(item)
                if clean:
                    _append_unique(folders, clean, limit=_MAX_FOLDER_PATTERNS)
    return folders


def _extract_snapshot_styles(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    styles: list[dict[str, Any]] = []
    for key in ("graph_styles", "style_observations", "styles"):
        val = snapshot.get(key)
        if isinstance(val, (list, tuple)):
            for item in val:
                clean = _sanitize_style_observation(item)
                if clean is not None:
                    styles.append(clean)
    return styles


def _render_profile_markdown(profile: dict[str, Any]) -> str:
    lines: list[str] = [
        f"# Workflow Profile: {profile['profile_name']}",
        "",
        f"- Recorded UTC: {profile['updated_at']}",
        f"- Session ID: {profile['record_id']}",
        f"- Profile Slug: {profile['slug']}",
        "- Record type: incremental workflow profile",
        f"- Observations: {profile['observations_count']}",
        f"- Experiments observed: {len(profile['experiments_seen'])}",
        "",
    ]

    experiments = profile.get("experiments_seen", [])
    if experiments:
        lines.append("## Experiments Observed")
        lines.append("")
        for exp in experiments:
            lines.append(f"- `{exp}`")
        lines.append("")

    folders = profile.get("folder_patterns", [])
    if folders:
        lines.append("## Data Folder Patterns")
        lines.append("")
        for folder in folders:
            lines.append(f"- `{folder}`")
        lines.append("")

    style = profile.get("style_defaults", {})
    if style and style.get("graphs_analyzed", 0) > 0:
        lines.append("## Graph Style Conventions")
        lines.append("")
        lines.append(f"- Graphs analyzed: {style['graphs_analyzed']}")
        if style.get("preferred_font") is not None:
            lines.append(f"- Preferred font: `{style['preferred_font']}`")
        if style.get("preferred_font_size") is not None:
            lines.append(f"- Preferred font size: `{style['preferred_font_size']}`")
        if style.get("preferred_tick") is not None:
            lines.append(f"- Preferred tick: `{style['preferred_tick']}`")
        if style.get("preferred_mirror") is not None:
            lines.append(f"- Preferred mirror: `{style['preferred_mirror']}`")
        if style.get("preferred_standoff") is not None:
            lines.append(f"- Preferred standoff: `{style['preferred_standoff']}`")
        if style.get("preferred_ax_thick") is not None:
            lines.append(f"- Preferred axis thickness: `{style['preferred_ax_thick']}`")
        if style.get("common_widths_pt"):
            lines.append(f"- Common widths (pt): {', '.join(str(v) for v in style['common_widths_pt'])}")
        if style.get("common_heights_pt"):
            lines.append(f"- Common heights (pt): {', '.join(str(v) for v in style['common_heights_pt'])}")
        if style.get("common_trace_lsizes"):
            lines.append(f"- Common trace line sizes: {', '.join(str(v) for v in style['common_trace_lsizes'])}")
        if style.get("common_markers"):
            lines.append(f"- Common markers: {', '.join(str(v) for v in style['common_markers'])}")
        lines.append(f"- Uses grid: {'yes' if style.get('uses_grid') else 'no'}")
        lines.append(f"- Uses legend: {'yes' if style.get('uses_legend') else 'no'}")
        if style.get("palette_rgb"):
            palette = ", ".join(f"({c[0]},{c[1]},{c[2]})" for c in style["palette_rgb"])
            lines.append(f"- Palette RGB (16-bit): {palette}")
        lines.append("")

    procedures = profile.get("procedures", {})
    if procedures:
        lines.append("## Procedures & Menus")
        lines.append("")
        for proc_name in sorted(procedures):
            proc = procedures[proc_name]
            lines.append(f"### `{proc_name}` ({proc.get('line_count', 0)} lines)")
            if proc.get("includes"):
                lines.append(f"- Includes: {', '.join(f'`{i}`' for i in proc['includes'])}")
            if proc.get("pragmas"):
                lines.append(f"- Pragmas: {', '.join(f'`{p}`' for p in proc['pragmas'][:5])}")
            for menu in proc.get("menus", []):
                items = ", ".join(menu.get("items", []))
                lines.append(f"- Menu `{menu['menu']}`: {items}")
            for defn in proc.get("definitions", [])[:30]:
                doc_suffix = f" — {defn['doc']}" if defn.get("doc") else ""
                lines.append(f"- `{defn['kind']} {defn['name']}`: `{defn['signature']}`{doc_suffix}")
            lines.append("")

    loaded_xops = profile.get("loaded_xops", [])
    if loaded_xops:
        lines.append("## Loaded XOPs (Hardware & Extensions)")
        lines.append("")
        lines.append(", ".join(f"`{xop}`" for xop in loaded_xops))
        lines.append("")

    notes = profile.get("user_notes", [])
    if notes:
        lines.append("## User Notes")
        lines.append("")
        for note in notes:
            lines.append(f"- {note}")
        lines.append("")

    content = "\n".join(lines).rstrip() + "\n"
    encoded = content.encode("utf-8")
    if len(encoded) > _MAX_MARKDOWN_BYTES:
        content = encoded[:_MAX_MARKDOWN_BYTES].decode("utf-8", errors="ignore").rstrip() + "\n\n... [truncated]\n"
    return content


def _atomic_write_text(path: Path, content: str) -> None:
    tmp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    os.replace(tmp_path, path)


def save_or_merge_workflow_profile(
    profile_name: str,
    snapshot: dict[str, Any],
    *,
    user_notes: str | None = None,
) -> dict[str, Any]:
    """Persist and incrementally merge workflow knowledge in IGOR_SESSION_DIR."""

    if not isinstance(profile_name, str):
        raise ValueError("profile_name must be a string")
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot must be a dict")
    if user_notes is not None and not isinstance(user_notes, str):
        raise ValueError("user_notes must be a string or None")

    slug = _slugify_profile_name(profile_name)
    clean_name = " ".join(profile_name.split())[:100] or slug
    now_iso = datetime.now(timezone.utc).isoformat()

    directory = igor_sessions._session_directory(create=True)
    json_path = directory / f"workflow-{slug}.json"
    md_path = directory / f"workflow-{slug}.md"

    with _PROFILE_LOCK:
        existed = False
        existing: dict[str, Any] = {}
        if json_path.is_file():
            try:
                loaded = json.loads(json_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
                    existed = True
            except (OSError, json.JSONDecodeError, UnicodeError):
                existing = {}
                existed = False

        record_id = existing.get("record_id")
        if not isinstance(record_id, str) or not re.fullmatch(r"[0-9a-f]{32}", record_id):
            record_id = _profile_record_id(slug)

        observations_count = int(existing.get("observations_count", 0)) + 1

        experiments_seen: list[str] = []
        for item in existing.get("experiments_seen", ()):
            clean = _sanitize_experiment_name(item)
            if clean:
                _append_unique(experiments_seen, clean, limit=_MAX_EXPERIMENTS)
        for item in _extract_snapshot_experiments(snapshot):
            _append_unique(experiments_seen, item, limit=_MAX_EXPERIMENTS)

        folder_patterns: list[str] = []
        for item in existing.get("folder_patterns", ()):
            clean = _sanitize_folder_pattern(item)
            if clean:
                _append_unique(folder_patterns, clean, limit=_MAX_FOLDER_PATTERNS)
        for item in _extract_snapshot_folders(snapshot):
            _append_unique(folder_patterns, item, limit=_MAX_FOLDER_PATTERNS)

        procedures: dict[str, dict[str, Any]] = {}
        raw_existing_procs = existing.get("procedures")
        if isinstance(raw_existing_procs, dict):
            for p_name, p_outline in raw_existing_procs.items():
                if isinstance(p_name, str) and isinstance(p_outline, dict):
                    procedures[p_name] = _normalize_procedure_outline(p_name, p_outline)
        for p_name, p_outline in _extract_snapshot_procedures(snapshot).items():
            if p_name in procedures:
                procedures[p_name] = _merge_procedure_outlines(procedures[p_name], p_outline)
            else:
                procedures[p_name] = p_outline

        style_observations: list[dict[str, Any]] = []
        for item in existing.get("style_observations", ()):
            clean = _sanitize_style_observation(item)
            if clean is not None:
                style_observations.append(clean)
        style_observations.extend(_extract_snapshot_styles(snapshot))
        if len(style_observations) > _MAX_STYLE_OBSERVATIONS:
            style_observations = style_observations[-_MAX_STYLE_OBSERVATIONS:]

        style_defaults = summarize_style_conventions(style_observations)

        notes: list[str] = []
        for item in existing.get("user_notes", ()):
            if isinstance(item, str) and item.strip():
                _append_unique(notes, item.strip()[:_MAX_NOTE_CHARS], limit=_MAX_USER_NOTES)
        snapshot_notes = snapshot.get("user_notes")
        if isinstance(snapshot_notes, str) and snapshot_notes.strip():
            _append_unique(notes, snapshot_notes.strip()[:_MAX_NOTE_CHARS], limit=_MAX_USER_NOTES)
        elif isinstance(snapshot_notes, (list, tuple)):
            for item in snapshot_notes:
                if isinstance(item, str) and item.strip():
                    _append_unique(notes, item.strip()[:_MAX_NOTE_CHARS], limit=_MAX_USER_NOTES)
        if user_notes is not None and user_notes.strip():
            _append_unique(notes, user_notes.strip()[:_MAX_NOTE_CHARS], limit=_MAX_USER_NOTES)

        loaded_xops: list[str] = []
        for item in existing.get("loaded_xops", ()):
            if isinstance(item, str) and item.strip():
                _append_unique(loaded_xops, item.strip()[:80], limit=50)
        raw_snapshot_xops = snapshot.get("loaded_xops")
        if isinstance(raw_snapshot_xops, (list, tuple)):
            for item in raw_snapshot_xops:
                if isinstance(item, str) and item.strip():
                    _append_unique(loaded_xops, item.strip()[:80], limit=50)

        merged: dict[str, Any] = {
            "profile_name": clean_name,
            "slug": slug,
            "record_id": record_id,
            "created_at": existing.get("created_at", now_iso),
            "updated_at": now_iso,
            "observations_count": observations_count,
            "experiments_seen": experiments_seen,
            "folder_patterns": folder_patterns,
            "loaded_xops": loaded_xops,
            "procedures": procedures,
            "style_observations": style_observations,
            "style_defaults": style_defaults,
            "user_notes": notes,
        }

        _atomic_write_text(
            json_path,
            json.dumps(merged, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        )
        _atomic_write_text(md_path, _render_profile_markdown(merged))

    return {
        "profile_name": clean_name,
        "slug": slug,
        "markdown_path": str(md_path),
        "json_path": str(json_path),
        "experiments_count": len(merged["experiments_seen"]),
        "observations_count": merged["observations_count"],
        "merged": existed,
        "style_defaults": merged["style_defaults"],
    }


def load_workflow_profile(profile_name: str) -> dict[str, Any] | None:
    """Load a persisted workflow profile by name or slug, or None if not found."""

    if not isinstance(profile_name, str) or not profile_name.strip():
        return None
    slug = _slugify_profile_name(profile_name)
    directory = igor_sessions._session_directory(create=False)
    if not directory.is_dir():
        return None
    json_path = directory / f"workflow-{slug}.json"
    safe_path = igor_sessions._safe_record_path(directory, json_path)
    if safe_path is None:
        return None
    try:
        data = json.loads(safe_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    md_path = directory / f"workflow-{slug}.md"
    data["json_path"] = str(safe_path)
    data["markdown_path"] = str(md_path)
    return data


def list_workflow_profiles() -> list[dict[str, Any]]:
    """List all persisted workflow profiles in IGOR_SESSION_DIR."""

    directory = igor_sessions._session_directory(create=False)
    if not directory.is_dir():
        return []
    profiles: list[dict[str, Any]] = []
    for path in sorted(directory.glob("workflow-*.json")):
        safe_path = igor_sessions._safe_record_path(directory, path)
        if safe_path is None:
            continue
        try:
            data = json.loads(safe_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        if not isinstance(data, dict):
            continue
        slug = str(data.get("slug") or safe_path.stem.removeprefix("workflow-"))
        md_path = directory / f"workflow-{slug}.md"
        experiments = data.get("experiments_seen", [])
        procedures = data.get("procedures", {})
        style_defaults = data.get("style_defaults", {})
        profiles.append(
            {
                "profile_name": str(data.get("profile_name") or slug),
                "slug": slug,
                "record_id": str(data.get("record_id", "")),
                "observations_count": int(data.get("observations_count", 0)),
                "experiments_count": len(experiments) if isinstance(experiments, list) else 0,
                "procedures_count": len(procedures) if isinstance(procedures, dict) else 0,
                "graphs_analyzed": int(style_defaults.get("graphs_analyzed", 0))
                if isinstance(style_defaults, dict)
                else 0,
                "updated_at": str(data.get("updated_at", "")),
                "markdown_path": str(md_path),
                "json_path": str(safe_path),
            }
        )
    profiles.sort(key=lambda item: (item["updated_at"], item["slug"]), reverse=True)
    return profiles


__all__ = [
    "list_workflow_profiles",
    "load_workflow_profile",
    "parse_graph_style",
    "parse_procedure_outline",
    "save_or_merge_workflow_profile",
    "summarize_style_conventions",
]
