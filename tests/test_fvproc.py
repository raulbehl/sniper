#!/usr/bin/env python3
"""fvproc (testbench processes as RTL) and fvreplay on the qs-ques AXI UVM testbench.

Needs examples/fv_axi/axi_uvm_tb_constructs.json (`make -C examples/fv_axi axi_uvm_tb_constructs.json`, from
the qs-ques checkout in QS) and yosys with yosys-slang, yosys-smtbmc, boolector; skipped without them.
"""
import json, os, re, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
AXI_JSON = os.path.join(ROOT, 'examples', 'fv_axi', 'axi_uvm_tb_constructs.json')
QS = os.environ.get('QS', os.path.expanduser('~/work/sniper_corpus/qs-ques'))


def available():
    return os.path.exists(AXI_JSON) and os.path.isdir(QS) and shutil.which('yosys') and shutil.which('boolector')


def test_models_and_groups():
    import fvproc
    d = json.load(open(AXI_JSON))
    work = tempfile.mkdtemp(prefix='fvproc_')
    idx = fvproc.generate_tests(d, work, {})
    groups = {g: sorted(v['tests']) for g, v in idx['groups'].items()}
    # the six run_rand tests differ only in cfg knobs and run_rand's arguments: one model
    rr = [g for g, t in groups.items() if 'axi_rw_rand_test' in t][0]
    assert groups[rr] == ['axi_backpressure_test', 'axi_ooo_test', 'axi_rd_rand_test', 'axi_rw_rand_test',
                          'axi_stress_test', 'axi_wr_rand_test'], groups
    p = idx['groups'][rr]['tests']
    assert p['axi_rd_rand_test']['T_RUN_RAND_WR_PCT'] == "32'd0" and p['axi_backpressure_test']['RSP_READY_PCT'] == "32'd25"
    assert p['axi_rw_rand_test']['T_RUN_RAND_NO_DELAY'] == "1'b0"           # the default argument
    assert not idx['failed'], idx['failed']
    sv = open(os.path.join(work, f'{rr}.sv')).read()
    # RTL shape: a state per wait named after its line, a counter for repeat (n) @(posedge clk)
    for st in ('env__a_agent__driver__r_loop__L47_DELAY', 'env__a_agent__driver__r_loop__L54_UNTIL',
               'env__m_agent__driver__req_loop__L56_GET_ITEM'):
        assert f'localparam logic' in sv and st in sv, st
    assert 'env__a_agent__driver__r_loop__cnt_q' in sv
    assert 'ROUNDS' not in sv and 'fv_stall' not in sv                       # no delta rounds
    return work, rr, len(idx['tests']), len(groups)


def test_cover_top_bounded(work, model, depth=12):
    """the coverage top with the model compiles, and a bounded run neither dies (assumptions satisfiable)
    nor hits a model bound; the first CPU request is accepted (a bin reached) within `depth` cycles"""
    A = f'{QS}/tutorials/sv_testbench/axi_slave_testbench'
    dut = [f'{A}/axi_pkg.sv', f'{QS}/questions/shared/verilog/qs_find_first.sv',
           f'{QS}/questions/shared/verilog/qs_pop_cnt.sv', f'{A}/axi_master.sv']
    shutil.copy(os.path.join(ROOT, 'examples', 'fv_axi', 'axi_cov_top.sv'), work)
    sys.path.insert(0, os.path.join(ROOT, 'examples', 'fv_axi'))
    import cover_closure as CC
    ys = (f'plugin -i slang; read_slang --unroll-limit 1000000 -U SYNTHESIS -DFORMAL -DFV_MODEL_NO_ASSERT '
          f'-DFV_TEST={model} -I {work} -I {QS}/tutorials/formal_verification --top axi_cov_top {" ".join(dut)} '
          f'{work}/{model}.sv {work}/axi_cov_top.sv; hierarchy -top axi_cov_top; {CC.PREP}; write_smt2 -wires {work}/m.smt2')
    r = subprocess.run(['yosys', '-q', '-p', ys], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:]
    r = subprocess.run(['yosys-smtbmc', '-s', 'boolector', '-c', '-t', str(depth), '--noprogress', f'{work}/m.smt2'],
                       capture_output=True, text=True, cwd=work)
    assert 'Assert failed' not in r.stdout, r.stdout[-2000:]             # tb_bounds: within the modelled bounds
    reached = re.findall(r'Reached cover statement in step (\d+)', r.stdout)
    assert reached, r.stdout[-2000:]
    return len(reached), min(int(x) for x in reached)


if __name__ == '__main__':
    if not available():
        print('skipped: needs examples/fv_axi/axi_uvm_tb_constructs.json, QS, yosys + yosys-slang, boolector'); sys.exit(0)
    work, model, nt, ng = test_models_and_groups()
    print(f'P1: {nt} tests in {ng} models; the run_rand tests share {model}; RTL state machines, no delta rounds')
    n, first = test_cover_top_bounded(work, model)
    print(f'P2: coverage top compiles; {n} covers reached within 12 cycles (first at edge {first}); no model bound hit')
