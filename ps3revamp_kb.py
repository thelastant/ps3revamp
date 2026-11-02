"""
ps3revamp_kb -- the deterministic knowledge base.

Nothing here calls a model. Every "learning" step is a plain aggregate over
facts the pipeline already produced (imports, NIDs, boot log, settings). The
store is a single SQLite file so a port is reproducible: same ELF + same KB =
same decisions.

What is learned, per title
--------------------------
* identity:   title id, name, ELF SHA-256, first/last seen, number of runs
* surface:    every firmware import (library, NID, resolved name, HLE coverage)
* behaviour:  boot verdict, reached milestones, unresolved calls, unimplemented
              NIDs, and whether the title still progressed past them
* patterns:   the settings that produced a build/boot (chunks, opt, jobs)

What the pipeline asks back
---------------------------
* known_blockers(sha)      - what stopped this title last time
* safe_to_stub(nid)        - has any title survived this NID being unimplemented
* preferred_settings(size) - settings that worked for similar titles
* coverage_gaps(library)   - NIDs that repeatedly need handlers
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS titles (
    sha TEXT PRIMARY KEY,
    title_id TEXT,
    title TEXT,
    first_seen TEXT,
    last_seen TEXT,
    runs INTEGER DEFAULT 0,
    verdict TEXT,
    progress INTEGER DEFAULT 0,     -- 0 none, 1 booted, 2 graphics, 3 frames
    settings TEXT                   -- json
);
CREATE TABLE IF NOT EXISTS imports (
    sha TEXT,
    library TEXT,
    nid TEXT,
    name TEXT,
    covered INTEGER,
    PRIMARY KEY (sha, nid)
);
CREATE TABLE IF NOT EXISTS blockers (
    sha TEXT,
    kind TEXT,          -- nid | call | crash | spin
    detail TEXT,
    name TEXT,
    resolution TEXT,
    seen_at TEXT,
    PRIMARY KEY (sha, kind, detail)
);
CREATE TABLE IF NOT EXISTS outcomes (
    nid TEXT,
    library TEXT,
    titles INTEGER DEFAULT 0,
    survived INTEGER DEFAULT 0,     -- titles that progressed with it unimplemented
    PRIMARY KEY (nid)
);
CREATE TABLE IF NOT EXISTS settings (
    bucket TEXT PRIMARY KEY,        -- function-count bucket
    chunks INTEGER,
    opt INTEGER,
    jobs INTEGER,
    samples INTEGER DEFAULT 0
);
"""

MILESTONE_RANK = {"graphics": 2, "frames": 3, "audio": 2, "assets": 1}


