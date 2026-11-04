#!/usr/bin/env python3
"""fvgen tests.

  T1  expression translation: every constraint expression below is proven equal, for all inputs, to the
      same SV expression elaborated by yosys-slang (independent of fvgen's translator)
  T2  whole constraint sets vs Verilator's constraint solver, both directions: every solution Verilator
      finds satisfies the generated assumes, and tuples the assumes accept are accepted by Verilator
  T3  coverage: bins and transition monitors vs a reference model of LRM 19.5 on random sample traces

    python tests/test_fvgen.py          (or pytest tests/)
Needs yosys with the slang plugin, sby, verilator, iverilog.
"""
import json, os, random, re, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

PKG = '''
package fvt_pkg;
  typedef enum logic [2:0] {E0 = 0, E1 = 1, E3 = 3, E6 = 6} e_t;
  typedef enum logic [1:0] {M0, M1, M2} m_t;
  typedef struct packed { logic [3:0] hi; m_t mid; logic lo; } ps_t;
endpackage
'''

# (declaration, packed width, signed) of each variable; arrays are unpacked [4] of 4 bits
VARS = [
    ('bit [7:0] a', 'a', 8), ('bit [7:0] b', 'b', 8), ('bit signed [7:0] sa', 'sa', 8),
    ('bit signed [7:0] sb', 'sb', 8), ('int i', 'i', 32), ('int unsigned u', 'u', 32),
    ('bit [3:0] n', 'n', 4), ('logic [15:0] h', 'h', 16), ('e_t e', 'e', 3), ('ps_t ps', 'ps', 7),
    ('bit [3:0] arr [4]', 'arr', 16), ('bit [2:0][3:0] pk', 'pk', 12), ('byte sbyte', 'sbyte', 8),
    ('shortint shi', 'shi', 16), ('bit [0:7] asc', 'asc', 8),
]

EXPRS = [
    'a + b > 8\'d200', 'a + b == 9\'d300', 'sa < sb', 'sa < b', 'i < u', 'i + a > 5', '(a * b) % 7 == 3',
    'a ** 2 < 100', '2 ** n > 100', '2 ** sb == 64', '(a >> n) == 1', '(sa >>> 1) == -8\'sd3', '(a << 2) > 100', '-sa > 3', '~a == 8\'h0F',
    '&a[3:0]', '|h', '^a', '!a', 'a[3]', 'a[n % 8]', 'h[a[3:0] +: 4] == 5', 'h[15 -: 8] == a',
    '{a, b} == 16\'h1234', '{2{n}} == 8\'h55', '(a > 3 ? b : a) < 10', 'a inside {[1:5], 9, [200:$]}',
    'sa inside {[-5:5]}', 'e inside {E1, E3}', 'e != E3',
    'ps.mid == M2', 'ps.hi > 3', 'ps.lo', 'arr[1] == arr[2] + 1', 'arr[n % 4] > 2', 'pk[1] == 4\'h3',
    'asc[0] == 1', 'asc[2:4] == 3\'b101', '$countones(a) == 3', '$countones(h) > i', '(a > b) == (sa > sb)',
    'i == -1', 'u == -1', 'sbyte < 0', 'shi + sbyte > -100', 'a - b > 250', 'int\'(a) - int\'(b) < -5',
    '8\'(a + b) < 10', 'signed\'(a) < 0', 'unsigned\'(sa) > 128', 'i % 3 == u % 3', 'sa / 3 == -2',
    'i >= -10 && i <= 10', 'u - 1 > u', 'h[a[2:0]]', 'asc[n % 8]', '{sa, a} > 0', 'e inside {[E1:E6]}',
    'ps inside {[7\'h10:7\'h20]}', '(ps.hi ^ n) != 0', 'a != b || i != 0', '(n + 4\'d15) == 4\'d2',
    '(n + 4\'d15) == 5\'d18', 'b inside {arr}', 'a inside {[b:8\'d200]}', 'i inside {[-3:$]}',
]


