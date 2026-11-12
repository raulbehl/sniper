#!/usr/bin/env python3
"""Do the formal cover traces of the testbench model happen in simulation?

For every cover trace (axi_cov_top + fv_agent_<test>, yosys-smtbmc -c): run the real UVM test in Verilator
with fv_replay.sv's overrides and +FV_REPLAY=<trace>.replay (the values the trace's free choices took), record
both buses (axi_trace_rec.sv, mem_trace_rec.sv), and compare them with the formal trace at every clock edge
up to the edge where the cover was reached. AXI read data is not compared: it comes from the testbench's
memory model, which is outside the formal model (free there). The axi_coverage bins of the requests the
simulation accepted must include the ones the formal trace reached.

    replay_check.py <sim binary> <replay dir> <cover dir> [--test axi_rw_rand_test] [-j N]
      replay dir: fv_replay.args and <trace>.replay (fvreplay.py);  cover dir: tr/<trace>.vcd, cover.log,
      cover_names.json
"""
import argparse, json, os, re, subprocess, sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', '..', '..'))
import check_driver as C      # noqa: E402  (AXI trace fields)
import fvreplay as R          # noqa: E402  (VCD reader)

MEM_FIELDS = ['reset_n', 'cpu_mem_valid', 'cpu_mem_id', 'cpu_mem_addr', 'cpu_mem_byte_en', 'cpu_mem_wr',
              'cpu_mem_ready', 'mem_cpu_valid', 'mem_cpu_id', 'mem_cpu_ready']
AXI_FIELDS = [('reset_n' if f[0] == 'reset_n' else 'axi_' + f[0]) for f in C.FIELDS]
SKIP = {'axi_rdata'}            # free in the model (axi_mem_model::read_word)
# DUT outputs that only mean something while their valid is high (before reset the formal DUT's registers
# start anywhere; the simulator's at 0)
QUAL = {'axi_arid': 'axi_arvalid', 'axi_arlen': 'axi_arvalid', 'axi_awid': 'axi_awvalid',
        'axi_wlast': 'axi_wvalid', 'mem_cpu_id': 'mem_cpu_valid'}


def bins(addr, be, wr, mid):
    """axi_coverage::write() for one accepted request"""
    lanes = [l for l in range(4) if (be >> (4 * l)) & 0xf]
    if not lanes: return set()
    fb = next(i for i in range(16) if (be >> i) & 1); nb = bin(be).count('1')
    k = 'WR' if wr else 'RD'
    return {f'cross {k}_lanes{len(lanes)}_at{lanes[0]}', f'shape {k}_head{int(fb % 4 != 0)}_tail{int((fb + nb) % 4 != 0)}',
            f'axi_id {(addr >> 4) & 7}', f'mem_id {mid}'}


def rows(path, names):
    out = []
    for line in open(path):
        v = line.split()
        if len(v) == len(names): out.append({n: int(x, 16) for n, x in zip(names, v)})
    return out


def cover_steps(cover_dir):
    cl = os.path.join(cover_dir, 'closure.json')          # cover_closure.py's: trace -> (edge, covers)
    if os.path.exists(cl):
        return {t: (x['edge'], x['covers']) for t, x in json.load(open(cl))['traces'].items()}
    names = json.load(open(os.path.join(cover_dir, 'cover_names.json')))
    per, cur, step = {}, [], 0
    for line in open(os.path.join(cover_dir, 'cover.log')):
        m = re.search(r'Reached cover statement in step (\d+) at \S+ (\$\d+)', line)
        if m: cur.append(names.get(m.group(2), m.group(2))); step = int(m.group(1))
        m = re.search(r'Writing trace to VCD file: (\S+)', line)
        if m:
            per[os.path.splitext(os.path.basename(m.group(1)))[0]] = (step, cur); cur = []
    return per


def check(sim, rdir, cdir, name, step, want, test, args):
    work = os.path.join(rdir, 'run', name)
    os.makedirs(work, exist_ok=True)
    cmd = [sim, f'+UVM_TESTNAME={test}', '+UVM_NO_RELNOTES', '+MAX_ERRORS=1', '+verilator+seed+1',
           f'+FV_REPLAY={os.path.join(rdir, name + ".replay")}', f'+MEM_TRACE={work}/mem.trc',
           f'+AXI_TRACE={work}/axi.trc', *args]
    r = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=600)
    open(os.path.join(work, 'sim.log'), 'w').write(r.stdout + r.stderr)
    sm, sa = rows(f'{work}/mem.trc', MEM_FIELDS), rows(f'{work}/axi.trc', AXI_FIELDS)
    sig, n = R.read_vcd(os.path.join(cdir, 'tr', name + '.vcd'), sorted(set(MEM_FIELDS + AXI_FIELDS)))
    diffs = []
    for k in range(min(step + 1, n, len(sm), len(sa))):
        simv = dict(sm[k]); simv.update(sa[k])
        for f in sorted(set(MEM_FIELDS + AXI_FIELDS) - SKIP):
            if f not in sig: continue
            fv = R.to_int(sig[f][k])
            if f in QUAL and not simv[QUAL[f]]: continue
            if simv[f] != fv: diffs.append(f'edge {k}: {f} sim={simv[f]:x} formal={fv:x}')
    hit = set()
    for k in range(min(step + 1, len(sm))):
        g = sm[k]
        if g['reset_n'] and g['cpu_mem_valid'] and g['cpu_mem_ready']:
            hit |= bins(g['cpu_mem_addr'], g['cpu_mem_byte_en'], g['cpu_mem_wr'], g['cpu_mem_id'])
    missing = sorted(w for w in set(want) - hit if not w.startswith('env '))   # (env covers: not bins)
    passed = 'TEST PASSED' in r.stdout
    return name, step, want, diffs, missing, passed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('sim'); ap.add_argument('replay_dir'); ap.add_argument('cover_dir')
    ap.add_argument('--test', default='axi_rw_rand_test')
    ap.add_argument('-j', type=int, default=4)
    a = ap.parse_args()
    args = open(os.path.join(a.replay_dir, 'fv_replay.args')).read().split()
    per = cover_steps(a.cover_dir)
    with ThreadPoolExecutor(a.j) as ex:
        res = list(ex.map(lambda kv: check(a.sim, a.replay_dir, a.cover_dir, kv[0], kv[1][0], kv[1][1], a.test, args),
                          sorted(per.items(), key=lambda kv: int(re.sub(r'\D', '', kv[0]) or 0))))
    ok = 0
    covered = set()
    for name, step, want, diffs, missing, passed in res:
        good = not diffs and not missing
        ok += good
        if good: covered |= set(want)
        print(f'{name:8s} edge {step:2d} {", ".join(want)}: '
              + ('replayed' if good else f'DIFFERS ({len(diffs)} values; first: {diffs[:3]}; bins not hit: {missing})')
              + ('' if passed else '  [test did not pass]'))
    nb = len([c for c in covered if not c.startswith('env ')])
    print(f'{ok}/{len(res)} traces replayed cycle-exactly in simulation; {nb} axi_coverage bins hit by them')
    sys.exit(0 if ok == len(res) else 1)


if __name__ == '__main__':
    main()
