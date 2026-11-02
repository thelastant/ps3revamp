# RSX backend bring-up log (measured)

This file records what the D3D12 backend was missing for the test title
(Super Stardust HD, NPUA80068), the evidence used, and the sources each
finding comes from. Every claim below was observed in `ports/SuperStardustHD`
logs with the env knob named next to it.

## 1. Fragment programs sampling texture units 4..15 failed PSO creation

Evidence (D3D12 debug layer, `D3D12_DBG=1`):

```
[FP] PSO FAIL (fp=0x03902001, 0x80070057)
[FP][DBG] ID3D12Device::CreateGraphicsPipelineState: Root Signature doesn't
  match Pixel Shader: Shader sampler descriptor range
  (BaseShaderRegister=4, NumDescriptors=1, RegisterSpace=0) is not fully bound
  in root signature
```

The FP decompiler (`libs/video/rsx_fp_decompiler.c`, `rsx_samp[%u]`,
`rsx_tex[%u]`) supports 16 texture units, and the guest exposes 16
(`NV4097_SET_TEXTURE_OFFSET` on unit 0..15). The backend only declared

- 4 SRV descriptors (t0-t3) and 4 sampler tables (s0-s3) in `vp_root_sig`
- `tex[4]`, `tex_rt[4]`, `tex_ds[4]`, and `cur_texs[4]`

`cur_texs[unit]` was also written with `unit` up to 15 (see
`d3d12_set_texture`), i.e. an out-of-bounds write into the following fields of
`s_d3d` on every texture-unit bind >= 4. That is a correctness bug independent
of the PSO failure.

Fix (in `vendor_runtime.py` / `patches/0100-runtime-complete.patch`):

- SRV range 16 descriptors, 16 sampler tables (`rp[19]`, `NumParameters = 19`)
- per-draw SRV window `DRAW_SRV_BASE + d*16`, `SRV_HEAP_SIZE` scaled
- all texture-unit arrays widened to 16, every unit loop to 16
- pipeline-failure path now drains `ID3D12InfoQueue` (the same diagnostic the
  VS failure path already had), so an opaque `E_INVALIDARG` always names the
  field that is wrong

Result: `[FP] PSO FAIL` count 0; the title went from ~17 FPS to ~24 FPS and
started presenting its post-processing chain.

Sources:

- Direct evidence: D3D12 debug layer message above.
- Guest ISA: RSX texture-unit register block (`NV4097_SET_TEXTURE_*`), as
  decoded in `libs/video/rsx_dispatch.h` and used by the decompiler.
- Cross-check of the SPURS/RSX ABI: RPCS3 `rpcs3/Emu/Cell/Modules/cellSpurs.cpp`
  and the RSX method tables (`rpcs3/Emu/RSX/RSXThread.cpp`), fetched from
  github.com/RPCS3/rpcs3 (see `knowledge/docs/rpcs3-cellSpurs.cpp`).

## 2. What the frame actually contains (DRAWARGS=40)

The title's frame is a full-screen post-processing chain, all 6-vertex quads:

```
rt=0x05530000 960x540  tex0=0x02800000        (first pass)
rt=0x05950000 480x270  tex0=0x05530000  <- 960x540
rt=0x05A50000 240x135  tex0=0x05950000
...                                         (blur pyramid, ping-pong)
rt=-1 (backbuffer) 1280x720 tex0=0x05530000  (final composite)
```

`texrt=-1` on the first pass means `off_rt_find(0x02800000)` did not find an
offscreen target for the scene source; the backend then sampled guest memory
instead, which is empty, so every downstream stage is black. The scene target
0x02800000 is created by the *legacy* (non-VP) draw path, which the RT
pre-pass does not register. Next step: register scene targets from the legacy
path too, or make the composite sample the correct RT.

Sources: `DRAWARGS=40` log; `libs/video/rsx_d3d12_backend.c`
(`vp_get_fp_pso`, `off_rt_find`, `render_frame`).

## 3. Cost breakdown (`PERF=1`)

```
[PERF] 22.9 fps | render_frame 0.12s (13%) | prepass 0.11s (13%)
      [srv 0% | pso 0.11s 13% 940 calls 4 MISS] | guest 0.75s (87%)
```

The recompiled PPU code dominates. The CMake cache had
`PS3REVAMP_LIFT_FLAGS=-O0` (a previous `--fast` build persisted it); the tool
now defaults `build --opt` to 2 and the port stage already defaults to 2.
Rebuilding the lifted TUs at `-O2` is the first speed lever; `--fast` remains
an iteration-only switch.

Sources: `PERF=1` log; `ports/*/project/build/CMakeCache.txt`
(`PS3REVAMP_LIFT_FLAGS`), `ps3revamp.py::build_project`.
