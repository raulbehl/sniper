#!/usr/bin/env python3
"""Is every recorded run of the UVM testbench a behaviour of the agent model fvgen generated?

Like check_driver.py (which checks the hand-written axi_slave_driver_fv.sv), for the generated
fv_agent_<agent>.sv: the model's inputs come from the trace, its outputs are assumed equal to what the
UVM testbench drove, and the solver must find choices (delays, picks, stalls, data) that produce the
whole run. Inputs the trace does not record are left free. The model's own assertions (uvm_error,
bounds) are compiled out (FV_MODEL_NO_ASSERT): the only assertion is the end of the trace.
The test's parameter overrides come from the model's report (fv_agent_<agent>.json: test_overrides).

    check_model.py <fv dir> <agent> <trace> <test> [--mutate rid|rlast|rdata] [--where] [--rounds N]
"""
import argparse, json, os, re, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import check_driver as C          # noqa: E402   (trace format, mutations, smtbmc prep)

# trace column (axi_trace_rec.sv) -> interface signal
SIG = {f[0]: (f[0] if f[0] == 'reset_n' else 'axi_' + f[0]) for f in C.FIELDS}


def top_sv(rows, rep, params, ignore=()):
    n = len(rows)
    w = sum(fw for _, fw, _ in C.FIELDS)
    packed = []
    for r in rows:
        v = 0
        for (name, fw, _), x in zip(C.FIELDS, r): v = (v << fw) | (x & ((1 << fw) - 1))
        packed.append(f"{w}'h{v:x}")
    off, sl = w, {}
    for name, fw, _ in C.FIELDS:
        off -= fw
        sl[SIG[name]] = f'row[{off + fw - 1}:{off}]' if fw > 1 else f'row[{off}]'
    sw = max(1, n.bit_length())
    ports = rep['ports']
    conns, eqs, decl = ['.clk(clk)'], [], []
    for sig, pi in sorted(ports.items()):
        if sig == rep['clock']: continue
        if pi['dir'] == 'in':
            if sig in sl: conns.append(f'.{sig}({sl[sig]})')
            else:
                decl.append(f"  `FV_ANYSEQ logic [{pi['w'] - 1}:0] free_{sig};   // not in the trace")
                conns.append(f'.{sig}(free_{sig})')
        else:
            decl.append(f"  wire [{pi['w'] - 1}:0] m_{sig};")
            conns.append(f'.{sig}(m_{sig})')
            if sig in sl and sig not in ignore: eqs.append(f'(m_{sig} == {sl[sig]})')
    conns += ['.fv_bound_hit()', '.fv_uvm_error()']
    p = ', '.join(f'.{k}({v})' for k, v in params.items())
    L = ['`include "fv_tool.svh"', 'module chk (input logic clk);', '  `include "prim_assert.sv"',
         f'  localparam logic [{w - 1}:0] TR [{n}] = ' + "'{" + ', '.join(packed) + '};',
         f"  logic [{sw - 1}:0] step = '0;",
         f"  always_ff @(posedge clk) if (step != {sw}'d{n}) step <= step + 1'b1;",
         f"  wire [{w - 1}:0] row = TR[(step < {sw}'d{n}) ? step : {sw}'d{n - 1}];", *decl,
         f"  {rep['module']} #({p}) u_m ({', '.join(conns)});",
         f"  `ASSUME(same_as_uvm, (step == {sw}'d{n}) || ({' && '.join(eqs)}), clk, 1'b0)",
         "  wire reset = 1'b0;",
         f"  `FV_ASSERT_I(trace_end, (step != {sw}'d{n - 1}), posedge)", 'endmodule']
    return '\n'.join(L) + '\n'


def check(fvdir, agent, trace, test, mutate=None, rows=None, timeout=7200, extra=None, ignore=()):
    rep = json.load(open(os.path.join(fvdir, f'fv_agent_{agent}.json')))
    rows = rows if rows is not None else C.read_trace(trace)
    if mutate: rows = C.mutated(rows, mutate)
    params = dict(rep.get('test_overrides', {}).get(test, {}))
    params.update(extra or {})
    work = tempfile.mkdtemp(prefix='mdlchk_')
    open(os.path.join(work, 'chk.sv'), 'w').write(top_sv(rows, rep, params, ignore))
    model = os.path.join(fvdir, f'fv_agent_{agent}.sv')
    ys = (f'plugin -i slang; read_slang --unroll-limit 1000000 -U SYNTHESIS -DFORMAL -DFV_MODEL_NO_ASSERT -I {fvdir} -I {C.FV} --top chk '
          f'{model} {work}/chk.sv; hierarchy -top chk; proc; setundef -anyseq a:anyseq %ci1:+[Y]; setundef -zero; '
          f'setundef -undriven -anyseq; prep -top chk; '
          f'{C.PREP_BMC}; write_smt2 {work}/m.smt2')
    r = subprocess.run(['yosys', '-q', '-p', ys], cwd=work, capture_output=True, text=True)
    if r.returncode: return 'ERROR', r.stdout[-3000:] + r.stderr[-3000:], len(rows)
    flags = ['--presat', '--unroll'] if os.environ.get('CHECK_UNROLL') else []
    r = subprocess.run(['yosys-smtbmc', '-s', os.environ.get('CHECK_SOLVER', 'boolector'), *flags, '--noprogress',
                        '-t', f'{len(rows)}:{len(rows) + 1}', f'{work}/m.smt2'], cwd=work, capture_output=True,
                       text=True, timeout=timeout)
    if 'Assert failed' in r.stdout: return 'CONTAINED', '', len(rows)
    if 'Status: PASSED' in r.stdout or 'Status: PREUNSAT' in r.stdout: return 'NOT CONTAINED', '', len(rows)
    return 'ERROR', r.stdout[-3000:], len(rows)


def first_divergence(fvdir, agent, trace, test, extra=None):
    rows = C.read_trace(trace)
    lo, hi = 1, len(rows)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if check(fvdir, agent, trace, test, rows=rows[:mid], extra=extra)[0] == 'CONTAINED': lo = mid
        else: hi = mid
    names = [f[0] for f in C.FIELDS]
    return hi - 1, [dict(zip(names, rows[k])) for k in range(max(0, hi - 4), hi)]


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('fvdir'); ap.add_argument('agent'); ap.add_argument('trace'); ap.add_argument('test')
    ap.add_argument('--mutate', choices=['rid', 'rlast', 'rdata'])
    ap.add_argument('--where', action='store_true')
    ap.add_argument('--param', action='append', default=[], help='NAME=VALUE (e.g. ROUNDS=8)')
    a = ap.parse_args()
    extra = dict(x.split('=', 1) for x in a.param)
    if a.where:
        k, ctx = first_divergence(a.fvdir, a.agent, a.trace, a.test, extra)
        print(f'first row the model cannot follow: {k}')
        for i, r in enumerate(ctx): print(f'  row {k - len(ctx) + 1 + i}:', ' '.join(f'{n}={v:x}' for n, v in r.items()))
        sys.exit(0)
    st, why, n = check(a.fvdir, a.agent, a.trace, a.test, a.mutate, extra=extra)
    print(f'{os.path.basename(a.trace):34s} {n:5d} cycles  {st}')
    if why: print(why)
    want = 'CONTAINED' if a.mutate in (None, 'rdata') else 'NOT CONTAINED'
    print(f'expected {want}: {"ok" if st == want else "MISMATCH"}')
    sys.exit(0 if st == want else 1)
