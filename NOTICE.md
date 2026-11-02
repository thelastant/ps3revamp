# Third-party components

PS3revamp itself (the driver, GUI, docs) is provided as-is. It bundles the
following unmodified or lightly patched components; each remains under its own
licence.

| Component | Version | Licence | Location |
|---|---|---|---|
| ps3recomp (PPU/SPU lifter, HLE runtime) | v0.18.0 | MIT | `toolkit/ps3recomp` |
| LLVM / clang / lld (llvm-mingw) | 23.1.4 | Apache-2.0 with LLVM exceptions | `toolchain/llvm-mingw` |
| CMake | 4.4.5 | BSD-3-Clause | `toolchain/cmake` |
| Ninja | 1.12.1 | Apache-2.0 | `toolchain/ninja` |
| sccache | 0.19.0 | Apache-2.0 | `toolchain/sccache` |
| CPython (embeddable) | 3.12.10 | PSF-2.0 | `python` |
| Capstone | 5.x | BSD-3-Clause | `pylibs` |
| construct | 2.10 | MIT | `pylibs` |
| PyCryptodome | 3.x | BSD-2-Clause / public domain | `pylibs` |
| tqdm | 4.x | MPL-2.0 / MIT | `pylibs` |
| Visual Studio Build Tools (used if installed) | 2022/18 | Microsoft EULA | system |

Two compatibility patches are applied to the vendored ps3recomp so it builds
with the bundled MinGW clang; see `docs/PATCHES.md`. They are no-ops under
clang-cl/MSVC.

PS3revamp ships no game code, keys, firmware or assets. A port is built on the
user's own machine from their own copy of a title.
