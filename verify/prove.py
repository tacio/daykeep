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


def load_frame_offsets():
    """The track frame's field offsets, from the `.set F_*` lines in timekeep.s."""
    import re
    src = open(os.path.join(ROOT, "timekeep.s")).read()
    return {m[1]: int(m[2], 16) for m in
            re.finditer(r"^\.set\s+F_(\w+),\s*(0x[0-9A-Fa-f]+)", src, re.M)}


SYM = load_symbols()
F = load_frame_offsets()
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


# ------------------------------------------------- track mode: saving
# The key loop runs with rsp == rbx == the frame base and F_OUTFD == 1.  TF is a
# concrete frame base; the code only addresses the frame through rbx.
TF = 0x660000
PFX, SFX = b"timekeep-", b".txt"


def frame_state(addr):
    st = proj.factory.blank_state(
        addr=addr,
        add_options={angr.options.SYMBOL_FILL_UNCONSTRAINED_REGISTERS,
                     angr.options.SYMBOL_FILL_UNCONSTRAINED_MEMORY})
    st.regs.rbx = TF
    st.regs.rsp = TF
    return st


def fld(s, name, size=4):
    return s.memory.load(TF + F[name], size, endness="Iend_LE")


class SysRet(angr.SimProcedure):
    """A syscall that may return anything: the kernel is not modelled.  The
    result is kept in the state's globals as ret_<name>."""
    NAME = "?"
    def run(self, *args):
        r = claripy.BVS(f"ret_{self.NAME}", 64)
        self.state.globals[f"ret_{self.NAME}"] = r
        self.state.regs.rax = r     # all 64 bits, whatever the C prototype says
        return None


def stub_syscalls(names):
    """Swap in SysRet for these syscalls; returns the library to restore."""
    old = proj.simos.syscall_library
    lib = old.copy()
    for n in names:
        lib.add(n, type(f"Sys_{n}", (SysRet,), {"NAME": n}))
    proj.simos.syscall_library = lib
    return old


def log_events(st):
    """Log syscalls, as ("sys", rax, rdi, rsi, rdx), into the state's globals,
    in order with the ("render", ...) calls RenderFileModel adds."""
    def on_sys(s):
        s.globals["ev"] = s.globals.get("ev", ()) + (
            ("sys", s.regs.rax, s.regs.rdi, s.regs.rsi, s.regs.rdx),)
    st.inspect.b("syscall", when=angr.BP_BEFORE, action=on_sys)


RENDER_CLOBBERS = ("rax", "rcx", "rdx", "rsi", "rdi", "r8", "r9", "r10", "r11",
                   "r14")


class RenderFileModel(angr.SimProcedure):
    """render with F_OUTFD != 1 (drawing into a save file): it returns, keeps
    rbx/rbp/r12/r13/r15, writes only the line buffer F_LINE (and the stack
    below its return slot), and its syscalls are writes to F_OUTFD, which it
    leaves unchanged; prove_render_* show the real render does this for every
    F_N <= 250.  Here the line buffer and the other registers become fresh
    symbols, and the call itself is logged with the F_OUTFD it saw."""
    def run(self):
        s = self.state
        s.globals["ev"] = s.globals.get("ev", ()) + (
            ("render", fld(s, "OUTFD"), s.regs.rsp),)
        # render's own writes; outside the model the line buffer is off limits
        s.memory.store(TF + F["LINE"], claripy.BVS("line_junk", 128 * 8),
                       inspect=False)
        for r in RENDER_CLOBBERS:
            setattr(s.regs, r, claripy.BVS(f"clob_{r}", 64))
        return


def dec_cl(t, k):
    """Decimal of 32-bit t as k digits, by the usual definition: q_0 = t,
    q_{j+1} = q_j / 10, digit j from the right is q_j % 10, and t has k digits
    iff q_k == 0 and q_{k-1} != 0 (or k == 1).  Returns (ASCII digits, most
    significant first; the condition that t has exactly k digits)."""
    q = [t]
    for _ in range(k):
        q.append(q[-1] / 10)
    digits = [(q[j] % 10)[7:0] + ord("0") for j in reversed(range(k))]
    has_k = claripy.And(q[k] == 0, q[k - 1] != 0) if k > 1 else q[1] == 0
    return digits, has_k


# ------------------------------------------------- render into a save file
# Justifies RenderFileModel for every stamp count 0..NMAX, by induction over
# render's two loop heads, labelled render_row and render_sum.  R is render's
# entry rsp (its return slot).  The invariant at both heads is:
#   rbx == TF, rsp == R, [R] == the return address, rbp/r12/r13/r15 as on
#   entry, F_N <= 250 and F_OUTFD == fd != 1 in the frame (render writes only
#   F_LINE and the stack, so both stay), and
#   render_row: r14 == zero-extended r14d <= F_N + 1
#   render_sum: r10 == zero-extended r10d <= F_N + 1, r9 == zero-extended r9d,
#               rdi == F_LINE + 6 (just past "total ")
R = STACK
RENDER_KEEPS = ("rbp", "r12", "r13", "r15")
NMAX = 250


