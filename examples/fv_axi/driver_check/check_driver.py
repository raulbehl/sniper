#!/usr/bin/env python3
"""Is every recorded run of the UVM testbench's AXI slave a behaviour of axi_slave_driver_fv?

For a trace (axi_trace_rec.sv: one row per clock edge) the check builds a formal top in which the model's
DUT-side inputs come from the trace, the model's outputs are assumed equal to what the UVM driver drove,
and one cover asks for the last row. Reached: the model can produce exactly this run (its free choices
-- delays, gaps, picks, ready bits, data -- are found by the solver). Unreached: the model cannot, i.e.
it is too strict for what the driver did. The model gets the test's own cfg values as parameters.

    check_driver.py <trace> <test> [--mutate rid|rlast|rdata] [--where]
      --mutate: controls on the first read burst (rid, rlast: must not be contained; rdata: must be)
      --where:  not contained -> the first row the model cannot follow
"""
import argparse, os, re, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FV = os.path.join(os.path.dirname(HERE), 'fv')
MODEL = os.path.join(os.path.dirname(HERE), 'axi_slave_driver_fv.sv')

# trace columns (axi_trace_rec.sv): name, width, direction seen from the model
FIELDS = [('reset_n', 1, 'in'), ('arvalid', 1, 'in'), ('arid', 3, 'in'), ('arlen', 2, 'in'),
          ('awvalid', 1, 'in'), ('awid', 3, 'in'), ('wvalid', 1, 'in'), ('wlast', 1, 'in'),
          ('rready', 1, 'in'), ('bready', 1, 'in'),
          ('arready', 1, 'out'), ('awready', 1, 'out'), ('wready', 1, 'out'), ('rvalid', 1, 'out'),
          ('rid', 3, 'out'), ('rdata', 32, 'out'), ('rlast', 1, 'out'), ('bvalid', 1, 'out'), ('bid', 3, 'out')]

# axi_slave_cfg values per test (uvm_tb/tests/*.svh); the rest keep axi_slave_cfg's defaults
CFG = {'axi_backpressure_test': dict(AR_READY_PCT=25, AW_READY_PCT=25, W_READY_PCT=25, R_GAP_PCT=50),
       'axi_ooo_test': dict(OOO_PCT=80, RD_LAT_MIN=5, RD_LAT_MAX=40, WR_LAT_MIN=5, WR_LAT_MAX=40),
       'axi_stress_test': dict(RD_LAT_MIN=0, RD_LAT_MAX=1, WR_LAT_MIN=0, WR_LAT_MAX=1)}

PREP = ('hierarchy -smtcheck; scc -select; simplemap; select -clear; memory_nordff; async2sync; '
        'chformal -assume -early; opt_clean; formalff -setundef -clk2ff -ff2anyinit -hierarchy; '
        'chformal -live -fair -assert -remove; opt_clean; check; setundef -undriven -anyseq; opt -fast; '
        'opt_clean; hierarchy -smtcheck; delete */t:$print; formalff -assume; dffunmap')


# a bounded proof keeps the assertions (sby's bmc prep removes covers instead)
PREP_BMC = PREP.replace('chformal -live -fair -assert -remove', 'chformal -live -fair -cover -remove')


def read_trace(path):
    rows = []
    for line in open(path):
        f = line.split()
        if len(f) != len(FIELDS): continue
        rows.append([int(x, 16) if 'x' not in x.lower() else 0 for x in f])
    return rows


def top_sv(rows, params, qdepth):
    n = len(rows)
    w = sum(fw for _, fw, _ in FIELDS)
    packed = []
    for r in rows:
        v = 0
        for (name, fw, _), x in zip(FIELDS, r): v = (v << fw) | (x & ((1 << fw) - 1))
        packed.append(f"{w}'h{v:x}")
    off, sl = w, {}
    for name, fw, _ in FIELDS:
        off -= fw
        sl[name] = f'row[{off + fw - 1}:{off}]' if fw > 1 else f'row[{off}]'
    sw = max(1, n.bit_length())
    p = ', '.join(f'.{k}({v})' for k, v in {**params, 'QDEPTH': qdepth}.items())
    ins = ', '.join(f'.{nm}({sl[nm]})' for nm, _, d in FIELDS if d == 'in')
    outs = ', '.join(f'.{nm}(m_{nm})' for nm, _, d in FIELDS if d == 'out')
    L = ['`include "fv_tool.svh"', 'module chk (input logic clk);', '  `include "prim_assert.sv"',
         f'  localparam logic [{w - 1}:0] TR [{n}] = ' + "'{" + ', '.join(packed) + '};',
         f"  logic [{sw - 1}:0] step = '0;",
         f"  always_ff @(posedge clk) if (step != {sw}'d{n}) step <= step + 1'b1;",
         f"  wire [{w - 1}:0] row = TR[(step < {sw}'d{n}) ? step : {sw}'d{n - 1}];"]
    L += [f'  wire [{fw - 1}:0] m_{nm};' for nm, fw, d in FIELDS if d == 'out']
    L.append(f'  axi_slave_driver_fv #({p}) u_drv (.clk, {ins}, {outs}, .q_overflow());')
    eq = ' && '.join(f'(m_{nm} == {sl[nm]})' for nm, _, d in FIELDS if d == 'out')
    # the cover sees the previous step's values (a clocked check): it fires one step after the last row,
    # a step the trace no longer constrains
    L.append(f"  `ASSUME(same_as_uvm, (step == {sw}'d{n}) || ({eq}), clk, 1'b0)")
    L.append("  wire reset = 1'b0;")
    # 'the last row is never reached' as an assertion: a counterexample is the model producing the whole trace
    # (a bounded check at the final step only, instead of a cover check at every step)
    L.append(f"  `FV_ASSERT_I(trace_end, (step != {sw}'d{n - 1}), posedge)")
    L.append('endmodule')
    return '\n'.join(L) + '\n'


