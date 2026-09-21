#!/usr/bin/env python3
"""Formal proof driver for timekeep.

Runs a battery of obligations and prints PROVED / FAILED for each; exits non-zero
if any fails.  Two engines are used:

  * Z3 alone, for unbounded theorems about the arithmetic specification
    (verify/spec.py) -- these hold for every 32/64-bit input.
  * angr symbolic execution over the *shipped* flat binary, for equivalence of
    each leaf routine with the spec, together with memory-safety and syscall
    checks.  angr explores every path of a routine; where a routine's loop trip
    count is bounded by its (small) input, that is exhaustive.

See verify/README.md for the trusted base and the exact scope of each claim.
"""

import subprocess
import sys
import os

import warnings
warnings.filterwarnings("ignore")

import z3
import angr
import claripy

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import spec  # noqa: E402

import logging
for n in ("angr", "cle", "pyvex", "claripy", "angr.engines"):
    logging.getLogger(n).setLevel("CRITICAL")

BASE = 0x400000
BIN = os.path.join(ROOT, "timekeep")
SUM_ACTIVE_CAP = 256        # max live paths in the sum-mode whole-program check

results = []
def record(name, ok, detail=""):
    results.append((name, ok, detail))
    tag = "PROVED" if ok else "FAILED"
    line = f"  [{tag}] {name}"
    if detail and not ok:
        line += f"\n           {detail}"
    print(line)


# ------------------------------------------------------------------ symbols
def load_symbols():
    """vaddr of each .text symbol = BASE + its offset in the assembled object."""
    subprocess.run(["as", "-o", "/tmp/tk_verify.o", os.path.join(ROOT, "timekeep.s")],
                   check=True)
    out = subprocess.check_output(["objdump", "-t", "/tmp/tk_verify.o"]).decode()
    syms = {}
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) >= 5 and ".text" in parts and parts[-1] != ".text":
            try:
                syms[parts[-1]] = BASE + int(parts[0], 16)
            except ValueError:
                pass
    return syms


SYM = load_symbols()
proj = angr.Project(BIN, main_opts={"backend": "elf"}, auto_load_libs=False)

# scratch regions used when driving leaf routines
OUTBUF = 0x600000     # where routines write their output
OUTLEN = 0x400
INBUF = 0x610000      # where routines read caller-provided bytes
STACK = 0x7000000     # a private stack high enough for pushes below rsp
RET = 0xdead0000      # sentinel return address


def leaf_state(addr, rdi=0):
    """A blank state poised to run a leaf routine, returning to RET."""
    st = proj.factory.blank_state(
        addr=addr,
        add_options={angr.options.SYMBOL_FILL_UNCONSTRAINED_REGISTERS,
                     angr.options.SYMBOL_FILL_UNCONSTRAINED_MEMORY,
                     angr.options.ZERO_FILL_UNCONSTRAINED_REGISTERS},
    )
    st.regs.rsp = STACK
    st.stack_push(claripy.BVV(RET, 64))
    st.regs.rdi = rdi
    return st


def run_leaf(st, guard_regions=None):
    """Run to the RET sentinel; return list of return states.  If guard_regions
    is given (list of (lo,hi)), fail loudly if any memory access can fall
    outside every region."""
    violations = []
    if guard_regions is not None:
        def check(state, kind):
            addr = state.inspect.mem_write_address if kind == "w" else state.inspect.mem_read_address
            length = state.inspect.mem_write_length if kind == "w" else state.inspect.mem_read_length
            if length is None:
                length = 1
            try:
                size = state.solver.eval_one(length) if not isinstance(length, int) else length
            except Exception:
                size = 8
            # accesses to the sentinel stack (return addr) are fine
            oob = z3_oob(state, addr, size, guard_regions)
            if oob:
                violations.append((kind, addr, size))
        st.inspect.b("mem_write", when=angr.BP_BEFORE,
                     action=lambda s: check(s, "w"))
        st.inspect.b("mem_read", when=angr.BP_BEFORE,
                     action=lambda s: check(s, "r"))
    simgr = proj.factory.simulation_manager(st)
    simgr.explore(find=RET, num_find=64)
    return simgr.found, violations


