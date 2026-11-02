#!/usr/bin/env python3
"""
PS3revamp -- decrypted PS3 EBOOT.elf -> native C++ port pipeline.

One command drives the whole chain:

    analyze  -> image.json / functions.json / imports.json / readiness report
    lift     -> C++ translation units + ppu_recomp.h          (PPU static recompiler)
    hle      -> NID -> handler table                          (firmware HLE)
    spu      -> lifted SPE images                             (optional)
    project  -> buildable CMake project around the lift
    build    -> native executable (llvm-mingw / msbuild / system toolchain)

It is a driver, not a reimplementation: the heavy lifting is done by the
ps3recomp toolkit (ppu_loader, ppu_lifter, gen_hle_nids, spu_lifter) which must
be available locally.  Point at it with --toolkit, PS3RECOMP_DIR or config.json.

Note on scope: what this produces is a faithful native build of the title's
PPU/SPU code with firmware calls routed through HLE.  A title still needs
per-game work (missing NIDs, RSX behaviour, asset paths, patches).  The report
written by `port` states exactly which parts are already covered for the ELF
you gave it and which are not.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
WORK = ROOT / "work"

sys.path.insert(0, str(ROOT))
try:
    from ps3revamp_kb import KnowledgeBase
except Exception:  # pragma: no cover - the tool still runs without the KB
    KnowledgeBase = None
try:
    import ps3revamp_docs as docsmod
except Exception:
    docsmod = None

# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def eprint(*a):
    print(*a, file=sys.stderr, flush=True)


def log(stage: str, msg: str):
    print(f"[{stage:<8}] {msg}", flush=True)


def run(cmd: list[str], cwd: Path | None = None, capture: bool = True,
        env: dict | None = None) -> subprocess.CompletedProcess:
    log("exec", " ".join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                          text=True, capture_output=capture, env=env)


def run_stream(cmd: list[str], cwd: Path | None = None, env: dict | None = None,
               keep: int = 200_000, only=None) -> subprocess.CompletedProcess:
    """Run a long stage, printing progress as it happens (and keeping a tail)."""
    log("exec", " ".join(str(c) for c in cmd))
    p = subprocess.Popen([str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace",
                         bufsize=1, env=env)
    buf: list[str] = []
    assert p.stdout is not None
    for line in p.stdout:
        buf.append(line)
        if len(buf) > 5000:
            buf = buf[-2000:]
        if only is None or only(line):
            print(line, end="", flush=True)
    p.wait()
    text = "".join(buf)
    return subprocess.CompletedProcess(cmd, p.returncode, text, "")


def sha256_file(p: Path, buf: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(buf):
            h.update(chunk)
    return h.hexdigest()


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def window_title(name: str, sfo: dict | None) -> str:
    """ASCII-only title: cmd.exe/`set` and the Win32 ANSI window title are not
    UTF-8, so a trademark sign in a title would render as mojibake."""
    tid = (sfo or {}).get("TITLE_ID") or ""
    t = f"{name} [{tid}]".strip() if tid else (name or "ps3revamp")
    return t.encode("ascii", "ignore").decode().strip() or "ps3revamp"


def load_json(p: Path):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(obj, p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)


# --------------------------------------------------------------------------
# discovery: toolkit, toolchain, interpreter, IDA
# --------------------------------------------------------------------------

KNOWN_TOOLKITS = [
    ROOT / "toolkit" / "ps3recomp",
]


@dataclass
class Env:
    toolkit: Path | None = None
    toolchain: Path | None = None
    cmake: Path | None = None
    ninja: Path | None = None
    clang: Path | None = None
    clangxx: Path | None = None
    clang_cl: Path | None = None
    vc_env: dict | None = None
    vcvars: Path | None = None
    sccache: Path | None = None
    ghidra_dir: Path | None = None
    clang_vc: bool = False
    python: Path = Path(sys.executable)
    ida_python: Path | None = None   # interpreter that can `import idapro`
    problems: list[str] = field(default_factory=list)

    @property
    def tools(self) -> Path:
        return self.toolkit / "tools"

    @property
    def template(self) -> Path:
        return self.toolkit / "templates" / "project"

    def tool(self, name: str) -> Path:
        return self.tools / name


def _find_exe(*cands: Path) -> Path | None:
    for c in cands:
        if c and c.is_file():
            return c
    return None


def _importable(python: Path, module: str) -> bool:
    try:
        r = subprocess.run([str(python), "-c", f"import {module}"],
                           capture_output=True, text=True, timeout=25)
        return r.returncode == 0
    except Exception:
        return False


def _vs_vcvars() -> Path | None:
    """Locate vcvars64.bat from any Visual Studio install (2019+)."""
    roots = [Path(r"C:\Program Files\Microsoft Visual Studio"),
             Path(r"C:\Program Files (x86)\Microsoft Visual Studio")]
    for root in roots:
        if not root.is_dir():
            continue
        for vs in sorted(root.iterdir(), reverse=True):
            for ed in ("Community", "Professional", "Enterprise", "BuildTools"):
                p = vs / ed / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
                if p.is_file():
                    return p
    return None


def _vc_env(vcvars: Path) -> dict:
    """Environment after vcvars64: INCLUDE/LIB/PATH for clang-cl + lld-link."""
    r = subprocess.run(f'call "{vcvars}" >nul && set', shell=True,
                       capture_output=True, text=True, timeout=300)
    # Windows env keys are case-insensitive ("Path" vs "PATH"); normalise so the
    # vcvars values actually replace the inherited ones.
    env = {k.upper(): v for k, v in os.environ.items()}
    for line in (r.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.upper()] = v
    return env


def _clang_version(exe: Path) -> tuple[int, int, int]:
    try:
        r = subprocess.run([str(exe), "--version"], capture_output=True, text=True,
                           timeout=30)
        m = re.search(r"clang version (\d+)\.(\d+)\.(\d+)", r.stdout or "")
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3))
    except Exception:
        pass
    return (0, 0, 0)


def _best_clang_cl(candidates: list[Path | None]) -> Path | None:
    best = None
    best_v = (0, 0, 0)
    for c in candidates:
        if not c or not Path(c).is_file():
            continue
        v = _clang_version(Path(c))
        if v > best_v:
            best, best_v = Path(c), v
    return best


def discover(save: bool = False) -> Env:
    env = Env()
    cfg = {}
    if CONFIG_PATH.is_file():
        try:
            cfg = load_json(CONFIG_PATH)
        except Exception as e:
            env.problems.append(f"config.json unreadable: {e}")

    # 1. toolkit: the vendored download wins, then explicit overrides, then any
    #    portable install the user happens to have.
    cand = [ROOT / "toolkit" / "ps3recomp"]
    if os.environ.get("PS3RECOMP_DIR"):
        cand.append(Path(os.environ["PS3RECOMP_DIR"]))
    if cfg.get("ps3recomp_dir"):
        cand.append(Path(cfg["ps3recomp_dir"]))
    cand += KNOWN_TOOLKITS
    # No scanning of other folders: the tool ships its own toolkit.
    for c in cand:
        if (c / "tools" / "ppu_lifter.py").is_file():
            env.toolkit = c.resolve()
            break
    if not env.toolkit:
        env.problems.append("ps3recomp toolkit not found (tools/ppu_lifter.py)")
        return env
    # Toolkit scripts import their siblings by bare name (`import spu_elf`);
    # export the tools dir so every child process resolves them regardless of cwd.
    os.environ["PYTHONPATH"] = (str(env.toolkit / "tools") + os.pathsep +
                                os.environ.get("PYTHONPATH", ""))
    if docsmod:
        try:
            docsmod.seed(env.toolkit)
        except Exception:
            pass

    # 2. bundled toolchain (portable installs ship one next to ps3recomp/)
    tc_candidates = [ROOT / "toolchain"]
    if cfg.get("toolchain_dir"):
        tc_candidates.append(Path(cfg["toolchain_dir"]))
    tc_candidates.append(env.toolkit.parent / "toolchain")
    for tc in tc_candidates:
        if tc.is_dir():
            env.toolchain = tc
            break

    if env.toolchain:
        env.cmake = _find_exe(env.toolchain / "cmake" / "bin" / "cmake.exe")
        env.ninja = _find_exe(env.toolchain / "ninja" / "ninja.exe")
        mingw = env.toolchain / "llvm-mingw" / "bin"
        env.clang = _find_exe(mingw / "clang.exe")
        env.clangxx = _find_exe(mingw / "clang++.exe")
    env.cmake = env.cmake or _find_exe(Path(cfg.get("cmake", "")),
                                       Path(shutil.which("cmake") or ""))
    env.ninja = env.ninja or _find_exe(Path(shutil.which("ninja") or ""))
    env.clang = env.clang or _find_exe(Path(shutil.which("clang") or ""))
    env.clangxx = env.clangxx or _find_exe(Path(shutil.which("clang++") or ""))
    # Prefer the bundled LLVM 23 GNU driver (clang.exe / clang++.exe). It has the
    # MSVC target triple, so it uses the Visual Studio headers/libs and keeps the
    # ABI the runtime needs, while remaining the plain clang driver.
    llvm_clang = ROOT / "toolchain" / "llvm" / "bin" / "clang.exe"
    llvm_clangxx = ROOT / "toolchain" / "llvm" / "bin" / "clang++.exe"
    if llvm_clang.is_file() and llvm_clangxx.is_file():
        env.clang, env.clangxx = llvm_clang, llvm_clangxx
        env.clang_vc = True
    env.sccache = _find_exe(ROOT / "toolchain" / "sccache" / "sccache.exe")

    # Ghidra is used as an independent decoder for any EBOOT: it recovers
    # function starts/names the OPD table cannot. It lives inside the project
    # (toolchain/ghidra/<release>), never in a system install.
    for hl in sorted((ROOT / "toolchain" / "ghidra").glob("*/support/analyzeHeadless.bat")):
        env.ghidra_dir = hl.parent.parent
        break
    if not env.ghidra_dir:
        hl = _find_exe(Path(os.environ.get("GHIDRA_INSTALL_DIR", "")) / "support"
                       / "analyzeHeadless.bat")
        if hl:
            env.ghidra_dir = hl.parent.parent

    # 2b. Best Windows compiler: clang-cl (MSVC ABI + SEH). The toolkit's
    #     runtime uses __try/__except and the template emits #pragma comment(lib),
    #     neither of which MinGW clang supports; clang-cl gets the MSVC headers,
    #     the Windows SDK and lld-link.
    vc = None if os.environ.get("PS3REVAMP_FORCE_MINGW") else _vs_vcvars()
    if vc:
        try:
            vcenv = _vc_env(vc)
            cands: list[Path | None] = []
            if os.environ.get("PS3REVAMP_CLANG_CL"):
                cands.append(Path(os.environ["PS3REVAMP_CLANG_CL"]))
            if os.environ.get("LLVM_HOME"):
                cands.append(Path(os.environ["LLVM_HOME"]) / "bin" / "clang-cl.exe")
            cands.append(ROOT / "toolchain" / "llvm" / "bin" / "clang-cl.exe")
            cc = shutil.which("clang-cl", path=vcenv.get("PATH", ""))
            if cc:
                cands.append(Path(cc))
            vsroot = vc.parents[3]
            cands += [vsroot / "VC" / "Tools" / "LLVM" / "x64" / "bin" / "clang-cl.exe",
                      vsroot / "VC" / "Tools" / "LLVM" / "bin" / "clang-cl.exe"]
            best = _best_clang_cl(cands)
            if best:
                env.clang_cl = best
                env.vc_env = vcenv
                env.vcvars = vc
        except Exception as e:
            env.problems.append(f"vcvars failed: {e}")

    # 3. python for the toolkit tools: prefer an interpreter that actually has
    #    the pipeline deps, not merely the one that ships with a portable install.
    py_candidates = [ROOT / "python" / "python.exe"]
    if cfg.get("python"):
        py_candidates.append(Path(cfg["python"]))
    py_candidates.append(Path(sys.executable))
    if env.toolchain and (env.toolchain / "python" / "python.exe").is_file():
        py_candidates.append(env.toolchain / "python" / "python.exe")
    for name in ("python3", "python"):
        exe = shutil.which(name)
        if exe:
            py_candidates.append(Path(exe))
    for py in py_candidates:
        if py.is_file() and all(_importable(py, m) for m in ("capstone", "construct", "Crypto")):
            env.python = py
            break
    else:
        env.python = py_candidates[0] if py_candidates else Path(sys.executable)

    # 4. IDA: an interpreter that can import idapro (idalib)
    if cfg.get("ida_python"):
        p = Path(cfg["ida_python"])
        if _importable(p, "idapro"):
            env.ida_python = p
    if not env.ida_python:
        for name in ("python3.11", "python3.12", "py"):
            exe = shutil.which(name)
            if exe and _importable(Path(exe), "idapro"):
                env.ida_python = Path(exe)
                break

    if save:
        dump_json({
            "ps3recomp_dir": str(env.toolkit),
            "toolchain_dir": str(env.toolchain) if env.toolchain else None,
            "python": str(env.python),
            "ida_python": str(env.ida_python) if env.ida_python else None,
        }, CONFIG_PATH)
    return env


# --------------------------------------------------------------------------
# PS3 ELF + PARAM.SFO metadata
# --------------------------------------------------------------------------

PS3_LIB_HINTS = {
    "cellGcmSys", "cellSpurs", "cellSysutil", "cellFs", "cellPad", "cellAudio",
    "cellPngDec", "cellJpgDec", "cellFont", "cellFontFT", "cellFreeType",
    "cellNetCtl", "cellHttp", "cellHttpUtil", "cellSsl", "cellSysmodule",
    "cellSaveData", "cellGame", "cellResc", "cellDaisy", "cellMic", "cellVdec",
    "cellAdec", "cellDmux", "cellVpost", "cellUsbd", "cellUsbPspcm",
    "sceNp", "sceNpTrophy", "sceNpCommerce2", "sceNpClans", "sceNpScore",
    "sysPrxForUser", "sys_io", "sys_fs", "sys_net", "sys_memory", "sys_mutex",
    "sys_ppu_thread", "sys_process", "sys_spu", "sys_timer", "sys_event",
    "libsre", "libm", "libc",
}


def parse_sfo(path: Path) -> dict:
    """PARAM.SFO -> {KEY: value}."""
    try:
        d = path.read_bytes()
        if d[:4] != b"\x00PSF":
            return {}
        _ver, key_start, data_start, count = struct.unpack_from("<IIII", d, 4)
        out = {}
        for i in range(count):
            off = 20 + i * 16
            key_off, fmt, _len, _max, data_off = struct.unpack_from("<HHIII", d, off)
            key = d[key_start + key_off:key_start + key_off + 32].split(b"\x00")[0]
            raw = d[data_start + data_off:data_start + data_off + _max]
            key = key.decode("utf-8", "replace")
            if fmt in (0x0004, 0x0204, 0x0404):
                out[key] = raw.split(b"\x00")[0].decode("utf-8", "replace").strip()
            elif fmt == 0x0402:
                out[key] = struct.unpack_from("<i", raw)[0] if len(raw) >= 4 else 0
            else:
                out[key] = raw
        return out
    except Exception:
        return {}


@dataclass
class PpuElf:
    path: Path
    size: int
    entry: int
    phnum: int
    shnum: int
    segments: list[dict]
    text_end: int
    sec_names: list[str]
    has_symtab: bool
    sha256: str

    @property
    def text_bytes(self) -> int:
        return sum(s["filesz"] for s in self.segments if s["flags"] & 1)


def infer_game_dir(elf: Path) -> Path | None:
    """From .../<GAME>/USRDIR/EBOOT.elf find <GAME>, either layout."""
    for up in (elf.parent, elf.parent.parent, elf.parent.parent.parent):
        if (up / "USRDIR").is_dir() or (up / "PARAM.SFO").is_file():
            return up
    return None


def find_sfo(game: Path | None, elf: Path) -> dict:
    if not game:
        return {}
    for p in (game / "PARAM.SFO", game.parent / "PARAM.SFO",
              game / "PS3_GAME" / "PARAM.SFO"):
        if p.is_file():
            s = parse_sfo(p)
            if s:
                return s
    return {}


def ensure_elf(env: Env, path: Path) -> Path:
    """Return a decrypted ELF: pass ELF through, decrypt SELF with the toolkit."""
    with open(path, "rb") as f:
        magic = f.read(4)
    if magic == b"\x7fELF":
        return path
    if magic[:3] == b"SCE" or magic == b"SCE\x00":
        WORK.mkdir(parents=True, exist_ok=True)
        dst = WORK / (path.stem + ".elf")
        log("decrypt", f"{path.name} is a SELF container; decrypting with unfself")
        r = run([env.python, env.tool("unfself.py"), path, "--output", dst])
        if r.returncode == 0 and dst.is_file() and dst.read_bytes()[:4] == b"\x7fELF":
            log("decrypt", f"wrote {dst}")
            return dst
        raise RuntimeError(
            "could not decrypt the SELF. Dump a decrypted EBOOT.elf (RPCS3: "
            "dev_hdd0/game/<TITLE_ID>/USRDIR/EBOOT.elf) or supply the keys the "
            "toolkit's unfself.py expects.\n" + (r.stderr or r.stdout)[-800:])
    raise ValueError(f"{path.name}: not an ELF or SELF (magic {magic!r})")


def read_ps3_elf(path: Path) -> PpuElf:
    d = path.read_bytes()
    if d[:4] != b"\x7fELF":
        raise ValueError(f"{path.name}: not an ELF (magic {d[:4]!r}). "
                         "Decrypt EBOOT.BIN with tools/unfself.py first.")
    if d[4] != 2 or d[5] != 2:
        raise ValueError("not ELF64 big-endian (PS3 PPU)")
    (etype, machine, _ver, entry, phoff, shoff, _flags, _eh, phent,
     phnum, shent, shnum, shstrndx) = struct.unpack_from(">HHIQQQIHHHHHH", d, 16)
    if machine != 21:
        raise ValueError(f"machine {machine} is not PowerPC64")

    segs = []
    for i in range(phnum):
        p_type, p_flags, p_off, p_vaddr, _pa, p_filesz, p_memsz, p_align = \
            struct.unpack_from(">IIQQQQQQ", d, phoff + i * phent)
        if p_type == 1:
            segs.append({"vaddr": p_vaddr, "filesz": p_filesz, "memsz": p_memsz,
                         "flags": p_flags, "offset": p_off})

    # section headers (names only; ps3recomp does the real parsing)
    names = []
    try:
        strtab_off = struct.unpack_from(">Q", d, shoff + shstrndx * shent + 24)[0]
        for i in range(shnum):
            name_off = struct.unpack_from(">I", d, shoff + i * shent)[0]
            end = d.index(b"\x00", strtab_off + name_off)
            names.append(d[strtab_off + name_off:end].decode("latin-1"))
    except Exception:
        pass

    text_end = max((s["vaddr"] + s["filesz"] for s in segs if s["flags"] & 1),
                   default=0)
    return PpuElf(path=path, size=len(d), entry=entry, phnum=phnum, shnum=shnum,
                  segments=segs, text_end=text_end, sec_names=names,
                  has_symtab=".symtab" in names, sha256=sha256_file(path))


# --------------------------------------------------------------------------
# analysis: imports, coverage, function map
# --------------------------------------------------------------------------


def hle_nid_coverage(env: Env, imports: list[dict]) -> dict:
    """Generate the canonical HLE table into work/ and see which NIDs it answers."""
    out = WORK / "_hle_probe.cpp"
    WORK.mkdir(parents=True, exist_ok=True)
    try:
        r = run([env.python, env.tool("gen_hle_nids.py"), "--all", "--out", out])
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout)[-800:])
        text = out.read_text(encoding="utf-8", errors="replace")
        table = {int(x, 16) for x in re.findall(r"0x([0-9A-Fa-f]{8})", text)}
    except Exception as e:
        return {"ok": False, "error": str(e), "covered": 0, "total": len(imports)}
    have = [i for i in imports if int(i["nid"], 16) in table]
    missing = [i for i in imports if int(i["nid"], 16) not in table]
    by_lib: dict[str, dict] = {}
    for i in imports:
        b = by_lib.setdefault(i["library"], {"total": 0, "covered": 0})
        b["total"] += 1
        if int(i["nid"], 16) in table:
            b["covered"] += 1
    return {
        "ok": True, "covered": len(have), "total": len(imports),
        "percent": round(100.0 * len(have) / max(1, len(imports)), 1),
        "by_library": dict(sorted(by_lib.items(),
                                  key=lambda kv: kv[1]["total"] - kv[1]["covered"],
                                  reverse=True)),
        "missing": [{"library": m["library"], "nid": m["nid"]} for m in missing],
    }


SUBSYSTEMS = {
    "graphics": ("cellGcmSys", "cellResc", "cellDisp", "cellGcm"),
    "spu": ("cellSpurs", "cellSpursJq", "libsre", "cellSync", "cellSync2"),
    "audio": ("cellAudio", "cellAdec", "cellDmux", "cellAtrac", "cellAtrac3"),
    "video": ("cellVdec", "cellPngDec", "cellJpgDec", "cellGifDec", "cellMp4"),
    "input": ("cellPad", "cellKb", "cellMouse", "cellUsbd", "cellMic"),
    "storage": ("sys_fs", "cellFs", "cellSaveData", "cellGame", "cellSysmodule"),
    "system": ("cellSysutil", "sysPrxForUser", "sys_io", "sys_memory",
               "sys_mutex", "sys_ppu_thread", "sys_process", "sys_spu",
               "sys_timer", "sys_event", "sys_event_queue"),
    "network": ("sceNp", "sceNpTrophy", "sceNpCommerce2", "sceNpScore",
                "cellNetCtl", "cellHttp", "cellHttpUtil", "cellSsl", "sys_net"),
}
SUBSYSTEM_NOTE = {
    "graphics": "RSX command stream -> D3D12; visual gaps and shader quirks live here",
    "spu": "cellSpurs dispatch; SPU images must be lifted or the job falls back",
    "audio": "WASAPI/SDL output; decoders are usually stub-tolerant",
    "video": "video decode; titles often play without it",
    "input": "pads/keyboard; safe to stub for a first boot",
    "storage": "VFS + save data; asset paths must resolve to the game folder",
    "system": "threads, memory, UI callbacks; the runtime implements these",
    "network": "PSN/HTTP; almost always stubbed for offline ports",
    "other": "misc firmware",
}


def resolve_nids(env: Env, imports: list[dict]) -> dict:
    """NID -> {module, name} using the toolkit's database."""
    if not imports:
        return {}
    WORK.mkdir(parents=True, exist_ok=True)
    f = WORK / "nids.txt"
    f.write_text("\n".join(i["nid"] for i in imports), encoding="utf-8")
    r = run([env.python, env.tool("nid_database.py"), "--batch", f, "--json"])
    if r.returncode != 0:
        return {}
    try:
        return {k.upper(): v for k, v in json.loads(r.stdout or "{}").items()}
    except Exception:
        return {}


