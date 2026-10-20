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
- **Class ID (CLSID)**: `{C9F06F80-14D5-11CE-AEF7-00AA00392F8A}`
- **Threading Model**: Single-Thread Apartment (STA).

## Core Application Methods

### `Execute(command)`
- **Signature**: `Execute(command: BSTR) -> long`
- **Behavior**: Executes a single Igor command string (up to 2,500 bytes).
- **Return Value**: `0` on success; non-zero error code on failure.
- **Limitation**: Does not return command history, output text, or error message strings.

### `Execute2(flags, unused, command, timeout, unused, unused, unused)`
- **Signature**: `Execute2(flags: long, unused: long, command: BSTR, timeout: long, unused: BSTR, unused: BSTR, unused: BSTR) -> (errorCode, errorMessage, history, results)`
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
