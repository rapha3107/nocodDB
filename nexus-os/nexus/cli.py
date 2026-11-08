"""nexusctl — talk to a running nexusd from a terminal (diagnostics, scripting).

    nexusctl status
    nexusctl actions
    nexusctl call music.play '{"query": "jazz"}'
    nexusctl ask "lance ma musique"
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from .core.config import Paths


def _req(method: str, path: str, body: dict | None = None):
    paths = Paths.detect()
    token = (paths.runtime / "token").read_text().strip()
    port = (paths.runtime / "port").read_text().strip()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"X-Nexus-Token": token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "status":
        out = _req("GET", "/api/status")
    elif cmd == "actions":
        out = _req("GET", "/api/actions")
    elif cmd == "call" and len(argv) >= 2:
        out = _req("POST", "/api/call", {"action": argv[1], "params": json.loads(argv[2]) if len(argv) > 2 else {}})
    elif cmd == "ask" and len(argv) >= 2:
        out = _req("POST", "/api/call", {"action": "ai.ask", "params": {"text": " ".join(argv[1:])}})
    elif cmd == "approve" and len(argv) == 2:
        out = _req("POST", f"/api/confirmations/{argv[1]}/approve", {})
    else:
        print(__doc__)
        return 1
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
