# daykeep verification

`make verify` runs `dkprove.py`, which checks the decimal-time library
`src/dktime.c` against `dkspec.py`. It proves the arithmetic model with Z3 and
uses seeded compiled checks to connect that model to the C implementation.

## How the claims connect to the code

1. **Z3 theorems over a model.** `dkprove.py` transcribes the arithmetic of
   `dktime.c`: C's truncating division and remainder, floor division, digit
   completion, range-end parsing, tick rounding, and decimal fraction parsing.
   Side conditions show every intermediate fits its declared width.
2. **The compiled C against the spec.** `dkprove.py` builds `dktime.c` in a
   temporary directory and calls it through `ctypes`. It compares results with
   the definition-based Python mirrors in `dkspec.py` using seeded random
   inputs and boundary values.

The compiled checks are sampling, not proof. They make the transcription
credible: planted off-by-one completion, rollover, and rounding mutations fail
at least one obligation.

## Trusted base

- `dkspec.py`, Z3, and the C compiler.
- The arithmetic transcription in `dkprove.py`, backed by compiled checks.
- libc's `snprintf` for printed digits; the theorems reason about the values
  passed to it and the checks compare formatted strings.

## Obligations

Each theorem runs at day lengths `1`, `3`, `9999`, `10000`, `10001`, `43200`,
`86400`, `88775`, `90000`, and `1000000000`. These are proofs at those stated
lengths, not a quantified proof over every accepted length. The completion and
range-end domain bounds stored seconds to `|s| < 2^49`; rounding and monotonic
tick claims cover signed 64-bit seconds where their shifted intermediate fits.

| Obligation | Claim |
|---|---|
| complete, no `+` | A partial day number completes to the latest matching day no later than the reference; a full number is literal. |
| complete, `+` | The same completion rule toward the future. |
| range end | Representable ends are the first matching instant on or after START; a literal earlier end is rejected. |
| range end, earliest | With a non-full-day fraction, no earlier matching instant lies between START and the result. |
| fraction digits | Zero padding preserves a parsed fraction; valid fractions remain inside one day. |
| ticks | Four-decimal ticks round half-up, do not overflow, are monotone, and advance by 10,000 per day. |
| stamp round-trip | A printed nonnegative stamp reads back to the same printed form, within the documented rounding error. |

The compiled checks cover day completion, stamp and range-end parsing in UTC,
and all decimal, hour/minute, and minute formatters. Local-time conversion and
the command line are covered by the shell tests in `tests/`.

## Fuzzing

`make fuzz` and `make fuzz-asan` run `fuzz.py`; `make fuzz-libfuzzer` runs the
parser harness in `dk_libfuzzer.c`. They vary the representative day lengths
and look for model disagreements, crashes, hangs, broken write-error handling,
or tracker logs that cannot be read back.

| Group | Oracle |
|---|---|
| sum | A model of input lines, tokens, open ranges, and formats on top of `dkspec.py`. |
| convert | The parsing model in fixed zones, plus stamp-to-clock-to-stamp checks in real DST zones using `date(1)`. |
| garbage | Permitted exit status, diagnostic and stdout conventions, and no unexpected files. |
| `/dev/full` | Exit status 2 with a write error. |
| track | Log persistence, grammar, read-back, and agreement with the final tracker screen. |
| libFuzzer | Sanitizers, range-end ordering, and printed-stamp round trips. |

Runs are seeded and bounded with `FUZZ_SEED` and `FUZZ_ITERS`. A discovered bug
becomes a regression case in `tests/`.
