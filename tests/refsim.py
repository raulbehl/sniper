"""Reference simulator for the LRM conformance tests (test_lrm.py): which simulator compiles and runs the
testbenches whose randomize() / covergroups are compared with fvgen.

    SNIPER_REF_SIM=verilator (default) | vcs | xcelium | questa

Verilator is what the tests run on today. The commercial backends are written from the tools' documented
command lines and have not been run yet (no license at the time of writing): the first run on each should
be checked, especially coverage(), which parses the tools' text coverage reports.
"""
import glob, os, re, subprocess

NAME = os.environ.get('SNIPER_REF_SIM', 'verilator')


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


class Sim:
    name = None
    coverage_supported = False

    def build(self, files, top, bdir, incdirs=(), defines=(), coverage=False):
        """compile+elaborate into bdir; returns CompletedProcess (returncode != 0: build failed)"""
        raise NotImplementedError

    def run(self, bdir, cwd, plusargs=(), seed=1):
        raise NotImplementedError

    def coverage(self, bdir, cwd):
        """{'<covergroup instance path>.<coverpoint/cross>.<bin>': hit count} after run()"""
        raise NotImplementedError(f'{self.name}: covergroup bin counts not read yet')


class Verilator(Sim):
    name = 'verilator'
    coverage_supported = True

    def build(self, files, top, bdir, incdirs=(), defines=(), coverage=False):
        os.makedirs(bdir, exist_ok=True)
        cmd = ['verilator', '--binary', '-Wno-fatal', '-Wno-lint', '-Wno-style', '-Wno-CONSTRAINTIGN',
               '-Wno-COVERIGN', '--top-module', top, '-Mdir', bdir, '-o', 'v']
        cmd += ['-I' + d for d in incdirs] + ['+define+' + d for d in defines]
        if coverage: cmd.append('--coverage')
        return _run(cmd + list(files), bdir)

    def run(self, bdir, cwd, plusargs=(), seed=1):
        return _run([os.path.join(bdir, 'v'), f'+verilator+seed+{seed}', *plusargs], cwd)

    def coverage(self, bdir, cwd):
        out = {}
        txt = open(os.path.join(cwd, 'coverage.dat'), errors='replace').read()
        for m in re.finditer(r"C '([^']+)' (\d+)", txt):
            e = m.group(1)
            if '\x01t\x02covergroup' not in e: continue
            h = re.search(r'\x01h\x02([^\x01]+)', e)
            if h: out[h.group(1)] = out.get(h.group(1), 0) + int(m.group(2))
        return out


class Vcs(Sim):
    name = 'vcs'
    coverage_supported = True

    def build(self, files, top, bdir, incdirs=(), defines=(), coverage=False):
        os.makedirs(bdir, exist_ok=True)
        cmd = ['vcs', '-full64', '-sverilog', '-timescale=1ns/1ps', '-top', top, '-o', os.path.join(bdir, 'simv'),
               '-Mdir=' + os.path.join(bdir, 'csrc')]
        cmd += ['+incdir+' + d for d in incdirs] + ['+define+' + d for d in defines]
        if coverage: cmd += ['-cm', 'cond', '-cm_dir', os.path.join(bdir, 'cov.vdb')]
        return _run(cmd + list(files), bdir)

    def run(self, bdir, cwd, plusargs=(), seed=1):
        return _run([os.path.join(bdir, 'simv'), f'+ntb_random_seed={seed}', *plusargs], cwd)

    def coverage(self, bdir, cwd):
        rd = os.path.join(bdir, 'urg')
        _run(['urg', '-full64', '-dir', os.path.join(bdir, 'cov.vdb'), '-format', 'text', '-report', rd], cwd)
        return parse_bin_table(''.join(open(f, errors='replace').read() for f in glob.glob(os.path.join(rd, 'groups*.txt'))))


class Xcelium(Sim):
    name = 'xcelium'
    coverage_supported = True

    def build(self, files, top, bdir, incdirs=(), defines=(), coverage=False):
        os.makedirs(bdir, exist_ok=True)
        cmd = ['xrun', '-elaborate', '-sv', '-timescale', '1ns/1ps', '-top', top, '-xmlibdirpath', bdir]
        cmd += ['-incdir ' + d for d in incdirs] + ['-define ' + d for d in defines]
        if coverage: cmd += ['-coverage', 'functional']
        return _run(cmd + list(files), bdir)

    def run(self, bdir, cwd, plusargs=(), seed=1):
        return _run(['xrun', '-R', '-xmlibdirpath', bdir, '-svseed', str(seed), '-covworkdir', os.path.join(bdir, 'cov'),
                     '-covscope', 'lrm', '-covtest', 'run', '-covoverwrite', *plusargs], cwd)

    def coverage(self, bdir, cwd):
        tcl = os.path.join(bdir, 'report.tcl')
        open(tcl, 'w').write('report -detail -metrics covergroup -all\n')
        r = _run(['imc', '-load', os.path.join(bdir, 'cov', 'lrm', 'run'), '-exec', tcl], cwd)
        return parse_bin_table(r.stdout)


class Questa(Sim):
    name = 'questa'
    coverage_supported = True

    def build(self, files, top, bdir, incdirs=(), defines=(), coverage=False):
        os.makedirs(bdir, exist_ok=True)
        lib = os.path.join(bdir, 'work')
        r = _run(['vlib', lib], bdir)
        cmd = ['vlog', '-sv', '-work', lib] + ['+incdir+' + d for d in incdirs] + ['+define+' + d for d in defines]
        r = _run(cmd + list(files), bdir)
        if r.returncode: return r
        self.top = top
        return _run(['vopt', '-work', lib, top, '-o', top + '_opt', '+acc'], bdir)

    def run(self, bdir, cwd, plusargs=(), seed=1):
        lib = os.path.join(bdir, 'work')
        ucdb = os.path.join(bdir, 'cov.ucdb')
        return _run(['vsim', '-c', '-lib', lib, f'{self.top}_opt', '-sv_seed', str(seed), *plusargs,
                     '-do', f'coverage save -onexit {ucdb}; run -all; quit -f'], cwd)

    def coverage(self, bdir, cwd):
        r = _run(['vcover', 'report', '-details', '-cvg', os.path.join(bdir, 'cov.ucdb')], cwd)
        return parse_bin_table(r.stdout)


def parse_bin_table(txt):
    """best effort over a text covergroup report: lines '<bin name> <hits> ...' under covergroup /
    coverpoint headers (VCS urg, Questa vcover, Xcelium imc). To be validated on each tool."""
    out, scope = {}, []
    for line in txt.splitlines():
        h = re.match(r'\s*(?:Covergroup|TYPE|INST|Coverpoint|Cross|Group|Variable)\s*(?:instance)?\s*[:]?\s*(\S+)', line, re.I)
        if h:
            name = h.group(1).strip(':')
            if re.match(r'\s*(Covergroup|TYPE|INST|Group)', line, re.I): scope = [name]
            else: scope = scope[:1] + [name]
            continue
        b = re.match(r'\s*(?:bin\s+)?([\w\[\]:=>$,\- ]+?)\s+(\d+)\s+(?:\d+\s+)?(?:Covered|ZERO|\d)', line)
        if b and scope:
            out['.'.join(scope + [b.group(1).strip()])] = int(b.group(2))
    return out


BACKENDS = {c.name: c for c in (Verilator, Vcs, Xcelium, Questa)}


def get():
    if NAME not in BACKENDS: raise SystemExit(f'SNIPER_REF_SIM={NAME}: one of {", ".join(BACKENDS)}')
    return BACKENDS[NAME]()
