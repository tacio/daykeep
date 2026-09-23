#!/usr/bin/env python3
"""Model-based and robustness fuzzing for daykeep.

Each group runs a fixed, seeded number of generated cases and prints ok or
FAIL; the script exits non-zero if any group fails.  A failure prints a
reproducer (argv, environment and input) so it can become a regression case
in tests/.

  sum        daykeep sum mode against a Python model of it built on
             dkspec.py: completion, END rollover, open ranges, every
             --format, -s; operands, -f FILE and stdin
  convert    daykeep --convert against the model in fixed-offset zones, and
             stamp -> date -> stamp round trips in real zones (with DST),
             with date(1) as the oracle for the clock strings
  garbage    random bytes, long lines, huge numbers and odd options: no
             crash or hang, exit status 0, 1 or 2, nothing but results on
             stdout
  track      random key streams into --track -a LOG over a pipe: the log
             always reads back with -f, keeps earlier sessions' lines, and
             holds exactly the entries on the tracker's last screen

FUZZ_ITERS (default 300) sets the cases per group, FUZZ_SEED the seed.
--daykeep BIN picks the binary (make fuzz-asan passes a sanitizer build).
"""

import argparse
import datetime
import os
import random
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import dkspec as S   # noqa: E402

DAY = S.DAY
EPOCH_UNIX = S.EPOCH
ITERS = int(os.environ.get("FUZZ_ITERS", "300"))
SEED = int(os.environ.get("FUZZ_SEED", "20260922"))
TIMEOUT = 10
SAN_EXIT = 99        # sanitizer reports exit with this, never 0..2

BASE_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "LC_ALL": "C",
    "ASAN_OPTIONS": f"exitcode={SAN_EXIT}:detect_leaks=1",
    "UBSAN_OPTIONS": f"halt_on_error=1:exitcode={SAN_EXIT}:print_stacktrace=1",
}

# fixed-offset zones: POSIX TZ string, seconds east of UTC
FIXED_ZONES = [("UTC0", 0), ("<-03>3", -10800), ("<+0545>-5:45", 20700),
               ("<+14>-14", 50400), ("<-12>12", -43200)]
# real zones for the round trips (DST, half-hour DST, odd offsets)
REAL_ZONES = ["America/Sao_Paulo", "Europe/Berlin", "America/New_York",
              "Australia/Lord_Howe", "Asia/Kathmandu", "Pacific/Chatham", "UTC"]

failures = 0


class Case(Exception):
    """A failed case: what went wrong and how to reproduce it."""


