"""Functional specification for timekeep, as Z3 bit-vector formulas plus plain
Python mirrors.  The Z3 forms are what the proofs check the binary against; the
Python forms drive differential fuzzing.  Everything here is the *trusted*
description of intended behaviour -- keep it small and obvious."""

import z3

BV32 = lambda n: z3.BitVecVal(n, 32)


# --- decimal digits ------------------------------------------------------
def digits_py(v):
    """ascii of v (unsigned) in base 10, no leading zeros (v=0 -> '0')."""
    return str(v & 0xFFFFFFFF)


# --- "<h>h <m>m" ---------------------------------------------------------
def fmt_hm_py(t):
    t &= 0xFFFFFFFF
    return f"{t // 60}h {t % 60}m"


# --- HH:MM (zero-padded, 2-digit) ---------------------------------------
def hm_py(m):
    m &= 0xFFFF
    return f"{m // 60:02d}:{m % 60:02d}"


# --- entry duration with midnight wrap ----------------------------------
def duration_py(start, end):
    d = (end - start) % 1440
    return d


def duration_z3(start, end):
    """start,end : BitVec32 assumed in [0,1440).  Result in [0,1440)."""
    d = end - start
    return z3.If(z3.Or(d >= 1440, d < 0), d + 1440, d)  # single wrap suffices for the domain


# --- local minute-of-day -------------------------------------------------
def minute_of_day_py(secs):
    s = secs % 86400
    return s // 60


def minute_of_day_z3(secs):
    """secs : BitVec64 (signed local seconds).  Result BitVec64 in [0,1440)."""
    s = z3.SRem(secs, z3.BitVecVal(86400, 64))
    s = z3.If(s < 0, s + 86400, s)
    return z3.UDiv(s, z3.BitVecVal(60, 64))


# --- HH:MM validator (matches ^([01]?[0-9]|2[0-3]):([0-5][0-9])$) --------
def parse_hm_py(s):
    """s : bytes/str of the typed characters.  Returns minute-of-day or -1."""
    b = s if isinstance(s, (bytes, bytearray)) else s.encode()
    n = len(b)
    if n < 4 or n > 5:
        return -1
    hd = 1 if b[1:2] == b":" else 2
    if n != hd + 3 or b[hd] != ord(":"):
        return -1
    hh = 0
    for i in range(hd):
        c = b[i]
        if c < ord("0") or c > ord("9"):
            return -1
        hh = hh * 10 + (c - ord("0"))
    if hh > 23:
        return -1
    c1, c2 = b[hd + 1], b[hd + 2]
    if c1 < ord("0") or c1 > ord("5"):
        return -1
    if c2 < ord("0") or c2 > ord("9"):
        return -1
    return hh * 60 + (c1 - ord("0")) * 10 + (c2 - ord("0"))


# --- streaming range parser (sum mode) ----------------------------------
WEIGHTS = 0x013CFFC4  # signed bytes -60,-1,60,1


class Parser:
    """One-byte-at-a-time parser; total() is the sequence of printed totals."""

    def __init__(self):
        self.reset_line()
        self.total = 0
        self.out = []

    def reset_line(self):
        self.weights = WEIGHTS
        self.cur = 0
        self.line = 0
        self.indig = False

    def byte(self, c):
        c &= 0xFF
        if ord("0") <= c <= ord("9"):
            self.cur = (self.cur * 10 + (c - ord("0"))) & 0xFFFFFFFF
            self.indig = True
            return
        if self.indig:
            w = self.weights & 0xFF
            w = w - 256 if w >= 128 else w          # signed low byte
            self.line = (self.line + w * self.cur) & 0xFFFFFFFF
            self.weights = (self.weights & 0xFFFFFFFF) >> 8
            self.cur = 0
            self.indig = False
            if self.weights == 0:
                sline = self.line if self.line < 2**31 else self.line - 2**32
                d = sline % 1440
                self.total = (self.total + d) & 0xFFFFFFFF
                self.out.append(fmt_hm_py(self.total))
                self.reset_line()
                return
        if c == 0x0A:
            self.reset_line()


def sum_mode_py(args):
    """args: list of argv strings (without argv[0]).  Returns printed lines."""
    p = Parser()
    for a in args:
        for ch in a.encode():
            p.byte(ch)
        p.byte(ord(" "))       # the NUL between arguments separates
    return p.out


# --- TZif v1 offset lookup ----------------------------------------------
def be32(b, o):
    return (b[o] << 24) | (b[o + 1] << 16) | (b[o + 2] << 8) | b[o + 3]


def tz_offset_py(tz, now):
    n = len(tz)
    if n < 44 or tz[0:4] != b"TZif":
        return 0
    timecnt = be32(tz, 32)
    typecnt = be32(tz, 36)
    if typecnt == 0:
        return 0
    base = 44 + 5 * timecnt + 6 * typecnt
    if base > n:
        return 0
    idx = 0
    for i in range(timecnt):
        t = be32(tz, 44 + 4 * i)
        if t >= 2**31:
            t -= 2**32
        if t <= now:
            idx = tz[44 + 4 * timecnt + i]
        else:
            break
    if idx >= typecnt:
        return 0
    off = be32(tz, 44 + 5 * timecnt + 6 * idx)
    return off - 2**32 if off >= 2**31 else off