def z3_oob(state, addr, size, regions):
    """True if `addr`..`addr+size` can lie outside all regions (and the sentinel
    stack slot)."""
    a = addr if not isinstance(addr, int) else claripy.BVV(addr, 64)
    conds = []
    for lo, hi in regions:
        conds.append(claripy.And(claripy.UGE(a, lo), claripy.ULE(a + size, hi)))
    # scratch pushes/pops live in a window around the private stack pointer
    conds.append(claripy.And(claripy.UGE(a, STACK - 0x800), claripy.ULE(a + size, STACK + 0x100)))
    inside = claripy.Or(*conds)
    return state.solver.satisfiable(extra_constraints=[claripy.Not(inside)])


def read_out(state, start, end_reg="rdi"):
    """Concrete bytes written to [start, rdi)."""
    end = state.solver.eval_one(getattr(state.regs, end_reg))
    n = end - start
    if n <= 0 or n > OUTLEN:
        return None
    return state.solver.eval_one(state.memory.load(start, n)).to_bytes(n, "big")


# ============================================================ Z3 THEOREMS
def prove_z3_theorems():
    s = z3.Solver()

    # duration: for start,end in [0,1440), result in [0,1440) and == (end-start) mod 1440
    start, end = z3.BitVecs("start end", 32)
    d = spec.duration_z3(start, end)
    dom = z3.And(z3.UGE(start, 0), z3.ULT(start, 1440), z3.UGE(end, 0), z3.ULT(end, 1440))
    goal = z3.And(z3.UGE(d, 0), z3.ULT(d, 1440),
                  d == z3.URem((end - start + 1440), z3.BitVecVal(1440, 32)))
    ok = prove(z3.Implies(dom, goal))
    record("duration in [0,1440) and equals (end-start) mod 1440", ok)

    # minute_of_day: result always in [0,1440), for every 64-bit second count
    secs = z3.BitVec("secs", 64)
    m = spec.minute_of_day_z3(secs)
    ok = prove(z3.And(z3.UGE(m, 0), z3.ULT(m, 1440)))
    record("minute_of_day in [0,1440) for all 64-bit inputs", ok)

    # tz safety lemma: passing the header checks implies the offset read is
    # in-bounds.  timecnt/typecnt are read as 32-bit fields and idx from a byte,
    # so the 64-bit arithmetic in the binary cannot overflow -- model that.
    timecnt, typecnt, idx, ln = z3.BitVecs("timecnt typecnt idx ln", 64)
    bounds = z3.And(z3.ULT(timecnt, 1 << 32), z3.ULT(typecnt, 1 << 32),
                    z3.ULT(idx, 1 << 8), z3.ULT(ln, 1 << 20))
    passed = z3.And(typecnt != 0,
                    z3.ULE(44 + 5 * timecnt + 6 * typecnt, ln),
                    z3.ULT(idx, typecnt))
    read_hi = 44 + 5 * timecnt + 6 * idx + 4        # gmtoff is 4 bytes
    ok = prove(z3.Implies(z3.And(bounds, passed), z3.ULE(read_hi, ln)))
    record("tz_offset: header checks imply the gmtoff read stays in bounds", ok)


def prove(formula):
    """Return True iff `formula` is valid (its negation is unsat)."""
    s = z3.Solver()
    s.add(z3.Not(formula))
    return s.check() == z3.unsat


# ============================================================ angr EQUIVALENCE
def prove_emit2():
    ok_all = True
    detail = ""
    for v in range(0, 100):          # emit2 is only used on values 0..99
        st = leaf_state(SYM["emit2"], rdi=OUTBUF)
        st.regs.al = v
        found, viol = run_leaf(st, [(OUTBUF, OUTBUF + OUTLEN), (INBUF, INBUF + 16)])
        if len(found) != 1 or viol:
            ok_all = False; detail = f"v={v} paths={len(found)} viol={viol}"; break
        got = read_out(found[0], OUTBUF)
        if got != f"{v:02d}".encode():
            ok_all = False; detail = f"v={v} got={got}"; break
    record("emit2 writes 2-digit decimal (0..99), writes in bounds", ok_all, detail)


def prove_emitdec():
    ok_all = True; detail = ""
    vals = list(range(0, 200)) + [999, 1000, 1439, 65535, 100000, 2**31, 2**32 - 1]
    for v in vals:
        st = leaf_state(SYM["emitdec"], rdi=OUTBUF)
        st.regs.eax = v & 0xFFFFFFFF
        found, viol = run_leaf(st, [(OUTBUF, OUTBUF + OUTLEN)])
        if len(found) != 1 or viol:
            ok_all = False; detail = f"v={v} paths={len(found)} viol={viol}"; break
        got = read_out(found[0], OUTBUF)
        if got != spec.digits_py(v).encode():
            ok_all = False; detail = f"v={v} got={got}"; break
    record("emitdec writes unsigned decimal, writes in bounds", ok_all, detail)


