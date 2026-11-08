"""Minimal, robust parser for Valve's *text* KeyValues format (VDF / ACF).

Supported:
  * quoted strings with escapes ``\\\\``, ``\\"``, ``\\n``, ``\\t`` (others kept literally);
  * unquoted tokens (``key value`` without quotes, as found in some old files);
  * nested sections ``key { ... }`` at any depth;
  * ``//`` line comments, anywhere outside a quoted string;
  * conditional suffixes like ``[$WIN32]`` after a value (ignored);
  * duplicate keys: sections are merged, scalar values -> last one wins.

Not supported (not needed here): binary VDF (``appinfo.vdf``, ``shortcuts.vdf``)
and ``#include`` / ``#base`` directives (they are skipped as plain tokens).

Lookups in Steam files are case-insensitive in practice ("AppState" vs
"appstate"), use :func:`get` for that.
"""

from __future__ import annotations

from typing import Any

__all__ = ["VDFError", "loads", "load_file", "get"]

_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}
MAX_DEPTH = 64


class VDFError(ValueError):
    pass


def _tokenize(text: str):
    i, n = 0, len(text)
    line = 1
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
        elif c in " \t\r﻿":
            i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif c in "{}":
            yield (c, None, line)
            i += 1
        elif c == '"':
            i += 1
            buf = []
            start_line = line
            while True:
                if i >= n:
                    raise VDFError(f"Chaîne non terminée (ligne {start_line})")
                ch = text[i]
                if ch == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    buf.append(_ESCAPES.get(nxt, "\\" + nxt))
                    i += 2
                    continue
                if ch == '"':
                    i += 1
                    break
                if ch == "\n":
                    line += 1
                buf.append(ch)
                i += 1
            yield ("str", "".join(buf), start_line)
        elif c == "[":
            # conditional like [$WIN32] / [!$X360]: skip it
            j = text.find("]", i)
            if j == -1:
                raise VDFError(f"Condition non terminée (ligne {line})")
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n{}"' and not text.startswith("//", j):
                j += 1
            yield ("str", text[i:j], line)
            i = j


def _merge(d: dict, key: str, value: Any) -> None:
    old = d.get(key)
    if isinstance(old, dict) and isinstance(value, dict):
        for k, v in value.items():
            _merge(old, k, v)
    else:
        d[key] = value


def loads(text: str) -> dict:
    """Parse KeyValues text into nested dicts of str. Raises VDFError on malformed input."""
    if not isinstance(text, str):
        raise VDFError("Entrée VDF invalide")
    root: dict = {}
    stack = [root]
    pending_key: str | None = None
    pending_line = 0
    for kind, val, line in _tokenize(text):
        if kind == "{":
            if pending_key is None:
                raise VDFError(f"Accolade ouvrante sans clé (ligne {line})")
            if len(stack) >= MAX_DEPTH:
                raise VDFError("Imbrication trop profonde")
            new: dict = {}
            _merge(stack[-1], pending_key, new)
            # _merge may have kept an existing dict: descend into the stored one
            stack.append(stack[-1][pending_key] if isinstance(stack[-1][pending_key], dict) else new)
            pending_key = None
        elif kind == "}":
            if pending_key is not None:
                raise VDFError(f"Clé sans valeur: {pending_key!r} (ligne {pending_line})")
            if len(stack) == 1:
                raise VDFError(f"Accolade fermante en trop (ligne {line})")
            stack.pop()
        else:
            if pending_key is None:
                pending_key, pending_line = val, line
            else:
                _merge(stack[-1], pending_key, val)
                pending_key = None
    if pending_key is not None:
        raise VDFError(f"Clé sans valeur: {pending_key!r} (ligne {pending_line})")
    if len(stack) != 1:
        raise VDFError("Accolade fermante manquante en fin de fichier")
    return root


def load_file(path, max_bytes: int = 32 * 1024 * 1024) -> dict:
    with open(path, "rb") as f:
        data = f.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise VDFError("Fichier VDF trop volumineux")
    return loads(data.decode("utf-8", errors="replace"))


def get(d: Any, *keys: str, default: Any = None) -> Any:
    """Case-insensitive nested lookup: get(doc, "AppState", "name")."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        if k in cur:
            cur = cur[k]
            continue
        lk = k.lower()
        for kk, vv in cur.items():
            if kk.lower() == lk:
                cur = vv
                break
        else:
            return default
    return cur
