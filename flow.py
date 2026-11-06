"""flow: run sniper then fvgen for one testbench, described by a project file (TOML).

    python flow.py project.toml [-o OUT] [--steps sniper,fvgen] [--dry-run]

The project file has one table per tool; see examples/ibex_icache/flow.toml:

    [testbench]   sniper: own (testbench sources), args (slang arguments), and optionally pkg,
                  skip_pkg, hier_depth
    [fvgen]       any fvgen option by its name, '-' or '_' (run = true, witness = true, jobs = 8,
                  covergroups = "...", ...)
    [rtl]         optional, run the covergroups on the DUT: the keys of an fvgen --rtl-config file
                  (top, filelists, binds, bind_tops, defines, clock, reset, reset_active_low, depth),
                  or config = "rtl_config.toml"

Paths are relative to the project file, ${VAR} is taken from the environment (also inside slang
filelists), sniper runs in the project file's directory. Outputs (-o OUT, default flow_out): OUT/tb.json (sniper), OUT/fv/
(fvgen: report.md, fv_*.sv, ...). Each command is printed before it runs, so the flow can be
rerun by hand, one tool at a time.
"""
import argparse, os, re, shlex, subprocess, sys, tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fvgen import RTL_KEYS

TB_KEYS = {'own': list, 'args': list, 'pkg': list, 'skip_pkg': list, 'hier_depth': int}
RTL_PATHS = ('filelists', 'binds')
RTL_FLAGS = {'top': '--rtl-top', 'filelists': '--rtl', 'binds': '--rtl-bind', 'bind_tops': '--rtl-bind-top',
             'defines': '--rtl-define', 'clock': '--rtl-clock', 'reset': '--rtl-reset',
             'reset_active_low': '--rtl-reset-low', 'depth': '--rtl-depth'}


def expand(v, where):
    """${VAR} from the environment, in every string of the project file"""
    if isinstance(v, dict): return {k: expand(x, f'{where}.{k}') for k, x in v.items()}
    if isinstance(v, list): return [expand(x, where) for x in v]
    if not isinstance(v, str): return v
    unset = [m for m in re.findall(r'\$\{(\w+)\}|\$(\w+)', v) for m in m if m and m not in os.environ]
    if unset: sys.exit(f'{where}: environment variable {", ".join(unset)} is not set')
    return os.path.expandvars(v)


def check(tbl, keys, where):
    unknown = set(tbl) - set(keys)
    if unknown: sys.exit(f"{where}: unknown keys {', '.join(sorted(unknown))} (known: {', '.join(keys)})")
    for k, t in keys.items():
        if k in tbl and not isinstance(tbl[k], t): sys.exit(f'{where}.{k} must be a {t.__name__}')


def options(tbl):
    """a TOML table as command-line options: true -> --flag, false -> left out, list -> repeated"""
    out = []
    for k, v in tbl.items():
        flag = '--' + k.replace('_', '-')
        if v is True: out.append(flag)
        elif v is False: continue
        else: out += [x for e in (v if isinstance(v, list) else [v]) for x in (flag, str(e))]
    return out


def commands(proj, path, out):
    base = os.path.dirname(os.path.abspath(path))
    rel = lambda p: os.path.normpath(os.path.join(base, p))
    unknown = set(proj) - {'testbench', 'fvgen', 'rtl'}
    if unknown: sys.exit(f"{path}: unknown tables {', '.join(sorted(unknown))} (known: testbench, fvgen, rtl)")
    tb = proj.get('testbench')
    if not tb or not tb.get('own') or not tb.get('args'):
        sys.exit(f'{path}: [testbench] needs own (testbench sources) and args (slang arguments)')
    check(tb, TB_KEYS, f'{path}: testbench')
    js = os.path.join(out, 'tb.json')
    sn = [sys.executable, os.path.join(HERE, 'sniper.py'), '-o', js]
    sn += [x for o in tb['own'] for x in ('--own', rel(o))]
    sn += options({k: v for k, v in tb.items() if k not in ('own', 'args')})
    sn += ['--'] + tb['args']
    fv = [sys.executable, os.path.join(HERE, 'fvgen.py'), js, '-o', os.path.join(out, 'fv')]
    fv += options(proj.get('fvgen', {}))
    rtl = dict(proj.get('rtl', {}))
    if 'config' in rtl:
        if len(rtl) > 1: sys.exit(f'{path}: [rtl] has config = "..." or the config keys, not both')
        fv += ['--rtl-config', rel(rtl['config'])]
    elif rtl:
        for k in ('clock', 'reset'):        # one port or a list
            if isinstance(rtl.get(k), str): rtl[k] = [rtl[k]]
        check(rtl, RTL_KEYS, f'{path}: rtl')
        for k in RTL_PATHS:
            if k in rtl: rtl[k] = [rel(p) for p in rtl[k]]
        for k, v in rtl.items():
            if k == 'reset_active_low': v = str(v).lower()
            fv += [x for e in (v if isinstance(v, list) else [v]) for x in (RTL_FLAGS[k], str(e))]
    return base, [('sniper', sn), ('fvgen', fv)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('project', help='project file (TOML)')
    ap.add_argument('-o', '--out', default='flow_out', help='output directory (default: flow_out)')
    ap.add_argument('--steps', default='sniper,fvgen', help='steps to run, of sniper,fvgen')
    ap.add_argument('--dry-run', action='store_true', help='print the commands only')
    a = ap.parse_args()
    with open(a.project, 'rb') as f: proj = expand(tomllib.load(f), a.project)
    out = os.path.abspath(a.out)
    base, cmds = commands(proj, a.project, out)
    steps = a.steps.split(',')
    bad = set(steps) - {s for s, _ in cmds}
    if bad: sys.exit(f"--steps: unknown {', '.join(sorted(bad))} (known: sniper, fvgen)")
    os.makedirs(out, exist_ok=True)
    for step, cmd in cmds:
        if step not in steps: continue
        print(f'== {step}: (cd {shlex.quote(base)} && {shlex.join(cmd)})', flush=True)
        if a.dry_run: continue
        if subprocess.run(cmd, cwd=base).returncode:
            sys.exit(f'{step} failed')
    if not a.dry_run and 'fvgen' in steps:
        print(f"== report: {os.path.join(out, 'fv', 'report.md')}")


if __name__ == '__main__':
    main()