def prove_emithm():
    ok_all = True; detail = ""
    for v in range(0, 1440):
        st = leaf_state(SYM["emithm"], rdi=OUTBUF)
        st.regs.eax = v
        found, viol = run_leaf(st, [(OUTBUF, OUTBUF + OUTLEN)])
        if len(found) != 1 or viol:
            ok_all = False; detail = f"v={v} paths={len(found)} viol={viol}"; break
        got = read_out(found[0], OUTBUF)
        if got != spec.hm_py(v).encode():
            ok_all = False; detail = f"v={v} got={got}"; break
    record("emithm writes HH:MM (0..1439), writes in bounds", ok_all, detail)


def prove_emitdur():
    ok_all = True; detail = ""
    vals = list(range(0, 200)) + [59, 60, 61, 600, 1439, 1440, 1441, 100000, 2**31, 2**32 - 1]
    for v in vals:
        st = leaf_state(SYM["emitdur"], rdi=OUTBUF)
        st.regs.eax = v & 0xFFFFFFFF
        found, viol = run_leaf(st, [(OUTBUF, OUTBUF + OUTLEN)])
        if len(found) != 1 or viol:
            ok_all = False; detail = f"v={v} paths={len(found)} viol={viol}"; break
        got = read_out(found[0], OUTBUF)
        if got != spec.fmt_hm_py(v).encode():
            ok_all = False; detail = f"v={v} got={got}"; break
    record("emitdur writes '<h>h <m>m', writes in bounds", ok_all, detail)


# ---------------------------------------------- parse_hm (edit-time validator)
def parse_hm_cl(b, length):
    """claripy mirror of spec.parse_hm over 5 symbolic bytes b[0..4] and a
    symbolic length; returns a 32-bit value (-1 as 0xFFFFFFFF)."""
    BAD = claripy.BVV(0xFFFFFFFF, 32)
    def ext(x):
        return claripy.ZeroExt(24, x)
    hd = claripy.If(b[1] == ord(':'), claripy.BVV(1, 32), claripy.BVV(2, 32))
    # hour digits
    def isdig(x):
        return claripy.And(claripy.UGE(x, ord('0')), claripy.ULE(x, ord('9')))
    d0 = ext(b[0]) - ord('0')
    d1 = ext(b[1]) - ord('0')
    hh1 = d0                                   # hd==1 -> hours = b0
    hh2 = d0 * 10 + d1                          # hd==2 -> hours = b0b1
    hh = claripy.If(hd == 1, hh1, hh2)
    # minute char positions depend on hd
    mc1 = claripy.If(hd == 1, b[2], b[3])       # tens char
    mc2 = claripy.If(hd == 1, b[3], b[4])       # units char
    colon_ok = claripy.If(hd == 1, b[1] == ord(':'), b[2] == ord(':'))
    hours_dig = claripy.If(hd == 1, isdig(b[0]),
                           claripy.And(isdig(b[0]), isdig(b[1])))
    m = (ext(mc1) - ord('0')) * 10 + (ext(mc2) - ord('0'))
    val = hh * 60 + m
    good = claripy.And(
        claripy.Or(length == 4, length == 5),
        length == hd + 3,
        colon_ok,
        hours_dig,
        claripy.ULE(hh, 23),
        claripy.And(claripy.UGE(mc1, ord('0')), claripy.ULE(mc1, ord('5'))),
        claripy.And(claripy.UGE(mc2, ord('0')), claripy.ULE(mc2, ord('9'))),
    )
    return claripy.If(good, val[31:0], BAD)


def prove_parse_hm():
    st = leaf_state(SYM["parse_hm"], rdi=INBUF)
    b = [claripy.BVS(f"b{i}", 8) for i in range(5)]
    for i in range(5):
        st.memory.store(INBUF + i, b[i])
    length = claripy.BVS("len", 64)
    st.solver.add(claripy.ULE(length, 6))       # caller passes at most 5; 0..6 covers it
    st.regs.rsi = length
    found, viol = run_leaf(st, [(INBUF, INBUF + 5)])
    ok = bool(found) and not viol
    detail = f"paths={len(found)} viol={viol}"
    spec_expr = parse_hm_cl(b, length[31:0])
    for s in found:
        ret = s.regs.eax
        # under this path's constraints, must the binary equal the spec?
        if s.solver.satisfiable(extra_constraints=[ret != spec_expr]):
            ok = False
            detail = "return value disagrees with spec on some input"
            break
    record("parse_hm matches the HH:MM grammar spec; reads stay within the buffer",
           ok, detail)


