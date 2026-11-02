# Optional: the RPCS3 oracle

RPCS3 is **not required** and is never used automatically. It is an on-request
reference: when a title behaves differently in the port, the same title is run in
RPCS3 and its log is kept beside the port for comparison.

```bat
ps3revamp.py oracle <portdir> --seconds 30
```

Output (`oracle.log`, `oracle.json`):

| signal | meaning |
|---|---|
| `spurs_init` | SPURS initialisation lines |
| `spu_task` | SPU task/thread activity |
| `gcm` | RSX/GCM activity |
| `flip` | frame presents |
| `fs` | file/VFS activity |

The table prints the emulator count next to the port's, for the same window. That
tells you which subsystem is alive in the reference and dead in the port before
reading a single line.

Notes:

- RPCS3 is located via `PS3REVAMP_RPCS3`, `config.json`, or the usual install
  paths; it is only launched by this command.
- RPCS3's log lives at `log/RPCS3.log` next to its executable; the command tails
  the part written during the run and also keeps stdout.
- Nothing from RPCS3 is needed at run time by the port; the oracle is evidence,
  not a dependency.

## Related analysis

| command | what it answers |
|---|---|
| `probe <port>` | what is blocked right now (thread, wait object, SPU spin site, queue) |
| `linkage <port>` | which firmware calls exist in the code but are never reached |
| `oracle <port>` | what the reference implementation does in the same window |
| `route <elf>` | which conversion route this title needs |
| `gaps` | which unimplemented imports unblock the most titles |