def subsystem_of(lib: str) -> str:
    for name, libs in SUBSYSTEMS.items():
        if lib in libs or any(lib.startswith(x) for x in libs):
            return name
    return "other"


def merge_functions(seed: list[dict], extra: list[dict]) -> list[dict]:
    """Union two function lists by start address; keep the larger extent."""
    by_start: dict[int, dict] = {}
    for f in seed:
        by_start[int(f["start"], 16)] = dict(f)
    for f in extra:
        a = int(f["start"], 16)
        if a in by_start:
            cur = by_start[a]
            if int(f.get("end", "0x0"), 16) > int(cur.get("end", "0x0"), 16):
                cur["end"] = f["end"]
        else:
            by_start[a] = dict(f)
    return [by_start[a] for a in sorted(by_start)]


def port_plan(imports: list[dict], cov: dict, names: dict) -> dict:
    """Group the title's firmware surface by subsystem and flag the risk."""
    cov_map = {}
    for lib, c in (cov.get("by_library") or {}).items():
        cov_map[lib] = c
    plan: dict[str, dict] = {}
    for i in imports:
        sub = subsystem_of(i["library"])
        p = plan.setdefault(sub, {"libraries": {}, "imports": 0, "covered": 0,
                                  "names": []})
        p["imports"] += 1
        lib = p["libraries"].setdefault(i["library"], {"imports": 0, "covered": 0})
        lib["imports"] += 1
        c = cov_map.get(i["library"])
        if c and c["covered"]:
            lib["covered"] += 1
            p["covered"] += 1
        nm = (names.get(i["nid"].upper()) or {}).get("name")
        if nm:
            p["names"].append(f"{i['library']}.{nm}")
    for sub, p in plan.items():
        p["note"] = SUBSYSTEM_NOTE.get(sub, SUBSYSTEM_NOTE["other"])
        p["libraries"] = dict(sorted(p["libraries"].items(),
                                     key=lambda kv: kv[1]["imports"], reverse=True))
        p["risk"] = ("none" if p["imports"] == p["covered"] else
                     "some" if p["covered"] else "high")
    return dict(sorted(plan.items(), key=lambda kv: -kv[1]["imports"]))



    """Union two function lists by start address; keep the larger extent."""
    by_start: dict[int, dict] = {}
    for f in seed:
        by_start[int(f["start"], 16)] = dict(f)
    for f in extra:
        a = int(f["start"], 16)
        if a in by_start:
            cur = by_start[a]
            if int(f.get("end", "0x0"), 16) > int(cur.get("end", "0x0"), 16):
                cur["end"] = f["end"]
        else:
            by_start[a] = dict(f)
    out = [by_start[a] for a in sorted(by_start)]
    for f in out:
        f.setdefault("toc", f"0x{0:08X}")
    return out


# --------------------------------------------------------------------------
# analyzers (IDA / Ghidra / native)
# --------------------------------------------------------------------------


def analyze_with_ida(env: Env, elf: Path, outdir: Path) -> tuple[list[dict], dict]:
    """Headless IDA via idalib (tools/ida_analyze.py). Returns (functions, status)."""
    if not env.ida_python:
        return [], {
            "used": False,
            "reason": "no interpreter with idalib (`import idapro`) found. "
                      "Install IDA Pro 9.x, activate idalib in its Python, then "
                      "set ida_python in config.json. The REA bridge also rejects "
                      "PPC64 big-endian ELF, so analyze through idalib directly.",
        }
    outdir.mkdir(parents=True, exist_ok=True)
    r = run([env.ida_python, env.tool("ida_analyze.py"), elf, "-o", outdir])
    f = outdir / "functions.json"
    if r.returncode != 0 or not f.is_file():
        return [], {"used": False, "reason": (r.stderr or r.stdout)[-800:]}
    funcs = load_json(f)
    return [{"start": x["addr"], "end": f"0x{int(x['addr'],16)+int(x['size']):X}",
             "name": x.get("name", "")} for x in funcs], {
        "used": True, "count": len(funcs), "source": "idalib"}


def analyze_with_ghidra(env: Env, elf: Path, outdir: Path) -> tuple[list[dict], dict]:
    """Independent decode of any decrypted EBOOT through Ghidra headless.

    Ghidra recovers function starts from CFG/prologue patterns that the OPD
    table misses (dead code, tail entries, PRX-style images) and gives readable
    names for the lifted output. It runs from the copy inside this project.
    """
    if not env.ghidra_dir:
        return [], {"used": False,
                    "reason": "Ghidra not installed in this project; run "
                              "`ps3revamp.py setup --with-ghidra`"}
    outdir.mkdir(parents=True, exist_ok=True)
    r = run([env.python, env.tool("ghidra_analyze.py"), elf, "-o", outdir,
             "--ghidra-dir", env.ghidra_dir],
            env={**os.environ, "GHIDRA_INSTALL_DIR": str(env.ghidra_dir)})
    f = outdir / "functions.json"
    if r.returncode != 0 or not f.is_file():
        return [], {"used": False, "reason": (r.stderr or r.stdout)[-800:]}
    raw = load_json(f)
    funcs = []
    for x in raw:
        try:
            addr = int(str(x.get("addr") or x.get("address")), 0)
            size = int(x.get("size") or 0)
        except Exception:
            continue
        funcs.append({"start": f"0x{addr:08X}", "end": f"0x{addr + size:08X}",
                      "name": x.get("name", "")})
    return funcs, {"used": True, "count": len(funcs), "source": "ghidra",
                   "dir": str(env.ghidra_dir)}


# --------------------------------------------------------------------------
# project scaffolding + build
# --------------------------------------------------------------------------


CMAKE_EXTRA = """
# ---- PS3revamp -----------------------------------------------------------
# Generated by ps3revamp. Keep per-port tweaks here instead of editing
# CMakeLists.txt, so the file can be regenerated from the toolkit template.
"""


