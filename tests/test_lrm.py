#!/usr/bin/env python3
"""LRM conformance of sniper + fvgen (IEEE 1800-2023 clauses 18 and 19).

  L18  constraints: one class per construct (tests/lrm_constraints.sv, classes L18_*). For every class, value
       tuples of its fields go through fvgen's constraint checker (fv_c_<class>, all hard atoms) and through
       Verilator's solver (`c.randomize() with {field == value; ...}` succeeds iff the tuple satisfies the
       class's constraints) in one simulation; the verdicts must agree. Tuples: the solver's own solutions
       (each must be accepted, soft constraints included), every solution mutated in one field (boundaries),
       and random tuples. Soft constraints are left out of the pinned comparison (a pinned value overrides a
       soft constraint, LRM 18.5.13). fvgen's port map (report.json) names the SV field behind each port bit.

    python tests/test_lrm.py      (or pytest tests/test_lrm.py)
Needs verilator, or the simulator named by SNIPER_REF_SIM (tests/refsim.py: vcs, xcelium, questa). Solver
deviations from check() are reported per class (they are the reference simulator's, not fvgen's).
"""
import json, os, random, re, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
sys.path.insert(0, HERE)
from test_fvgen import DUMMY_MACROS, run      # noqa: E402
import refsim                                  # noqa: E402


def rel(path):
    """'L18_x.items[0].v' -> 'items[0].v' (relative to the object)"""
    head = re.match(r'[A-Za-z_]\w*', path).group(0)
    return path[len(head) + 1:]


class Slice:
    def __init__(self, i, port, lsb, w, path, role, kind):
        self.v, self.port, self.lsb, self.w, self.path, self.role, self.kind = f'v_{i}', port, lsb, w, path, role, kind
        self.rel = rel(path)
        self.is_size = path.endswith('.size()')
        self.slot = re.search(r' slot (\d+) (key|present)$|\[slot (\d+)\]$', path)


def slices(pm):
    out = []
    for port in sorted(pm):
        for lsb, w, path, role, kind in pm[port]:
            out.append(Slice(len(out), port, lsb, w, path, role, kind))
    return out


def arr_of(s):
    return s.rel[:-len('.size()')]


def presize(s):
    """create a dynamic array / queue at the tuple's size before randomize()"""
    a = arr_of(s)
    if s.kind == 'QueueType':
        return f'c.{a}.delete(); repeat ({s.v}) c.{a}.push_back(0);'
    return f'c.{a} = new[{s.v}];'


def guard(sl, sizes):
    """an element of a dynamic dimension exists only below its size"""
    gs = []
    for z in sizes:
        m = re.match(re.escape(arr_of(z)) + r'\[(\d+)\]', sl.rel)
        if m: gs.append(f'{m.group(1)} < {z.v}')
    return ' && '.join(gs)