# yosys-slang does not elaborate '$' in an open range; for the reference it is the type's maximum
REF_TEXT = {"a inside {[1:5], 9, [200:$]}": "a inside {[1:5], 9, [200:255]}",
            "i inside {[-3:$]}": "i inside {[-3:2147483647]}",
            # nor an unpacked array in a range list: x inside {arr} is membership in its elements
            # and only constant items: write membership in non-constant items as comparisons
            "b inside {arr}": "(b == arr[0] || b == arr[1] || b == arr[2] || b == arr[3])",
            "a inside {[b:8'd200]}": "(a >= b && a <= 8'd200)",
            # and the SMT backend has no $pow: 2**n / a**2 written out
            "a ** 2 < 100": "a * a < 100",
            "2 ** n > 100": "(n >= 0 ? (32'sd1 << n) : 0) > 100",
            "2 ** sb == 64": "(sb >= 0 ? (32'sd1 << sb) : 32'sd0) == 64"}


def run(cmd, cwd=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f'{cmd[0]} failed:\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}')
    return r


def sniper_fvgen(sv_text, top, work, extra=()):
    src = os.path.join(work, 'probe.sv')
    open(src, 'w').write(sv_text)
    js = os.path.join(work, 'probe.json')
    run([PY, os.path.join(ROOT, 'sniper.py'), '--own', work, '-o', js, '--', src, *(['--top', top] if top else [])])
    d = json.load(open(js))
    assert d['meta']['errors'] == 0, d['meta']['error_list']
    out = os.path.join(work, 'fv')
    run([PY, os.path.join(ROOT, 'fvgen.py'), js, '-o', out, '--expose', *extra])
    return d, json.load(open(os.path.join(out, 'report.json'))), out


def test_expression_equivalence():
    work = tempfile.mkdtemp(prefix='fvt1_')
    cls = 'class eq_c;\n' + ''.join(f'  rand {d};\n' for d, _, _ in VARS)
    cls += ''.join(f'  constraint k{j} {{ {e}; }}\n' for j, e in enumerate(EXPRS)) + 'endclass\n'
    sv = PKG + 'package fvt_cls;\n  import fvt_pkg::*;\n' + cls + 'endpackage\n'
    d, rep, out = sniper_fvgen(sv, None, work)
    order = rep['atom_order']['fv_c_eq_c']
    pos = {}
    for i, a in enumerate(order):
        if a['kind'] == 'item': pos.setdefault(a['block'], []).append(i)
    missing = [j for j in range(len(EXPRS)) if f'k{j}' not in pos]
    unsupported = [e for e in rep['entries'] if e.get('status') == 'unsupported']
    assert not unsupported, unsupported
    assert not missing, [EXPRS[j] for j in missing]
    # reference: the same expressions elaborated by slang, over the same packed bits
    ref = [PKG, 'module ref_eq import fvt_pkg::*; (']
    ref.append(',\n'.join(f'  input logic [{w - 1}:0] {nm}_f' for _, nm, w in VARS) + f',\n  output logic [{len(EXPRS) - 1}:0] r);')
    for decl, nm, w in VARS:
        if nm == 'arr':
            ref.append('  bit [3:0] arr [4];')
            ref.append('  always_comb for (int p = 0; p < 4; p++) arr[p] = arr_f[p*4 +: 4];')
        else:
            ref.append(f'  {decl};')
            ref.append(f'  assign {nm} = {decl.split()[0] if decl.split()[0] in ("e_t", "ps_t") else "type(" + nm + ")"}\'({nm}_f);')
    for j, e in enumerate(EXPRS):
        ref.append(f'  assign r[{j}] = |({REF_TEXT.get(e, e)});')
    ref.append('endmodule')
    open(os.path.join(out, 'ref.sv'), 'w').write('\n'.join(ref) + '\n')
    fv_ports = {nm for nm in re.findall(r'input logic (?:signed )?\[\d+:0\] (\w+)', open(os.path.join(out, 'fv_constraints.sv')).read())}
    mit = ['module miter (input logic clk);']
    for _, nm, w in VARS:
        mit.append(f'  (* anyconst *) logic [{w - 1}:0] {nm};')
    mit.append(f'  logic [{len(EXPRS) - 1}:0] r;')
    mit.append(f'  logic [{len(order) - 1}:0] at;')
    mit.append('  ref_eq u_ref (' + ', '.join(f'.{nm}_f({nm})' for _, nm, _ in VARS) + ', .r(r));')
    conns = ['.clk(clk)', ".reset(1'b1)", ".en(1'b0)"] + [f'.{nm}({nm})' for _, nm, _ in VARS if nm in fv_ports]
    mit.append('  fv_c_eq_c u_fv (' + ', '.join(conns) + ', .fv_atoms(at));')
    # division by zero is x in both, compared as different values: keep divisors non-zero
    mit.append('  always_comb begin')
    for j in range(len(EXPRS)):
        mit.append(f'    eq_{j}: assert (r[{j}] == at[{pos[f"k{j}"][0]}]);')
    mit.append('  end')
    mit.append('endmodule')
    open(os.path.join(out, 'miter.sv'), 'w').write('\n'.join(mit) + '\n')
    # inputs are anyconst and the logic is combinational: one BMC step covers every input value
    open(os.path.join(out, 'eq.sby'), 'w').write('''[options]
mode bmc
depth 1
[engines]
smtbmc yices
[script]
plugin -i slang
read_slang ref.sv --top ref_eq
read_verilog -sv -formal -DFORMAL fv_constraints.sv miter.sv
prep -top miter
[files]
ref.sv
fv_constraints.sv
miter.sv
prim_assert.sv
prim_assert_yosys_macros.svh
''')
    r = run(['sby', '-f', 'eq.sby'], cwd=out, check=False)
    log = r.stdout + r.stderr
    if 'DONE (PASS' not in log:
        fails = sorted({int(x) for x in re.findall(r'Assert failed in miter: .*?eq_(\d+)', log)})
        raise AssertionError(f'translation differs from slang for: {[EXPRS[j] for j in fails] or "?"}\n{log[-4000:]}')
    return len(EXPRS)