def render_state(addr):
    st = frame_state(addr)
    st.regs.rsp = R
    st.memory.store(R, claripy.BVV(RET, 64), endness="Iend_LE")
    n, fd = claripy.BVS("n", 32), claripy.BVS("fd", 32)
    st.memory.store(TF + F["N"], n, endness="Iend_LE")
    st.memory.store(TF + F["OUTFD"], fd, endness="Iend_LE")
    st.solver.add(claripy.ULE(n, NMAX), fd != 1)
    keep = {r: claripy.BVS(r, 64) for r in RENDER_KEEPS}
    for r, v in keep.items():
        setattr(st.regs, r, v)
    log_events(st)
    line = TF + F["LINE"]
    code = (BASE, BASE + os.path.getsize(BIN))
    viol = watch_mem(st, [(line, line + 128), (R - 0x100, R)],
                     [(TF, TF + max(F.values()) + 4),   # through the last field
                      (R - 0x100, R + 8), code])
    return st, n, fd, keep, viol


def render_ok(s, keep, rsp=R):
    return claripy.And(s.regs.rbx == TF, s.regs.rsp == rsp,
                       *[getattr(s.regs, r) == v for r, v in keep.items()])


def one_line_write(s, fd):
    """The path made exactly one syscall: write(fd, F_LINE, 1..128)."""
    ev = s.globals.get("ev", ())
    return len(ev) == 1 and must(s, claripy.And(
        ev[0][1] == 1, ev[0][2] == fd.zero_extend(32), ev[0][3] == TF + F["LINE"],
        claripy.ULE(1, ev[0][4]), claripy.ULE(ev[0][4], 128)))


def prove_render_prologue():
    # From render's entry with F_OUTFD != 1: reach render_row with r14 == 0,
    # no writes, no syscalls (the screen clear is skipped).
    st, n, fd, keep, viol = render_state(SYM["render"])
    simgr, why = explore_capped(st, [SYM["render_row"]])
    ok = len(simgr.found) == 1 and not simgr.deadended and not viol and why is None
    detail = f"paths={len(simgr.found)} viol={viol[:3]} cap={why}"
    for s in simgr.found:
        if s.globals.get("ev") or not must(s, claripy.And(render_ok(s, keep),
                                                          s.regs.r14 == 0)):
            ok = False; detail = "render_row reached without Inv, or a syscall"
    record("render (file) prologue: reaches render_row with r14 = 0, no clear, "
           "no writes", ok, detail)


def prove_render_row():
    # One step from render_row.  If r14 < F_N: one write(fd, F_LINE, 1..128)
    # of the row and back to render_row with r14 + 2.  Otherwise: "total " into
    # F_LINE, r9 = r10 = 0, on to render_sum, no syscall.
    st, n, fd, keep, viol = render_state(SYM["render_row"])
    i = claripy.BVS("i", 32)
    st.regs.r14 = i.zero_extend(32)
    st.solver.add(claripy.ULE(i, n + 1))
    simgr, why = explore_capped(st, [SYM["render_row"], SYM["render_sum"]],
                                max_active=256)
    ok = bool(simgr.found) and not simgr.deadended and not viol and why is None
    detail = f"paths={len(simgr.found)} viol={viol[:3]} cap={why}"
    rows = sums = 0
    for s in simgr.found:
        if s.addr == SYM["render_row"]:
            rows += 1
            good = one_line_write(s, fd) and must(s, claripy.And(
                render_ok(s, keep), claripy.ULT(i, n), s.regs.r14 == (i + 2).zero_extend(32)))
        else:
            sums += 1
            good = not s.globals.get("ev") and must(s, claripy.And(
                render_ok(s, keep), claripy.UGE(i, n), s.regs.r9 == 0,
                s.regs.r10 == 0, s.regs.rdi == TF + F["LINE"] + 6))
        if not good:
            ok = False
            detail = f"{'row' if s.addr == SYM['render_row'] else 'exit'} path breaks Inv"
            break
    ok = ok and rows > 0 and sums == 1
    record("render_row step: writes one row to fd (1..128 bytes of F_LINE) and "
           "advances, or moves to the total; keeps Inv", ok, detail)


