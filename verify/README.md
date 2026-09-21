# Formal verification

`prove.py` checks a list of obligations against the **shipped** `timekeep`
binary, the flat ELF built from `timekeep.s`, and prints `PROVED` or `FAILED`
for each one. It exits non-zero if any obligation fails. Run it with
`make verify`, which applies a memory cap (see the top-level README).

## Trusted base

The results depend on these being correct:

- `spec.py`: the functional spec. Keep it small and obvious. Every
  equivalence proof compares the binary against it.
- Z3, angr, and angr's x86_64 lifter (VEX).
- GNU `as`/`objdump`: symbol addresses come from re-assembling `timekeep.s`
  and reading its symbol table. The addresses are then used on the flat binary
  loaded at `0x400000`.
- The hand-written models used in place of routines that are proved
  separately (for example, `sum_emit` is modelled while `feedb` is checked).

## Obligations

### Z3 theorems (hold for every input)

| Obligation | Claim |
|---|---|
| duration | For `start, end` in `[0,1440)`, the result is in `[0,1440)` and equals `(end-start) mod 1440` |
| minute_of_day | The result is in `[0,1440)` for every 64-bit second count |
| tz_offset bounds | If the TZif header checks pass, the gmtoff read stays inside the buffer |

### angr equivalence over the shipped binary

Each leaf routine runs with symbolic inputs and every path is explored. The
output is compared to the spec, and every write must land in the expected
output region.

- `emit2`, `emitdec`, `emithm`, `emitdur`: number formatting
- `parse_hm`: follows the `HH:MM` grammar and reads stay within the buffer
- `sum_emit`: adds the wrapped (`mod 1440`) entry duration to the total
- `feedb`: one step of the sum-mode parser matches the spec step for every
  parser state and input byte. This is the base case of the argument that the
  whole parser matches the spec.

### angr whole-program checks

- **Terminal restore:** with raw mode on, the quit path calls
  `ioctl(TCSETS, saved)` before `exit`. With raw mode off, it calls no ioctl.
- **Sum-mode safety (bounded, weak):** runs the program on one symbolic
  8-byte printable argument. It checks that no write lands in the code image
  and that the only syscalls are `write` and `exit`.

## Known limitation: the sum-mode safety check

The parser forks on every argument byte, so the number of paths grows
exponentially. Exploring all of them used 25–30 GB of RAM and got OOM-killed.
The check now drops finished paths as it goes and keeps at most
`SUM_ACTIVE_CAP` (256) live paths. Paths over that cap are discarded and
counted in the result's detail field, which `record()` prints only when an
obligation fails.

A current run explores 256 paths and discards 848. None of the explored paths
makes a `write` syscall; they all exit early. The check therefore passes
without looking at a run that produces output, so treat this `PROVED` as a
smoke test, not a proof. Possible fixes: fail or flag the obligation when any
path was discarded, and constrain the argument to valid entries (or check the
argument loop separately) so the search can finish.
