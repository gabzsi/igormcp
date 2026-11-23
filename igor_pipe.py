"""Native Igor Pro 10.04+ JSON-RPC 2.0 named-pipe transport client."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
import itertools
import json
import os
import re
import sys
import tempfile
import threading
import time
from typing import Any, Callable

if sys.platform == "win32":
    import pywintypes
    import win32event
    import win32file
    import win32pipe
    import winerror
else:  # pragma: no cover - non-Windows import guard
    pywintypes = None
    win32event = None
    win32file = None
    win32pipe = None
    winerror = None


DEFAULT_INSTANCES_FILENAME = "IgorProInstances.json"
_READ_BUFFER_SIZE = 65_536
_DEFAULT_MAX_LINE_BYTES = 32 * 1024 * 1024
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_VERSION_RE = re.compile(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", re.IGNORECASE)

if sys.platform == "win32":
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _KERNEL32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    _KERNEL32.OpenProcess.restype = ctypes.c_void_p
    _KERNEL32.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    _KERNEL32.GetExitCodeProcess.restype = ctypes.c_int
    _KERNEL32.CloseHandle.argtypes = [ctypes.c_void_p]
    _KERNEL32.CloseHandle.restype = ctypes.c_int
else:  # pragma: no cover
    _KERNEL32 = None

_PRE_DISPATCH_WRITE_ERRORS = frozenset(
    {
        109,  # ERROR_BROKEN_PIPE
        232,  # ERROR_NO_DATA
        233,  # ERROR_PIPE_NOT_CONNECTED
    }
)


class IgorPipeError(RuntimeError):
    """Base exception for Igor Pro named-pipe transport errors."""


class PipeNotAvailableError(IgorPipeError):
    """Pre-dispatch error: no instance found, pipe missing, or connection failed.

    Because no request bytes were dispatched to Igor Pro, callers may safely
    fall back to COM or retry connection without risking duplicate execution.
    """


class PipeDispatchError(IgorPipeError):
    """Post-dispatch error: pipe broke or returned an invalid response after send.

    Callers MUST NOT replay mutating commands over COM after this error.
    """


class PipeTimeoutError(PipeDispatchError):
    """Timed out waiting for Igor Pro's response after request dispatch."""


class PipeRPCError(IgorPipeError):
    """Igor Pro returned a JSON-RPC 2.0 error object."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        detail = f"JSON-RPC error {code}: {message}"
        if data is not None:
            detail = f"{detail} (data={data!r})"
        super().__init__(detail)


@dataclass(frozen=True)
class PipeInstance:
    """Metadata for a discovered Igor Pro named-pipe instance."""

    pid: int | None
    pipe: str
    igor_version: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _parse_version_tuple(version_str: str | None) -> tuple[int, int, int] | None:
    if not version_str or not isinstance(version_str, str):
        return None
    match = _VERSION_RE.match(version_str)
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2)) if match.group(2) is not None else 0
    patch = int(match.group(3)) if match.group(3) is not None else 0
    return (major, minor, patch)


def supports_native_pipe(version_str: str | None) -> bool:
    """Return True if the Igor Pro version string is 10.04 or newer."""
    parsed = _parse_version_tuple(version_str)
    if parsed is None:
        return False
    return (parsed[0], parsed[1]) >= (10, 4)


def default_instances_path() -> str:
    """Return the default path to IgorProInstances.json."""
    env_path = os.environ.get("IGOR_INSTANCES_PATH", "").strip()
    if env_path:
        return env_path
    return os.path.join(tempfile.gettempdir(), DEFAULT_INSTANCES_FILENAME)


def is_pid_alive(pid: int) -> bool:
    """Return True if a Windows process with the given PID is still running."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if sys.platform != "win32":  # pragma: no cover
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    handle = _KERNEL32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Access denied means the process exists (e.g., elevated) even though we
        # cannot query its exit code.
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        exit_code = ctypes.c_uint32(0)
        ok = _KERNEL32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        return bool(ok and exit_code.value == _STILL_ACTIVE)
    finally:
        _KERNEL32.CloseHandle(handle)


def _parse_instances_payload(text: str) -> list[dict[str, Any]]:
    stripped = text.strip()
    if not stripped:
        return []

    # Try full-document JSON first (array, {"instances": [...]}, or single object).
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        if isinstance(parsed.get("instances"), list):
            return [item for item in parsed["instances"] if isinstance(item, dict)]
        if any(key in parsed for key in ("pipe", "pipeName", "pipe_name")):
            return [parsed]
        return []

    # Fall back to JSONL (one JSON object per non-empty line).
    entries: list[dict[str, Any]] = []
    for line in stripped.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries


