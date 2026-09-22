#!/usr/bin/env python3
"""Verification driver for daykeep's decimal time (src/dktime.c).

Prints PROVED / FAILED for each obligation and exits non-zero if any fails.
Two kinds of check:

  * Z3 theorems, for every input in the stated domain: a line-by-line model
    of the C arithmetic (C's truncating / and %, dk_floor_div, the ndigits
    loop) meets the rules in verify/dkspec.py, and no intermediate overflows
    64 bits.
  * Property checks of the compiled C, through ctypes: it agrees with the
    model on seeded random and exhaustive inputs, so the model is the code.

See verify/README.md for the trusted base and the exact scope of each claim.
"""

import ctypes
import os
import random
import subprocess
import sys
import tempfile

import z3

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import dkspec as S  # noqa: E402

DAY, TICKS = S.DAY, S.TICKS
LIMIT = 2**49                  # |seconds| domain: about 17.8 million years
ITERS = int(os.environ.get("DK_VERIFY_ITERS", "200000"))
SEED = int(os.environ.get("DK_VERIFY_SEED", "20260922"))

results = []
def record(name, ok, detail=""):
    results.append((name, ok, detail))
    tag = "PROVED" if ok else "FAILED"
    line = f"  [{tag}] {name}"
    if detail and not ok:
        line += f"\n           {detail}"
    print(line)


def prove(formula):
    """(True, "") if formula is valid, else (False, counterexample)."""
    s = z3.Solver()
    s.set("timeout", 120_000)
    s.add(z3.Not(formula))
    r = s.check()
    if r == z3.unsat:
        return True, ""
    if r == z3.sat:
        return False, str(s.model())
    return False, f"solver: {r}"


