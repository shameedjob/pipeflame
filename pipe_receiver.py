"""Receive profiler events from multiple local clients: a named pipe on Windows,
a Unix domain socket on macOS."""

from __future__ import annotations

import argparse
import ctypes
import os
import socket
import stat
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator

PIPE_ENV_VAR = "PIPEFLAME_PIPE"
BUFFER_SIZE = 4096


def split_lines(pending: bytearray, data: bytes) -> list[bytes]:
    """Append data to pending and return the complete lines; the unfinished tail stays in pending."""
    pending.extend(data)
    *lines, tail = pending.split(b"\n")
    pending[:] = tail
    return lines


class PipeReceiver(ABC):
    """Accepts local clients and calls on_line with each decoded line they send.
    on_line runs on the client's reader thread, one thread per client.
    Subclasses supply the platform transport: _accept, _chunks and _close."""

    def __init__(self, on_line: Callable[[str], None], pipe_name: str):
        self.on_line = on_line
        self.pipe_name = pipe_name
        self._closed = False

    def start(self) -> threading.Thread:
        """Accept clients on a background thread, e.g. alongside the Flask app."""
        thread = threading.Thread(target=self.serve_forever, name="pipe-receiver", daemon=True)
        thread.start()
        return thread

    def serve_forever(self) -> None:
        while not self._closed:
            conn = self._accept()
            if conn is not None:
                threading.Thread(target=self._read_client, args=(conn,), daemon=True).start()

    @abstractmethod
    def _accept(self):
        """Block until a client connects and return its connection; None if it left before we saw it."""

    @abstractmethod
    def _chunks(self, conn) -> Iterator[bytes]:
        """Yield data from the client until it disconnects."""

    @abstractmethod
    def _close(self, conn) -> None:
        """Release the client's connection."""

    def _read_client(self, conn) -> None:
        pending = bytearray()
        try:
            for chunk in self._chunks(conn):
                for line in split_lines(pending, chunk):
                    self._emit(line)
            if pending:
                self._emit(bytes(pending))
        finally:
            self._close(conn)

    def _emit(self, raw_line: bytes) -> None:
        self.on_line(raw_line.decode("utf-8", errors="replace").rstrip("\r"))


class WindowsPipeReceiver(PipeReceiver):
    """Named pipe server; each client gets its own pipe instance. Remote clients are rejected."""

    PIPE_ACCESS_INBOUND = 0x00000001
    PIPE_TYPE_BYTE = 0x00000000
    PIPE_READMODE_BYTE = 0x00000000
    PIPE_WAIT = 0x00000000
    PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
    PIPE_UNLIMITED_INSTANCES = 255
    ERROR_PIPE_CONNECTED = 535
    ERROR_NO_DATA = 232
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    def __init__(self, on_line: Callable[[str], None], pipe_name: str):
        if os.name != "nt":
            raise OSError("WindowsPipeReceiver requires Windows named pipes")
        super().__init__(on_line, pipe_name)
        self.kernel32 = self._load_kernel32()

    @staticmethod
    def _load_kernel32():
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateNamedPipeW.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        )
        kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
        kernel32.ConnectNamedPipe.argtypes = (wintypes.HANDLE, wintypes.LPVOID)
        kernel32.ConnectNamedPipe.restype = wintypes.BOOL
        kernel32.ReadFile.argtypes = (
            wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
        )
        kernel32.ReadFile.restype = wintypes.BOOL
        kernel32.DisconnectNamedPipe.argtypes = (wintypes.HANDLE,)
        kernel32.DisconnectNamedPipe.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        return kernel32

    def _accept(self) -> int | None:
        handle = self.kernel32.CreateNamedPipeW(
            self.pipe_name,
            self.PIPE_ACCESS_INBOUND,
            self.PIPE_TYPE_BYTE | self.PIPE_READMODE_BYTE | self.PIPE_WAIT | self.PIPE_REJECT_REMOTE_CLIENTS,
            self.PIPE_UNLIMITED_INSTANCES,
            BUFFER_SIZE,
            BUFFER_SIZE,
            0,
            None,
        )
        if handle == self.INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())

        if self.kernel32.ConnectNamedPipe(handle, None):
            return handle
        error = ctypes.get_last_error()
        if error == self.ERROR_PIPE_CONNECTED:  # client connected between create and connect
            return handle
        self.kernel32.CloseHandle(handle)
        if error == self.ERROR_NO_DATA:  # client connected and already closed
            return None
        raise ctypes.WinError(error)

    def _chunks(self, handle: int) -> Iterator[bytes]:
        from ctypes import wintypes

        buffer = ctypes.create_string_buffer(BUFFER_SIZE)
        count = wintypes.DWORD()
        while self.kernel32.ReadFile(handle, buffer, BUFFER_SIZE, ctypes.byref(count), None):
            yield buffer.raw[:count.value]

    def _close(self, handle: int) -> None:
        self.kernel32.DisconnectNamedPipe(handle)
        self.kernel32.CloseHandle(handle)


