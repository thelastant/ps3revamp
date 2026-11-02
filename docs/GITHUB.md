# Publishing PS3revamp on GitHub

The repository contains **only the tool** (driver, GUI, knowledge/documentation
modules, docs, licences). It never contains a game, keys, firmware, assets, a
generated port, or third-party binaries.

## What is excluded, and why

| Path | Contains | Why it is not committed |
|---|---|---|
| `ports/` | C++ translated from *your* game | derived from copyrighted game code — never publish |
| `knowledge/` | per-title facts + cached docs | built locally; sharing it is a deliberate act |
| `toolkit/`, `toolchain/`, `python/`, `pylibs/` | third-party binaries/sources | reproducible from `vendor.lock.json`; keeps the repo small and licence-clean |
| `work/`, `cache/`, `downloads/`, `config.json` | scratch/local state | machine-specific |

`vendor.lock.json` records the exact version, URL and SHA-256 of every fetched
component, with its licence. `NOTICE.md` repeats the licence table in prose.

## First push

```bat
cd C:\Users\PC\Desktop\PS3revamp

git init
git add .
git commit -m "PS3revamp: EBOOT.elf -> native C++ pipeline, knowledge base, docs cache"

:: create the repo (requires GitHub CLI, `gh auth login` once)
gh repo create ps3revamp --public --source=. --remote=origin --push
```

Without `gh`: create an empty repository in the browser, then

```bat
git remote add origin https://github.com/<user>/ps3revamp.git
git branch -M main
git push -u origin main
```

## Offline users

Because the binary bundle (~1.1 GB) is not in git, publish it as a **release
asset** built from a clean checkout:

```bat
python ps3revamp.py setup          :: populate toolkit/ toolchain/ python/ pylibs/
python ps3revamp.py setup --offline :: verify
:: zip the folder as ps3revamp-<version>-win-x64.zip and attach it to a release
```

The repo's `setup` command is the single entry point; `setup --offline` proves a
bundle is complete on an air-gapped machine.

## Checklist before a release

- [ ] `git status` shows no file under `ports/`, `knowledge/`, or `work/`
- [ ] `LICENSE` and `NOTICE.md` present and current
- [ ] `vendor.lock.json` hashes match `downloads/`
- [ ] `python ps3revamp.py setup --offline` passes on a clean machine
- [ ] `python ps3revamp.py doctor` is clean
- [ ] no game file, key, firmware or asset anywhere in the tree
