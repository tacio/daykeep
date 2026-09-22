# timekeep

A tiny x86_64 Linux program that sums `HH:MM - HH:MM` time ranges. It is a
study piece: a hand-built ELF of a few hundred bytes, formally verified.

It ships in two forms that behave the same:

| Binary       | Source        | Size     | Notes                                        |
|--------------|---------------|----------|----------------------------------------------|
| `timekeep`   | `timekeep.s`  | 434 B    | hand-built ELF, no libc, raw syscalls        |
| `timekeep-c` | `timekeep.c`  | 840 B    | freestanding C reference for the assembly    |

`timekeep.s` is the shipped program. `timekeep.c` is its readable reference.
Both run the same tests, and `verify/` checks the assembled bytes formally.

## Usage

### `timekeep <entries...>`

Every run of digits is a number, and anything else is a separator. Each group
of four numbers makes one entry (`start_h start_m end_h end_m`). After each
entry it prints the running total:

```console
$ timekeep '09:00 - 10:30' '11:00 - 11:45'
1h 30m
2h 15m
$ timekeep "$(cat today.log)"      # one range per line
```

- Ranges that cross midnight wrap: `23:24 - 00:52` is `1h 28m`.
- A newline drops a half-finished entry. Blank lines and `\r\n` are fine.
- An entry can span several arguments, so `timekeep 10:00 - 11:00` works.
- With no arguments there is nothing to sum, so it prints nothing and exits 0.

## daykeep

`daykeep` is a hosted-C companion utility (in `src/`) that works in decimal
time: a day number plus a fraction of a day, counted from day 0 =
1987-07-28 00:00 UTC. 90 minutes is `.0625`. It sums ranges, tracks time
interactively, and does `--now` and `--convert`.

```console
$ daykeep .9 - .1                      # an END is completed from its START
.2000
$ daykeep --hm '09:00 - 10:30' 11:00 - 11:45    # old timekeep input works
1h 30m
2h 15m
$ daykeep -s --format=minutes -f today.log      # -f FILE, else stdin
135
$ daykeep --now
14301.4564
$ daykeep --convert .5 9 +9 10:00      # stamps may leave out digits
2026-09-22 09:00:00 -0300
2026-09-19 21:00:00 -0300
2026-09-29 21:00:00 -0300
14301.5417
```

See `daykeep --help` for the digit-completion rules, `man daykeep`, or
`info daykeep` for the full manual (the calendar, the epoch, the units).

### Tracker

`daykeep --track` (`-t`) is the interactive tracker:

```console
$ daykeep -t -a work.log
 1  14301.3750 - 14301.4375   .0625
 2  14301.4564 - ...          (running)
total .0625
[enter] stamp  [x] remove last  [e] edit last  [s] save  [q] quit
```

- Enter stamps the current time. Stamps start and end entries in turn. If
  the start was edited into the future, the entry ends at its start instead
  (then edit the end), so no entry ends before it starts.
- `e` edits the last stamp. It takes any stamp form (`.4`, `14:30`,
  `14301.4`), completed from the stamp before it, so `.9` then `.1` ends the
  next day.
- `x` removes the last stamp. `q`, `^C`, `^D` or end of input quit.
- With `-a LOG`, the session's entries go to LOG as `START - END` lines
  (`START -` while open). LOG is rewritten after every change, so a signal
  or crash loses nothing, and `daykeep -f LOG` sums it. Older lines are never
  touched. If LOG ends in an open entry, the next `-t -a LOG` resumes it. Only
  one tracker at a time can use a LOG.
- Without `-a`, nothing is written to disk.
- `--hm`/`--format` and `-u` apply as in sum mode.

## Building

Requires GNU `as`, `objcopy`, and gcc (glibc for daykeep). In a git
checkout, the daykeep docs also need `help2man` and `makeinfo` (Texinfo);
the release tarball ships them already built.

```sh
make            # builds timekeep, timekeep-c and daykeep
make size       # byte counts
make test       # timekeep test suite against both binaries
make check      # make test plus the daykeep tests
make doc        # daykeep man page (doc/daykeep.1) and manual (doc/daykeep.info)
make install    # daykeep, its man page, manual and bash completion
make uninstall
make dist       # daykeep-VERSION.tar.gz
make distcheck  # build, check, install and uninstall that tarball
make clean      # maintainer-clean also removes the generated docs
```

