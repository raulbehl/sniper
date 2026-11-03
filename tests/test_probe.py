#!/usr/bin/env python3
"""Regression test: run sniper on tests/probe_tb.sv and check what it extracts.

    python tests/test_probe.py      (or pytest tests/)
"""
import json, os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(HERE)


def run_probe(src='probe_tb.sv', top='probe_top'):
    out = os.path.join(tempfile.mkdtemp(), 'probe.json')
    subprocess.run([sys.executable, os.path.join(ROOT, 'sniper.py'), '--own', HERE, '-o', out, '--',
                    '--top', top, os.path.join(HERE, src)], check=True, capture_output=True)
    with open(out) as f:
        return json.load(f)


def test_probe():
    d = run_probe()
    m = d['meta']
    assert m['errors'] == 0 and m['own_diagnostics'] == []

    # types: package params, typedefs and enum values
    assert {e['name']: [v['name'] for v in e['values']] for e in d['types']['enums']} == {'op_e': ['RD', 'WR', 'ATOMIC']}
    assert {'op_e', 'addr_t'} <= {t['name'] for t in d['types']['typedefs']}

    # classes: rand members with widths, constraint item kinds, soft, user methods only
    cls = {c['name']: c for c in d['classes']}
    assert set(cls) == {'base_item', 'knobs', 'driver', 'test'}
    bi = cls['base_item']
    rand = {p['name']: p['width'] for p in bi['properties'] if p['rand'] == 'Rand'}
    assert rand == {'addr': 8, 'op': 2, 'len': 32, 'be': None}
    kinds = {c['name']: c['item_kinds'] for c in bi['constraints']}
    assert kinds == {'c_len': ['Expression', 'Expression'], 'c_dist': ['Expression'],
                     'c_impl': ['Implication', 'Conditional'], 'c_each': ['Foreach']}
    assert [i.get('isSoft') for i in bi['constraints'][0]['items']] == [False, True]
    assert bi['constraints'][1]['items'][0]['expr']['k'] == 'Dist'
    assert bi['methods'] == ['new', 'post_randomize']
    assert cls['knobs']['specialization_of'] == 'knobs'

    # covergroup: coverpoints, bin kinds and ranges, transition bin, iff, cross with ignore_bins, options
    cg = d['coverage']['covergroups'][0]
    assert cg['name'] == 'cg_item' and cg['owner'] == 'base_item'
    assert cg['options'] == ['option.per_instance = 1;']
    cps = {c['name']: c for c in cg['coverpoints']}
    assert [(b['name'], b['kind']) for b in cps['cp_op']['bins']] == [('no_atomic', 'IgnoreBins')]
    assert [(b['name'], b['kind'], b['array'], b.get('ranges')) for b in cps['cp_len']['bins']] == [
        ('sml', 'Bins', False, [["32'd1", "32'd4"]]), ('big', 'Bins', True, [["32'd5", "32'd16"]]),
        ('zero', 'IllegalBins', False, ['0'])]
    assert cps['cp_addr']['iff']['src'] == 'op != ATOMIC'
    assert [b['name'] for b in cps['cp_addr']['bins'] if b.get('transition')] == ['t']
    x = cg['crosses'][0]
    assert x['targets'] == ['cp_op', 'cp_len'] and [(b['name'], b['kind']) for b in x['bins']] == [('i', 'IgnoreBins')]

    # assertions
    assert [(a['assertion_kind'], a['line']) for a in d['coverage']['assertions']] == [('CoverProperty', 83), ('Assume', 84)]

    # randomization: class, syscall in post_randomize, obj.randomize() with, randcase, std::randomize
    r = [(x['kind'], x.get('target'), x.get('line')) for x in d['randomization']]
    assert r == [('class', None, None), ('syscall', None, 41), ('randomize', 'it', 56), ('randcase', None, 57),
                 ('std::randomize', None, 88)]
    inline = d['randomization'][2]['ast']['subroutine']['extraInfo']['inlineConstraints']
    assert [i['src'] for i in inline['list']] == ['len <= k.n;', 'op != ATOMIC;']

    # driver facts: vif writes (non-blocking), reads, timing, loops
    run = next(s for s in d['subroutines'] if s['name'] == 'run')
    f = run['facts']
    assert [(w['signal'], w['line'], w['nonblocking']) for w in f['vif_writes']] == [
        ('valid', 61, True), ('addr', 62, True), ('op', 63, True), ('valid', 65, True)]
    assert sorted({(r['signal'], r['line']) for r in f['vif_reads']}) == [('clk', 58), ('clk', 64), ('ready', 64)]
    assert [(t['timing'], t['line']) for t in f['timing']] == [('SignalEvent', 58), ('SignalEvent', 64)]
    assert [l['loop'] for l in f['loops']] == ['RepeatLoop', 'RepeatLoop', 'DoWhileLoop']

    # stimulus map and config override through a handle chain
    assert sorted(d['stimulus']['driver']['drives']) == ['probe_if.addr', 'probe_if.op', 'probe_if.valid']
    assert [(o['path'], o['value']) for o in d['config']['overrides']] == [('d.k.pct', "32'd80")]

    # hierarchy: interface signals/modport, DUT ports and connections
    top = d['hierarchy'][0]
    kids = {c['name']: c for c in top['children']}
    assert kids['pif']['kind'] == 'interface' and kids['pif']['modports'][0]['name'] == 'drv'
    assert {c['port']: c['expr'] for c in kids['dut']['connections']}['valid'] == 'pif.valid'


def test_corpus_regressions():
    """Constructs from real testbenches: see tests/probe_corpus.sv."""
    d = run_probe('probe_corpus.sv', 'corpus_top')
    assert d['meta']['errors'] == 0 and d['meta']['counts']['invalid_code'] == 0

    # dist weights keep their value and := / :/ kind (slang does not fold weight expressions)
    it = next(c for c in d['classes'] if c['name'] == 'item')
    w = [(i['weight']['expr']['const'], i['weight']['kind']) for i in it['constraints'][0]['items'][0]['expr']['items']]
    assert w == [('1', 'PerRange'), ('4', 'PerRange'), ('2', 'PerValue')]
    # an unbounded '$' bin bound is kept
    assert d['coverage']['covergroups'][0]['coverpoints'][0]['bins'][0]['ranges'] == [["8'd128", '$']]

    subs = {s['name']: s for s in d['subroutines']}
    run = subs['run']['facts']
    # a leading comment is not part of a node's source text
    assert [(r['target'], r['src']) for r in run['rand_sites']] == [('this', 'this.randomize()')]
    # a parameterized interface is named without its parameter values
    assert [(w['iface'], w['iface_params'], w['signal']) for w in run['vif_writes']] == [('pif_p', '(W=16)', 'data')]
    # struct members are not handle accesses: only it.p.b (it is a class handle) is a knob override
    assert [(w['path'], w['via_handle']) for w in subs['set_pair']['facts']['field_writes']] == [('p.a', False)]
    assert subs['fill']['facts']['field_writes'] == []
    assert [o['path'] for o in d['config']['overrides']] == ['it.p.b']
    # code in a generate branch that is not elaborated is marked, not reported as invalid
    assert [(p['start'], p.get('uninstantiated')) for p in d['procedures']] == [(40, True)]


if __name__ == '__main__':
    test_probe()
    test_corpus_regressions()
    print('probe tests passed')