def gen_tb(cls, mod, sls, atoms, site=False, soft_oracle=False):
    """testbench for one class: +MODE=solve -> free randomize() per state tuple, prints the solution and
    fvgen's verdict (all atoms); +MODE=check -> pinned randomize() per tuple, prints both verdicts"""
    ports = {}
    for s in sls: ports[s.port] = max(ports.get(s.port, 0), s.lsb + s.w)
    # an atom constrains only while its enable parameter (constraint_mode, INLINE_EN, SOFT_EN) is on
    hard = [i for i, a in enumerate(atoms) if not a['soft'] and a.get('enabled', True)]
    acc_hard = ' && '.join(f'atoms[{i}]' for i in hard) or "1'b1"
    acc_all = ' && '.join(f'atoms[{i}]' for i, a in enumerate(atoms) if a.get('enabled', True)) or "1'b1"
    sizes = sorted([s for s in sls if s.is_size], key=lambda s: s.rel.count('['))
    vals = [s for s in sls if not s.is_size and not s.slot]
    slots = {}
    for s in sls:
        if s.slot:
            i = s.slot.group(1) or s.slot.group(3)
            slots.setdefault((re.match(r'(\w+)', s.rel).group(1), i), {})[s.slot.group(2) or 'val'] = s
    L = [f'module tb_{cls};', f'  {cls} c;']
    L += [f'  logic [{w - 1}:0] p_{p};' for p, w in sorted(ports.items())]
    na = max(len(atoms), 1)
    L.append(f'  logic [{na - 1}:0] atoms;')
    conn = ', '.join(f'.{p}(p_{p})' for p in sorted(ports))
    L.append(f"  {mod} u (.clk(1'b0), .reset(1'b0), .en(1'b1){', ' + conn if conn else ''}"
             f"{', .fv_atoms(atoms)' if atoms else ''});")
    if not atoms: L.append("  assign atoms = '1;")
    if sls: L.append('  longint ' + ', '.join(s.v for s in sls) + ';')
    L += ['  int fd, n, ok, native, nsoft; string mode;', '  initial begin',
          '    if (!$value$plusargs("MODE=%s", mode)) mode = "check";',
          f'    fd = $fopen("{cls}.tup", "r");', '    while (1) begin']
    if sls:
        L.append('      n = $fscanf(fd, "' + ' '.join(['%d'] * len(sls)) + '", ' + ', '.join(s.v for s in sls) + ');')
        L.append(f'      if (n != {len(sls)}) break;')
    else:
        L.append('      n = $fscanf(fd, "%d", ok); if (n != 1) break;')
    L.append('      c = new;')
    for s in sizes: L.append('      ' + presize(s))
    for s in sls:
        if s.kind == 'AssocKey': L.append(f'      c.{s.rel} = 0;')      # the constant keys the array holds
    for (a, i), d in sorted(slots.items()):      # associative entries the array holds
        if 'present' in d and 'key' in d:
            L.append(f"      if ({d['present'].v}) c.{a}[{d['key'].v}] = 0;")
    for s in sls:
        if s.kind == 'NullFlag': L.append(f'      if ({s.v}) c.{s.rel} = null; else c.{s.rel} = new;')
    vals = [s for s in vals if s.kind != 'NullFlag']
    for s in vals:                               # the tuple as the object's state: the oracle check()
        g = guard(s, sizes)
        L.append(f'      {"if (" + g + ") " if g else ""}c.{s.rel} = type(c.{s.rel})\'({s.v});')
    for (a, i), d in sorted(slots.items()):
        if 'val' in d and 'present' in d and 'key' in d:
            L.append(f"      if ({d['present'].v}) c.{a}[{d['key'].v}] = type(c.{a}[{d['key'].v}])'({d['val'].v});")
    L.append('      native = c.check();')
    L.append(f'      nsoft = {"c.check_soft()" if soft_oracle else "1"};')
    L.append('      if (mode == "solve") begin')
    L.append('        ok = c.go();' if site else '        ok = c.randomize();')
    L.append('        native = c.check();')
    L.append(f'        nsoft = {"c.check_soft()" if soft_oracle else "1"};')
    for s in sizes:
        L.append(f'        {s.v} = c.{s.rel};')
    for s in sls:
        if s.kind == 'NullFlag': L.append(f'        {s.v} = (c.{s.rel} == null);')
    for s in vals:
        g = guard(s, sizes)
        L.append(f'        {s.v} = {"(" + g + ") ? " if g else ""}c.{s.rel}{" : 0" if g else ""};')
    for (a, i), d in sorted(slots.items()):
        if 'val' in d and 'present' in d and 'key' in d:
            L.append(f"        if ({d['present'].v}) {d['val'].v} = c.{a}[{d['key'].v}];")
    L.append('      end else begin')
    if site: L.append('        ok = native;   // the call site\'s own randomize(): no pinned form')
    else: L.append('        ok = c.randomize() with {')
    for s in (sizes if not site else []):
        if s.role == 'rand': L.append(f'          {s.rel} == {s.v};')
    for s in (vals if not site else []):
        if s.role == 'rand':
            g = guard(s, sizes)
            pin = f"{s.rel} == {s.w}'({s.v});"
            L.append(f'          if ({g}) {pin}' if g else f'          {pin}')
    for (a, i), d in (sorted(slots.items()) if not site else []):
        if 'val' in d and 'present' in d and 'key' in d and d['val'].role == 'rand':
            L.append(f"          if ({d['present'].v}) {a}[{d['key'].v}] == {d['val'].w}'({d['val'].v});")
    if not site: L.append('        };')
    L.append('      end')
    for s in sls: L.append(f"      p_{s.port}[{s.lsb} +: {s.w}] = {s.w}'({s.v});")
    L.append('      #1;')
    L.append('      if (mode == "solve") $write("S %0d %0d %0d %0d %0d", ok, native, (' + acc_hard + ') ? 1 : 0, (' + acc_all + ') ? 1 : 0, nsoft);')
    L.append('      else $write("R %0d %0d %0d %0d %0d", ok, native, (' + acc_hard + ') ? 1 : 0, (' + acc_all + ') ? 1 : 0, nsoft);')
    if sls: L.append('      $display(" ' + ' '.join(['%0d'] * len(sls)) + '", ' + ', '.join(s.v for s in sls) + ');')
    else: L.append('      $display("");')
    L += ['    end', '    $finish;', '  end', 'endmodule']
    return '\n'.join(L)


