#!/usr/bin/env python3
"""fvgen's commercial formal backends (fvtools.py) without the tools.

  F1  `fvgen --run --formal-tool jasper|vcf` end to end with stand-in `jg` / `vcf` executables: the script
      each backend writes is checked (FV_SVA defined, every free signal made free by a hierarchical name
      that exists, covers proven), the stand-in answers every cover as reached, and fvgen's report must
      show them reached (result parsing).
  F2  the generated code with FV_SVA defined (concurrent SVA, no yosys attributes) passes Verilator's lint,
      for the generated modules and every job top.

    python tests/test_fvtools.py      (or pytest tests/test_fvtools.py)
Needs verilator.
"""
import json, os, re, stat, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
sys.path.insert(0, ROOT)
import fvtools                       # noqa: E402

STANDIN = r'''#!/usr/bin/env python3
# stand-in for {tool}: checks the script fvgen wrote, then reports every cover of the job as reached
import re, sys
args = sys.argv[1:]
tcl = open(args[args.index('{flag}') + 1]).read()
assert 'FV_SVA' in tcl, 'FV_SVA not defined'
files = re.findall(r'(\S+(?:_lib\.sv|\.sv))(?=\s|\}})', tcl)
src = '\n'.join(open(f).read() for f in files if not f.startswith('+'))
free = set(re.findall(r'`FV_ANYSEQ\s+(?:logic|bit)(?:\s+signed)?(?:\s*\[[^\]]*\])*\s+(\w+)', src))
for path in re.findall(r'{freecmd}\s+(?:-env\s+)?\{{(\S+)\}}', tcl):
    assert path.split('.')[-1] in free, f'free signal {{path}} is not an FV_ANYSEQ declaration'
assert re.search(r'{provecmd}', tcl), 'no prove command'
n = 0
for c in re.findall(r'`FV_COVER\((\w+),', src):
    print(f'FVCOVER {status} 3 top.u.{{c}}'); n += 1
print(f'stand-in {tool}: {{n}} covers', file=sys.stderr)
'''


def run(cmd, cwd=None, env=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    if r.returncode: raise RuntimeError(f'{cmd[0]} failed:\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}')
    return r


def extract(work):
    src = os.path.join(HERE, 'fv_regress_a.sv')
    js = os.path.join(work, 'r.json')
    run([PY, os.path.join(ROOT, 'sniper.py'), '--own', HERE, '-o', js, '--', '--top', 'fv_regress_top', src,
         os.path.join(HERE, 'fv_regress_b.sv')])
    return js


def test_commercial_backends():
    work = tempfile.mkdtemp(prefix='fvtools_')
    js = extract(work)
    bind = os.path.join(work, 'bin')
    os.makedirs(bind)
    for exe, tool, flag, freecmd, provecmd, status in (
            ('jg', 'JasperGold', '-tcl', 'stopat', r'prove -property \[get_property_list -include \{type cover\}\]', 'covered'),
            ('vcf', 'VC Formal', '-f', 'snip_driver', r'check_fv ', 'covered')):
        p = os.path.join(bind, exe)
        open(p, 'w').write(STANDIN.format(tool=tool, flag=flag, freecmd=freecmd, provecmd=provecmd, status=status))
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    env = dict(os.environ, PATH=bind + os.pathsep + os.environ['PATH'])
    counts = {}
    for tool in ('jasper', 'vcf'):
        out = os.path.join(work, 'fv_' + tool)
        run([PY, os.path.join(ROOT, 'fvgen.py'), js, '-o', out, '--run', '--keep', '--formal-tool', tool], env=env)
        rep = json.load(open(os.path.join(out, 'report.json')))
        s = rep['smoke']
        assert s['errors'] == 0 and s['timeouts'] == 0, s['problems'][:5]
        assert s['covers'] > 0 and s['reached'] == s['covers'], s       # every cover answered and parsed
        scripts = [f for f in os.listdir(os.path.join(out, 'smoke')) if f.endswith(f'.{tool}.tcl')]
        assert len(scripts) == s['jobs'], (len(scripts), s['jobs'])
        counts[tool] = (s['jobs'], s['covers'])
    # F2: the FV_SVA form of the generated code is legal SV (concurrent assume / cover property)
    out = os.path.join(work, 'fv_jasper')
    smoke = os.path.join(out, 'smoke')
    open(os.path.join(out, 'prim_assert_dummy_macros.svh'), 'w').write('')
    tops = sorted(f[:-3] for f in os.listdir(smoke) if f.endswith('.sv') and not f.endswith('_lib.sv'))
    for top in tops:
        run(['verilator', '--lint-only', '-Wno-fatal', '-Wno-lint', '-Wno-style', '-Wno-MULTITOP', '+define+FV_SVA',
             '+define+FORMAL', '-I' + out, '--top-module', top, os.path.join(smoke, top + '_lib.sv'),
             os.path.join(smoke, top + '.sv')], cwd=smoke)
    txt = ''.join(open(os.path.join(smoke, f)).read() for f in os.listdir(smoke) if f.endswith('.sv'))
    assert '(* anyseq *)' not in txt and '`FV_ANYSEQ' in txt
    return counts, len(tops)


def test_free_signal_paths():
    """hierarchical names of the free signals: through instances, below the top"""
    lib = '''module leaf (input logic clk);
  `FV_ANYSEQ logic [3:0] nxt_a;
  `FV_ANYSEQ logic signed [7:0] nxt_b;
endmodule
module mid; leaf #(.P(1)) u_leaf (.clk(clk)); `FV_ANYSEQ logic m; endmodule
module top; mid u_mid (); leaf u2 (.clk(clk)); `FV_ANYSEQ logic randomize; endmodule
'''
    got = sorted(fvtools.free_signals([lib], 'top'))
    assert got == sorted(['randomize', 'u_mid.m', 'u_mid.u_leaf.nxt_a', 'u_mid.u_leaf.nxt_b', 'u2.nxt_a', 'u2.nxt_b']), got
    log = 'x\nFVCOVER covered 4 top.u_cg.cov_a__b\nFVCOVER unreachable  top.u_cg.cov_c\nFVCOVER undetermined  top.u.cov_d\n'
    st, reached, unreached, _ = fvtools.parse_fvcover(log, {'covered': True, 'unreachable': False})
    assert (st, reached, unreached) == ('PASS', {'cov_a__b': 4}, {'cov_c'}), (st, reached, unreached)


if __name__ == '__main__':
    test_free_signal_paths()
    print('F0: free-signal paths and result parsing')
    counts, n = test_commercial_backends()
    print(f'F1: jasper / vcf stand-ins: {counts} (jobs, covers), every cover parsed as reached')
    print(f'F2: {n} job tops lint clean with FV_SVA (concurrent SVA)')
