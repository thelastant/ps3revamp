# RPCS3 SPURS reference (gathered from the decomp/emulator source)

Full sources are cached in the project (offline afterwards):

```
knowledge/docs/rpcs3-cellSpurs.cpp      (156 KB)
knowledge/docs/rpcs3-cellSpursJq.cpp    (10 KB)
```

## Console-side scheduler semantics to reproduce

From `cellSpursCreateTask`, `_cellSpursSendSignal` and the `_spurs` namespace in
that file:

1. `cellSpursCreateTask` = `_spurs::create_task` + `_spurs::task_start`.
2. `task_start` marks the task **ready**, sets its signalled bit, then asks the
   SPURS policy to dispatch it (`cellSpursSendWorkloadSignal` + `cellSpursWakeUp`).
   The task itself is *not* entered from the PPU call.
3. The policy runs on an SPU: it takes ready tasks, clears `READY`, sets
   `RUNNING`, and enters the task entry. The task's first `WAIT_SIGNAL` returns
   because its signalled bit is already latched.
4. `_cellSpursSendSignal` validates the taskset state against the enabled /
   running / ready / waiting bitmaps (a waiting+running, ready+pending, disabled
   bit, or unknown task is an error) and only when the task is genuinely waiting
   does it signal it and wake the policy.
5. Task exit clears running/enabled, publishes the exit code and sets the
   taskset event flag the PPU waits on (`cellSpursEventFlagWait`).

## Where this maps in the port

| Reference | Port code |
|---|---|
| `cellSpursCreateTask` | `libs/spurs/cellSpurs.c` |
| policy dispatch | `runtime/spu/spurs_pm.c`, `spurs_policy.c` |
| signalled/ready/running bitmaps | `libs/spurs/spurs_taskset.h` (`CSTS_SIGNALLED` …) |
| event flag completion | `libs/spurs/cellSpurs.c` (`cellSpursEventFlagSet/Wait`) |
| LF queue push/pop | `libs/spurs/cellSpurs.c` (`_cellSpursLFQueuePushBody`) |

The implementation must be this state machine. Boosting semaphores is not a
substitute: measured, it advances the loader once and otherwise only turns the
block into a busy loop (`docs/SPURS_WORK.md`).

## Measured behaviour of the test title

- 3 tasks created, dispatched to the lifted image, then park in `WAIT_SIGNAL`.
- Workers spin on LF queue `0x11063780` (empty, depth 16).
- PPU waits on taskset flag `0x11063600`.
- Gates: loader `sem 7`, unpack `sem 10`, worker `sem 5` — all consequences of the
  missing policy, not the cause.
