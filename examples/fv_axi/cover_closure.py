#!/usr/bin/env python3
"""Coverage closure for the qs-ques AXI master with its own UVM test as the environment.

    cover_closure.py [-o OUT] [--test axi_rw_rand_test] [--depth 40] [--replay]

1. fvgen --agent-tests (once): every test as an RTL model (fvproc.py), tests that differ only in parameters
   sharing one; --test picks the test's model and parameter values
2. axi_cov_top.sv: that model around the DUT, a cover per axi_coverage bin
3. yosys-smtbmc -c: the shortest run of the testbench reaching each bin (OUT/tr/*.vcd)
4. fvreplay.py: fv_replay.sv (override classes, applied with +FV_REPLAY) and a <trace>.replay per trace
5. --replay: build the UVM testbench with fv_replay.sv in Verilator and replay every trace
   (driver_check/replay_check.py: both buses must match the formal trace at every edge up to the cover)

Environment: QS (qs-ques checkout), UVM_SRC (UVM src dir), as for the Makefile.
"""
import argparse, json, os, re, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
QS = os.environ.get('QS', os.path.expanduser('~/work/sniper_corpus/qs-ques'))
UVM = os.environ.get('UVM_SRC', os.path.expanduser('~/work/sniper_corpus/uvm-core/src'))
AXI = f'{QS}/tutorials/sv_testbench/axi_slave_testbench'
TB = f'{AXI}/uvm_tb'
PRIM = f'{QS}/tutorials/formal_verification'
DUT = [f'{AXI}/axi_pkg.sv', f'{QS}/questions/shared/verilog/qs_find_first.sv',
       f'{QS}/questions/shared/verilog/qs_pop_cnt.sv', f'{AXI}/axi_master.sv']
# the formal prep sby uses for cover mode. Free variables first: yosys-slang drives the model's (* anyseq *)
# variables with x, and every other x (an out-of-range read) is 0, as in the testbench's two-state types --
# never a free value the testbench could not produce
PREP = ('proc; setundef -anyseq a:anyseq %ci1:+[Y]; setundef -zero; setundef -undriven -anyseq; '
        'prep -top axi_cov_top; flatten; opt; hierarchy -smtcheck; scc -select; simplemap; select -clear; '
        'memory_nordff; async2sync; chformal -assume -early; opt_clean; formalff -setundef -clk2ff -ff2anyinit '
        '-hierarchy; chformal -live -fair -remove; opt_clean; check; setundef -undriven -anyseq; opt -fast; '
        'opt_clean; hierarchy -smtcheck; delete */t:$print; formalff -assume; dffunmap')


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode: sys.exit(f'{cmd[0]} failed:\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}')
    return r


