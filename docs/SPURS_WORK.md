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

