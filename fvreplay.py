#!/usr/bin/env python3
"""fvreplay: replay formal cover traces of an fvproc testbench model in simulation.

    fvreplay.py SNIPER_JSON MODEL_JSON --src DIR --trace NAME=TRACE.vcd [--trace ...] -o fv_replay.sv
                [--inst u_tb]

Every free choice of the model (fv_agent_<name>.json: free_choices) is a $urandom_range / $urandom call
or a rand field of a randomize() call in the testbench source. A cover trace gives the values each one
took, in order. fv_replay.sv holds, per class that makes such choices, `<class>__fv_replay extends
<class>`:
  - the methods with $urandom_range / $urandom calls (and the methods on the way to them from a virtual
    method: run_phase, body, post_randomize, ...) copied from the source, each call replaced by
    fv_urandom_range(<site>, ...): the trace's next value for that call, the original call once the
    trace's values are used up;
  - for rand fields: a constraint that pins each field to the trace's value of that randomize() call.
Each trace's values go to <NAME>.replay (one "site value" per line, in order). Run any test with the
overrides (fv_replay.args: +uvm_set_type_override=...) and +FV_REPLAY=<NAME>.replay; nothing else in the
testbench changes, and one simulator build replays every trace. Choices with no source to replay (data returned by objects
outside the model) are listed in fv_replay.json.
"""
import argparse, json, os, re, sys

ENTRY = {'run_phase', 'main_phase', 'reset_phase', 'configure_phase', 'shutdown_phase', 'body', 'pre_body',
         'post_body', 'pre_randomize', 'post_randomize', 'pre_start', 'post_start'}


def ident(s):
    return re.sub(r'[^A-Za-z0-9_]', '_', s)


# ------------------------------------------------------------------ VCD
def read_vcd(path, want):
    """values of the signals whose full name ends with one of `want` at each clock step:
    {suffix: [value str per step]} (steps: the posedges of the top's clk)"""
    ids, scope, names = {}, [], {}
    for_suffix = {}
    vals, cur = {}, {}
    times = []
    clk_id = None
    with open(path) as f:
        it = iter(f)
        for line in it:
            tok = line.split()
            if not tok: continue
            if tok[0] == '$scope': scope.append(tok[2])
            elif tok[0] == '$upscope': scope.pop()
            elif tok[0] == '$var':
                code, nm = tok[3], tok[4]
                full = '.'.join(scope + [nm])
                if len(scope) == 1 and nm == 'clk': clk_id = code
                for w in want:
                    if full.endswith('.' + w) or full == w:
                        for_suffix.setdefault(w, code)
                names.setdefault(code, full)
            elif tok[0] == '$enddefinitions': break
        t, steps = 0, []
        prev_clk = '0'
        for line in it:
            line = line.strip()
            if not line: continue
            if line[0] == '#':
                t = int(line[1:]); continue
            if line[0] in 'bB':
                v, code = line[1:].split()
            elif line[0] in '01xzXZ':
                v, code = line[0], line[1:]
            else:
                continue
            cur[code] = v
            if code == clk_id:
                if v == '1' and prev_clk != '1': steps.append(t)
                prev_clk = v
            vals.setdefault(code, []).append((t, v))
    out = {}
    for w, code in for_suffix.items():
        seq, k, last = [], 0, '0'
        ch = vals.get(code, [])
        for st in steps:
            while k < len(ch) and ch[k][0] <= st:
                last = ch[k][1]; k += 1
            seq.append(last)
        out[w] = seq
    return out, len(steps)


def to_int(v):
    v = v.replace('x', '0').replace('z', '0').replace('X', '0').replace('Z', '0')
    return int(v, 2) if v else 0


def site_values(vcd, inst, sites):
    """per site id: the values the trace took, in order"""
    want = []
    for s in sites:
        want += [f'{inst}.fv_ch{s["id"]}_v', f'{inst}.fv_ch{s["id"]}_u']
    sig, nsteps = read_vcd(vcd, want)
    out = {}
    for s in sites:
        v, u = sig.get(f'{inst}.fv_ch{s["id"]}_v'), sig.get(f'{inst}.fv_ch{s["id"]}_u')
        if v is None or u is None: continue
        w, d = s['width'], s.get('per_edge', 1)
        seq = []
        for k in range(nsteps):
            n = to_int(u[k])
            x = to_int(v[k])
            if d == 1:
                if n: seq.append(x)
            else:
                for i in range(n): seq.append((x >> (i * w)) & ((1 << w) - 1))
        out[s['id']] = seq
    return out