class KnowledgeBase:
    def __init__(self, root: Path):
        self.path = root / "knowledge" / "kb.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ------------------------------------------------------------------ learn
    def learn(self, report: dict, boot: dict | None) -> dict:
        """Ingest one finished port. Returns a short summary of what changed."""
        sha = report.get("elf_sha256") or ""
        if not sha:
            return {"ok": False, "reason": "no sha256 in report"}
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        sfo = report.get("sfo") or {}
        verdict = (boot or {}).get("verdict") or (
            "built" if (report.get("build") or {}).get("ok") else "lifted")
        progress = self._progress(boot)

        self.db.execute(
            """INSERT INTO titles (sha, title_id, title, first_seen, last_seen,
                                  runs, verdict, progress, settings)
               VALUES (?,?,?,?,?,1,?,?,?)
               ON CONFLICT(sha) DO UPDATE SET
                 last_seen=excluded.last_seen,
                 runs=runs+1,
                 title_id=COALESCE(excluded.title_id, titles.title_id),
                 title=COALESCE(excluded.title, titles.title),
                 verdict=excluded.verdict,
                 progress=MAX(titles.progress, excluded.progress),
                 settings=excluded.settings""",
            (sha, sfo.get("TITLE_ID"), sfo.get("TITLE"), now, now, verdict,
             progress, json.dumps(report.get("timings") or {})))

        names = (report.get("nid_names") or {})
        covered_nids = self._covered_nids(report)
        imports = report.get("_imports") or []
        for imp in imports:
            nid = imp["nid"].upper().replace("0X", "0x")
            self.db.execute(
                """INSERT INTO imports (sha, library, nid, name, covered)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(sha, nid) DO UPDATE SET
                     library=excluded.library, name=excluded.name,
                     covered=excluded.covered""",
                (sha, imp["library"], nid, names.get(nid) or names.get(nid.upper()),
                 1 if nid in covered_nids else 0))

        # Blockers: unimplemented NIDs and unresolved lifted calls.
        unimplemented = (boot or {}).get("unimplemented_named") or {}
        if not unimplemented and boot:
            for nid, _lib in boot.get("unimplemented_nids") or []:
                unimplemented[nid] = (report.get("nid_names") or {}).get(nid)
        for nid, name in unimplemented.items():
            self.db.execute(
                """INSERT OR IGNORE INTO blockers (sha, kind, detail, name,
                                                  resolution, seen_at)
                   VALUES (?,?,?,?,?,?)""",
                (sha, "nid", nid, name or "", "stubbed CELL_OK", now))
            self.db.execute(
                """INSERT INTO outcomes (nid, library, titles, survived)
                   VALUES (?,?,1,?)
                   ON CONFLICT(nid) DO UPDATE SET
                     titles=titles+1, survived=survived+excluded.survived""",
                (nid, self._library_of(nid, imports) or "?",
                 1 if progress >= 1 else 0))
        for call in (boot or {}).get("unresolved_calls") or []:
            self.db.execute(
                """INSERT OR IGNORE INTO blockers (sha, kind, detail, name,
                                                  resolution, seen_at)
                   VALUES (?,?,?,?,?,?)""",
                (sha, "call", call, "", "relift with CFG seeds", now))

        self._learn_settings(report)
        self.db.commit()
        return {"ok": True, "sha": sha, "verdict": verdict,
                "blockers": len(unimplemented) +
                            len((boot or {}).get("unresolved_calls") or []),
                "db": str(self.path)}

    def _learn_settings(self, report: dict):
        n = (report.get("lift") or {}).get("units")
        lift = report.get("_functions") or 0
        if not lift:
            return
        bucket = self._bucket(lift)
        st = report.get("_settings") or {}
        self.db.execute(
            """INSERT INTO settings (bucket, chunks, opt, jobs, samples)
               VALUES (?,?,?,?,1)
               ON CONFLICT(bucket) DO UPDATE SET
                 samples=samples+1,
                 chunks=COALESCE(excluded.chunks, settings.chunks),
                 opt=COALESCE(excluded.opt, settings.opt),
                 jobs=COALESCE(excluded.jobs, settings.jobs)""",
            (bucket, st.get("chunks"), st.get("opt"), st.get("jobs")))

    # ------------------------------------------------------------------- read
    def known_blockers(self, sha: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT kind, detail, name, resolution FROM blockers WHERE sha=? "
            "ORDER BY kind, detail", (sha,)).fetchall()
        return [{"kind": r[0], "detail": r[1], "name": r[2], "resolution": r[3]}
                for r in rows]

    def safe_to_stub(self, nid: str) -> dict | None:
        row = self.db.execute(
            "SELECT titles, survived FROM outcomes WHERE nid=?", (nid,)).fetchone()
        if not row:
            return None
        return {"titles": row[0], "survived": row[1],
                "safe": row[0] > 0 and row[1] == row[0]}

    def preferred_settings(self, functions: int) -> dict:
        row = self.db.execute(
            "SELECT chunks, opt, jobs, samples FROM settings WHERE bucket=?",
            (self._bucket(functions),)).fetchone()
        if not row or not row[3]:
            return {}
        return {"chunks": row[0], "opt": row[1], "jobs": row[2], "samples": row[3]}

    def coverage_gaps(self, library: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT nid, name, SUM(1-covered), COUNT(*) FROM imports "
            "WHERE library=? GROUP BY nid ORDER BY SUM(1-covered) DESC, nid LIMIT 50",
            (library,)).fetchall()
        return [{"nid": r[0], "name": r[1], "missing_runs": r[2], "titles": r[3]}
                for r in rows if r[2]]

    def stats(self) -> dict:
        q = self.db.execute
        return {
            "db": str(self.path),
            "titles": q("SELECT COUNT(*) FROM titles").fetchone()[0],
            "imports": q("SELECT COUNT(*) FROM imports").fetchone()[0],
            "blockers": q("SELECT COUNT(*) FROM blockers").fetchone()[0],
            "nids_seen": q("SELECT COUNT(*) FROM outcomes").fetchone()[0],
            "settings_buckets": q("SELECT COUNT(*) FROM settings").fetchone()[0],
            "booted": q("SELECT COUNT(*) FROM titles WHERE progress>=1").fetchone()[0],
            "graphics": q("SELECT COUNT(*) FROM titles WHERE progress>=2").fetchone()[0],
        }

    def titles(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            "SELECT title, title_id, verdict, runs, progress, last_seen "
            "FROM titles ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()
        return [{"title": r[0], "title_id": r[1], "verdict": r[2],
                 "runs": r[3], "progress": r[4], "last_seen": r[5]} for r in rows]

    # ------------------------------------------------------------------ util
    @staticmethod
    def _bucket(n: int) -> str:
        if n < 1000:
            return "s"
        if n < 5000:
            return "m"
        if n < 15000:
            return "l"
        return "xl"

    @staticmethod
    def _progress(boot: dict | None) -> int:
        if not boot:
            return 0
        p = 1
        for obs in boot.get("observations") or []:
            low = obs.lower()
            for key, rank in MILESTONE_RANK.items():
                if key in low:
                    p = max(p, rank)
        return p

    @staticmethod
    def _covered_nids(report: dict) -> set[str]:
        """NIDs covered by the HLE table (missing list is the complement)."""
        hle = report.get("hle") or {}
        if not hle.get("ok"):
            return set()
        missing = {m["nid"].upper() for m in hle.get("missing") or []}
        imports = report.get("_imports") or []
        return {i["nid"].upper() for i in imports} - missing

    @staticmethod
    def _library_of(nid: str, imports: list[dict]) -> str | None:
        up = nid.upper()
        for i in imports:
            if i["nid"].upper() == up:
                return i["library"]
        return None