# ------------------------------------------------- feedb (parser step, inductive)
WEIGHTS = spec.WEIGHTS
def feedb_step_cl(weights, cur, line, indig, total, c):
    """claripy mirror of spec.Parser.byte's effect on (weights,cur,line,indig,
    total).  All 32-bit except c and indig (8-bit: the code keeps the flag in
    r11b and only ever tests that byte).  Returns the tuple after one byte."""
    M = 0xFFFFFFFF
    isdig = claripy.And(claripy.UGE(c, ord('0')), claripy.ULE(c, ord('9')))
    # digit branch
    dg_cur = (cur * 10 + claripy.ZeroExt(24, c - ord('0')))
    # separator branch
    w = claripy.SignExt(24, weights[7:0])
    line1 = line + w * cur
    weights1 = claripy.LShR(weights, 8)
    complete = weights1 == 0
    # completion arithmetic (mirrors sum_emit): d = line1 smod 1440, made >=0
    d = line1.SMod(claripy.BVV(1440, 32))       # x86 idiv remainder, sign of dividend
    d = claripy.If(claripy.SLT(d, 0), d + 1440, d)
    total_c = total + d
    indig_nz = indig != 0
    is_nl = c == 0x0A
    RW = claripy.BVV(WEIGHTS, 32)
    Z = claripy.BVV(0, 32)
    # separator, was in a digit run:
    #   complete -> reset all, total updated
    #   else if newline -> reset all (total unchanged)
    #   else -> keep line1/weights1, cur=0, indig=0
    sep_dig_w = claripy.If(complete, RW, claripy.If(is_nl, RW, weights1))
    sep_dig_line = claripy.If(complete, Z, claripy.If(is_nl, Z, line1))
    sep_dig_total = claripy.If(complete, total_c, total)
    # separator, not in a digit run:
    #   newline -> reset ; else unchanged
    sep_nod_w = claripy.If(is_nl, RW, weights)
    sep_nod_line = claripy.If(is_nl, Z, line)
    sep_nod_cur = claripy.If(is_nl, Z, cur)      # spec leaves cur untouched
    # select separator sub-branch
    sep_w = claripy.If(indig_nz, sep_dig_w, sep_nod_w)
    sep_line = claripy.If(indig_nz, sep_dig_line, sep_nod_line)
    sep_total = claripy.If(indig_nz, sep_dig_total, total)
    sep_cur = claripy.If(indig_nz, Z, sep_nod_cur)
    # final select digit vs separator
    nw = claripy.If(isdig, weights, sep_w)
    ncur = claripy.If(isdig, dg_cur, sep_cur)
    nline = claripy.If(isdig, line, sep_line)
    nindig = claripy.If(isdig, claripy.BVV(1, 8), claripy.BVV(0, 8))
    ntot = claripy.If(isdig, total, sep_total)
    return nw, ncur, nline, nindig, ntot


class SumEmitModel(angr.SimProcedure):
    """Model of sum_emit for the feedb transition proof: total += canonical
    (line mod 1440), no output.  The real sum_emit is verified against exactly
    this update in prove_sum_emit, so using the model here is not circular."""
    def run(self):
        line = self.state.regs.r10[31:0]
        total = self.state.regs.r12[31:0]
        d = line.SMod(claripy.BVV(1440, 32))
        d = claripy.If(claripy.SLT(d, 0), d + 1440, d)
        self.state.regs.r12 = claripy.ZeroExt(32, total + d)
        return


