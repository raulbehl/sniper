"""fvgen's formal backends: run one cover job (a top, the generated modules it needs, its covers) and
report which covers were reached and at which step.

    fvgen ... --formal-tool yosys (default) | jasper | vcf

yosys:  yosys (+ yosys-slang for RTL) and yosys-smtbmc, what the jobs' .sby files describe.
jasper: Cadence JasperGold, batch Tcl (jg -batch -tcl <top>.tcl).
vcf:    Synopsys VC Formal, batch Tcl (vcf -batch -f <top>.tcl).

The generated SV is the same for every tool: with FV_SVA defined (the commercial scripts define it) the
assumptions / covers are concurrent SVA and `FV_ANYSEQ` is empty; the signals it marks are made free by
the script (stopat -env / snip_driver), found by scanning the generated code. The commercial backends are
written from the tools' documented Tcl commands and have not been run yet (no license at the time of
writing): check the first run on each, especially the result parsing.
"""
import os, re

FREE_DECL = re.compile(r'`FV_ANYSEQ\s+(?:logic|bit)(?:\s+signed)?(?:\s*\[[^\]]*\])*\s+(\w+)\s*;')
MODULE = re.compile(r'\bmodule\s+(\w+)\b(.*?)\bendmodule\b', re.S)
INSTANCE = re.compile(r'(?:^|;)\s*(\w+)\s+(?:#\s*\(.*?\)\s*)?(\w+)\s*\(', re.S | re.M)
NOT_TYPES = {'module', 'assign', 'always', 'always_ff', 'always_comb', 'initial', 'wire', 'logic', 'if', 'else',
             'begin', 'end', 'for', 'assert', 'assume', 'cover', 'function', 'return', 'case', 'input', 'output'}


def free_signals(texts, top):
    """hierarchical names (below the top) of every `FV_ANYSEQ signal in the job's generated code"""
    mods = {m.group(1): m.group(2) for t in texts for m in MODULE.finditer(t)}
    out = []

    def walk(mod, prefix, depth=0):
        body = mods.get(mod)
        if body is None or depth > 8: return
        out.extend(prefix + s for s in FREE_DECL.findall(body))
        for typ, inst in INSTANCE.findall(body):
            if typ in mods and typ not in NOT_TYPES: walk(typ, f'{prefix}{inst}.', depth + 1)
    walk(top, '')
    return out


class Tool:
    name = None
    defines = ()            # extra defines for reading the generated code

    def prepare(self, job, gen):
        """write what the job needs next to it; returns the command lines to run in job.d, in order"""
        raise NotImplementedError

    def parse(self, job, log):
        """(status PASS/FAIL/ERROR, reached {cover: step or None}, unreached set, traces {cover: vcd})"""
        raise NotImplementedError


class Yosys(Tool):
    name = 'yosys'

    def prepare(self, job, gen):
        return list(job.commands(gen))

    def parse(self, job, log):
        if 'Status: PASSED' in log or 'Status: FAILED' in log: status = 'PASS' if 'PASSED' in log else 'FAIL'
        else: status = 'ERROR'
        reached, unreached, pending, traces = {}, set(), [], {}
        for line in log.splitlines():
            mm = re.search(r'Reached cover statement in step (\d+) at \S+: (\S+)', line)
            if mm:      # flattened designs name covers by instance path (u_cg.cov_x)
                c = mm.group(2).split('.')[-1]
                reached[c] = int(mm.group(1)) // (2 if job.multiclock else 1); pending.append(c); continue
            mm = re.search(r'Writing trace to VCD file: (\S+)', line)
            if mm:
                for c in pending: traces[c] = os.path.join(job.d, mm.group(1))
                pending = []
                continue
            mm = re.search(r'Unreached cover statement at \S+: (\S+)', line)
            if mm: unreached.add(mm.group(1).split('.')[-1])
        return status, reached, unreached, traces