T2_CLASS = '''
class t2_c;
  rand bit [3:0] a, b;
  rand bit [2:0] c;
  rand e_t e;
  rand bit [3:0] arr [4];
  rand byte unsigned q [];
  rand bit [3:0] u1, u2, u3;
  rand bit flag;
  int unsigned lim = 9;
  constraint c1 { a < b; b inside {[2:12]}; }
  constraint c2 { flag -> a == 1; if (c > 4) b != 7; else b < 10; }
  constraint c3 { foreach (arr[i]) arr[i] > i; arr.sum() with (int'(item)) < 30; }
  constraint c4 { q.size() inside {[1:3]}; foreach (q[j]) q[j] < 5; }  // (no sum() here: the reference solver cannot solve it on a dynamic array)
  constraint c5 { unique {u1, u2, u3}; e dist {E1 := 2, E3 := 0, E6 :/ 1}; }
  constraint c6 { a <= lim; }
  constraint c7 { soft c == 3; (c == 3) -> u1 < 8; }
endclass
'''
T2_SCALARS = ['a', 'b', 'c', 'e', 'u1', 'u2', 'u3', 'flag']
T2_W = {'a': 4, 'b': 4, 'c': 3, 'e': 3, 'u1': 4, 'u2': 4, 'u3': 4, 'flag': 1}
DUMMY_MACROS = ''.join(f'`define {m}(__name, __prop, __clk = clk, __rst = reset)\n'
                       for m in ('ASSERT', 'ASSUME', 'COVER', 'ASSERT_NEVER', 'ASSERT_KNOWN'))
DUMMY_MACROS += '`define ASSERT_I(__name, __prop)\n`define ASSUME_I(__name, __prop)\n'


