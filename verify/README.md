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
- The kernel's process start-up: argc at the initial rsp, then argv[0..argc)
  pointing to NUL-terminated strings, all at or above that rsp.

## Obligations

### Z3 theorems (hold for every input)

| Obligation | Claim |
|---|---|
| duration | For `start, end` in `[0,1440)`, the result is in `[0,1440)` and equals `(end-start) mod 1440` |

This one is about `spec.duration_z3` only. Since the tracker was removed, no
routine in the binary uses it: sum mode wraps with `line mod 1440`, which the
`sum_emit` obligation covers.

### angr equivalence over the shipped binary

Each leaf routine runs with symbolic inputs and every path is explored. The
output is compared to the spec, and every write must land in the expected
output region.

- `emitdec`, `emitdur`: number formatting
- `sum_emit`: adds the wrapped (`mod 1440`) entry duration to the total. With
  the real `emitdur`/`emitdec`, for every 32-bit line and total, it makes one
  `write(1, buf, 1..16)` of fully written bytes, writes only inside
  `[rsp-0x80, rsp+0x50)`, and keeps rsp and the loop's registers.
- `feedb`: one step of the sum-mode parser matches the spec step for every
  parser state and input byte. It keeps rsp, r9, r13, r14 and r15, and it
  writes only the return slot of its call to `sum_emit`.

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
| prologue | From `_start`, for any `argc >= 1`, the single path reaches `sum_arg` with `Inv` and the spec's initial parser state, writing nothing |
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
argument, and `sum_arg` exits once `r13 == argc`. With no arguments
(`argc == 1`) that is the first step: the program exits 0 without output.
`argc == 0` is outside the claim; Linux has passed at least an empty `argv[0]`
since 5.18.

**Result, for every argv:** the only syscalls are `write(1, …)` and `exit(0)`.
There are no writes to argc, argv, the argument strings or the code image, and
the running totals written are the spec's. The only caveat is the text of each
line: it comes from `emitdur`, which is checked on sampled values, not all
32-bit totals.

**Memory.** No check runs more than one loop iteration, so path counts stay
small. `explore_capped` in `prove.py` fails an obligation if its path or step
cap is hit instead of pruning paths. A full `make verify` peaks at about 420 MB
of RSS and takes about 40 seconds.

The cut-point checks fix concrete stack and buffer addresses. The loop never
compares a pointer with an absolute address, so the choice doesn't matter.

# daykeep: decimal time

`dkprove.py` checks the time library `src/dktime.c` against `dkspec.py`.
Run it with `make verify` (after the timekeep proofs) or on its own with
`make verify-daykeep`. It takes about 35 seconds and uses no angr.

## How the claims connect to the code

1. **Z3 theorems over a model.** `dkprove.py` has a class `C` that transcribes
   the arithmetic of `dktime.c` line by line: C's truncating `/` and `%`,
   `dk_floor_div`, the `ndigits` loop (unrolled), `dk_complete_day`, the
   decimal branch of `dk_parse_end`, `ticks`, and the fraction conversion in
   `parse_decimal`. Every intermediate adds a side condition that it fits in
   64 bits, so each theorem also shows the code does not overflow. The rules
   in `dkspec.py` are stated as properties (for example "no later day ≤ ref
   ends in these digits"), not as the same formula.
2. **The compiled C against the spec.** `dkprove.py` builds `dktime.c` as a
   shared library in a temporary directory and calls it through ctypes. It
   compares the results with the Python mirrors in `dkspec.py`, which work
   from the definitions (the completion mirrors search day by day). The
   inputs are seeded random values (`DK_VERIFY_SEED`, `DK_VERIFY_ITERS`,
   200000 cases by default) plus every second of days -1, 0 and 14301.

Step 2 is sampling, not proof: it is what makes the model in step 1 believable
as the code. A mutation of either the C or the model (an off-by-one in the
"full number" test, a wrong rollover day, truncating instead of rounding)
fails at least one obligation.

## Trusted base

- `dkspec.py`, Z3, and the C compiler.
- The transcription in class `C` for the theorems, backed by the checks.
- libc's `snprintf` for the digits of formatted values (the theorems reason
  about the day and tick numbers it is given; the checks compare the strings).

## Obligations

Domain: seconds `|s| < 2^49` (about 17.8 million years either side of day 0),
days `|ref| < 2^49 / 86400`, typed day digits `n = 0..9` (dktime reads at most
9), `0 <= typed < 10^n`, and a time of day `0 <= frac <= 86400` seconds.

### Z3 theorems (hold for every input in the domain)

| Obligation | Claim |
|---|---|
| complete, no `+` | With no digits the day is ref. A full day number (at least as many digits as ref, or ref < 0) is literal. Otherwise the result ends in the typed digits, is ≤ ref, and no later day ≤ ref ends in them |
| complete, `+` | The same, but ≥ ref with no earlier day ≥ ref ending in them |
| range end | A full day number is literal and is an error (`-2`) exactly when it falls before START. Otherwise the result is on a day ≥ START's day that ends in the typed digits, is not before START, and is the least such day |
| range end, earliest | When `frac < 86400`, no instant ending in the typed digits with that time of day lies between START and the result |
| fraction digits | 1–4 typed fraction digits land exactly on the tick they name (`.5` = `.5000`), and any 1–9 digits give a time of day in `[0, 86400]` |
| ticks | Rounding to 1/10000 day is half-up and does not overflow |
| ticks, monotone | `s ≤ s'` gives `ticks(s) ≤ ticks(s')`, and one day later is exactly 10000 ticks later |
| stamp round-trip | For day numbers 0 to 999999999, reading back a printed stamp gives a value that prints the same |
| stamp round-trip, error | That value is within 4 s of the original |

The "earliest" claim needs `frac < 86400`. Nine fraction digits can round up
to a full day (`.999999999` is 86400 s), and that END is placed on the day
the digits name, the day before the next midnight, not at START itself.

### The compiled C (seeded checks)

| Check | Against |
|---|---|
| `dk_complete_day` | the day-by-day search, n = 0..4 |
| `dk_parse_stamp` (UTC) | the grammar, completion from now, `HH:MM[:SS]` on now's day, rejections |
| `dk_parse_end` (UTC) | the day-by-day search from START's day, literal and `-2` cases, `HH:MM` rollover |
| `dk_fmt_stamp`, `dk_fmt_dur`, `dk_fmt_hm`, `dk_fmt_minutes` | the Python formatters, plus `dk_parse_literal` round-trips of each stamp |

## Not covered

- Local time. `HH:MM` and `--convert` in a real time zone go through
  `mktime`/`localtime_r`, whose DST handling is libc's. The checks run in UTC.
- `dk_parse_date`, `dk_fmt_clock` and the command line. The shell tests in
  `tests/` cover them.
