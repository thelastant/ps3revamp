# PS3revamp

**Decrypted PS3 `EBOOT.elf` → native C++ project.**

PS3revamp drives the whole static-recompilation chain from one command: it reads
the ELF, discovers every PPU function from the OPD table (plus IDA/Ghidra if you
have them), lifts PowerPC64 code to C++, binds the firmware imports to HLE
handlers, scaffolds a CMake project and can build the native executable.

It is a **driver**, not a new recompiler. The lifting engine is
[ps3recomp](https://github.com/sp00nznet/ps3recomp) (the toolkit behind the
Project Pyrrhus / Project Helghast ports). PS3revamp adds the parts that toolkit
leaves to you: discovery, coverage accounting, scaffolding, one-command runs and
a per-title report.

---

## Quick start (Windows)

```bat
python ps3revamp.py setup    :: downloads ps3recomp + clang + cmake + ninja
python ps3revamp.py doctor
```

Then either double-click **`PS3revamp.cmd`** (or drag an `EBOOT.elf` onto it), or:

1. Pick the decrypted `EBOOT.elf`. The game folder is detected from the path.
2. Press **Convert to PC**. The log shows loader → lift → HLE → build → boot test.
3. Press **Play** when it finishes.

Command line equivalents:

```bat
python ps3revamp.py doctor
python ps3revamp.py analyze "C:\path\to\EBOOT.elf"
python ps3revamp.py port    "C:\path\to\EBOOT.elf" --game "C:\...\NPUA80068" --build
python ps3revamp.py play    "ports\Title"
```

Result:

```
ports\<Title>\
  analysis\        EBOOT.functions.json / .imports.json / .image.json
  recompiled\      ppu_recomp.h, func_XXXX.cpp (one per chunk), ppu_hle_nids.cpp
  project\         CMake project (preset "ps3revamp"), main.cpp, stubs.cpp
  spu\             lifted SPE images (only with --spu)
  build.log
  port.json        machine-readable result
  PORT.md          coverage table + honest status + next steps
```

Run the built binary with the ELF path:

```bat
"ports\<Title>\project\build\<Title>.exe" "C:\path\to\EBOOT.elf"
```

---

## Fully offline, self-contained

The folder is the product. Everything it needs is inside it and nothing calls
the network at run time:

```
python\            CPython 3.12 embeddable (python.exe / pythonw.exe)
pylibs\            capstone, construct, pycryptodome, tqdm
toolkit\ps3recomp\ loader, lifter, HLE table generator, runtime sources
toolchain\         clang 23 (llvm-mingw), cmake, ninja, sccache
```

- `PS3revamp.cmd` / `ps3revamp.cmd` use `python\python.exe` when present, so a
  machine needs neither Python nor pip.
- `python ps3revamp.py setup --offline` verifies the bundle without touching the
  network.
- Copy the whole folder to an air-gapped PC and it works the same.
- If Visual Studio is present its `clang-cl` is preferred (better Windows ABI
  fit); otherwise the bundled llvm-mingw clang is used. Both paths are tested;
  the two MinGW gaps in the toolkit (`__try` and a linkage mismatch) are patched
  in the vendored copy, with the changes documented in `docs/PATCHES.md`.
- `setup` (without `--offline`) exists only to re-fetch components if the folder
  was stripped down; normal use never reaches it.

## Works on any EBOOT, not one title

Nothing in the pipeline is keyed to a specific game:

- `EBOOT.BIN` (SELF) is decrypted in place through the toolkit; a decrypted
  `EBOOT.elf` is used as-is.
- Function discovery prefers the title's own OPD table and falls back to a
  CFG/prologue scan when a build has none — with the module TOC applied to the
  recovered functions.
- SPU images are extracted and lifted automatically when the title has any
  (`--no-spu` to skip).
- Titles, IDs and versions come from that title's `PARAM.SFO`; names are
  sanitised for NTFS, so trademark/unicode titles are fine.
- HLE coverage, subsystem risk and missing-NID lists are computed per ELF.
- Compile chunks scale with the function count, so a 13k-function title does not
  become one giant translation unit.

---