def prove_render_sum():
    # One step from render_sum.  If r10 + 1 < F_N: add one entry to r9 and back
    # to render_sum with r10 + 2, no writes, no syscall.  Otherwise: one
    # write(fd, F_LINE, 1..128) of the total line, then return (F_OUTFD != 1
    # skips the message and prompt), rsp == R + 8, kept registers intact.
    st, n, fd, keep, viol = render_state(SYM["render_sum"])
    j, tot = claripy.BVS("j", 32), claripy.BVS("tot", 32)
    st.regs.r10 = j.zero_extend(32)
    st.regs.r9 = tot.zero_extend(32)
    st.regs.rdi = TF + F["LINE"] + 6
    st.solver.add(claripy.ULE(j, n + 1))
    simgr, why = explore_capped(st, [SYM["render_sum"], RET], max_active=256)
    ok = bool(simgr.found) and not simgr.deadended and not viol and why is None
    detail = f"paths={len(simgr.found)} viol={viol[:3]} cap={why}"
    for s in simgr.found:
        if s.addr == SYM["render_sum"]:
            good = not s.globals.get("ev") and must(s, claripy.And(
                render_ok(s, keep), claripy.ULT(j + 1, n),
                s.regs.r10 == (j + 2).zero_extend(32), s.regs.r9[63:32] == 0,
                s.regs.rdi == TF + F["LINE"] + 6))
        else:
            good = one_line_write(s, fd) and must(s, claripy.And(
                render_ok(s, keep, R + 8), claripy.UGE(j + 1, n)))
        if not good:
            ok = False
            detail = f"{'loop' if s.addr == SYM['render_sum'] else 'return'} path breaks Inv"
            break
    record("render_sum step: adds one entry, or writes the total line to fd and "
           "returns; keeps Inv", ok, detail)


def prove_track_name():
    # From track_name, for any time() result t, reach track_named with
    # F_PATH = "timekeep-" + decimal(t mod 2^32) + ".txt" NUL-terminated and
    # F_PATHLEN its length (14..23, so the name fits F_PATH's 32 bytes).  The
    # only writes are F_PATH, F_PATHLEN and the stack below rsp; the only
    # syscall is time; rbx and rsp are kept.
    old = stub_syscalls(["time"])
    st = frame_state(SYM["track_name"])
    log_events(st)
    path, plen = TF + F["PATH"], TF + F["PATHLEN"]
    viol = watch_mem(st, [(path, path + 32), (plen, plen + 4), (TF - 0x60, TF)])
    simgr, why = explore_capped(st, [SYM["track_named"]])
    proj.simos.syscall_library = old
    ok = len(simgr.found) == 10 and not simgr.deadended and not viol and why is None
    detail = f"paths={len(simgr.found)} viol={viol} cap={why}"
    for s in simgr.found:
        ev = s.globals.get("ev", ())
        if len(ev) != 1 or not must(s, claripy.And(ev[0][1] == 201, ev[0][2] == 0)):
            ok = False; detail = "not exactly one syscall, time(NULL)"; break
        t = s.globals["ret_time"][31:0]
        n = s.solver.eval_upto(fld(s, "PATHLEN"), 2)
        k = n[0] - len(PFX) - len(SFX) if len(n) == 1 else 0
        if not 1 <= k <= 10:
            ok = False; detail = f"F_PATHLEN not a fixed 14..23: {n}"; break
        digits, has_k = dec_cl(t, k)
        want = ([claripy.BVV(c, 8) for c in PFX] + digits
                + [claripy.BVV(c, 8) for c in SFX + b"\0"])
        # one small query per fact rather than one large conjunction
        facts = [has_k, s.regs.rbx == TF, s.regs.rsp == TF] + [
            s.memory.load(path + i, 1) == w for i, w in enumerate(want)]
        if not all(must(s, f) for f in facts):
            ok = False; detail = f"{k}-digit path: wrong name, or rbx/rsp changed"; break
    record("file name: F_PATH = 'timekeep-<time() mod 2^32>.txt', NUL-terminated, "
           "length 14..23; only syscall is time", ok, detail)


