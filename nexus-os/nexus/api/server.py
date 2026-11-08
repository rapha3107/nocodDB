"""nexusd — local HTTP API + static server for NEXUS Home.

Security model:
  * binds 127.0.0.1 only;
  * Host header must be 127.0.0.1/localhost (blocks DNS rebinding from web pages
    opened in NEXUS Browser);
  * every state-changing request needs X-Nexus-Token (a per-boot secret injected
    into index.html and written to $XDG_RUNTIME_DIR/nexus/token, mode 0600);
    cross-origin pages cannot read it (no CORS headers are ever sent);
  * the SSE stream requires the token as a query parameter.

Routes:
  GET  /                          NEXUS Home (index.html with token)
  GET  /<static>                  files under ui/
  GET  /api/health
  GET  /api/status                modules status + load errors
  GET  /api/actions               action catalog (also used by NEXUS AI)
  POST /api/call                  {action, params, confirmation_id?}
  GET  /api/confirmations
  POST /api/confirmations/<id>/approve   approve + execute the pending action
  POST /api/confirmations/<id>/reject
  GET  /api/tasks, /api/tasks/<id>
  GET  /api/events?token=...      Server-Sent Events
  GET  /api/m/<module>/<path>     module-served files (covers, artwork)
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import mimetypes
import os
import queue
import secrets
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..core.app import build_context, setup_logging
from ..core.errors import NexusError, NotFound, PermissionDenied
from ..security.permissions import Origin
from ..security.verify import safe_join

log = logging.getLogger("nexus.api")
UI_DIR = Path(os.environ.get("NEXUS_UI_DIR", Path(__file__).resolve().parents[2] / "ui"))
MAX_BODY = 1024 * 1024


class NexusServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, ctx, token: str, ui_dir: Path = UI_DIR):
        super().__init__(addr, Handler)
        self.ctx = ctx
        self.token = token
        self.ui_dir = ui_dir
        self.stopping = threading.Event()


class Handler(BaseHTTPRequestHandler):
    server: NexusServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug("%s " + fmt, self.address_string(), *args)

    # ---- helpers -----------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data):
        self._send(status, json.dumps(data, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")

    def _error(self, e: NexusError):
        self._json(e.http_status, e.to_dict())

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1")

    def _token_ok(self, provided: str | None) -> bool:
        return bool(provided) and hmac.compare_digest(provided, self.server.token)

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True  # same-origin fetches from older engines / nexusctl
        o = urlparse(origin)
        return o.hostname in ("127.0.0.1", "localhost", "::1")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise NexusError("Requête trop volumineuse", code="too_large")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise NexusError("JSON invalide", code="invalid_json")
        if not isinstance(data, dict):
            raise NexusError("Objet JSON attendu", code="invalid_json")
        return data

    # ---- verbs -------------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return self._json(421, {"error": "bad_host"})
        url = urlparse(self.path)
        p = url.path
        ctx = self.server.ctx
        try:
            if p == "/api/health":
                return self._json(200, {"ok": True})
            if p == "/api/events":
                return self._sse(parse_qs(url.query).get("token", [None])[0])
            if p.startswith("/api/"):
                # Read endpoints still require the token: they expose library data.
                if not self._token_ok(self.headers.get("X-Nexus-Token") or parse_qs(url.query).get("token", [None])[0]):
                    raise PermissionDenied("Jeton de session manquant ou invalide")
            if p == "/api/status":
                mods = {}
                for name, m in ctx.registry.modules.items():
                    try:
                        mods[name] = {"title": m.title, **m.status()}
                    except Exception as e:
                        mods[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                return self._json(200, {"modules": mods, "load_errors": ctx.registry.load_errors})
            if p == "/api/actions":
                return self._json(200, ctx.registry.describe())
            if p == "/api/confirmations":
                return self._json(200, ctx.permissions.pending())
            if p == "/api/tasks":
                return self._json(200, ctx.tasks.list())
            if p.startswith("/api/tasks/"):
                t = ctx.tasks.get(p.rsplit("/", 1)[1])
                if not t:
                    raise NotFound("Tâche inconnue")
                return self._json(200, t.to_dict())
            if p == "/api/settings":
                return self._json(200, ctx.settings.all())
            if p.startswith("/api/m/"):
                return self._module_file(p[len("/api/m/"):])
            if p.startswith("/api/"):
                raise NotFound("Route inconnue")
            return self._static(p)
        except NexusError as e:
            self._error(e)

    do_HEAD = do_GET

    def do_POST(self):
        if not self._host_ok():
            return self._json(421, {"error": "bad_host"})
        if not self._origin_ok():
            return self._json(403, {"error": "bad_origin", "message": "Origine refusée"})
        if not self._token_ok(self.headers.get("X-Nexus-Token")):
            return self._json(403, {"error": "permission_denied", "message": "Jeton de session invalide"})
        ctx = self.server.ctx
        p = urlparse(self.path).path
        try:
            body = self._body()
            if p == "/api/call":
                result = ctx.registry.call(body.get("action", ""), body.get("params") or {},
                                           origin=Origin.USER, confirmation_id=body.get("confirmation_id"))
                return self._json(200, {"ok": True, "result": result})
            if p.startswith("/api/confirmations/"):
                parts = p.split("/")
                if len(parts) != 5:
                    raise NotFound("Route inconnue")
                cid, verb = parts[3], parts[4]
                if verb == "reject":
                    ctx.permissions.reject(cid)
                    return self._json(200, {"ok": True})
                if verb == "approve":
                    pending = ctx.permissions.approve(cid, Origin.USER)
                    result = ctx.registry.call(pending.action, pending.params,
                                               origin=pending.origin, confirmation_id=cid)
                    return self._json(200, {"ok": True, "result": result})
            raise NotFound("Route inconnue")
        except NexusError as e:
            self._error(e)

    # ---- streams & files ---------------------------------------------------
    def _sse(self, token):
        if not self._token_ok(token):
            return self._json(403, {"error": "permission_denied"})
        q = self.server.ctx.events.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while not self.server.stopping.is_set():
                try:
                    ev = q.get(timeout=15)
                    data = json.dumps(ev, ensure_ascii=False, default=str)
                    self.wfile.write(f"event: {ev['topic']}\ndata: {data}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.ctx.events.unsubscribe(q)

    def _module_file(self, rest: str):
        name, _, sub = rest.partition("/")
        m = self.server.ctx.registry.modules.get(name)
        serve = getattr(m, "serve_file", None)
        path = serve(sub) if serve else None
        if not path or not Path(path).is_file():
            raise NotFound("Fichier introuvable")
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if not ctype.startswith(("image/", "audio/")):
            raise PermissionDenied("Type de fichier non servi")
        self._send(200, Path(path).read_bytes(), ctype, {"Cache-Control": "max-age=3600"})

    def _static(self, p: str):
        ui = self.server.ui_dir
        if p in ("", "/"):
            p = "/index.html"
        try:
            f = safe_join(ui, p.lstrip("/"))
        except PermissionDenied:
            raise NotFound("Fichier introuvable")
        if not f.is_file():
            raise NotFound("Fichier introuvable")
        body = f.read_bytes()
        ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        if ctype == "text/javascript":
            ctype = "application/javascript"
        extra = {}
        if f.name == "index.html":
            body = body.replace(b"__NEXUS_TOKEN__", self.server.token.encode())
            extra["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' data: https:; media-src 'self' https: http:; "
                "connect-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'")
            extra["X-Frame-Options"] = "DENY"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        self._send(200, body, ctype, extra)


def make_server(ctx, host="127.0.0.1", port=7777, token: str | None = None, ui_dir: Path = UI_DIR) -> NexusServer:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("nexusd ne doit écouter que sur l'interface locale")
    token = token or secrets.token_urlsafe(32)
    srv = NexusServer((host, port), ctx, token, ui_dir)
    tf = ctx.paths.runtime / "token"
    tf.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(tf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    (ctx.paths.runtime / "port").write_text(str(srv.server_address[1]))
    return srv


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nexusd")
    ap.add_argument("--port", type=int, default=int(os.environ.get("NEXUS_PORT", 7777)))
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)
    from ..core.config import Paths
    paths = Paths.detect()
    setup_logging(paths, logging.DEBUG if args.debug else logging.INFO)
    if os.geteuid() == 0 and os.environ.get("NEXUS_ALLOW_ROOT_DEV") != "1":
        log.error("nexusd refuse de s'exécuter en root")
        raise SystemExit(2)
    ctx = build_context(paths=paths)
    srv = make_server(ctx, port=args.port)
    log.info("NEXUS Home: http://127.0.0.1:%s/", srv.server_address[1])

    def _stop(*_):
        srv.stopping.set()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        srv.serve_forever()
    finally:
        ctx.registry.stop_all()
        ctx.tasks.shutdown()


if __name__ == "__main__":
    main()