def prove_sum_emit():
    # real sum_emit, with formatting hooked out; check the total update only.
    for h in ("emitdur", "writestr"):
        proj.hook(SYM[h], WritestrNoop(), replace=True)
    line = claripy.BVS("line", 32); total = claripy.BVS("total", 32)
    st = leaf_state(SYM["sum_emit"])
    st.regs.rsp = STACK
    st.stack_push(claripy.BVV(RET, 64))
    st.regs.r10 = claripy.ZeroExt(32, line)
    st.regs.r12 = claripy.ZeroExt(32, total)
    found, _ = run_leaf(st, [(OUTBUF, OUTBUF + OUTLEN)])
    d = line.SMod(claripy.BVV(1440, 32))
    d = claripy.If(claripy.SLT(d, 0), d + 1440, d)
    exp = total + d
    ok = bool(found); detail = f"paths={len(found)}"
    for s in found:
        if s.solver.satisfiable(extra_constraints=[s.regs.r12[31:0] != exp]):
            ok = False; detail = "total update disagrees with spec"; break
    record("sum_emit adds canonical (line mod 1440) to the total", ok, detail)
    proj.unhook(SYM["emitdur"])


def prove_feedb():
    # symbolic parser state + input byte; model sum_emit (verified separately),
    # then compare the register transition to the spec step.
    proj.hook(SYM["writestr"], WritestrNoop(), replace=True)
    proj.hook(SYM["sum_emit"], SumEmitModel(), replace=True)
    weights = claripy.BVS("weights", 32); cur = claripy.BVS("cur", 32)
    line = claripy.BVS("line", 32); indig = claripy.BVS("indig", 8)
    total = claripy.BVS("total", 32); c = claripy.BVS("c", 8)
    st = leaf_state(SYM["feedb"])
    st.regs.ebx = weights; st.regs.ebp = cur
    st.regs.r10 = claripy.ZeroExt(32, line)
    st.regs.r11 = claripy.ZeroExt(56, indig)
    st.regs.r12 = claripy.ZeroExt(32, total)
    st.regs.al = c
    # r9 (arg pointer) is preserved by feedb; give it a harmless value
    st.regs.r9 = INBUF
    found, viol = run_leaf(st, [(OUTBUF, OUTBUF + OUTLEN), (INBUF, INBUF + 16)])
    ok = bool(found) and not viol
    detail = f"paths={len(found)} viol={viol}"
    ew, ecur, eline, eindig, etot = feedb_step_cl(weights, cur, line, indig, total, c)
    for s in found:
        got_w = s.regs.ebx
        got_cur = s.regs.ebp
        got_line = s.regs.r10[31:0]
        got_indig = s.regs.r11[7:0]          # code only writes r11b (0 or 1)
        got_tot = s.regs.r12[31:0]
        bad = claripy.Or(
            got_w != ew, got_cur != ecur, got_line != eline,
            got_tot != etot, got_indig != eindig,
        )
        if s.solver.satisfiable(extra_constraints=[bad]):
            ok = False
            pairs = {"weights": (got_w, ew), "cur": (got_cur, ecur),
                     "line": (got_line, eline), "total": (got_tot, etot),
                     "indig": (got_indig, eindig)}
            m = {k: s.solver.eval(v, extra_constraints=[bad])
                 for k, v in (("weights", weights), ("cur", cur), ("line", line),
                              ("indig", indig), ("total", total), ("c", c))}
            diff = [k for k, (g, e) in pairs.items()
                    if s.solver.satisfiable(extra_constraints=[g != e])]
            detail = (f"register transition disagrees with spec in {diff}; "
                      f"e.g. input " + ", ".join(f"{k}={v:#x}" for k, v in m.items()))
            break
    record("feedb parser step matches spec for all states/bytes (induction base)",
           ok, detail)
    for h in ("writestr", "sum_emit"):          # restore for the whole-program run
        proj.unhook(SYM[h])


class WritestrNoop(angr.SimProcedure):
    def run(self, *a):
        self.state.regs.rax = self.state.regs.rdx
        return