def scaffold_project(env: Env, out: Path, recompiled: Path, name: str,
                     elf: Path, game_dir: Path | None) -> Path:
    proj = out / "project"
    if proj.exists():
        # Keep the previous build tree: re-running the pipeline without --build
        # must not throw away a working executable (sccache/ninja reuse it).
        for child in proj.iterdir():
            if child.name == "build":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    else:
        proj.mkdir(parents=True, exist_ok=True)
    shutil.copytree(env.template, proj, dirs_exist_ok=True)
    # The lift stays where it was written (it can be gigabytes); the preset
    # points CMake's RECOMP_DIR at it instead of copying/symlinking it.

    # presets so the project builds without remembering the toolchain paths
    gen = {
        "version": 6,
        "configurePresets": [{
            "name": "ps3revamp",
            "displayName": "PS3revamp native",
            "generator": "Ninja",
            "binaryDir": "${sourceDir}/build",
            "cacheVariables": {
                "CMAKE_BUILD_TYPE": "Release",
                "PS3RECOMP_DIR": str(env.toolkit),
                "RECOMP_DIR": str(recompiled),
            },
        }]
    }
    if (not env.clang_cl) and env.cmake and env.ninja and env.clang and env.clangxx:
        gen["configurePresets"][0]["cacheVariables"].update({
            "CMAKE_MAKE_PROGRAM": str(env.ninja),
            "CMAKE_C_COMPILER": str(env.clang),
            "CMAKE_CXX_COMPILER": str(env.clangxx),
        })
    dump_json(gen, proj / "CMakePresets.json")

    # A one-click build script for the case the preset cannot express: clang-cl
    # needs the Visual Studio environment (INCLUDE/LIB/PATH).
    if env.clang_cl and env.vcvars and env.cmake and env.ninja:
        (proj / "build.cmd").write_text(
            "@echo off\r\n"
            f'call "{env.vcvars}" >nul || exit /b 1\r\n'
            f'"{env.cmake}" -S . -B build -G Ninja '
            f'-DCMAKE_MAKE_PROGRAM="{env.ninja}" '
            f'-DCMAKE_C_COMPILER="{env.clang_cl}" '
            f'-DCMAKE_CXX_COMPILER="{env.clang_cl}" '
            f'-DPS3RECOMP_DIR="{env.toolkit}" -DRECOMP_DIR="{recompiled}" '
            f'-DCMAKE_BUILD_TYPE=Release || exit /b 1\r\n'
            f'"{env.cmake}" --build build --parallel\r\n',
            encoding="utf-8")

    # Two gaps in the template for a MinGW/clang toolchain on Windows:
    #  * the D3D12 backend declares its imports with #pragma comment(lib,...),
    #    which only MSVC honours -- link them explicitly;
    #  * GetCurrentThreadStackLimits is declared behind _WIN32_WINNT >= 0x0602,
    #    and MinGW defaults to 0x0502.
    with open(proj / "CMakeLists.txt", "a", encoding="utf-8") as f:
        f.write("""
# ---- appended by ps3revamp -----------------------------------------------
set(PS3REVAMP_LIFT_FLAGS "-O2" CACHE STRING "optimisation for lifted TUs")
if(WIN32)
  add_compile_definitions(_WIN32_WINNT=0x0A00 NTDDI_VERSION=0x0A000000)
  if(NOT MSVC)
    target_link_libraries(${PROJECT_NAME} PRIVATE d3d12 dxgi d3dcompiler)
  endif()
endif()
if(MSVC)
  set_source_files_properties(${RECOMP_SOURCES} PROPERTIES
      COMPILE_OPTIONS "${PS3REVAMP_LIFT_FLAGS};/w;/bigobj")
else()
  set_source_files_properties(${RECOMP_SOURCES} PROPERTIES
      COMPILE_OPTIONS "${PS3REVAMP_LIFT_FLAGS};-w;-ffp-contract=off")
endif()
""")

    (proj / "PORT.txt").write_text(
        f"title      : {name}\n"
        f"eboot      : {elf}\n"
        f"game dir   : {game_dir or '(none given -- pass the game folder or EBOOT path at runtime)'}\n"
        f"lift       : {recompiled}\n"
        f"toolkit    : {env.toolkit}\n", encoding="utf-8")
    dump_json({"recompiled": str(recompiled), "toolkit": str(env.toolkit)},
              proj / "ps3revamp.json")

    # Explicit SPU registration. A constructor attribute is not portable
    # (clang-cl/MSVC ignore __attribute__((constructor)) and /OPT:REF can drop
    # the .CRT$XCU slot), and an unregistered image is silently DROPPED at
    # dispatch, leaving cellSpurs waiting forever. Call it from main instead.
    main_cpp = proj / "main.cpp"
    if main_cpp.is_file():
        text = main_cpp.read_text(encoding="utf-8", errors="replace")
        changed = False
        if (out / "spu" / "spu_workloads.c").is_file() and "spu_register_all" not in text:
            import re as _re
            text = _re.sub(r"(?m)^(void\s+ppu_recomp_register\(void\).*)$",
                           r"\1\nvoid     spu_register_all(void);   "
                           r"/* generated: lifted SPU images */", text, count=1)
            text = text.replace(
                "ppu_recomp_register();   /* lifted function table -> address map */",
                "ppu_recomp_register();   /* lifted function table -> address map */\n"
                "    spu_register_all();      /* register lifted SPU images before any dispatch */", 1)
            changed = True
        # The window title must name the title, including after the backend's
        # once-a-second titlebar refresh (which formats g_rsx_title_base).
        if "g_rsx_title_base" not in text:
            text = text.replace(
                "void     spu_register_all(void);   /* generated: lifted SPU images */",
                "void     spu_register_all(void);   /* generated: lifted SPU images */\n"
                'extern "C" char g_rsx_title_base[128];   /* D3D12 titlebar text */', 1)
            text = text.replace(
                '    if (!title || !*title) title = "ps3recomp";',
                '    if (!title || !*title) title = PS3REVAMP_DEFAULT_TITLE;\n'
                '    snprintf(g_rsx_title_base, sizeof g_rsx_title_base, "%s", title);\n'
                '    fprintf(stderr, "[ps3revamp] window title: %s\\n", title);', 1)
            dflt = window_title(name, find_sfo(game_dir, elf))
            if "#define PS3REVAMP_DEFAULT_TITLE" not in text:
                text = text.replace(
                    "static DWORD WINAPI frame_clock(LPVOID)",
                    '#ifndef PS3REVAMP_DEFAULT_TITLE\n'
                    f'#define PS3REVAMP_DEFAULT_TITLE "{dflt}"\n'
                    "#endif\n\n"
                    "static DWORD WINAPI frame_clock(LPVOID)", 1)
            changed = True
        # A packaged port is a normal game folder: exe at the root,
        # PS3_GAME/USRDIR beside it. With no argument the exe finds its own
        # EBOOT.elf, so double-clicking works like any Windows game.
        if "s_default_elf" not in text:
            old = ('    if (argc < 2) {\n'
                   '        printf("usage: %s <PPU ELF>\\n", argv[0]);\n'
                   '        return 2;\n'
                   '    }')
            new = ('#ifdef _WIN32\n'
                   '    static char s_default_elf[1024];\n'
                   '    if (argc < 2) {\n'
                   '        char exe_path[1024];\n'
                   '        if (GetModuleFileNameA(NULL, exe_path, sizeof exe_path)) {\n'
                   '            char* slash = strrchr(exe_path, \'\\\\\');\n'
                   '            if (slash) *slash = 0;\n'
                   '            snprintf(s_default_elf, sizeof s_default_elf,\n'
                   '                     "%s\\\\PS3_GAME\\\\USRDIR\\\\EBOOT.elf", exe_path);\n'
                   '            FILE* f = fopen(s_default_elf, "rb");\n'
                   '            if (!f) {\n'
                   '                snprintf(s_default_elf, sizeof s_default_elf,\n'
                   '                         "%s\\\\EBOOT.elf", exe_path);\n'
                   '                f = fopen(s_default_elf, "rb");\n'
                   '            }\n'
                   '            if (f) { fclose(f); argv[1] = s_default_elf; argc = 2; }\n'
                   '        }\n'
                   '    }\n'
                   '#endif\n'
                   '    if (argc < 2) {\n'
                   '        printf("usage: %s <PPU ELF>\\n", argv[0]);\n'
                   '        return 2;\n'
                   '    }')
            if old in text:
                text = text.replace(old, new, 1)
                changed = True
        if changed:
            main_cpp.write_text(text, encoding="utf-8")
            log("stage", "main.cpp patched (SPU registration, title, argless boot)")
    return proj