def tuple_random(sls, rng, sizes_max):
    t = []
    for s in sls:
        if s.is_size: t.append(rng.randint(0, sizes_max.get(s.port, 2)))
        elif s.slot and s.slot.group(2) == 'present': t.append(rng.randint(0, 1))
        elif s.slot and s.slot.group(2) == 'key': t.append(rng.choice([rng.randint(-3, 20), rng.randint(0, 1 << 20)]))
        else:
            hi = (1 << min(s.w, 31)) - 1
            t.append(rng.choice([rng.randint(0, min(hi, 15)), rng.randint(0, hi), 0, min(hi, 1)]))
    return t


def norm(sls, t):
    """keys of the symbolic associative entries distinct from each other and from the constant keys (two
    entries of one key are one entry in SV)"""
    t = list(t)
    seen = {}
    for s in sls:
        if s.kind == 'AssocKey':
            m = re.match(r'(\w+)\[(-?\d+)\]$', s.rel)
            if m: seen.setdefault(m.group(1), set()).add(int(m.group(2)))
    for i, s in enumerate(sls):
        if s.slot and s.slot.group(2) == 'key':
            arr = re.match(r'(\w+)', s.rel).group(1)
            while t[i] in seen.setdefault(arr, set()): t[i] += 1
            seen[arr].add(t[i])
    return t