## How a PS3 game becomes a native PC game

There is no emulator loop. A PS3 title is converted in five layers, and every
one of them is mechanical until the title does something the runtime has never
seen:

1. **PPU code → C++.** PowerPC64 is translated ahead of time, function by
   function, into C that operates on the guest's 256 MB memory arena and a
   `ppu_context` register file. Calls and returns become host calls; indirect
   calls go through a generated function table keyed by guest address. The CPU
   therefore executes *native* code — this is static recompilation, not
   interpretation.
2. **Firmware → HLE.** The game never talks to a PS3 kernel; it calls import
   stubs by NID. The runtime implements those (`sys_fs`, `sys_memory`,
   `sys_ppu_thread`, `cellSysutil`, `cellPad`, `cellAudio`, `cellGcmSys`,
   `cellSpurs`, `cellSaveData`, …) as ordinary C. Unknown NIDs fall back to
   "log once, return `CELL_OK`" — deliberately load-bearing, because titles
   often only need a call to *succeed*, not to do anything.
3. **RSX → D3D12.** `cellGcmSys` calls and the RSX command buffer are translated
   into D3D12 pipeline state, shaders, textures and render targets by the
   runtime's video backend. This is where most visual gaps live.
4. **SPU → C++ (or a shim).** SPU programs embedded in the title are lifted the
   same way as PPU code and dispatched by the `cellSpurs` HLE; jobs that are
   only side effects can instead get a PPU-side fallback.
5. **Assets → host files.** `cellFs` paths are mapped through a VFS rooted at
   your game folder, so the port reads the original data files in place.

Boot is then: map the ELF segments at their real addresses, register the lifted
function table, initialise the HLE, create a PPU thread at the entry OPD and
run. That is exactly how the Killzone 2 port (Project Pyrrhus) runs native on
Windows.

Where per-title work remains: behaviour the runtime has not implemented yet
(an NID that must do something real, an RSX quirk, a timing patch). PS3revamp's
job is to make that set as small and as *visible* as possible — which is why
`port --test` boots the build and classifies exactly where it stopped.

---

## Commands

| command | what it does |
|---|---|
| `doctor` | checks toolkit, C++ toolchain, Python deps, IDA/idalib; writes `config.json` |
| `analyze <elf>` | ELF metadata + function count + import list + HLE coverage, no lifting |
| `port <elf> [-o DIR] [--game DIR] [--build] [--spu]` | full pipeline |
| `build <portdir>` | re-configure + rebuild an existing port |
| `play <portdir>` | run the built executable with the right asset root |

The GUI (`ps3revamp_gui.pyw`) only calls `port`, so everything below applies to
both. `EBOOT.BIN` SELF files are decrypted automatically through the toolkit's
`unfself.py`; if that fails you need a decrypted `EBOOT.elf`.

Useful flags:

- `--analyzer auto|native|ida|ghidra` — `auto` uses IDA when idalib is
  reachable, otherwise the OPD/CFG scanner.
- `--jobs N` — lifter and build parallelism.
- `--opt 0..3` — optimisation for the lifted translation units (default 2).
  Runtime and scaffold always build at `/O2` (or `-O1` with MinGW clang).
- `--fast` — build lifted code unoptimised: the quickest way to iterate.
- `--test SECONDS` — boot the result and classify where it stopped.
- `--chunks N` — number of generated TUs (default: ~2 per compile job, so a
  big title compiles in parallel instead of on one core).
- `--chunk-lines N` — lower for small-RAM hosts (more, smaller C++ files).
- `--names FILE` — `ghidra_names.py` output for readable comments.
- `--lifter-arg "--flag=value"` — passthrough to `ppu_lifter.py`.
- `--spu` — extract and lift embedded SPE images.

## Toolchain choice

`doctor` picks, in order:

1. **clang-cl** from Visual Studio (MSVC ABI, newest installed LLVM). This is
   the supported Windows path: the runtime uses `__try/__except`, the template
   emits `#pragma comment(lib)`, and both need the MSVC headers + Windows SDK.
   `lld-link` links it.
