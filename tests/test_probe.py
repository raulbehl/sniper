#!/usr/bin/env python3
"""Regression test: run sniper on tests/probe_tb.sv and check what it extracts.

    python tests/test_probe.py      (or pytest tests/)
"""
import json, os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(HERE)


def run_probe(src='probe_tb.sv', top='probe_top', extra=()):
    out = os.path.join(tempfile.mkdtemp(), 'probe.json')
    subprocess.run([sys.executable, os.path.join(ROOT, 'sniper.py'), '--own', HERE, '-o', out, '--',
                    '--top', top, *extra, os.path.join(HERE, src)], check=True, capture_output=True)
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
    # extern constraints: prototype span plus the out-of-class body; static flag
    ext = next(c for c in d['classes'] if c['name'] == 'ext_c')
    assert [(b['name'], b['start'], b['body']['start'], b['body']['end'], b['static']) for b in ext['constraints']] == [
        ('e_c', 46, 49, 51, False), ('s_c', 47, 52, 52, True)]
    assert next(c for c in d['classes'] if c['name'] == 'item')['constraints'][0]['static'] is False
    # a parameterized class nothing specializes is reported through its default specialization
    g = next(c for c in d['classes'] if c['name'] == 'gen_c')
    assert g['default_specialization'] and [b['name'] for b in g['constraints']] == ['c_n']
    assert g['parameters'] == [{'name': 'N', 'value': '4', 'type': 'int'}]
    assert 'twice' in subs or any(s['name'] == 'twice' for s in d['subroutines'])
    assert not any(c['default_specialization'] for c in d['classes'] if c['name'] != 'gen_c')
    # a specialization reached only through a typedef'd member type is reported
    pc = next(c for c in d['classes'] if c['name'] == 'pred_c')
    assert pc['parameters'] == [{'name': 'CFG_T', 'type_param': 'item'}] and not pc['default_specialization']
    assert any(s['name'] == 'predict' for s in d['subroutines'])
    # code in a generate branch that is not elaborated is marked, not reported as invalid
    assert [(p['start'], p.get('uninstantiated')) for p in d['procedures']] == [(40, True)]



EXT_PKG = """package fv_ext_pkg;
  function automatic bit [7:0] ext_lim(int k);
    return 8'(k * 3);
  endfunction
endpackage
"""