def test_constraints_lrm(seed=7, n_solve=40, n_random=150, only=None):
    work = tempfile.mkdtemp(prefix='lrm18_')
    src = os.path.join(HERE, 'lrm_constraints.sv')
    js = os.path.join(work, 'c.json')
    run([PY, os.path.join(ROOT, 'sniper.py'), '--own', src, '-o', js, '--', src])
    d = json.load(open(js))
    assert d['meta']['errors'] == 0, d['meta']['error_list']
    out = os.path.join(work, 'fv')
    run([PY, os.path.join(ROOT, 'fvgen.py'), js, '-o', out, '--expose', '--max-array', '3'])
    rep = json.load(open(os.path.join(out, 'report.json')))
    open(os.path.join(out, 'prim_assert_dummy_macros.svh'), 'w').write(DUMMY_MACROS)
    unsup = [(e.get('class'), e.get('src'), e.get('notes')) for e in rep['entries']
             if e['kind'] == 'constraint' and e.get('status') == 'unsupported' and (e.get('class') or '').startswith('L18_')]
    rng = random.Random(seed)
    results, failures, deviations = {}, [], []
    classes = sorted((m[len('fv_c_'):], m) for m in rep['modules']['constraints']
                     if re.fullmatch(r'fv_c_L18_\w+', m) and '__' not in m[len('fv_c_L18_'):])
    # a call-site class: its randomize() call is in go(); the site's module holds class + inline constraints
    classes += sorted((re.match(r'fv_c_(L18S_\w+?)__', m).group(1), m) for m in rep['modules']['constraints']
                      if re.match(r'fv_c_L18S_\w+?__go_', m))
    if only: classes = [c for c in classes if re.search(only, c[0])]
    tbs = []
    srctext = open(src).read()
    soft_of = {}
    for cls, mod in classes:
        body = re.search(r'class %s\b.*?endclass' % cls, srctext, re.S)
        if not body or 'function bit check' not in body.group(0): continue     # a base class of another case
        sls = slices(rep['port_map'][mod])
        atoms = rep['atom_order'][mod]
        body = re.search(r'class %s\b.*?endclass' % cls, srctext, re.S)
        soft_oracle = bool(body and 'function bit check_soft' in body.group(0))
        open(os.path.join(out, f'tb_{cls}.sv'), 'w').write(gen_tb(cls, mod, sls, atoms, cls.startswith('L18S_'), soft_oracle))
        tbs.append((cls, sls, atoms))
        soft_of[cls] = soft_oracle
    # one Verilator build for all classes: a top with a +CLS selector would need one module; build each
    sim = refsim.get()
    for cls, sls, atoms in tbs:
        bd = os.path.join(out, 'b_' + cls)
        r = sim.build([src, os.path.join(out, 'fv_constraints.sv'), os.path.join(out, f'tb_{cls}.sv')], 'tb_' + cls,
                      bd, incdirs=[out])
        if r.returncode:
            failures.append((cls, f'{sim.name} build failed: ' + (r.stderr or r.stdout)[-1500:])); continue
        smax = {s.port: 3 for s in sls if s.is_size}
        # 1. solver solutions under random state
        tup = [norm(sls, tuple_random(sls, rng, smax)) for _ in range(n_solve)]
        with open(os.path.join(out, f'{cls}.tup'), 'w') as f:
            for t in tup: f.write(' '.join(map(str, t)) + '\n')
        r = sim.run(bd, out, ['+MODE=solve'], seed)
        sols, solve_bad, soft_drop = [], [], []
        names = [s.path for s in sls]
        for line in r.stdout.splitlines():
            if not line.startswith('S '): continue
            f = list(map(int, line.split()[1:]))
            ok, nat, hard, allv, nsoft, vals = f[0], f[1], f[2], f[3], f[4], f[5:]
            if not ok: continue
            sols.append(vals)
            if not nat:
                deviations.append((cls, 'solver solution violates the constraints (check())', list(zip(names, vals))))
            elif not hard:
                solve_bad.append(vals)
            elif not nsoft:
                deviations.append((cls, 'solver solution drops a soft constraint the LRM keeps (check_soft())',
                                   list(zip(names, vals))))
            elif not allv:
                soft_drop.append(vals)
        if solve_bad:
            failures.append((cls, f'{len(solve_bad)} valid solver solutions rejected by fvgen, e.g. '
                                  + str(list(zip(names, solve_bad[0])))))
        if soft_drop:
            deviations.append((cls, f'{len(soft_drop)} solver solutions drop a soft constraint fvgen keeps (LRM 18.5.13: '
                                    'a satisfiable soft constraint is honored)', list(zip(names, soft_drop[0]))))
        # 2. pinned: solutions, one-field mutations, random tuples
        tup = list(sols)
        for v in sols[:30]:
            for _ in range(3):
                m = list(v)
                i = rng.randrange(len(m)) if m else 0
                if m:
                    s = sls[i]
                    m[i] = (m[i] + rng.choice([1, -1, 2])) if not s.is_size else rng.randint(0, 3)
                    if not s.is_size and not s.slot: m[i] &= (1 << s.w) - 1
                    tup.append(norm(sls, m))
        tup += [norm(sls, tuple_random(sls, rng, smax)) for _ in range(n_random)]
        with open(os.path.join(out, f'{cls}.tup'), 'w') as f:
            for t in tup: f.write(' '.join(map(str, t)) + '\n')
        r = sim.run(bd, out, ['+MODE=check'], seed)
        n = acc_n = 0
        diffs, sdev = [], []
        for line in r.stdout.splitlines():
            if not line.startswith('R '): continue
            f = list(map(int, line.split()[1:]))
            ok, nat, fv, fv_all, nsoft, vals = f[0], f[1], f[2], f[3], f[4], f[5:]
            n += 1
            acc_n += nat
            if fv != nat: diffs.append(('check()', nat, 'fvgen', fv, list(zip(names, vals))))
            elif soft_of[cls] and nat and fv_all != nsoft:
                diffs.append(('check_soft()', nsoft, 'fvgen (soft kept)', fv_all, list(zip(names, vals))))
            if ok != nat: sdev.append((ok, nat, list(zip(names, vals))))
        results[cls] = (n, acc_n, len(sols))
        if diffs:
            failures.append((cls, f'{len(diffs)}/{n} fvgen verdicts differ from check(): first {diffs[:2]}'))
        if sdev:
            deviations.append((cls, f'{len(sdev)}/{n} solver verdicts differ from check() (solver, check)', sdev[0]))
        if n == 0:
            failures.append((cls, 'no tuples checked: ' + r.stdout[-500:]))
        if len(sols) == 0:
            deviations.append((cls, 'the solver found no solution', None))
    shutil.rmtree(work, ignore_errors=True) if not failures else None
    return results, unsup, failures, deviations, work


def test_l18():
    res, unsup, fails, devs, work = test_constraints_lrm()
    assert not fails, fails
    assert not unsup, unsup


if __name__ == '__main__':
    only = sys.argv[1] if len(sys.argv) > 1 else None
    res, unsup, fails, devs, work = test_constraints_lrm(only=only)
    for c, (n, a, s) in sorted(res.items()):
        print(f'  {c:40s} {n:4d} tuples ({a} accepted), {s} solutions')
    for u in unsup: print('UNSUPPORTED', u)
    seen = {}
    print(f'reference simulator: {refsim.NAME}')
    for d in devs: seen.setdefault((d[0], d[1].split(' (')[0] if 'solution violates' in d[1] else d[1]), []).append(d[2])
    for (c, what), ex in seen.items(): print(f'VERILATOR {c}: {what}' + (f' x{len(ex)}' if len(ex) > 1 else '') + f'; e.g. {str(ex[0])[:200]}')
    for c, f in fails: print('FAIL', c, f)
    print(f'L18: {len(res)} classes, {len(fails)} failures, {len(unsup)} unsupported items' + (f' ({work})' if fails else ''))
