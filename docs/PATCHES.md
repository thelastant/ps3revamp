# Vendored patches

`toolkit/ps3recomp` is a pinned copy of ps3recomp v0.18.0 (MIT). The changes
below are what makes it build and behave correctly with this toolchain and with
runtime-loaded SPU work. They live as patch files so the repository stays free of
third-party source:

```
patches/0001-ppu-loader-mingw.patch    MinGW build fixes (SEH probe, linkage)
patches/0002-parallel-mid.patch        parallel mid-function pass
patches/0003-spu-registration.patch    MSVC-safe SPU registry initialiser
patches/0004-lfqueue-push.patch        _cellSpursLFQueuePushBody implementation
```

Apply on a fresh toolkit checkout:

```bat
cd toolkit\ps3recomp
git init -q && git add -A && git -c user.name=x -c user.email=x@x commit -qm base
for %f in (..\..\patches\*.patch) do git apply -p1 "%f"
```

(The copy shipped in this folder already has them applied.)

## `runtime/ppu/ppu_loader.cpp`

### 1. SEH in `samp_stack_guest`

Upstream:

```c
__try { v = sp[i]; } __except (1) { return 0; }
```

MinGW clang has no `__try`. Replaced with a `VirtualQuery` page probe under
`#ifdef __MINGW32__`; the MSVC path is unchanged.

### 2. `ppu_resv_break` linkage

Upstream forward-declares it `extern "C"` but defines it `static inline`, which
strict clang rejects ("static declaration follows non-static declaration").
The forward declaration is now `static inline`, matching the definition.

## 3. Parallel mid-function pass (`PS3RECOMP_MID_JOBS`)

`generate_mid_function_entries` lifts every branch target that lands inside an
existing function, one at a time, for up to 25 passes. On a large title that is
thousands of independent tail lifts and the slowest single-threaded phase left
(in the test title: 11,391 wrappers over 7 passes).

The vendored copy lifts each pass's batch in worker processes
(`_parallel_mid_entries` / `_mid_task`): every worker re-disassembles its own
4-byte-aligned range and uses a fresh `Lifter` with the parent's configuration,
so no mutable state is shared. Results are merged in input order, and any pool
failure falls back to the original serial loop. Controlled by
`PS3RECOMP_MID_JOBS` (1 = upstream behaviour); the driver sets it to `--jobs`.

## 4. Explicit SPU registration in the generated `main.cpp`

The workload registry emitted by `build_spu_workloads.py --constructor` only had
an `__attribute__((constructor))`. clang-cl/MSVC do not run it, and an
unregistered image is silently dropped at dispatch:

```
[spu_workload] ERROR: ... no lifted image ... the program is DROPPED
[cellSpurs] EventFlagWait BLOCKED ... waiting for a workload
```

Two fixes, both general: the generator now emits a `.CRT$XCU` initialiser under
`_MSC_VER` (kept for MSVC), and `ps3revamp` patches the generated `main.cpp` to
call `spu_register_all()` right after `ppu_recomp_register()`. The boot test now
reports `dispatch HIT` and `spu_misses` is a first-class failure verdict.

## Re-applying

Re-downloading the toolkit (delete `toolkit/ps3recomp`, run `setup`) discards
these patches. `ps3revamp.py setup` re-applies nothing — either keep the vendored
copy or re-apply the two hunks above. `doctor` is the quickest way to notice:
a build failure with `__try` or `static declaration` means the copy was replaced.
