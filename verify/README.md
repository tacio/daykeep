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
  separately (for example, `sum_emit` is modelled while `feedb` is checked,
  and `feedb` is modelled while the sum-mode loop is checked).
- The key-loop facts the track-mode checks start from: `rsp == rbx` (the frame
  base), `F_OUTFD == 1` and `F_N <= 250`. The key loop itself is not proved.
- The kernel's process start-up: argc at the initial rsp, then argv[0..argc)
  pointing to NUL-terminated strings, all at or above that rsp.

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
- `sum_emit`: adds the wrapped (`mod 1440`) entry duration to the total. With
  the real `emitdur`/`emitdec`, for every 32-bit line and total, it makes one
  `write(1, buf, 1..16)` of fully written bytes, writes only inside
  `[rsp-0x80, rsp+0x50)`, and keeps rsp and the loop's registers.
- `feedb`: one step of the sum-mode parser matches the spec step for every
  parser state and input byte. It keeps rsp, r9, r13, r14 and r15, and it
  writes only the return slot of its call to `sum_emit`.

### angr whole-program checks

- **Terminal restore:** with raw mode on, `track_quit` calls
  `ioctl(TCSETS, saved)` before `exit`. With raw mode off, it calls no ioctl.
  These start at `track_quit`, after the save. The next obligation covers the
  whole quit path.

### Track mode: saving

Syscalls here return an arbitrary 64-bit value (`SysRet`): the kernel is not
modelled, so every success and failure branch is explored.

