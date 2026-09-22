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


# --- entry duration with midnight wrap ----------------------------------
def duration_py(start, end):
    d = (end - start) % 1440
    return d


def duration_z3(start, end):
    """start,end : BitVec32 assumed in [0,1440).  Result in [0,1440)."""
    d = end - start
    return z3.If(z3.Or(d >= 1440, d < 0), d + 1440, d)  # single wrap suffices for the domain


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