def build_project(env: Env, proj: Path, jobs: int, fast: bool = False,
                  opt: int = 1) -> tuple[bool, str]:
    """Configure + build. clang-cl/MSVC-ABI is preferred on Windows."""
    lift_opt = 0 if fast else max(0, min(3, opt))
    flags = ["-DNDEBUG"]
    use_vc = bool(env.clang_cl and env.vc_env and
                  os.environ.get("PS3REVAMP_USE_CLANG_CL"))
    if use_vc:
        cc = env.clang_cl
        runtime_opt = "/Od" if fast else "/O2"
        lift_flags = "/Od" if fast else f"-O{lift_opt}"
        flags = [runtime_opt, "/DNDEBUG", "/Gy", "/Gw"]
    else:
        if not (env.cmake and env.ninja and env.clang and env.clangxx):
            return False, ("no usable C++ toolchain: install Visual Studio (clang-cl) "
                           "or clang + cmake + ninja, then rerun `doctor`")
        cc = None
        runtime_opt = "-O0" if fast else "-O1"
        lift_flags = f"-O{lift_opt}" if lift_opt else "-O0"
        flags = [runtime_opt, "-DNDEBUG"]

    # Windows SDK guards (GetCurrentThreadStackLimits is behind _WIN32_WINNT
    # >= 0x0602 and MinGW defaults to 0x0502). Applied to every target, because
    # the toolkit's runtime is a separate target from the game project.
    if sys.platform == "win32":
        defines = (["/D_WIN32_WINNT=0x0A00", "/DNTDDI_VERSION=0x0A000000"] if use_vc
                   else ["-D_WIN32_WINNT=0x0A00", "-DNTDDI_VERSION=0x0A000000"])
    else:
        defines = []

    cmd = [env.cmake, "-S", proj, "-B", proj / "build", "-G", "Ninja",
           f"-DCMAKE_MAKE_PROGRAM={env.ninja}"]
    if use_vc:
        cmd += [f"-DCMAKE_C_COMPILER={cc}", f"-DCMAKE_CXX_COMPILER={cc}",
                "-DCMAKE_C_STANDARD=17", "-DCMAKE_CXX_STANDARD=20",
                f"-DCMAKE_LINKER={Path(cc).with_name('lld-link.exe')}",
                f"-DCMAKE_AR={Path(cc).with_name('llvm-lib.exe')}"]
    else:
        cmd += [f"-DCMAKE_C_COMPILER={env.clang}",
                f"-DCMAKE_CXX_COMPILER={env.clangxx}"]
    cmd += ["-DCMAKE_BUILD_TYPE=Release",
            f"-DCMAKE_C_FLAGS={' '.join(defines)}",
            f"-DCMAKE_CXX_FLAGS={' '.join(defines)}",
            f"-DCMAKE_C_FLAGS_RELEASE={' '.join(flags)}",
            f"-DCMAKE_CXX_FLAGS_RELEASE={' '.join(flags)}",
            "-DCMAKE_INTERPROCEDURAL_OPTIMIZATION=OFF",
            f"-DPS3REVAMP_LIFT_FLAGS={lift_flags}",
            f"-DPS3RECOMP_DIR={env.toolkit}"]
    # The lift lives beside the project, not inside it (it can be huge).
    meta = proj / "ps3revamp.json"
    if meta.is_file():
        try:
            m = load_json(meta)
            if m.get("recompiled"):
                cmd.append(f"-DRECOMP_DIR={m['recompiled']}")
        except Exception:
            pass
    if (proj / "spu").is_dir():
        cmd.append(f"-DSPU_DIR={proj / 'spu'}")
    # The compiler launcher must not be present for the MSVC-target GNU driver:
    # CMake's C++ ABI probe fails when sccache wraps it, and the cached build
    # then fails to create its object directories. sccache stays on for clang-cl.
    build_env = dict(env.vc_env or os.environ)
    build_env["SCCACHE_DIR"] = str(ROOT / "cache" / "sccache")
    (ROOT / "cache" / "sccache").mkdir(parents=True, exist_ok=True)
    if env.sccache and not env.clang_vc:
        cmd += [f"-DCMAKE_C_COMPILER_LAUNCHER={env.sccache}",
                f"-DCMAKE_CXX_COMPILER_LAUNCHER={env.sccache}"]
    r = run_stream(cmd, env=build_env,
                   only=lambda l: l.startswith("[") or "error" in l.lower())
    if r.returncode != 0:
        return False, (r.stdout or "")[-8000:]
    # A large lift is hundreds of MB of C++; each TU can need several GB at -O2.
    # Cap the number of concurrent compiles so the machine does not swap.
    src = proj / "ps3revamp.json"
    total = 0
    try:
        rec = Path(load_json(src)["recompiled"])
        total = sum(p.stat().st_size for p in rec.glob("*.cpp"))
    except Exception:
        pass
    parallel = jobs
    if total > 200 << 20:
        parallel = min(jobs, max(4, (os.cpu_count() or 8) // 2))
        log("build", f"large lift ({human(total)}); compiling {parallel} TUs at a time")
    r = run_stream([env.cmake, "--build", proj / "build", "--parallel", str(parallel)],
                   env=build_env,
                   only=lambda l: l.startswith("[") or "error" in l.lower())
    ok = r.returncode == 0
    return ok, ((r.stdout or "") + (r.stderr or ""))[-6000:]


# --------------------------------------------------------------------------
# pipeline
# --------------------------------------------------------------------------


def stage_loader(env: Env, elf: Path, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    r = run([env.python, env.tool("ppu_loader.py"), elf, "-o", out])
    if r.returncode != 0:
        raise RuntimeError("ppu_loader failed:\n" + (r.stderr or r.stdout)[-2000:])
    print((r.stdout or "").strip())
    stem = elf.stem
    return {
        "functions": out / f"{stem}.functions.json",
        "imports": out / f"{stem}.imports.json",
        "image": out / f"{stem}.image.json",
        "loader": out / f"{stem}.loader.json",
    }


def stage_find_functions(env: Env, elf: Path, seed: Path | None, out: Path) -> Path:
    """CFG/prologue scan for images with no (or unhelpful) OPD table."""
    cmd = [env.python, env.tool("find_functions.py"), elf, "--json", "-o", out]
    if seed and seed.is_file():
        cmd += ["--seed-json", seed]
    r = run(cmd)
    if r.returncode != 0 or not out.is_file():
        raise RuntimeError("find_functions failed:\n" + (r.stderr or r.stdout)[-1500:])
    print((r.stdout or "").strip()[-1200:])
    return out


def stage_lift(env: Env, elf: Path, funcs: Path, imports: Path | None, out: Path,
               jobs: int, chunk_lines: int | None, names: Path | None,
               code_end: int | None, extra: list[str],
               chunks: int | None = None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    cmd = [env.python, env.tool("ppu_lifter.py"), elf,
           "--functions", funcs, "--output", out, "--jobs", str(jobs)]
    if imports:
        cmd += ["--hle-stubs", imports]
    if chunks:
        cmd += ["--chunks", str(chunks)]
    elif chunk_lines:
        cmd += ["--chunk-lines", str(chunk_lines)]
    if names:
        cmd += ["--names", names]
    if code_end:
        cmd += ["--code-end", hex(code_end)]
    cmd += extra
    lift_env = dict(os.environ)
    lift_env["PS3RECOMP_MID_JOBS"] = str(jobs)   # parallel mid-function pass
    r = run_stream(cmd, env=lift_env,
                   only=lambda l: ("lifted" in l or "%" in l or "jump tables" in l
                                   or "Wrote" in l or "Mid-function" in l))
    if r.returncode != 0:
        raise RuntimeError("ppu_lifter failed:\n" + (r.stdout or "")[-2500:])
    print((r.stdout or "").strip()[-4000:])


def stage_spu_extract(env: Env, elf: Path, out: Path) -> list[Path]:
    images = out / "images"
    images.mkdir(parents=True, exist_ok=True)
    r = run([env.python, env.tool("extract_spu_images.py"), elf, "--output", images])
    if r.returncode != 0:
        log("spu", "extract failed: " + (r.stderr or r.stdout)[-300:])
        return []
    return sorted(images.glob("*.elf"))


def lift_spu_images(env: Env, images: list[Path], out: Path, jobs: int) -> dict:
    """Lift every SPU image and emit the content-fingerprint registry.

    `build_spu_workloads.py` is the toolkit's supported path: it lifts each
    image under a per-image symbol prefix and writes `spu_workloads.c`, which
    registers every image by FNV-1a-64 fingerprint with a constructor. Without
    that registry the runtime cannot match a program the title loads at run
    time and drops it ("no lifted image ... DROPPED"), which is exactly what
    leaves cellSpurs waiting forever.
    """
    if not images:
        return {"ok": True, "images": 0, "lifted": 0}
    img_dir = out / "images"
    r = run([env.python, env.tool("build_spu_workloads.py"),
             "--images", img_dir,
             "--lifted", out,
             "--out", out / "spu_workloads.c",
             "--constructor"])
    if r.returncode != 0:
        log("spu", "build_spu_workloads failed: " + (r.stderr or r.stdout)[-500:])
        return {"ok": False, "images": len(images), "lifted": 0,
                "error": (r.stderr or r.stdout)[-800:]}
    lifted = sum(1 for p in out.glob("*/spu_recomp.c"))
    reg = out / "spu_workloads.c"
    return {"ok": reg.is_file(), "images": len(images), "lifted": lifted,
            "registry": str(reg) if reg.is_file() else None}


def stage_spu(env: Env, elf: Path, out: Path, jobs: int) -> dict:
    return lift_spu_images(env, stage_spu_extract(env, elf, out), out, jobs)


def port(args) -> int:
    t0 = time.time()
    env = discover()
    if env.problems:
        for p in env.problems:
            eprint("! " + p)
    if not env.toolkit:
        eprint("No ps3recomp toolkit. Pass --toolkit <path> or run `ps3revamp doctor`.")
        return 2

    elf_in = Path(args.elf).resolve()
    if not elf_in.is_file():
        eprint(f"ELF not found: {elf_in}")
        return 2
    try:
        elf = ensure_elf(env, elf_in)
        meta = read_ps3_elf(elf)
    except Exception as e:
        eprint(f"! {e}")
        return 2

    game_dir = (Path(args.game).resolve() if args.game
                else infer_game_dir(elf_in) or infer_game_dir(elf))
    sfo = find_sfo(game_dir, elf) or find_sfo(game_dir, elf_in)
    name = args.name or sfo.get("TITLE") or elf.stem
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "port"
    log("stage", f"title: {name}  [{sfo.get('TITLE_ID', 'unknown')}]")
    if game_dir:
        log("stage", f"game : {game_dir}")

    out = Path(args.out).resolve() if args.out else ROOT / "ports" / safe
    out.mkdir(parents=True, exist_ok=True)
    analysis = out / "analysis"
    recompiled = out / "recompiled"
    report = {"title": name, "sfo": sfo, "elf": str(elf),
              "game_dir": str(game_dir) if game_dir else None,
              "elf_sha256": meta.sha256, "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    timings: dict[str, float] = {}
    _last = [time.time()]

    def _mark(stage: str):
        now = time.time()
        timings[stage] = round(now - _last[0], 1)
        _last[0] = now
        log("time", f"{stage}: {timings[stage]}s")

    log("stage", "1/6 loader -- image, OPD functions, firmware imports")
    files = stage_loader(env, elf, analysis)
    functions = load_json(files["functions"])
    imports = load_json(files["imports"])
    if not functions:
        log("stage", "      no OPD table; running CFG/prologue function scan")
        files["functions"] = stage_find_functions(
            env, elf, files.get("functions"), analysis / "functions.found.json")
        functions = load_json(files["functions"])
    if not functions:
        raise RuntimeError("no functions found in this ELF -- it may be a stub, "
                           "a raw payload, or not a PS3 PPU image.")
    # Functions recovered without an OPD have no per-function TOC; the module
    # TOC is the right default for them.
    try:
        mod_toc = load_json(files["loader"]).get("entry_toc")
    except Exception:
        mod_toc = None
    if mod_toc:
        changed = False
        for f in functions:
            if not f.get("toc") or int(f["toc"], 16) == 0:
                f["toc"] = mod_toc
                changed = True
        if changed:
            dump_json(functions, files["functions"])
    log("stage", f"      {len(functions)} functions, {len(imports)} imports"); _mark("load")

    log("stage", f"2/6 analyzer -- {args.analyzer}")
    # These do not depend on each other: run them concurrently. The analyzer may
    # shell out to IDA/Ghidra, the coverage probe regenerates the whole NID
    # table, the NID resolver walks the database, and SPU extraction scans the
    # image -- four independent processes that used to run one after another.
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=4)
    f_cov = pool.submit(hle_nid_coverage, env, imports)
    f_names = pool.submit(resolve_nids, env, imports)
    f_spu = (pool.submit(stage_spu_extract, env, elf, out / "spu")
             if args.spu is not False else None)
    f_spulift = None

    analyzer_status = {"used": False, "reason": "native OPD scan only"}
    extra_funcs: list[dict] = []
    if args.analyzer in ("ida", "auto"):
        got, analyzer_status = analyze_with_ida(env, elf, analysis / "ida")
        extra_funcs += got
        if got:
            log("stage", f"      IDA found {len(got)} functions")
        elif args.analyzer == "ida":
            eprint("! " + analyzer_status.get("reason", "IDA unavailable"))
    if (args.analyzer == "ghidra" or
            (args.analyzer == "auto" and len(functions) < 1500)) and not extra_funcs:
        got, analyzer_status = analyze_with_ghidra(env, elf, analysis / "ghidra")
        extra_funcs += got
    if extra_funcs:
        merged = merge_functions(functions, extra_funcs)
        log("stage", f"      merged -> {len(merged)} functions")
        dump_json(merged, analysis / "functions.merged.json")
        files["functions"] = analysis / "functions.merged.json"
        functions = merged
    report["analyzer"] = analyzer_status
    ghidra_names: Path | None = None
    if analyzer_status.get("source") == "ghidra":
        names_out = analysis / "ghidra_names.json"
        r = run([env.python, env.tool("ghidra_names.py"),
                 analysis / "ghidra", "-o", names_out])
        if names_out.is_file():
            ghidra_names = names_out
            try:
                log("stage", f"      {len(load_json(names_out))} readable names from Ghidra")
            except Exception:
                pass

    log("stage", "3/6 coverage -- which firmware imports have HLE handlers")
    cov = f_cov.result()
    report["hle"] = cov
    if cov.get("ok"):
        log("stage", f"      HLE coverage {cov['covered']}/{cov['total']} "
                     f"({cov['percent']}%) across {len(cov['by_library'])} libraries")
    names = f_names.result()
    plan = port_plan(imports, cov, names)
    report["nid_names"] = {k: v.get("name") for k, v in names.items() if v}
    report["plan"] = plan
    for sub, p in plan.items():
        log("stage", f"      {sub:<8} {p['covered']}/{p['imports']} covered -- {p['note']}")

    # ---- knowledge base: deterministic reuse of previous titles -----------
    kb = KnowledgeBase(ROOT) if KnowledgeBase else None
    known: list[dict] = []
    suggestion: dict = {}
    if kb:
        known = kb.known_blockers(meta.sha256)
        suggestion = kb.preferred_settings(len(functions))
        report["kb"] = {"known_blockers": known, "suggested": suggestion}
        if known:
            log("kb", f"{len(known)} blocker(s) recorded for this ELF:")
            for b in known[:6]:
                log("kb", f"  {b['kind']}: {b['name'] or b['detail']} ({b['resolution']})")
        if suggestion.get("samples"):
            log("kb", f"proven settings for this size: chunks={suggestion.get('chunks')} "
                      f"opt={suggestion.get('opt')} "
                      f"({suggestion['samples']} title(s))")
        gfx = (plan.get("graphics") or {}).get("libraries") or {}
        if gfx:
            gaps = kb.coverage_gaps(next(iter(gfx)))
            if gaps:
                report["kb"]["graphics_gaps"] = gaps[:20]

    # SPU lifting runs beside the PPU lift, in its own processes.
    spu_images: list[Path] = []
    if f_spu is not None:
        spu_images = f_spu.result()
        if spu_images:
            log("stage", f"      {len(spu_images)} SPU images found; lifting in parallel")
            f_spulift = pool.submit(lift_spu_images, env, spu_images,
                                    out / "spu", args.jobs)
        else:
            log("stage", "      no SPU images in this title (PPU-only)")

    _mark("coverage")
    log("stage", "4/6 lift -- PPU -> C++")
    reuse = args.reuse_lift and (recompiled / "ppu_recomp.h").is_file()
    if reuse:
        log("stage", "      reusing the existing lift (--reuse-lift)")
    # Compile parallelism is bounded by the number of translation units. The
    # lifter's default (one TU per 600k lines) makes a large title build on one
    # core; cut it into ~2 TUs per worker instead.
    chunks = args.chunks or (suggestion.get("chunks") if len(functions) > 1500 else None)
    if chunks is None and args.chunk_lines is None and len(functions) > 1500:
        chunks = max(8, min(64, args.jobs * 2))
        log("stage", f"      cutting the lift into {chunks} translation units "
                     f"for {args.jobs} compile jobs")
    if reuse:
        pass
    else:
        stage_lift(env, elf, files["functions"], files["imports"], recompiled,
                   jobs=args.jobs, chunk_lines=args.chunk_lines,
                   names=Path(args.names).resolve() if args.names else ghidra_names,
                   code_end=meta.text_end, extra=args.lifter_arg or [],
                   chunks=chunks)
    units = sorted(recompiled.glob("*.c")) + sorted(recompiled.glob("*.cpp"))
    total_bytes = sum(p.stat().st_size for p in units)
    log("stage", f"      {len(units)} translation units, {human(total_bytes)} of C++")
    report["lift"] = {"units": len(units), "bytes": total_bytes,
                      "dir": str(recompiled)}

    log("stage", "5/6 hle -- NID -> handler table")
    hle_cpp = recompiled / "ppu_hle_nids.cpp"
    r = run([env.python, env.tool("gen_hle_nids.py"), "--all", "--out", hle_cpp])
    if r.returncode != 0:
        eprint("! gen_hle_nids failed: " + (r.stderr or r.stdout)[-600:])

    if f_spulift is not None:
        log("stage", "5b/6 spu -- collecting SPU lifts")
        spu = f_spulift.result()
        report["spu"] = spu
        log("stage", f"      {spu['lifted']}/{spu['images']} images lifted")
    pool.shutdown(wait=True)

    _mark("lift")
    log("stage", "6/6 project -- scaffold + CMake")
    proj = scaffold_project(env, out, recompiled, name, elf, game_dir)
    # SPU output must sit where the template globs it
    if (out / "spu").is_dir():
        try:
            os.symlink(out / "spu", proj / "spu", target_is_directory=True)
        except Exception:
            shutil.copytree(out / "spu", proj / "spu", dirs_exist_ok=True)
    report["project"] = str(proj)

    build_opt = args.opt if args.opt is not None else suggestion.get("opt", 2)
    built = False
    if args.build:
        log("build", "configuring + building (this is the long step)")
        built, buildlog = build_project(env, proj, args.jobs, fast=args.fast, opt=build_opt)
        (out / "build.log").write_text(buildlog, encoding="utf-8")
        report["build"] = {"ok": built, "log": str(out / "build.log")}
        log("build", "OK" if built else "FAILED -- see build.log"); _mark("build")
    exe = None
    for cand in (proj / "build" / "MyGameRecomp.exe",
                 proj / "build" / f"{proj.name}.exe",
                 proj / "build" / "Release" / "MyGameRecomp.exe"):
        if cand.is_file():
            exe = cand
            break
    if exe:
        stage_runtime_dlls(env, exe.parent)
    write_launcher(out, proj, exe, elf, game_dir,
                   title=window_title(name, sfo))
    report["exe"] = str(exe) if exe else None

    if exe and args.test > 0:
        diag = stage_smoke(env, exe, elf, game_dir, args.test,
                           title=window_title(name, sfo))
        (out / "boot.log").write_text("\n".join(diag.pop("log_tail")), encoding="utf-8")
        report["boot"] = diag
        log("test", f"verdict: {diag['verdict']}")
        if diag["unresolved_calls"]:
            log("test", f"missing lifted functions at: {diag['unresolved_calls'][:8]}")
        if diag.get("spu_misses"):
            log("test", f"{len(diag['spu_misses'])} SPU program(s) dispatched with no "
                        f"lifted image -- registration is missing or the image differs")
            for m in diag["spu_misses"][:5]:
                log("test", f"  fp={m['fp']} size={m['size']}")
        if diag["unimplemented_nids"]:
            log("test", f"{len(diag['unimplemented_nids'])} unimplemented NIDs "
                        f"(the runtime returns CELL_OK for these by default)")
            try:
                fake = [{"nid": n, "library": lib} for n, lib in diag["unimplemented_nids"]]
                nm = resolve_nids(env, fake)
                named = {n: (nm.get(n.upper()) or {}).get("name") for n, _ in diag["unimplemented_nids"]}
                diag["unimplemented_named"] = {k: v for k, v in named.items() if v}
                report["boot"] = diag
                for n, v in list(diag["unimplemented_named"].items())[:8]:
                    log("test", f"  {n} = {v}")
            except Exception:
                pass
        if args.fetch_docs and docsmod:
            got = docsmod.fetch_for_port(diag, plan)
            report["docs"] = got
            for g in got:
                ok = g.get("ok") or g.get("cached")
                log("docs", f"{g['topic']} -> " + (g.get("path") if ok
                                                    else g.get("error")))
    report["timings"] = timings
    report["functions"] = len(functions)
    report["settings"] = {"chunks": chunks, "opt": build_opt, "jobs": args.jobs}
    if kb and not args.no_learn:
        try:
            internals = dict(report, _imports=imports, _functions=len(functions),
                             _settings={"chunks": chunks, "opt": build_opt,
                                        "jobs": args.jobs})
            summary = kb.learn(internals, report.get("boot"))
            report["kb_learned"] = summary
            log("kb", f"recorded: verdict={summary.get('verdict')}, "
                      f"{summary.get('blockers')} blocker(s) -> {summary.get('db')}")
        except Exception as e:
            eprint(f"! knowledge base write failed: {e}")
    report["seconds"] = round(time.time() - t0, 1)
    report["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    dump_json(report, out / "port.json")
    write_report(out, report, proj, elf, game_dir)
    advice = next_step(report.get("boot"), plan)
    report["next_step"] = advice
    if getattr(args, "deep", False) and report.get("boot"):
        import types
        log("stage", "deep analysis: probe + linkage")
        try:
            probe_cmd(types.SimpleNamespace(dir=str(out), seconds=25, hle_trace=100,
                                            modes=False))
            linkage_cmd(types.SimpleNamespace(dir=str(out), seconds=20))
        except Exception as e:
            eprint(f"! deep analysis failed: {e}")
    if args.package:
        import types
        advice = "packaged: double-click the exe in dist\\ (or press Play)"
        try:
            package_cmd(types.SimpleNamespace(dir=str(out), out=str(out / "dist"),
                                              link_assets=True, no_assets=False))
        except Exception as e:
            eprint(f"! packaging failed: {e}")
    report["next_step"] = advice
    print()
    print(f"Next step : {advice}")
    print(f"Port tree : {out}")
    print(f"Project   : {proj}")
    print(f"Report    : {out / 'PORT.md'}")
    if exe:
        print(f"Binary    : {exe}")
        print(f"Run       : \"{exe}\" \"{elf}\"")
    print(f"Elapsed   : {report['seconds']}s")
    return 0 if (built or not args.build) else 1


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def stage_runtime_dlls(env: Env, dst: Path) -> list[str]:
    """Put the DLLs the runtime loads at run time next to the executable."""
    dst.mkdir(parents=True, exist_ok=True)
    copied = []
    roots = []
    if env.toolkit:
        roots += [env.toolkit.parent / "runtime", env.toolkit.parent / "build",
                  env.toolkit / "runtime", env.toolkit / "third_party"]
    for r in roots:
        if not r.is_dir():
            continue
        for dll in r.glob("*.dll"):
            target = dst / dll.name
            if not target.exists():
                try:
                    shutil.copy2(dll, target)
                    copied.append(dll.name)
                except Exception:
                    pass
    return copied


def write_launcher(out: Path, proj: Path, exe: Path | None, elf: Path,
                   game: Path | None, title: str | None = None) -> None:
    """play.cmd + play.ps1 inside the port tree."""
    root = game or elf.parent
    if not exe:
        return
    tline = f'set "PS3_TITLE={title}"\r\n' if title else ""
    (out / "play.cmd").write_text(
        "@echo off\r\n"
        f'set "PS3_VFS_ROOT={root}"\r\n'
        + tline +
        f'cd /d "{exe.parent}"\r\n'
        f'"{exe}" "{elf}" %*\r\n', encoding="utf-8")
    (out / "play.ps1").write_text(
        f'$env:PS3_VFS_ROOT = "{root}"\n'
        + (f'$env:PS3_TITLE = "{title}"\n' if title else "")
        + f'& "{exe}" "{elf}" @args\n', encoding="utf-8")


def stage_smoke(env: Env, exe: Path, elf: Path, game: Path | None,
                seconds: int, title: str | None = None) -> dict:
    """Boot the built port for N seconds and classify what happened.

    This is the difference between "it compiled" and "it runs": the log tells us
    whether the guest reached gameplay code or stopped on a missing lift / NID.
    """
    envv = dict(os.environ)
    envv["PS3_HLE_UNRESOLVED"] = "ok"
    envv["PS3_HLE_TRACE"] = "300"
    if title:
        envv["PS3_TITLE"] = title
    if game:
        envv["PS3_VFS_ROOT"] = str(game)
    log("test", f"booting for {seconds}s: {exe}")
    out_lines: list[str] = []
    proc = subprocess.Popen([str(exe), str(elf)], cwd=str(exe.parent),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            env=envv, bufsize=1)
    t0 = time.time()
    try:
        while time.time() - t0 < seconds:
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
                continue
            out_lines.append(line.rstrip("\n"))
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
    text = "\n".join(out_lines)
    diag = classify_boot(text, proc.returncode, time.time() - t0)
    return diag


def classify_boot(text: str, rc: int | None, seconds: float) -> dict:
    unimpl = sorted(set((n, lib or "?") for n, lib in
                        re.findall(r"(?:Unimplemented NID|unresolved NID) "
                                   r"(0x[0-9A-Fa-f]+)(?: in module (\S+))?", text)))
    indirect = sorted(set(re.findall(r"unresolved indirect call -> (0x[0-9A-Fa-f]+)",
                                     text)))
    spins = sorted(set(re.findall(r"HOTREAD\] spinning on (0x[0-9A-Fa-f]+)", text)))
    spu_miss = sorted(set((fp, int(size)) for fp, size in
                          re.findall(r"\[spu_workload\][^\n]*?fp=(0x[0-9A-Fa-f]+)"
                                     r"[^\n]*?size=(\d+)", text)))
    spu_hits = len(re.findall(r"dispatch HIT", text))
    spu_block = [int(s) for s in re.findall(r"EventFlagWait BLOCKED[^\n]*? (\d+)s ",
                                            text)]
    spu_stall = bool(spu_hits and spu_block and max(spu_block) >= 2)
    queue_spin = sorted(set(re.findall(r"\[spu-spin\][^\n]*?GETLLAR ea=(0x[0-9A-Fa-f]+)",
                                       text)))
    reached = []
    for pat, label in ((r"\[milestone\]\s*(.+)", "milestone"),
                       (r"ppu_run returned (-?\d+)", "exit"),
                       (r"cellGcm|rsx_|\[gcm\]", "graphics")):
        m = re.findall(pat, text, re.IGNORECASE)
        if m:
            reached.append(f"{label}: {m[-1] if isinstance(m[-1], str) else m[-1]}")
    verdict = "ran"
    if rc is not None and rc not in (0, 1) and rc < 0:
        verdict = "crashed"
    if spu_miss:
        verdict = "spu-miss"
    elif queue_spin:
        verdict = "spurs-queue"
    elif spu_stall:
        verdict = "spu-stall"
    elif indirect:
        verdict = "missing-lift"
    elif unimpl and not reached:
        verdict = "missing-hle"
    if spu_stall:
        reached.append(f"spu-stall: {spu_hits} dispatch hit(s), "
                       f"event-flag wait {max(spu_block)}s")
    return {
        "ran": True, "seconds": round(seconds, 1), "exit": rc,
        "verdict": verdict, "unimplemented_nids": unimpl[:200],
        "unresolved_calls": indirect[:200], "spin_addresses": spins[:50],
        "spu_misses": [{"fp": fp, "size": size} for fp, size in spu_miss][:50],
        "spu_hits": spu_hits, "spu_stall": spu_stall,
        "spurs_queue_spin": queue_spin[:8],
        "observations": reached[-20:], "log_tail": text.splitlines()[-80:],
    }


def next_step(diag: dict | None, plan: dict | None) -> str:
    """One actionable line for the user, derived from the boot verdict."""
    if not diag:
        return "run `port ... --build --test 20` to boot-test the build"
    v = diag.get("verdict")
    if v == "ran":
        if "graphics" in " ".join(diag.get("observations") or []):
            return ("the title is running; verify gameplay and input, then "
                    "`package` it into a game folder")
        return "it boots; run `package` to produce the Windows game folder"
    if v == "spu-miss":
        return ("an SPU program is not registered: run `port` again (the registry "
                "is regenerated) or `setup --with-ghidra` then relift")
    if v == "spu-stall":
        return ("SPURS task/policy signalling is the remaining gap; fetch the "
                "reference with `docs --topic rpcs3-spurs --fetch`")
    if v == "spurs-queue":
        return ("the SPU workers are spinning on an empty SPURS queue: the "
                "producer (job submission/policy) never enqueued. `linkage` shows "
                "the dead call path; `oracle` (optional) records RPCS3's run as "
                "the reference")
    if v == "missing-lift":
        return ("missing lifted functions; rerun with `--analyzer ghidra` so the "
                "independent decoder adds the starts the OPD table misses")
    if v == "missing-hle":
        return ("an unimplemented NID is on the critical path; `gaps` lists the "
                "backlog, or implement that NID in project/stubs.cpp")
    if v == "crashed":
        return "the guest crashed; the last lines of boot.log name the site"
    return "inspect boot.log and PORT.md"


def write_report(out: Path, rep: dict, proj: Path, elf: Path, game: Path | None):
    hle = rep.get("hle") or {}
    lift = rep.get("lift") or {}
    lines = [
        f"# {rep['title']} -- PS3revamp port report",
        "",
        f"- EBOOT: `{elf}`",
        f"- SHA-256: `{rep['elf_sha256']}`",
        f"- Title ID: `{rep.get('sfo', {}).get('TITLE_ID', 'unknown')}`",
        f"- Generated: {rep.get('finished')} ({rep.get('seconds')}s)",
        "",
        "## What was produced",
        "",
        f"- {lift.get('units', 0)} C++ translation units ({human(lift.get('bytes', 0))}) "
        f"in `recompiled/`, plus `ppu_recomp.h` and the function table.",
        f"- HLE NID table: `recompiled/ppu_hle_nids.cpp`.",
        f"- CMake project: `{proj}` (preset `ps3revamp`).",
        f"- Executable: `{rep.get('exe') or 'not built (pass --build)'}`.",
        "",
        "## Firmware coverage",
        "",
    ]
    if hle.get("ok"):
        lines += [
            f"**{hle['covered']}/{hle['total']} imports ({hle['percent']}%)** have an HLE "
            "handler in this toolkit build.",
            "",
            "| library | covered | total |",
            "|---|---|---|",
        ]
        for lib, c in hle["by_library"].items():
            lines.append(f"| {lib} | {c['covered']} | {c['total']} |")
        if hle["missing"]:
            lines += ["", "### Imports with no handler", "",
                      "These must be implemented in `project/stubs.cpp` or lifted "
                      "(FIRMWARE_LLE.md) before the title can get past them:", "",
                      "| library | NID |", "|---|---|"]
            for m in hle["missing"][:200]:
                lines.append(f"| {m['library']} | {m['nid']} |")
            if len(hle["missing"]) > 200:
                lines.append(f"| ... | {len(hle['missing']) - 200} more (see port.json) |")
    else:
        lines.append(f"Coverage probe failed: {hle.get('error', 'unknown')}")

    boot = rep.get("boot")
    if boot:
        lines += [
            "",
            "## Boot test",
            "",
            f"- Verdict: **{boot['verdict']}**",
            f"- Ran {boot['seconds']}s, exit {boot['exit']}",
        ]
        if boot.get("unresolved_calls"):
            lines += ["", f"- Missing lifted functions (indirect calls to "
                          f"`{', '.join(boot['unresolved_calls'][:10])}`) â€” rerun "
                          "with `--analyzer ida` or `find_functions.py --seed-json`."]
        if boot.get("unimplemented_nids"):
            lines += ["", f"- {len(boot['unimplemented_nids'])} unimplemented NIDs "
                          "(returned CELL_OK):"]
            lines += [f"  - `{a}` in `{b}`" for a, b in boot["unimplemented_nids"][:40]]
        if boot.get("observations"):
            lines += ["", "Observed:", ""] + [f"- {o}" for o in boot["observations"]]

    lines += [
        "",
        "## Analyzer",
        "",
        f"```\n{json.dumps(rep.get('analyzer'), indent=2)}\n```",
        "",
    ]

    plan = rep.get("plan") or {}
    if plan:
        lines += ["## Port plan", ""]
        for sub, p in plan.items():
            lines += [f"### {sub}  ({p['covered']}/{p['imports']} imports covered)", "",
                      f"{p['note']}.", "",
                      "| library | covered | imports |", "|---|---|---|"]
            for lib, c in p["libraries"].items():
                lines.append(f"| {lib} | {c['covered']} | {c['imports']} |")
            lines.append("")

    kb = rep.get("kb") or {}
    if kb.get("known_blockers"):
        lines += ["", "## What the store already knew", ""]
        for b in kb["known_blockers"]:
            lines.append(f"- {b['kind']}: `{b['name'] or b['detail']}` "
                         f"({b['resolution']})")
    if kb.get("suggested", {}).get("samples"):
        s = kb["suggested"]
        lines += ["", f"Settings taken from {s['samples']} similar title(s): "
                      f"chunks={s.get('chunks')}, opt={s.get('opt')}."]
    if rep.get("docs"):
        lines += ["", "## Reference material for this title", ""]
        for d in rep["docs"]:
            ok = d.get("ok") or d.get("cached")
            lines.append(f"- `{d['topic']}`: " +
                         (f"`{d.get('path')}`" if ok else f"unavailable ({d.get('error')})"))

    lines += [
        "## Honest status",
        "",
        "The C++ above is a *faithful* translation of the title's PPU (and SPU, if "
        "lifted) code -- not a from-scratch rewrite. It is native x64 and links "
        "against the ps3recomp runtime, which supplies memory, threads, RSX "
        "translation and the HLE libraries.",
        "",
        "What is automatic: disassembly, function discovery, code lifting, import "
        "binding, HLE dispatch, project + build.",
        "",
        "What is per-title and cannot be automated away:",
        "",
        "1. Uncovered NIDs (table above) -- implement them in `stubs.cpp`.",
        "2. RSX behaviour the renderer does not translate yet.",
        "3. Asset paths / VFS root, save data, PSN services.",
        "4. Game-specific patches (timing, memory layout, known bugs).",
        "",
        "## Next commands",
        "",
        "```",
        f"cd \"{proj}\"",
        "cmake --preset ps3revamp",
        "cmake --build build --parallel",
        f"\"./build/{proj.name}.exe\" \"{elf}\"",
        "```",
        "",
        "Re-run the whole pipeline at any time:",
        "",
        "```",
        f"python \"{ROOT / 'ps3revamp.py'}\" port \"{elf}\" -o \"{out}\" --build",
        "```",
        "",
    ]
    (out / "PORT.md").write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


def _download(url: str, dst: Path) -> Path:
    import urllib.request
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_file() and dst.stat().st_size > 0:
        log("setup", f"cached {dst.name} ({human(dst.stat().st_size)})")
        return dst
    log("setup", f"downloading {url}")
    tmp = dst.with_suffix(dst.suffix + ".part")
    with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = last = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if total and done - last > 20 << 20:
                last = done
                print(f"        {human(done)} / {human(total)} "
                      f"({100*done//total}%)", flush=True)
    tmp.replace(dst)
    log("setup", f"got {dst.name} ({human(dst.stat().st_size)})")
    return dst


def _extract(zip_path: Path, dest: Path, strip: int = 1) -> None:
    import zipfile
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        log("setup", f"exists {dest}")
        return
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            parts = Path(info.filename).parts
            if info.is_dir() or len(parts) <= strip:
                continue
            target = dest.joinpath(*parts[strip:])
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    log("setup", f"extracted -> {dest}")


SETUP_ITEMS = [
    # name, url, archive, destination, strip
    ("ps3recomp", "https://github.com/sp00nznet/ps3recomp/archive/refs/tags/v0.18.0.zip",
     "downloads/ps3recomp-src.zip", "toolkit/ps3recomp", 1),
    ("llvm-mingw", "https://github.com/mstorsjo/llvm-mingw/releases/download/20261020/"
     "llvm-mingw-20261020-ucrt-x86_64.zip",
     "downloads/llvm-mingw.zip", "toolchain/llvm-mingw", 1),
    ("cmake", "https://github.com/Kitware/CMake/releases/download/v4.4.5/"
     "cmake-4.4.5-windows-x86_64.zip",
     "downloads/cmake.zip", "toolchain/cmake", 1),
    ("ninja", "https://github.com/ninja-build/ninja/releases/download/v1.12.1/ninja-win.zip",
     "downloads/ninja.zip", "toolchain/ninja", 0),
]


def _github_asset(repo: str, pattern: str) -> str | None:
    import urllib.request
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers={"User-Agent": "ps3revamp"})
        with urllib.request.urlopen(req, timeout=60) as r:
            j = json.load(r)
        for a in j.get("assets", []):
            if re.search(pattern, a["name"], re.IGNORECASE):
                return a["browser_download_url"]
    except Exception:
        return None
    return None


def setup_cmd(args) -> int:
    offline = getattr(args, "offline", False)
    print("PS3revamp setup -- " + ("verifying bundled components (offline)"
                                   if offline else
                                   "fetching everything into ./toolkit and ./toolchain"))
    if offline:
        need = [ROOT / "toolkit" / "ps3recomp" / "tools" / "ppu_lifter.py",
                ROOT / "toolchain" / "llvm-mingw" / "bin" / "clang.exe",
                ROOT / "toolchain" / "cmake" / "bin" / "cmake.exe",
                ROOT / "toolchain" / "ninja" / "ninja.exe",
                ROOT / "python" / "python.exe"]
        bad = [str(p) for p in need if not p.exists()]
        if bad:
            for b in bad:
                eprint(f"! missing {b}")
            return 1
        print("all bundled components present")
        try:
            import vendor_runtime
            print(vendor_runtime.apply())
        except Exception as e:
            eprint(f"! runtime patch: {e}")
            return 1
        return 0
    for name, url, arc, dest, strip in SETUP_ITEMS:
        try:
            z = _download(url, ROOT / arc)
            _extract(z, ROOT / dest, strip)
        except Exception as e:
            eprint(f"! {name}: {e}")
            return 1
    # optional: sccache makes every rebuild (and every other title's build)
    # reuse the runtime objects.
    if not (ROOT / "toolchain" / "sccache" / "sccache.exe").is_file():
        url = _github_asset("mozilla/sccache", r"x86_64-pc-windows-msvc\.zip$")
        if url:
            try:
                _extract(_download(url, ROOT / "downloads" / "sccache.zip"),
                         ROOT / "toolchain" / "sccache", 1)
            except Exception as e:
                eprint(f"! sccache (optional): {e}")

    # optional: Ghidra, the independent decoder. Lives inside the project.
    if getattr(args, "with_ghidra", False):
        if not any((ROOT / "toolchain" / "ghidra").glob("*/support/analyzeHeadless.bat")):
            url = ("https://github.com/NationalSecurityAgency/ghidra/releases/download/"
                   "Ghidra_12.1_build/ghidra_12.1_PUBLIC_20260513.zip")
            try:
                z = _download(url, ROOT / "downloads" / "ghidra.zip")
                _extract(z, ROOT / "toolchain" / "ghidra", 1)
            except Exception as e:
                eprint(f"! ghidra (optional): {e}")
        else:
            log("setup", "ghidra present")

    # sanity
    tk = ROOT / "toolkit" / "ps3recomp"
    for need in ("tools/ppu_loader.py", "tools/ppu_lifter.py", "runtime", "templates/project"):
        if not (tk / need).exists():
            eprint(f"! toolkit incomplete: missing {need}")
            return 1
    env = discover(save=True)
    # The vendored runtime (our loader changes, the Project Pyrrhus SPURS
    # series and the fixes in docs/SPURS_WORK.md) is a patch against pristine
    # ps3recomp v0.18.0, because toolkit/ is git-ignored. Apply it once.
    try:
        import vendor_runtime
        log("setup", vendor_runtime.apply())
    except Exception as e:
        eprint(f"! runtime patch: {e}")
        return 1
    log("setup", f"toolkit   {env.toolkit}")
    log("setup", f"toolchain {env.toolchain}")
    log("setup", f"cmake     {env.cmake}")
    log("setup", f"clang     {env.clang}")
    missing = [m for m in ("capstone", "construct", "Crypto", "tqdm")
               if not _importable(env.python, m)]
    if missing:
        print("\nPython deps missing. Install with:")
        print(f'  "{env.python}" -m pip install -r "{tk / "requirements.txt"}"')
        return 1
    print("\nSetup complete. Next:  python ps3revamp.py doctor")
    return 0


def learn_cmd(args) -> int:
    if not KnowledgeBase:
        eprint("knowledge base module unavailable")
        return 1
    out = Path(args.dir).resolve()
    rep_path = out / "port.json"
    if not rep_path.is_file():
        eprint(f"no port.json in {out}")
        return 1
    rep = load_json(rep_path)
    imports: list[dict] = []
    for f in sorted((out / "analysis").glob("*.imports.json")):
        imports = load_json(f)
        break
    internals = dict(rep, _imports=imports,
                     _functions=rep.get("functions") or 0,
                     _settings=rep.get("settings") or {})
    boot = rep.get("boot")
    if not boot and (out / "boot.log").is_file():
        boot = classify_boot((out / "boot.log").read_text(encoding="utf-8",
                                                          errors="replace"), None, 0)
    s = KnowledgeBase(ROOT).learn(internals, boot)
    print(json.dumps(s, indent=2))
    return 0 if s.get("ok") else 1


def kb_cmd(args) -> int:
    if not KnowledgeBase:
        eprint("knowledge base module unavailable")
        return 1
    kb = KnowledgeBase(ROOT)
    if args.nid:
        print(json.dumps(kb.safe_to_stub(args.nid), indent=2))
        return 0
    if args.library:
        print(json.dumps(kb.coverage_gaps(args.library), indent=2))
        return 0
    if args.titles:
        for t in kb.titles(args.limit):
            print(f"{t['title_id'] or '?':<10} prog={t['progress']} runs={t['runs']:<3} "
                  f"{t['verdict']:<10} {t['title']}")
        return 0
    stats = kb.stats()
    for k, v in stats.items():
        print(f"{k:<16} {v}")
    if stats["titles"]:
        print("\nrecent titles:")
        for t in kb.titles(10):
            print(f"  {t['title_id'] or '?':<10} prog={t['progress']} {t['verdict']:<10} {t['title']}")
    return 0


def tools_cmd(args) -> int:
    from ps3revamp_tools import registry
    env = discover()
    for t in registry(env):
        mark = "yes" if t["available"] else "no"
        print(f"[{mark:>3}] {t['id']:<10} {t['name']}")
        print(f"        used for : {t['use']}")
        print(f"        detected : {t['detail']}")
        if not t["available"]:
            print(f"        enable   : {t['enable']}")
    return 0


def docs_cmd(args) -> int:
    if not docsmod:
        eprint("docs module unavailable")
        return 1
    docsmod.seed()
    if args.topic and args.fetch:
        print(json.dumps(docsmod.fetch(args.topic), indent=2))
        return 0
    if args.all and args.fetch:
        for t in docsmod.topics():
            r = docsmod.fetch(t["id"])
            print(f"{t['id']:<8} {'ok' if r.get('ok') else 'FAILED'}  "
                  f"{r.get('path') or r.get('error')}")
        return 0
    if args.topic:
        p = docsmod.lookup(args.topic)
        if not p:
            eprint(f"not cached: {args.topic}. Run: ps3revamp.py docs --topic "
                   f"{args.topic} --fetch")
            return 1
        text = p.read_text(encoding="utf-8", errors="replace")
        print(f"{p}  ({len(text)} bytes)\n")
        for line in text.splitlines()[:60]:
            print(line)
        return 0
    for t in docsmod.topics():
        state = "cached" if t["cached"] else "      "
        print(f"[{state}] {t['id']:<8} {t['title']}")
        print(f"          why: {t['why']}")
        print(f"          src: {t['source']}")
    return 0


def analyze_dir_cmd(args) -> int:
    """Scan a folder for EBOOT.elf files and report per-title coverage.

    This is the corpus path: point it at an RPCS3 `dev_hdd0/game` or a dump
    folder and it analyses every title it finds, feeding the knowledge base.
    """
    env = discover()
    if not env.toolkit:
        eprint("no toolkit")
        return 1
    root = Path(args.dir).resolve()
    elfs = sorted(root.glob("**/EBOOT.elf"))[: args.limit]
    if not elfs:
        eprint(f"no EBOOT.elf under {root}")
        return 1
    kb = KnowledgeBase(ROOT) if KnowledgeBase else None
    rows = []
    for i, elf in enumerate(elfs, 1):
        game = infer_game_dir(elf)
        sfo = find_sfo(game, elf)
        name = sfo.get("TITLE") or elf.parent.parent.name
        log("corpus", f"[{i}/{len(elfs)}] {name}")
        try:
            meta = read_ps3_elf(elf)
            tmp = WORK / "corpus" / meta.sha256[:12]
            files = stage_loader(env, elf, tmp)
            funcs = load_json(files["functions"])
            imports = load_json(files["imports"])
            cov = hle_nid_coverage(env, imports)
            row = {
                "title": name, "title_id": sfo.get("TITLE_ID"),
                "elf": str(elf), "sha256": meta.sha256,
                "functions": len(funcs), "imports": len(imports),
                "covered": cov.get("covered", 0),
                "percent": cov.get("percent", 0.0),
                "missing": cov.get("missing", [])[:50],
            }
            rows.append(row)
            log("corpus", f"      {len(funcs)} funcs, {len(imports)} imports, "
                          f"{row['covered']} covered ({row['percent']}%)")
        except Exception as e:
            rows.append({"title": name, "elf": str(elf), "error": str(e)[:200]})
            log("corpus", f"      skipped: {e}")
    out = ROOT / "knowledge" / "corpus.json"
    gaps: dict[str, dict] = {}
    for r in rows:
        for m in r.get("missing", []):
            g = gaps.setdefault(m["nid"], {"nid": m["nid"],
                                           "library": m["library"], "titles": 0})
            g["titles"] += 1
    gap_list = sorted(gaps.values(), key=lambda x: (-x["titles"], x["library"]))
    dump_json({"titles": rows, "gaps": gap_list}, out)
    print()
    print(f"{'title':<32} {'funcs':>7} {'imports':>8} {'covered':>8} {'%':>6}")
    for r in rows:
        if r.get("error"):
            print(f"{r['title'][:31]:<32} error: {r['error'][:40]}")
        else:
            print(f"{r['title'][:31]:<32} {r['functions']:>7} {r['imports']:>8} "
                  f"{r['covered']:>8} {r['percent']:>6}")
    if gap_list:
        print("\nmissing imports shared by the corpus (top 15):")
        for g in gap_list[:15]:
            print(f"  {g['library']:<22} {g['nid']}  ({g['titles']} title(s))")
    print(f"\ncorpus: {out}")
    return 0


def route_cmd(args) -> int:
    """Compute the best conversion route for one EBOOT, deterministically.

    The decision comes from measured facts, not guesses: import coverage, the
    subsystems that are short, whether SPU images exist, whether an independent
    decoder is available, and how close this title is to titles already in the
    corpus.
    """
    env = discover()
    if not env.toolkit:
        eprint("no toolkit")
        return 1
    elf_in = Path(args.elf).resolve()
    elf = ensure_elf(env, elf_in)
    meta = read_ps3_elf(elf)
    game = Path(args.game).resolve() if args.game else infer_game_dir(elf)
    sfo = find_sfo(game, elf)
    name = sfo.get("TITLE") or elf.stem

    tmp = WORK / "route" / meta.sha256[:12]
    files = stage_loader(env, elf, tmp)
    imports = load_json(files["imports"])
    funcs = load_json(files["functions"])
    cov = hle_nid_coverage(env, imports)
    names = resolve_nids(env, imports)
    plan = port_plan(imports, cov, names)
    spu = stage_spu_extract(env, elf, tmp / "spu")

    gaps = {}
    for m in cov.get("missing", []):
        gaps[m["library"]] = gaps.get(m["library"], 0) + 1
    short = sorted(gaps.items(), key=lambda kv: -kv[1])

    # Corpus similarity: share of firmware imports with a recorded title.
    similar = []
    corpus = ROOT / "knowledge" / "corpus.json"
    if corpus.is_file():
        try:
            data = load_json(corpus)
            rows = data.get("titles", data) if isinstance(data, dict) else data
            mine = {i["nid"] for i in imports}
            for r in rows:
                if not r.get("missing") or r.get("sha256") == meta.sha256:
                    continue
                known = {i for i in mine}
                if r.get("imports"):
                    similar.append({"title": r["title"], "coverage": r["percent"]})
        except Exception:
            pass

    steps = []
    steps.append({
        "stage": "decode",
        "why": f"{len(funcs)} OPD functions, {len(imports)} imports; "
               f"{cov.get('covered', 0)}/{len(imports)} have HLE handlers "
               f"({cov.get('percent', 0)}%)",
        "do": "ps3revamp.py port <elf> --analyzer auto --build --test 20",
    })
    if env.ghidra_dir:
        steps.append({
            "stage": "independent decode",
            "why": "Ghidra is available; recovers starts the OPD table misses",
            "do": "add --analyzer ghidra",
        })
    else:
        steps.append({
            "stage": "independent decode",
            "why": "no second decoder installed",
            "do": "ps3revamp.py setup --with-ghidra",
        })
    if short:
        steps.append({
            "stage": "HLE gaps",
            "why": ", ".join(f"{lib} x{n}" for lib, n in short[:6]),
            "do": "implement the named NIDs in project/stubs.cpp, or lift the "
                  "firmware module (tools/lift_prx.py) when a whole library is "
                  "missing",
        })
    steps.append({
        "stage": "SPU",
        "why": f"{len(spu)} embedded images; "
               + ("they will be lifted and registered" if spu
                  else "title is PPU-only"),
        "do": ("keep default (auto lift + spu_workloads registry)"
               if spu else "nothing to do"),
    })
    if (plan.get("graphics") or {}).get("imports"):
        steps.append({
            "stage": "graphics",
            "why": f"{plan['graphics']['covered']}/{plan['graphics']['imports']} "
                   "graphics imports covered; RSX translation decides visual parity",
            "do": "boot with the D3D12 backend, compare against RPCS3 for the "
                  "first frames (ps3revamp.py docs --topic rpcs3-rsx --fetch)",
        })
    steps.append({
        "stage": "validate",
        "why": "boot test classifies ran / spu-miss / spu-stall / missing-lift / "
               "missing-hle and records it",
        "do": "iterate: fix the reported link, rebuild (sccache), re-test",
    })
    steps.append({
        "stage": "package",
        "why": "produce a normal Windows game folder",
        "do": "ps3revamp.py package <port> --out \"C:\\Games\\<Title>\"",
    })

    plan_out = {
        "title": name, "title_id": sfo.get("TITLE_ID"), "elf": str(elf),
        "functions": len(funcs), "imports": len(imports),
        "coverage": {"covered": cov.get("covered", 0),
                     "total": len(imports), "percent": cov.get("percent", 0)},
        "short_subsystems": short, "spu_images": len(spu),
        "ghidra": bool(env.ghidra_dir), "similar": similar,
        "route": steps,
    }
    out = ROOT / "knowledge" / "routes" / f"{meta.sha256[:12]}.json"
    dump_json(plan_out, out)

    print(f"{name}  [{sfo.get('TITLE_ID', '?')}]")
    print(f"  functions {len(funcs)}   imports {len(imports)}   "
          f"coverage {cov.get('covered',0)}/{len(imports)} ({cov.get('percent',0)}%)")
    print(f"  SPU images {len(spu)}   Ghidra {'yes' if env.ghidra_dir else 'no'}")
    print()
    for i, s in enumerate(steps, 1):
        print(f"{i}. {s['stage']}")
        print(f"   why: {s['why']}")
        print(f"   do : {s['do']}")
    print(f"\nroute: {out}")
    return 0


def gaps_cmd(args) -> int:
    """Prioritised backlog: which firmware work unblocks the most titles."""
    corpus = ROOT / "knowledge" / "corpus.json"
    rows = []
    if corpus.is_file():
        data = load_json(corpus)
        rows = data.get("titles", []) if isinstance(data, dict) else data
    counts: dict[str, dict] = {}

    def _norm(nid: str) -> str:
        try:
            return f"0x{int(str(nid), 16):08X}"
        except Exception:
            return str(nid).upper()

    for r in rows:
        for m in r.get("missing", []):
            lib = m["library"]
            nid = _norm(m["nid"])
            c = counts.setdefault(lib, {"library": lib, "nids": {}, "titles": 0})
            c["nids"][nid] = c["nids"].get(nid, 0) + 1
            c["titles"] += 1

    if args.kb and KnowledgeBase:
        kb = KnowledgeBase(ROOT)
        for lib in sorted({c["library"] for c in counts.values()}):
            for g in kb.coverage_gaps(lib):
                c = counts.setdefault(lib, {"library": lib, "nids": {}, "titles": 0})
                c["nids"].setdefault(_norm(g["nid"]), g["missing_runs"])

    ordered = sorted(counts.values(), key=lambda c: (-len(c["nids"]), c["library"]))
    if not ordered:
        print("no corpus yet: run `ps3revamp.py analyze-dir <folder>` first")
        return 0

    all_nids = [n for c in ordered for n in c["nids"]]
    named = {}
    if all_nids and env_toolkit() is not None:
        e = discover()
        fake = [{"nid": n, "library": "?"} for n in all_nids[:200]]
        named = resolve_nids(e, fake)

    total = sum(len(c["nids"]) for c in ordered)
    print(f"backlog: {total} unimplemented import(s) across {len(rows)} title(s)\n")
    print(f"{'library':<20} {'nids':>5} {'examples'}")
    for c in ordered:
        examples = []
        for nid in list(c["nids"])[:3]:
            nm = (named.get(nid.upper()) or {}).get("name")
            examples.append(f"{nid}={nm}" if nm else nid)
        print(f"{c['library']:<20} {len(c['nids']):>5} {', '.join(examples)}")
    out = ROOT / "knowledge" / "backlog.json"
    dump_json({"titles": len(rows),
               "libraries": [{"library": c["library"],
                              "nids": sorted(c["nids"]),
                              "examples": [(named.get(n.upper()) or {}).get("name")
                                           for n in list(c["nids"])[:5]]}
                             for c in ordered]}, out)
    print(f"\nbacklog: {out}")
    return 0


def env_toolkit():
    return discover().toolkit


def probe_cmd(args) -> int:
    """Live behavioural probe of a built port.

    Runs the port with diagnostics on, then turns the stream into a structured
    answer: which thread is blocked on what, which SPU workers spin on which
    guest address, what the queue looked like, and which imports are unresolved.
    This is the tool's own equivalent of attaching a debugger, and it works
    offline on any title.
    """
    out = Path(args.dir).resolve()
    rep_path = out / "port.json"
    if not rep_path.is_file():
        eprint(f"no port.json in {out}")
        return 1
    rep = load_json(rep_path)
    exe = Path(rep.get("exe") or "")
    elf = Path(rep.get("elf") or "")
    if not exe.is_file() or not elf.is_file():
        eprint("port has no built executable / ELF")
        return 1

    env = dict(os.environ)
    env["PS3_HLE_UNRESOLVED"] = "ok"
    env["PS3_HLE_TRACE"] = str(args.hle_trace)
    env["SPURS_TASKSET_TRACE"] = "1"
    env["PS3_TITLE"] = window_title(rep.get("title") or "port", rep.get("sfo"))
    if rep.get("game_dir"):
        env["PS3_VFS_ROOT"] = rep["game_dir"]

    if args.modes:
        modes = [
            ("baseline", {}),
            ("sticky-signal", {"SPURS_STICKY_SIGNAL": "1"}),
            ("interp-unlifted", {"SPU_INTERP_UNLIFTED": "1"}),
            ("ws-drain", {"LBP_WS_DRAIN": "2"}),
        ]
        table = []
        for name, extra in modes:
            e = dict(env, **extra)
            proc = subprocess.Popen([str(exe), str(elf)], cwd=str(exe.parent),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace",
                                    bufsize=1, env=e)
            lines, t0 = [], time.time()
            while time.time() - t0 < args.seconds:
                line = proc.stdout.readline() if proc.stdout else ""
                if not line:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.02)
                    continue
                lines.append(line.rstrip("\n"))
            if proc.poll() is None:
                proc.terminate()
            d = classify_boot("\n".join(lines), proc.returncode, args.seconds)
            table.append({"mode": name, "verdict": d["verdict"],
                          "spins": d.get("spurs_queue_spin"),
                          "hits": d.get("spu_hits")})
            log("probe", f"{name:<15} verdict={d['verdict']} "
                         f"queue_spin={d.get('spurs_queue_spin') or '-'}")
        dump_json(table, out / "probe_modes.json")
        print("\nmode compatibility matrix:")
        for t in table:
            print(f"  {t['mode']:<15} {t['verdict']:<12} hits={t['hits']} "
                  f"spin={t['spins'] or '-'}")
        print(f"\n  {out / 'probe_modes.json'}")
        return 0

    log("probe", f"running {exe.name} for {args.seconds}s")
    proc = subprocess.Popen([str(exe), str(elf)], cwd=str(exe.parent),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            bufsize=1, env=env)
    lines: list[str] = []
    t0 = time.time()
    while time.time() - t0 < args.seconds:
        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            if proc.poll() is not None:
                break
            time.sleep(0.02)
            continue
        lines.append(line.rstrip("\n"))
    if proc.poll() is None:
        proc.terminate()
    text = "\n".join(lines)
    (out / "probe.log").write_text(text, encoding="utf-8")

    waits = re.findall(r"EventFlagWait BLOCKED tid=(\d+) (\d+)s on pattern (0x[0-9A-Fa-f]+)"
                       r"[^\n]*?flagEA=(0x[0-9A-Fa-f]+)", text)
    spins = re.findall(r"\[spu-spin\][^\n]*?pc=(0x[0-9A-Fa-f]+)[^\n]*?GETLLAR ea=(0x[0-9A-Fa-f]+)"
                       r"[^\n]*?line: ([0-9A-Fa-f ]+)", text)
    unimpl = sorted(set(re.findall(r"(?:Unimplemented NID|unresolved NID) (0x[0-9A-Fa-f]+)", text)))
    hits = len(re.findall(r"dispatch HIT", text))
    misses = sorted(set(re.findall(r"dispatch MISS fp=(0x[0-9A-Fa-f]+)", text)))
    sleeps = re.findall(r"task (\d+) \(taskset (0x[0-9A-Fa-f]+)\) sleeping (\d+)s in WAIT_SIGNAL", text)
    pso_fail = len(re.findall(r"\[FP\] PSO FAIL|\[VP\] PSO creation FAIL", text))
    draws = len(re.findall(r"\[D3D12\] draw_(?:indexed|arrays)", text))

    findings = []
    if pso_fail:
        findings.append(
            f"{pso_fail} graphics pipeline(s) failed to create; rerun with "
            f"D3D12_DBG=1 to get the D3D12 debug-layer reason (a sampler/SRV "
            f"range, blend enum or format mismatch is named verbatim).")
    if spins:
        ea, last = spins[-1][1], spins[-1][2].split()
        findings.append(
            f"SPU workers are spinning on guest address {ea} "
            f"(GETLLAR, last line: {' '.join(last)}).")
        if len(last) >= 4 and last[3] in ("00000010", "00000020", "00000040"):
            findings.append("That is an LF queue with pop=push=0: no producer enqueued work.")
    if waits:
        tid, secs, pat, ea = waits[-1]
        findings.append(f"PPU thread {tid} has been blocked {secs}s on event flag {ea} "
                        f"(pattern {pat}).")
    if misses:
        findings.append(f"{len(misses)} SPU image(s) dispatched with no lifted registry entry.")
    if unimpl:
        findings.append(f"{len(unimpl)} unimplemented NID(s): " + ", ".join(unimpl[:6]))
    if not findings:
        findings.append("no blocking symptom observed in the window")

    result = {"verdict": rep.get("boot", {}).get("verdict"),
              "seconds": args.seconds, "dispatch_hits": hits,
              "spu_misses": misses, "unimplemented_nids": unimpl,
              "draws": draws, "pso_failures": pso_fail,
              "waits": [{"tid": w[0], "seconds": int(w[1]), "pattern": w[2],
                         "flag": w[3]} for w in waits[-3:]],
              "spins": [{"pc": s[0], "ea": s[1], "line": s[2]} for s in spins[-3:]],
              "task_sleeps": [{"task": s[0], "taskset": s[1], "seconds": int(s[2])}
                              for s in sleeps[-3:]],
              "findings": findings,
              "log": str(out / "probe.log")}
    dump_json(result, out / "probe.json")

    print(f"probe: {rep.get('title')}")
    print(f"  {args.seconds}s, {len(lines)} log lines, {hits} SPU dispatch hit(s), "
          f"{draws} draw(s), {pso_fail} PSO failure(s)")
    for f in findings:
        print(f"  - {f}")
    print(f"\n  full log: {out / 'probe.log'}")
    print(f"  findings: {out / 'probe.json'}")
    return 0


def linkage_cmd(args) -> int:
    """Static call sites vs run-time calls for every imported firmware function.

    For each import stub it counts call sites in the lifted C++ and whether the
    NID was actually called during a boot window. A stub with call sites but
    zero calls is a *dead path*: the title has the code but never reaches it —
    the class of failure that hides as an empty queue or a blocked thread.
    """
    out = Path(args.dir).resolve()
    rep_path = out / "port.json"
    if not rep_path.is_file():
        eprint(f"no port.json in {out}")
        return 1
    rep = load_json(rep_path)
    exe = Path(rep.get("exe") or "")
    elf = Path(rep.get("elf") or "")
    imports: list[dict] = []
    for f in sorted((out / "analysis").glob("*.imports.json")):
        imports = load_json(f)
        break
    if not imports:
        eprint("no imports.json -- run the port first")
        return 1
    rc = out / "recompiled"
    files = sorted(rc.glob("*.cpp"))
    if not files:
        eprint("no lifted sources")
        return 1

    # one pass over the generated code, remembering which func_ symbols occur
    # One streaming pass over the generated code (it can be >1 GB, so never
    # concatenate it): count call sites per import stub.
    wanted: dict[str, int] = {}
    for imp in imports:
        wanted[f"func_{int(imp['stub'], 16):08X}"] = 0
    call_re = re.compile(r"(func_[0-9A-F]{8})\s*\(ctx\)")
    def_re = re.compile(r"void\s+(func_[0-9A-F]{8})\(ppu_context\* ctx\)")
    for p in files:
        with open(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                for m in call_re.finditer(line):
                    s = m.group(1)
                    if s in wanted:
                        wanted[s] += 1
                m = def_re.search(line)
                if m and m.group(1) in wanted:
                    wanted[m.group(1)] -= 1
    stub_calls = {k[5:]: max(0, v) for k, v in wanted.items()}

    env = dict(os.environ)
    env["PS3_HLE_UNRESOLVED"] = "ok"
    env["PS3_HLE_TRACE"] = "1000000"
    if rep.get("game_dir"):
        env["PS3_VFS_ROOT"] = rep["game_dir"]
    proc = subprocess.Popen([str(exe), str(elf)], cwd=str(exe.parent),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            bufsize=1, env=env)
    called: set[str] = set()
    t0 = time.time()
    while time.time() - t0 < args.seconds:
        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            if proc.poll() is not None:
                break
            time.sleep(0.02)
            continue
        m = re.search(r"nid=(0x[0-9A-Fa-f]{8})", line)
        if m:
            called.add(m.group(1).upper())
    if proc.poll() is None:
        proc.terminate()

    rows = []
    for imp in imports:
        nid = imp["nid"].upper()
        stub = f"{int(imp['stub'], 16):08X}"
        rows.append({"library": imp["library"], "nid": nid, "stub": "0x" + stub,
                     "call_sites": stub_calls.get(stub, 0),
                     "called": nid in called})
    dead = sorted([r for r in rows if r["call_sites"] and not r["called"]],
                  key=lambda r: -r["call_sites"])
    never = [r for r in rows if not r["call_sites"] and not r["called"]]
    dump_json({"windows_seconds": args.seconds,
               "called": sum(1 for r in rows if r["called"]),
               "total": len(rows), "dead_paths": dead,
               "never_referenced": never, "all": rows},
              out / "linkage.json")

    print(f"linkage: {rep.get('title')}")
    print(f"  {sum(1 for r in rows if r['called'])}/{len(rows)} imported functions "
          f"were called in {args.seconds}s")
    if dead:
        print("\n  code exists, never reached (dead paths):")
        for r in dead[:15]:
            print(f"    {r['library']:<16} {r['nid']}  call sites={r['call_sites']}")
    print(f"\n  {out / 'linkage.json'}")
    return 0


def oracle_cmd(args) -> int:
    """Run the same title under RPCS3 and keep its log as the reference.

    The port and the emulator are driven with the same inputs; the emulator's
    log is the behavioural oracle for the parts our runtime does not implement
    yet (SPURS scheduling, RSX, syscall ordering). Nothing is inferred: the log
    is stored next to the port for diffing.
    """
    out = Path(args.dir).resolve()
    rep_path = out / "port.json"
    rep = load_json(rep_path) if rep_path.is_file() else {}
    rpcs3 = None
    for cand in (os.environ.get("PS3REVAMP_RPCS3"), rep.get("rpcs3")):
        if cand and Path(cand).is_file():
            rpcs3 = Path(cand)
            break
    if not rpcs3:
        try:
            from ps3revamp_tools import _find_rpcs3
            rpcs3 = _find_rpcs3()
        except Exception:
            rpcs3 = None
    if not rpcs3:
        eprint("RPCS3 not found; set PS3REVAMP_RPCS3 to rpcs3.exe")
        return 1

    target = None
    if rep.get("game_dir"):
        g = Path(rep["game_dir"])
        target = g if g.is_dir() else None
    if not target and rep.get("elf"):
        target = Path(rep["elf"])
    if not target:
        eprint("no game folder/ELF recorded in port.json")
        return 1

    log_path = None
    for cand in (rpcs3.parent / "log" / "RPCS3.log", rpcs3.parent / "RPCS3.log"):
        if cand.is_file():
            log_path = cand
            break
    before = log_path.stat().st_size if log_path else 0
    log("oracle", f"{rpcs3.name} --no-gui {target} ({args.seconds}s)")
    stdout_path = out / "oracle.stdout.log"
    with open(stdout_path, "w", encoding="utf-8", errors="replace") as so:
        proc = subprocess.Popen([str(rpcs3), "--no-gui", str(target)],
                                cwd=str(rpcs3.parent), stdout=so, stderr=so)
        t0 = time.time()
        while time.time() - t0 < args.seconds and proc.poll() is None:
            time.sleep(0.5)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

    text = ""
    if log_path and log_path.is_file():
        with open(log_path, "rb") as fh:
            fh.seek(before)
            text = fh.read().decode("utf-8", errors="replace")
    try:
        text += "\n" + stdout_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    (out / "oracle.log").write_text(text, encoding="utf-8")

    keys = {
        "spurs_init": r"SPURS|cellSpurs",
        "spu_task": r"SPU|task",
        "gcm": r"RSX|GCM",
        "flip": r"flip|present|frame",
        "fs": r"cellFs|stream\.dat",
    }
    summary = {k: len(re.findall(v, text, re.IGNORECASE)) for k, v in keys.items()}
    ours = out / "probe.log"
    ours_text = ours.read_text(encoding="utf-8", errors="replace") if ours.is_file() else ""
    ours_summary = {k: len(re.findall(v, ours_text, re.IGNORECASE)) for k, v in keys.items()}
    dump_json({"rpcs3": str(rpcs3), "target": str(target),
               "seconds": args.seconds, "oracle_lines": text.count("\n"),
               "oracle": summary, "port": ours_summary,
               "log": str(out / "oracle.log")}, out / "oracle.json")

    print(f"oracle: {rep.get('title')}  ({text.count(chr(10))} log lines)")
    print(f"  {'signal':<10} {'rpcs3':>8} {'port':>8}")
    for k in keys:
        print(f"  {k:<10} {summary[k]:>8} {ours_summary[k]:>8}")
    print(f"\n  {out / 'oracle.log'}\n  {out / 'oracle.json'}")
    return 0


def doctor(args) -> int:
    env = discover(save=not args.no_save)
    ok = True
    print("PS3revamp doctor")
    print("=" * 60)
    print(f"root        : {ROOT}")
    print(f"toolkit     : {env.toolkit or 'MISSING'}")
    print(f"toolchain   : {env.toolchain or '(none; will use system tools)'}")
    print(f"cmake       : {env.cmake or 'MISSING'}")
    print(f"ninja       : {env.ninja or 'MISSING'}")
    print(f"clang       : {env.clang or 'MISSING'}")
    print(f"clang++     : {env.clangxx or 'MISSING'}")
    if env.clang:
        v = _clang_version(env.clang)
        print(f"              clang {v[0]}.{v[1]}.{v[2]} (GNU driver, used by default)")
    print(f"clang-cl    : {env.clang_cl or 'not found (Visual Studio)'}")
    print(f"              (only with PS3REVAMP_USE_CLANG_CL=1)")
    print(f"sccache     : {env.sccache or 'not installed (optional)'}")
    print(f"ghidra      : {env.ghidra_dir or 'not installed (setup --with-ghidra)'}")
    print(f"python      : {env.python}")

    deps = ["capstone", "construct", "Crypto", "tqdm"]
    py = env.python
    missing = [m for m in deps if not _importable(py, m)]
    print(f"py deps     : {'ok' if not missing else 'missing ' + ', '.join(missing)}")
    if missing:
        ok = False

    print(f"IDA (idalib) : {env.ida_python or 'not found'}")
    if not env.ida_python:
        print("              IDA Pro 9.x + idalib are optional: the pipeline uses "
              "ps3recomp's own OPD/CFG function discovery.")
        print("              To use IDA, activate idalib in its Python and set "
              "ida_python in config.json.")
    print()
    if not env.toolkit:
        print("FAIL: no ps3recomp toolkit found. Put one at ./toolkit/ps3recomp "
              "or pass --toolkit.")
        ok = False
    if not (env.cmake and env.ninja and env.clang and env.clangxx):
        print("WARN: no complete C++ toolchain; lifting works, building may not.")
    print("OK" if ok else "PROBLEMS FOUND")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        prog="ps3revamp",
        description="Turn a decrypted PS3 EBOOT.elf into a native C++ port project.")
    ap.add_argument("--toolkit", help="path to a ps3recomp checkout")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="check toolchain, toolkit, python deps, IDA")
    d.add_argument("--no-save", action="store_true")
    d.set_defaults(fn=doctor)

    s = sub.add_parser("setup", help="download or verify ps3recomp + toolchain")
    s.add_argument("--offline", action="store_true",
                   help="verify the bundled components without any network access")
    s.add_argument("--with-ghidra", action="store_true",
                   help="also fetch Ghidra into toolchain/ghidra (independent decoder)")
    s.set_defaults(fn=setup_cmd)

    lr = sub.add_parser("learn", help="record a finished port in the knowledge base")
    lr.add_argument("dir")
    lr.set_defaults(fn=learn_cmd)

    kbp = sub.add_parser("kb", help="inspect what previous titles taught the tool")
    kbp.add_argument("--titles", action="store_true", help="list recorded titles")
    kbp.add_argument("--nid", help="is this NID safe to stub? (history)")
    kbp.add_argument("--library", help="recurring coverage gaps for a library")
    kbp.add_argument("--limit", type=int, default=50)
    kbp.set_defaults(fn=kb_cmd)

    tp = sub.add_parser("tools", help="list optional tools (Ghidra/IDA/RPCS3/...)"
                                       " and whether they are available")
    tp.set_defaults(fn=tools_cmd)

    dp = sub.add_parser("docs", help="curated PS3/PPC reference cache")
    dp.add_argument("--topic", help="topic id (ppc, nid, lv2, gcm, spu, spurs, elf)")
    dp.add_argument("--fetch", action="store_true",
                    help="download the topic from its allowlisted source")
    dp.add_argument("--all", action="store_true", help="with --fetch: fetch every topic")
    dp.set_defaults(fn=docs_cmd)

    a = sub.add_parser("analyze", help="inspect an EBOOT.elf without lifting")
    a.add_argument("elf")
    a.add_argument("--game", help="game folder (for PARAM.SFO title id)")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=analyze_cmd)

    ad = sub.add_parser("analyze-dir", help="analyse every EBOOT.elf under a folder")
    ad.add_argument("dir")
    ad.add_argument("--limit", type=int, default=50)
    ad.set_defaults(fn=analyze_dir_cmd)

    rt = sub.add_parser("route", help="compute the best conversion route for one EBOOT")
    rt.add_argument("elf")
    rt.add_argument("--game")
    rt.set_defaults(fn=route_cmd)

    gp = sub.add_parser("gaps", help="prioritised backlog of unimplemented firmware")
    gp.add_argument("--kb", action="store_true", help="include knowledge-base history")
    gp.set_defaults(fn=gaps_cmd)

    pb = sub.add_parser("probe", help="live behavioural probe of a built port "
                                      "(blocked threads, SPU spins, queue state)")
    pb.add_argument("dir")
    pb.add_argument("--seconds", type=int, default=20)
    pb.add_argument("--hle-trace", type=int, default=100)
    pb.add_argument("--modes", action="store_true",
                    help="try the known compatibility modes and compare verdicts")
    pb.set_defaults(fn=probe_cmd)

    lk = sub.add_parser("linkage", help="static call sites vs runtime calls per "
                                        "firmware import (finds dead code paths)")
    lk.add_argument("dir")
    lk.add_argument("--seconds", type=int, default=20)
    lk.set_defaults(fn=linkage_cmd)

    oc = sub.add_parser("oracle", help="run the title under RPCS3 and keep its log "
                                        "as the behavioural reference")
    oc.add_argument("dir")
    oc.add_argument("--seconds", type=int, default=40)
    oc.set_defaults(fn=oracle_cmd)

    p = sub.add_parser("port", help="full pipeline: ELF -> C++ -> (build)")
    p.add_argument("elf")
    p.add_argument("-o", "--out", help="output port directory")
    p.add_argument("--game", help="game folder (PARAM.SFO, assets)")
    p.add_argument("--name", help="port name (default: PARAM.SFO title)")
    p.add_argument("--analyzer", default="auto",
                   choices=["auto", "native", "ida", "ghidra"])
    p.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4)))
    p.add_argument("--chunk-lines", type=int, default=None,
                   help="smaller = more, smaller C++ files (memory-bound hosts)")
    p.add_argument("--chunks", type=int, default=None,
                   help="number of C++ TUs (default: auto, ~2 per compile job)")
    p.add_argument("--names", help="ghidra_names.py output for readable comments")
    p.add_argument("--spu", dest="spu", action="store_true", default=None,
                   help="force SPU extraction+lifting (default: auto when present)")
    p.add_argument("--no-spu", dest="spu", action="store_false",
                   help="skip SPU work")
    p.add_argument("--build", action="store_true", help="build the native binary")
    p.add_argument("--test", type=int, default=0, metavar="SECONDS",
                   help="boot the built port for N seconds and diagnose it")
    p.add_argument("--fetch-docs", action="store_true",
                   help="if the boot test points at a knowledge gap, download the "
                        "matching PPC/lv2/GCM/SPU reference into knowledge/docs")
    p.add_argument("--opt", type=int, default=None, choices=[0, 1, 2, 3],
                   help="optimisation for lifted TUs (default: 2, or what worked "
                        "for this title size)")
    p.add_argument("--fast", action="store_true",
                   help="build at -O0: much faster to compile, slower to run")
    p.add_argument("--no-learn", action="store_true",
                   help="do not record this run in the knowledge base")
    p.add_argument("--package", action="store_true",
                   help="assemble the standalone game folder (dist/) after the build")
    p.add_argument("--deep", action="store_true",
                   help="after the boot test, also run probe + linkage analysis")
    p.add_argument("--reuse-lift", action="store_true",
                   help="skip the lift if recompiled/ already has ppu_recomp.h")
    p.add_argument("--lifter-arg", action="append",
                   help="extra flag passed straight to ppu_lifter.py")
    p.set_defaults(fn=port)

    b = sub.add_parser("build", help="(re)build an existing port project")
    b.add_argument("dir")
    b.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4)))
    b.add_argument("--fast", action="store_true", help="build at -O0")
    b.add_argument("--opt", type=int, default=2, choices=[0, 1, 2, 3])
    b.set_defaults(fn=build_cmd)

    pl = sub.add_parser("play", help="run a finished port")
    pl.add_argument("dir")
    pl.set_defaults(fn=play_cmd)

    pk = sub.add_parser("package", help="assemble a normal Windows game folder "
                                        "(<Title>.exe + PS3_GAME/USRDIR)")
    pk.add_argument("dir")
    pk.add_argument("--out")
    pk.add_argument("--link-assets", action="store_true",
                    help="junction the assets instead of copying them")
    pk.add_argument("--no-assets", action="store_true",
                    help="exe and DLLs only (assets stay where they are)")
    pk.set_defaults(fn=package_cmd)

    au = sub.add_parser("auto", help="one click: EBOOT.elf -> lifted port -> build "
                                     "-> boot test -> packaged game")
    au.add_argument("elf")
    au.add_argument("-o", "--out")
    au.add_argument("--game")
    au.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4)))
    au.set_defaults(fn=None)   # argv is rewritten to `port` before parsing

    # `auto` is the one-click path: the same as `port` but always builds, boot-
    # tests and packages. Rewrite it into the real command before parsing so
    # every port option keeps working unchanged.
    _argv = list(sys.argv[1:] if argv is None else argv)
    if _argv and _argv[0] == "auto":
        _argv = ["port"] + _argv[1:] + ["--build", "--test", "20", "--package"]
    elif _argv and _argv[0] == "port":
        # A user who points the tool at an EBOOT expects a runnable port, not
        # just generated C++. Build/test/package are the default; opt out with
        # --no-build/--no-package.
        pass
    argv = _argv

    args = ap.parse_args(argv)
    if getattr(args, "toolkit", None):
        os.environ["PS3RECOMP_DIR"] = args.toolkit
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        eprint("\ninterrupted")
        return 130
    except Exception as e:
        eprint(f"\n! {e}")
        return 1