def normalize_pipe_name(pipe_name: str) -> str:
    """Normalize a bare Igor pipe name (e.g. 'IgorPro-...') to '\\\\.\\pipe\\...'."""
    cleaned = pipe_name.strip()
    if not cleaned:
        return ""
    if cleaned.startswith("\\\\") or cleaned.startswith("//"):
        return cleaned.replace("/", "\\")
    return f"\\\\.\\pipe\\{cleaned}"


def _entry_to_instance(entry: dict[str, Any]) -> PipeInstance | None:
    pipe = entry.get("pipe") or entry.get("pipeName") or entry.get("pipe_name")
    if not isinstance(pipe, str) or not pipe.strip():
        return None
    normalized_pipe = normalize_pipe_name(pipe)
    if not normalized_pipe:
        return None

    raw_pid = entry.get("pid", entry.get("processId"))
    pid: int | None
    if isinstance(raw_pid, int) and not isinstance(raw_pid, bool):
        pid = raw_pid
    elif isinstance(raw_pid, str) and raw_pid.strip().isdigit():
        pid = int(raw_pid.strip())
    else:
        pid = None

    raw_version = (
        entry.get("igorVersion")
        or entry.get("igor_version")
        or entry.get("version")
    )
    igor_version = str(raw_version).strip() if raw_version is not None else None
    if igor_version == "":
        igor_version = None

    return PipeInstance(
        pid=pid,
        pipe=normalized_pipe,
        igor_version=igor_version,
        raw=dict(entry),
    )


def discover_instances(
    instances_path: str | os.PathLike[str] | None = None,
    *,
    check_alive: bool = True,
    pid_checker: Callable[[int], bool] | None = None,
) -> list[PipeInstance]:
    """Discover active Igor Pro named-pipe instances from the registry file."""
    path = os.fspath(instances_path) if instances_path is not None else default_instances_path()
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            text = fh.read()
    except OSError:
        return []

    entries = _parse_instances_payload(text)
    checker = pid_checker or is_pid_alive
    discovered: list[PipeInstance] = []
    for entry in entries:
        instance = _entry_to_instance(entry)
        if instance is None:
            continue
        if check_alive:
            if instance.pid is None or not checker(instance.pid):
                continue
        discovered.append(instance)
    return discovered


def select_instance(
    instances_path: str | os.PathLike[str] | None = None,
    *,
    version_prefix: str | None = None,
    pipe_name: str | None = None,
    check_alive: bool = True,
    pid_checker: Callable[[int], bool] | None = None,
) -> PipeInstance:
    """Select the best matching Igor Pro named-pipe instance.

    Respects explicit ``pipe_name`` / ``IGOR_PIPE_NAME`` (skipping discovery)
    and ``version_prefix`` / ``IGOR_VERSION_PREFIX``.
    """
    explicit_pipe = (
        pipe_name.strip()
        if isinstance(pipe_name, str) and pipe_name.strip()
        else os.environ.get("IGOR_PIPE_NAME", "").strip()
    )
    if explicit_pipe:
        return PipeInstance(
            pid=None,
            pipe=explicit_pipe,
            igor_version=None,
            raw={"pipe": explicit_pipe, "source": "override"},
        )

    instances = discover_instances(
        instances_path=instances_path,
        check_alive=check_alive,
        pid_checker=pid_checker,
    )
    if not instances:
        raise PipeNotAvailableError("No active Igor Pro named-pipe instances found.")

    prefix = (
        version_prefix.strip()
        if isinstance(version_prefix, str) and version_prefix.strip()
        else os.environ.get("IGOR_VERSION_PREFIX", "").strip()
    )
    candidates = instances
    if prefix:
        matched = [
            inst
            for inst in instances
            if isinstance(inst.igor_version, str) and inst.igor_version.startswith(prefix)
        ]
        if not matched:
            raise PipeNotAvailableError(
                f"No active Igor Pro instance matched version prefix {prefix!r}."
            )
        candidates = matched

    # Prefer the highest parsed version; break ties by latest entry in registry.
    indexed = list(enumerate(candidates))
    _, best = max(
        indexed,
        key=lambda item: (_parse_version_tuple(item[1].igor_version) or (0, 0, 0), item[0]),
    )
    return best


