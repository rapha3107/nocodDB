"""mpv JSON IPC client (UNIX socket, stdlib only) and background player launcher.

Protocol (https://mpv.io/manual/stable/#json-ipc): one JSON object per line.
Request : {"command": ["get_property", "volume"], "request_id": 7}
Reply   : {"request_id": 7, "error": "success", "data": 70.0}
Events  : {"event": "file-loaded"} / {"event": "property-change", "id": 1, "name": "pause", "data": true}

mpv runs as an independent detached process (`runner.spawn`, own session), so the
music keeps playing while a game is in the foreground and even if nexusd
restarts: NEXUS simply reconnects to the existing socket.
"""

from __future__ import annotations

import itertools
import json
import socket
import threading
import time
from pathlib import Path
from typing import Callable

MAX_LINE = 1024 * 1024
SOCKET_NAME = "mpv-music.sock"
# Unix socket paths are limited to 108 bytes (sun_path) on Linux.
MAX_SOCKET_PATH = 107
FLATPAK_MPV = "io.mpv.Mpv"


class MpvError(Exception):
    """mpv answered with an error (e.g. 'property unavailable')."""


class MpvNotRunning(Exception):
    """No mpv is listening on the IPC socket."""


class MpvIpc:
    """Short-lived connection per call: robust to mpv restarts, no shared state."""

    def __init__(self, path: Path, timeout: float = 2.0):
        self.path = Path(path)
        self.timeout = timeout
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def _connect(self) -> socket.socket:
        if len(str(self.path)) > MAX_SOCKET_PATH:
            raise MpvNotRunning(f"chemin de socket trop long: {self.path}")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(str(self.path))
        except OSError as e:
            s.close()
            raise MpvNotRunning(str(e)) from e
        return s

    def alive(self) -> bool:
        try:
            self._connect().close()
            return True
        except MpvNotRunning:
            return False

    def commands(self, cmds: list[list]) -> list:
        """Send several commands on one connection; returns one result per command.
        An mpv error becomes an MpvError instance in the result list (not raised)."""
        s = self._connect()
        try:
            with self._lock:
                ids = [next(self._ids) for _ in cmds]
            payload = b"".join(json.dumps({"command": c, "request_id": i}).encode() + b"\n"
                               for c, i in zip(cmds, ids))
            s.sendall(payload)
            pending = set(ids)
            replies: dict[int, object] = {}
            buf = b""
            deadline = time.monotonic() + self.timeout
            while pending:
                if time.monotonic() > deadline:
                    raise MpvNotRunning("délai de réponse IPC dépassé")
                try:
                    chunk = s.recv(65536)
                except socket.timeout as e:
                    raise MpvNotRunning("délai de réponse IPC dépassé") from e
                if not chunk:
                    raise MpvNotRunning("connexion IPC fermée")
                buf += chunk
                if len(buf) > MAX_LINE * 4:
                    raise MpvNotRunning("réponse IPC trop volumineuse")
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    rid = msg.get("request_id") if isinstance(msg, dict) else None
                    if rid in pending:
                        pending.discard(rid)
                        err = msg.get("error", "success")
                        replies[rid] = msg.get("data") if err == "success" else MpvError(str(err))
            return [replies[i] for i in ids]
        finally:
            s.close()

    def command(self, *cmd):
        r = self.commands([list(cmd)])[0]
        if isinstance(r, MpvError):
            raise r
        return r

    # convenience wrappers ---------------------------------------------------
    def get_property(self, name: str):
        """Returns None when mpv reports the property as unavailable (e.g. idle)."""
        try:
            return self.command("get_property", name)
        except MpvError:
            return None

    def get_properties(self, names: list[str]) -> dict:
        res = self.commands([["get_property", n] for n in names])
        return {n: (None if isinstance(r, MpvError) else r) for n, r in zip(names, res)}

    def set_property(self, name: str, value):
        return self.command("set_property", name, value)

    def loadfile(self, target: str, mode: str = "replace"):
        return self.command("loadfile", target, mode)

    def playlist_next(self):
        return self.command("playlist-next")

    def playlist_prev(self):
        return self.command("playlist-prev")

    def stop(self):
        return self.command("stop")