# ------------------------------------------------------------------ source
class Src:
    def __init__(self, d, root):
        self.d, self.root = d, root
        self.subs = {s['path']: s for s in d['subroutines']}
        self.classes = {c['path']: c for c in d['classes']}
        self.files = {}

    def lines(self, f):
        if f not in self.files:
            p = f if os.path.isabs(f) else os.path.join(self.root, f)
            self.files[f] = open(p).read().split('\n')
        return self.files[f]

    def owner(self, sub_path):
        return sub_path.rsplit('::', 1)[0]


CALL_RE = re.compile(r'\$urandom_range\s*\(|\$urandom\s*\(|\$urandom\b')


def substitute(line, ids):
    """replace the $urandom_range / $urandom calls of a source line, left to right, with replay calls"""
    out, pos, k = '', 0, 0
    for m in CALL_RE.finditer(line):
        if k >= len(ids): break
        out += line[pos:m.start()]
        sid = ids[k]; k += 1
        txt = m.group(0)
        if txt.startswith('$urandom_range'):
            out += f'fv_urandom_range({sid}, '
        elif txt.endswith('('):
            out += f'fv_urandom({sid}, '
        else:
            out += f'fv_urandom({sid})'
        pos = m.end()
    return out + line[pos:], k


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('json'); ap.add_argument('model')
    ap.add_argument('--src', required=True, help='directory the sniper file names are relative to')
    ap.add_argument('--trace', action='append', default=[], help='NAME=cover trace (.vcd)')
    ap.add_argument('--inst', default='u_tb', help='instance of the model in the formal top')
    ap.add_argument('--import', dest='imports', action='append', default=[], help='package to import')
    ap.add_argument('--top', default='tb_top', help='testbench top module (the link module is bound into it)')
    ap.add_argument('-o', '--out', default='fv_replay.sv')
    a = ap.parse_args()
    d = json.load(open(a.json))
    rep = json.load(open(a.model))
    src = Src(d, a.src)
    sites = rep['free_choices']

    # source call sites: (file, line, k-th call on the line) <- model sites (one per inlined copy)
    by_line = {}
    for s in sites:
        if s.get('kind') in ('urandom_range', 'urandom', 'random'):
            by_line.setdefault((s['file'], s['line']), []).append(s)
    key_of, groups = {}, {}
    for (f, l), ss in by_line.items():
        ss = sorted(ss, key=lambda s: s['id'])
        ncalls = len(CALL_RE.findall(src.lines(f)[l - 1]))
        for i, s in enumerate(ss):
            k = (f, l, i % max(1, ncalls), s['sub'])
            key_of[s['id']] = k
            groups.setdefault(k, []).append(s['id'])
    rid = {k: i for i, k in enumerate(sorted(groups, key=lambda k: (k[0], k[1], k[2])))}
    fields = [s for s in sites if s.get('kind') == 'field' and s.get('rand')]
    for s in fields:
        k = ('field', s['class'], s['field'])
        groups.setdefault(k, []).append(s['id'])
    rid.update({k: len(rid) + i for i, k in enumerate(sorted(k for k in groups if k[0] == 'field'))})
    unreplayable = [s for s in sites if s.get('kind') not in ('urandom_range', 'urandom', 'random', 'field')
                    or (s.get('kind') == 'field' and not s.get('rand'))]

    # values per replay site per trace
    tables = []
    for t in a.trace:
        name, path = t.split('=', 1)
        vals = site_values(path, a.inst, sites)
        per = {}
        for k, ids in groups.items():
            seq = []
            for i in sorted(ids):
                seq += vals.get(i, [])
            if len(ids) > 1 and any(vals.get(i) for i in ids[1:]):
                print(f'warning: {k}: several model copies took values; replayed in id order', file=sys.stderr)
            if seq: per[rid[k]] = seq
        tables.append((name, per))

    # replay classes
    classes = {}
    for k in groups:
        if k[0] == 'field':
            classes.setdefault(k[1], {'subs': set(), 'fields': []})['fields'].append(k)
        else:
            classes.setdefault(src.owner(k[3]), {'subs': set(), 'fields': []})['subs'].add(k[3])
    body = ['// Generated by fvreplay.py -- do not edit. Replays formal cover traces of the testbench model:',
            '// run a test with the overrides in fv_replay.args and +FV_HIT=<trace name>.',
            '`ifndef FV_REPLAY_SV', '`define FV_REPLAY_SV', '`include "uvm_macros.svh"', 'package fv_replay_pkg;',
            '  import uvm_pkg::*;'] + [f'  import {p}::*;' for p in a.imports]
    body += ['', '  // the values of the trace given by +FV_REPLAY=<file>: per call site, in the order they are taken',
             '  longint unsigned fv_vals[int][$];', '  int fv_loaded = 0;', '',
             '  function automatic void fv_load_file();',
             '    string fn; int fd, site, n; longint unsigned v;',
             '    fv_loaded = 1;',
             '    if (!$value$plusargs("FV_REPLAY=%s", fn)) return;',
             '    fd = $fopen(fn, "r");',
             '    if (fd == 0) begin $display("fv_replay: cannot open %s", fn); return; end',
             '    while ($fscanf(fd, "%d %h", site, v) == 2) fv_vals[site].push_back(v);',
             '    $fclose(fd);',
             '    $display("fv_replay: %0d call sites from %s", fv_vals.num(), fn);',
             '  endfunction', '',
             '  function automatic bit fv_take(int site, output longint unsigned v);',
             '    v = 0;',
             '    if (!fv_loaded) fv_load_file();',
             '    if (!fv_vals.exists(site) || fv_vals[site].size() == 0) return 0;',
             '    v = fv_vals[site].pop_front();',
             '    return 1;',
             '  endfunction', '',
             '  // $urandom_range(maxval, minval) / $urandom() at a replayed call site',
             '  function automatic int unsigned fv_urandom_range(int site, int unsigned maxval, int unsigned minval = 0);',
             '    longint unsigned v;',
             '    if (fv_take(site, v)) return v[31:0];',
             '    return $urandom_range(maxval, minval);',
             '  endfunction',
             '  function automatic int unsigned fv_urandom(int site, int seed = 0);',
             '    longint unsigned v;',
             '    if (fv_take(site, v)) return v[31:0];',
             '    return $urandom();',
             '  endfunction', '']
    overrides, report = [], {'traces': [nm for nm, _ in tables], 'classes': {}, 'not_replayed': []}
    for cls, info in sorted(classes.items()):
        c = src.classes.get(cls)
        if c is None:
            print(f'warning: {cls}: not a class of the extraction', file=sys.stderr); continue
        cname = c['name']
        rname = f'{cname}__fv_replay'
        lineage = [str(x) for x in c.get('lineage', [])]
        # methods to copy: those with replayed calls, and their callers in the class up to a virtual method
        todo, copy = list(info['subs']), set()
        while todo:
            p = todo.pop()
            if p in copy: continue
            copy.add(p)
            s = src.subs[p]
            if s.get('virtual') or s['name'] in ENTRY: continue
            callers = [q for q, x in src.subs.items() if src.owner(q) == cls and x.get('body') is not None and
                       any(cl.get('target') == p for cl in (x.get('facts') or {}).get('calls', []))]
            if not callers:
                print(f'warning: {p}: no virtual method of {cname} reaches it -- not replayed', file=sys.stderr)
            todo += callers
        L = [f'  // {cname} ({c["file"]}:{c["start"]})', f'  class {rname} extends {cname};']
        if 'uvm_component' in lineage:
            L += [f'    `uvm_component_utils({rname})',
                  f'    function new(string name = "{rname}", uvm_component parent = null); super.new(name, parent); endfunction']
        else:
            L += [f'    `uvm_object_utils({rname})',
                  f'    function new(string name = "{rname}"); super.new(name); endfunction']
        # pinned rand fields
        fl = sorted(info['fields'], key=lambda k: k[2])
        if fl:
            for k in fl:
                w = next(s['width'] for s in fields if (s['class'], s['field']) == (k[1], k[2]))
                L.append(f'    bit fv_en_{k[2]}; logic [{w - 1}:0] fv_v_{k[2]};   // replay site {rid[k]}')
            L.append('    constraint fv_replay_c {')
            L += [f'      fv_en_{k[2]} -> {k[2]} == fv_v_{k[2]};' for k in fl]
            L.append('    }')
        copied_pre = False
        for p in sorted(copy, key=lambda p: src.subs[p]['start']):
            s = src.subs[p]
            lines = src.lines(s['file'])[s['start'] - 1:s['end']]
            text = []
            for i, ln in enumerate(lines):
                l = s['start'] + i
                ids = [rid[k] for k in sorted((k for k in groups if k[0] == s['file'] and k[1] == l and k[3] == p),
                                              key=lambda k: k[2])]
                if ids:
                    ln, n = substitute(ln, ids)
                    if n != len(ids): print(f'warning: {s["file"]}:{l}: {len(ids)} sites, {n} calls', file=sys.stderr)
                text.append(ln)
            text[0] = re.sub(r'\b' + re.escape(cname) + r'::', '', text[0])
            if s['name'] == 'pre_randomize' and fl:
                copied_pre = True
                # the copy also loads the pins (first thing in the body)
                text.insert(1, '      fv_load();')
            L.append(f'    // copied from {s["file"]}:{s["start"]}')
            L += ['  ' + x for x in text]
        if fl:
            L += ['    function void fv_load();', '      longint unsigned v;']
            for k in fl:
                L.append(f'      fv_en_{k[2]} = fv_take({rid[k]}, v); fv_v_{k[2]} = v;')
            L.append('    endfunction')
            if not copied_pre:
                L.append('    function void pre_randomize(); super.pre_randomize(); fv_load(); endfunction')
        L += ['  endclass', '']
        body += L
        overrides.append(f'+uvm_set_type_override={cname},{rname}')
        report['classes'][cname] = {'override': rname, 'copied': sorted(src.subs[p]['name'] for p in copy),
                                    'pinned_fields': [k[2] for k in fl]}
    # with +FV_REPLAY the package overrides the classes itself: a package variable's initializer runs before any
    # initial block (so before run_test builds anything) and needs no command-line processor (UVM_NO_DPI builds)
    body += ['  function automatic bit fv_set_overrides();', '    string fn;',
             '    if (!$value$plusargs("FV_REPLAY=%s", fn)) return 0;']
    body += [f'    {src.classes[k]["name"]}::type_id::set_type_override({src.classes[k]["name"]}__fv_replay::get_type());'
             for k in sorted(classes) if k in src.classes]
    body += ['    $display("fv_replay: replaying %s (overrides set)", fn);', '    return 1;', '  endfunction',
             '  bit fv_overrides_set = fv_set_overrides();', '']
    body += ['endpackage', '',
             '// references the replay classes so that every simulator elaborates (and factory-registers) them; bound',
             '// into the testbench top (fvreplay.py --top), like any checker module: the testbench is unchanged',
             'module fv_replay_link;',
             '  initial begin']
    body += [f'    void\'(fv_replay_pkg::{c}__fv_replay::get_type());' for c in
             sorted(src.classes[k]['name'] for k in classes if k in src.classes)]
    body += ['  end', 'endmodule', f'bind {a.top} fv_replay_link u_fv_replay_link ();', '`endif', '']
    for s in unreplayable:
        report['not_replayed'].append(s['what'])
    open(a.out, 'w').write('\n'.join(body))
    base = os.path.splitext(a.out)[0]
    open(base + '.args', 'w').write('\n'.join(overrides) + '\n')
    report['sites'] = {str(v): list(k) for k, v in rid.items()}
    report['values'] = {nm: {str(k): v for k, v in per.items()} for nm, per in tables}
    json.dump(report, open(base + '.json', 'w'), indent=1)
    for nm, per in tables:
        with open(os.path.join(os.path.dirname(os.path.abspath(a.out)), f'{nm}.replay'), 'w') as f:
            for sid, seq in sorted(per.items()):
                for x in seq: f.write(f'{sid} {x:x}\n')
    print(f'{a.out}: {len(classes)} replay classes, {len(rid)} call sites, {len(tables)} traces; '
          f'overrides in {base}.args')


if __name__ == '__main__':
    main()
