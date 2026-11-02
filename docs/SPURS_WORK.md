# SPURS producer: the one component between "engine start" and "first frame"

Measured on NPUA80068 (Super Stardust HD), reproducible with
`python ps3revamp.py probe ports/SuperStardustHD`.

## Evidence

```
[cellSpurs] CreateTaskset() ea=0x11061D00 spurs=0x10009600
[cellSpurs] _LFQueueInitialize(owner=0x10009601 q=0x11063780 buf=0x11063800 size=32 depth=16 dir=3)
[cellSpurs] CreateTask(id=0..2, entry=0x754D00, arg=11063780 11063780 00000000 00806D6C)
[spu_workload] dispatch HIT ... x3        (images are lifted and registered)
[spu-spin] img=2 pc=0x0963C GETLLAR ea=0x11063780 x4M
          line: 00000000 00000000 00000000 00000010   (pop=0 push=0 depth=16)
[cellSpurs] EventFlagWait BLOCKED ... flagEA=0x11063600 pattern=0x0001
```

* The three tasks run and block in `WAIT_SIGNAL`, then spin on the queue.
* The queue is empty: nothing ever pushes.
* `_cellSpursLFQueuePushBody` exists now but is never called by the title.
* No compatibility mode changes the picture
  (`probe --modes`: baseline / sticky-signal / interp-unlifted / ws-drain all
  end in `spurs-queue` on `0x11063780`).

## What must be implemented

The producer is the SPURS **taskset policy / job submission** path, which the
console provides in firmware, not in the title:

1. `cellSpursInitialize` must start the taskset policy context (the SPURS policy
   module runs on an SPU and is responsible for dispatching tasks and signalling
   the taskset event flag).
2. `cellSpursCreateTask` must queue the task as *ready* and let the policy
   dispatch it, instead of dispatching it directly from the PPU call.
3. The policy must:
   - pick ready tasks and enter them (satisfying the start `WAIT_SIGNAL`),
   - run job chains / workloads and push entries into the LF queue,
   - set the taskset event flag (`CSTS_SIGNALLED` / `cellSpursEventFlagSet`) when
     a workload completes.
4. `_cellSpursLFQueuePushBody` / `PopBody` must be exercised by that path, and
   `cellSpursSendWorkloadSignal` must wake the policy.

Reference implementations:
`docs --topic rpcs3-spurs --fetch` (RPCS3 `sys_spurs.cpp`, `SPUThread.cpp`).

## Definition of done

- `probe` shows queue `pop != push` at least once and no `[spu-spin]` on it.
- No `EventFlagWait BLOCKED` beyond the first second.
- The title reaches its first `flip` (frame) and the window leaves the clear
  colour.
- `port --test` verdict becomes `ran` with `observations` containing a frame
  milestone; the knowledge base records the new handler set.

## Reverse-engineering finding (lifted PPU code)

The guest does contain the producer call. In `recompiled/ppu_recomp_029.cpp` the
import stub at `0x006AD244` (`_cellSpursLFQueuePushBody`) is called from
two sites (LR `0x006AAC34` and `0x006AAD9C`). The caller builds a 0x70-byte
element on its stack and calls push with `blocking = 0`.

So the API is used by the title; the runtime never sees the call because the
producer thread (`Streamer loader thread`, host tid 11952) does not reach that
code path at run time. The stall is therefore in guest control flow / task
pipeline ordering, not a missing stub.

Next diagnostic: sample the loader thread's guest PC while the title is stalled
(`PS3_SAMPLE_MAIN` / `PS3_SAMPLE_RVAS` in the runtime, or `probe`) to see
which wait it is parked on before the push, then align the HLE semaphore/event
semantics it expects.


## Gate experiments (measured)

SEMTID tracing showed the waits: loader -> sem 7, unpack -> sem 10, worker -> sem 5.

- One-shot boost (SPURS_BOOST_SEMS=7,10,5): the loader advanced after sem 7
  (re-opened stream.dat, dispatched three more tasks), then stalled at the same
  completion flag. The gates are real, but they are symptoms.
- Continuous boost (SPURS_BOOST_ALWAYS=1): the worker spins on sem 5
  (217k log lines in 25s) and no frame appears. The semaphore is not the
  producer; forcing it only turns a block into a busy loop.

Conclusion unchanged and now experimentally confirmed: the missing component is
the SPURS taskset policy / job dispatch that would satisfy these gates and push
work into the LF queue.


## Session 2026-11-02: SPURS pipeline live, render loop reached

The title now gets through SPURS init, dispatches real tasks, decodes queue
items, and enters its render loop at ~17-18 FPS. Three concrete runtime bugs
were found and fixed:

1. `rsx_perf_enabled` is a FUNCTION (`int rsx_perf_enabled(void)`, see
   `runtime/syscalls/sys_timer.c`), but the compat shim defined it as a data
   object. The guest's `sys_timer_usleep` (lv2 syscall 141) therefore called
   the variable's address and executed non-executable `.bss`, crashing just
   after `cellGcmGetLabelAddress(0xFF)` / the first flip fence. Fixed in
   `libs/input/ps3revamp_compat.c`.
2. `_cellSpursLFQueuePush/PopBody` used `eaSignal` (the CellSpurs instance,
   low bit tagged, e.g. `0x10009601`) as a taskset EA for
   `spu_taskset_signal_task`. The bit landed in the wrong object and a
   consumer parked on the queue never woke. Fixed by waking by WAIT_SIGNAL
   object (the queue EA): `spu_taskset_wake_object()` in
   `runtime/spu/spu_workload.c`, plus an object wake-latch consumed by the
   WAIT_SIGNAL handler, so a push that beats the park is not lost.
3. `cellKbRead` was a compat stub outside the `cell*/sce*/sys*` module files,
   so `tools/gen_hle_nids.py` never registered NID 0xFF0A21B7 and the guest
   polled it in a tight loop (thousands of `[hle] unresolved NID` lines).
   Moved to `libs/input/cellKb.c`; the NID table now has one handler table
   entry and only `0xFAA275A4` (AV config, harmless) remains unresolved.

Added a fairness yield in the SPU lock-line path: a task polling an empty
SPURS queue used to burn a whole core in GETLLAR (three decoder tasks pinned
every core, loader advanced at ~1 chunk / 5 s). After a burst of polls on the
same line the thread yields / sleeps 1 ms. Measured on the same title: 49
chunks in 30 s instead of 4 in 45 s, and the CPU burn dropped from ~64 core-
seconds per 6 wall seconds to ~12 cores.

Measured state after the fixes (25 s probe): no blocked PPU thread, no queue
spin in the summary, 42 taskset create/shutdown cycles, 49 `stream.dat`
4 KiB reads, 60 draw calls, and the game window runs at 17 FPS with 28 draws
per frame.

Remaining blocker: the render loop is black. The title's per-chunk loader
creates a taskset + 3 decoder tasks + LF queue, pushes one item, waits for
the SPURS->PPU event flag, then tears the taskset down and repeats. After 49
chunks the loader threads (tid 4 on sem 7, tid 5 on sem 10) stop receiving
work and all three tasks park in WAIT_SIGNAL while the frame loop keeps
presenting. Next: trace the guest producer that posts sems 7/10 and find the
condition that ends the chunk loop, and inspect the `[FP] PSO FAIL
(0x03902001, E_INVALIDARG)` pipeline that keeps some guest draws out.