def test_constraints_vs_solver(n_solver=300, n_candidates=6000, seed=1):
    work = tempfile.mkdtemp(prefix='fvt2_')
    sv = PKG + 'package fvt_cls;\n  import fvt_pkg::*;\n' + T2_CLASS + 'endpackage\n'
    d, rep, out = sniper_fvgen(sv, None, work)
    order = rep['atom_order']['fv_c_t2_c']
    nat = len(order)
    cons = open(os.path.join(out, 'fv_constraints.sv')).read()
    qmax = int(re.search(r'input logic \[(\d+):0\] q,', cons).group(1)) + 1
    qmax //= 8
    assert qmax == 3, qmax          # size() <= 3 is inferred from the constraints
    open(os.path.join(out, 'prim_assert_dummy_macros.svh'), 'w').write(DUMMY_MACROS)
    rng = random.Random(seed)
    # candidates: biased random tuples (small values hit the tight constraints)
    def rv(w):
        return rng.choice([rng.randrange(1 << w), rng.randrange(min(1 << w, 6))])
    cands = []
    for _ in range(n_candidates):
        cands.append({**{s: rv(T2_W[s]) for s in T2_SCALARS},
                      'arr': [rng.choice([rng.randrange(16), rng.randrange(1, 8)]) for _ in range(4)],
                      'q': [rng.randrange(6) for _ in range(rng.randrange(0, 5))]})
    with open(os.path.join(out, 'cands.txt'), 'w') as f:
        for c in cands:
            f.write(' '.join(str(c[s]) for s in T2_SCALARS) + ' ' + ' '.join(map(str, c['arr'])) +
                    f" {len(c['q'])} " + ' '.join(map(str, c['q'] + [0] * (4 - len(c['q'])))) + '\n')
    tb = f'''
module tb;
  import fvt_pkg::*;
  import fvt_cls::*;
  logic [3:0] a, b, u1, u2, u3; logic [2:0] c; logic [2:0] e; logic flag;
  logic [15:0] arr; logic [{qmax * 8 - 1}:0] q; logic signed [31:0] q__size; logic [31:0] lim;
  logic [{nat - 1}:0] at, at0;
  fv_c_t2_c u_fv (.clk(1'b0), .reset(1'b0), .en(1'b1), .a, .b, .c, .e, .u1, .u2, .u3, .flag, .arr, .q,
                  .q__size, .lim, .fv_atoms(at));
  // pinning every field leaves no room for a soft constraint: SV drops it, so compare with SOFT_EN=0
  fv_c_t2_c #(.SOFT_EN(1'b0)) u_fv0 (.clk(1'b0), .reset(1'b0), .en(1'b1), .a, .b, .c, .e, .u1, .u2, .u3, .flag,
                  .arr, .q, .q__size, .lim, .fv_atoms(at0));
  function automatic bit soft_ok(logic [{nat - 1}:0] v);   // atoms that are not soft
    return (v | SOFT_MASK) === '1;
  endfunction
  localparam logic [{nat - 1}:0] SOFT_MASK = {nat}'b{''.join('1' if a['soft'] else '0' for a in reversed(order))};
  // The reference solver unrolls foreach over a dynamic array with the size it had before randomize():
  // pin on a fresh object whose array already has the pinned size
  function automatic bit pin(t2_c o0, int va, vb, vc, ve, vu1, vu2, vu3, vf, bit [3:0] ra [4], byte unsigned rq [], int qs);
    t2_c o = new;
    o.q = new[qs];
    return o.randomize() with {{ a == va; b == vb; c == vc; e == e_t'(ve); u1 == vu1; u2 == vu2; u3 == vu3;
                                flag == vf; foreach (arr[i]) arr[i] == ra[i]; q.size() == qs;
                                foreach (q[j]) q[j] == rq[j]; }};
  endfunction
  task automatic load(t2_c o);
    a = o.a; b = o.b; c = o.c; e = o.e; u1 = o.u1; u2 = o.u2; u3 = o.u3; flag = o.flag; lim = o.lim;
    for (int p = 0; p < 4; p++) arr[p*4 +: 4] = o.arr[p];
    q = '0; q__size = o.q.size();
    for (int p = 0; p < o.q.size() && p < {qmax}; p++) q[p*8 +: 8] = o.q[p];
  endtask
  initial begin
    t2_c o = new;
    int fd, n_bad = 0, n_acc = 0, n_rej_ok = 0, n_rej_bad = 0;
    int va, vb, vc, ve, vu1, vu2, vu3, vf, ar0, ar1, ar2, ar3, qs, q0, q1, q2, q3;
    // 1. every solver solution satisfies every assume
    for (int k = 0; k < {n_solver}; k++) begin
      if (!o.randomize()) begin $display("SOLVER_FAIL"); $finish; end
      load(o); #1;
      if (at !== '1) begin n_bad++; $display("OVER %0d a=%0d b=%0d c=%0d e=%0d u=%0d,%0d,%0d f=%0d qs=%0d at=%b", k, a, b, c, e, u1, u2, u3, flag, o.q.size(), at); end
    end
    $display("SOLUTIONS_CHECKED %0d violations %0d", {n_solver}, n_bad);
    // 2. tuples the assumes accept are legal for the solver; rejected ones are illegal
    fd = $fopen("cands.txt", "r");
    while ($fscanf(fd, "%d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d %d", va, vb, vc, ve, vu1, vu2, vu3, vf,
                   ar0, ar1, ar2, ar3, qs, q0, q1, q2, q3) == 17) begin
      bit [3:0] ra [4];
      byte unsigned rq [];
      bit ok;
      ra = '{{ar0[3:0], ar1[3:0], ar2[3:0], ar3[3:0]}};
      rq = new[qs];
      foreach (rq[j]) rq[j] = (j == 0) ? q0 : (j == 1) ? q1 : (j == 2) ? q2 : q3;
      a = va; b = vb; c = vc; e = ve; u1 = vu1; u2 = vu2; u3 = vu3; flag = vf; lim = o.lim;
      for (int p = 0; p < 4; p++) arr[p*4 +: 4] = ra[p];
      q = '0; q__size = qs;
      for (int p = 0; p < qs && p < {qmax}; p++) q[p*8 +: 8] = rq[p];
      #1;
      ok = pin(o, va, vb, vc, ve, vu1, vu2, vu3, vf, ra, rq, qs);
      // The reference solver pre-picks a dist bucket by weight, then fails a pin outside it (LRM 18.5.4: dist only
      // restricts the legal set): confirm a refusal on fresh objects
      for (int t = 0; t < 25 && !ok && soft_ok(at0); t++) begin t2_c o2 = new; ok = pin(o2, va, vb, vc, ve, vu1, vu2, vu3, vf, ra, rq, qs); end
      if (soft_ok(at0)) begin n_acc++; if (!ok) begin n_bad++; $display("UNDER a=%0d b=%0d c=%0d e=%0d u=%0d,%0d,%0d f=%0d arr=%0d,%0d,%0d,%0d q=%0d:%0d,%0d,%0d", va, vb, vc, ve, vu1, vu2, vu3, vf, ar0, ar1, ar2, ar3, qs, q0, q1, q2); end end
      else if (ok) begin n_rej_bad++; $display("REJECTED_LEGAL a=%0d b=%0d c=%0d e=%0d u=%0d,%0d,%0d f=%0d qs=%0d at=%b", va, vb, vc, ve, vu1, vu2, vu3, vf, qs, at0); end
      else n_rej_ok++;
    end
    // 3. boundary candidates: a solver solution with one field nudged (+-1 or random)
    for (int k = 0; k < {n_solver * 4}; k++) begin
      bit [3:0] ra [4];
      byte unsigned rq [];
      bit ok;
      int f, dv;
      void'(o.randomize());
      va = o.a; vb = o.b; vc = o.c; ve = o.e; vu1 = o.u1; vu2 = o.u2; vu3 = o.u3; vf = o.flag;
      foreach (ra[p]) ra[p] = o.arr[p];
      rq = o.q; qs = o.q.size();
      f = $urandom_range(0, 11); dv = $urandom_range(0, 2) == 0 ? $urandom_range(0, 15) : -1 + 2 * $urandom_range(0, 1);
      case (f)
        0: va = (dv > 1) ? dv : (va + dv) & 15;   1: vb = (dv > 1) ? dv : (vb + dv) & 15;
        2: vc = (dv > 1) ? dv & 7 : (vc + dv) & 7;  3: ve = (dv > 1) ? dv & 7 : (ve + dv) & 7;
        4: vu1 = (dv > 1) ? dv : (vu1 + dv) & 15; 5: vu2 = (dv > 1) ? dv : (vu2 + dv) & 15;
        6: vu3 = vu1;                              7: vf = !vf;
        8: begin int p = $urandom_range(0, 3); ra[p] = (dv > 1) ? dv : ra[p] + dv; end
        9: begin qs = (qs + dv) & 3; rq = new[qs](rq); end
        10: if (qs > 0) begin int p = $urandom_range(0, qs - 1); rq[p] = (dv > 1) ? dv : rq[p] + dv; end
        default: ;
      endcase
      a = va; b = vb; c = vc; e = ve; u1 = vu1; u2 = vu2; u3 = vu3; flag = vf;
      for (int p = 0; p < 4; p++) arr[p*4 +: 4] = ra[p];
      q = '0; q__size = qs;
      for (int p = 0; p < qs && p < {qmax}; p++) q[p*8 +: 8] = rq[p];
      #1;
      ok = pin(o, va, vb, vc, ve, vu1, vu2, vu3, vf, ra, rq, qs);
      for (int t = 0; t < 25 && !ok && soft_ok(at0); t++) ok = pin(o, va, vb, vc, ve, vu1, vu2, vu3, vf, ra, rq, qs);
      if (soft_ok(at0)) begin n_acc++; if (!ok) begin n_bad++; $display("UNDER(mut %0d) a=%0d b=%0d c=%0d e=%0d u=%0d,%0d,%0d f=%0d qs=%0d", f, va, vb, vc, ve, vu1, vu2, vu3, vf, qs); end end
      else if (ok) begin n_rej_bad++; $display("REJECTED_LEGAL(mut %0d) a=%0d b=%0d c=%0d e=%0d u=%0d,%0d,%0d f=%0d qs=%0d at=%b", f, va, vb, vc, ve, vu1, vu2, vu3, vf, qs, at0); end
      else n_rej_ok++;
    end
    $display("CANDIDATES accepted %0d (illegal per solver: %0d) rejected %0d (legal per solver: %0d)",
             n_acc, n_bad, n_rej_ok + n_rej_bad, n_rej_bad);
    $finish;
  end
endmodule
'''
    open(os.path.join(out, 'tb.sv'), 'w').write(tb)
    open(os.path.join(out, 'pkg.sv'), 'w').write(sv)
    run(['verilator', '--binary', '--timing', '-Wno-fatal', '-Wno-lint', '-Wno-style', '-I.', '--top-module', 'tb',
         'pkg.sv', 'fv_constraints.sv', 'tb.sv', '-o', 'vtb'], cwd=out)
    r = run([os.path.join(out, 'obj_dir', 'vtb')], cwd=out)
    log = r.stdout
    m1 = re.search(r'SOLUTIONS_CHECKED (\d+) violations (\d+)', log)
    m2 = re.search(r'CANDIDATES accepted (\d+) \(illegal per solver: (\d+)\) rejected (\d+) \(legal per solver: (\d+)\)', log)
    assert m1 and m2, log[-3000:]
    bad = [l for l in log.splitlines() if l.startswith(('OVER', 'UNDER', 'REJECTED_LEGAL'))]
    assert int(m1.group(2)) == 0 and int(m2.group(2)) == 0 and int(m2.group(4)) == 0, '\n'.join(bad[:20])
    assert int(m2.group(1)) >= 300, f'too few accepted candidates to test the under-constraint direction: {m2.group(1)}'
    return int(m1.group(1)), int(m2.group(1)), int(m2.group(3))


