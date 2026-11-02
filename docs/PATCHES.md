# Vendored patches

`toolkit/ps3recomp` is a pinned copy of ps3recomp v0.18.0 (MIT). Two small
changes make the MSVC-only corners build with the bundled llvm-mingw clang, so
the tool works on a machine without Visual Studio. Both are no-ops under
clang-cl/MSVC.

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

## Re-applying

Re-downloading the toolkit (delete `toolkit/ps3recomp`, run `setup`) discards
these patches. `ps3revamp.py setup` re-applies nothing — either keep the vendored
copy or re-apply the two hunks above. `doctor` is the quickest way to notice:
a build failure with `__try` or `static declaration` means the copy was replaced.