def read_list(job, gen):
    """files and options of a job for a commercial tool: generated code, plus the RTL for RTL jobs"""
    out_dir = os.path.abspath(os.path.dirname(job.d))
    files = [os.path.join(job.d, f'{job.top}_lib.sv'), os.path.join(job.d, f'{job.top}.sv')]
    defs = ['FORMAL', 'FV_SVA'] + [d[2:] for d in job.defs.split() if d.startswith('-D')]
    rtl = getattr(job, 'rtl_read', None) or {}
    return files, defs, [out_dir] + rtl.get('incdirs', []), rtl


class Jasper(Tool):
    name = 'jasper'

    def prepare(self, job, gen):
        files, defs, incs, rtl = read_list(job, gen)
        texts = [open(f).read() for f in files]
        free = free_signals(texts, job.top)
        L = ['clear -all', 'set_proofgrid_bridge off']
        if rtl.get('filelists'):
            L.append('analyze -sv12 ' + ' '.join(f'+define+{d}' for d in defs + rtl.get('defines', [])) + ' ' +
                     ' '.join(f'-f {f}' for f in rtl['filelists']))
        L.append('analyze -sv12 ' + ' '.join(f'+define+{d}' for d in defs) + ' ' + ' '.join(f'+incdir+{i}' for i in incs)
                 + ' ' + ' '.join(files))
        L.append(f'elaborate -top {job.top}')
        L.append('clock clk' + (' -both_edges' if job.multiclock else ''))
        L.append('reset -none')              # the tops hold their own reset for the first cycles
        for s in free: L.append(f'stopat -env {{{job.top}.{s}}}')
        # design assertions are not proven here: only the covers (assumptions constrain them)
        L.append(f'set_prove_per_property_max_time_limit {gen.a.timeout}s')
        L.append(f'prove -property [get_property_list -include {{type cover}}] -bg -engine_mode {{Ht Hp N}} '
                 f'-max_trace_length {job.depth}')
        L.append('foreach p [get_property_list -include {type cover}] {')
        L.append('  puts "FVCOVER [get_status $p] [get_trace_length -quiet $p] $p"')
        L.append('}')
        L.append('exit -force')
        tcl = os.path.join(job.d, job.top + '.jasper.tcl')
        open(tcl, 'w').write('\n'.join(L) + '\n')
        return [['jg', '-batch', '-no_gui', '-tcl', tcl]]

    def parse(self, job, log):
        return parse_fvcover(log, {'covered': True, 'unreachable': False})


class VcFormal(Tool):
    name = 'vcf'

    def prepare(self, job, gen):
        files, defs, incs, rtl = read_list(job, gen)
        texts = [open(f).read() for f in files]
        free = free_signals(texts, job.top)
        opts = ' '.join(f'+define+{d}' for d in defs + rtl.get('defines', [])) + ' ' + \
            ' '.join(f'+incdir+{i}' for i in incs)
        src = ' '.join([f'-f {f}' for f in rtl.get('filelists', [])] + files)
        L = ['set_fml_appmode FPV',
             f'read_file -top {job.top} -format sverilog -sva -vcs {{-sverilog {opts} {src}}}',
             'create_clock clk -period 100' + ('' if not job.multiclock else '   ;# both edges are used by the design'),
             'sim_run -stable', 'sim_save_reset']
        for s in free: L.append(f'snip_driver {{{job.top}.{s}}}')
        L += [f'set_fml_var fml_max_time {max(1, gen.a.timeout // 60)}M',
              f'set_fml_var fml_cov_tgl_input_port false',
              f'check_fv -depth {job.depth} -block',
              'foreach_in_collection p [get_props -type cover] {',
              '  puts "FVCOVER [get_attribute $p status] [get_attribute -quiet $p depth] [get_attribute $p name]"',
              '}', 'quit']
        tcl = os.path.join(job.d, job.top + '.vcf.tcl')
        open(tcl, 'w').write('\n'.join(L) + '\n')
        return [['vcf', '-batch', '-f', tcl]]

    def parse(self, job, log):
        return parse_fvcover(log, {'covered': True, 'uncoverable': False, 'unreachable': False})


