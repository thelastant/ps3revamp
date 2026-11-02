# The pipeline route: any PS3 title → a Windows game

This is the decision procedure the tool follows, and the reasoning behind each
choice. It is deterministic: measured facts select the route, and every run
records what happened so the next attempt starts further along.

```
EBOOT.BIN ─(SELF)→ EBOOT.elf ─┬─ OPD scan ──────────────┐
                              ├─ Ghidra decode ─────────┤→ function map + names
                              └─ IDA (idalib, optional) ┘
                                        │
                     import/NID coverage + subsystem plan
                                        │
        ┌───────────────────────────────┼───────────────────────────────┐
        │ route A: HLE (default)        │ route B: LLE for a library     │
        │ every import → ps3_hle_call   │ lift the firmware PRX itself   │
        └───────────────────────────────┴───────────────────────────────┘
                                        │
                          PPU lift (parallel, mid-function recovery)
                                        │
                    ┌───────────────────┴───────────────────┐
                    │ SPU: lift + registry (default)        │ interpreter fallback
                    └───────────────────┬───────────────────┘
                                        │
                          scaffold + build (clang-cl + sccache)
                                        │
                    ┌───────────────────┴───────────────────┐
                    │ boot test → classify the stopping link │
                    └───────────────────┬───────────────────┘
                                        │
             fix exactly that link (stub / lift / patch / renderer) → rebuild
                                        │
                                 package → C:\Games\<Title>
```

## Route selection (what the tool decides and why)

| Observation | Route chosen |
|---|---|
| coverage ≥ 95%, PPU-only | direct HLE; expect a boot on the first build |
| coverage 80–95% | HLE + implement the named missing NIDs (usually I/O, PSN, pad) |
| coverage < 80% or a whole library missing | add LLE: lift the firmware module with `tools/lift_prx.py` and bind its exports |
| SPU images present | lift every image, emit `spu_workloads.c`, register before dispatch |
| SPU load from data files at run time | scan payloads (`ps3revamp_spu.py`), lift the found image, register |
| Boot stops at `spu-miss` | registration/registry problem (fixed by explicit `spu_register_all`) |
| Boot stops at `spu-stall` | SPURS task/policy semantics: implement the wait/signal path against RPCS3's reference |
| Boot stops at `unresolved indirect call` | missing lift: re-decode with Ghidra, merge starts, relift |
| Boot stops at an unimplemented NID | implement that handler; the log names it |
| Graphics mismatch / clear colour after logic runs | RSX pipeline: compare with RPCS3's command handling |
| No second decoder installed | `setup --with-ghidra` (bundled, offline) |

The `route` command prints this plan for a specific EBOOT and writes
`knowledge/routes/<sha>.json`.

## What "fully compatible" requires, subsystem by subsystem

1. **CPU** — every reachable PPU function lifted; verified by the count vs the
   OPD table and by Ghidra's independent list (test title: 13,180 OPD → 13,380
   Ghidra → 25,378 after mid-function recovery).
2. **Firmware** — every imported NID answered either by HLE or by a lifted
   module. Coverage is reported per library; the remainder is the work list.
3. **SPU** — every embedded image lifted and registered by content fingerprint;
   jobs dispatched by `cellSpurs`. Unlifted, runtime-loaded code needs the
   interpreter path.
4. **GPU (RSX)** — the GCM command stream translated to D3D12: state, shaders,
   textures, surfaces, flips. This is where visual parity is won or lost.
5. **Timing/threads** — PPU threads are host threads; vblank and flip pacing are
   driven by the frame clock.
6. **Title behaviour** — patches for engine-specific quirks (the same layer the
   existing ports carry in `port/patches`).

## Validation: the differential oracle

For each title, the portable build and RPCS3 are both run on the same inputs.
The comparison is deterministic and does not need any model:

- milestones: module loads, GCM init, first flip, first frame with N draws
- NID call sequence at boot (`PS3_HLE_TRACE`)
- SPU dispatch hits/misses and task states
- memory checkpoints at known EAs (`rpcs3_probe` scripts)

Disagreement at a milestone becomes the next work item; agreement moves the
frontier forward.

## Definition of done for a title

1. `port --build --test N` reports `ran` with no `spu-miss`, no `spu-stall`,
   no `unresolved indirect call`, and the title reaches its own render path.
2. The window carries the title name; input, audio, save data and assets resolve
   through the VFS.
3. `package` produces `<Title>.exe` + `PS3_GAME/USRDIR`; the game launches with
   no arguments and no dependency on the original dump.
4. The run is recorded in the knowledge base so the next title of the same
   engine starts from the known-good settings and handlers.

## Why this route and not another

- **Emulation** would run the console, not deliver a game; rejected.
- **Source rewrite** needs the source; rejected.
- **Pure static recompilation without HLE** would have to lift all of firmware;
  possible (LLE) but far slower per title. HLE is the default and LLE is the
  fallback for libraries that resist HLE.
- **Pure HLE without static recompilation** is an emulator loop; rejected.

The chosen route is the only one that yields a native executable per title while
keeping per-title work proportional to that title's missing behaviour.