def bin_name(cover):
    """g_kind[1].g_lanes[2].g_at[0].cov_cross -> cross WR_lanes2_at0 (axi_coverage's key)"""
    ix = {k: int(v) for k, v in re.findall(r'g_(\w+?)\[(\d+)\]', cover)}
    kind = 'WR' if ix.get('kind') else 'RD'
    leaf = cover.split('.')[-1]
    if leaf == 'cov_cross': return f'cross {kind}_lanes{ix["lanes"]}_at{ix["at"]}'
    if leaf == 'cov_shape': return f'shape {kind}_head{ix["head"]}_tail{ix["tail"]}'
    if leaf == 'cov_axi_id': return f'axi_id {ix["axi_id"]}'
    if leaf == 'cov_mem_id': return f'mem_id {ix["mem_id"]}'
    return 'env ' + leaf[4:] if leaf.startswith('env_') else leaf


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('-o', '--out', default='cover_closure')
    ap.add_argument('--json', default=os.path.join(HERE, 'axi_uvm_tb_constructs.json'))
    ap.add_argument('--test', default='axi_rw_rand_test')
    ap.add_argument('--models', help='directory of the per-test models (default: OUT/../fv_tests, built once)')
    ap.add_argument('--regen', action='store_true', help='rebuild the models')
    ap.add_argument('--depth', type=int, default=40)
    ap.add_argument('--solver', default='boolector')
    ap.add_argument('--replay', action='store_true', help='build the testbench with fv_replay.sv and replay')
    ap.add_argument('--formal-tool', default='yosys', choices=['yosys', 'jasper', 'vcf'],
                    help='jasper / vcf: write the cover search as a batch Tcl script (OUT/cover.<tool>.tcl) to run '
                         'with the tool, instead of running yosys-smtbmc')
    a = ap.parse_args()
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)

    # every test of the testbench, once: tests that differ only in parameters share a model
    models = os.path.abspath(a.models or os.path.join(out, '..', 'fv_tests'))
    if a.regen or not os.path.exists(f'{models}/fv_tests.json'):
        print(f'== models of every test: fvgen --agent-tests -> {models}')
        run([sys.executable, os.path.join(ROOT, 'fvgen.py'), a.json, '-o', models, '--agent-tests'])
    idx = json.load(open(f'{models}/fv_tests.json'))
    if a.test not in idx['tests']:
        sys.exit(f'{a.test}: no model ({idx["failed"].get(a.test, "not a uvm_test of the extraction")})')
    model = idx['tests'][a.test]
    ov = idx['groups'][model]['tests'][a.test]
    print(f'== {a.test}: model {model} (shared by {len(idx["groups"][model]["tests"])} tests)')
    for f in (f'{model}.sv', f'{model}.json', 'fv_tool.svh'):
        if os.path.exists(f'{models}/{f}'): shutil.copy(f'{models}/{f}', out)
    shutil.copy(os.path.join(HERE, 'axi_cov_top.sv'), out)

    if a.formal_tool != 'yosys':
        sys.path.insert(0, ROOT)
        import fvtools
        with open(f'{out}/fv_test_params.svh', 'w') as f:
            f.write('`define FV_TEST_PARAMS ' + ('#(' + ', '.join(f'.{k}({v})' for k, v in sorted(ov.items())) + ')'
                                                 if ov else '') + '\n')
        texts = [open(f'{out}/{model}.sv').read(), open(f'{out}/axi_cov_top.sv').read()]
        free = ['u_tb.' + x for x in fvtools.FREE_DECL.findall(texts[0])]     # the model's free choices (instance u_tb)
        defs = ['FORMAL', 'FV_SVA', 'FV_MODEL_NO_ASSERT', f'FV_TEST={model}']
        files = DUT + [f'{out}/{model}.sv', f'{out}/fv_test_params.svh', f'{out}/axi_cov_top.sv']
        incs = [out, PRIM]
        if a.formal_tool == 'jasper':
            L = ['clear -all', 'analyze -sv12 ' + ' '.join(f'+define+{d}' for d in defs) + ' ' +
                 ' '.join(f'+incdir+{i}' for i in incs) + ' ' + ' '.join(files),
                 'elaborate -top axi_cov_top', 'clock clk', 'reset -none']
            L += [f'stopat -env {{axi_cov_top.{x}}}' for x in free]
            L += [f'prove -property [get_property_list -include {{type cover}}] -max_trace_length {a.depth}',
                  'foreach p [get_property_list -include {type cover}] {',
                  '  puts "FVCOVER [get_status $p] [get_trace_length -quiet $p] $p"', '}', 'exit -force']
        else:
            opts = ' '.join(f'+define+{d}' for d in defs) + ' ' + ' '.join(f'+incdir+{i}' for i in incs)
            L = ['set_fml_appmode FPV', f'read_file -top axi_cov_top -format sverilog -sva -vcs {{-sverilog {opts} '
                 f'{" ".join(files)}}}', 'create_clock clk -period 100', 'sim_run -stable', 'sim_save_reset']
            L += [f'snip_driver {{axi_cov_top.{x}}}' for x in free]
            L += [f'check_fv -depth {a.depth} -block', 'foreach_in_collection p [get_props -type cover] {',
                  '  puts "FVCOVER [get_attribute $p status] [get_attribute -quiet $p depth] [get_attribute $p name]"',
                  '}', 'quit']
        open(f'{out}/cover.{a.formal_tool}.tcl', 'w').write('\n'.join(L) + '\n')
        print(f'== {out}/cover.{a.formal_tool}.tcl: {len(free)} free signals, {a.depth} cycles. Not run here: run it '
              f'with the tool; to replay, save each covered trace as a VCD with the model\'s signals '
              f'(u_tb.fv_ch*_v / _u) and give them to fvreplay.py --trace NAME=file.vcd')
        return

    print('== formal top -> SMT')
    # the test's parameters: its configure() knobs and the constants of its own code
    if ov: print('   parameters: ' + ', '.join(f'{k}={v}' for k, v in sorted(ov.items())))
    with open(f'{out}/fv_test_params.svh', 'w') as f:
        f.write('`define FV_TEST_PARAMS ' + ('#(' + ', '.join(f'.{k}({v})' for k, v in sorted(ov.items())) + ')' if ov else '')
                + '\n')
    ys = (f'plugin -i slang; read_slang --single-unit --unroll-limit 1000000 -U SYNTHESIS -DFORMAL -DFV_MODEL_NO_ASSERT '
          f'-DFV_TEST={model} -I {out} -I {PRIM} --top axi_cov_top {" ".join(DUT)} {out}/{model}.sv '
          f'{out}/fv_test_params.svh {out}/axi_cov_top.sv; hierarchy -top axi_cov_top; {PREP}; write_smt2 -wires {out}/m.smt2')
    run(['yosys', '-q', '-l', f'{out}/yosys.log', '-p', ys])

    print(f'== cover search: {a.solver}, depth {a.depth}')
    shutil.rmtree(f'{out}/tr', ignore_errors=True); os.makedirs(f'{out}/tr')
    r = subprocess.run(['yosys-smtbmc', '-s', a.solver, '-c', '-t', str(a.depth), '--noprogress',
                        '--dump-vcd', f'{out}/tr/cov_%.vcd', f'{out}/m.smt2'], capture_output=True, text=True,
                       env=dict(os.environ, PYTHONUNBUFFERED='1'))
    open(f'{out}/cover.log', 'w').write(r.stdout + r.stderr)
    traces, cur, reached, unreached = {}, [], {}, []
    for line in r.stdout.split('\n'):
        m = re.search(r'Reached cover statement in step (\d+) at axi_cov_top: \\?(\S+)', line)
        if m:
            cur.append(bin_name(m.group(2))); reached[bin_name(m.group(2))] = int(m.group(1))
        m = re.search(r'Writing trace to VCD file: (\S+)', line)
        if m:
            traces[os.path.splitext(os.path.basename(m.group(1)))[0]] = cur; cur = []
        m = re.search(r'Unreached cover statement at axi_cov_top: \\?(\S+)', line)
        if m: unreached.append(bin_name(m.group(1)))
    names = {f'\\{c}' if not c.startswith('$') else c: bin_name(c) for c in
             re.findall(r'cover statement (?:in step \d+ )?at axi_cov_top: \\?(\S+)', r.stdout)}
    json.dump(names, open(f'{out}/cover_names.json', 'w'), indent=0)
    bins = [b for b in reached if not b.startswith('env ')]
    print(f'   {len(bins)} bins reached (deepest at edge {max(reached.values(), default=0)}), '
          f'{len(unreached)} unreached{": " + ", ".join(unreached) if unreached else ""}')

    print('== replay classes and values')
    args = []
    for t in sorted(traces): args += ['--trace', f'{t}={out}/tr/{t}.vcd']
    run([sys.executable, os.path.join(ROOT, 'fvreplay.py'), a.json, f'{out}/{model}.json', '--src', TB,
         '--import', 'axi_uvm_pkg', '-o', f'{out}/fv_replay.sv', *args])
    rep = {'test': a.test, 'depth': a.depth, 'reached': reached, 'unreached': unreached,
           'traces': {t: {'covers': c, 'edge': max(reached[x] for x in c)} for t, c in traces.items() if c}}
    json.dump(rep, open(f'{out}/closure.json', 'w'), indent=1)
    with open(f'{out}/closure.md', 'w') as f:
        f.write(f'# Coverage closure: {a.test}\n\n{len(bins)} axi_coverage bins reached, {len(unreached)} '
                f'unreached, within {a.depth} cycles.\n\n| trace | edge | bins |\n|---|---|---|\n')
        for t, x in sorted(rep['traces'].items(), key=lambda kv: int(re.sub(r'\D', '', kv[0]))):
            f.write(f'| `{t}.replay` | {x["edge"]} | {", ".join(x["covers"])} |\n')
        f.write(f'\nReplay one: run `{a.test}` with `fv_replay.sv` compiled in and `+FV_REPLAY=<trace>.replay`.\n')

    if a.replay:
        print('== Verilator build with fv_replay.sv')
        dc = os.path.join(HERE, 'driver_check')
        inc = [f'+incdir+{d}' for d in (UVM, f'{TB}/tb', f'{TB}/mem_agent', f'{TB}/axi_agent', f'{TB}/env', f'{TB}/tests')]
        run(['verilator', '--binary', '--timing', '-j', '8', '-Wno-fatal', '-Wno-lint', '-Wno-style',
             '--timescale-override', '1ns/1ps', '--top-module', 'tb_top', '-Mdir', f'{out}/sim',
             '+define+UVM_NO_DPI', '+define+UVM_REPORT_DISABLE_FILE_LINE', *inc, f'{UVM}/uvm_pkg.sv', *DUT,
             f'{TB}/tb/mem_bus_if.sv', f'{TB}/tb/axi_bus_if.sv', f'{TB}/tb/axi_uvm_pkg.sv', f'{out}/fv_replay.sv',
             f'{TB}/tb/tb_top.sv', f'{dc}/axi_trace_rec.sv', f'{dc}/mem_trace_rec.sv', '-o', 'Vtb_top'])
        print('== replay every trace')
        r = subprocess.run([sys.executable, f'{dc}/replay_check.py', f'{out}/sim/Vtb_top', out, out,
                            '--test', a.test], text=True)
        sys.exit(r.returncode)


if __name__ == '__main__':
    main()