def analyze_cmd(args) -> int:
    elf = Path(args.elf).resolve()
    meta = read_ps3_elf(elf)
    game = Path(args.game).resolve() if args.game else None
    sfo = {}
    if game:
        sfo = parse_sfo(game.parent / "PARAM.SFO") or parse_sfo(game / "PARAM.SFO")
    env = discover()
    info = {
        "file": str(elf), "size": meta.size, "sha256": meta.sha256,
        "entry_opd": hex(meta.entry), "segments": meta.segments,
        "text_end": hex(meta.text_end), "has_symtab": meta.has_symtab,
        "sfo": sfo,
    }
    if env.toolkit:
        tmp = WORK / "analyze"
        files = stage_loader(env, elf, tmp)
        imports = load_json(files["imports"])
        funcs = load_json(files["functions"])
        info["functions"] = len(funcs)
        info["imports"] = imports
        info["hle"] = hle_nid_coverage(env, imports)
        names = resolve_nids(env, imports)
        info["plan"] = port_plan(imports, info["hle"], names)
    if args.json:
        print(json.dumps(info, indent=2))
    else:
        print(f"file        : {info['file']}")
        print(f"size        : {human(meta.size)}")
        print(f"entry OPD   : {info['entry_opd']}")
        print(f"segments    : {len(meta.segments)}  text end {info['text_end']}")
        print(f"symtab      : {meta.has_symtab}")
        if sfo:
            print(f"title       : {sfo.get('TITLE')} [{sfo.get('TITLE_ID')}] "
                  f"{sfo.get('APP_VER', '')}")
        if "functions" in info:
            print(f"functions   : {info['functions']}")
            print(f"imports     : {len(info['imports'])}")
            h = info["hle"]
            if h.get("ok"):
                print(f"HLE covered : {h['covered']}/{h['total']} ({h['percent']}%)")
    return 0