| Obligation | Claim |
|---|---|
| file name | From `track_name` to `track_named`, for every `time()` result `t`: `F_PATH` holds `timekeep-` + the decimal digits of `t mod 2^32` + `.txt` and a NUL, and `F_PATHLEN` is its length (14..23, within `F_PATH`'s 32 bytes). Digits are stated by repeated division by 10, and the digit count rules out leading zeros. The only writes are `F_PATH`, `F_PATHLEN` and the stack; the only syscall is `time(NULL)`; rbx and rsp are kept |
| quit path | From `track_quit_save`, the entry for every quit (`q`, Ctrl-C, Ctrl-D, end of input), with the rest of the frame arbitrary and `F_PATHLEN` in 14..23 (from the file-name obligation). If `F_N != 0`, the events are exactly `open(F_PATH, O_WRONLY\|O_CREAT\|O_TRUNC, 0644)`; then, if it succeeded, `render` with `F_OUTFD` set to that fd and `close(fd)`; then `write(1, F_MSG, 6 or 12 + F_PATHLEN)` and `write(1, "\n", 1)`, with `F_OUTFD` back to 1. Then `ioctl(0, TCSETS, F_TERMIOS)` if and only if `F_RAW != 0`, then `exit(0)`, on every path. Outside `render`, the only writes are `F_MSG`, `F_MSGLEN`, `F_OUTFD` and the stack, so the saved termios and the raw flag reach the restore unchanged |

In the quit-path check, `render` is replaced by `RenderFileModel`: with
`F_OUTFD != 1` it returns, keeps rbx, rbp, r12, r13 and r15, writes only the
line buffer `F_LINE` (and the stack below its return slot), makes only writes to
`F_OUTFD`, and leaves `F_OUTFD` unchanged. The next section proves the real
`render` does this.

### Render into a save file, by induction over the loop's cut points

`render` has two loops, labelled `render_row` (one row per stamp pair) and
`render_sum` (the total). Let `R` be its entry rsp, which holds the return
address. The invariant at both heads is:

- `rbx` is the frame, `rsp == R`, and rbp, r12, r13, r15 are as on entry
- `F_N <= 250` and `F_OUTFD == fd != 1` in the frame
- `render_row`: `r14` (zero-extended) `<= F_N + 1`
- `render_sum`: `r10` (zero-extended) `<= F_N + 1`, `r9` zero-extended, and
  `rdi` just past `"total "` in `F_LINE`

Every check also bounds memory. Writes stay in `F_LINE[0,128)` and the stack
below `R`. Reads stay in the frame, the stack and the code image, so `render`
can't fault on the way to returning.

| Obligation | Claim |
|---|---|
| prologue | From `render` with `F_OUTFD != 1`, one path to `render_row` with `r14 == 0`: the screen clear is skipped, no writes, no syscalls |
| `render_row` step | If `r14 < F_N`: exactly one `write(fd, F_LINE, 1..128)` (the row), then `render_row` with `r14 + 2`. Otherwise `"total "` goes into `F_LINE`, `r9 = r10 = 0`, on to `render_sum` with no syscall |
| `render_sum` step | If `r10 + 1 < F_N`: `render_sum` with `r10 + 2`, no syscall. Otherwise exactly one `write(fd, F_LINE, 1..128)` (the total), then return with `rsp == R + 8`: the message and prompt are skipped |

Stamp times are arbitrary 16-bit values, not just valid minutes of the day.

**The induction.** A file-mode `render` is the prologue, then `render_row` steps
while `r14 < F_N` (r14 rises by 2 each time), then `render_sum` steps while
`r10 + 1 < F_N` (r10 rises by 2), then the return. Each step keeps the
invariant, and both counters are bounded by `F_N <= 250`, so the run ends. This
is exactly `RenderFileModel`'s contract. The model's only other effect,
"`F_OUTFD` unchanged", follows because `F_OUTFD` is outside `F_LINE`.

Each obligation was checked against deliberately broken builds (for example,
skipping the close, wrong `open` flags, clobbering the saved termios, writing
the message out of bounds, a wrong or unterminated name), and each break was
reported as `FAILED`. The render obligations were checked the same way (rows
written to stdout, the clear or prompt written into the file, a clobbered r12,
off-by-one loop bounds, a wrong step, the line buffer overrun).

### Sum mode, by induction over the loop's cut points

`run_sum`'s loop has two heads, labelled `sum_arg` and `sum_byte` in
`timekeep.s`. Let `A` be the initial rsp (the argc slot) and `S = A - 0x100`
the frame. The invariant `Inv` at both heads is:

- `rsp == S`, `r15 == A+8` (argv), `r14 == argc`, `1 <= r13 <= argc`
  (`< argc` at `sum_byte`)
- the parser registers (ebx, ebp, r10d, r11b, r12d) hold the spec `Parser`
  state for the bytes fed so far
- nothing at or above `S+0x100` has been written, so argc, argv and the
  argument strings are as the kernel left them

| Obligation | Claim |
|---|---|
| prologue | From `_start`, for any `argc >= 2`, the single path reaches `sum_arg` with `Inv` and the spec's initial parser state, writing nothing |
| `sum_arg` step | If `r13 == argc`, exactly one syscall, `exit(0)`. Otherwise one 8-byte read, of `argv[r13]` (inside argv), into r9, then `sum_byte` with everything else unchanged. No writes |
| `sum_byte` step | One byte read, at r9. A nonzero byte `c` is fed to `feedb` as `c` and r9 advances, back to `sum_byte`. A NUL is fed as `' '` and r13 advances, back to `sum_arg`. Parser registers become the spec step. The only write is the call's return slot at `S-8`. No syscalls |

Each check runs from a symbolic state satisfying `Inv`, for one step. `feedb`
is replaced by the spec step, which the `feedb` obligation justifies.

**The induction.** Every run of sum mode is the prologue followed by a sequence
of `sum_arg`/`sum_byte` steps, and each step keeps `Inv`. So the parser sees
exactly the bytes of argv[1..argc), with `' '` after each argument, and follows
`spec.Parser`, which is `spec.sum_mode_py`. `feedb` is entered at `S-8` and
`sum_emit` at `X = S-16`. The `sum_emit` bound gives writes below `S+0x40`, well
under `A`, so `Inv`'s "nothing at or above `S+0x100` written" holds. The run
ends: each `sum_byte` step advances within a finite string or moves to the next
argument, and `sum_arg` exits once `r13 == argc`.

**Result, for every argv:** the only syscalls are `write(1, …)` and `exit(0)`.
There are no writes to argc, argv, the argument strings or the code image, and
the running totals written are the spec's. The only caveat is the text of each
line: it comes from `emitdur`, which is checked on sampled values, not all
32-bit totals.

**Memory.** No check runs more than one loop iteration, so path counts stay in
the tens (the `render_row` step, the largest, has 46). `explore_capped` in
`prove.py` fails an obligation if its path or step cap is hit instead of pruning
paths. A full `make verify` peaks at about 1.5 GB of RSS and takes about 2.5
minutes. The earlier bounded whole-program check needed 25–30 GB and was
replaced by these obligations.

The cut-point checks fix concrete stack and buffer addresses. The loop never
compares a pointer with an absolute address, so the choice doesn't matter.
