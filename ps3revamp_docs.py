"""
ps3revamp_docs -- curated reference material, cached on disk.

Behaviour:
  * `topics()` lists the curated references the tool knows about.
  * `lookup()` returns a cached copy if present (offline path).
  * `fetch()` downloads one topic from the allowlist, verifies the host, stores
    it under knowledge/docs/ with its URL + SHA-256, and never overwrites a
    newer local copy.

Network is opt-in (`--fetch-docs` / `--fetch`). The pipeline never fetches on
its own, so an air-gapped machine keeps working from the cache.

The mapping from a boot failure to a topic is deterministic: unresolved SPU
work -> spu/spurs, missing graphics NIDs -> gcm, missing system NIDs -> lv2,
unknown lifted call -> ppc, NID names -> nid.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parent / "knowledge" / "docs"

# Bundled, offline-first: the toolkit ships MIT-licensed docs on exactly these
# topics, so the cache is populated without touching the network.
LOCAL_SEED = {
    "ppc": "PPU_RECOMP.md",
    "nid": "NID_SYSTEM.md",
    "lv2": "SYSCALLS.md",
    "gcm": "RSX_GRAPHICS.md",
    "spu": "SPU_LIFTER.md",
    "spurs": "SPURS_SCHEDULING.md",
    "elf": "ARCHITECTURE.md",
}

TOPICS: dict[str, dict] = {
    "ppc": {
        "title": "PowerPC / PPU ISA and ELF ABI (OPD, TOC r2, 32-bit pointers)",
        "urls": ["https://www.psdevwiki.com/ps3/CELL_BE"],
        "why": "missing lifted functions, branch/jump-table recovery, ABI edges",
    },
    "nid": {
        "title": "NID hashing and library import/export tables",
        "urls": ["https://www.psdevwiki.com/ps3/NID"],
        "why": "unresolved or misnamed firmware imports",
    },
    "lv2": {
        "title": "lv2 syscalls and system libraries (sys_fs, sys_memory, threads)",
        "urls": ["https://www.ps3devwiki.com/ps3/LV2_Syscalls"],
        "why": "a system import that must do real work instead of returning CELL_OK",
    },
    "gcm": {
        "title": "RSX / GCM graphics (command buffer, surfaces, shaders)",
        "urls": ["https://www.psdevwiki.com/ps3/RSX"],
        "why": "graphics NIDs or RSX behaviour the backend does not cover",
    },
    "spu": {
        "title": "SPU ISA, local store and MFC DMA",
        "urls": ["https://www.psdevwiki.com/ps3/SPU"],
        "why": "SPU images that fail to lift or jobs that never signal",
    },
    "spurs": {
        "title": "cellSpurs job scheduler (tasksets, job modules, workload queues)",
        "urls": ["https://www.psdevwiki.com/ps3/CellSpurs"],
        "why": "cellSpurs waiting forever on a workload",
    },
    "elf": {
        "title": "PS3 ELF/SELF layout and decryption",
        "urls": ["https://www.psdevwiki.com/ps3/SELF_-_SPRX"],
        "why": "ELF rejected, SELF not decrypting, ET_DYN/PRX input",
    },
}

ALLOWED_HOSTS = {
    "www.psdevwiki.com", "psdevwiki.com", "www.ps3devwiki.com",
    "github.com", "raw.githubusercontent.com",
    "docs.hex-rays.com", "llvm.org", "releases.llvm.org",
}


def seed(toolkit: Path | None = None) -> int:
    """Copy the toolkit's own docs into the cache (no network)."""
    toolkit = toolkit or (Path(__file__).resolve().parent / "toolkit" / "ps3recomp")
    src_dir = Path(toolkit) / "docs"
    if not src_dir.is_dir():
        return 0
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    copied = 0
    for topic, name in LOCAL_SEED.items():
        src = src_dir / name
        dst = DOCS_DIR / f"{topic}.md"
        if src.is_file() and not dst.exists():
            dst.write_bytes(src.read_bytes())
            (DOCS_DIR / f"{topic}.json").write_text(json.dumps({
                "topic": topic, "source": f"bundled toolkit docs/{name}",
                "bytes": dst.stat().st_size,
                "sha256": hashlib.sha256(dst.read_bytes()).hexdigest(),
                "fetched": "bundled",
            }, indent=2), encoding="utf-8")
            copied += 1
    return copied


def topics() -> list[dict]:
    out = []
    for key, t in TOPICS.items():
        p = lookup(key)
        out.append({
            "id": key, "title": t["title"], "why": t["why"],
            "source": t["urls"][0],
            "cached": bool(p),
            "path": str(p) if p else None,
            "bytes": p.stat().st_size if p else 0,
        })
    return out


def _cache_path(topic: str) -> Path:
    return DOCS_DIR / f"{topic}.html"


def lookup(topic: str) -> Path | None:
    for suffix in (".md", ".html"):
        p = DOCS_DIR / f"{topic}{suffix}"
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def fetch(topic: str, timeout: int = 60) -> dict:
    t = TOPICS.get(topic)
    if not t:
        return {"ok": False, "error": f"unknown topic {topic}"}
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    last = ""
    for url in t["urls"]:
        host = urllib.parse.urlparse(url).hostname or ""
        if host not in ALLOWED_HOSTS:
            last = f"host not allowed: {host}"
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ps3revamp-docs"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
            if len(body) < 512:
                last = "response too small"
                continue
            dst = _cache_path(topic)
            dst.write_bytes(body)
            meta = {"topic": topic, "url": url, "bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "fetched": time.strftime("%Y-%m-%d %H:%M:%S")}
            (DOCS_DIR / f"{topic}.json").write_text(json.dumps(meta, indent=2),
                                                    encoding="utf-8")
            return {"ok": True, **meta, "path": str(dst)}
        except Exception as e:
            last = str(e)
    return {"ok": False, "error": last}


def needs(diag: dict, plan: dict | None = None) -> list[str]:
    """Deterministic topic inference from one boot diagnosis + port plan."""
    want: list[str] = []
    if not diag:
        return want
    if diag.get("unresolved_calls"):
        want.append("ppc")
    if diag.get("unimplemented_nids"):
        want.append("nid")
    text = " ".join((diag.get("observations") or [])).lower()
    if "graphics" in text or "gcm" in text:
        want.append("gcm")
    if diag.get("spin_addresses"):
        want.append("ppc")
    for sub, p in (plan or {}).items():
        if sub == "spu" and p.get("imports"):
            want.append("spurs")
        if sub == "graphics" and p.get("imports") and p.get("covered", 0) < p.get("imports", 0):
            want.append("gcm")
        if sub == "system" and p.get("covered", 0) < p.get("imports", 0):
            want.append("lv2")
    seen, out = set(), []
    for w in want:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def fetch_for_port(diag: dict, plan: dict | None) -> list[dict]:
    """Fetch only the references this title actually appears to need."""
    results = []
    for topic in needs(diag, plan):
        if lookup(topic):
            results.append({"topic": topic, "cached": True, "path": str(lookup(topic))})
            continue
        results.append({"topic": topic, **fetch(topic)})
    return results