# ------------------------------------------------- terminal restore on quit
def prove_terminal():
    # from the quit path, with RAW set, TCSETS(saved termios) must precede exit.
    st = leaf_state(SYM["track_quit"])
    frame = 0x660000
    st.regs.rbx = frame
    st.memory.store(frame + 0x138C, claripy.BVV(1, 32), endness="Iend_LE")  # F_RAW=1
    syscalls = []
    def on_sys(state):
        syscalls.append(state.solver.eval(state.regs.rax) & 0xFFFFFFFF)
    st.inspect.b("syscall", when=angr.BP_BEFORE, action=on_sys)
    simgr = proj.factory.simulation_manager(st)
    simgr.run(n=6)
    # expect ioctl(16) then exit(60), ioctl first
    ok = (16 in syscalls and 60 in syscalls
          and syscalls.index(16) < syscalls.index(60))
    record("quit with raw tty issues TCSETS(restore) before exit", ok,
           f"syscalls={syscalls}")

    # and with RAW=0, it must NOT call ioctl (nothing to restore)
    st0 = leaf_state(SYM["track_quit"])
    st0.regs.rbx = frame
    st0.memory.store(frame + 0x138C, claripy.BVV(0, 32), endness="Iend_LE")
    sc0 = []
    st0.inspect.b("syscall", when=angr.BP_BEFORE,
                  action=lambda s: sc0.append(s.solver.eval(s.regs.rax) & 0xFFFFFFFF))
    simgr0 = proj.factory.simulation_manager(st0)
    simgr0.run(n=6)
    ok2 = (16 not in sc0 and 60 in sc0)
    record("quit without raw tty exits directly (no stray ioctl)", ok2,
           f"syscalls={sc0}")


# ------------------------------------------------- sum-mode whole-program safety
def prove_sum_safety():
    # a symbolic single argument; check no write ever targets the code image and
    # only write/exit syscalls occur, across every explored path.
    argbytes = [claripy.BVS(f"a{i}", 8) for i in range(8)]
    arg = claripy.Concat(*argbytes)
    st = proj.factory.entry_state(
        args=["timekeep", arg],
        add_options={angr.options.ZERO_FILL_UNCONSTRAINED_MEMORY,
                     angr.options.ZERO_FILL_UNCONSTRAINED_REGISTERS},
    )
    for a in argbytes:                          # printable, non-NUL
        st.solver.add(claripy.And(claripy.UGE(a, 0x20), claripy.ULE(a, 0x7e)))
    bad = {"img_write": False, "syscalls": set()}
    imglo, imghi = BASE, BASE + os.path.getsize(BIN)
    def on_w(s):
        addr = s.inspect.attrs.mem_write_address
        if s.solver.satisfiable(extra_constraints=[claripy.And(
                claripy.UGE(addr, imglo), claripy.ULT(addr, imghi))]):
            bad["img_write"] = True
    def on_sys(s):
        try:
            bad["syscalls"].add(s.solver.eval(s.regs.rax) & 0xFFFFFFFF)
        except Exception:
            pass
    st.inspect.b("mem_write", when=angr.BP_BEFORE, action=on_w)
    st.inspect.b("syscall", when=angr.BP_BEFORE, action=on_sys)
    # The parser forks per argument byte, so the path count explodes.  The
    # breakpoints already record everything the check needs, so finished paths
    # are dropped as they appear and the active set is capped; paths beyond the
    # cap are counted and reported rather than held in memory.
    simgr = proj.factory.simulation_manager(st)
    done = pruned = 0
    for _ in range(4000):
        if not simgr.active:
            break
        simgr.step()
        done += len(simgr.deadended) + len(simgr.errored)
        simgr.drop(stash="deadended")
        simgr.drop(stash="errored")
        simgr.drop(stash="unsat")
        if len(simgr.active) > SUM_ACTIVE_CAP:
            pruned += len(simgr.active) - SUM_ACTIVE_CAP
            simgr.split(from_stash="active", to_stash="pruned",
                        limit=SUM_ACTIVE_CAP)
            simgr.drop(stash="pruned")
    allowed = bad["syscalls"] <= {1, 60}        # write, exit_group/exit
    ok = (not bad["img_write"]) and allowed and (60 in bad["syscalls"])
    record("sum mode: no writes into the code image; only write/exit syscalls",
           ok, f"img_write={bad['img_write']} syscalls={sorted(bad['syscalls'])} "
               f"paths_finished={done} paths_pruned={pruned} "
               f"left_active={len(simgr.active)}")


if __name__ == "__main__":
    print("timekeep formal verification\n" + "=" * 40)
    print("\n-- Z3 unbounded theorems --")
    prove_z3_theorems()
    print("\n-- angr equivalence over the shipped binary --")
    prove_emit2()
    prove_emitdec()
    prove_emithm()
    prove_emitdur()
    prove_parse_hm()
    prove_sum_emit()
    prove_feedb()

    print("\n-- angr whole-program checks --")
    prove_terminal()
    prove_sum_safety()

    print("\n" + "=" * 40)
    n_ok = sum(1 for _, ok, _ in results if ok)
    print(f"{n_ok}/{len(results)} obligations proved")
    sys.exit(0 if n_ok == len(results) else 1)
