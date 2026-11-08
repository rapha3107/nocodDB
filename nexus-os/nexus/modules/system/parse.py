"""Pure parsers for bootc / rpm-ostree output (no I/O, unit-tested).

Both tools describe the same three slots of an atomic system:
  * booted   — the deployment running now;
  * staged   — a downloaded update, applied at the next boot;
  * rollback — the previous deployment, kept on disk and offered by GRUB.
They are normalised into the same dict shape so the UI never cares which tool
answered.
"""

from __future__ import annotations

import json
import re
from typing import Any

_IMG_PREFIXES = ("ostree-image-signed:", "ostree-unverified-registry:", "ostree-unverified-image:",
                 "ostree-remote-image:", "ostree-remote-registry:")


def _clean_ref(ref: str | None) -> str | None:
    """`ostree-image-signed:docker://ghcr.io/x/y:stable` -> `ghcr.io/x/y:stable`."""
    if not ref:
        return None
    for p in _IMG_PREFIXES:
        if ref.startswith(p):
            ref = ref[len(p):]
            # ostree-remote-image:<remote>:docker://...
            if p.startswith("ostree-remote") and ":" in ref and not ref.startswith("docker://"):
                ref = ref.split(":", 1)[1]
            break
    if ref.startswith("docker://"):
        ref = ref[len("docker://"):]
    return ref


def _empty() -> dict:
    return {"booted": None, "staged": None, "rollback": None}


# ---- bootc -----------------------------------------------------------------

def _bootc_entry(entry: dict | None) -> dict | None:
    if not isinstance(entry, dict):
        return None
    img = entry.get("image") or {}
    ref = (img.get("image") or {}) if isinstance(img, dict) else {}
    ostree = entry.get("ostree") or {}
    return {
        "image": ref.get("image"),
        "transport": ref.get("transport"),
        "version": img.get("version"),
        "timestamp": img.get("timestamp"),
        "digest": img.get("imageDigest"),
        "checksum": ostree.get("checksum"),
        "pinned": bool(entry.get("pinned", False)),
    }


def parse_bootc_status(text: str) -> dict:
    """Parse `bootc status --format=json` (BootcHost, apiVersion org.containers.bootc/v1)."""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ValueError(f"JSON bootc invalide: {e}") from e
    if not isinstance(data, dict) or "status" not in data:
        raise ValueError("Sortie bootc inattendue (champ status absent)")
    st = data.get("status") or {}
    out = _empty()
    out["booted"] = _bootc_entry(st.get("booted"))
    out["staged"] = _bootc_entry(st.get("staged"))
    out["rollback"] = _bootc_entry(st.get("rollback"))
    cached = (st.get("booted") or {}).get("cachedUpdate") if isinstance(st.get("booted"), dict) else None
    out["cached_update"] = ({"version": cached.get("version"), "digest": cached.get("imageDigest"),
                             "timestamp": cached.get("timestamp")} if isinstance(cached, dict) else None)
    spec_img = ((data.get("spec") or {}).get("image") or {})
    out["tracking"] = spec_img.get("image") if isinstance(spec_img, dict) else None
    out["rollback_queued"] = bool(st.get("rollbackQueued", False))
    out["backend"] = "bootc"
    return out


# ---- rpm-ostree -------------------------------------------------------------

def _ostree_entry(d: dict) -> dict:
    return {
        "image": _clean_ref(d.get("container-image-reference")),
        "transport": "registry" if d.get("container-image-reference") else None,
        "version": d.get("version"),
        "timestamp": d.get("timestamp"),
        "digest": d.get("container-image-reference-digest"),
        "checksum": d.get("checksum"),
        "pinned": bool(d.get("pinned", False)),
    }


def parse_rpm_ostree_status(text: str) -> dict:
    """Parse `rpm-ostree status --json`.

    Deployments are listed in boot order: a staged/pending deployment (if any)
    comes before the booted one; the first non-booted deployment after the
    booted one is the rollback target.
    """
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ValueError(f"JSON rpm-ostree invalide: {e}") from e
    deps = data.get("deployments") if isinstance(data, dict) else None
    if not isinstance(deps, list):
        raise ValueError("Sortie rpm-ostree inattendue (deployments absent)")
    out = _empty()
    booted_idx = next((i for i, d in enumerate(deps) if d.get("booted")), None)
    if booted_idx is not None:
        out["booted"] = _ostree_entry(deps[booted_idx])
        before = deps[:booted_idx]
        after = deps[booted_idx + 1:]
        staged = next((d for d in before if d.get("staged")), before[0] if before else None)
        out["staged"] = _ostree_entry(staged) if staged else None
        out["rollback"] = _ostree_entry(after[0]) if after else None
    cu = data.get("cached-update")
    out["cached_update"] = ({"version": cu.get("version"), "digest": cu.get("container-image-reference-digest"),
                             "timestamp": cu.get("timestamp")} if isinstance(cu, dict) else None)
    out["tracking"] = out["booted"]["image"] if out["booted"] else None
    out["rollback_queued"] = False
    out["backend"] = "rpm-ostree"
    return out


# ---- upgrade --check ----------------------------------------------------------

_NO_UPDATE = re.compile(r"no (update|upgrade)s? available|no changes in", re.I)


def parse_upgrade_check(backend: str, returncode: int, stdout: str, stderr: str = "") -> dict:
    """Interpret `bootc upgrade --check` / `rpm-ostree upgrade --check`.

    rpm-ostree exits 77 when no upgrade is available. bootc prints
    "No changes in: ..." / "No update available." when up to date, and
    "Update available for: ..." followed by Version/Digest otherwise.
    """
    text = f"{stdout}\n{stderr}"
    if backend == "rpm-ostree" and returncode == 77:
        return {"available": False, "version": None, "digest": None}
    if returncode != 0:
        raise RuntimeError(text.strip()[-2000:] or f"code {returncode}")
    version = digest = None
    for line in stdout.splitlines():
        s = line.strip()
        low = s.lower()
        if low.startswith("version:") and version is None:
            version = s.split(":", 1)[1].strip() or None
        elif low.startswith("digest:") and digest is None:
            digest = s.split(":", 1)[1].strip() or None
    if _NO_UPDATE.search(text) and not re.search(r"update available for", text, re.I):
        return {"available": False, "version": None, "digest": None}
    available = bool(version or digest or re.search(r"(update|upgrade) available|availableupdate", text, re.I))
    return {"available": available, "version": version, "digest": digest}


def tail_lines(text: str, n: int) -> list[str]:
    lines = text.splitlines()
    return lines[-n:] if n > 0 else []


def as_jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str))