def _remaining_ms(deadline: float) -> int:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 0
    return max(1, int(remaining * 1000))


def _cancel_overlapped(handle: Any, overlapped: Any) -> None:
    """Cancel pending overlapped I/O and wait until the kernel releases buffers."""
    try:
        win32file.CancelIo(handle)
    except pywintypes.error:
        pass
    try:
        win32file.GetOverlappedResult(handle, overlapped, True)
    except pywintypes.error:
        pass


class IgorPipeClient:
    """Newline-delimited JSON-RPC 2.0 client over a Windows named pipe."""

    def __init__(
        self,
        pipe_name: str | None = None,
        *,
        instances_path: str | os.PathLike[str] | None = None,
        version_prefix: str | None = None,
        default_timeout: float = 30.0,
    ) -> None:
        self._configured_pipe_name = pipe_name
        self._pipe_name: str | None = pipe_name
        self._instances_path = instances_path
        self._version_prefix = version_prefix
        self._default_timeout = float(default_timeout)
        self._handle: Any = None
        self._read_buffer = bytearray()
        self._id_counter = itertools.count(1)
        self._id_lock = threading.Lock()
        self._io_lock = threading.Lock()

    @property
    def pipe_name(self) -> str | None:
        return self._pipe_name

    @property
    def is_connected(self) -> bool:
        return self._handle is not None

    def _next_id(self) -> int:
        with self._id_lock:
            return next(self._id_counter)

    def connect(self, timeout_seconds: float | None = None) -> None:
        """Connect to the Igor Pro named pipe using overlapped I/O."""
        if sys.platform != "win32":  # pragma: no cover
            raise PipeNotAvailableError("Windows named pipes are only supported on Windows.")

        if self._handle is not None:
            return

        timeout = self._default_timeout if timeout_seconds is None else float(timeout_seconds)
        if timeout <= 0:
            raise PipeNotAvailableError("Connection timeout must be positive.")

        pipe_name = self._configured_pipe_name
        if not pipe_name:
            instance = select_instance(
                instances_path=self._instances_path,
                version_prefix=self._version_prefix,
            )
            pipe_name = instance.pipe
        pipe_name = normalize_pipe_name(pipe_name)

        deadline = time.monotonic() + timeout
        while True:
            try:
                handle = win32file.CreateFile(
                    pipe_name,
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0,
                    None,
                    win32file.OPEN_EXISTING,
                    win32file.FILE_FLAG_OVERLAPPED,
                    None,
                )
                self._pipe_name = pipe_name
                self._handle = handle
                self._read_buffer.clear()
                return
            except pywintypes.error as exc:
                if exc.winerror == winerror.ERROR_PIPE_BUSY:
                    wait_ms = _remaining_ms(deadline)
                    if wait_ms <= 0:
                        raise PipeNotAvailableError(
                            f"Timed out waiting for busy pipe {pipe_name!r}."
                        ) from exc
                    try:
                        win32pipe.WaitNamedPipe(pipe_name, min(wait_ms, 250))
                    except pywintypes.error as wait_exc:
                        if wait_exc.winerror not in (
                            winerror.ERROR_SEM_TIMEOUT,
                            winerror.ERROR_PIPE_BUSY,
                        ):
                            raise PipeNotAvailableError(
                                f"Failed waiting for pipe {pipe_name!r}: {wait_exc}"
                            ) from wait_exc
                    if time.monotonic() >= deadline:
                        raise PipeNotAvailableError(
                            f"Timed out waiting for busy pipe {pipe_name!r}."
                        ) from exc
                    continue

                raise PipeNotAvailableError(
                    f"Failed to connect to Igor Pro pipe {pipe_name!r}: {exc}"
                ) from exc

    def close(self) -> None:
        """Close the named pipe handle if open."""
        handle = self._handle
        self._handle = None
        self._read_buffer.clear()
        if handle is not None:
            try:
                win32file.CloseHandle(handle)
            except pywintypes.error:
                pass

    def __enter__(self) -> IgorPipeClient:
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _write_all(self, payload: bytes, deadline: float) -> None:
        """Write all request bytes with overlapped I/O.

        Raises PipeNotAvailableError if the pipe failed before any bytes were
        queued/sent; raises PipeTimeoutError or PipeDispatchError once bytes
        have been queued or partially transmitted.
        """
        handle = self._handle
        if handle is None:
            raise PipeNotAvailableError("Pipe client is not connected.")

        view = memoryview(payload)
        offset = 0
        total = len(payload)
        dispatched = False

        while offset < total:
            remaining_ms = _remaining_ms(deadline)
            if remaining_ms <= 0:
                self.close()
                if dispatched:
                    raise PipeTimeoutError("Timed out while writing JSON-RPC request to pipe.")
                raise PipeNotAvailableError("Timed out before writing JSON-RPC request to pipe.")

            overlapped = pywintypes.OVERLAPPED()
            event = win32event.CreateEvent(None, True, False, None)
            overlapped.hEvent = event
            try:
                try:
                    rc, written = win32file.WriteFile(handle, view[offset:], overlapped)
                except pywintypes.error as exc:
                    self.close()
                    if not dispatched and offset == 0 and exc.winerror in _PRE_DISPATCH_WRITE_ERRORS:
                        raise PipeNotAvailableError(
                            f"Pipe disconnected before request dispatch: {exc}"
                        ) from exc
                    raise PipeDispatchError(
                        f"Failed writing JSON-RPC request to pipe: {exc}"
                    ) from exc

                dispatched = True
                if rc == winerror.ERROR_IO_PENDING:
                    wait_rc = win32event.WaitForSingleObject(event, _remaining_ms(deadline))
                    if wait_rc == win32event.WAIT_TIMEOUT:
                        _cancel_overlapped(handle, overlapped)
                        self.close()
                        raise PipeTimeoutError(
                            "Timed out waiting for overlapped pipe write to complete."
                        )
                    if wait_rc != win32event.WAIT_OBJECT_0:
                        _cancel_overlapped(handle, overlapped)
                        self.close()
                        raise PipeDispatchError(
                            f"Unexpected wait result ({wait_rc}) during pipe write."
                        )
                    try:
                        written = win32file.GetOverlappedResult(handle, overlapped, False)
                    except pywintypes.error as exc:
                        self.close()
                        raise PipeDispatchError(
                            f"Overlapped pipe write failed: {exc}"
                        ) from exc
                elif rc != 0:
                    self.close()
                    raise PipeDispatchError(f"Pipe write returned unexpected status {rc}.")

                if written <= 0:
                    self.close()
                    raise PipeDispatchError("Pipe write transferred zero bytes.")
                offset += written
            finally:
                try:
                    win32file.CloseHandle(event)
                except pywintypes.error:
                    pass

    def _read_line(
        self,
        deadline: float,
        method: str,
        timeout_seconds: float,
        max_line_bytes: int = _DEFAULT_MAX_LINE_BYTES,
    ) -> bytes:
        """Read a single newline-delimited response line using overlapped I/O."""
        while True:
            newline_idx = self._read_buffer.find(b"\n")
            if newline_idx != -1:
                line = bytes(self._read_buffer[: newline_idx + 1])
                del self._read_buffer[: newline_idx + 1]
                if len(line) > max_line_bytes:
                    self.close()
                    raise PipeDispatchError(
                        f"Response line for {method!r} exceeded maximum size "
                        f"({len(line)} > {max_line_bytes} bytes)."
                    )
                return line

            if len(self._read_buffer) > max_line_bytes:
                self.close()
                raise PipeDispatchError(
                    f"Response for {method!r} exceeded maximum line size "
                    f"({max_line_bytes} bytes) without newline delimiter."
                )

            handle = self._handle
            if handle is None:
                raise PipeDispatchError(
                    f"Pipe closed before response to {method!r} was received."
                )

            remaining_ms = _remaining_ms(deadline)
            if remaining_ms <= 0:
                self.close()
                raise PipeTimeoutError(
                    f"Timed out waiting for response to {method!r} after {timeout_seconds}s."
                )

            buf = win32file.AllocateReadBuffer(_READ_BUFFER_SIZE)
            overlapped = pywintypes.OVERLAPPED()
            event = win32event.CreateEvent(None, True, False, None)
            overlapped.hEvent = event
            try:
                try:
                    rc, read_view = win32file.ReadFile(handle, buf, overlapped)
                except pywintypes.error as exc:
                    self.close()
                    raise PipeDispatchError(
                        f"Pipe read failed after dispatching {method!r}: {exc}"
                    ) from exc

                if rc == winerror.ERROR_IO_PENDING:
                    wait_rc = win32event.WaitForSingleObject(event, _remaining_ms(deadline))
                    if wait_rc == win32event.WAIT_TIMEOUT:
                        _cancel_overlapped(handle, overlapped)
                        self.close()
                        raise PipeTimeoutError(
                            f"Timed out waiting for response to {method!r} after {timeout_seconds}s."
                        )
                    if wait_rc != win32event.WAIT_OBJECT_0:
                        _cancel_overlapped(handle, overlapped)
                        self.close()
                        raise PipeDispatchError(
                            f"Unexpected wait result ({wait_rc}) while reading response to {method!r}."
                        )
                    try:
                        n_read = win32file.GetOverlappedResult(handle, overlapped, False)
                    except pywintypes.error as exc:
                        if exc.winerror == winerror.ERROR_MORE_DATA:
                            n_read = len(read_view)
                        else:
                            self.close()
                            raise PipeDispatchError(
                                f"Pipe closed or errored while reading response to {method!r}: {exc}"
                            ) from exc
                elif rc in (0, winerror.ERROR_MORE_DATA):
                    try:
                        n_read = win32file.GetOverlappedResult(handle, overlapped, False)
                    except pywintypes.error as exc:
                        if exc.winerror == winerror.ERROR_MORE_DATA:
                            n_read = len(read_view)
                        else:
                            self.close()
                            raise PipeDispatchError(
                                f"Failed getting overlapped read result for {method!r}: {exc}"
                            ) from exc
                else:
                    self.close()
                    raise PipeDispatchError(
                        f"Pipe read returned unexpected status {rc} for {method!r}."
                    )

                if n_read <= 0:
                    self.close()
                    raise PipeDispatchError(
                        f"Pipe closed before complete response to {method!r} was received."
                    )

                self._read_buffer.extend(read_view[:n_read])
            finally:
                try:
                    win32file.CloseHandle(event)
                except pywintypes.error:
                    pass

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Send a JSON-RPC 2.0 request and return the response ``result``."""
        timeout = self._default_timeout if timeout_seconds is None else float(timeout_seconds)
        if timeout <= 0:
            raise ValueError("timeout_seconds must be positive.")

        deadline = time.monotonic() + timeout
        with self._io_lock:
            if self._handle is None:
                self.connect(timeout_seconds=max(0.05, deadline - time.monotonic()))

            req_id = self._next_id()
            request: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
            }
            if params is not None:
                request["params"] = params

            payload = (
                json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                + b"\n"
            )

            self._write_all(payload, deadline)

            # Request has now been dispatched; any subsequent I/O or protocol
            # failure MUST raise PipeDispatchError / PipeTimeoutError (never
            # PipeNotAvailableError).
            while True:
                raw_line = self._read_line(deadline, method, timeout)
                try:
                    text = raw_line.decode("utf-8").strip()
                except UnicodeDecodeError as exc:
                    self.close()
                    raise PipeDispatchError(
                        f"Invalid UTF-8 in JSON-RPC response for {method!r}: {exc}"
                    ) from exc

                if not text:
                    continue

                try:
                    response = json.loads(text)
                except json.JSONDecodeError as exc:
                    self.close()
                    raise PipeDispatchError(
                        f"Malformed JSON in response for {method!r}: {exc}"
                    ) from exc

                if not isinstance(response, dict):
                    self.close()
                    raise PipeDispatchError(
                        f"JSON-RPC response for {method!r} was not an object."
                    )

                # Ignore unsolicited JSON-RPC notifications while awaiting our id.
                if "id" not in response and "method" in response:
                    continue

                resp_id = response.get("id")
                if resp_id is not None and resp_id != req_id:
                    self.close()
                    raise PipeDispatchError(
                        f"Mismatched JSON-RPC response id for {method!r}: "
                        f"expected {req_id!r}, got {resp_id!r}."
                    )

                if "error" in response and response["error"] is not None:
                    err = response["error"]
                    if isinstance(err, dict):
                        raw_code = err.get("code", -32000)
                        try:
                            code = -32000 if isinstance(raw_code, bool) else int(raw_code)
                        except (TypeError, ValueError):
                            code = -32000
                        message = str(err.get("message", "Unknown JSON-RPC error"))
                        data = err.get("data")
                    else:
                        code = -32000
                        message = str(err)
                        data = None
                    raise PipeRPCError(code=code, message=message, data=data)

                if resp_id != req_id:
                    self.close()
                    raise PipeDispatchError(
                        f"Mismatched JSON-RPC response id for {method!r}: "
                        f"expected {req_id!r}, got {resp_id!r}."
                    )

                if "result" not in response:
                    self.close()
                    raise PipeDispatchError(
                        f"Invalid JSON-RPC response for {method!r}: missing 'result'."
                    )

                result = response["result"]
                if not isinstance(result, dict):
                    self.close()
                    raise PipeDispatchError(
                        "Unexpected non-object JSON-RPC result"
                    )
                return result

    # ------------------------------------------------------------------
    # High-level Igor Pro 10.04+ JSON-RPC helper methods
    # ------------------------------------------------------------------

    def capabilities(self, timeout_seconds: float = 5.0) -> dict[str, Any]:
        """Query Igor Pro's native JSON-RPC capabilities."""
        return self.call("igor.capabilities", timeout_seconds=timeout_seconds)

    def execute_command(
        self, command: str, timeout_seconds: float = 30.0
    ) -> dict[str, Any]:
        """Execute an Igor command line via ``igor.command.execute``."""
        return self.call(
            "igor.command.execute",
            {"cmd": command},
            timeout_seconds=timeout_seconds,
        )

    def call_function(
        self,
        name: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """Invoke an Igor function via ``igor.fn.call``."""
        params: dict[str, Any] = {
            "name": name,
            "args": list(args) if args is not None else [],
        }
        if kwargs is not None:
            params["kwargs"] = dict(kwargs)
        return self.call("igor.fn.call", params, timeout_seconds=timeout_seconds)

    def capture_window(
        self,
        window: str | None = None,
        render: str = "auto",
        max_dimension: int = 1200,
        fmt: str = "png",
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """Capture an Igor window image via ``igor.window.capture``."""
        params: dict[str, Any] = {
            "render": render,
            "maxDimension": max_dimension,
            "format": fmt,
        }
        if window is not None:
            params["window"] = window
        return self.call("igor.window.capture", params, timeout_seconds=timeout_seconds)

    def get_state(self, timeout_seconds: float = 5.0) -> dict[str, Any]:
        """Query Igor Pro application state via ``igor.app.state``."""
        return self.call("igor.app.state", timeout_seconds=timeout_seconds)

    def cancel_request(self, timeout_seconds: float = 10.0) -> dict[str, Any]:
        """Request cancellation of the currently running Igor operation."""
        return self.call("igor.request.cancel", timeout_seconds=timeout_seconds)

    def compile_procedures(self, timeout_seconds: float = 30.0) -> dict[str, Any]:
        """Trigger procedure compilation via ``igor.procedures.compile``."""
        return self.call("igor.procedures.compile", timeout_seconds=timeout_seconds)

    def get_compile_status(self, timeout_seconds: float = 10.0) -> dict[str, Any]:
        """Query procedure compilation status via ``igor.procedures.compileStatus``."""
        return self.call(
            "igor.procedures.compileStatus", timeout_seconds=timeout_seconds
        )

    def close_procedures(
        self, file_paths: list[str], timeout_seconds: float = 30.0
    ) -> dict[str, Any]:
        """Close open procedure windows via ``igor.procedures.close``."""
        return self.call(
            "igor.procedures.close",
            {"filePaths": list(file_paths)},
            timeout_seconds=timeout_seconds,
        )


def cancel_igor_request(
    pipe_name: str | None = None,
    timeout_seconds: float = 10.0,
    *,
    instances_path: str | os.PathLike[str] | None = None,
    version_prefix: str | None = None,
) -> dict[str, Any]:
    """Send ``igor.request.cancel`` on an independent pipe connection.

    Using a separate connection ensures cancellation never blocks behind an
    in-flight request on a shared ``IgorPipeClient``.
    """
    with IgorPipeClient(
        pipe_name=pipe_name,
        instances_path=instances_path,
        version_prefix=version_prefix,
        default_timeout=timeout_seconds,
    ) as client:
        return client.cancel_request(timeout_seconds=timeout_seconds)


__all__ = [
    "DEFAULT_INSTANCES_FILENAME",
    "IgorPipeClient",
    "IgorPipeError",
    "PipeDispatchError",
    "PipeInstance",
    "PipeNotAvailableError",
    "PipeRPCError",
    "PipeTimeoutError",
    "cancel_igor_request",
    "default_instances_path",
    "discover_instances",
    "is_pid_alive",
    "select_instance",
    "supports_native_pipe",
]
