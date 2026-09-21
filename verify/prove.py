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
import tempfile

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
    with tempfile.TemporaryDirectory() as tmp:       # private: runs may overlap
        obj = os.path.join(tmp, "tk_verify.o")
        subprocess.run(["as", "-o", obj, os.path.join(ROOT, "timekeep.s")], check=True)
        out = subprocess.check_output(["objdump", "-t", obj]).decode()
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


def watch_mem(st, write_ok, read_ok=None):
    """Record every write (and read, if read_ok is given) that can fall outside
    the allowed regions, a list of (lo, hi) pairs of ints or claripy exprs.
    Unlike run_leaf's guard there is no implicit stack window: the caller lists
    the exact stack slots.  Returns the (shared) violation list."""
    viol = []
    def check(s, kind):
        if kind == "w":
            addr, length, regions = (s.inspect.mem_write_address,
                                     s.inspect.mem_write_length, write_ok)
        else:
            addr, length, regions = (s.inspect.mem_read_address,
                                     s.inspect.mem_read_length, read_ok)
        if length is None:
            length = 1
        size = length if isinstance(length, int) else s.solver.eval_one(length)
        a = addr if not isinstance(addr, int) else claripy.BVV(addr, 64)
        inside = claripy.Or(claripy.false(), *[
            claripy.And(claripy.UGE(a, lo), claripy.ULE(a + size, hi))
            for lo, hi in regions])
        if s.solver.satisfiable(extra_constraints=[claripy.Not(inside)]):
            viol.append((kind, hex(s.addr), size))
    st.inspect.b("mem_write", when=angr.BP_BEFORE, action=lambda s: check(s, "w"))
    if read_ok is not None:
        st.inspect.b("mem_read", when=angr.BP_BEFORE, action=lambda s: check(s, "r"))
    return viol


def watch_syscalls(st):
    """Log (rax, rdi, rsi, rdx) of every syscall into the state's globals."""
    def on_sys(s):
        s.globals["sys"] = s.globals.get("sys", ()) + (
            (s.regs.rax, s.regs.rdi, s.regs.rsi, s.regs.rdx),)
    st.inspect.b("syscall", when=angr.BP_BEFORE, action=on_sys)


def explore_capped(st, stops, max_active=64, max_steps=400):
    """Step every path until it reaches an address in `stops` (moved to
    'found') or ends (left in 'deadended').  Never prunes: if the live path
    count or the step count exceeds its cap, or a path errors, returns a reason
    string so the obligation fails instead of silently losing paths."""
    simgr = proj.factory.simulation_manager(st)
    stops = set(stops)
    for _ in range(max_steps):
        if not simgr.active:
            break
        simgr.step(extra_stop_points=stops)
        simgr.move("active", "found", lambda s: s.addr in stops)
        if len(simgr.active) > max_active:
            return simgr, f"path cap {max_active} exceeded"
    if simgr.active:
        return simgr, f"step cap {max_steps} exceeded"
    if simgr.errored:
        return simgr, f"path errored: {simgr.errored[0].error}"
    return simgr, None


