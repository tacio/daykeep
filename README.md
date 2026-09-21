# timekeep

A tiny work-session tracker for x86_64 Linux. It sums `HH:MM - HH:MM` time
ranges, or runs as an interactive tracker you stamp with a keypress.

It ships in two forms that behave the same:

| Binary       | Source        | Size     | Notes                                        |
|--------------|---------------|----------|----------------------------------------------|
| `timekeep`   | `timekeep.s`  | ~2.2 KB  | hand-built ELF, no libc, raw syscalls        |
| `timekeep-c` | `timekeep.c`  | ~3.0 KB  | freestanding C reference for the assembly    |

`timekeep.s` is the shipped program. `timekeep.c` is its readable reference.
Both run the same tests, and `verify/` checks the assembled bytes formally.

## Usage

### Sum mode: `timekeep <entries...>`

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

### Tracker mode: `timekeep` (no arguments)

```
 1  09:00 - 10:30   1h 30m
 2  11:00 - ...     (running)
total 1h 30m
[enter] stamp  [x] remove last  [e] edit last  [q] quit
```

| Key                  | Action                                              |
|----------------------|-----------------------------------------------------|
| Enter                | stamp the current local time (starts or ends an entry) |
| `x`                  | remove the last stamp                               |
| `e`                  | edit the last stamp (`HH:MM`; bad input keeps the old value) |
| `q`, Ctrl-C, Ctrl-D  | quit                                                |

On a terminal, stdin goes into raw mode (no echo, single keystrokes) and is
restored on quit. The local time comes from `/etc/localtime` (TZif). If that
file is missing or malformed, it falls back to UTC. Nothing is saved: the
session exists only while the program runs. It holds at most 250 stamps.

## Building

Requires GNU `as`, `objcopy`, and gcc for the C reference.

```sh
make            # builds timekeep and timekeep-c
make size       # byte counts of both
make test       # runs both test suites against both binaries
make clean
```

## Tests

- `test.sh <bin>`: sum-mode cases (wrapping, CRLF, blank lines, entries split
  across arguments, and so on).
- `test-track.sh <bin>`: drives the tracker through a pipe. Times are made
  predictable by stamping and then editing to a fixed value. A final
  `live-clock` case checks that a bare stamp matches `date +%H:%M`.

## Formal verification

```sh
make verify-setup   # one time: creates verify/.venv with angr + z3 (needs uv)
make verify         # runs verify/prove.py
```

`make verify` runs inside a user systemd scope capped at 12 GB of RAM with
swap disabled. Symbolic execution can use a lot of memory, and without the
cap a runaway run gets OOM-killed along with the terminal it runs in. Set a
different cap with `make verify VERIFY_MEM=8G`.

See [`verify/README.md`](verify/README.md) for what is proved and what is not.

## Layout

```
timekeep.s        hand-built ELF (headers included), the shipped program
timekeep.c        freestanding C reference
Makefile
test.sh           sum-mode tests
test-track.sh     tracker-mode tests
verify/
  spec.py         trusted functional spec (Z3 formulas + Python mirrors)
  prove.py        proof driver (Z3 theorems + angr over the shipped binary)
```
