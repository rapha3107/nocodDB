"""Safe command execution.

Rules enforced here (audited by the Security agent):
  * argv lists only, never a shell (shell=False, no string commands);
  * the executable must be in ALLOWED_BINARIES (resolved to an absolute path);
  * every argument is a str without NUL bytes;
  * per-command validators reject dangerous sub-commands/flags;
  * timeouts and output caps; everything is audited.
NEXUS never runs as root. Privileged operations go through the target tool's
own polkit policy (rpm-ostree, flatpak --system), never `sudo`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Sequence

from ..core.errors import CommandFailed, PermissionDenied, Unavailable

MAX_OUTPUT = 2 * 1024 * 1024

ALLOWED_BINARIES: set[str] = {
    "flatpak", "rpm-ostree", "bootc", "ujust", "steam", "xdg-open", "mpv", "wpctl", "pactl",
    "pw-record", "arecord", "whisper-cli", "piper", "piper-tts", "ollama", "lsusb", "vulkaninfo",
    "powerprofilesctl", "gamemoded", "lutris", "systemctl", "loginctl", "nmcli", "bluetoothctl",
    "gsettings", "notify-send", "glxinfo", "monado-service", "lscpu", "upower",
    "gamemoderun", "mangohud", "nvidia-smi", "lspci", "pw-play", "paplay", "aplay", "gamescope",
    "ffprobe", "free", "uname", "journalctl",
}


def _flatpak_validator(argv: Sequence[str]) -> None:
    allowed = {"install", "uninstall", "update", "list", "info", "search", "run",
               "remote-ls", "remotes", "remote-info", "kill", "ps", "override", "history"}
    sub = next((a for a in argv[1:] if not a.startswith("-")), None)
    if sub not in allowed:
        raise PermissionDenied(f"flatpak sub-command refusée: {sub}")
    if sub == "run":
        # `flatpak run <app-id> [app args]`: no flatpak options (e.g. --command,
        # --filesystem, --devel) may widen the sandbox.
        i = argv.index("run")
        if len(argv) <= i + 1 or argv[i + 1].startswith("-"):
            raise PermissionDenied("flatpak run: options interdites avant l'identifiant d'application")
    if sub == "override":
        # Only allow read-only inspection, never granting extra permissions.
        if not any(a == "--show" for a in argv):
            raise PermissionDenied("flatpak override ne peut être utilisé qu'en lecture (--show)")
    for a in argv:
        if a.startswith("--from=") or a.endswith(".flatpakref") or a.endswith(".flatpakrepo"):
            raise PermissionDenied("Installation flatpak depuis une source externe refusée")
        if a in ("--no-gpg-verify", "--noninteractive=false"):
            raise PermissionDenied(f"Option flatpak refusée: {a}")


def _rpm_ostree_validator(argv: Sequence[str]) -> None:
    sub = argv[1] if len(argv) > 1 else ""
    if sub not in {"status", "upgrade", "rollback", "update"}:
        raise PermissionDenied(f"rpm-ostree sub-command refusée: {sub}")


def _bootc_validator(argv: Sequence[str]) -> None:
    sub = argv[1] if len(argv) > 1 else ""
    if sub not in {"status", "upgrade", "rollback"}:
        raise PermissionDenied(f"bootc sub-command refusée: {sub}")


def _systemctl_validator(argv: Sequence[str]) -> None:
    args = [a for a in argv[1:] if not a.startswith("-")]
    if not args:
        raise PermissionDenied("systemctl sans commande")
    if args[0] in {"poweroff", "reboot", "suspend"}:
        return
    if args[0] in {"status", "is-active", "is-enabled"}:
        return
    if args[0] in {"start", "stop", "restart"} and "--user" in argv:
        return  # user units only
    raise PermissionDenied(f"systemctl refusé: {' '.join(args)}")


def _journalctl_validator(argv: Sequence[str]) -> None:
    # read-only, current user's logs only
    if "--user" not in argv or any(a in ("--rotate", "--vacuum-size", "--vacuum-time", "--flush") or a.startswith("--vacuum") for a in argv):
        raise PermissionDenied("journalctl: lecture des journaux utilisateur uniquement")


def _xdg_open_validator(argv: Sequence[str]) -> None:
    if len(argv) != 2 or not argv[1].startswith(("steam://", "heroic://", "https://", "lutris:")):
        raise PermissionDenied("xdg-open n'accepte que des URL steam://, heroic://, lutris: ou https://")


VALIDATORS: dict[str, Callable[[Sequence[str]], None]] = {
    "flatpak": _flatpak_validator,
    "rpm-ostree": _rpm_ostree_validator,
    "bootc": _bootc_validator,
    "systemctl": _systemctl_validator,
    "xdg-open": _xdg_open_validator,
    "journalctl": _journalctl_validator,
}


@dataclass
class Result:
    argv: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def validate_argv(argv: Sequence[str]) -> None:
    if isinstance(argv, (str, bytes)) or not argv:
        raise PermissionDenied("Une commande doit être une liste d'arguments non vide")
    for a in argv:
        if not isinstance(a, str) or "\x00" in a:
            raise PermissionDenied("Argument de commande invalide")
    name = argv[0]
    if "/" in name or name not in ALLOWED_BINARIES:
        raise PermissionDenied(f"Exécutable non autorisé: {name}")
    v = VALIDATORS.get(name)
    if v:
        v(argv)


class Runner:
    """Executes allow-listed commands. Tests use FakeRunner (same interface)."""

    def __init__(self, audit=None):
        self.audit = audit
        self._children: dict[int, subprocess.Popen] = {}

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def available(self, name: str) -> bool:
        return self.which(name) is not None

    def run(self, argv: Sequence[str], *, timeout: float = 120, check: bool = False,
            env: dict | None = None, input: str | None = None) -> Result:
        argv = list(argv)
        validate_argv(argv)
        exe = self.which(argv[0])
        if not exe:
            raise Unavailable(f"Programme absent du système: {argv[0]}", details={"binary": argv[0]})
        if self.audit:
            self.audit.log("exec", argv=argv)
        full_env = {**os.environ, **(env or {})}
        try:
            p = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=timeout,
                               env=full_env, input=input, shell=False, check=False)
        except subprocess.TimeoutExpired as e:
            raise CommandFailed(f"Délai dépassé: {argv[0]}", details={"argv": argv}) from e
        res = Result(argv, p.returncode, p.stdout[-MAX_OUTPUT:], p.stderr[-MAX_OUTPUT:])
        if check and not res.ok:
            raise CommandFailed(f"Échec de {argv[0]} (code {p.returncode})",
                                details={"argv": argv, "stderr": res.stderr[-2000:]})
        return res

    def spawn(self, argv: Sequence[str], *, env: dict | None = None) -> int:
        """Start a long-running detached process (game, mpv, browser). Returns pid."""
        argv = list(argv)
        validate_argv(argv)
        exe = self.which(argv[0])
        if not exe:
            raise Unavailable(f"Programme absent du système: {argv[0]}", details={"binary": argv[0]})
        if self.audit:
            self.audit.log("spawn", argv=argv)
        p = subprocess.Popen([exe, *argv[1:]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env={**os.environ, **(env or {})},
                             start_new_session=True, shell=False)
        self._reap()
        self._children[p.pid] = p
        return p.pid

    def _reap(self) -> None:
        for pid, p in list(self._children.items()):
            if p.poll() is not None:
                self._children.pop(pid, None)

    def is_running(self, pid: int) -> bool:
        p = self._children.get(pid)
        return p is not None and p.poll() is None

    def terminate(self, pid: int, timeout: float = 5) -> bool:
        """Stop a process previously started by spawn() (and only those). Reaps it."""
        p = self._children.get(pid)
        if p is None:
            raise PermissionDenied("Ce processus n'a pas été lancé par NEXUS")
        if self.audit:
            self.audit.log("terminate", pid=pid, argv=list(p.args) if isinstance(p.args, list) else str(p.args))
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=timeout)
        self._children.pop(pid, None)
        return True


@dataclass
class FakeRunner(Runner):
    """Test double: records calls, returns scripted results, still validates argv."""

    responses: dict[tuple, Result] = field(default_factory=dict)
    binaries: set[str] = field(default_factory=lambda: set(ALLOWED_BINARIES))
    calls: list[list[str]] = field(default_factory=list)
    spawned: list[list[str]] = field(default_factory=list)
    terminated: list[int] = field(default_factory=list)
    running: set[int] = field(default_factory=set)
    audit: object = None
    _next_pid: int = 4242

    def which(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if name in self.binaries else None

    def script(self, prefix: Sequence[str], returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.responses[tuple(prefix)] = Result(list(prefix), returncode, stdout, stderr)

    def run(self, argv, *, timeout=120, check=False, env=None, input=None) -> Result:
        argv = list(argv)
        validate_argv(argv)
        if not self.which(argv[0]):
            raise Unavailable(f"Programme absent du système: {argv[0]}", details={"binary": argv[0]})
        self.calls.append(argv)
        best = None
        for prefix, r in self.responses.items():
            if tuple(argv[: len(prefix)]) == prefix and (best is None or len(prefix) > len(best[0])):
                best = (prefix, r)
        res = Result(argv, best[1].returncode, best[1].stdout, best[1].stderr) if best else Result(argv, 0)
        if check and not res.ok:
            raise CommandFailed(f"Échec de {argv[0]}", details={"argv": argv, "stderr": res.stderr})
        return res

    def spawn(self, argv, *, env=None) -> int:
        argv = list(argv)
        validate_argv(argv)
        if not self.which(argv[0]):
            raise Unavailable(f"Programme absent du système: {argv[0]}", details={"binary": argv[0]})
        self.spawned.append(argv)
        pid = self._next_pid
        self._next_pid += 1
        self.running.add(pid)
        return pid

    def is_running(self, pid: int) -> bool:
        return pid in self.running

    def terminate(self, pid: int, timeout: float = 5) -> bool:
        if pid not in self.running:
            raise PermissionDenied("Ce processus n'a pas été lancé par NEXUS")
        self.running.discard(pid)
        self.terminated.append(pid)
        return True
