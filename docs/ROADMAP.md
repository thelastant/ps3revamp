# Roadmap: what it takes to convert *any* title

The tool already does everything mechanical for any EBOOT: decrypt, decode (OPD +
Ghidra), plan coverage, lift PPU, lift/register SPU, build, boot-test, diagnose,
learn, package. What remains between "any title converts" and "any title plays"
is the runtime frontier below. Ordered by leverage: the first two items unblock
the most titles per unit of work, and the corpus/backlog commands measure the
effect after every change.

`ps3revamp.py analyze-dir <folder>` builds the corpus.
`ps3revamp.py gaps --kb` prints this backlog from measured imports.

## P0 — the blockers that stop a boot

**1. SPURS task/policy semantics** (`cellSpurs`, 23 NIDs in the corpus)
The current test title stops here: tasks park in `WAIT_SIGNAL`, the PPU blocks in
`cellSpursEventFlagWait`. Needed:
- task lifecycle: create → ready → policy dispatch → run → exit → join → signal
- the LF-queue data path (`_cellSpursLFQueuePushBody/PopBody/GetSize`) end-to-end
- event flags, lv2 event queues, workload signals, job chains
- the taskset policy module actually running (it is SPU code, not HLE)
Reference: RPCS3 `sys_spurs.cpp` + `SPUThread.cpp` (fetched by
`docs --topic rpcs3-spurs --fetch`).
Done when: no `spu-stall`, job chains complete, the title reaches its render loop.

**2. Firmware HLE completion** (corpus backlog: sys_net 39, cellSysutil 33,
sceNp 30, cellGcmSys 29, sysPrxForUser 25, cellResc 16, sys_io 13, sys_fs 11)
Each import needs the correct ABI and behaviour, not a stub returning 0:
- `cellSysutil`: callbacks, dialogs, save data, video-out config, system params
- `cellGcmSys`: init/flip surfaces, labels, tile/zcull, shader upload
- `sys_io`/`cellKb`/`cellPad`: real input state (cellKbRead currently blocks a loop)
- `sys_net`/`sceNp`: offline-safe success semantics + optional real sockets
Done when: corpus coverage is 100% with verified behaviour, not just presence.

**3. SPU execution completeness**
- lift every embedded image (done) and every image loaded from data at runtime
  (`ps3revamp_spu.py` scans payloads; wire the hit into the registry automatically)
- interpreter fallback for unlifted/runtime-generated code
- job-module overlays, DMA/coherency, SPU printf
Done when: zero `dispatch MISS` and zero parked tasks with no work.

## P1 — the gap between "runs" and "draws the game"

**4. RSX parity**
- GCM command coverage: state, shaders, textures, surfaces, flips, sync
- shader microcode translation (`sce_vp_rsx`/`sce_fp_rsx`) correctness
- pixel formats, swizzle, zcull, multisample, gamma
Method: differential frames against RPCS3 on identical inputs; every mismatch
becomes a regression fixture.
Done when: first playable frame and frame-to-frame parity on the test corpus.

**5. CPU/ABI edge cases**
Unresolved indirect calls, jump tables in data, VMX lanes, float rounding,
TLS/stack edge cases. Detected by `classify_boot` (`missing-lift`), fixed by
Ghidra re-decode + insn patches.

**6. Audio / video / saves / input**
cellAudio (WASAPI), ATRAC3+ (present), cellVdec, pad/keyboard, save-data paths.

## P2 — scale and finish

**7. Per-title patch layer**
Engine quirks, timing, memory layout. Recorded in the knowledge base so the next
title on the same engine inherits them (the corpus already groups titles by
shared imports).

**8. Differential validation harness**
Automated: boot both the port and RPCS3 with the same inputs, compare milestones,
NID traces, SPU states, frame hashes, memory checkpoints. Gate every change.

**9. Packaging and distribution**
`package` produces `<Title>.exe` + `PS3_GAME/USRDIR`; add saves, settings,
updates; keep the repo licence-clean (no game data).

## How much of "any title" is reachable

- **Mechanical conversion: 100%** of decrypted EBOOTs (measured: OPD + Ghidra
  decode, 25k-function lifts, SPU lift, build, package).
- **Boot to engine: high**, limited by P0 items.
- **Fully playable: proportional to P1/P2 coverage** for the engine family. Titles
  built on already-covered engines become cheap; bespoke renderers stay expensive.

"Any title" is therefore a coverage frontier that the corpus measures and the
backlog orders — not a single switch. The tool is built to move that frontier
deterministically, title by title, and to never regress one while doing it.