def run(argv, stdin=b"", env=None, cwd=None):
    e = dict(BASE_ENV)
    e.update(env or {})
    try:
        p = subprocess.run(argv, input=stdin, env=e, cwd=cwd, timeout=TIMEOUT,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired:
        raise Case(f"hang (> {TIMEOUT} s)")
    if p.returncode == SAN_EXIT or b"Sanitizer" in p.stderr or b"runtime error" in p.stderr:
        raise Case(f"sanitizer report\n{p.stderr.decode('latin-1')[-4000:]}")
    if p.returncode < 0:
        raise Case(f"killed by signal {-p.returncode}\nstderr: {p.stderr[-2000:]!r}")
    return p.returncode, p.stdout, p.stderr


def group(name, fn, rng, iters):
    """Run fn(rng) iters times; stop at the first failure and show it."""
    global failures
    for i in range(iters):
        repro = {}
        try:
            fn(rng, repro)
        except Case as e:
            failures += 1
            print(f"FAIL {name} (case {i}): {e}")
            for k, v in repro.items():
                print(f"     {k}: {v!r}")
            return
    print(f"ok   {name} ({iters} cases)")


# ------------------------------------------------------- daykeep model
BLANKS = b" \t\r\n\f\v"
TOKEN = re.compile(rb"-|[^ \t\r\n\f\v-]+")
FMT = {"decimal": S.fmt_dur_py, "hm": S.fmt_hm_py, "minutes": S.fmt_minutes_py}


def sum_model(text, now, off, fmt, summarize):
    """daykeep's sum mode on TEXT (lines split at \\n, each cut at a NUL):
    -> (stdout, status)."""
    total, out, status = 0, [], 0

    def add(a, b):
        nonlocal total, status
        if S.checked(b - a) is None or S.checked(total + b - a) is None:
            status = 1
            return
        total += b - a
        if not summarize:
            out.append(FMT[fmt](total))

    for line in text.split(b"\n"):
        line = line.split(b"\0")[0]
        state, start, bad = "start", 0, False
        for tok in TOKEN.findall(line):
            t = tok.decode("latin-1")
            if state == "start":
                start = None if tok == b"-" else S.parse_stamp_py(t, now, off)
                if start is None:
                    bad = True
                    break
                state = "dash"
            elif state == "dash":
                if tok != b"-":
                    bad = True
                    break
                state = "end"
            else:
                end = None if tok == b"-" else S.parse_end_py(t, start, off)
                if end is None or end == "before":
                    bad = True
                    break
                add(start, end)
                state = "start"
        if bad or state == "dash" or (state == "end" and now < start):
            status = 1
        elif state == "end":
            add(start, now)
    if summarize:
        out.append(FMT[fmt](total))
    return b"".join(o.encode() + b"\n" for o in out), status


def rand_now(rng):
    day = rng.choice([9761, 9761, 14301, 14301, 14300, 9999, 10000, 99999, 100000,
                      123456, 3, 0, -2])
    return day * DAY + rng.randrange(DAY)


def rand_zone(rng):
    """-> (TZ, off, extra args)"""
    tz, off = rng.choice(FIXED_ZONES)
    if rng.random() < 0.2:
        return tz, 0, ["-u"]
    return tz, off, []


def digits(rng, n):
    return "".join(rng.choice("0123456789") for _ in range(n))


def rand_stamp(rng, ref_day, max_full=9):
    """A stamp: completed digits, a full day number, HH:MM, or junk."""
    k = rng.random()
    if k < 0.2:
        h = rng.randrange(26) if rng.random() < 0.1 else rng.randrange(24)
        m = rng.randrange(61) if rng.random() < 0.1 else rng.randrange(60)
        s = f"{h}:{m:02d}" if rng.random() < 0.5 else f"{h:02d}:{m:02d}"
        return s + (f":{rng.randrange(60):02d}" if rng.random() < 0.2 else "")
    if k < 0.25:
        return "".join(rng.choice("0123456789+.:x\xe9") for _ in range(rng.randrange(1, 6)))
    full = len(str(abs(ref_day)))
    if k < 0.85 or full > max_full:
        n = rng.randrange(0, min(full, 5))
    else:
        n = rng.randrange(full, max_full + 1)
    s = ("+" if rng.random() < 0.25 else "") + digits(rng, n)
    # fraction digits past 9 are read and ignored; a few runs are long
    frac = digits(rng, rng.randrange(0, 11) if rng.random() < 0.95 else rng.randrange(60, 300))
    if frac or n == 0 or rng.random() < 0.3:
        s += "." + frac
    return s


def rand_sum_text(rng, ref_day, allow_nul):
    lines = []
    for _ in range(rng.randrange(0, 5)):
        parts = []
        for _ in range(rng.randrange(0, 4)):
            parts.append(rand_stamp(rng, ref_day))
            r = rng.random()
            if r < 0.05:
                continue                                   # missing dash
            parts.append(rng.choice(["-", "-", "--"]) if r < 0.97 else "")
            if rng.random() < 0.9:
                parts.append(rand_stamp(rng, ref_day))     # else: open range
        sep = lambda: rng.choice([" ", " ", "\t", "  ", " \f", "", "\v"])
        line = "".join(p + sep() for p in parts)
        if allow_nul and rng.random() < 0.05:
            pos = rng.randrange(len(line) + 1)
            line = line[:pos] + "\0" + line[pos:]
        lines.append(line)
    eol = rng.choice(["\n", "\r\n"])
    text = eol.join(lines) + (eol if rng.random() < 0.7 else "")
    return text.encode("latin-1")


def now_env(now, tz):
    return {"DAYKEEP_NOW": f"@{now + EPOCH_UNIX}", "TZ": tz,
            "DAYKEEP_DAY_LENGTH": str(DAY)}


def choose_length(rng):
    global DAY
    DAY = S.DAY = rng.choice(S.LENGTHS)


def case_sum(rng, repro, dk, tmp):
    choose_length(rng)
    now = rand_now(rng)
    tz, off, zargs = rand_zone(rng)
    fmt = rng.choice(list(FMT))
    summarize = rng.random() < 0.2
    how = rng.choice(["operand", "words", "file", "stdin"])
    text = rand_sum_text(rng, now // DAY, how in ("file", "stdin"))
    args = [dk, *zargs, f"--format={fmt}"] + (["-s"] if summarize else [])
    stdin = b""
    if how == "operand":
        args += ["--", text]
    elif how == "words":
        args += ["--", *[w for w in text.split(b" ")]]
    elif how == "file":
        path = os.path.join(tmp, "in.txt")
        with open(path, "wb") as f:
            f.write(text)
        args += ["-f", path]
    else:
        stdin = text
    env = now_env(now, tz)
    repro.update(argv=args, env=env, stdin=stdin, text=text)
    want, wrc = sum_model(text, now, off, fmt, summarize)
    rc, out, err = run(args, stdin, env)
    if (out, rc) != (want, wrc) or bool(err) != (wrc == 1):
        raise Case(f"exit {rc} (want {wrc}), got {out!r}, want {want!r}, "
                   f"stderr {err[-500:]!r}")


# ----------------------------------------------------------- --convert
def clock_py(s, off):
    z = datetime.timezone(datetime.timedelta(seconds=off))
    t = datetime.datetime.fromtimestamp(s + EPOCH_UNIX, z)
    return t.strftime("%Y-%m-%d %H:%M:%S ") + ("+" if off >= 0 else "-") + \
        f"{abs(off) // 3600:02d}{abs(off) % 3600 // 60:02d}"


def case_convert_model(rng, repro, dk):
    """Values with ':' print a stamp, others a clock, per the model."""
    choose_length(rng)
    now = 14301 * DAY + rng.randrange(DAY) if rng.random() < 0.8 else rand_now(rng)
    now = min(max(now, 0), 100000000000)
    tz, off, zargs = rand_zone(rng)
    vals = [rand_stamp(rng, now // DAY, max_full=6) for _ in range(rng.randrange(1, 20))]
    vals = [v for v in vals if v and not v.startswith("-")]
    # Python's calendar oracle supports only four-digit years.
    vals = [v for v in vals if (s := S.parse_stamp_py(v, now, off)) is None
            or -31536000 <= s <= 100000000000]
    want, wrc = b"", 0
    for v in vals:
        s = S.parse_stamp_py(v, now, off)
        if s is None:
            wrc = 1
        elif ":" in v:
            want += S.fmt_stamp_py(s).encode() + b"\n"
        else:
            want += clock_py(s, off).encode() + b"\n"
    args = [dk, *zargs, "--convert", "--", *[v.encode("latin-1") for v in vals]]
    env = now_env(now, tz)
    repro.update(argv=args, env=env)
    if not vals:
        return
    rc, out, err = run(args, b"", env)
    if (out, rc) != (want, wrc):
        raise Case(f"exit {rc} (want {wrc}), got {out!r}, want {want!r}, stderr {err!r}")


def case_convert_roundtrip(rng, repro, dk):
    """stamp -> clock (checked by date(1)) -> stamp, in a real zone."""
    choose_length(rng)
    zone = rng.choice(REAL_ZONES)
    env = {"TZ": zone, "DAYKEEP_NOW": "0", "DAYKEEP_DAY_LENGTH": str(DAY)}
    secs = [rng.randrange(0, min(60000 * DAY, 100000000000)) for _ in range(40)]
    stamps = [S.fmt_stamp_py(s) for s in secs]
    repro.update(env=env, stamps=stamps)
    rc, out, err = run([dk, "--convert", *stamps], b"", env)
    clocks = out.decode().splitlines()
    if rc != 0 or len(clocks) != len(stamps):
        raise Case(f"stamps -> clocks: exit {rc}, {err!r}")
    exact = [S.parse_stamp_py(st, 0) for st in stamps]    # full: literal
    feed = "".join(f"@{e + EPOCH_UNIX}\n" for e in exact).encode()
    rc, dout, err = run(["date", "-f", "-", "+%Y-%m-%d %H:%M:%S %z"], feed, env)
    if rc != 0 or dout.decode().splitlines() != clocks:
        bad = [(st, c, d) for st, c, d in zip(stamps, clocks, dout.decode().splitlines())
               if c != d]
        raise Case(f"daykeep and date(1) disagree: {bad[:3]}")
    rc, back, err = run([dk, "--convert", *clocks], b"", env)
    if rc != 0 or back.decode().splitlines() != stamps:
        bad = [(st, c, b) for st, c, b in zip(stamps, clocks, back.decode().splitlines())
               if st != b]
        raise Case(f"clocks -> stamps: exit {rc} {err!r}, not the identity: {bad[:3]}")


# -------------------------------------------------------------- garbage
OUT_LINE = re.compile(rb"-?[0-9]*\.[0-9]{4}|-?[0-9]+h [0-9]+m|-?[0-9]+"
                      rb"|[0-9]{4,}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} [+-][0-9]{4}"
                      rb"|\?")
OPTIONS = ["--day-length", "--day-length=1", "--day-length=1000000000",
           "--day-length=0", "--day-length=-1", "--day-length=1.5",
           "--day-length=1000000001", "--epoch=1987-07-28",
           "-s", "--summarize", "--hm", "--format=hm", "--format=minutes",
           "--format=bogus", "--format", "-u", "--utc", "--now", "-c", "--convert",
           "-a", "log", "--append=log", "-f", "--", "-", "-q", "-z", "--bogus",
           "--zz=1", "-fx", "-sz"]


def junk(rng, n):
    k = rng.random()
    if k < 0.3:
        return bytes(rng.randrange(256) for _ in range(n))
    if k < 0.6:                                           # huge numbers
        return b" - ".join(str(rng.randrange(10**rng.randrange(1, 60))).encode()
                           + rng.choice([b"", b".", b".9", b":00",
                                         b"." + b"9" * rng.randrange(1, 400)])
                           for _ in range(max(1, n // 20)))
    return bytes(rng.choice(b"0123456789+-.: \t\r\n") for _ in range(n))


def case_garbage(rng, repro, dk, tmp):
    choose_length(rng)
    n = rng.choice([0, 1, 10, 100, 1000, 70000])
    data = junk(rng, n)
    with open(os.path.join(tmp, "g.txt"), "wb") as f:
        f.write(data)
    args = [dk]
    for _ in range(rng.randrange(0, 5)):
        args.append(rng.choice(OPTIONS + ["g.txt", "missing.txt", "/"]))
    ops = [junk(rng, rng.randrange(0, 40)).replace(b"\0", b"") for _ in range(rng.randrange(0, 3))]
    args += ops
    env = {"TZ": rng.choice(FIXED_ZONES)[0] if rng.random() < 0.7 else rng.choice(REAL_ZONES)}
    env["DAYKEEP_DAY_LENGTH"] = str(DAY)
    r = rng.random()
    if r < 0.8:
        env["DAYKEEP_NOW"] = f"@{rand_now(rng) + EPOCH_UNIX}"
    elif r < 0.9:
        env["DAYKEEP_NOW"] = junk(rng, 12).replace(b"\0", b"").decode("latin-1")
    stdin = data if rng.random() < 0.5 else b""
    repro.update(argv=args, env=env, stdin=stdin[:200], stdin_len=len(stdin))
    rc, out, err = run(args, stdin, env, cwd=tmp)
    if rc not in (0, 1, 2):
        raise Case(f"exit {rc}, stderr {err[-500:]!r}")
    if rc != 0 and not err:
        raise Case(f"exit {rc} with nothing on stderr")
    for line in out.splitlines():
        if not OUT_LINE.fullmatch(line):
            raise Case(f"unexpected stdout line {line[:200]!r}")
    if os.path.exists(os.path.join(tmp, "log")):
        raise Case("wrote a file without --track (-a)")


def case_full(rng, repro, dk):
    """stdout on /dev/full: exit 2 with a write error, never silence."""
    env = {"TZ": "UTC0", "DAYKEEP_NOW": "14301.4564"}
    for args in (["--now"], [".3", "-", ".4"], ["--convert", ".5"], ["-s", "1:00-2:00"]):
        with open("/dev/full", "wb") as full:
            p = subprocess.run([dk, *args], env={**BASE_ENV, **env}, stdout=full,
                               stderr=subprocess.PIPE, timeout=TIMEOUT)
        repro["argv"] = args
        if p.returncode != 2 or b"write error" not in p.stderr:
            raise Case(f"exit {p.returncode}, stderr {p.stderr!r}")


# ------------------------------------------------------------ tracker
KEYS = (["\n"] * 6 + ["\r", "x", "x", "e", "e", "e", "s", "Z"]
        + list("0123456789.:+") * 2 + ["\x15", "\x7f", "\b", "\x1b", "\x03"])
LOG_LINE = re.compile(rb"[0-9]{5,}\.[0-9]{4} - [0-9]{5,}\.[0-9]{4}")
OPEN_LINE = re.compile(rb"[0-9]{5,}\.[0-9]{4} -")


def rand_keys(rng):
    keys, editing = [], False
    for _ in range(rng.randrange(1, 80)):
        k = rng.choice(KEYS)
        if k == "e" and not editing:
            editing = True
            keys.append("e")
            if rng.random() < 0.7:
                keys.append("\x15")                      # clear the prefill
            continue
        if editing and k in "\n\r\x1b\x03":
            editing = False
        elif not editing and k == "\x03":
            break                                        # ^C quits
        keys.append(k)
    if rng.random() < 0.5:
        keys.append("q")
    return "".join(keys).encode()


def case_track(rng, repro, dk, tmp):
    choose_length(rng)
    now = 14301 * S.CIVIL_DAY + rng.randrange(S.CIVIL_DAY)
    today = now // DAY
    log = os.path.join(tmp, "track.log")
    old = b""
    if rng.random() < 0.3:                               # earlier sessions
        old = f"{today - 1:05d}.1000 - {today - 1:05d}.2000\n".encode()
        old += rng.choice([b"", f"{today:05d}.0000 -\n".encode(),
                           f"{today:05d}.0000 -".encode(),
                           f"{today - 1:05d}.3000 - {today - 1:05d}.4000".encode()])
    with open(log, "wb") as f:
        f.write(old)
    tz, off, zargs = rand_zone(rng)
    fmt = rng.choice(list(FMT))
    env = now_env(now, tz)
    keys = rand_keys(rng)
    args = [dk, "--track", "-a", log, f"--format={fmt}", *zargs]
    repro.update(argv=args, env=env, keys=keys, old_log=old)
    rc, out, err = run(args, keys, env)
    if rc != 0:
        raise Case(f"tracker exit {rc}, stderr {err!r}")
    with open(log, "rb") as f:
        content = f.read()
    repro["log"] = content
    lines = content.splitlines()
    kept = old.splitlines()
    if kept and OPEN_LINE.fullmatch(kept[-1]):
        kept = kept[:-1]                                  # resumed by this session
    if lines[:len(kept)] != kept:
        raise Case("the tracker changed lines of earlier sessions")
    for i, line in enumerate(lines):
        last = i == len(lines) - 1
        if not (LOG_LINE.fullmatch(line) or (last and OPEN_LINE.fullmatch(line))):
            raise Case(f"bad log line {line!r}")
    closed = b"".join(l + b"\n" for l in lines if LOG_LINE.fullmatch(l))
    path = os.path.join(tmp, "closed.log")
    with open(path, "wb") as f:
        f.write(closed)
    rc, _, serr = run([dk, "-s", "-f", path], b"", env)
    if rc != 0:
        raise Case(f"the log does not read back: exit {rc}, {serr!r}")
    # the session's lines are exactly the entries on the tracker's last screen
    screens = out.split(b"\n[enter]")
    last = screens[-2] if len(screens) > 1 else b""
    shown = [m.group(1) + b" - " + (m.group(2) if m.group(2) != b"..." else b"")
             for m in map(SCREEN_ENTRY.fullmatch, last.split(b"\n")) if m]
    shown = [e.rstrip() if e.endswith(b"- ") else e for e in shown]
    if lines[len(kept):] != shown:
        raise Case(f"log has {lines[len(kept):]!r}, screen shows {shown!r}")


SCREEN_ENTRY = re.compile(rb" *[0-9]+  ([0-9.]+) - ([0-9.]+|\.\.\.) .*")


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--daykeep", default=os.path.join(ROOT, "daykeep"))
    a = ap.parse_args()
    dk = os.path.abspath(a.daykeep)

    print(f"fuzzing (seed {SEED}, {ITERS} cases per group, daykeep = {dk})")
    rng = random.Random(SEED)
    with tempfile.TemporaryDirectory() as tmp:
        group("daykeep sum mode == model",
              lambda r, p: case_sum(r, p, dk, tmp), rng, ITERS)
        group("daykeep --convert == model (fixed zones)",
              lambda r, p: case_convert_model(r, p, dk), rng, ITERS)
        group("daykeep --convert round trip == date(1) (real zones)",
              lambda r, p: case_convert_roundtrip(r, p, dk), rng, max(1, ITERS // 10))
        group("daykeep garbage: no crash, status 0-2, clean stdout",
              lambda r, p: case_garbage(r, p, dk, tmp), rng, ITERS)
        group("daykeep write errors (/dev/full)",
              lambda r, p: case_full(r, p, dk), rng, 1)
        group("daykeep --track: log reads back and matches the screen",
              lambda r, p: case_track(r, p, dk, tmp), rng, ITERS)
    print("all groups passed" if not failures else f"{failures} group(s) failed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