class UnixSocketReceiver(PipeReceiver):
    """Unix domain socket server for macOS (also works on Linux). The socket file is
    readable and writable by the current user only."""

    def __init__(self, on_line: Callable[[str], None], pipe_name: str):
        if not hasattr(socket, "AF_UNIX"):
            raise OSError("UnixSocketReceiver requires Unix domain sockets")
        super().__init__(on_line, pipe_name)
        self._server: socket.socket | None = None

    def _listen(self) -> socket.socket:
        # clear a socket file left by a previous run, but never delete anything else
        try:
            if not stat.S_ISSOCK(os.lstat(self.pipe_name).st_mode):
                raise FileExistsError(f"{self.pipe_name} exists and is not a socket")
            os.unlink(self.pipe_name)
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)  # create the socket file as 0600
        try:
            server.bind(self.pipe_name)
        finally:
            os.umask(old_umask)
        server.listen()
        return server

    def _accept(self) -> socket.socket | None:
        if self._server is None:
            self._server = self._listen()
        try:
            conn, _ = self._server.accept()
        except OSError:
            if self._closed:  # close() was called while waiting
                return None
            raise
        return conn

    def _chunks(self, conn: socket.socket) -> Iterator[bytes]:
        while chunk := conn.recv(BUFFER_SIZE):
            yield chunk

    def _close(self, conn: socket.socket) -> None:
        conn.close()

    def close(self) -> None:
        """Stop listening and remove the socket file."""
        self._closed = True
        if self._server is not None:
            self._server.close()
            self._server = None
            try:
                os.unlink(self.pipe_name)
            except FileNotFoundError:
                pass


def create_receiver(on_line: Callable[[str], None], pipe_name: str | None = None) -> PipeReceiver:
    """The receiver for this OS. pipe_name falls back to $PIPEFLAME_PIPE; one of them is required."""
    pipe_name = pipe_name or os.environ.get(PIPE_ENV_VAR)
    if not pipe_name:
        raise OSError(f"no pipe name: pass --pipe or set ${PIPE_ENV_VAR}")
    cls = WindowsPipeReceiver if os.name == "nt" else UnixSocketReceiver
    return cls(on_line, pipe_name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipe", help=r"pipe name on Windows (e.g. \\.\pipe\pipeflame), "
                                        "socket path elsewhere (e.g. /tmp/pipeflame.sock); "
                                        f"overrides ${PIPE_ENV_VAR}")
    args = parser.parse_args()

    print_lock = threading.Lock()

    def print_line(line: str) -> None:
        with print_lock:
            print(line, flush=True)

    try:
        receiver = create_receiver(print_line, args.pipe)
    except OSError as e:
        parser.error(str(e))
    # serve on a background thread: a blocking ConnectNamedPipe on the main thread ignores Ctrl+C
    receiver.start()
    print(f"listening on {receiver.pipe_name}", file=sys.stderr)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if isinstance(receiver, UnixSocketReceiver):
            receiver.close()


if __name__ == "__main__":
    main()