COL = {f[0]: i for i, f in enumerate(FIELDS)}


def mutated(rows, kind):
    """controls on the first read burst: 'rid' (answered with an id never requested) and 'rlast' (rlast
    missing on its last beat) must not be contained; 'rdata' (other data: memory contents are free in the
    model) must still be contained"""
    rows = [list(r) for r in rows]
    beats = [k for k, r in enumerate(rows) if r[COL['rvalid']]]
    if not beats: raise SystemExit('no read beat in the trace')
    first_rid = rows[beats[0]][COL['rid']]
    burst = []
    for k in beats:
        if rows[k][COL['rid']] != first_rid: break
        burst.append(k)
        if rows[k][COL['rlast']]: break
    if kind == 'rid':
        requested = {r[COL['arid']] for r in rows if r[COL['arvalid']]}
        other = next(i for i in range(8) if i not in requested)
        for k in burst: rows[k][COL['rid']] = other
    elif kind == 'rlast':
        rows[burst[-1]][COL['rlast']] = 0
    elif kind == 'rdata':            # rdata holds after a burst (the driver never clears it): from the burst on
        for k in range(burst[0], len(rows)): rows[k][COL['rdata']] ^= 0xdeadbeef
    else: raise SystemExit(f'--mutate {kind}: rid | rlast | rdata')
    return rows


def check(trace, test, mutate=None, qdepth=8, timeout=3600, rows=None):
    rows = rows if rows is not None else read_trace(trace)
    if mutate is not None: rows = mutated(rows, mutate)
    work = tempfile.mkdtemp(prefix='drvchk_')
    open(os.path.join(work, 'chk.sv'), 'w').write(top_sv(rows, CFG.get(test, {}), qdepth))
    ys = (f'plugin -i slang; read_slang -U SYNTHESIS -DFORMAL -I {FV} --top chk {MODEL} {work}/chk.sv; '
          # yosys-slang keeps (* anyseq *) only as an attribute: free the undriven choice wires before prep folds them
          f'hierarchy -top chk; proc; setundef -undriven -anyseq; prep -top chk; {PREP_BMC}; write_smt2 {work}/m.smt2')
    r = subprocess.run(['yosys', '-q', '-p', ys], cwd=work, capture_output=True, text=True)
    if r.returncode: return 'ERROR', r.stdout[-2000:] + r.stderr[-2000:], len(rows)
    r = subprocess.run(['yosys-smtbmc', '-s', 'yices', '--presat', '--unroll', '--noprogress',
                        '-t', f'{len(rows)}:{len(rows) + 1}', f'{work}/m.smt2'], cwd=work, capture_output=True, text=True, timeout=timeout)
    if 'Assert failed' in r.stdout: return 'CONTAINED', '', len(rows)
    # PREUNSAT: no choice sequence of the model agrees with the trace up to the end
    if 'Status: PASSED' in r.stdout or 'Status: PREUNSAT' in r.stdout: return 'NOT CONTAINED', '', len(rows)
    return 'ERROR', r.stdout[-2000:], len(rows)


def first_divergence(trace, test, qdepth=8):
    """the first row the model cannot follow (shortest prefix that is not contained)"""
    rows = read_trace(trace)
    lo, hi = 1, len(rows)           # prefix of lo rows contained, of hi rows not
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if check(trace, test, qdepth=qdepth, rows=rows[:mid])[0] == 'CONTAINED': lo = mid
        else: hi = mid
    names = [f[0] for f in FIELDS]
    return hi - 1, [dict(zip(names, rows[k])) for k in range(max(0, hi - 4), hi)]


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('trace'); ap.add_argument('test')
    ap.add_argument('--mutate', choices=['rid', 'rlast', 'rdata']); ap.add_argument('--qdepth', type=int, default=8)
    ap.add_argument('--where', action='store_true', help='not contained: the first row the model cannot follow')
    a = ap.parse_args()
    if a.where:
        k, ctx = first_divergence(a.trace, a.test, a.qdepth)
        print(f'first row the model cannot follow: {k}')
        for i, r in enumerate(ctx): print(f'  row {k - len(ctx) + 1 + i}:', ' '.join(f'{n}={v:x}' for n, v in r.items()))
        sys.exit(0)
    st, why, n = check(a.trace, a.test, a.mutate, a.qdepth)
    print(f'{os.path.basename(a.trace):34s} {n:5d} cycles  {st}')
    if why: print(why)
    want = 'CONTAINED' if a.mutate in (None, 'rdata') else 'NOT CONTAINED'
    print(f'expected {want}: {"ok" if st == want else "MISMATCH"}')
    sys.exit(0 if st == want else 1)
