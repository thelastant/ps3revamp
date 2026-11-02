# PS3 → native: the knowledge this tool is built on

Everything PS3revamp does is derived from the facts below. Sources are listed at
the end; where a claim is about a specific implementation, the implementation is
named.

---

## 1. The machine

| Part | What it is | Why it matters for a port |
|---|---|---|
| **Cell BE** | 1 PPE (2-way SMT PowerPC core) + 8 SPEs | PPU code is ordinary PowerPC64; SPU code is a separate VLIW ISA |
| **PPU** | 3.2 GHz, 64-bit *big-endian*, VMX (AltiVec) | all game logic, audio mixing control, graphics command submission |
| **SPU** | 3.2 GHz, 128-bit SIMD, **256 KB local store**, no cache | jobs: decompression, physics, audio, particles; DMA in/out via MFC |
| **RSX** | NVIDIA G70-class, 256 MB GDDR3, no unified memory | draws what the PPU builds in GCM command buffers |
| **RAM** | 256 MB XDR + 256 MB VRAM | a port must model a flat 32-bit guest address space |
| **Firmware** | CellOS / lv2 hypervisor + system libraries | the game calls it through NID imports, never syscalls directly |

Retail hardware: 7 SPEs usable (1 disabled for yield, 1 reserved by the
hypervisor). A port may use all host cores.

## 2. The software stack

```
game EBOOT.BIN            SELF container (encrypted, signed)
   └─ EBOOT.elf            ELF64 BE PowerPC64, ET_EXEC, fixed load address
        ├─ PT_LOAD 1..n    .text / .rodata / .data / .bss
        ├─ .opd            function descriptors
        ├─ .rodata.sceNid  NID import tables
        ├─ .lib.stub       library import stubs
        ├─ .sceModuleInfo  module exports/imports
        └─ entry OPD       → (code address, TOC)
```

### ELF / ABI facts that break naive tooling

- **PowerPC64 ELFv1**: every function pointer is an **OPD** (official procedure
  descriptor): 8 bytes of `{u32 code, u32 toc}`. The loader resolves them into a
  function table; the recompiler emits one C++ function per descriptor.
- **TOC in r2**: position-independent data access is `r2 + offset`. Each
  function's OPD carries the TOC it expects; the lifter sets r2 on entry.
- **32-bit ABI pointers**: addresses are 32-bit even though registers are 64-bit.
  The port maps a flat 4 GB arena so *every* 32-bit guest value is valid (garbage
  pointers read zero instead of crashing).
- **Big-endian**: all guest memory is BE; helpers do the byte swaps. Host is LE.
- **ET_EXEC, no relocations**: a retail EBOOT is loaded at its link address.
  PRX/SPRX (ET_DYN) do need relocation — that's the LLE path.

### NIDs

Imports/exports are named by a 32-bit **NID**:
`sha1(name + suffix)[0:4]`, byte-swapped for the little-endian field. The
standard 16-byte suffix is
`67 59 65 99 04 25 04 90 56 64 27 49 94 89 74 1A`.
The recompiler rewrites each import trampoline into `ps3_hle_call(nid)`; the
runtime maps NID → handler. Unknown NIDs are deliberately **not fatal**: the
default handler logs once and returns `CELL_OK` (many calls only need to
succeed).

## 3. What "porting" actually means here

Three possible approaches, and why static recompilation was chosen:

| Approach | How | Cost |
|---|---|---|
| Emulation (RPCS3) | interpret/JIT guest code at runtime | accurate, heavy, not a port |
| Static recompilation (ps3recomp, this tool) | translate PPU/SPU ahead of time to C++, link against an HLE runtime | native speed, no emulator loop, per-title fixes |
| Source port | rewrite the game | infeasible without source |

The result is a **native x64 executable** whose functions *are* the game's
functions, translated. The runtime supplies what the console supplied:
memory, threads, files, audio, pads, network stubs and the GPU translation.

### The five layers