T3_CLASS = '''
class cov_c;
  covergroup cg with function sample(bit [3:0] x, bit [2:0] y, bit signed [3:0] s, e_t z);
    cp_x : coverpoint x {
      bins lo = {[0:3]};
      bins mid[] = {[4:6]};
      bins hi[3] = {[7:15]};
      ignore_bins ig = {5, 13};
      illegal_bins il = {15};
      wildcard bins w = {4'b1?1?};
      bins t1 = (1 => 2 => 3);
      bins t2 = (4 [*2:3] => 6);
      bins t3 = (6 [-> 2] => 7);
      bins t4 = (8 [= 2] => 9);
      bins tarr[] = (1, 2 => 3, 4);
      bins dflt = default;
    }
    cp_y : coverpoint y;
    cp_s : coverpoint s iff (y != 0) { bins neg = {[-8:-1]}; bins pos = {[1:$]}; bins zero = {0}; }
    cp_w : coverpoint x { bins b[] = {[0:15]} with (item % 3 == 0); }
    cp_z : coverpoint z;
    x_xy : cross cp_x, cp_y {
      ignore_bins i1 = binsof(cp_y) intersect {[6:7]};
      bins sel = binsof(cp_x.lo) && binsof(cp_y) intersect {0};
      illegal_bins il2 = binsof(cp_x.mid) intersect {6} && binsof(cp_y) intersect {5};
      ignore_bins i2 = binsof(cp_x.lo) with (cp_x == 2);
      illegal_bins il3 = binsof(cp_x.w) with (cp_y == 3);
    }
  endgroup
  function new(); cg = new(); endfunction
endclass
'''
ENUM_Z = {'E0': 0, 'E1': 1, 'E3': 3, 'E6': 6}


