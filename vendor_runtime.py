"""Apply the project's vendored-runtime patch to a pristine ps3recomp tree.

`toolkit/` is git-ignored, so the runtime this project ships (our loader changes,
the Project Pyrrhus SPURS series and the fixes in docs/SPURS_WORK.md) lives in a
single patch, patches/0100-runtime-complete.patch, generated against upstream
ps3recomp v0.18.0. `ps3revamp.py setup` applies it after fetching the toolkit.

The applier is deliberately self-contained: `git apply` silently skips paths
under the ignored `toolkit/` tree, and `patch.exe` is not guaranteed on a clean
Windows install.
"""
from __future__ import annotations

import pathlib
import re
import shutil

HERE = pathlib.Path(__file__).resolve().parent
PATCH = HERE / "patches" / "0100-runtime-complete.patch"

# A file the patch adds. Its presence means the tree is already prepared.
MARKER = pathlib.Path("libs/input/ps3revamp_compat.c")


class PatchError(RuntimeError):
    pass


def _parse(data: str):
    """Yield (old_path, new_path, hunks). A hunk is (old_start, old_len,
    new_start, new_len, [lines])."""
    lines = data.splitlines(True)
    i, n = 0, len(lines)
    while i < n:
        if not lines[i].startswith("--- "):
            i += 1
            continue
        old = lines[i][4:].strip()
        if i + 1 >= n or not lines[i + 1].startswith("+++ "):
            raise PatchError(f"malformed header at line {i + 1}")
        new = lines[i + 1][4:].strip()
        i += 2
        hunks = []
        while i < n and lines[i].startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", lines[i])
            if not m:
                raise PatchError(f"malformed hunk header: {lines[i].strip()}")
            os_, ol = int(m.group(1)), int(m.group(2) or 1)
            ns, nl = int(m.group(3)), int(m.group(4) or 1)
            i += 1
            body = []
            while i < n and not lines[i].startswith(("@@", "--- ", "diff --git ")):
                body.append(lines[i])
                i += 1
            hunks.append((os_, ol, ns, nl, body))
        yield old, new, hunks


def _split_path(p: str) -> str:
    p = p.strip()
    if "\t" in p:
        p = p.split("\t", 1)[0]
    if p.startswith(("a/", "b/")):
        p = p[2:]
    return p


def _find(hay, needle, near, span=400):
    """Return the index of needle in hay starting near `near` (0-based)."""
    if not needle:
        return max(0, min(near, len(hay)))
    best = None
    lo, hi = max(0, near - span), min(len(hay), near + span + len(needle))
    for i in range(lo, hi):
        if hay[i:i + len(needle)] == needle:
            if best is None or abs(i - near) < abs(best - near):
                best = i
    return best


def _apply_file(path: pathlib.Path, hunks) -> None:
    raw = path.read_bytes() if path.is_file() else b""
    text = raw.decode("utf-8", errors="surrogateescape").replace("\r\n", "\n")
    src = text.splitlines(True)
    out = list(src)
    # Apply hunks from the bottom so earlier offsets stay valid.
    for os_, ol, ns, nl, body in reversed(hunks):
        needle = [l[1:] for l in body if l[:1] in (" ", "-")]
        repl = [l[1:] for l in body if l[:1] in (" ", "+")]
        if not needle:
            at = max(0, min(os_ - 1, len(out)))
            out[at:at] = repl
            continue
        at = _find(out, needle, max(0, os_ - 1))
        if at is None:
            raise PatchError(f"hunk does not apply to {path}: {body[0].strip() if body else ''}")
        out[at:at + len(needle)] = repl
    text = "".join(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8", errors="surrogateescape"))


def apply(patch: pathlib.Path = PATCH, target: pathlib.Path | None = None) -> str:
    target = target or (HERE / "toolkit" / "ps3recomp")
    if not patch.is_file():
        return f"no runtime patch ({patch.name}); using the toolkit as fetched"
    if (target / MARKER).is_file():
        return "runtime patch already applied"

    parsed = list(_parse(patch.read_text(encoding="utf-8", errors="surrogateescape")))
    # Dry-run every file first so a partial apply can never leave a broken tree.
    touched = []
    for old, new, hunks in parsed:
        op, np_ = _split_path(old), _split_path(new)
        if op == "/dev/null":
            touched.append(("add", target / np_, hunks))
        elif np_ == "/dev/null":
            touched.append(("del", target / op, hunks))
        else:
            touched.append(("mod", target / np_, hunks))

    backups = {}
    for kind, path, hunks in touched:
        if path.is_file():
            backups[path] = path.read_bytes()
    try:
        for kind, path, hunks in touched:
            if kind == "del":
                if path.exists():
                    path.unlink()
            elif kind == "add":
                if path.exists():
                    continue
                _apply_file(path, hunks)
            else:
                _apply_file(path, hunks)
    except Exception:
        for path, data in backups.items():
            path.write_bytes(data)
        raise

    return f"applied {patch.name} ({len(parsed)} files)"


if __name__ == "__main__":
    print(apply())
