"""Real hardware probing used by `emulation.recommend` (paths injectable for tests)."""

from __future__ import annotations

import re
from pathlib import Path


def read_cpu(cpuinfo: Path) -> dict:
    try:
        text = cpuinfo.read_text(errors="replace")
    except OSError:
        return {"known": False, "flags": [], "threads": None, "model": None, "avx2": None}
    flags: set[str] = set()
    threads = 0
    model = None
    for line in text.splitlines():
        k, _, v = line.partition(":")
        k = k.strip().lower()
        if k == "processor":
            threads += 1
        elif k in ("flags", "features") and not flags:
            flags = set(v.split())
        elif k == "model name" and model is None:
            model = v.strip()
    return {"known": True, "model": model, "threads": threads or None,
            "flags": sorted(flags & {"avx", "avx2", "sse4_1", "sse4_2", "avx512f", "fma", "bmi2"}),
            "avx2": "avx2" in flags, "sse4_1": "sse4_1" in flags}


def read_mem_gb(meminfo: Path) -> float | None:
    try:
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 1024 / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass
    return None


def read_vulkan(runner) -> dict:
    """`vulkaninfo --summary` through the runner. None fields = unknown."""
    if not runner.available("vulkaninfo"):
        return {"known": False, "available": None, "devices": [], "api_version": None,
                "reason": "vulkaninfo absent"}
    try:
        r = runner.run(["vulkaninfo", "--summary"], timeout=20)
    except Exception as e:  # noqa: BLE001 — probing must never break recommend()
        return {"known": False, "available": None, "devices": [], "api_version": None, "reason": str(e)}
    if not r.ok:
        return {"known": True, "available": False, "devices": [], "api_version": None,
                "reason": (r.stderr or "").strip()[-300:]}
    devices = [m.strip() for m in re.findall(r"deviceName\s*=\s*(.+)", r.stdout)]
    apis = re.findall(r"apiVersion\s*=\s*([0-9]+\.[0-9]+(?:\.[0-9]+)?)", r.stdout)
    return {"known": True, "available": bool(devices), "devices": devices,
            "api_version": max(apis, key=lambda v: tuple(int(x) for x in v.split("."))) if apis else None}


def requirement_warnings(name: str, req: dict, cpu: dict, mem_gb: float | None, vk: dict) -> list[str]:
    w = []
    if req.get("avx2") and cpu.get("known"):
        if not cpu.get("avx2"):
            if req["avx2"] == "required":
                w.append(f"CPU sans AVX2 : {name} risque de ne pas démarrer ou d'être inutilisable.")
            else:
                w.append(f"CPU sans AVX2 : {name} sera lent.")
    if req.get("min_cores") and cpu.get("threads") and cpu["threads"] < req["min_cores"]:
        w.append(f"{cpu['threads']} threads CPU détectés : {name} recommande au moins {req['min_cores']}.")
    if req.get("min_ram_gb") and mem_gb is not None and mem_gb < req["min_ram_gb"] - 0.5:
        w.append(f"{mem_gb} Go de RAM : {name} recommande au moins {req['min_ram_gb']} Go.")
    if req.get("vulkan"):
        if vk.get("available") is False:
            w.append(f"Aucun périphérique Vulkan détecté : {name} "
                     + ("en a besoin." if req["vulkan"] == "required" else "sera limité à OpenGL/logiciel."))
        elif vk.get("available") is None:
            w.append("Support Vulkan non vérifié (vulkaninfo indisponible).")
    return w
