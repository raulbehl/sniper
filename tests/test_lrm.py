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


# ======================================================================== clause 19
# examples where fvgen deliberately differs from the LRM text; each with the reason (shown, not failed)
L19_KNOWN = {
    '19.5.2 b7 = (1 => 2 [= 3:6] => 5)':
        'the text counts only sample 13; with the meaning [=] has in sequences (16.9.2: 2[->n] ##1 !2[*0:$] ##1 5) '
        'each later 5 completes the sequence again (samples 14..18). fvgen follows 16.9.2; reachability is the '
        'same either way, only hit counts differ. To be settled against a commercial simulator.',
}
L19_TRACE1 = [4, 5, 7, 11, 8, 12, 2, 2, 3]
L19_TRACE2 = [1, 4, 3, 2, 3, 3, 2, 2, 3, 2, 3, 1, 5, 5, 5, 5, 5, 5]


class CgDriver:
    """drive fvgen covergroup modules with sample streams: per sample, the covers hit"""

    def __init__(self, out, rep, sim):
        self.out, self.rep, self.sim = out, rep, sim
        self.covers_sv = open(os.path.join(out, 'fv_covers.sv')).read()

    def ports(self, mod):
        hdr = re.search(r'^module %s\b.*?\);' % mod, self.covers_sv, re.S | re.M).group(0)
        return [(int(w), p) for w, p in re.findall(r'input logic (?:signed )?\[(\d+):0\] (\w+)', hdr)
                if p not in ('clk', 'reset', 'sample')] + \
               [(0, p) for p in re.findall(r'input logic (?:signed )?(\w+)\s*[,)/]', hdr) if p not in ('clk', 'reset', 'sample')]

    def run(self, mod, samples):
        """samples: list of {port suffix: value}; returns list of sets of cover names"""
        ports = self.ports(mod)
        order = self.rep['hit_order'][mod]
        names = [p for _, p in ports]
        tag = mod[len('fv_cg_'):]
        with open(os.path.join(self.out, f'{tag}.smp'), 'w') as f:
            for s in samples:
                row = []
                for p in names:
                    key = next((k for k in s if p == k or p.endswith('__' + k)), None)
                    row.append(s.get(key, 0) if key else 0)
                f.write(' '.join(str(x) for x in row) + '\n')
        decl = '\n'.join(f'  logic [{w}:0] {p};' for w, p in ports)
        conn = ', '.join(f'.{p}({p})' for p in names)
        rd = ' '.join(['%d'] * len(names))
        tb = f'''module tb_{tag};
  logic clk = 0, sample = 1;
{decl}
  logic [{len(order) - 1}:0] hits;
  {mod} u (.clk, .reset(1'b0), .sample{', ' + conn if conn else ''}, .fv_hits(hits));
  int fd, n; longint tv_[{max(len(names), 1)}];
  initial begin
    fd = $fopen("{tag}.smp", "r");
    while (1) begin
      n = $fscanf(fd, "{rd}", {", ".join(f"tv_[{i}]" for i in range(len(names)))});
      if (n != {len(names)}) break;
      {" ".join(f"{p} = tv_[{i}];" for i, p in enumerate(names))}
      #1 $display("H %b", hits);
      clk = 1; #1 clk = 0;
    end
    $finish;
  end
endmodule
'''
        open(os.path.join(self.out, f'tb_{tag}.sv'), 'w').write(tb)
        bd = os.path.join(self.out, 'b_' + tag)
        r = self.sim.build([os.path.join(self.out, 'fv_covers.sv'), os.path.join(self.out, f'tb_{tag}.sv')],
                           'tb_' + tag, bd, incdirs=[self.out])
        assert not r.returncode, (r.stderr or r.stdout)[-2000:]
        r = self.sim.run(bd, self.out)
        got = []
        for line in r.stdout.splitlines():
            if line.startswith('H '):
                bits = line[2:].strip()[::-1]
                got.append({order[i] for i, b in enumerate(bits) if b == '1'})
        assert len(got) == len(samples), (len(got), len(samples), r.stdout[-500:])
        return got


def value_sets(hits, values, prefix):
    """bin -> sorted values that hit it (one sample per value)"""
    out = {}
    for v, h in zip(values, hits):
        for c in h:
            if c.startswith(prefix): out.setdefault(c[len(prefix):], []).append(v)
    return out


