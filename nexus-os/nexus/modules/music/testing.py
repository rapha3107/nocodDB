"""Fake mpv JSON-IPC server on a real UNIX socket (used by tests and for local dev).

It speaks the same line-based protocol as mpv, records every command received
and keeps a tiny property model so the module's state logic can be exercised
without mpv installed.
"""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path


class FakeMpvServer:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.received: list[list] = []
        self.props: dict = {"pause": False, "idle-active": True, "volume": 70.0, "path": None,
                            "media-title": None, "time-pos": None, "duration": None,
                            "playlist-pos": -1, "playlist-count": 0}
        self.playlist: list[str] = []
        self.pos = -1
        self.fail: set[str] = set()   # command names that should answer with an error
        self._clients: list[socket.socket] = []
        self._observers: list[socket.socket] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock: socket.socket | None = None

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> "FakeMpvServer":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(self.path))
        s.listen(16)
        s.settimeout(0.2)
        self._sock = s
        threading.Thread(target=self._accept, daemon=True).start()
        return self

    def close(self) -> None:
        self._stop.set()
        if self._sock:
            self._sock.close()
        with self._lock:
            for c in self._clients:
                try:
                    c.close()
                except OSError:
                    pass
        try:
            self.path.unlink()
        except OSError:
            pass

    def commands(self, name: str | None = None) -> list[list]:
        with self._lock:
            return [c for c in self.received if name is None or c[0] == name]

    # -- server loop -----------------------------------------------------------
    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                c, _ = self._sock.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            with self._lock:
                self._clients.append(c)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _send(self, c: socket.socket, obj: dict) -> None:
        try:
            c.sendall(json.dumps(obj).encode() + b"\n")
        except OSError:
            pass

    def _serve(self, c: socket.socket) -> None:
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = c.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                msg = json.loads(line)
                cmd = msg["command"]
                with self._lock:
                    self.received.append(cmd)
                err, data = self._handle(c, cmd)
                reply = {"error": err, "data": data}
                if "request_id" in msg:
                    reply["request_id"] = msg["request_id"]
                self._send(c, reply)

    def emit(self, event: dict) -> None:
        with self._lock:
            obs = list(self._observers)
        for c in obs:
            self._send(c, event)

    def _goto(self, i: int) -> None:
        self.pos = i
        if 0 <= i < len(self.playlist):
            p = self.playlist[i]
            self.props.update({"path": p, "idle-active": False, "media-title": Path(p).name,
                               "time-pos": 0.0, "duration": 200.0, "playlist-pos": i})
        else:
            self.props.update({"path": None, "idle-active": True, "media-title": None,
                               "time-pos": None, "duration": None, "playlist-pos": -1})
        self.props["playlist-count"] = len(self.playlist)

    def _handle(self, c, cmd):
        name = cmd[0]
        if name in self.fail:
            return "error running command", None
        if name == "get_property":
            v = self.props.get(cmd[1])
            return ("property unavailable", None) if v is None else ("success", v)
        if name == "set_property":
            self.props[cmd[1]] = cmd[2]
            self.emit({"event": "property-change", "name": cmd[1], "data": cmd[2]})
            return "success", None
        if name == "observe_property":
            with self._lock:
                self._observers.append(c)
            return "success", None
        if name == "loadfile":
            mode = cmd[2] if len(cmd) > 2 else "replace"
            if mode == "replace":
                self.playlist = [cmd[1]]
                self._goto(0)
                self.emit({"event": "file-loaded"})
            else:
                self.playlist.append(cmd[1])
                self.props["playlist-count"] = len(self.playlist)
            return "success", None
        if name in ("playlist-next", "playlist-prev"):
            n = self.pos + (1 if name == "playlist-next" else -1)
            if not 0 <= n < len(self.playlist):
                return "error running command", None
            self._goto(n)
            return "success", None
        if name == "stop":
            self.playlist = []
            self._goto(-1)
            return "success", None
        return "invalid parameter", None
