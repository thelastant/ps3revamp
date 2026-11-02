"""
ps3revamp_spu -- find SPU programs inside the title's own data files.

Some titles do not embed every SPU program in the EBOOT: they stream it out of
`stream.dat` / `.sdat` / other payloads and hand it to cellSpurs at run time.
The runtime then reports

    [spu_workload] dispatch MISS fp=0x... size=...

This module scans local files for ELF32 big-endian EM_SPU images, fingerprints
the executable PT_LOAD exactly like the runtime does, and reports candidates so
the driver can lift and register the missing program.

Usage:
    python ps3revamp_spu.py scan <file> [<file> ...]
    python ps3revamp_spu.py extract <file> <offset> <size> <out.elf>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent / "toolkit" / "ps3recomp" / "tools"
sys.path.insert(0, str(TOOLS))

try:
    import build_spu_workloads as bsw
except Exception as e:  # pragma: no cover
    bsw = None
    _IMPORT_ERROR = str(e)


def scan_bytes(data: bytes, name: str = "", max_hits: int = 64):
    """Yield {file, offset, size, fp} for every valid SPU ELF in `data`."""
    hits = []
    start = 0
    while len(hits) < max_hits:
        off = data.find(b"\x7fELF", start)
        if off < 0:
            break
        start = off + 4
        chunk = data[off:off + (8 << 20)]
        if not bsw.is_spu_elf(chunk):
            continue
        size = bsw.img_size(chunk)
        if not (0x40 <= size <= (8 << 20)) or off + size > len(data):
            continue
        img = chunk[:size]
        fp = bsw.text_fp(img)
        if fp is None:
            continue
        hits.append({"file": name, "offset": off, "size": size,
                     "fp": f"0x{fp:016X}"})
    return hits


def scan_file(path: Path, max_hits: int = 64):
    try:
        if path.stat().st_size > (4 << 30):
            return []
        return scan_bytes(path.read_bytes(), str(path), max_hits)
    except Exception:
        return []


def extract(path: Path, offset: int, size: int, out: Path) -> bool:
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(size)
        if len(data) != size or not bsw.is_spu_elf(data):
            return False
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        return True
    except Exception:
        return False


def main(argv: list[str]) -> int:
    if not bsw:
        print(json.dumps({"error": f"toolkit import failed: {_IMPORT_ERROR}"}))
        return 1
    if len(argv) >= 2 and argv[0] == "scan":
        out = []
        for a in argv[1:]:
            out += scan_file(Path(a))
        print(json.dumps(out, indent=2))
        return 0
    if len(argv) == 5 and argv[0] == "extract":
        ok = extract(Path(argv[1]), int(argv[2], 0), int(argv[3], 0), Path(argv[4]))
        print(json.dumps({"ok": ok}))
        return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