class MpvWatcher(threading.Thread):
    """Persistent connection observing pause/media/idle changes; calls on_change()."""

    PROPS = ("pause", "media-title", "idle-active", "path")

    def __init__(self, path: Path, on_change: Callable[[dict], None]):
        super().__init__(name="nexus-music-mpv-watch", daemon=True)
        self.path = Path(path)
        self.on_change = on_change
        self._stop = threading.Event()
        self._sock: socket.socket | None = None

    def run(self) -> None:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(str(self.path))
        except OSError:
            return
        self._sock = s
        s.settimeout(0.5)
        try:
            for i, p in enumerate(self.PROPS, 1):
                s.sendall(json.dumps({"command": ["observe_property", i, p]}).encode() + b"\n")
            buf = b""
            while not self._stop.is_set():
                try:
                    chunk = s.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf = (buf + chunk)[-MAX_LINE:]
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(msg, dict) and msg.get("event") in (
                            "property-change", "file-loaded", "end-file", "shutdown"):
                        try:
                            self.on_change(msg)
                        except Exception:  # a callback bug must not kill the watcher
                            pass
                        if msg.get("event") == "shutdown":
                            return
        except OSError:
            pass
        finally:
            s.close()

    def close(self) -> None:
        self._stop.set()


class MpvPlayer:
    """Finds/launches the mpv backend and exposes its IPC client."""

    def __init__(self, runner, socket_path: Path, flatpak=None, start_timeout: float = 5.0,
                 initial_volume: Callable[[], int] | None = None):
        self.runner = runner
        self.socket_path = Path(socket_path)
        self.flatpak = flatpak
        self.start_timeout = start_timeout
        self.initial_volume = initial_volume or (lambda: 70)
        self.ipc = MpvIpc(self.socket_path)
        self.pid: int | None = None
        self._flatpak_cache: bool | None = None
        self._lock = threading.Lock()

    def set_socket_path(self, path: Path) -> None:
        self.socket_path = Path(path)
        self.ipc = MpvIpc(self.socket_path)

    def backend(self) -> str | None:
        """'mpv' (native, preferred), 'flatpak' (io.mpv.Mpv, experimental) or None."""
        if self.runner.available("mpv"):
            return "mpv"
        if self.flatpak is not None and self.runner.available("flatpak"):
            if self._flatpak_cache is None:
                try:
                    self._flatpak_cache = self.flatpak.is_installed(FLATPAK_MPV)
                except Exception:
                    self._flatpak_cache = False
            if self._flatpak_cache:
                return "flatpak"
        return None

    def mpv_args(self) -> list[str]:
        vol = int(self.initial_volume())
        return [
            f"--input-ipc-server={self.socket_path}",
            "--no-video", "--idle=yes", "--really-quiet", "--no-terminal",
            "--force-window=no", "--audio-display=no",
            "--ytdl=no",                     # never resolve web pages through yt-dlp
            f"--volume={max(0, min(100, vol))}",
        ]

    def argv(self, backend: str) -> list[str]:
        if backend == "mpv":
            return ["mpv", *self.mpv_args()]
        return self.flatpak.run_argv(FLATPAK_MPV, *self.mpv_args())

    def running(self) -> bool:
        return self.ipc.alive()

    def ensure(self) -> MpvIpc:
        """Return a connected IPC client, spawning mpv if needed.
        Raises LookupError(reason) when no backend is installed / it does not answer."""
        with self._lock:
            if self.ipc.alive():
                return self.ipc
            backend = self.backend()
            if backend is None:
                raise LookupError("missing")
            if len(str(self.socket_path)) > MAX_SOCKET_PATH:
                raise LookupError("socket_path")
            self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.socket_path.exists():
                self.socket_path.unlink()  # stale socket from a dead mpv
            self.pid = self.runner.spawn(self.argv(backend))
            deadline = time.monotonic() + self.start_timeout
            while time.monotonic() < deadline:
                if self.ipc.alive():
                    return self.ipc
                time.sleep(0.05)
            raise LookupError("no_socket_flatpak" if backend == "flatpak" else "no_socket")