2. **clang + Ninja from the bundled llvm-mingw**, when no Visual Studio exists.
   Configure-time patches in the generated project cover the MinGW gaps
   (`_WIN32_WINNT`, explicit `d3d12/dxgi/d3dcompiler` imports).

MSVC `cl.exe` cannot compile the lifted code (its own docs say clang is
required), which is why clang-cl is preferred over `cl`.

---

## Performance: where the cores go

Every stage that can run in parallel does:

| Stage | Parallelism |
|---|---|
| ELF load / OPD scan | 1 process (seconds) |
| Analyzer (IDA/Ghidra) | alongside coverage, NID resolution and SPU extraction (4 worker threads, independent subprocesses) |
| PPU lift + mid-function pass | `--jobs` worker **processes**; the mid-function pass lifts its batch in parallel (vendored patch, `PS3RECOMP_MID_JOBS`) |
| SPU lift | one process **per image**, up to `--jobs` |
| C++ compile | Ninja at `--jobs`, auto-capped for very large lifts, with sccache |
| Boot/runtime | PPU threads are host threads |

Tuning knobs:

- `--jobs N` — lift/compile/SPU concurrency (default: all logical cores).
- `--chunks N` — more, smaller TUs means better compile parallelism and less
  memory per TU (auto: ~2 per job once a title passes 1,500 functions).
- `--opt 1` (default) / `2` / `3` / `0` — optimisation of lifted code.
  `--fast` is `0`: quickest rebuild while iterating.
- sccache is used automatically, so a relift or a second title reuses the
  runtime objects.

### Measured on the test title (NPUA80068, 8.1 MB EBOOT)

| Stage | Result |
|---|---|
| load + OPD scan | 13,180 functions, 200 imports in seconds |
| coverage | 164/200 NIDs (82%) |
| lift (16 workers) | 13,987 → 25,378 functions (11,391 mid-function tails) in **2.4 min** |
| output | 1.1 GB of C++ across 49 TUs |
| build (clang-cl, `--fast`) | **162 s** cold, **44 s** with sccache warm; 845 MB executable |
| SPU | 27/27 images extracted and lifted in parallel |
| boot test | ran: PPU threads started, `stream.dat` opened and read, `cellGcm` reached, `cellSpurs` waiting on a workload |

The serial upstream mid-function pass alone took ~15+ minutes on the same
input; that phase is now parallel.

---

---

## Learning without AI

Every finished port is distilled into `knowledge/kb.sqlite` (SQLite, no model,
no telemetry):

| Learned | Used for |
|---|---|
| title identity + ELF SHA | recognising the same title on the next run |
| every import (library, NID, name, covered?) | coverage statistics across titles |
| boot verdict, milestones, unresolved calls | "this title stopped here last time" |
| unimplemented NIDs + whether the title progressed anyway | `safe_to_stub(nid)` |
| settings that produced a build (chunks/opt/jobs per size bucket) | starting point for the next title |

```bat
ps3revamp.py kb                :: totals
ps3revamp.py kb --titles       :: what has been converted
ps3revamp.py kb --nid 0xFF0A21B7   :: has any title survived this NID being stubbed?
ps3revamp.py kb --library cellGcmSys
ps3revamp.py learn <portdir>   :: (re)record a finished port
```

`port` consults the store before lifting (known blockers, proven settings) and
records the result afterwards; `--no-learn` disables writing. Nothing is shared
between machines unless you copy `knowledge/kb.sqlite`.

## Reference cache (`docs`)

PS3/PPC reference material is cached on disk and consulted offline. The toolkit's
MIT docs seed the cache on first run (PPU/ELF, NIDs, syscalls, RSX, SPU, SPURS).

```bat
ps3revamp.py docs                    :: topics + cached state
ps3revamp.py docs --topic gcm        :: read the cached copy
ps3revamp.py docs --topic gcm --fetch :: download it (allowlisted hosts only)
ps3revamp.py port ... --test 10 --fetch-docs
```

`--fetch-docs` maps the boot failure to the references that title actually
needs (unresolved calls → PPU, unimplemented NIDs → NID/lv2, cellSpurs waiting →
SPURS, graphics → GCM) and stores them under `knowledge/docs/`. Network access is
opt-in; the default path is fully offline.