`make install` honors `prefix`, `bindir`, `mandir`, `infodir`,
`bashcompdir` and `DESTDIR`. `make install-strip` strips the binary.

## Tests

`test.sh <bin>` covers wrapping, CRLF, blank lines, entries split across
arguments, no arguments, and so on.

### Fuzzing

```sh
make fuzz            # verify/fuzz.py, about 5 seconds (needs verify/.venv)
make fuzz-asan       # the same against daykeep built with ASan + UBSan
make fuzz-libfuzzer  # coverage-guided fuzzing of the parsers (needs clang)
```

`verify/fuzz.py` runs seeded, bounded groups of generated cases. timekeep
and timekeep-c must agree with `verify/spec.py` on random and mutated argv.
daykeep sum mode and `--convert` must agree with a Python model built on
`verify/dkspec.py`, and `--convert` round trips are checked against
`date(1)` in zones with DST. Garbage input must not crash or hang daykeep.
Random key streams into `--track -a LOG` must leave a log that reads back
with `-f`. `FUZZ_ITERS=3000` runs longer, `FUZZ_SEED=n` tries other cases,
and `make fuzz-libfuzzer FUZZ_RUNS=n` sets the libFuzzer run count. Each
failure prints a reproducer; bugs it finds become cases in `tests/`. Fuzzing
is not part of `make check`.

## Formal verification

```sh
make verify-setup   # one time: creates verify/.venv with angr + z3 (needs uv)
make verify         # runs verify/prove.py, then verify/dkprove.py
make verify-daykeep # only the daykeep part
```

`verify/dkprove.py` covers daykeep's decimal time: Z3 proves that a model of
`src/dktime.c` completes typed digits to the right day, rolls range ends over
correctly and rounds so every printed stamp reads back as itself, and seeded
checks of the compiled `dktime.c` (via ctypes) tie that model to the code.
It takes about 35 seconds; `DK_VERIFY_ITERS` and `DK_VERIFY_SEED` change the
number of random cases and the seed.

`make verify` runs inside a user systemd scope capped at 12 GB of RAM with
swap disabled. A normal run peaks at about 420 MB and takes under a minute.
The cap is there so that a runaway symbolic execution (for example, after a
change to the program) gets OOM-killed on its own instead of taking the
terminal with it. Set a different
cap with `make verify VERIFY_MEM=8G`.

See [`verify/README.md`](verify/README.md) for what is proved and what is not.

## License

GPLv3 or later; see [`COPYING`](COPYING). The manual, the Makefile and the
bash completion are under an all-permissive license (see each file).
[`NEWS`](NEWS) lists user-visible changes, [`AUTHORS`](AUTHORS) and
[`THANKS`](THANKS) the people and projects involved.

## Layout

```
timekeep.s        hand-built ELF (headers included), the shipped program
timekeep.c        freestanding C reference
Makefile
test.sh           timekeep tests
src/
  dktime.[ch]     decimal-time library (parse, complete, format)
  daykeep.h       what the command line and the tracker share
  daykeep.c       daykeep command line
  track.c         the interactive tracker (--track)
tests/
  daykeep-core.sh daykeep tests (clock pinned via DAYKEEP_NOW)
  daykeep-sum.sh  daykeep sum mode, cross-checked against ./timekeep
  daykeep-track.sh the tracker, driven over a pipe
  distcheck.sh    checks a release tarball (make distcheck)
doc/
  daykeep.texi    the daykeep manual (Texinfo)
  daykeep.h2m     extra man page sections for help2man
completion/
  daykeep         bash completion
verify/
  spec.py         trusted functional spec (Z3 formulas + Python mirrors)
  prove.py        proof driver (Z3 theorems + angr over the shipped binary)
  dkspec.py       daykeep's decimal-time rules (Z3 predicates + mirrors)
  dkprove.py      Z3 theorems over dktime.c, and checks of the compiled C
  fuzz.py         differential and robustness fuzzing (make fuzz)
  dk_libfuzzer.c  libFuzzer harness for the parsers (make fuzz-libfuzzer)
```