def tuples_of(hits, cross, cps):
    """cross bin -> set of coverpoint-bin tuples seen with it"""
    out = {}
    for h in hits:
        bins = []
        for cp in cps:
            bs = [c[len(f'cov_{cp}__'):] for c in h if c.startswith(f'cov_{cp}__')]
            bins.append(bs)
        for c in h:
            if c.startswith(f'cov_{cross}__'):
                for combo in [(x, y) for x in bins[0] for y in bins[1]]:
                    out.setdefault(c[len(f'cov_{cross}__'):], set()).add(combo)
    return out


def test_coverage_lrm():
    """clause 19 worked examples: fvgen's covers against what 1800-2023 states for each"""
    work = tempfile.mkdtemp(prefix='lrm19_')
    src = os.path.join(HERE, 'lrm_coverage.sv')
    js = os.path.join(work, 'c.json')
    run([PY, os.path.join(ROOT, 'sniper.py'), '--own', src, '-o', js, '--', src])
    out = os.path.join(work, 'fv')
    run([PY, os.path.join(ROOT, 'fvgen.py'), js, '-o', out, '--expose', '--max-bins', '1024'])
    rep = json.load(open(os.path.join(out, 'report.json')))
    open(os.path.join(out, 'prim_assert_dummy_macros.svh'), 'w').write(DUMMY_MACROS)
    drv = CgDriver(out, rep, refsim.get())
    fails, known = [], []

    def check(what, got, want):
        if got == want: return
        if what in L19_KNOWN: known.append((what, got, want, L19_KNOWN[what]))
        else: fails.append((what, got, want))

    # 19.5.1 bins
    vals = list(range(1024))
    vs = value_sets(drv.run('fv_cg_l19__g_bins', [{'v_a': v} for v in vals]), vals, 'cov_v_a__')
    check('19.5.1 a', vs.get('a'), list(range(64)) + [65])
    check('19.5.1 b[] (65 bins b[127]..b[191])', sorted(k for k in vs if k.startswith('b__')),
          sorted(f'b__{v}' for v in range(127, 192)))
    check('19.5.1 c[]', {k: vs[k] for k in vs if k.startswith('c__')}, {f'c__{v}': [v] for v in (200, 201, 202)})
    check('19.5.1 d = [1000:$]', vs.get('d'), list(range(1000, 1024)))
    taken = set(range(64)) | {65} | set(range(127, 192)) | {200, 201, 202} | set(range(1000, 1024))
    check('19.5.1 others[] = default: a bin per remaining value', {k: v for k, v in vs.items() if k.startswith('others__')},
          {f'others__{v}': [v] for v in range(1024) if v not in taken})
    # 19.5.1 fixed
    vals = list(range(16))
    vs = value_sets(drv.run('fv_cg_l19__g_fixed', [{'f': v} for v in vals]), vals, 'cov_cf__')
    check('19.5.1 fixed[4]', {k: vs.get(k) for k in ('fixed__0', 'fixed__1', 'fixed__2', 'fixed__3')},
          {'fixed__0': [1, 2, 3], 'fixed__1': [4, 5, 6], 'fixed__2': [7, 8, 9], 'fixed__3': [1, 4, 7, 10]})
    check('19.5.1 few[5]: <1> <4> <7> < > < >', {k: vs.get(k) for k in ('few__0', 'few__1', 'few__2', 'few__3', 'few__4')},
          {'few__0': [1], 'few__1': [4], 'few__2': [7], 'few__3': None, 'few__4': None})
    # 19.5.3 auto
    vals = list(range(8))
    vs = value_sets(drv.run('fv_cg_l19__g_auto', [{'w': v} for v in vals]), vals, 'cov_cw__')
    check('19.5.3 auto, M=3 N=3', sorted(vs.values()), [[0, 1], [2, 3], [4, 5, 6, 7]])
    # 19.5.1.1 with
    vals = list(range(256))
    vs = value_sets(drv.run('fv_cg_l19__g_with', [{'x': v} for v in vals]), vals, 'cov_cx__')
    check('19.5.1.1 mod3[]', sorted(v[0] for k, v in vs.items() if k.startswith('mod3__')), list(range(0, 256, 3)))
    check('19.5.1.1 top[] = cx with', sorted(v[0] for k, v in vs.items() if k.startswith('top__')), list(range(251, 256)))
    # 19.5.2 transitions (trace 1): sa on 7=>11 (sample 4), 8=>12 (6); sc on 12..3 (9)
    h = drv.run('fv_cg_l19__g_trans', [{'t': v} for v in L19_TRACE1])
    at = lambda name: [i + 1 for i, x in enumerate(h) if f'cov_ct__{name}' in x]
    check('19.5.2 sa', at('sa'), [4, 6])
    check('19.5.2 sb[7=>11]', at('sb__7_11'), [4])
    check('19.5.2 sb[8=>12]', at('sb__8_12'), [6])
    check('19.5.2 sc', at('sc'), [9])
    # 19.5.2 repetitions (trace 2): the samples the LRM states
    h = drv.run('fv_cg_l19__g_rep', [{'v': v} for v in L19_TRACE2])
    at = lambda name: [i + 1 for i, x in enumerate(h) if f'cov_cv__{name}' in x]
    check('19.5.2 b2 = (2 [-> 3:5])', at('b2'), [8, 10])
    check('19.5.2 b3 = (3 [-> 3:5])', at('b3'), [6, 9, 11])
    check('19.5.2 b5 = (5 [* 3])', at('b5'), [15, 16, 17, 18])
    check('19.5.2 b6 = (1 => 3 [-> 4:6] => 1)', at('b6'), [12])
    check('19.5.2 b7 = (1 => 2 [= 3:6] => 5)', at('b7'), [13])
    # 19.5.4 wildcard
    vals = list(range(16))
    vs = value_sets(drv.run('fv_cg_l19__g_wild', [{'wv': v} for v in vals]), vals, 'cov_cwv__')
    check('19.5.4 g12_15', vs.get('g12_15'), [12, 13, 14, 15])
    check('19.5.4 g12_15_array[]', sorted(v for k, v in vs.items() if k.startswith('g12_15_array')), [[12], [13], [14], [15]])
    pairs = [(a, b) for a in range(4) for b in range(4)]
    h = drv.run('fv_cg_l19__g_wild', [s for a, b in pairs for s in ({'wt': a}, {'wt': b})])
    t03 = sorted((pairs[i // 2]) for i, x in enumerate(h) if i % 2 == 1 and 'cov_cwt__T0_3' in x)
    check('19.5.4 T0_3 = (0,1 => 2,3)', t03, [(0, 2), (0, 3), (1, 2), (1, 3)])
    arr = sorted(c for x in h for c in x if c.startswith('cov_cwt__T0_3_array'))
    check('19.5.4 T0_3_array[]: 4 bins', len(set(arr)), 4)
    # 19.5.5 ignored values leave the automatic bins (19.11.1: 14 bins); 19.5.6 illegal values
    vals = list(range(16))
    vs = value_sets(drv.run('fv_cg_l19__g_excl', [{'ia': v, 'ib': 0} for v in vals]), vals, 'cov_cia__')
    check('19.5.5 ignore_vals {7,8}: values left in the automatic bins', sorted(x for v in vs.values() for x in v),
          [v for v in range(16) if v not in (7, 8)])
    vs = value_sets(drv.run('fv_cg_l19__g_excl', [{'ib': v, 'ia': 0} for v in vals]), vals, 'cov_cib__')
    check('19.5.6 all minus illegal {1,2,3}', vs.get('all'), [0] + list(range(4, 16)))
    # 19.5.7 value resolution
    h = drv.run('fv_cg_l19__g_res', [{'p1': v, 'p2': v} for v in range(8)])
    sg = lambda v: v - 8 if v >= 4 else v
    check('19.5.7 b1 = {1:7}', [v for v in range(8) if 'cov_cp1__b1' in h[v]], list(range(1, 8)))
    check('19.5.7 b2 = {1:7}', [v for v in range(8) if 'cov_cp1__b2' in h[v]], list(range(1, 8)))
    check('19.5.7 b3 = {1,2,3}', sorted(sg(v) for v in range(8) if 'cov_cp2__b3' in h[v]), [1, 2, 3])
    check('19.5.7 b4 = {-1,1,2,3}', sorted(sg(v) for v in range(8) if 'cov_cp2__b4' in h[v]), [-1, 1, 2, 3])
    # 19.6 cov3: 16 x 10 = 160 cross products
    cc = [c for c in rep['hit_order']['fv_cg_l19__g_cov3'] if c.startswith('cov_CC__')]
    check('19.6 CC: 160 cross products', len(cc), 160)
    # 19.6.1 x1 (4 products), x2 = i_zero + the 2 automatic bins it does not overlap
    smp = [{'i': a, 'j': b} for a in (0, 1) for b in (0, 1)]
    t = tuples_of(drv.run('fv_cg_l19__g_ct', smp), 'x2', ['i', 'j'])
    check('19.6.1 x2 i_zero', t.get('i_zero'), {('i__0', 'j__0'), ('i__0', 'j__1')})
    check('19.6.1 x2 bins', sorted(t), sorted(['i_zero', 'i__1__j__0', 'i__1__j__1']))
    # 19.6.1.1 select expressions
    reps = [0, 64, 128, 192]
    smp = [{'v_c': a, 'v_d': b} for a in reps for b in (0, 1, 85, 170)]
    t = tuples_of(drv.run('fv_cg_l19__g_sel', smp), 'c', ['a', 'b'])
    A = ['a1', 'a2', 'a3', 'a4']; B = ['b1', 'b2', 'b3', 'b4']
    check('19.6.1.1 c1', t.get('c1'), {('a1', b) for b in B})
    check('19.6.1.1 c2', t.get('c2'), {('a2', b) for b in B} | {(a, 'b2') for a in A})
    check('19.6.1.1 c3', t.get('c3'), {('a1', 'b4')})
    auto = {k: v for k, v in t.items() if k not in ('c1', 'c2', 'c3')}
    check('19.6.1.1 retained automatic', set().union(*auto.values()) if auto else set(),
          {('a3', 'b1'), ('a4', 'b1'), ('a3', 'b3'), ('a4', 'b3'), ('a3', 'b4'), ('a4', 'b4')})
    # 19.6.2 / 19.6.3: ignored and illegal products leave the cross
    yx = [c for c in rep['hit_order']['fv_cg_l19__g_yy'] if c.startswith('cov_yx__')]
    want = {f'cov_yx__auto_{a}__auto_{b}' for a in range(8) for b in range(8) if a not in (1, 2, 3, 5) and b != 6}
    check('19.6.2/3 yx products', set(yx), want)
    # 19.5.1 per-bin iff and coverpoint iff
    smp = [{'q': q, 'en_b': eb, 'en_cp': ec} for q in range(4) for eb in (0, 1) for ec in (0, 1)]
    h = drv.run('fv_cg_l19__g_iff', smp)
    check('iff lo (bin iff and coverpoint iff)', [(s['q'], s['en_b'], s['en_cp']) for s, x in zip(smp, h) if 'cov_cq__lo' in x],
          [(q, 1, 1) for q in (0, 1)])
    check('iff hi (coverpoint iff)', [(s['q'], s['en_cp']) for s, x in zip(smp, h) if 'cov_cq__hi' in x],
          [(q, 1) for q in (2, 3) for _ in (0, 1)])
    unsup = [(e.get('src') or e.get('name'), e.get('notes')) for e in rep['entries']
             if e['kind'] != 'constraint' and e.get('status') == 'unsupported']
    # a known divergence is settled by the reference simulator when it supports the construct (Verilator
    # does not support [=] in transition bins): its hit count for b7 on the LRM trace
    sim = refsim.get()
    if sim.name != 'verilator' and known:
        tb = os.path.join(out, 'tb_native_rep.sv')
        open(tb, 'w').write('module tb_native_rep;\n  l19 m ();\n  initial begin\n' +
                            ''.join(f'    m.v = {x}; m.gi_rep.sample();\n' for x in L19_TRACE2) + '    $finish;\n  end\nendmodule\n')
        bd = os.path.join(out, 'b_native_rep')
        r = sim.build([src, tb], 'tb_native_rep', bd, coverage=True)
        if not r.returncode:
            sim.run(bd, out)
            cnt = {k: v for k, v in sim.coverage(bd, out).items() if k.endswith('.b7')}
            for i, k in enumerate(known):
                if k[0].startswith('19.5.2 b7'):
                    known[i] = k[:3] + (k[3] + f' {sim.name}: b7 hit {cnt} (fvgen 6 increments, LRM text 1).',)
    if not fails: shutil.rmtree(work, ignore_errors=True)
    return fails, known, unsup, work


def test_l19():
    fails, known, unsup, work = test_coverage_lrm()
    assert not fails, fails


if __name__ == '__main__' and len(sys.argv) > 1 and sys.argv[1] == '19':
    fails, known, unsup, work = test_coverage_lrm()
    for f in known: print('KNOWN', f[0], f'(got {str(f[1])[:80]}, LRM text {str(f[2])[:80]}):', f[3])
    for f in fails: print('FAIL', f[0], '\n   got ', str(f[1])[:400], '\n   want', str(f[2])[:400])
    for u in unsup: print('UNSUPPORTED', u)
    print(f'L19: {len(fails)} failures, {len(unsup)} unsupported' + (f' ({work})' if fails else ''))
    sys.exit(0)

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