def prove_quit_save():
    # From track_quit_save (every quit: q, Ctrl-C, Ctrl-D, end of input), with
    # the frame arbitrary except the key-loop facts F_OUTFD == 1 and F_PATHLEN
    # in 14..23 (prove_track_name), and the kernel returning anything:
    #   * every path ends in exit(0), and the syscalls are exactly
    #       [open(F_PATH, O_WRONLY|O_CREAT|O_TRUNC, 0644),
    #        [render to that fd, close(fd)] if the open succeeded,
    #        write(1, F_MSG, 6 or 12 + F_PATHLEN), write(1, "\n", 1)]
    #         if F_N != 0,
    #       then ioctl(0, TCSETS, F_TERMIOS) if F_RAW != 0, then exit(0);
    #   * outside render, the only writes are F_MSG, F_MSGLEN, F_OUTFD and the
    #     stack below rsp, so F_TERMIOS and F_RAW reach the restore intact.
    # render is RenderFileModel, which the prove_render_* obligations justify.
    old = stub_syscalls(["open", "close", "write", "ioctl"])
    proj.hook(SYM["render"], RenderFileModel(), replace=True)
    st = frame_state(SYM["track_quit_save"])
    n, raw, plen = (claripy.BVS(x, 32) for x in ("n", "raw", "pathlen"))
    st.memory.store(TF + F["N"], n, endness="Iend_LE")
    st.memory.store(TF + F["RAW"], raw, endness="Iend_LE")
    st.memory.store(TF + F["PATHLEN"], plen, endness="Iend_LE")
    st.memory.store(TF + F["OUTFD"], claripy.BVV(1, 32), endness="Iend_LE")
    st.solver.add(claripy.ULE(len(PFX) + 1 + len(SFX), plen),
                  claripy.ULE(plen, len(PFX) + 10 + len(SFX)))
    log_events(st)
    at = lambda f, size: (TF + F[f], TF + F[f] + size)
    viol = watch_mem(st, [at("MSG", 96), at("MSGLEN", 4), at("OUTFD", 4),
                          (TF - 0x100, TF)])
    simgr, why = explore_capped(st, [], max_active=128)
    proj.unhook(SYM["render"])
    proj.simos.syscall_library = old
    ok = bool(simgr.deadended) and not viol and why is None
    detail = f"paths={len(simgr.deadended)} viol={viol} cap={why}"
    nl = SYM["nlstr"]
    for s in simgr.deadended:
        ev = list(s.globals.get("ev", ()))
        sysc = lambda e, nr, *args: e[0] == "sys" and must(s, claripy.And(
            e[1] == nr, *[x == y for x, y in zip(e[2:], args)]))
        bad = None
        # every path must have settled each branch the claim splits on
        for c in ([n == 0, raw == 0] + ([claripy.SLT(s.globals["ret_open"], 0)]
                                         if "ret_open" in s.globals else [])):
            if not (must(s, c) or must(s, claripy.Not(c))):
                bad = f"a path leaves {c} undecided"
        if bad is None and not must(s, n == 0):
            if not ev or not sysc(ev[0], 2, TF + F["PATH"], 0x241, 0x1A4):
                bad = "save does not start with open(F_PATH, 0x241, 0644)"
            else:
                fd = s.globals["ret_open"]
                ev.pop(0)
                if not must(s, claripy.SLT(fd, 0)):
                    if (len(ev) < 2 or ev[0][0] != "render"
                            or not must(s, ev[0][1] == fd[31:0])
                            or not sysc(ev[1], 3, fd[31:0].zero_extend(32))):
                        bad = "open succeeded but not render(fd) then close(fd)"
                    ev = ev[2:]
                msg = claripy.Or(fld(s, "MSGLEN") == plen + 6,
                                 fld(s, "MSGLEN") == plen + 12)
                if bad is None and (len(ev) < 2
                        or not sysc(ev[0], 1, 1, TF + F["MSG"])
                        or not must(s, claripy.And(msg, ev[0][4] == fld(s, "MSGLEN").zero_extend(32)))
                        or not sysc(ev[1], 1, 1, nl, 1)):
                    bad = "save message is not write(1, F_MSG, 6|12+len), write(1, nl, 1)"
                ev = ev[2:]
                if bad is None and not must(s, fld(s, "OUTFD") == 1):
                    bad = "F_OUTFD not back to 1"
        if bad is None and not must(s, raw == 0):
            if not ev or not sysc(ev[0], 16, 0, 0x5402, TF + F["TERMIOS"]):
                bad = "raw tty but no ioctl(0, TCSETS, F_TERMIOS)"
            ev = ev[1:]
        if bad is None and (len(ev) != 1 or not sysc(ev[0], 60, 0)):
            bad = f"does not end in a lone exit(0): {len(ev)} events left"
        if bad:
            ok = False; detail = bad + (f"; viol={viol[:3]}" if viol else ""); break
    record("quit: saves (if F_N != 0) via open/render/close and reports it, then "
           "restores the tty iff raw and exits(0); outside render, writes only "
           "F_MSG/F_MSGLEN/F_OUTFD/stack", ok, detail)


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
    prove_track_name()
    prove_quit_save()

    print("\n-- render into a save file, by induction over cut points --")
    prove_render_prologue()
    prove_render_row()
    prove_render_sum()

    print("\n-- sum-mode loop, by induction over cut points --")
    prove_sum_prologue()
    prove_sum_arg()
    prove_sum_byte()

    print("\n" + "=" * 40)
    n_ok = sum(1 for _, ok, _ in results if ok)
    print(f"{n_ok}/{len(results)} obligations proved")
    sys.exit(0 if n_ok == len(results) else 1)
