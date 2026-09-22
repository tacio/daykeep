"""Functional specification of daykeep's decimal time (src/dktime.c), as Z3
predicates over unbounded integers plus plain Python mirrors.  The Z3 forms
state what the completion and rounding rules *mean*; dkprove.py checks a model
of the C code against them, and checks the compiled C against the mirrors.
Everything here is trusted -- keep it small and obvious.

Values are integer seconds since day 0 = 1987-07-28 00:00 UTC.  A typed stamp
is (n, typed, frac): n day digits whose value is typed (0 <= typed < 10**n),
and a fraction of a day already converted to seconds (0 <= frac <= DAY)."""

import re

import z3

DAY = 86400          # seconds per day
TICKS = 10000        # output steps per day (4 decimal places, 8.64 s each)
MAX_DIGITS = 9       # longest day number / fraction dktime reads


# --- the rules, as Z3 predicates (Int arithmetic: / and % floor) ---------
def ends_in(d, typed, n):
    """day d's decimal number ends in the n digits typed (always, if n == 0)."""
    return d % 10**n == typed


def is_full(n, ref):
    """n typed digits are a full day number: at least as many digits as ref."""
    return z3.And(n > 0, z3.Or(ref < 0, ref < 10**n))


def complete_ok(r, typed, n, ref, forward, other):
    """r is the day completed from n typed digits relative to day ref:
    ref itself if nothing was typed, the typed number if it is full, else the
    most recent day <= ref ending in them (the first >= ref if forward).
    OTHER is a free variable standing for every other day."""
    if n == 0:
        return r == ref
    if forward:
        best = z3.And(ends_in(r, typed, n), r >= ref,
                      z3.Implies(z3.And(ends_in(other, typed, n), other >= ref),
                                 other >= r))
    else:
        best = z3.And(ends_in(r, typed, n), r <= ref,
                      z3.Implies(z3.And(ends_in(other, typed, n), other <= ref),
                                 other <= r))
    return z3.If(is_full(n, ref), r == typed, best)


def end_ok(out, err, typed, n, frac, start, other):
    """out is the END of a range starting at START.  A full day number is
    literal, and an error if it falls before START.  Otherwise out is on the
    first day >= START's day ending in the typed digits whose time-of-day FRAC
    is not before START.  OTHER stands for every other day."""
    ref = start / DAY
    lit = typed * DAY + frac
    d = (out - frac) / DAY
    first = z3.And(
        z3.Not(err), (out - frac) % DAY == 0,
        ends_in(d, typed, n), d >= ref, out >= start,
        z3.Implies(z3.And(ends_in(other, typed, n), other >= ref,
                          other * DAY + frac >= start), other >= d))
    return z3.If(is_full(n, ref),
                 z3.And(err == (lit < start), z3.Implies(z3.Not(err), out == lit)),
                 first)


def end_earliest(out, typed, n, frac, start, other):
    """For frac < DAY, no matching instant between START and out."""
    return z3.Implies(z3.And(ends_in(other, typed, n), other * DAY + frac >= start),
                      other * DAY + frac >= out)


def ticks(s):
    """seconds -> steps of 1/10000 day, rounded half-up."""
    return (s * TICKS + DAY // 2) / DAY


def frac_of(num, k):
    """k typed fraction digits with value num -> seconds, rounded half-up."""
    return (num * DAY + 10**k // 2) / 10**k


# --- Python mirrors (for the checks against the compiled C) --------------
def ticks_py(s):
    return (s * TICKS + DAY // 2) // DAY


def fmt_stamp_py(s):
    t = ticks_py(s)
    d, f = divmod(t, TICKS)
    return f"{d:05d}.{f:04d}" if d >= 0 else f"-{-d:04d}.{f:04d}"


def fmt_dur_py(d):
    sg, d = ("-", -d) if d < 0 else ("", d)
    t = ticks_py(d)
    return f"{sg}.{t:04d}" if t < TICKS else f"{sg}{t // TICKS}.{t % TICKS:04d}"


def fmt_minutes_py(d):
    sg, d = ("-", -d) if d < 0 else ("", d)
    return f"{sg}{(d + 30) // 60}"


def fmt_hm_py(d):
    sg, d = ("-", -d) if d < 0 else ("", d)
    m = (d + 30) // 60
    return f"{sg}{m // 60}h {m % 60}m"


DECIMAL = re.compile(r"(\+?)([0-9]*)(?:\.([0-9]*))?")
CLOCK = re.compile(r"([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?")


def parse_decimal_py(s):
    """-> (plus, n, typed, frac), or None."""
    m = DECIMAL.fullmatch(s)
    if not m or len(m.group(2)) > MAX_DIGITS or not (m.group(2) or m.group(3)):
        return None
    day, fr = m.group(2), (m.group(3) or "")[:MAX_DIGITS]
    frac = (int(fr) * DAY + 10**len(fr) // 2) // 10**len(fr) if fr else 0
    return bool(m.group(1)), len(day), int(day or 0), frac


def parse_clock_py(s):
    """-> seconds into the day, or None."""
    m = CLOCK.fullmatch(s)
    if not m:
        return None
    h, mi, sec = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    return None if h > 23 or mi > 59 or sec > 59 else h * 3600 + mi * 60 + sec


def complete_py(typed, n, ref, forward):
    """By the definition: search from ref (up to 10**n steps)."""
    if n == 0:
        return ref
    if ref < 0 or ref < 10**n:
        return typed
    step = 1 if forward else -1
    d = ref
    while d % 10**n != typed:
        d += step
    return d


def parse_stamp_py(s, now):
    """UTC: HH:MM is on now's day.  -> seconds, or None."""
    c = parse_clock_py(s)
    if c is not None:
        return now // DAY * DAY + c
    p = parse_decimal_py(s)
    if p is None:
        return None
    plus, n, typed, frac = p
    return complete_py(typed, n, now // DAY, plus) * DAY + frac


def parse_end_py(s, start):
    """UTC.  -> seconds, None if not a stamp, or 'before' for a full day
    number before START.  Searches up to 2 * 10**n days."""
    c = parse_clock_py(s)
    if c is not None:
        n, typed, frac = 0, 0, c
    else:
        p = parse_decimal_py(s)
        if p is None:
            return None
        _, n, typed, frac = p
    ref = start // DAY
    if n > 0 and (ref < 0 or ref < 10**n):
        out = typed * DAY + frac
        return "before" if out < start else out
    # the first day >= ref ending in the digits, whose frac is not before start
    d = ref
    while d % 10**n != typed or d * DAY + frac < start:
        d += 1
    return d * DAY + frac
