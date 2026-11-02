# Verified status

What is proven by running the tool, and what is not. Numbers are from this
machine (16 logical cores, RTX 3080, Windows 11).

## Pipeline (title-independent)

| Stage | Status |
|---|---|
| ELF / SELF load, OPD table, imports | verified (13,180 functions / 200 imports on the test EBOOT) |
| HLE coverage + named NIDs + port plan | verified (164/200 covered, per-subsystem table) |
| PPU lift, parallel mid-function pass | verified (25,378 functions, 1.1 GB C++, 2.4 min) |
| SPU extraction + lift + registry | verified (27/27 images, dispatch HIT at runtime) |
| Project scaffold, clang-cl build, sccache | verified (845 MB exe, 21 s warm build) |
| Boot test + diagnosis + learning store | verified |
| Packaging (`<Title>.exe` + `PS3_GAME/USRDIR`) | verified (exe and layout) |
| Per-title window title | verified (`SUPER STARDUST HD [NPUA80068]`) |

## Test title NPUA80068 (Super Stardust HD)

Reached, in order: D3D12 device + window, module loads, cellSpurs init, cellGcm
init (tiles, zcull, display buffers), cellGame BootCheck, keyboard/pad/video out,
three SPU tasks created, **SPU dispatch HIT for all three**, engine start.

Stops at: the taskset's three tasks park in `WAIT_SIGNAL` and the PPU blocks in
`cellSpursEventFlagWait` on `0x11063600`; the completion signal is never
produced. The frame stays at the clear colour.

Tried and measured:

| Attempt | Result |
|---|---|
| registry via `__attribute__((constructor))` | ignored by clang-cl -> every image DROPPED |
| explicit `spu_register_all()` in main | fixed: dispatch HIT |
| MSVC `.CRT$XCU` initialiser in generator | keeps MSVC builds working |
| `_cellSpursLFQueuePushBody` implementation | correct for producers that use it; this title never calls it |
| auto-start signal after CreateTask | signal delivered, task still parks (the SPU wait consumes a different condition) |
| `LBP_WS_DRAIN=2`, `SPU_INTERP_UNLIFTED=1` | no change for this stall |

Remaining work: implement the SPURS task-start/completion protocol the title
actually uses (the SPU-side wait object, not the `+0x40` signalled bit). That is
runtime work inside `runtime/spu/spu_channels.c` + `libs/spurs/cellSpurs.c`, and
it is the difference between "reaches the engine" and "draws a frame".

## Ghidra: the offline independent decoder

Ghidra 12.1 lives at `toolchain/ghidra/ghidra_12.1_PUBLIC` (inside the project;
`setup --with-ghidra` reproduces it). `--analyzer ghidra` runs headless with
`PowerPC:BE:64:64-32addr` and exports functions/symbols/strings.

Measured on NPUA80068:

| output | count |
|---|---|
| functions | 13,380 (OPD table: 13,180 â€” 200 extra starts recovered) |
| strings | 7,262 |
| readable names for lifted code | 422 (`ghidra_names.py`, merged as `--names`) |

The merged list and the names feed the same lifter, so a title whose OPD table is
incomplete is still decoded completely. No REA provider, no external install, no
network at run time.

## Corpus (`analyze-dir`)

Run against the available dumps (Killzone 2: 34,351 functions / 343 imports /
73.5% covered; Super Stardust HD: 13,180 / 200 / 82.5%). Shared gaps:
`sys_net` 37, `cellSysutil` 15, `sysPrxForUser` 10, `cellSpurs` 5,
`cellGcmSys` 4 â€” the work list the tool produces automatically for any folder of
titles.



## Honest scope

The mechanical conversion is universal: any decrypted EBOOT with a valid OPD
table can be lifted, linked and launched. Whether the result *plays* depends on
how much of that title's firmware/GPU/SPU behaviour the runtime implements. The
tool maximises that automatically and reports the exact remaining gap; it cannot
invent missing console behaviour for every title.

The tool itself is deterministic: no model calls, no telemetry, no network unless
`--fetch-docs` is passed. Every decision is a rule or an aggregate of previous
runs in `knowledge/kb.sqlite`.