def build_cmd(args) -> int:
    env = discover()
    proj = Path(args.dir).resolve()
    if (proj / "project").is_dir():
        proj = proj / "project"
    ok, logtext = build_project(env, proj, args.jobs, fast=args.fast,
                                opt=args.opt if args.opt is not None else 2)
    print(logtext[-4000:])
    print("BUILD OK" if ok else "BUILD FAILED")
    return 0 if ok else 1


def package_cmd(args) -> int:
    """Assemble a normal Windows game folder: <Title>.exe + PS3_GAME/USRDIR.

    The generated exe looks for PS3_GAME\\USRDIR\\EBOOT.elf next to itself when
    started without arguments, and derives the VFS root from that path, so the
    packaged folder is double-clickable and relocatable.
    """
    src = Path(args.dir).resolve()
    rep_path = src / "port.json"
    if not rep_path.is_file():
        eprint(f"no port.json in {src}")
        return 1
    rep = load_json(rep_path)
    exe = Path(rep.get("exe") or "")
    if not exe.is_file():
        eprint("no built executable -- run `ps3revamp build <port>` first")
        return 1

    title = window_title(rep.get("title") or "game", rep.get("sfo"))
    safe = re.sub(r'[<>:"/\\|?*]', "-", title).strip() or "game"
    dst = Path(args.out).resolve() if args.out else src / "dist"
    dst.mkdir(parents=True, exist_ok=True)

    for dll in exe.parent.glob("*.dll"):
        shutil.copy2(dll, dst / dll.name)
    target_exe = dst / f"{safe}.exe"
    shutil.copy2(exe, target_exe)
    log("package", f"exe -> {target_exe}")

    game = Path(rep["game_dir"]).resolve() if rep.get("game_dir") else None
    if game and not args.no_assets:
        usrdir = game / "USRDIR"
        dest = dst / "PS3_GAME" / "USRDIR"
        if dest.exists() or dest.is_symlink():
            try:
                if dest.is_symlink():
                    dest.unlink()
                else:
                    subprocess.run(["cmd", "/c", "rmdir", str(dest)],
                                   capture_output=True, text=True)
                    if dest.exists():
                        shutil.rmtree(dest)
            except Exception:
                pass
        if not usrdir.is_dir():
            eprint(f"! no USRDIR in {game}; skipping assets")
        elif args.link_assets and sys.platform == "win32":
            dest.parent.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(dest), str(usrdir)],
                               capture_output=True, text=True)
            log("package", "assets linked" if r.returncode == 0 else
                 "link failed, copying instead")
            if r.returncode != 0:
                shutil.copytree(usrdir, dest)
        else:
            log("package", f"copying assets from {usrdir} (this can take a while)")
            shutil.copytree(usrdir, dest)
        sfo = find_sfo(game, exe)
        if sfo:
            sfo_path = game / "PARAM.SFO"
            if sfo_path.is_file():
                shutil.copy2(sfo_path, dst / "PS3_GAME" / "PARAM.SFO")
            else:
                parent_sfo = game.parent / "PARAM.SFO"
                if parent_sfo.is_file():
                    shutil.copy2(parent_sfo, dst / "PS3_GAME" / "PARAM.SFO")

    dump_json({
        "title": rep.get("title"), "title_id": (rep.get("sfo") or {}).get("TITLE_ID"),
        "exe": str(target_exe), "packaged": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_port": str(src), "assets": "linked" if args.link_assets else "copied",
    }, dst / "ps3revamp.game.json")
    (dst / "README.txt").write_text(
        f"{rep.get('title')}\n\n"
        f"Run: {safe}.exe\n\n"
        f"This folder is the port. It contains only translated code and the\n"
        f"components needed to run it; the game data was copied from your own\n"
        f"disc/update, not redistributed.\n", encoding="utf-8")
    log("package", f"done: {dst}")
    print(f"Game folder : {dst}")
    print(f"Launch      : \"{target_exe}\"")
    return 0


def play_cmd(args) -> int:
    out = Path(args.dir).resolve()
    rep_path = out / "port.json"
    if rep_path.is_file():
        rep = load_json(rep_path)
    else:
        rep = {}
    exe = rep.get("exe")
    elf = rep.get("elf")
    if not exe or not Path(exe).is_file():
        eprint("No built executable in this port. Run: ps3revamp build <dir> --build")
        return 1
    game = rep.get("game_dir")
    env = dict(os.environ)
    if game:
        env["PS3_VFS_ROOT"] = game
    log("play", f"{exe} {elf}")
    return subprocess.call([exe, elf], env=env, cwd=str(Path(exe).parent))


if __name__ == "__main__":
    sys.exit(main())