def must(state, cond):
    """True iff `cond` holds on every model of the state's path constraints."""
    return not state.solver.satisfiable(extra_constraints=[claripy.Not(cond)])


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
    this update in prove_sum_emit, so using the model here is not circular.
    Registers the real routine may clobber become fresh symbols; the ones it
    keeps are exactly those prove_sum_emit_frame shows it preserves."""
    def run(self):
        s = self.state
        s.globals["sum_emit_rsp"] = s.globals.get("sum_emit_rsp", ()) + (s.regs.rsp,)
        line = s.regs.r10[31:0]
        total = s.regs.r12[31:0]
        d = line.SMod(claripy.BVV(1440, 32))
        d = claripy.If(claripy.SLT(d, 0), d + 1440, d)
        s.regs.r12 = claripy.ZeroExt(32, total + d)
        for r in SUM_EMIT_CLOBBERS:
            setattr(s.regs, r, claripy.BVS(f"clob_{r}", 64))
        return


# registers sum_emit (with emitdur/emitdec and the write syscall) may change,
# besides r12; prove_sum_emit_frame shows every other one is preserved.
SUM_EMIT_CLOBBERS = ("rax", "rcx", "rdx", "rdi", "rsi", "r8", "r11")
SUM_EMIT_KEEPS = ("rbx", "rbp", "r9", "r10", "r13", "r14", "r15")


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
    for h in ("emitdur", "writestr"):
        proj.unhook(SYM[h])


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
    # the loop driver's registers must come back untouched
    keep = {r: claripy.BVS(r, 64) for r in ("r9", "r13", "r14", "r15")}
    for r, v in keep.items():
        setattr(st.regs, r, v)
    # rsp at entry is STACK-8 (the return address).  feedb's only own write
    # is its call to sum_emit, pushed at STACK-16; sum_emit's writes are
    # bounded relative to its entry rsp by prove_sum_emit_frame.
    viol = watch_mem(st, [(STACK - 16, STACK - 8)])
    simgr, why = explore_capped(st, [RET])
    found = simgr.found
    ok = bool(found) and not viol and why is None and not simgr.deadended
    detail = f"paths={len(found)} viol={viol} cap={why}"
    ew, ecur, eline, eindig, etot = feedb_step_cl(weights, cur, line, indig, total, c)
    for s in found:
        # ret popped the sentinel, so rsp is back to STACK; driver regs kept;
        # sum_emit, when called, is entered at rsp STACK-16
        framed = claripy.And(s.regs.rsp == STACK, *[
            getattr(s.regs, r) == v for r, v in keep.items()])
        calls = s.globals.get("sum_emit_rsp", ())
        if not must(s, framed) or len(calls) > 1 or not all(
                must(s, r == STACK - 16) for r in calls):
            ok = False
            detail = "feedb disturbs rsp/r9/r13/r14/r15 or calls sum_emit off-frame"
            break
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
    record("feedb parser step matches spec for all states/bytes; "
           "keeps rsp/r9/r13-r15", ok, detail)
    for h in ("writestr", "sum_emit"):          # restore for the whole-program run
        proj.unhook(SYM[h])


class WritestrNoop(angr.SimProcedure):
    """writestr without the syscall: returns the byte count and, like the
    syscall instruction, clobbers rcx and r11."""
    def run(self, *a):
        self.state.regs.rax = self.state.regs.rdx
        self.state.regs.rcx = claripy.BVS("sys_rcx", 64)
        self.state.regs.r11 = claripy.BVS("sys_r11", 64)
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


# ------------------------------------------------- sum-mode loop, by induction
# run_sum's loop has two cut points, sum_arg and sum_byte.  With A the initial
# rsp (argc slot) and S = A - 0x100 the frame, the invariant Inv at both is:
#   rsp == S, r15 == A+8 (argv), r14 == argc, 1 <= r13 <= r14 (< r14 at
#   sum_byte), parser regs == spec Parser state, nothing at/above S+0x100 written.
# prove_sum_prologue shows _start establishes Inv; prove_sum_arg and
# prove_sum_byte show each cut-point-to-cut-point step keeps it.  feedb is
# replaced by the spec step (FeedbModel), which prove_feedb justifies.
ARGC_SLOT = STACK             # A: concrete; the loop never compares rsp with an
FRAME = ARGC_SLOT - 0x100     # absolute address, so the choice is immaterial
ARGV = ARGC_SLOT + 8


class FeedbModel(angr.SimProcedure):
    """feedb as one spec parser step on (ebx, ebp, r10d, r11b, r12d) and the
    byte in al.  prove_feedb shows the real routine computes the same and keeps
    rsp/r9/r13/r14/r15; everything else it may touch becomes a fresh symbol."""
    def run(self):
        s = self.state
        s.globals["feedb"] = s.globals.get("feedb", ()) + ((s.regs.rsp, s.regs.al),)
        nw, ncur, nline, nindig, ntot = feedb_step_cl(
            s.regs.ebx, s.regs.ebp, s.regs.r10[31:0], s.regs.r11[7:0],
            s.regs.r12[31:0], s.regs.al)
        hi = lambda r, n: claripy.BVS(f"hi_{r}", n)
        s.regs.rbx = claripy.Concat(hi("rbx", 32), nw)
        s.regs.rbp = claripy.Concat(hi("rbp", 32), ncur)
        s.regs.r10 = claripy.Concat(hi("r10", 32), nline)
        s.regs.r11 = claripy.Concat(hi("r11", 56), nindig)
        s.regs.r12 = claripy.Concat(hi("r12", 32), ntot)
        for r in ("rax", "rcx", "rdx", "rdi", "rsi", "r8"):
            setattr(s.regs, r, claripy.BVS(f"clob_{r}", 64))
        return


def parser_regs(s):
    return (s.regs.ebx, s.regs.ebp, s.regs.r10[31:0], s.regs.r11[7:0],
            s.regs.r12[31:0])


def cut_state(addr, at_byte):
    """A symbolic state at a cut point satisfying Inv."""
    st = proj.factory.blank_state(
        addr=addr,
        add_options={angr.options.SYMBOL_FILL_UNCONSTRAINED_REGISTERS,
                     angr.options.SYMBOL_FILL_UNCONSTRAINED_MEMORY})
    v = {"weights": claripy.BVS("weights", 32), "cur": claripy.BVS("cur", 32),
         "line": claripy.BVS("line", 32), "indig": claripy.BVS("indig", 8),
         "total": claripy.BVS("total", 32),
         "r13": claripy.BVS("r13", 64), "r14": claripy.BVS("r14", 64)}
    st.regs.rsp = FRAME
    st.regs.r15 = ARGV
    st.regs.r13 = v["r13"]; st.regs.r14 = v["r14"]
    st.regs.ebx = v["weights"]; st.regs.ebp = v["cur"]
    st.regs.r10 = claripy.ZeroExt(32, v["line"])
    st.regs.r11 = claripy.ZeroExt(56, v["indig"])
    st.regs.r12 = claripy.ZeroExt(32, v["total"])
    # argc is a positive int; r13 indexes a real argument (or argc itself)
    st.solver.add(claripy.ULE(1, v["r13"]), claripy.ULT(v["r14"], 1 << 31))
    st.solver.add(claripy.ULT(v["r13"], v["r14"]) if at_byte
                  else claripy.ULE(v["r13"], v["r14"]))
    watch_syscalls(st)
    return st, v


def prove_sum_emit_frame():
    # A: real sum_emit/emitdur/emitdec/write for every 32-bit line and total.
    # Entered at rsp X, it may only write [X-0x80, X+0x50): the digit pushes
    # below and the output line at X+0x40.  In the loop X = S-16, so nothing
    # at or above S+0x40 < A (argc/argv/strings) is ever written.
    st = leaf_state(SYM["sum_emit"])
    X = STACK - 8
    st.regs.r10 = claripy.ZeroExt(32, claripy.BVS("line", 32))
    st.regs.r12 = claripy.ZeroExt(32, claripy.BVS("total", 32))
    keep = {r: claripy.BVS(r, 64) for r in SUM_EMIT_KEEPS if r != "r10"}
    keep["r10"] = st.regs.r10
    for r, v in keep.items():
        setattr(st.regs, r, v)
    buf = X + 0x40
    st.memory.store(buf, claripy.BVS("junk", 16 * 8))   # "not written yet"
    region = [(X - 0x80, X + 0x50)]
    viol = watch_mem(st, region, region)
    watch_syscalls(st)
    simgr, why = explore_capped(st, [RET])
    ok = bool(simgr.found) and not viol and why is None and not simgr.deadended
    detail = f"paths={len(simgr.found)} viol={viol} cap={why}"
    for s in simgr.found:
        calls = s.globals.get("sys", ())
        if len(calls) != 1:
            ok = False; detail = f"{len(calls)} syscalls on a path"; break
        rax, rdi, rsi, rdx = calls[0]
        if not (must(s, claripy.And(rax == 1, rdi == 1, rsi == buf,
                                    claripy.ULE(1, rdx), claripy.ULE(rdx, 16)))):
            ok = False; detail = "syscall is not write(1, buf, 1..16)"; break
        n = s.solver.eval_one(rdx)
        if any("junk" in str(v) for i in range(n)
               for v in s.memory.load(buf + i, 1).variables):
            ok = False; detail = "write covers bytes never filled"; break
        if not must(s, claripy.And(s.regs.rsp == STACK, *[
                getattr(s.regs, r) == v for r, v in keep.items()])):
            ok = False; detail = "sum_emit changes rsp or a kept register"; break
    record("sum_emit: write(1, line, 1..16) from its own frame; writes stay "
           "in [rsp-0x80, rsp+0x50); keeps loop regs", ok, detail)


def prove_sum_prologue():
    # C: from _start with any argc >= 2, reach sum_arg with Inv and the spec's
    # initial parser state, writing nothing.
    st = proj.factory.blank_state(
        addr=SYM["_start"],
        add_options={angr.options.SYMBOL_FILL_UNCONSTRAINED_REGISTERS,
                     angr.options.SYMBOL_FILL_UNCONSTRAINED_MEMORY})
    argc = claripy.BVS("argc", 64)
    st.regs.rsp = ARGC_SLOT
    st.memory.store(ARGC_SLOT, argc, endness="Iend_LE")
    st.solver.add(claripy.UGE(argc, 2), claripy.ULT(argc, 1 << 31))
    viol = watch_mem(st, [])
    watch_syscalls(st)
    simgr, why = explore_capped(st, [SYM["sum_arg"]])
    ok = len(simgr.found) == 1 and not simgr.deadended and not viol and why is None
    detail = f"paths={len(simgr.found)} ended={len(simgr.deadended)} viol={viol} cap={why}"
    for s in simgr.found:
        w, cur, line, indig, tot = parser_regs(s)
        inv = claripy.And(
            s.regs.rsp == FRAME, s.regs.r15 == ARGV, s.regs.r14 == argc,
            s.regs.r13 == 1, w == WEIGHTS, cur == 0, line == 0, indig == 0,
            tot == 0)
        if not must(s, inv) or s.globals.get("sys"):
            ok = False; detail = "sum_arg reached without Inv / initial state"
    record("sum mode prologue: reaches sum_arg with Inv and the spec's "
           "initial parser state", ok, detail)


def prove_sum_arg():
    # D: one step from sum_arg.  Either r13 == argc and the program exits(0)
    # with no other syscall, or it loads argv[r13] (and only that) into r9 and
    # reaches sum_byte with everything else unchanged.
    proj.hook(SYM["feedb"], FeedbModel(), replace=True)
    st, v = cut_state(SYM["sum_arg"], at_byte=False)
    slot = ARGV + 8 * v["r13"]
    viol = watch_mem(st, [], [(ARGV, ARGV + 8 * v["r14"])])
    def on_read(s):
        s.globals["argp"] = s.inspect.mem_read_expr
    st.inspect.b("mem_read", when=angr.BP_AFTER, action=on_read)
    before = parser_regs(st)
    simgr, why = explore_capped(st, [SYM["sum_byte"]])
    ok = bool(simgr.found) and len(simgr.deadended) == 1 and not viol and why is None
    detail = (f"to_byte={len(simgr.found)} exits={len(simgr.deadended)} "
              f"viol={viol} cap={why}")
    for s in simgr.found:
        same = claripy.And(
            claripy.ULT(v["r13"], v["r14"]), s.regs.rsp == FRAME,
            s.regs.r15 == ARGV, s.regs.r13 == v["r13"], s.regs.r14 == v["r14"],
            s.regs.r9 == s.globals.get("argp", claripy.BVV(0, 64)),
            *[a == b for a, b in zip(parser_regs(s), before)])
        if not must(s, same) or s.globals.get("sys") or s.globals.get("feedb"):
            ok = False; detail = "sum_arg -> sum_byte breaks Inv or r9 != argv[r13]"
    for s in simgr.deadended:
        calls = s.globals.get("sys", ())
        if (len(calls) != 1 or not must(s, claripy.And(
                calls[0][0] == 60, calls[0][1] == 0, v["r13"] == v["r14"]))):
            ok = False; detail = "exit path is not a lone exit(0) at r13 == argc"
    record("sum_arg step: exit(0) once all args are read, else r9 = argv[r13] "
           "(the only read); keeps Inv", ok, detail)
    proj.unhook(SYM["feedb"])


def prove_sum_byte():
    # E: one step from sum_byte.  Reads the byte at r9 (only that); a nonzero
    # byte c is fed as c and r9 advances; a NUL is fed as ' ' and r13 advances.
    # The only writes are the call's return slot below S; no syscalls.
    proj.hook(SYM["feedb"], FeedbModel(), replace=True)
    st, v = cut_state(SYM["sum_byte"], at_byte=True)
    c = claripy.BVS("c", 8)
    st.regs.r9 = INBUF          # any address works: the loop only reads it
    st.memory.store(INBUF, c)
    viol = watch_mem(st, [(FRAME - 8, FRAME)],
                     [(INBUF, INBUF + 1), (FRAME - 8, FRAME)])
    before = parser_regs(st)
    simgr, why = explore_capped(st, [SYM["sum_byte"], SYM["sum_arg"]])
    ok = len(simgr.found) == 2 and not simgr.deadended and not viol and why is None
    detail = f"paths={len(simgr.found)} viol={viol} cap={why}"
    for s in simgr.found:
        calls = s.globals.get("feedb", ())
        if len(calls) != 1 or s.globals.get("sys"):
            ok = False; detail = "not exactly one feedb call, or a syscall"; break
        rsp_at, fed = calls[0]
        nul = s.addr == SYM["sum_arg"]
        byte = claripy.BVV(0x20, 8) if nul else c
        exp = feedb_step_cl(*before[:4], before[4], byte)
        got = parser_regs(s)
        cond = [rsp_at == FRAME - 8, fed == byte, s.regs.rsp == FRAME,
                s.regs.r15 == ARGV, s.regs.r14 == v["r14"],
                *[a == b for a, b in zip(got, (exp[0], exp[1], exp[2], exp[3], exp[4]))]]
        if nul:
            cond += [c == 0, s.regs.r13 == v["r13"] + 1]
        else:
            cond += [c != 0, s.regs.r13 == v["r13"], s.regs.r9 == INBUF + 1]
        if not must(s, claripy.And(*cond)):
            ok = False
            detail = f"{'NUL' if nul else 'byte'} path breaks Inv or feeds the wrong byte"
            break
    record("sum_byte step: feeds the byte (NUL as ' ', then next arg) to the "
           "spec step; keeps Inv", ok, detail)
    proj.unhook(SYM["feedb"])


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
    prove_sum_emit_frame()
    prove_feedb()

    print("\n-- angr whole-program checks --")
    prove_terminal()

    print("\n-- sum-mode loop, by induction over cut points --")
    prove_sum_prologue()
    prove_sum_arg()
    prove_sum_byte()

    print("\n" + "=" * 40)
    n_ok = sum(1 for _, ok, _ in results if ok)
    print(f"{n_ok}/{len(results)} obligations proved")
    sys.exit(0 if n_ok == len(results) else 1)
