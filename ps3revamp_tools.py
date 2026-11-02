"""
ps3revamp_tools -- what the pipeline may use, and how.

Every entry is optional. The driver never needs more than ps3recomp + a C++
compiler; the rest are requested explicitly (`--analyzer ida`, `--analyzer
ghidra`, `ps3revamp.py oracle`) and are reported by `ps3revamp.py tools`.

Detection only: nothing here downloads, installs or runs a tool on its own.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

RPCS3_HINTS = [
    Path(r"C:\Program Files\RPCS3"),
    Path(r"C:\Program Files (x86)\RPCS3"),
    Path(r"C:\RPCS3"),
    Path.home() / "Desktop" / "RPCS3",
    Path.home() / "Downloads" / "RPCS3",
    Path.home() / "AppData" / "Local" / "RPCS3",
]


def _find_rpcs3() -> Path | None:
    for env in ("PS3REVAMP_RPCS3", "RPCS3_DIR"):
        p = os.environ.get(env)
        if p:
            c = Path(p)
            if c.is_file():
                return c
            if (c / "rpcs3.exe").is_file():
                return c / "rpcs3.exe"
    exe = shutil.which("rpcs3")
    if exe:
        return Path(exe)
    for d in RPCS3_HINTS:
        for name in ("rpcs3.exe", "rpcs3"):
            if (d / name).is_file():
                return d / name
    return None


def _find_ghidra() -> tuple[Path | None, str]:
    for env in ("GHIDRA_HOME", "GHIDRA_INSTALL_DIR"):
        p = os.environ.get(env)
        if p and (Path(p) / "support" / "analyzeHeadless.bat").is_file():
            return Path(p) / "support" / "analyzeHeadless.bat", f"${env}={p}"
    exe = shutil.which("analyzeHeadless")
    if exe:
        return Path(exe), "PATH"
    return None, "GHIDRA_HOME not set and analyzeHeadless not on PATH"


def registry(env) -> list[dict]:
    """Return one dict per known tool, with availability and purpose."""
    out: list[dict] = []

    tk = env.toolkit
    out.append({
        "id": "ps3recomp",
        "name": "ps3recomp (loader, PPU/SPU lifter, HLE runtime)",
        "available": bool(tk),
        "detail": str(tk) if tk else "not found",
        "use": "the conversion itself: OPD scan, lifting, HLE table, runtime",
        "enable": "run `ps3revamp.py setup` (bundled by default)",
    })

    cc = env.clang_cl or env.clang
    out.append({
        "id": "clang",
        "name": "clang / clang-cl (LLVM)",
        "available": bool(cc),
        "detail": str(cc) if cc else "no compiler found",
        "use": "compile the lifted C++ and the runtime",
        "enable": "install Visual Studio (clang-cl) or use the bundled llvm-mingw",
    })

    g, why = _find_ghidra()
    out.append({
        "id": "ghidra",
        "name": "Ghidra (headless)",
        "available": bool(g),
        "detail": str(g) if g else why,
        "use": "independent function map for hard-to-recover code (--analyzer ghidra)",
        "enable": "install Ghidra and set GHIDRA_HOME",
    })

    out.append({
        "id": "ida",
        "name": "IDA Pro (idalib, headless)",
        "available": bool(env.ida_python),
        "detail": str(env.ida_python) if env.ida_python else
                  "no interpreter with `import idapro`",
        "use": "function map / names cross-check (--analyzer ida)",
        "enable": "install IDA 9.x, activate idalib, set ida_python in config.json",
    })

    rp = _find_rpcs3()
    out.append({
        "id": "rpcs3",
        "name": "RPCS3 (reference emulator)",
        "available": bool(rp),
        "detail": str(rp) if rp else "not found (set PS3REVAMP_RPCS3)",
        "use": "behaviour oracle: run the same title to compare milestones and logs",
        "enable": "install RPCS3 or point PS3REVAMP_RPCS3 at rpcs3.exe",
    })

    out.append({
        "id": "sccache",
        "name": "sccache",
        "available": bool(getattr(env, "sccache", None)),
        "detail": str(env.sccache) if getattr(env, "sccache", None) else "not installed",
        "use": "compile cache: makes relifts and later titles much faster",
        "enable": "run `ps3revamp.py setup` (bundled by default)",
    })
    return out