def t3_reference(trace):
    """per cycle: the set of covers hit, from the LRM (19.5 bins, 19.5.2 transitions, 19.6 crosses)"""
    excl = {5, 13, 15}                              # ignore + illegal values leave every value bin
    hi_parts = [list(range(7, 10)), list(range(10, 13)), list(range(13, 16))]   # 9 values / 3 bins

    def x_bins(x):
        out = []
        if x in range(0, 4) and x not in excl: out.append('lo')
        for v in (4, 6):
            if x == v: out.append(f'mid__{v}')
        for i, part in enumerate(hi_parts):
            if x in part and x not in excl: out.append(f'hi__{i}')
        if (x & 0b1010) == 0b1010 and x not in excl: out.append('w')
        return out
    # transitions as regexes over the sampled values (one char per sample)
    ch = lambda vs: '[' + ''.join(chr(65 + v) for v in vs) + ']'
    nch = lambda vs: '[^' + ''.join(chr(65 + v) for v in vs) + ']'
    trans = {'t1': ch([1]) + ch([2]) + ch([3]),
             't2': ch([4]) + '{2,3}' + ch([6]),
             't3': '(?:' + nch([6]) + '*' + ch([6]) + '){2}' + ch([7]),
             't4': '(?:' + nch([8]) + '*' + ch([8]) + '){2}' + nch([8]) + '*' + ch([9])}
    for a in (1, 2):
        for b in (3, 4):
            trans[f'tarr__{a}_{b}'] = ch([a]) + ch([b])
    hist = ''
    hits = []
    for smp, x, y, s, z in trace:
        h = set()
        if smp:
            hist += chr(65 + x)
            xb = x_bins(x)
            for b in xb: h.add(f'cov_cp_x__{b}')
            for t, rx in trans.items():
                if re.search('(?:' + rx + ')$', hist): h.add(f'cov_cp_x__{t}')
            if not xb and x not in excl: h.add('cov_cp_x__dflt')
            h.add(f'cov_cp_y__auto_{y}')
            if y != 0:
                h.add('cov_cp_s__neg' if s < 0 else 'cov_cp_s__pos' if s > 0 else 'cov_cp_s__zero')
            if x % 3 == 0: h.add(f'cov_cp_w__b__{x}')
            h.add('cov_cp_z__auto_' + next(k for k, v in ENUM_Z.items() if v == z))
            for b in xb:                            # cross products (transition / default bins are not crossed)
                if y in (6, 7): continue            # ignore_bins i1
                if b == 'mid__6' and y == 5: continue     # illegal_bins il2
                if b == 'lo' and x == 2: continue         # ignore_bins i2 (with filter: value combinations)
                if b == 'w' and y == 3: continue          # illegal_bins il3 (with filter)
                if b == 'lo' and y == 0: h.add('cov_x_xy__sel'); continue
                h.add(f'cov_x_xy__{b}__auto_{y}')
        hits.append(h)
    return hits