def parse_fvcover(log, meaning):
    """lines 'FVCOVER <status> <trace length or empty> <property path>' printed by the commercial scripts"""
    reached, unreached = {}, set()
    seen = False
    for line in log.splitlines():
        m = re.match(r'FVCOVER\s+(\w+)\s+(\d*)\s*(\S+)', line.strip())
        if not m: continue
        seen = True
        st, n, name = m.group(1).lower(), m.group(2), re.split(r'[.:/]', m.group(3))[-1]
        if meaning.get(st) is True: reached[name] = int(n) if n else None
        elif meaning.get(st) is False: unreached.add(name)
    return ('PASS' if seen else 'ERROR'), reached, unreached, {}


TOOLS = {t.name: t for t in (Yosys, Jasper, VcFormal)}


def get(name):
    if name not in TOOLS: raise SystemExit(f'--formal-tool {name}: one of {", ".join(TOOLS)}')
    return TOOLS[name]()


TOOL_HEADER = '''// fv_tool.svh (generated by fvgen): what differs between formal tools in the generated code.
// yosys reads it as is; the commercial scripts define FV_SVA (concurrent SVA, free signals by command).
`ifndef FV_TOOL_SVH
`define FV_TOOL_SVH
`ifdef FV_SVA
  `define FV_ANYSEQ
  `define FV_COVER(__name, __prop, __edge) __name: cover property (@(__edge clk) disable iff (reset !== 1'b0) (__prop));
  `define FV_ASSERT_I(__name, __prop, __edge) __name: assert property (@(__edge clk) disable iff (reset !== 1'b0) (__prop));
`else
  `define FV_ANYSEQ (* anyseq *)
  `define FV_COVER(__name, __prop, __edge) always_ff @(__edge clk) if (reset === 1'b0) __name: cover (__prop);
  `define FV_ASSERT_I(__name, __prop, __edge) always_ff @(__edge clk) if (reset === 1'b0) __name: assert (__prop);
`endif
`endif
'''

SVA_MACROS = '''// fv_sva_macros.svh (generated by fvgen): prim_assert's macros as concurrent SVA, for formal tools that
// read SVA natively (included by prim_assert.sv when FV_SVA is defined; yosys uses its own macros).
`define ASSERT(__name, __prop, __clk = `ASSERT_DEFAULT_CLK, __rst = `ASSERT_DEFAULT_RST) \\
  __name: assert property (@(posedge __clk) disable iff ((__rst) !== '0) (__prop));
`define ASSUME(__name, __prop, __clk = `ASSERT_DEFAULT_CLK, __rst = `ASSERT_DEFAULT_RST) \\
  __name: assume property (@(posedge __clk) disable iff ((__rst) !== '0) (__prop));
`define COVER(__name, __prop, __clk = `ASSERT_DEFAULT_CLK, __rst = `ASSERT_DEFAULT_RST) \\
  __name: cover property (@(posedge __clk) disable iff ((__rst) !== '0) (__prop));
`define ASSERT_NEVER(__name, __prop, __clk = `ASSERT_DEFAULT_CLK, __rst = `ASSERT_DEFAULT_RST) \\
  __name: assert property (@(posedge __clk) disable iff ((__rst) !== '0) (!(__prop)));
`define ASSERT_KNOWN(__name, __sig, __clk = `ASSERT_DEFAULT_CLK, __rst = `ASSERT_DEFAULT_RST)
`define ASSERT_I(__name, __prop) __name: assert final (__prop);
`define ASSUME_I(__name, __prop) __name: assume final (__prop);
`define ASSERT_INIT(__name, __prop) initial __name: assert (__prop);
`define ASSERT_INIT_NET(__name, __prop) initial #0 __name: assert (__prop);
`define ASSERT_FINAL(__name, __prop)
`define ASSERT_NODIS(__name, __prop, __clk = `ASSERT_DEFAULT_CLK, __rst = `ASSERT_DEFAULT_RST) \\
  __name: assert property (@(posedge __clk) (__prop));
'''