# ------------------------------------------------ model of the C (dktime.c)
class C:
    """Builds Z3 terms for C int64 arithmetic, collecting a no-overflow side
    condition for every intermediate."""

    def __init__(self):
        self.ov = []

    def v(self, x):
        self.ov.append(z3.And(x >= -2**63, x < 2**63))
        return x

    def div(self, a, b):                 # C '/', b > 0 constant
        return self.v(z3.If(a >= 0, a / b, -((-a) / b)))

    def rem(self, a, b):                 # C '%', b > 0 constant
        return self.v(a - self.div(a, b) * b)

    def floor_div(self, a, b):           # dk_floor_div
        q = self.div(a, b)
        return z3.If(z3.And(self.rem(a, b) != 0, (a < 0) != (b < 0)), q - 1, q)

    def floor_mod(self, a, b):
        return self.v(a - self.v(self.floor_div(a, b) * b))

    @staticmethod
    def ndigits(v):                      # the while loop, unrolled (v < 2**63)
        return 1 + z3.Sum([z3.If(v >= 10**k, 1, 0) for k in range(1, 19)])

    def complete_day(self, typed, n, ref, forward):
        if n == 0:
            return ref
        full = z3.Or(ref < 0, n >= self.ndigits(ref))
        m = 10**n
        if forward:
            r = self.v(ref + self.floor_mod(self.v(typed - ref), m))
        else:
            r = self.v(ref - self.floor_mod(self.v(ref - typed), m))
        return z3.If(full, typed, r)

    def parse_end(self, typed, n, frac, start):
        """-> (out, err) of dk_parse_end's decimal branch; err is the -2."""
        ref = self.floor_div(start, DAY)
        literal = z3.And(n > 0, z3.Or(ref < 0, n >= self.ndigits(ref)))
        out1 = self.v(self.v(self.complete_day(typed, n, ref, 1) * DAY) + frac)
        out2 = self.v(self.v(self.complete_day(typed, n, self.v(ref + 1), 1) * DAY) + frac)
        early = out1 < start
        return z3.If(early, out2, out1), z3.And(early, literal)

    def ticks(self, s):
        return self.floor_div(self.v(self.v(s * TICKS) + DAY // 2), DAY)

    def frac(self, num, k):              # parse_decimal: num, den = 10**k
        den = 10**k
        return self.div(self.v(self.v(num * DAY) + den // 2), den)


def within(x, lo, hi):
    return z3.And(x >= lo, x < hi)


# ----------------------------------------------------------- Z3 theorems
def prove_complete(forward):
    fails = []
    for n in range(S.MAX_DIGITS + 1):
        c = C()
        typed, ref, other = z3.Ints("typed ref other")
        dom = z3.And(within(typed, 0, 10**n), within(ref, -LIMIT // DAY, LIMIT // DAY))
        r = c.complete_day(typed, n, ref, forward)
        ok, why = prove(z3.Implies(dom, z3.And(
            S.complete_ok(r, typed, n, ref, forward, other), *c.ov)))
        if not ok:
            fails.append(f"n={n}: {why}")
    what = "first day >= ref" if forward else "most recent day <= ref"
    record(f"dk_complete_day ({'+' if forward else 'no +'}): ends in the typed "
           f"digits and is the {what}; full numbers literal; n = 0..9",
           not fails, "; ".join(fails))


def prove_end():
    fails, fails_early = [], []
    for n in range(S.MAX_DIGITS + 1):
        c = C()
        typed, frac, start, other = z3.Ints("typed frac start other")
        dom = z3.And(within(typed, 0, 10**n), within(frac, 0, DAY + 1),
                     within(start, -LIMIT, LIMIT))
        out, err = c.parse_end(typed, n, frac, start)
        ok, why = prove(z3.Implies(dom, z3.And(
            S.end_ok(out, err, typed, n, frac, start, other), *c.ov)))
        if not ok:
            fails.append(f"n={n}: {why}")
        ok, why = prove(z3.Implies(z3.And(dom, frac < DAY, z3.Not(S.is_full(n, start / DAY))),
                                   S.end_earliest(out, typed, n, frac, start, other)))
        if not ok:
            fails_early.append(f"n={n}: {why}")
    record("dk_parse_end: first match on or after START's day, rolling over; "
           "full numbers literal, -2 iff before START; n = 0..9", not fails,
           "; ".join(fails))
    record("dk_parse_end: with frac < 1 day, no matching instant lies between "
           "START and the result", not fails_early, "; ".join(fails_early))


def prove_padding():
    # ".5" == ".5000": k typed fraction digits land on the tick they name
    fails = []
    for k in range(1, 5):
        c = C()
        num = z3.Int("num")
        ok, why = prove(z3.Implies(within(num, 0, 10**k), z3.And(
            c.ticks(c.frac(num, k)) == num * 10**(4 - k), *c.ov)))
        if not ok:
            fails.append(f"k={k}: {why}")
    for k in range(1, S.MAX_DIGITS + 1):    # and every fraction stays in a day
        c = C()
        num = z3.Int("num")
        f = c.frac(num, k)
        ok, why = prove(z3.Implies(within(num, 0, 10**k), z3.And(
            f == S.frac_of(num, k), f >= 0, f <= DAY, *c.ov)))
        if not ok:
            fails.append(f"k={k} range: {why}")
    record("fraction digits: right digits omitted count as zeros (k = 1..4); "
           "any 1..9 digits give 0 <= frac <= 1 day", not fails, "; ".join(fails))


def prove_rounding():
    c = C()
    s, s2 = z3.Ints("s s2")
    dom = within(s, -LIMIT, LIMIT)
    t = c.ticks(s)
    ok1, why1 = prove(z3.Implies(dom, z3.And(t == S.ticks(s), *c.ov)))
    c2 = C()
    ok2, why2 = prove(z3.Implies(
        z3.And(dom, within(s2, -LIMIT, LIMIT), s <= s2),
        z3.And(c2.ticks(s) <= c2.ticks(s2),
               c2.ticks(s + DAY) == c2.ticks(s) + TICKS)))
    record("ticks: half-up rounding to 1/10000 day, no overflow for "
           "|s| < 2^49", ok1, why1)
    record("ticks: monotone, and a day later is exactly 10000 ticks later",
           ok2, why2)


def prove_roundtrip():
    # dk_fmt_stamp prints day d = t div 10000 and f = t mod 10000 as d.ffff;
    # dk_parse_literal reads that back as d * DAY + frac(f, 4)
    c = C()
    s = z3.Int("s")
    t = c.ticks(s)
    d, f = c.floor_div(t, TICKS), c.floor_mod(t, TICKS)
    back = c.v(c.v(d * DAY) + c.frac(f, 4))
    dom = z3.And(within(s, -LIMIT, LIMIT), d >= 0, d < 10**S.MAX_DIGITS)
    ok1, why1 = prove(z3.Implies(dom, z3.And(c.ticks(back) == t, *c.ov)))
    ok2, why2 = prove(z3.Implies(dom, z3.And(back - s <= 4, s - back <= 4)))
    record("stamp round-trip: parse_literal(fmt_stamp(s)) prints the same, "
           "for day numbers 0..999999999", ok1, why1)
    record("stamp round-trip: the value read back is within 4 s of s",
           ok2, why2)


# ---------------------------------------------- the compiled C, via ctypes
def load_lib(tmp):
    so = os.path.join(tmp, "libdktime.so")
    subprocess.run([os.environ.get("CC", "cc"), "-std=c11", "-D_DEFAULT_SOURCE",
                    "-O2", "-shared", "-fPIC", "-o", so,
                    os.path.join(ROOT, "src", "dktime.c")], check=True)
    lib = ctypes.CDLL(so)
    ll, i, p = ctypes.c_longlong, ctypes.c_int, ctypes.c_char_p
    lib.dk_complete_day.argtypes = [ll, i, ll, i]
    lib.dk_complete_day.restype = ll
    for fn in (lib.dk_parse_stamp,):
        fn.argtypes = [p, ll, i, ctypes.POINTER(ll)]
    lib.dk_parse_end.argtypes = [p, ll, i, ctypes.POINTER(ll)]
    lib.dk_parse_literal.argtypes = [p, ctypes.POINTER(ll)]
    for f in ("stamp", "dur", "hm", "minutes"):
        getattr(lib, f"dk_fmt_{f}").argtypes = [p, ll]
    return lib


class Lib:
    def __init__(self, lib):
        self.lib = lib
        self.buf = ctypes.create_string_buffer(64)
        self.out = ctypes.c_longlong()

    def fmt(self, kind, s):
        getattr(self.lib, f"dk_fmt_{kind}")(self.buf, s)
        return self.buf.value.decode()

    def parse_stamp(self, txt, now):
        r = self.lib.dk_parse_stamp(txt.encode(), now, 1, ctypes.byref(self.out))
        return self.out.value if r == 0 else None

    def parse_end(self, txt, start):
        r = self.lib.dk_parse_end(txt.encode(), start, 1, ctypes.byref(self.out))
        return {0: self.out.value, -1: None, -2: "before"}[r]

    def parse_literal(self, txt):
        r = self.lib.dk_parse_literal(txt.encode(), ctypes.byref(self.out))
        return self.out.value if r == 0 else None


def rand_secs(rng):
    """Mostly near today, sometimes anywhere in the domain."""
    kind = rng.random()
    if kind < 0.6:
        return rng.randrange(14000 * DAY, 15000 * DAY)
    if kind < 0.8:
        return rng.randrange(-2 * DAY, 20 * DAY)          # near day 0
    if kind < 0.9:                                        # at a power of ten
        return 10**rng.randrange(1, 9) * DAY + rng.randrange(-2 * DAY, 2 * DAY)
    return rng.randrange(-LIMIT, LIMIT)


def rand_stamp(rng, ref):
    """A stamp string: digits typed as for completion, full, or odd."""
    full = len(str(abs(ref)))
    kind = rng.random()
    if kind < 0.1:
        return f"{rng.randrange(24)}:{rng.randrange(60):02d}" + \
            (f":{rng.randrange(60):02d}" if rng.random() < 0.3 else "")
    if kind < 0.15:
        return "".join(rng.choice("0123456789+.:- x") for _ in range(rng.randrange(6)))
    if kind < 0.85 or full > S.MAX_DIGITS:      # completed (the mirror searches)
        n = rng.randrange(0, min(full, 5))
    else:                                       # full, or too long to be read
        n = rng.randrange(full, S.MAX_DIGITS + 2)
    day = "".join(rng.choice("0123456789") for _ in range(n))
    frac = "".join(rng.choice("0123456789") for _ in range(rng.randrange(0, 12)))
    s = ("+" if rng.random() < 0.3 else "") + day
    if frac or rng.random() < 0.5:
        s += "." + frac
    return s


def check_complete(lib, rng):
    bad = []
    for _ in range(ITERS):
        ref = rand_secs(rng) // DAY
        n = rng.randrange(0, 5)
        typed = rng.randrange(10**n)
        fwd = rng.randrange(2)
        got = lib.lib.dk_complete_day(typed, n, ref, fwd)
        if got != S.complete_py(typed, n, ref, fwd):
            bad.append((typed, n, ref, fwd, got))
            break
    record(f"C dk_complete_day == definition ({ITERS} seeded cases, n = 0..4)",
           not bad, f"typed,n,ref,fwd,got = {bad[:1]}")


def check_parse(lib, rng):
    bad_s, bad_e = [], []
    for _ in range(ITERS):
        now = rand_secs(rng)
        txt = rand_stamp(rng, now // DAY)
        if lib.parse_stamp(txt, now) != S.parse_stamp_py(txt, now):
            bad_s.append((txt, now))
        start = rand_secs(rng)
        txt = rand_stamp(rng, start // DAY)
        if lib.parse_end(txt, start) != S.parse_end_py(txt, start):
            bad_e.append((txt, start))
        if bad_s and bad_e:
            break
    record(f"C dk_parse_stamp == spec, UTC ({ITERS} seeded strings)",
           not bad_s, f"text,now = {bad_s[:1]}")
    record(f"C dk_parse_end == spec, UTC ({ITERS} seeded strings)",
           not bad_e, f"text,start = {bad_e[:1]}")


def check_formats(lib, rng):
    bad = []
    # every second of day -1, day 0 and today, then random ones
    cases = [*range(-DAY, DAY), *range(14301 * DAY, 14302 * DAY)]
    cases += [rand_secs(rng) for _ in range(ITERS)]
    for s in cases:
        st = lib.fmt("stamp", s)
        if st != S.fmt_stamp_py(s):
            bad.append(("stamp", s, st))
        elif 0 <= S.ticks_py(s) < 10**S.MAX_DIGITS * TICKS:
            back = lib.parse_literal(st)
            if back is None or lib.fmt("stamp", back) != st or abs(back - s) > 4:
                bad.append(("round-trip", s, st))
        for kind, py in (("dur", S.fmt_dur_py), ("hm", S.fmt_hm_py),
                         ("minutes", S.fmt_minutes_py)):
            if lib.fmt(kind, s) != py(s):
                bad.append((kind, s, lib.fmt(kind, s)))
        if bad:
            break
    record(f"C formatters == spec, and stamps round-trip ({len(cases)} values, "
           "3 days exhaustive)", not bad, f"{bad[:1]}")


if __name__ == "__main__":
    print("daykeep decimal-time verification\n" + "=" * 40)
    print("\n-- Z3 theorems over a model of dktime.c --")
    prove_complete(False)
    prove_complete(True)
    prove_end()
    prove_padding()
    prove_rounding()
    prove_roundtrip()

    print(f"\n-- the compiled dktime.c against the spec (seed {SEED}) --")
    with tempfile.TemporaryDirectory() as tmp:
        lib = Lib(load_lib(tmp))
        rng = random.Random(SEED)
        check_complete(lib, rng)
        check_parse(lib, rng)
        check_formats(lib, rng)

    print("\n" + "=" * 40)
    n_ok = sum(1 for _, ok, _ in results if ok)
    print(f"{n_ok}/{len(results)} obligations proved")
    sys.exit(0 if n_ok == len(results) else 1)
