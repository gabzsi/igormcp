---
type: concept
collection: guides
tags: [igor-pro, com, automation, windows, activex, mcp]
provenance: "Project-authored technical guide; consult WaveMetrics documentation for authoritative product details."
related:
  - "curated/Igor-Pro-10-Syntax-and-Commands.md"
---

# Igor Pro 10 Automation and COM

WaveMetrics Igor Pro acts as an ActiveX Automation (COM) server on Microsoft Windows, enabling external clients and language models to drive experiment data processing, analysis, and visualization.

## COM Object Model

- **ProgID**: `IgorPro.Application`
- **Class ID (CLSID)**: `{F13943F7-0B3A-4BD8-B429-B41750CE8FF3}`
- **Threading Model**: Single-Thread Apartment (STA).

## Core Application Methods

### `Execute(command)`
- **Signature**: `Execute(command: BSTR) -> void` (returns COM `HRESULT` in C++ / `None` in Python `win32com`)
- **Behavior**: Executes a single Igor command string (up to 2,500 bytes).
- **Limitation**: Does not return Igor error codes, command history, output text, or error message strings.

### `Execute2(flags, codePage, command, pErrorCode, errorMsg, history, results)`
- **Signature**: `Execute2(flags: int, codePage: int, command: BSTR, pErrorCode: int* [out], errorMsg: BSTR* [out], history: BSTR* [out], results: BSTR* [out])` — in Python `win32com`, the four `[out]` parameters are returned as `(errorCode, errorMsg, history, results)`.
- **Behavior**: Preferred high-performance execution method introduced in modern Igor Pro. Executes a command synchronously, returns detailed error codes and messages, and captures all generated console history output.
- **Latency**: `< 5 ms` per call without disk I/O or temporary file creation.

## Apartment and Process Boundaries on Windows

1. **Active Desktop Binding**:
   Igor Pro is an interactive desktop application. Calling `GetActiveObject("IgorPro.Application")` requires the calling process to run in the same interactive Windows session and desktop window station (`WinSta0`).
2. **ROT Visibility**:
   Igor Pro registers in the Running Object Table (ROT) of its interactive desktop. Windows security sandboxes and separate worker processes spawned across process boundaries cannot access this ROT entry, resulting in `MK_E_UNAVAILABLE` (`-2147221021`).
3. **In-Process Thread Architecture**:
   To ensure deterministic, ultra-low latency execution, automation clients (including the `igor-pro` MCP server) must maintain an active COM reference within their primary process thread apartment rather than dispatching calls across spawned OS processes.

See also: [Syntax and Commands](Igor-Pro-10-Syntax-and-Commands.md)