def test_coverage_vs_reference(n=4000, seed=3):
    work = tempfile.mkdtemp(prefix='fvt3_')
    sv = PKG + 'package fvt_cls;\n  import fvt_pkg::*;\n' + T3_CLASS + 'endpackage\n'
    d, rep, out = sniper_fvgen(sv, None, work)
    mod = 'fv_cg_cov_c__cg'
    order = rep['hit_order'][mod]
    unsup = [e for e in rep['entries'] if e.get('status') == 'unsupported']
    assert not unsup, unsup
    rng = random.Random(seed)
    trace = []
    for _ in range(n):
        # small alphabets make transitions likely
        x = rng.choice([rng.randrange(16), rng.choice([1, 2, 3, 4, 4, 6, 6, 7, 8, 8, 9])])
        trace.append((rng.random() < 0.8, x, rng.randrange(8), rng.randrange(-8, 8), rng.choice(list(ENUM_Z.values()))))
    with open(os.path.join(out, 'trace.txt'), 'w') as f:
        for smp, x, y, s, z in trace: f.write(f'{int(smp)} {x} {y} {s & 15} {z}\n')
    open(os.path.join(out, 'prim_assert_dummy_macros.svh'), 'w').write(DUMMY_MACROS)
    nh = len(order)
    tb = f'''
module tb;
  logic clk = 0, sample; logic [3:0] x; logic [2:0] y; logic signed [3:0] s; logic [2:0] z;
  logic [{nh - 1}:0] hits;
  {mod} u (.clk, .reset(1'b0), .sample, .x, .y, .s, .z, .fv_hits(hits));
  initial begin
    int fd, a, b, c, dd, e;
    fd = $fopen("trace.txt", "r");
    while ($fscanf(fd, "%d %d %d %d %d", a, b, c, dd, e) == 5) begin
      sample = a; x = b; y = c; s = dd[3:0]; z = e;
      #1; $display("H %b", hits);
      clk = 1; #1; clk = 0;
    end
    $finish;
  end
endmodule
'''
    open(os.path.join(out, 'tb.sv'), 'w').write(tb)
    run(['verilator', '--binary', '--timing', '-Wno-fatal', '-Wno-lint', '-Wno-style', '-I.', '--top-module', 'tb',
         'fv_covers.sv', 'tb.sv', '-o', 'vtb'], cwd=out)
    r = run([os.path.join(out, 'obj_dir', 'vtb')], cwd=out)
    got = []
    for line in r.stdout.splitlines():
        if line.startswith('H '):
            bits = line[2:].strip()[::-1]           # LSB first = hit_order
            got.append({order[i] for i, b in enumerate(bits) if b == '1'})
    want = t3_reference(trace)
    assert len(got) == len(want), (len(got), len(want))
    names = set(order)
    expect_names = set().union(*want)
    assert expect_names <= names, f'covers the reference hits but fvgen did not generate: {sorted(expect_names - names)}'
    diffs = [(i, sorted(g - w), sorted(w - g)) for i, (g, w) in enumerate(zip(got, want)) if g != w]
    assert not diffs, f'{len(diffs)} cycles differ, first: {diffs[:5]}'
    never = sorted(names - set().union(*got))
    return n, len(names), never


if __name__ == '__main__':
    n = test_expression_equivalence()
    print(f'T1: {n} expressions equivalent to slang')
    s, acc, rej = test_constraints_vs_solver()
    print(f'T2: {s} solver solutions satisfy the assumes; {acc} accepted / {rej} rejected candidates agree with the solver')
    n, nb, never = test_coverage_vs_reference()
    print(f'T3: {nb} covers match the reference on {n} random cycles (never hit, as in the reference: {never})')
