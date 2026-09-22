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

## Building

Requires GNU `as`, `objcopy`, and gcc for the C reference.

```sh
make            # builds timekeep and timekeep-c
make size       # byte counts of both
make test       # runs the test suite against both binaries
make clean
```

## Tests

`test.sh <bin>` covers wrapping, CRLF, blank lines, entries split across
arguments, no arguments, and so on.

## Formal verification

```sh
make verify-setup   # one time: creates verify/.venv with angr + z3 (needs uv)
make verify         # runs verify/prove.py
```

`make verify` runs inside a user systemd scope capped at 12 GB of RAM with
swap disabled. A normal run peaks at about 420 MB and takes under a minute.
The cap is there so that a runaway symbolic execution (for example, after a
change to the program) gets OOM-killed on its own instead of taking the
terminal with it. Set a different
cap with `make verify VERIFY_MEM=8G`.

See [`verify/README.md`](verify/README.md) for what is proved and what is not.

## Layout

```
timekeep.s        hand-built ELF (headers included), the shipped program
timekeep.c        freestanding C reference
Makefile
test.sh           tests
verify/
  spec.py         trusted functional spec (Z3 formulas + Python mirrors)
  prove.py        proof driver (Z3 theorems + angr over the shipped binary)
```