def test_fv_extraction():
    """What fvgen reads: see tests/probe_fv.sv."""
    ext = os.path.join(tempfile.mkdtemp(), 'fv_ext_pkg.sv')      # outside --own
    with open(ext, 'w') as f: f.write(EXT_PKG)
    d = run_probe('probe_fv.sv', 'fv_top', [ext])
    assert d['meta']['errors'] == 0 and d['meta']['counts']['invalid_code'] == 0
    it = next(c for c in d['classes'] if c['name'] == 'item')
    ti = {p['name']: p['ti'] for p in it['properties']}
    items = {i['src']: i['expr'] for b in it['constraints'] for i in b['items']}

    # every expression node carries its SV width and signedness; implicit conversions are explicit
    e = items["a + sa > 8'd3;"]
    assert (e['w'], e['signed']) == (1, False)
    add = e['left']
    assert (add['w'], add['signed']) == (8, False)
    assert (add['right']['k'], add['right']['operand']['w'], add['right']['operand']['signed']) == ('Conversion', 8, True)
    # packed struct member: bit offset in the struct (ps_t = {hi[6:3], op[2:1], lo[0]})
    e = items['ps.op == WR;']
    assert (e['left']['k'], e['left']['offset'], e['left']['w']) == ('MemberAccess', 1, 2)
    # enum values and parameters carry their constant
    assert e['right']['const'] == "2'b1"
    assert items['n < LIM;']['right']['operand']['const'] == '12'

    # type_info: kind, width, sign, enum values, struct fields with offsets, element type and range
    assert (ti['sa']['w'], ti['sa']['signed']) == (8, True)
    assert [(x['name'], x['value']) for x in ti['op']['enum']] == [('RD', "2'b0"), ('WR', "2'b1"), ('AT', "2'b11")]
    assert [(f['name'], f['offset'], f['type']['w']) for f in ti['ps']['fields']] == [('hi', 3, 4), ('op', 1, 2), ('lo', 0, 1)]
    assert (ti['q']['kind'], ti['q']['elem']['w']) == ('QueueType', 4)
    assert (ti['fx']['range'], ti['fx']['size'], ti['fx']['elem']['w']) == ([0, 3], 4, 4)

    # functions called from constraints: own ones are reported anyway, others are added as external
    subs = {s['name']: s for s in d['subroutines']}
    assert 'twice' in subs and not subs['twice'].get('external')
    assert subs['ext_lim'].get('external') is True and subs['ext_lim']['body']

    # coverage: per-coverpoint option values; transitions parsed into steps (several sets in one bin,
    # value ranges, [*m:n] / [->n] / [=n] repeats); an unlabelled coverpoint is named without trivia
    cg = d['coverage']['covergroups'][0]
    cps = {cp['name']: cp for cp in cg['coverpoints']}
    assert set(cps) == {'cp_a', 'op'}
    assert [(o['name'], o['type_option'], o['value']) for o in cps['cp_a']['options']] == [('at_least', False, '2')]
    tr = {b['name']: b['trans'] for b in cps['cp_a']['bins']}
    assert tr['t2'] == [[{'values': ['0']}, {'values': ['1']}], [{'values': ['2']}, {'values': ['3']}]]
    assert tr['tr'] == [[{'values': ['1'], 'repeat': {'kind': '*', 'from': '2', 'to': '3'}},
                         {'values': [['4', '5']]},
                         {'values': ['6'], 'repeat': {'kind': '->', 'from': '1', 'to': '1'}},
                         {'values': ['7'], 'repeat': {'kind': '=', 'from': '2', 'to': '2'}}]]
    assert cps['op']['ti']['enum'][2]['name'] == 'AT'

    # covergroup with function sample(...): its arguments, with class types resolvable by path
    pg = next(g for g in d['coverage']['covergroups'] if g['name'] == 'pkg_cg')
    assert [(a['name'], a['ti'].get('class_path') or a['ti']['kind']) for a in pg['sample_args']] == \
        [('t', 'fv_pkg::item'), ('k', 'PredefinedIntegerType')]
    assert ti['q'].get('class_path') is None

    # constraint_mode / rand_mode setters: receiver, block or field (None: the whole object), value
    run = subs['run']['facts']['calls']
    modes = [(c['name'], c['receiver'], c['member'], c['value'], c['line']) for c in run
             if c['name'] in ('constraint_mode', 'rand_mode')]
    assert modes == [('constraint_mode', 'it', 'c_off', '0', 49), ('rand_mode', 'it', 'n', '0', 50),
                     ('constraint_mode', 'it', None, "1'b1", 51), ('rand_mode', 'it', None, "1'b0", 53)]


def test_rtl_mapping_extraction():
    """What fvgen --rtl reads: the register model's field positions and HDL paths (any file), and the
    testbench top's continuous assigns."""
    d = run_probe('rtl_tb.sv', 'rf_tb', [os.path.join(HERE, 'rtl_dut.sv')])
    assert d['meta']['errors'] == 0, d['meta']['error_list']
    ral = d['ral']
    assert ral['regs'] == {'rf_pkg::rf_reg_cfg': {'mode': {'lsb': 0, 'size': 2, 'access': 'RW'}}}
    blk = ral['blocks']['rf_pkg::rf_reg_block']
    assert blk['roots'] == [{'path': 'rf_tb.dut', 'kind': 'RTL'}]
    assert blk['regs'] == {'cfg': {'class': 'rf_pkg::rf_reg_cfg',
                                   'slices': [{'path': 'cfg_q', 'offset': 0, 'size': 2, 'kind': 'RTL'}]}}
    tb = next(h for h in d['hierarchy'] if h['name'] == 'rf_tb')
    assert [(a['lhs'], a['rhs']) for a in tb['assigns']] == [('rif.level', 'lvl'), ('rif.busy', 'dut.busy'),
                                                             ('rif.wr', 'wr')]


def test_invalid_code():
    """Code slang cannot bind is reported in facts.invalid and counted, not analysed."""
    d = run_probe('probe_invalid.sv', 'invalid_top')
    assert d['meta']['errors'] == 1 and d['meta']['counts']['invalid_code'] == 1
    assert [str(e.get('code', e)) if isinstance(e, dict) else e for e in d['meta']['error_list']] == \
        ['DiagCode(BadBinaryExpression)']
    p = d['procedures'][0]
    assert [i['line'] for i in p['facts']['invalid']] == [9]
    assert p['facts']['signal_writes'] == []


if __name__ == '__main__':
    test_probe()
    test_corpus_regressions()
    test_fv_extraction()
    test_rtl_mapping_extraction()
    test_invalid_code()
    print('probe tests passed')