1. **PPU → C++**: disassemble, build CFGs and jump tables, lift to C with a
   guest register file and a flat memory arena.
2. **Firmware → HLE**: `sys_*` syscalls and `cell*` libraries implemented as C.
3. **RSX → D3D12**: translate the GCM command stream (state, shaders, textures,
   render targets) into host GPU work.
4. **SPU → C++**: lift each embedded SPU image; `cellSpurs` dispatches it, or a
   PPU-side fallback implements the job.
5. **Assets → host files**: `cellFs` paths resolved through a VFS rooted at the
   game folder.

## 4. RSX / GCM in one paragraph

GCM is a thin API over a **command buffer in main memory**. The PPU fills it and
kicks the RSX; the RSX "pusher" reads commands, and its FIFO/state machine
applies them. Shaders are Cg compiled offline to `sce_vp_rsx` / `sce_fp_rsx`
microcode, so a port must translate both the command stream *and* the shader
microcode (or interpret it). Textures are swizzled and often compressed (DXT,
GTF); surfaces are pitch- or swizzle-addressed with specific color formats.
That translation is where most visual differences come from.

## 5. SPU / SPURS in one paragraph

An SPU can only see its 256 KB local store; data moves by **MFC DMA** to/from
main memory with tags and mailboxes for synchronisation. `cellSpurs` is Sony's
job scheduler: tasksets, job modules, workload queues and a policy module decide
which SPU runs which job. A port either lifts the SPU programs and implements
SPURS' dispatch HLE, or provides PPU-side replacements for the jobs that matter.

## 6. Consequence for this tool

Mechanical (automatic):

- OPD discovery, CFG/jump-table recovery, PPU+SPU lifting
- NID import binding, HLE table generation
- flat guest memory, thread model, file VFS
- project, compiler, linker, packaging

Per-title (human, but *located* automatically):

- an NID whose side effects matter (table in `PORT.md`)
- RSX/shader behaviour the backend does not cover
- asset paths, save data, PSN
- timing/memory patches

That is why `port --test` boots the build and reports the exact stopping point
instead of just saying "built".

## 7. Worked example of why HLE leniency matters

A title that calls `cellSaveDataAutoLoad` at startup only needs `CELL_OK` to
continue into its menu; a title that calls `cellGcmSetFlip` needs a real
implementation or nothing appears. The same "unimplemented" line means
"harmless" in one case and "blank screen" in the other. The coverage table plus
the boot log separates the two.

## Sources

- Copetti, *PlayStation 3 Architecture* — https://www.copetti.org/writings/consoles/playstation-3/
- IBM, *Cell Broadband Engine Programming Tutorial v2.0* — https://arcm.csc.ncsu.edu/~mueller/cluster/ps3/CBE_Tutorial_v2.0_15December2006.pdf
- IBM, *SPU Instruction Set Architecture v1.2* — https://arcb.csc.ncsu.edu/~mueller/cluster/ps3/SDK3.0/docs/arch/SPU_ISA_v1.2_27Jan2007_pub.pdf
- PS3 Developer wiki, *CELL BE* and *NID* — https://www.psdevwiki.com/ps3/CELL_BE , https://www.psdevwiki.com/ps3/NID
- Newcastle University, *Introduction to GCM* / *Texturing with GCM* — https://research.ncl.ac.uk/game/mastersdegree/workshops/ps3introductiontogcm/
- ps3recomp docs: `PPU_RECOMP.md`, `SPU_LIFTER.md`, `GAME_PORTING_GUIDE.md`, `FIRMWARE_LLE.md` — https://github.com/sp00nznet/ps3recomp
- RPCS3 RSX sources (GCM enums, formats) — https://github.com/RPCS3/rpcs3
- rustc target docs, `powerpc64-sony-ps3` (ELFv1, big-endian, lld caveat) — https://doc.rust-lang.org/beta/rustc/platform-support/powerpc64-sony-ps3.html