## Optional tools

```bat
ps3revamp.py tools
```

Shows what is available and how it would be used: ps3recomp, clang, Ghidra
(`--analyzer ghidra`), IDA/idalib (`--analyzer ida`), RPCS3 (behaviour oracle),
sccache. A tool is only ever invoked when a run asks for it; nothing is installed
or downloaded implicitly.

## Publishing

`LICENSE` covers this tool's code (MIT). `NOTICE.md` lists every bundled
component and its licence, `vendor.lock.json` pins versions/URLs/SHA-256, and
`.gitignore` keeps generated ports, the knowledge store and third-party trees
out of git (they are user- or licence-specific, not source). See
`docs/GITHUB.md` for the exact publish steps.

---

## Requirements

- Windows x64 (the bundled toolchain path assumes this; Linux/macOS work with a
  system clang once `config.json` points at it).
- Python 3.10+ with `capstone`, `construct`, `pycryptodome`, `tqdm`.
- A **ps3recomp toolkit** checkout. Auto-detected from:
  1. `--toolkit <path>` / `PS3RECOMP_DIR`
  2. `config.json`
  3. `./toolkit/ps3recomp`
  4. a Project Pyrrhus / Helghast portable install on the Desktop
- A C++20 clang + CMake + Ninja. Portable installs ship these under
  `toolchain/`; they are picked up automatically.

`python ps3revamp.py doctor` tells you exactly what is missing.

---

## What it cannot do (read this)

A PS3 title is not a single binary. What PS3revamp produces is **faithful
translated code, not a rewrite**: every PPU function becomes native C++ that
runs on a flat 256 MB guest VM and calls HLE handlers for `cellGcmSys`,
`cellSpurs`, `cellSysutil`, `sys_fs`, etc. That is how the existing
Pyrrhus/Helghast ports work and why they are playable at all.

Automatic:

- OPD/CFG function discovery, jump tables, VMX/Altivec lowering
- import-stub → `ps3_hle_call(nid)` binding
- NID → handler table generation
- SPU image extraction and lifting
- project, toolchain, build

Not automatic — **per title**:

1. **Imports without an HLE handler.** `PORT.md` lists them; each needs a
   handler in `project/stubs.cpp` (or a real lifted firmware module — see
   `docs/FIRMWARE_LLE.md` in the toolkit).
2. **RSX behaviour** the renderer does not translate yet.
3. **Asset/VFS paths**, save data, PSN services.
4. **Game-specific patches** — timing, memory layout, known engine bugs.

A title with high HLE coverage and mostly PPU code (small PSN games, puzzle/
arcade titles) can boot quickly. A title with heavy SPU job systems and custom
RSX use needs the per-game work described in the toolkit's
`docs/GAME_PORTING_GUIDE.md`.

**No tool can turn an arbitrary PS3 game into a working PC game with zero
per-title work.** PS3revamp removes the mechanical 80% and tells you precisely
what is left.

---

## IDA Pro / REA

The original ask was to drive IDA through REA. Three blockers, each verified
against this machine:

- IDA Pro is not installed here.
- REA's `open_binary` rejects the file: *unsupported ELF architecture* — its
  ELF gate does not accept 64-bit big-endian PowerPC.
- REA's `inspect_binary_layout` is limited to a Linux x64 artifact profile.

So the REA bridge cannot analyse PS3 binaries. PS3revamp therefore talks to
**idalib directly** (`tools/ida_analyze.py`), the supported headless path:
install IDA Pro 9.x, activate idalib in its Python, then set `ida_python` in
`config.json`. `--analyzer ida` merges IDA's function map with the OPD scan
(union by address, larger extent wins). Without it, the OPD scanner is used,
which for PS3 is already the authoritative source: the game's own function
descriptors.

---

## Layout

```
ps3revamp.py          CLI + pipeline + report
config.json           generated by `doctor` (toolkit / toolchain / ida_python)
toolkit/ps3recomp/    optional local toolkit copy
ports/                generated ports
work/                 scratch (analyze cache, HLE probe)
```
