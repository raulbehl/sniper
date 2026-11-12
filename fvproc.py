#!/usr/bin/env python3
"""fvproc: a UVM agent's processes -- driver threads, monitor, sequence bodies, the TLM channels and
sequencer handshakes between them -- as one synthesizable formal module, from a sniper extraction.

    fvproc.py SNIPER_JSON --agent <agent class> -o OUT [--qdepth N] [--sync N] [--cnt-w N]

writes OUT/fv_agent_<agent>.sv (module fv_agent_<agent>) and OUT/fv_agent_<agent>.json (what each part
of the testbench became, every approximation, the per-test parameter overrides).

The agent can be a whole test (a uvm_test: its env, every agent, the test's own sequences). With fvgen
--agent-tests every test of the testbench is modelled once; the constants a test's own code supplies
(run_rand(60, 0), seq.addr = 'h10) and the cfg fields its configure() sets become parameters, so tests that
differ only in those share one model: OUT/fv_tests_g<k>.sv, and OUT/fv_tests.json gives each test's model and
parameter values. A regression of thousands of tests is a handful of models.

The model is RTL: every testbench process becomes a state machine on clk, with registers for what it keeps
across clock edges and combinational logic for what it does between them (the style of the qs-ques RTL
lessons: counters for delays, flops for state, the next state computed from the current one).

Translation rules (one per testbench construct)
  process (run_phase, fork branch, sequence body)
                        -> a state machine: <proc>__pc_q holds the wait it is in, one state per wait,
                           named after its source line (driver__r_loop__L54_UNTIL)
  @(posedge clk), @(cb), ##n
                        -> a state left at the next edge
  repeat (n) @(posedge clk)
                        -> a counter state: <proc>__cnt_q loaded with n (n == 0: no state at all),
                           counted down one per edge, left when it reaches 1 (CNT_W bits; an assertion
                           fails if n does not fit)
  do @(posedge clk) while (!c);   while (!c) @(posedge clk);   wait (c)
                        -> a state left at the first edge where c holds
  other loops with a wait inside
                        -> their variables are registers and the loop is the state machine's path back
                           to an earlier state; loops without a wait are SV for loops with a static bound
                           (an assertion fails if the bound is too small)
  code between two waits
                        -> combinational logic computing the registers' next values and the next state
  vif.sig <= v          -> a register: the DUT sees it from the next edge; reads of vif.sig are the
                           value at the edge (what a clocking block or an NBA-driven signal gives)
  class handle of a sequence item / created object
                        -> a packed struct, copied by value
  queue, dynamic array  -> QDEPTH entries and a count; associative array with a small key -> an array
                           plus valid bits
  uvm_tlm_(analysis_)fifo, mailbox
                        -> a queue with push (put / analysis write) and pop (get / try_get)
  seq_item_port <-> sequencer <-> sequence
                        -> the handshake as state: get_next_item raises a request, start_item takes
                           the grant, finish_item hands the item over, item_done releases the sequence
  sequencer with no sequence started in the agent
                        -> a free source: any item allowed by the item class's constraints, whenever
                           the driver asks or later
  $urandom_range, $urandom, randomize()
                        -> free values assumed to satisfy the range / constraints (one per edge, or one
                           per loop iteration inside a loop)
  cfg object fields     -> parameters (defaults: the field initializers; per-test overrides in the json)
  methods of objects outside the agent (memory models, ...)
                        -> free return values; their side effects are dropped
  processes at the same edge
                        -> run once each, in a fixed order: whoever puts into a channel / hands over an
                           item before whoever takes it (the monitor before the sequence it feeds). A
                           driver and its sequence, which hand work back and forth in one edge
                           (item_done -> next start_item -> get_next_item), run SYNC passes; an
                           assertion fails if one is still ready to run after them. A blocking call that
                           can return at once does so in the same pass
  try_get / try_peek of a channel another process puts into at the same edge
                        -> a free bit per channel and edge: the getter ran before the put (it sees only
                           the older entries) or after it (the LRM allows both)
  `uvm_error / `uvm_fatal / failed immediate assert
                        -> an assertion that the line is never reached
"""
import argparse, hashlib, json, os, re, sys

UVM_MSG = {'uvm_info', 'uvm_warning', 'uvm_error', 'uvm_fatal', 'uvm_info_context', 'uvm_warning_context',
           'uvm_error_context', 'uvm_fatal_context'}
CHANNEL_CLASSES = {'uvm_tlm_analysis_fifo', 'uvm_tlm_fifo', 'mailbox'}
PORT_RE = re.compile(r'^uvm_(analysis_port|analysis_export|analysis_imp|seq_item_pull_port|seq_item_pull_export|'
                     r'seq_item_pull_imp|blocking_\w+|nonblocking_\w+|\w+_port|\w+_export|\w+_imp)\b')
DROP_CALLS = {'$display', '$write', '$info', '$warning', '$error', '$fatal', '$sformatf', '$sformat', '$finish',
              '$stop', '$monitor', '$strobe', 'raise_objection', 'drop_objection', 'set_id_info',
              'set_report_verbosity_level', 'print', 'record', 'set_response_queue_error_report_enabled',
              'uvm_report_info', 'uvm_report_warning', 'set_drain_time', 'set_name', 'get_name', 'get_full_name',
              'convert2string', 'sprint', 'display'}


BUILD_TIME = {'new', 'build_phase', 'connect_phase', 'end_of_elaboration_phase', 'start_of_simulation_phase',
              'extract_phase', 'check_phase', 'report_phase', 'final_phase', 'configure'}


class Unsupported(Exception):
    pass


def ident(s):
    s = re.sub(r'[^A-Za-z0-9_]', '_', s)
    s = re.sub(r'_{3,}', '__', s)
    if s and s[0].isdigit(): s = '_' + s
    return s or 'x'


def cw(n):
    """bits to hold 0..n"""
    return max(1, int(n).bit_length())


def iw(n):
    """bits to index 0..n-1"""
    return max(1, (int(n) - 1).bit_length())


def lhs_name(n):
    """seq.addr -> seq_addr (a readable name for an assignment target)"""
    n = strip_conv(n) or {}
    if n.get('k') == 'NamedValue': return n['symbol']['sym']
    if n.get('k') == 'MemberAccess': return f"{lhs_name(n['value'])}_{n['member']['sym']}"
    if n.get('k') in ('ElementSelect', 'RangeSelect'): return lhs_name(n.get('value'))
    return 'x'


def strip_conv(n):
    while n is not None and n.get('k') == 'Conversion' and n.get('isImplicit'):
        n = n['operand']
    return n


def sv_const(c, w=None, s=False):
    """slang's constant text as an SV literal (x/z bits read as 0: the model is two-valued)"""
    c = str(c).strip()
    if c.startswith('"'): raise Unsupported(f'string constant {c}')
    if re.search(r"'[sS]?[bBoOhHdD].*[xXzZ?]", c):
        c = re.sub(r'[xXzZ?]', '0', c)
    if re.fullmatch(r'-?\d+', c) and w is not None:
        v = int(c)
        if v < 0: return f"-{w}'sd{-v}" if s else f"{w}'({v})"
        return f"{w}'{'s' if s else ''}d{v}"
    return c


def const_int(c):
    c = str(c).strip().replace('_', '')
    m = re.fullmatch(r"(-)?(?:(\d+)?'[sS]?([bBoOhHdD]))?([0-9a-fA-F]+)", c)
    if not m: return None
    base = {'b': 2, 'o': 8, 'h': 16, 'd': 10, None: 10}[m.group(3).lower() if m.group(3) else None]
    v = int(m.group(4), base)
    return -v if m.group(1) else v


# ======================================================================== types
class Ty:
    """bits(w, s) | struct(cls) | array(elem, n, akind: fixed|queue|assoc, keyw) | obj (a static
    object: component, channel, port, sequencer, cfg, vif) | opaque (strings, reals, ...: dropped)"""
    __slots__ = ('kind', 'w', 's', 'cls', 'elem', 'n', 'akind', 'keyw', 'lo')

    def __init__(self, kind, w=0, s=False, cls=None, elem=None, n=0, akind=None, keyw=0, lo=0):
        self.kind, self.w, self.s, self.cls, self.elem, self.n, self.akind, self.keyw, self.lo = \
            kind, w, s, cls, elem, n, akind, keyw, lo

    def __repr__(self):
        if self.kind == 'bits': return f'bits{self.w}{"s" if self.s else ""}'
        if self.kind == 'array': return f'{self.akind}[{self.n}]{self.elem!r}'
        return f'{self.kind}:{self.cls}'


def BITS(w, s=False):
    return Ty('bits', max(1, int(w)), bool(s))


OPAQUE = Ty('opaque')


# ======================================================================== the design (sniper json)
class Design:
    def __init__(self, d):
        self.d = d
        self.classes = {c['path']: c for c in d['classes']}
        self.by_name = {}
        for c in d['classes']:
            self.by_name.setdefault(c['name'], c['path'])
        self.subs = {s['path']: s for s in d['subroutines']}
        self.config = d.get('config', {})

    def cpath(self, ref):
        """class path from a name / path / specialization text, or None (not an own class)"""
        if ref is None: return None
        ref = str(ref)
        if ref in self.classes: return ref
        base = re.sub(r'#\(.*$', '', ref)
        if base in self.classes: return base
        nm = base.split('::')[-1]
        return self.by_name.get(nm)

    def cls(self, ref):
        p = self.cpath(ref)
        return self.classes.get(p) if p else None

    def bases(self, path):
        """path, then own base classes"""
        out, p = [], path
        while p and p not in out:
            out.append(p)
            c = self.classes.get(p)
            p = self.cpath(c.get('base')) if c else None
        return out

    def lineage(self, path):
        c = self.classes.get(path)
        if c is None: return []
        return [b.split('::')[-1] for b in self.bases(path)] + list(c.get('lineage') or [])

    def is_a(self, path, name):
        return name in self.lineage(path)

    def props(self, path):
        seen, out = set(), []
        for p in self.bases(path):
            for pr in self.classes[p].get('properties') or []:
                if pr['name'] in seen or pr.get('static'): continue
                seen.add(pr['name'])
                out.append(pr)
        return out

    def prop(self, path, name):
        for pr in self.props(path):
            if pr['name'] == name: return pr
        return None

    def method(self, path, name):
        for p in self.bases(path):
            s = self.subs.get(f'{p}::{name}')
            if s is not None and s.get('body') is not None: return s
        return None

    def type_params(self, path):
        """the type arguments of the closest UVM base specialization: uvm_driver#(REQ,RSP) -> [REQ, RSP]"""
        for p in self.bases(path):
            bt = self.classes[p].get('base_type') or ''
            m = re.search(r'#\((.*)\)$', bt)
            if m and re.match(r'uvm_', bt):
                return [a.strip() for a in split_args(m.group(1))]
        return []


def split_args(s):
    out, depth, cur = [], 0, ''
    for ch in s:
        if ch in '([{': depth += 1
        if ch in ')]}': depth -= 1
        if ch == ',' and depth == 0:
            out.append(cur); cur = ''
        else:
            cur += ch
    if cur.strip(): out.append(cur)
    return out


def generic_of(ti_or_ty):
    """'uvm_pkg::uvm_tlm_analysis_fifo#(axi_rd_req)' -> ('uvm_tlm_analysis_fifo', ['axi_rd_req'])"""
    s = ti_or_ty if isinstance(ti_or_ty, str) else (ti_or_ty.get('class_path') or ti_or_ty.get('class') or '')
    m = re.match(r'^(?:\w+::)*(\w+)(?:#\((.*)\))?$', s.strip())
    if not m: return s, []
    return m.group(1), [a.strip() for a in split_args(m.group(2))] if m.group(2) else []


# ======================================================================== static objects
class Inst:
    """a static object of the agent: component, sequencer, sequence (process instance), channel,
    port, configuration / external object, virtual interface"""

    def __init__(self, kind, name, cls=None):
        self.kind, self.name, self.cls = kind, name, cls
        self.members = {}       # name -> Inst | V
        self.targets = []       # analysis port: connected channels / ports; pull port: [sequencer]
        self.elem = None        # channel / sequencer: element Ty
        self.bound = 0          # channel: 0 = unbounded (QDEPTH modelled)
        self.sqr = None         # sequence: its sequencer
        self.iface = None       # vif: interface name
        self.proto = None       # sequencer: handshake variable names
        self.src = None         # sequencer: free source process (no sequence started)

    def __repr__(self):
        return f'<{self.kind} {self.name}>'


class V:
    """a translated expression / location: SV text and type; `inst` for a static object"""
    __slots__ = ('t', 'ty', 'inst', 'lv', 'key')

    def __init__(self, t, ty, inst=None, lv=False, key=None):
        self.t, self.ty, self.inst, self.lv, self.key = t, ty, inst, lv, key

    def __repr__(self):
        return f'V({self.t!r},{self.ty!r})'


# ======================================================================== control flow graph
class Node:
    """code (lines, nxt) | br (cond, t, f) | fork (children, nxt) | end | wait: a state of the process
    wk: clk    @(posedge clk)                      -> resumes at the next edge
        rep    repeat (cnt) @(posedge clk)         -> a counter state: loaded with cnt (0: no wait at all)
        until  do @(posedge clk) while (!cond)     -> resumes at the first edge where cond holds
        chan / gni / start_item / finish_item / join
               blocking channel and handshake calls -> resume when `ready` holds (in the same edge);
                                                      imm: checked when reached, not only at the next pass"""
    __slots__ = ('id', 'kind', 'lines', 'cond', 't', 'f', 'nxt', 'ready', 'enter', 'resume', 'wk', 'children',
                 'join', 'state', 'desc', 'line', 'cnt', 'imm', 'consume', 'sname', 'g_end')

    def __init__(self, nid, kind, **kw):
        self.id, self.kind = nid, kind
        self.lines, self.cond, self.t, self.f, self.nxt = [], None, None, None, None
        self.ready, self.enter, self.resume, self.wk = None, [], [], None
        self.children, self.join, self.state, self.desc = [], None, None, ''
        self.line, self.cnt, self.imm, self.consume, self.sname, self.g_end = None, None, False, [], None, None
        for k, v in kw.items(): setattr(self, k, v)


class Proc:
    def __init__(self, g, name, desc, parent=None):
        self.g, self.name, self.desc, self.parent = g, ident(name), desc, parent
        self.id = len(g.procs)
        self.nodes = []
        self.entry = None
        self.top = parent is None
        self.acc = set()            # ('w'|'r', kind, name): channels / handshakes it writes / waits on
        self.children = []
        g.procs.append(self)
        self.end = self.node('end', desc='end')
        g.procs_by_name[self.name] = self

    def node(self, kind, **kw):
        kw.setdefault('line', getattr(self.g, 'cur_line', None))
        n = Node(len(self.nodes), kind, **kw)
        self.nodes.append(n)
        return n

    @property
    def pc(self):
        return f'{self.name}__pc'


class Ctx:
    """translation context: the process being built, `this`, local symbols, loop targets"""

    def __init__(self, g, proc, this, prefix, mode='proc'):
        self.g, self.proc, self.this, self.prefix, self.mode = g, proc, this, prefix, mode
        self.env = {}           # symbol path -> V
        self.loops = []         # CFG loops: (break node, continue node)
        self.nloops = []        # native (emitted) loops: exit-flag name or None
        self.nmult = []         # their iteration bounds (how often a free choice inside can be taken per edge)
        self.ret = None         # CFG task inlining: (exit node, return var V or None)
        self.pre = []           # statements an expression needs before it (pop_front, randomize)
        self.fdecls = None      # function mode: local declarations
        self.fret = None        # function mode: return type
        self.cond_depth = 0     # inside a short-circuit / conditional operand (no pre statements)

    def sub(self, this=None, prefix=None):
        c = Ctx(self.g, self.proc, self.this if this is None else this, prefix or self.prefix, self.mode)
        c.fdecls = self.fdecls
        c.nmult = list(self.nmult)
        return c


# ======================================================================== the generator
class AgentGen:
    def __init__(self, design, agent, opts):
        self.D, self.o = design, opts
        self.agent_path = design.cpath(agent)
        if self.agent_path is None: raise SystemExit(f'--agent {agent}: no such class in the extraction')
        self.name = ident(self.agent_path.split('::')[-1])
        self.QD = opts.get('qdepth', 8)
        self.SYNC = opts.get('sync', 2)         # passes of processes that hand work to each other in one edge
        self.CNT_W = opts.get('cnt_w', 8)       # width of repeat (n) @(posedge clk) counters
        self.LOOP = opts.get('loop_bound', 16)
        self.CHOICES = opts.get('choices', 8)
        self.UNROLL = opts.get('unroll', 4)
        self.procs = []
        self.procs_by_name = {}
        self.vars = {}          # name -> (Ty, init text, kind: state|temp)
        self.var_order = []
        self.params = {}        # name -> (Ty, default text, source)
        self.ports = {}         # signal -> {'w', 'dir': in|out, 'init'}
        self.clock = None
        self.vifs = {}          # interface name -> Inst
        self.exts = {}          # class path -> Inst (configuration / external singletons)
        self.structs = {}       # class path -> [(field, Ty)]
        self.struct_order = []
        self.funcs = {}         # key -> name
        self.func_text = []
        self.sites = []         # free choice sites: (id, w, desc, per-edge depth)
        self.site_meta = {}     # id -> where it comes from (kind, file, line, sub, class, field)
        self.site_funcs = []
        self.errors = []        # (k, desc)
        self.notes = []         # approximations
        self.unsupported = []
        self.chans = []
        self.lates = []         # channels with a free 'the getter ran before this edge's puts' bit
        self.sqrs = []
        self.value_classes = set()
        self.tmp_n = 0
        self.vbound = {}
        self.rd = None          # reads of the statement being translated (None: they are roots)
        self.roots, self.deps = set(), []
        self.live = opts.get('live')    # second pass: the live variables / fields
        self.act_vars = []
        self.pruned = []
        self.acc = None         # the channel / handshake accesses of the code being translated
        self.lift_tests = design.is_a(self.agent_path, 'uvm_test')
        self.lift_count, self.test_args = {}, {}
        self.func_acc = {}      # function key -> its accesses (added to every caller)
        self.iface_inits = {}
        for top in design.d.get('hierarchy') or []:
            self._iface_inits(top)

    def _iface_inits(self, h):
        if h.get('kind') == 'interface':
            for s in h.get('signals') or []:
                self.iface_inits.setdefault(h['definition'], {})[s['name']] = s.get('init')
        for c in h.get('children') or []:
            self._iface_inits(c)

    # ------------------------------------------------------------ liveness (what reaches an output)
    def read(self, key, part='e'):
        if key is None: return
        k = key + (part,) if key[0] == 'f' else key
        (self.rd if self.rd is not None else self.roots).add(k)

    def is_pruned(self, cls, f):
        k = ('f', cls, f)
        return self.is_dead(k, 'e') and self.is_dead(k, 'n')

    def is_dead(self, key, part='e'):
        if self.live is None or key is None: return False
        k = key + (part,) if key[0] == 'f' else key
        return k not in self.live

    def record(self, lhs, reads, parts=('e',)):
        if lhs is None or lhs.key is None:
            self.roots |= reads; return
        for pt in parts:
            k = lhs.key + (pt,) if lhs.key[0] == 'f' else lhs.key
            self.deps.append((k, set(reads)))

    def note(self, msg):
        if msg not in self.notes: self.notes.append(msg)

    def touch(self, rw, *key):
        """the code being translated writes ('w') / waits on or reads ('r') a channel or handshake variable:
        orders the processes of one clock edge (writers first) and finds the ones that wake each other"""
        if self.acc is not None: self.acc.add((rw,) + key)

    # ------------------------------------------------------------ variables
    def var(self, name, ty, init=None, kind='state'):
        name = ident(name)
        base, k = name, 1

        def taken(nm):      # x, x_q (its register), x__n / x__e (queue size, valid bits)
            return any(c in self.vars for c in (nm, nm + '_q', nm + '__n', nm + '__e')) or \
                (nm.endswith('_q') and nm[:-2] in self.vars)
        while taken(name):
            name = f'{base}_{k}'; k += 1
        self.vars[name] = (ty, init, kind)
        self.var_order.append(name)
        if ty.kind == 'array' and ty.akind in ('queue',):
            self.vars[name + '__n'] = (BITS(cw(ty.n)), None, kind)
            self.var_order.append(name + '__n')
        if ty.kind == 'array' and ty.akind == 'assoc':
            self.vars[name + '__e'] = (BITS(ty.n), None, kind)
            self.var_order.append(name + '__e')
        return V(name, ty, lv=True, key=('v', name))

    def tmp(self, ctx, ty, hint='t'):
        self.tmp_n += 1
        nm = f'__{hint}{self.tmp_n}'
        if ctx.mode == 'func':
            ctx.fdecls.append((nm, ty))
            return V(nm, ty, lv=True, key=('v', ctx.prefix + '.' + nm))
        return self.var(nm, ty, kind='temp')

    def decl(self, ty, name):
        """an SV declaration of `name` with type ty (packed)"""
        if ty.kind == 'bits':
            # two-state (bit), as the testbench's own types: an out-of-range read is 0 in every tool, never an x a
            # formal tool could take as a free value. Only the free choices (`FV_ANYSEQ) are logic
            return f"bit {'signed ' if ty.s else ''}[{ty.w - 1}:0] {name}"
        if ty.kind == 'struct':
            return f'{self.struct(ty.cls)} {name}'
        if ty.kind == 'array':
            dims, e = [], ty
            while e.kind == 'array':
                dims.append(f'[{e.n - 1}:0]'); e = e.elem
            if e.kind == 'bits': return f"bit {''.join(dims)}[{e.w - 1}:0] {name}"
            if e.kind == 'struct': return f"{self.struct(e.cls)} {''.join(dims)} {name}"
        raise Unsupported(f'cannot declare {ty!r}')

    # ------------------------------------------------------------ types
    def ty_ti(self, ti, what=''):
        """type_info dict -> Ty"""
        if ti is None: return OPAQUE
        k = ti.get('kind')
        if k in ('QueueType', 'DynamicArrayType'):
            return Ty('array', elem=self.ty_ti(ti.get('elem'), what), n=self.QD, akind='queue')
        if k == 'FixedSizeUnpackedArrayType':
            r = ti.get('range') or [ti.get('size', 1) - 1, 0]
            return Ty('array', elem=self.ty_ti(ti.get('elem'), what), n=ti.get('size') or abs(r[0] - r[1]) + 1,
                      akind='fixed', lo=min(r))
        if k == 'AssociativeArrayType':
            idx = ti.get('index') or {}
            if 'w' not in idx or idx['w'] > 8:
                raise Unsupported(f'associative array {what}: key wider than 8 bits')
            return Ty('array', elem=self.ty_ti(ti.get('elem'), what), n=1 << idx['w'], akind='assoc', keyw=idx['w'])
        if 'w' in ti: return BITS(ti['w'], ti.get('signed'))
        if k == 'ClassType':
            return self.ty_class(ti.get('class_path') or ti.get('class'))
        if k == 'VirtualInterfaceType': return Ty('obj', cls='vif')
        return OPAQUE

    def ty_class(self, ref):
        g, args = generic_of(ref)
        if g in CHANNEL_CLASSES or PORT_RE.match(g) or g.startswith('uvm_'): return Ty('obj', cls=ref)
        p = self.D.cpath(ref)
        if p is None: return OPAQUE
        if self.is_value(p): return Ty('struct', cls=p)
        return Ty('obj', cls=p)

    def ty_str(self, s, w=None, signed=False):
        """a type from the AST's 'ty' text (when there is no type_info)"""
        if w is not None: return BITS(w, signed)
        if s is None: return OPAQUE
        s = s.strip()
        m = re.match(r'^(.*)\$\[\$\]$', s)
        if m: return Ty('array', elem=self.ty_str(m.group(1)), n=self.QD, akind='queue')
        if s in ('int', 'integer'): return BITS(32, True)
        if s in ('int unsigned',): return BITS(32)
        if s in ('bit', 'logic', 'reg'): return BITS(1)
        if s in ('byte',): return BITS(8, True)
        if s in ('shortint',): return BITS(16, True)
        if s in ('longint',): return BITS(64, True)
        if s == 'time': return BITS(64)
        m = re.match(r'^(?:bit|logic)(?:\[(\d+):(\d+)\])$', s)
        if m: return BITS(abs(int(m.group(1)) - int(m.group(2))) + 1)
        return self.ty_class(s)

    def is_value(self, path):
        if path in self.value_classes: return True
        lin = self.D.lineage(path)
        if 'uvm_component' in lin or 'uvm_sequence_base' in lin: return False
        return 'uvm_sequence_item' in lin

    def struct(self, cls):
        """the packed struct typedef name of value class cls (declared on first use)"""
        nm = ident(cls.split('::')[-1]) + '_t'
        if cls in self.structs: return nm
        self.structs[cls] = None
        fields = []
        for pr in self.D.props(cls):
            try:
                ty = self.ty_ti(pr.get('ti'), f'{cls}.{pr["name"]}')
            except Unsupported as e:
                self.note(f'{cls.split("::")[-1]}.{pr["name"]}: not modelled ({e})'); continue
            if ty.kind in ('opaque', 'obj'): continue
            if ty.kind == 'array' and ty.elem.kind == 'array':
                self.note(f'{cls.split("::")[-1]}.{pr["name"]}: nested array not modelled'); continue
            if self.live is not None:
                key = ('f', cls, pr['name'])
                if ty.kind == 'array':
                    de, dn = self.is_dead(key, 'e'), self.is_dead(key, 'n')
                    if de and dn: self.pruned.append(f'{cls.split("::")[-1]}.{pr["name"]}'); continue
                    if de:
                        ty = Ty('array', elem=Ty('none'), n=ty.n, akind=ty.akind, keyw=ty.keyw)
                        self.pruned.append(f'{cls.split("::")[-1]}.{pr["name"]} (contents; size kept)')
                elif self.is_dead(key, 'e'):
                    self.pruned.append(f'{cls.split("::")[-1]}.{pr["name"]}'); continue
            fields.append((pr['name'], ty, pr))
        self.structs[cls] = fields
        self.struct_order.append(cls)
        return nm

    def struct_fields(self, cls):
        self.struct(cls)
        return self.structs[cls]

    def field_ty(self, cls, name):
        for f, ty, _ in self.struct_fields(cls):
            if f == name: return ty
        return None

    def default_value(self, ty):
        return "'0"

    # ------------------------------------------------------------ static objects
    def vif(self, iface):
        if iface not in self.vifs:
            self.vifs[iface] = Inst('vif', iface)
            self.vifs[iface].iface = iface
        return self.vifs[iface]

    def ext(self, path):
        if path not in self.exts:
            self.exts[path] = Inst('ext', ident(path.split('::')[-1]), path)
        return self.exts[path]

    def build(self, cls, name, kind='comp'):
        inst = Inst(kind, name, cls)
        for pr in self.D.props(cls):
            nm, ti = pr['name'], pr.get('ti') or {}
            if ti.get('kind') == 'VirtualInterfaceType' or pr.get('vif'):
                inst.members[nm] = self.vif(re.sub(r'\..*$', '', pr['type']).split('::')[-1])
                continue
            if ti.get('kind') == 'ClassType':
                cp = ti.get('class_path') or ti.get('class')
                g, args = generic_of(cp)
                own = self.D.cpath(cp)
                if g in CHANNEL_CLASSES:
                    ch = Inst('chan', f'{name}__{nm}', cp)
                    ch.elem = self.elem_ty(args[0] if args else None, f'{name}.{nm}')
                    inst.members[nm] = ch
                    self.chans.append(ch)
                elif PORT_RE.match(g):
                    inst.members[nm] = Inst('port', f'{name}__{nm}', g)
                elif g in ('uvm_sequencer', 'uvm_sequencer_param_base') and not own:
                    # typedef uvm_sequencer #(REQ, RSP) my_sequencer
                    sq = Inst('sqr', f'{name}__{nm}' if name else nm, cp)
                    sq.elem = self.elem_ty(args[0] if args else None, nm)
                    sq.members['__rsp_ty'] = self.elem_ty(args[1] if len(args) > 1 else (args[0] if args else None), nm)
                    sq.members['seq_item_export'] = sq
                    self.sqrs.append(sq)
                    inst.members[nm] = sq
                elif own and self.D.is_a(own, 'uvm_sequencer_base'):
                    inst.members[nm] = self.build_sqr(own, f'{name}__{nm}' if name else nm)
                elif own and self.D.is_a(own, 'uvm_component'):
                    inst.members[nm] = self.build(own, f'{name}__{nm}' if name else nm)
                elif own and self.is_value(own):
                    inst.members[nm] = self.var(f'{name}__{nm}', Ty('struct', cls=own))
                elif own:
                    inst.members[nm] = self.ext(own)
                continue
            try:
                ty = self.ty_ti(ti, f'{cls}.{nm}')
            except Unsupported as e:
                self.unsupported.append(f'{cls.split("::")[-1]}.{nm}: {e}'); continue
            if ty.kind in ('opaque', 'obj'): continue
            init = self.init_of(pr) if ty.kind == 'bits' else None
            inst.members[nm] = self.var(f'{name}__{nm}', ty, sv_const(init, ty.w, ty.s) if init else None)
        lin = self.D.lineage(cls)
        if 'uvm_driver' in lin:
            tp = self.D.type_params(cls)
            req = self.D.cpath(tp[0]) if tp else None
            inst.members.setdefault('seq_item_port', Inst('port', f'{name}__seq_item_port', 'uvm_seq_item_pull_port'))
            if req:
                inst.members.setdefault('req', self.var(f'{name}__req', Ty('struct', cls=req)))
                rsp = self.D.cpath(tp[1]) if len(tp) > 1 else req
                if rsp: inst.members.setdefault('rsp', self.var(f'{name}__rsp', Ty('struct', cls=rsp)))
        return inst

    def build_sqr(self, cls, name):
        inst = self.build(cls, name, 'sqr')
        tp = self.D.type_params(cls)
        inst.elem = self.elem_ty(tp[0] if tp else None, name)
        rsp = tp[1] if len(tp) > 1 and self.D.cpath(tp[1]) else (tp[0] if tp else None)
        inst.members['__rsp_ty'] = self.elem_ty(rsp, name)
        inst.members['seq_item_export'] = inst
        self.sqrs.append(inst)
        return inst

    def elem_ty(self, ref, what):
        if ref is None: raise Unsupported(f'{what}: element type unknown')
        p = self.D.cpath(ref)
        if p:
            self.value_classes.add(p)
            return Ty('struct', cls=p)
        return self.ty_str(ref)

    def seq_inst(self, cls, name, sqr):
        inst = Inst('seq', name, cls)
        inst.sqr = sqr
        for pr in self.D.props(cls):
            nm, ti = pr['name'], pr.get('ti') or {}
            if nm in ('p_sequencer', 'm_sequencer'):
                inst.members[nm] = sqr; continue
            if ti.get('kind') == 'ClassType':
                own = self.D.cpath(ti.get('class_path') or ti.get('class'))
                if own and self.is_value(own): inst.members[nm] = self.var(f'{name}__{nm}', Ty('struct', cls=own))
                elif own: inst.members[nm] = self.ext(own)
                continue
            try:
                ty = self.ty_ti(ti, f'{cls}.{nm}')
            except Unsupported as e:
                self.unsupported.append(f'{cls.split("::")[-1]}.{nm}: {e}'); continue
            if ty.kind in ('opaque', 'obj'): continue
            init = self.init_of(pr) if ty.kind == 'bits' else None
            inst.members[nm] = self.var(f'{name}__{nm}', ty, sv_const(init, ty.w, ty.s) if init else None)
        inst.members.setdefault('m_sequencer', sqr)
        inst.members.setdefault('p_sequencer', sqr)
        tp = self.D.type_params(cls)
        req = self.D.cpath(tp[0]) if tp else None
        if req:
            inst.members.setdefault('req', self.var(f'{name}__req', Ty('struct', cls=req)))
            inst.members.setdefault('rsp', self.var(f'{name}__rsp', Ty('struct', cls=req)))
        return inst

    def ext_member(self, inst, name, n=None):
        """a field of a configuration / external object"""
        if name in inst.members: return inst.members[name]
        pr = self.D.prop(inst.cls, name)
        if pr is None: raise Unsupported(f'{inst.cls}.{name}: unknown member')
        ti = pr.get('ti') or {}
        if ti.get('kind') == 'VirtualInterfaceType' or pr.get('vif'):
            v = self.vif(re.sub(r'\..*$', '', pr['type']).split('::')[-1])
        elif ti.get('kind') == 'ClassType':
            own = self.D.cpath(ti.get('class_path') or ti.get('class'))
            v = self.ext(own) if own else Inst('ext', name, ti.get('class_path'))
        else:
            ty = self.ty_ti(ti, f'{inst.cls}.{name}')
            if ty.kind != 'bits': raise Unsupported(f'{inst.cls}.{name}: non-integral configuration field')
            pname = self.param_name(inst, name)
            init = self.init_of(pr)
            self.params[pname] = (ty, sv_const(init, ty.w, ty.s) if init else "'0", f'{inst.cls.split("::")[-1]}.{name}')
            v = V(pname, ty)
        inst.members[name] = v
        return v

    def init_of(self, pr):
        """a property's initial value: slang's constant, an enum name, or a fill literal ('0 / '1)"""
        v = pr.get('init_value') or self.enum_init(pr)
        if v is None and str(pr.get('init') or '').strip() in ("'0", "'1"): v = pr['init'].strip()
        return v

    def enum_init(self, pr):
        for e in (pr.get('ti') or {}).get('enum') or []:
            if e['name'] == pr.get('init'): return e['value']
        return None

    def param_name(self, inst, name):
        nm = name.upper()
        clash = [p for p, (_, _, src) in self.params.items() if p == nm and not src.endswith('.' + name)]
        return f'{inst.name.upper()}_{nm}' if clash else nm

    # ------------------------------------------------------------ discovery
    def comp_classes(self, path, out):
        if path in out: return out
        out.append(path)
        for pr in self.D.props(path):
            ti = pr.get('ti') or {}
            if ti.get('kind') == 'ClassType':
                own = self.D.cpath(ti.get('class_path') or ti.get('class'))
                if own and self.D.is_a(own, 'uvm_component'): self.comp_classes(own, out)
        return out

    def discover(self):
        self.prescan(self.comp_classes(self.agent_path, []))
        self.agent = self.build(self.agent_path, '')
        # a test as the root: its process is `test`, not the class name, so tests that differ only in their
        # constants give the same model text (fvproc.generate_tests groups them)
        self.agent.name = 'test' if self.lift_tests else self.name
        self.comps = []
        self._comps(self.agent)
        for comp in self.comps:
            cp = self.D.method(comp.cls, 'connect_phase')
            if cp is not None:
                self.connections(cp['body'], comp)

    def _comps(self, inst):
        if inst in self.comps: return
        self.comps.append(inst)
        for m in inst.members.values():
            if isinstance(m, Inst) and m.kind in ('comp', 'sqr'): self._comps(m)

    def prescan(self, classes):
        """classes created by the agent's code are value classes (struct copies)"""
        seen = set()

        def walk(n):
            if isinstance(n, dict):
                if n.get('k') == 'NewClass':
                    p = self.D.cpath(n.get('ty'))
                    if p and not self.D.is_a(p, 'uvm_component'): self.value_classes.add(p)
                if n.get('k') == 'Call' and n.get('subroutineName') == 'create' and n.get('cty'):
                    p = self.D.cpath(n['cty'])
                    if p and not self.D.is_a(p, 'uvm_component') and not self.D.is_a(p, 'uvm_sequence_base'):
                        self.value_classes.add(p)
                if n.get('k') == 'Call' and n.get('subroutine') and n['subroutine'].get('path'):
                    sp = n['subroutine']['path']
                    if sp not in seen and sp in self.D.subs:
                        seen.add(sp); walk(self.D.subs[sp].get('body'))
                if n.get('k') == 'Call' and n.get('subroutineName') == 'start' and n.get('thisClass'):
                    p = self.D.cpath(n['thisClass'].get('ty'))
                    if p:
                        b = self.D.method(p, 'body')
                        if b and b['path'] not in seen: seen.add(b['path']); walk(b['body'])
                for v in n.values(): walk(v)
            elif isinstance(n, list):
                for v in n: walk(v)
        for c in classes:
            for p in self.D.bases(c):           # every method: write() of a subscriber creates objects too
                for sub in self.D.subs.values():
                    if sub.get('body') is not None and sub['path'].rsplit('::', 1)[0] == p and sub['path'] not in seen \
                            and sub['name'] not in BUILD_TIME:      # (cfg objects are created at build time)
                        seen.add(sub['path']); walk(sub['body'])

    def connections(self, body, comp):
        def walk(n):
            if isinstance(n, dict):
                if n.get('k') == 'Call' and n.get('subroutineName') == 'connect' and n.get('thisClass') is not None:
                    try:
                        src = self.static_ref(n['thisClass'], comp)
                        dst = self.static_ref(strip_conv(n['arguments'][0]), comp)
                    except Unsupported as e:
                        self.note(f'connect() not resolved: {e}'); return
                    if src is not None and dst is not None: src.targets.append(dst)
                    return
                for v in n.values(): walk(v)
            elif isinstance(n, list):
                for v in n: walk(v)
        walk(body)

    def static_ref(self, n, this):
        """an expression naming a static object (in connect_phase / start): -> Inst"""
        n = strip_conv(n)
        k = n.get('k')
        if k == 'NamedValue':
            nm = n['symbol']['sym']
            m = this.members.get(nm)
            if isinstance(m, Inst): return m
            raise Unsupported(f'{nm} is not a static object of {this.name}')
        if k == 'MemberAccess':
            base = self.static_ref(n['value'], this)
            nm = n['member']['sym']
            if base.kind == 'chan' and nm in ('analysis_export', 'put_export', 'get_export', 'blocking_put_export',
                                              'blocking_get_export', 'get_peek_export', 'blocking_get_peek_export'):
                return base
            if base.kind == 'ext': return self.ext_member(base, nm)
            m = base.members.get(nm)
            if isinstance(m, Inst): return m
            raise Unsupported(f'{base.name}.{nm} is not a static object')
        raise Unsupported(f'static object expression {k}')

    # ------------------------------------------------------------ processes
    def processes(self):
        for comp in self.comps:
            for m in ('run_phase',):
                s = self.D.method(comp.cls, m)
                if s is None: continue
                p = Proc(self, comp.name, f'{comp.cls.split("::")[-1]}::{m}')
                ctx = Ctx(self, p, comp, p.name)
                self.cur_file = s.get('file'); self.cur_sub = s['path']
                self.acc = p.acc
                p.entry = self.lower(s['body'], p.end, ctx)
                self.acc = None
        for sqr in self.sqrs:
            if sqr.proto is not None and not sqr.proto.get('seqs'):
                self.free_source(sqr)

    # ------------------------------------------------------------ waits: what blocks
    def has_wait(self, n, depth=0):
        if n is None: return False
        if isinstance(n, list): return any(self.has_wait(x, depth) for x in n)
        if not isinstance(n, dict): return False
        k = n.get('k')
        if k in ('Timed', 'Wait', 'WaitFork', 'WaitOrder', 'EventTrigger'): return True
        if k == 'Block' and n.get('blockKind') in ('JoinAll', 'JoinAny', 'JoinNone'): return True
        if k == 'Call':
            nm = n.get('subroutineName')
            if nm in ('get', 'peek', 'put', 'get_next_item', 'start_item', 'finish_item', 'get_response', 'start',
                      'wait_for_grant', 'send_request', 'wait_for_item_done', 'get_and_drive'):
                if n.get('subroutineKind') == 'Task' or nm in ('start_item', 'finish_item'): return True
            sp = (n.get('subroutine') or {}).get('path')
            if n.get('subroutineKind') == 'Task' and sp and sp in self.D.subs and depth < 8:
                # the task called, or any override of it (virtual: the object's class decides)
                for s in [self.D.subs[sp]] + [x for x in self.D.subs.values()
                                              if x.get('name') == nm and x['path'] != sp and x.get('kind') == 'Task']:
                    if s.get('body') is not None and self.has_wait(s['body'], depth + 1): return True
        if k == 'Return' or k == 'Break' or k == 'Continue':
            return False
        return any(self.has_wait(v, depth) for kk, v in n.items() if isinstance(v, (dict, list)))

    def escapes(self, n, inloop=False):
        """break / continue / return that leaves the statement (needs the CFG)"""
        if n is None: return False
        if isinstance(n, list): return any(self.escapes(x, inloop) for x in n)
        if not isinstance(n, dict): return False
        k = n.get('k')
        if k == 'Return': return True
        if k in ('Break', 'Continue'): return not inloop
        if k in ('ForLoop', 'WhileLoop', 'DoWhileLoop', 'RepeatLoop', 'ForeverLoop', 'ForeachLoop'):
            return self.escapes(n.get('body'), True)
        return any(self.escapes(v, inloop) for kk, v in n.items() if isinstance(v, (dict, list)) and kk != 'expr')

    def needs_cfg(self, n, ctx):
        if self.has_wait(n): return True
        return ctx.mode == 'proc' and (ctx.ret is not None or ctx.loops) and self.escapes(n)

    # ------------------------------------------------------------ lowering to the CFG
    def code(self, ctx, lines, nxt, desc=''):
        if not lines: return nxt
        n = ctx.proc.node('code', lines=lines, nxt=nxt, desc=desc)
        return n

    def where(self):
        f, l = getattr(self, 'cur_file', None), getattr(self, 'cur_line', None)
        return f'{f}:{l}' if f else f'line {l}'

    def lower(self, n, k, ctx):
        """statement n, then node k -> the node that starts n"""
        if n is None: return k
        kind = n.get('k')
        if n.get('line'): self.cur_line = n['line']
        if kind == 'List':
            items = [x for x in (n.get('list') or []) if x is not None]
            if not items: return k
            # forward order (declarations bind names for the statements after them): placeholders
            conts = [ctx.proc.node('code', desc='') for _ in items[:-1]] + [k]
            entry, prev = None, None
            for i, x in enumerate(items):
                e = self.lower(x, conts[i], ctx)
                if prev is not None: prev.nxt = e
                else: entry = e
                prev = conts[i] if i < len(items) - 1 else None
            return entry
        if not self.needs_cfg(n, ctx):
            return self.code(ctx, self.stmt(n, ctx), k, n.get('src', '')[:60])
        if kind == 'Block':
            bk = n.get('blockKind')
            if bk in ('JoinAll', 'JoinAny', 'JoinNone'): return self.lower_fork(n, k, ctx)
            return self.lower(n.get('body'), k, ctx)
        if kind == 'Timed':
            w = self.timing(n['timing'], ctx)
            body = self.lower(n.get('stmt'), k, ctx)
            return self.wait_chain(w, body, ctx)
        if kind == 'Conditional':
            conds = n.get('conditions') or []
            if len(conds) != 1 or conds[0].get('pattern'): raise Unsupported('if with a pattern / &&&')
            c = self.cond(conds[0]['expr'], ctx)
            t = self.lower(n.get('ifTrue'), k, ctx)
            f = self.lower(n.get('ifFalse'), k, ctx)
            return self.with_pre(ctx, c, lambda cc: ctx.proc.node('br', cond=cc, t=t, f=f, desc=n.get('src', '')[:60]))
        if kind == 'Case':
            sel = self.ex(n['expr'], ctx)
            tv = self.g_tmpvar(ctx, sel.ty, 'cs')
            pre = ctx.pre + [f'{tv.t} = {sel.t};']; ctx.pre = []
            nxt = self.lower(n.get('defaultCase'), k, ctx)
            for it in reversed(n.get('items') or []):
                conds = []
                for e in it.get('expressions') or []:
                    conds.append(f'({tv.t} == {self.ex(e, ctx).t})')
                body = self.lower(it.get('stmt'), k, ctx)
                nxt = ctx.proc.node('br', cond=' || '.join(conds) or "1'b0", t=body, f=nxt)
            return self.code(ctx, pre, nxt)
        if kind in ('ForeverLoop', 'WhileLoop', 'DoWhileLoop', 'ForLoop', 'RepeatLoop', 'ForeachLoop'):
            return self.lower_loop(n, k, ctx)
        if kind == 'ExpressionStatement':
            return self.lower_call_stmt(n['expr'], k, ctx)
        if kind == 'Wait':
            # wait (c): sampled at clock edges
            self.note('wait(expr) is evaluated at clock edges')
            c = self.cond(n['cond'], ctx)
            if ctx.pre: raise Unsupported('wait() condition with side effects')
            w = self.clk_wait(ctx, desc=(n.get('src') or 'wait(...)').strip(), wk='until')
            w.cond, w.nxt = c, k
            return ctx.proc.node('br', cond=c, t=k, f=w, desc='wait')
        if kind == 'Return':
            if ctx.ret is None: raise Unsupported('return outside a task')
            exitn, rv = ctx.ret
            lines = []
            if n.get('expr') is not None and rv is not None:
                e = self.ex(n['expr'], ctx)
                lines = ctx.pre + [f'{rv.t} = {e.t};']; ctx.pre = []
            return self.code(ctx, lines, exitn) if lines else exitn
        if kind == 'Break':
            if not ctx.loops: raise Unsupported('break outside a loop')
            return ctx.loops[-1][0]
        if kind == 'Continue':
            if not ctx.loops: raise Unsupported('continue outside a loop')
            return ctx.loops[-1][1]
        if kind == 'VariableDeclaration':
            return self.code(ctx, self.stmt(n, ctx), k)
        raise Unsupported(f'statement {kind} with a wait inside')

    def with_pre(self, ctx, c, mk):
        pre = ctx.pre; ctx.pre = []
        node = mk(c)
        return self.code(ctx, pre, node) if pre else node

    def g_tmpvar(self, ctx, ty, hint):
        return self.tmp(ctx, ty, hint)

    def group_macros(self, items):
        return items

    def wait_chain(self, w, body, ctx):
        """w: a wait node or a list (##n: several), continue with body"""
        if isinstance(w, list):
            for i in range(len(w) - 1): w[i].nxt = w[i + 1]
            w[-1].nxt = body
            return w[0]
        w.nxt = body
        return w

    def clk_wait(self, ctx, desc='@(posedge clk)', wk='clk'):
        """fv_ck[p]: the edge of this evaluation has not been used by process p yet (a wait entered after the
        edge -- `consume` -- waits for the next one; one entered at time 0 takes this one)"""
        p = ctx.proc
        return p.node('wait', wk=wk, ready=f'fv_ck[{p.id}]', resume=[f'fv_ck[{p.id}] = 1\'b0;'],
                      consume=[f'fv_ck[{p.id}] = 1\'b0;'], desc=desc, line=self.cur_line)

    def test_code(self):
        """the code being translated is a method of a test class (configure / traffic / run_phase of a uvm_test)"""
        sp = getattr(self, 'cur_sub', None)
        if not self.lift_tests or not sp or '::' not in sp: return False
        own = self.D.cpath(sp.rsplit('::', 1)[0])
        return own is not None and self.D.is_a(own, 'uvm_test')

    def lift(self, hint, node, ty):
        """a constant the test's own code supplies (run_rand(60, 0), seq.addr = 'h10) -> a parameter: tests that
        differ only in such constants share one model, each with its parameter values"""
        n = strip_conv(node)
        if n is None or n.get('const') is None or n.get('k') == 'StringLiteral' or ty.kind != 'bits': return None
        try:
            val = sv_const(n['const'], ty.w, ty.s)
        except Unsupported:
            return None
        base = 'T_' + ident(hint).upper().strip('_')
        k = self.lift_count.get(base, 0) + 1
        self.lift_count[base] = k
        nm = base if k == 1 else f'{base}_{k}'
        self.params[nm] = (ty, val, f'test constant: {hint}')
        self.test_args[nm] = val
        return V(nm, ty)

    def counter(self, p, w=None):
        """the process's delay counter (one per process: it waits in one place at a time)"""
        w = w or self.CNT_W
        if getattr(p, 'cnt', None) is None:
            p.cnt = self.var(f'{p.name}__cnt', BITS(w)).t
        ty, init, kind = self.vars[p.cnt]
        if w > ty.w: self.vars[p.cnt] = (BITS(w), init, kind)
        return p.cnt

    def pure_clk(self, n):
        """n is exactly `@(posedge clk);` (possibly in a begin-end): its event, else None"""
        while n is not None and n.get('k') in ('Block', 'List'):
            if n.get('k') == 'Block':
                if n.get('blockKind') not in (None, 'Sequential'): return None
                n = n.get('body'); continue
            items = [x for x in n.get('list') or [] if x is not None and x.get('k') != 'Empty']
            if len(items) != 1: return None
            n = items[0]
        if n is None or n.get('k') != 'Timed': return None
        if (n.get('stmt') or {}).get('k') not in (None, 'Empty'): return None
        t = n['timing']
        if t.get('k') != 'SignalEvent' or t.get('edge') != 'PosEdge': return None
        return t

    def clk_wait_of(self, t, ctx, wk, desc):
        """a counter / condition wait on the clock event t (else None: an edge of another signal)"""
        w = self.timing(t, ctx)
        if isinstance(w, list) or w.ready != f'fv_ck[{ctx.proc.id}]': return None
        w.wk, w.desc = wk, desc
        return w

    def timing(self, t, ctx):
        k = t.get('k')
        if k == 'SignalEvent':
            e = strip_conv(t['expr'])
            sig = self.vif_signal(e, ctx)
            if sig is not None and t.get('edge') == 'PosEdge' and (self.clock in (None, sig)):
                self.clock = sig
                return self.clk_wait(ctx, f'@(posedge {sig})')
            if sig is not None and t.get('edge') in ('PosEdge', 'NegEdge') and self.clock and sig != self.clock:
                return self.edge_wait(sig, t['edge'], ctx)
            cb = self.clocking(e, ctx)
            if cb: return self.clk_wait(ctx, '@(clocking block)')
            raise Unsupported(f'event control {t.get("edge")} {e.get("k")}')
        if k == 'CycleDelay':
            c = strip_conv(t['expr'])
            n = const_int(c.get('const')) if c.get('const') is not None else None
            if n is None: raise Unsupported('##n with a non-constant n')
            return [self.clk_wait(ctx, f'##{n}') for _ in range(max(1, n))]
        if k == 'Delay':
            raise Unsupported('#delay (only clock-edge timing is modelled)')
        if k == 'EventList':
            raise Unsupported('event list @(a or b)')
        raise Unsupported(f'timing control {k}')

    def edge_wait(self, sig, edge, ctx):
        """@(posedge sig) for a signal other than the clock: sampled at the clock"""
        p = ctx.proc
        self.port(sig, 1, 'in')
        prev = f'fv_prev_{sig}'
        if prev not in self.vars: self.var(prev, BITS(1), kind='prev')
        cond = f'({sig} && !{prev})' if edge == 'PosEdge' else f'(!{sig} && {prev})'
        self.note(f'@({edge.lower()} {sig}) is detected at clock edges')
        return p.node('wait', wk='clk', ready=f'(fv_ck[{p.id}] && {cond})', resume=[f"fv_ck[{p.id}] = 1'b0;"],
                      consume=[f"fv_ck[{p.id}] = 1'b0;"], desc=f'@({edge} {sig})', line=self.cur_line)

    def clocking(self, e, ctx):
        if e.get('k') in ('NamedValue', 'MemberAccess') and 'ClockingBlock' in json.dumps(e.get('symbol', e.get('member', {}))):
            return True
        return False

    def lower_fork(self, n, k, ctx):
        bk = n['blockKind']
        if ctx.loops and bk != 'JoinAll':
            raise Unsupported('fork ... join_none/join_any inside a loop (a dynamic number of processes)')
        items = (n.get('body') or {}).get('list') if (n.get('body') or {}).get('k') == 'List' else [n.get('body')]
        pre, branches = [], []
        for it in items or []:
            if it is None: continue
            if it.get('k') == 'VariableDeclaration': pre.extend(self.stmt(it, ctx)); continue
            branches.append(it)
        kids = []
        for i, b in enumerate(branches):
            nm = self.branch_name(b) or f'fork{i}'
            child = Proc(self, f'{ctx.proc.name}__{nm}', f'{ctx.proc.desc} / fork branch {nm}', parent=ctx.proc)
            cctx = ctx.sub(prefix=child.name)
            cctx.proc = child
            cctx.loops, cctx.ret = [], None
            cctx.env = dict(ctx.env)
            saved, self.acc = self.acc, child.acc
            child.entry = self.lower(b, child.end, cctx)
            self.acc = saved
            kids.append(child)
            ctx.proc.children.append(child)
        p = ctx.proc
        fk = p.node('fork', children=kids, desc=f'fork ({bk})')
        if bk == 'JoinNone':
            fk.nxt = k
        else:
            done = [f'({c.pc} == {c.name}__END)' for c in kids]
            cond = (' && ' if bk == 'JoinAll' else ' || ').join(done) or "1'b1"
            for c in kids: c.acc.add(('w', 'end', c.name)); self.touch('r', 'end', c.name)
            j = p.node('wait', wk='join', ready=f'({cond})', desc=f'join ({bk})', line=self.cur_line)
            j.nxt = k
            fk.nxt = j
        return self.code(ctx, pre, fk)

    def branch_name(self, b):
        b = b or {}
        if b.get('k') == 'ExpressionStatement' and (b.get('expr') or {}).get('k') == 'Call':
            c = b['expr']
            nm = c.get('subroutineName')
            if nm == 'start' and c.get('thisClass') is not None:
                return ident((c['thisClass'].get('symbol') or {}).get('sym') or 'seq')
            return ident(nm or 'call')
        return None

    def lower_loop(self, n, k, ctx):
        kind = n['k']
        p = ctx.proc
        if kind == 'ForeverLoop':
            head = p.node('code', desc='forever')
            ctx.loops.append((k, head))
            head.nxt = self.lower(n.get('body'), head, ctx)
            ctx.loops.pop()
            return head
        t = self.pure_clk(n.get('body'))
        if t is not None and kind in ('RepeatLoop', 'DoWhileLoop', 'WhileLoop'):
            src = (n.get('src') or '').strip()
            if kind == 'RepeatLoop':
                cnt = self.ex(n['count'], ctx)
                pre = ctx.pre; ctx.pre = []
                w = self.clk_wait_of(t, ctx, 'rep', src)
                if w is not None:
                    w.cnt, w.nxt = cnt, k
                    # a count from a parameter / constant gets the bits it needs (a watchdog of 500000 cycles);
                    # others (an item's delay) CNT_W, with an assertion that they fit
                    kc = const_int(cnt.t.split("'")[-1].lstrip('sdh')) if re.fullmatch(r"\d*'s?d\d+|\d+", cnt.t) else None
                    if kc is not None: need = cw(kc)
                    elif re.sub(r"^\d+'\((\w+)\)$", r'\1', cnt.t) in self.params: need = 32
                    else: need = self.CNT_W
                    self.counter(p, need)
                    return self.code(ctx, pre, w)
                ctx.pre = pre
            else:
                c = self.cond(n['cond'], ctx)
                if not ctx.pre:
                    w = self.clk_wait_of(t, ctx, 'until', src)
                    if w is not None:
                        w.cond, w.nxt = f'!({c})', k
                        if kind == 'DoWhileLoop': return w
                        return p.node('br', cond=c, t=w, f=k, desc=src)
                ctx.pre = []
        if kind == 'WhileLoop':
            head = p.node('br', desc='while')
            ctx.loops.append((k, head))
            body = self.lower(n.get('body'), head, ctx)
            ctx.loops.pop()
            c = self.cond(n['cond'], ctx)
            pre = ctx.pre; ctx.pre = []
            head.cond, head.t, head.f = c, body, k
            if pre:
                pn = p.node('code', lines=pre, nxt=head)
                ctx.loops.append((k, pn))     # (continue re-evaluates the condition)
                ctx.loops.pop()
                return pn
            return head
        if kind == 'DoWhileLoop':
            test = p.node('br', desc='do-while')
            ctx.loops.append((k, test))
            body = self.lower(n.get('body'), test, ctx)
            ctx.loops.pop()
            c = self.cond(n['cond'], ctx)
            if ctx.pre: raise Unsupported('do-while condition with side effects')
            test.cond, test.t, test.f = c, body, k
            return body
        if kind == 'RepeatLoop':
            cnt = self.ex(n['count'], ctx)
            cv = self.var(f'{ctx.prefix}__rep{len(p.nodes)}', BITS(32))
            pre = ctx.pre + [f'{cv.t} = 32\'({cnt.t});']; ctx.pre = []
            head = p.node('br', cond=f'({cv.t} != 32\'d0)', desc='repeat')
            dec = p.node('code', lines=[f"{cv.t} = {cv.t} - 32'd1;"])
            ctx.loops.append((k, head))
            dec.nxt = self.lower(n.get('body'), head, ctx)
            ctx.loops.pop()
            head.t, head.f = dec, k
            return self.code(ctx, pre, head)
        if kind == 'ForLoop':
            init = []
            for lv in n.get('loopVars') or []:
                init += self.loop_decl(lv, ctx)
            for e in n.get('initializers') or []:
                init += self.expr_stmt(e, ctx)
            head = p.node('br', desc='for')
            step_lines = []
            for e in n.get('steps') or []:
                step_lines += self.expr_stmt(e, ctx)
            step = p.node('code', lines=step_lines, nxt=head)
            ctx.loops.append((k, step))
            body = self.lower(n.get('body'), step, ctx)
            ctx.loops.pop()
            if n.get('stopExpr') is not None:
                head.cond = self.cond(n['stopExpr'], ctx)
                if ctx.pre: raise Unsupported('for condition with side effects')
            else:
                head.cond = "1'b1"
            head.t, head.f = body, k
            return self.code(ctx, init, head)
        if kind == 'ForeachLoop':
            arr = self.ex(n['arrayRef'], ctx)
            dims = n.get('loopDims') or []
            if len(dims) != 1 or not dims[0].get('loopVar'): raise Unsupported('foreach over several dimensions')
            lv = dims[0]['loopVar']
            iv = self.var(f'{ctx.prefix}__{lv.get("sym") or "i"}', BITS(32, True))
            ctx.env[lv.get('path') or lv.get('sym')] = iv
            size = self.size_of(arr)
            head = p.node('br', cond=f'({iv.t} < $signed(32\'({size})))', desc='foreach')
            step = p.node('code', lines=[f'{iv.t} = {iv.t} + 1;'], nxt=head)
            ctx.loops.append((k, step))
            body = self.lower(n.get('body'), step, ctx)
            ctx.loops.pop()
            head.t, head.f = body, k
            return self.code(ctx, [f'{iv.t} = 0;'], head)
        raise Unsupported(f'loop {kind}')

    # ------------------------------------------------------------ blocking calls
    def lower_call_stmt(self, e, k, ctx):
        e0 = e
        e = strip_conv(e)
        if e.get('k') == 'Assignment' and strip_conv(e['right']).get('k') == 'Call':
            # x = f(...) with f a task with waits: not in SV (tasks have no return) -- function calls only
            raise Unsupported('assignment from a call with a wait')
        if e.get('k') != 'Call': raise Unsupported(f'expression statement with a wait: {e.get("k")}')
        nm = e.get('subroutineName')
        obj = None
        if e.get('thisClass') is not None:
            obj = self.ex(e['thisClass'], ctx)
        p = ctx.proc
        args = e.get('arguments') or []
        if obj is not None and obj.inst is not None:
            inst = obj.inst
            if inst.kind == 'chan' and nm in ('get', 'peek'):
                lhs = self.lvalue_arg(args[0], ctx)
                ch = self.chan(inst)
                rd = [f'{lhs.t} = {ch}__q[0];'] + ([f'{ch}__pop();'] if nm == 'get' else [])
                self.touch('r', 'ch', ch)
                return p.node('wait', wk='chan', ready=f'({ch}__n != 0)', resume=rd, nxt=k, desc=f'{inst.name}.{nm}()',
                              line=self.cur_line)
            if inst.kind == 'chan' and nm == 'put':
                v = self.ex(self.arg_expr(args[0]), ctx)
                ch = self.chan(inst)
                self.touch('w', 'ch', ch)
                if not inst.bound: return self.code(ctx, ctx.pre + [f'{ch}__push({v.t});'], k)
                return p.node('wait', wk='chan', ready=f'({ch}__n < {inst.bound})', resume=[f'{ch}__push({v.t});'], nxt=k,
                              desc=f'{inst.name}.put()', line=self.cur_line)
            if inst.kind == 'port' and nm in ('get_next_item', 'get', 'peek'):
                sq = self.port_sqr(inst)
                lhs = self.lvalue_arg(args[0], ctx)
                pr = self.proto(sq)
                res = [f'{lhs.t} = {pr["item"]};']
                if nm != 'peek': res += [f"{pr['iv']} = 1'b0;", f"{pr['req']} = 1'b0;"]
                if nm == 'get': res += [f"{pr['done']} = 1'b1;", f"{pr['gnt']} = 1'b0;"]
                pr['drivers'] += 1
                self.touch('w', 'req', sq.name); self.touch('r', 'iv', sq.name)
                if nm == 'get': self.touch('w', 'done', sq.name)
                return p.node('wait', wk='gni', ready=f'{pr["iv"]}', enter=[f"{pr['req']} = 1'b1;"], resume=res, nxt=k,
                              desc=f'{inst.name}.{nm}()', line=self.cur_line)
            if inst.kind == 'sqr' and nm in ('get_next_item', 'get'):
                raise Unsupported('sequencer method called directly')
            if inst.kind == 'seq' and nm == 'start':
                sqr = self.static_ref(args[0], ctx.this) if args else inst.sqr
                return self.inline_seq(inst, sqr, k, ctx)
        if nm in ('start_item', 'finish_item') and isinstance(ctx.this, Inst) and ctx.this.kind == 'seq':
            sq = ctx.this.sqr
            pr = self.proto(sq)
            pr['seqs'].add(p.id)
            item = self.ex(self.arg_expr(args[0]), ctx)
            if nm == 'start_item':
                self.touch('r', 'req', sq.name)
                return p.node('wait', wk='start_item', ready=f'({pr["req"]} && !{pr["gnt"]} && !{pr["iv"]})',
                              resume=[f"{pr['gnt']} = 1'b1;", f"{pr['owner']} = {pr['ow']}'d{p.id};"], nxt=k,
                              desc='start_item', line=self.cur_line)
            self.touch('w', 'iv', sq.name); self.touch('r', 'done', sq.name)
            return p.node('wait', wk='finish_item', enter=[f'{pr["item"]} = {item.t};', f"{pr['iv']} = 1'b1;"],
                          ready=f"({pr['done']} && {pr['owner']} == {pr['ow']}'d{p.id})",
                          resume=[f"{pr['done']} = 1'b0;"], nxt=k, desc='finish_item', line=self.cur_line)
        if nm == 'get_response' and isinstance(ctx.this, Inst) and ctx.this.kind == 'seq':
            pr = self.proto(ctx.this.sqr)
            lhs = self.lvalue_arg(args[0], ctx)
            self.touch('r', 'ch', pr['rsp'])
            return p.node('wait', wk='chan', ready=f'({pr["rsp"]}__n != 0)',
                          resume=[f'{lhs.t} = {pr["rsp"]}__q[0];', f'{pr["rsp"]}__pop();'], nxt=k, desc='get_response',
                          line=self.cur_line)
        # a task of the agent with waits inside: inline it
        sp = (e.get('subroutine') or {}).get('path')
        if sp and sp in self.D.subs and self.D.subs[sp].get('body') is not None:
            this = ctx.this
            if obj is not None:
                if obj.inst is None: raise Unsupported(f'{nm}(): task of a non-static object')
                this = obj.inst
            sub = self.D.method(this.cls, nm) if isinstance(this, Inst) and this.cls else self.D.subs[sp]
            sub = sub or self.D.subs[sp]
            return self.inline_task(sub, args, this, k, ctx)
        raise Unsupported(f'blocking call {nm}()')

    def arg_expr(self, a):
        a = strip_conv(a)
        if a.get('k') == 'Assignment' and a.get('isLValueArg'): return a['left']
        return a

    def lvalue_arg(self, a, ctx):
        a = strip_conv(a)
        if a.get('k') == 'Assignment' and a.get('isLValueArg'): a = a['left']
        return self.lv(a, ctx)

    def inline_task(self, sub, args, this, k, ctx):
        depth = getattr(ctx, 'depth', 0)
        if depth > 6: raise Unsupported(f'{sub["name"]}(): recursion / nesting too deep')
        self.inl_n = getattr(self, 'inl_n', 0) + 1
        nm_ = ident(sub["name"])
        prefix = ctx.prefix if ctx.prefix.endswith('__' + nm_) else f'{ctx.prefix}__{nm_}'
        c = ctx.sub(this=this, prefix=prefix)
        c.depth = depth + 1
        c.loops, c.env = [], {}
        exitn = ctx.proc.node('code', desc=f'end of {sub["name"]}')
        c.ret = (exitn, None)
        pre, post = [], []
        formals = sub.get('args') or []
        for i, f in enumerate(formals):
            ty = self.ty_ti(f.get('ti'), f['name']) if f.get('ti') else self.ty_str(f.get('type'), f.get('w'), f.get('signed'))
            a = args[i] if i < len(args) else None
            d = (f.get('dir') or f.get('direction') or 'In').lower()
            fv = self.var(f'{prefix}__{f["name"]}', ty)
            c.env[f.get('path') or f'{sub["path"]}.{f["name"]}'] = fv
            if a is None or strip_conv(a).get('k') == 'EmptyArgument':
                if f.get('default_ast') is not None:
                    dv = self.lift(f'{sub["name"]}_{f["name"]}', f['default_ast'], ty) if self.test_code() else None
                    pre.append(f'{fv.t} = {(dv or self.ex(f["default_ast"], ctx)).t};')
                continue
            if d in ('in', 'input', 'inout', 'ref', 'constref', 'const ref'):
                if d == 'ref' and strip_conv(a).get('k') in ('NamedValue', 'MemberAccess', 'ElementSelect'):
                    c.env[f.get('path') or f'{sub["path"]}.{f["name"]}'] = self.lvalue_arg(a, ctx)
                    continue
                v = self.lift(f'{sub["name"]}_{f["name"]}', self.arg_expr(a), ty) if self.test_code() else None
                v = v or self.ex(self.arg_expr(a), ctx)
                pre += ctx.pre + [self.assign_text(fv, v)]; ctx.pre = []
            if d in ('out', 'output', 'inout'):
                lhs = self.lvalue_arg(a, ctx)
                post.append(self.assign_text(lhs, fv))
        exitn.lines = post
        exitn.nxt = k
        saved, saved_sub = getattr(self, 'cur_file', None), getattr(self, 'cur_sub', None)
        self.cur_file, self.cur_sub = sub.get('file'), sub['path']
        body = self.lower(sub['body'], exitn, c)
        self.cur_file, self.cur_sub = saved, saved_sub
        return self.code(ctx, pre, body)

    def inline_seq(self, inst, sqr, k, ctx):
        """seq.start(sqr): the sequence's body (pre_body / post_body included when defined)"""
        inst.sqr = sqr
        for nm in ('p_sequencer', 'm_sequencer'):
            inst.members[nm] = sqr
        pr = self.proto(sqr)
        pr['seqs'].add(ctx.proc.id)
        nxt = k
        for m in ('post_body', 'body', 'pre_body'):
            s = self.D.method(inst.cls, m)
            if s is None or s['path'].startswith('uvm_pkg::'): continue
            nxt = self.inline_task(s, [], inst, nxt, ctx)
        return nxt

    # ------------------------------------------------------------ sequencer handshake / channels
    def proto(self, sq):
        if sq.proto is None:
            n = sq.name
            elem = sq.elem
            rsp_ty = sq.members.get('__rsp_ty') or elem
            ow = 8
            sq.proto = {'req': self.var(f'{n}__req', BITS(1)).t, 'gnt': self.var(f'{n}__gnt', BITS(1)).t,
                        'iv': self.var(f'{n}__iv', BITS(1)).t, 'done': self.var(f'{n}__done', BITS(1)).t,
                        'owner': self.var(f'{n}__owner', BITS(ow)).t, 'ow': ow,
                        'item': self.var(f'{n}__item', elem).t, 'seqs': set(), 'drivers': 0}
            rq = Inst('chan', f'{n}__rspq', None)
            rq.elem = rsp_ty
            self.chans.append(rq)
            sq.proto['rsp'] = self.chan(rq)
            sq.proto['rspq'] = rq
        return sq.proto

    def port_sqr(self, port):
        for t in port.targets:
            if t.kind == 'sqr': return t
            if t.kind == 'port':
                s = self.port_sqr(t)
                if s: return s
        raise Unsupported(f'{port.name}: not connected to a sequencer in connect_phase')

    def chan(self, inst):
        """a channel's variables and push/pop functions (declared once)"""
        nm = inst.name
        if getattr(inst, '_decl', False): return nm
        inst._decl = True
        if inst.elem is None: raise Unsupported(f'{nm}: element type unknown')
        self.var(f'{nm}__q', Ty('array', elem=inst.elem, n=self.QD, akind='fixed'))
        self.var(f'{nm}__n', BITS(cw(self.QD)))
        self.var(f'{nm}__old', BITS(cw(self.QD)), init=f'{nm}__n_q', kind='snap')
        self.lates.append(nm)
        ed = self.decl(inst.elem, 'v')
        qd, cwn = self.QD, cw(self.QD)
        self.func_text.append(f'''  // channel {nm}: QDEPTH entries, oldest first. __old: the entries it held before this edge (not yet
  // taken). A non-blocking get may run before or after the process that puts at the same edge
  // (fv_late_{nm}: before -- it sees only the older entries); a blocking get always sees the put
  function automatic void {nm}__push(input {ed});
    if ({nm}__n == {cwn}'d{qd}) fv_ovf = 1'b1;
    else begin {nm}__q[{nm}__n] = v; {nm}__n = {nm}__n + 1'b1; end
  endfunction
  function automatic void {nm}__pop();
    for (int i = 0; i < {qd - 1}; i++) {nm}__q[i] = {nm}__q[i + 1];
    if ({nm}__n != 0) {nm}__n = {nm}__n - 1'b1;
    if ({nm}__old != 0) {nm}__old = {nm}__old - 1'b1;
  endfunction
  function automatic logic {nm}__try_get(output {ed});
    v = {nm}__q[0];
    if ({nm}__n == 0 || (fv_late_{nm} && {nm}__old == 0)) return 1'b0;
    {nm}__pop();
    return 1'b1;
  endfunction
  function automatic logic {nm}__try_peek(output {ed});
    v = {nm}__q[0];
    return {nm}__n != 0 && !(fv_late_{nm} && {nm}__old == 0);
  endfunction''')
        return nm

    def analysis_targets(self, port, seen=None):
        seen = seen or set()
        out = []
        for t in port.targets:
            if id(t) in seen: continue
            seen.add(id(t))
            if t.kind == 'chan': out.append(t)
            elif t.kind == 'port': out += self.analysis_targets(t, seen)
        return out

    def free_source(self, sq):
        """a sequencer the agent starts no sequence on: any item the item class allows, whenever the driver
        asks or later"""
        pr = self.proto(sq)
        p = Proc(self, f'{sq.name}__src', f'free item source for {sq.name}')
        ctx = Ctx(self, p, sq, p.name)
        p.acc |= {('r', 'req', sq.name), ('w', 'iv', sq.name), ('r', 'done', sq.name)}
        item = self.var(f'{p.name}__item', sq.elem)
        offer = self.site(1, False, f'{sq.name}: a sequence offers an item at this edge', depth=1, meta={'kind': 'offer'})
        start = p.node('wait', wk='start_item', ready=f'({pr["req"]} && !{pr["gnt"]} && !{pr["iv"]} && {offer}_v)',
                       resume=[f"{pr['gnt']} = 1'b1;", f"{pr['owner']} = {pr['ow']}'d{p.id};"], desc='start_item')
        lines = []
        if sq.elem.kind == 'struct':
            lines += [f"{item.t} = '0;"] + self.randomize_lines(item, sq.elem.cls, None, ctx, all_fields=True)
        else:
            s = self.site(sq.elem.w, sq.elem.s, f'{sq.name}: item', ctx)
            lines.append(f'{item.t} = {s}(1\'b1);')
        fin = p.node('wait', wk='finish_item', enter=[f'{pr["item"]} = {item.t};', f"{pr['iv']} = 1'b1;"],
                     ready=f"({pr['done']} && {pr['owner']} == {pr['ow']}'d{p.id})", resume=[f"{pr['done']} = 1'b0;"],
                     desc='finish_item')
        mk = p.node('code', lines=lines, nxt=fin)
        start.nxt = mk
        fin.nxt = start
        p.entry = start
        pr['seqs'].add(p.id)
        pr['free'] = True
        self.note(f'{sq.name}: no sequence is started on it inside the agent -- modelled as a free source of '
                  f'{sq.elem.cls.split("::")[-1] if sq.elem.kind == "struct" else "items"} (class constraints, '
                  'non-rand fields free)')

    # ------------------------------------------------------------ free choices
    def site(self, w, s, desc, ctx=None, depth=None, meta=None):
        """a free choice: fv_chN_v holds the values it may take in one clock edge (depth of them: 1 unless the
        site is inside a loop or a function); fv_chN() takes the next one"""
        if depth is None:
            depth = 1
            if ctx is None or ctx.mode == 'func': depth = self.CHOICES
            else:
                for b in ctx.nmult: depth *= b
            depth = max(1, min(depth, 64))
        sid = len(self.sites)
        self.sites.append((sid, w, desc, depth))
        m = dict(meta or {})
        m.setdefault('file', getattr(self, 'cur_file', None)); m.setdefault('line', getattr(self, 'cur_line', None))
        m.setdefault('sub', getattr(self, 'cur_sub', None))
        self.site_meta[sid] = m
        nm = f'fv_ch{sid}'
        if depth == 1:
            body = f"    {nm} = {nm}_v;\n    if ({nm}_u) fv_ovf = 1'b1;\n    {nm}_u = 1'b1;"
        else:
            uw = iw(depth)
            body = (f"    {nm} = {nm}_v[{nm}_u];\n"
                    f"    if ({nm}_u == {uw}'d{depth - 1}) fv_ovf = 1'b1; else {nm}_u = {nm}_u + 1'b1;")
        self.site_funcs.append(f'''  // {desc}
  function automatic logic {"signed " if s else ""}[{w - 1}:0] {nm}(input logic en);
{body}
  endfunction''')
        return nm

    def urandom(self, call, ctx):
        nm = call.get('subroutineName')
        saved, self.rd = self.rd, None
        try:
            args = [self.ex(a, ctx) for a in call.get('arguments') or []]
        finally:
            self.rd = saved
        if nm == '$urandom_range':
            cs = [const_int(strip_conv(a).get('const')) if strip_conv(a).get('const') is not None else None
                  for a in call.get('arguments') or []]
            w = 32
            if cs and all(c is not None and c >= 0 for c in cs): w = cw(max(cs))     # constant range: narrow
            s = self.site(w, False, f'$urandom_range at {self.where()}', ctx, meta={'kind': 'urandom_range'})
            hi = f"32'({args[0].t})"
            lo = f"32'({args[1].t})" if len(args) > 1 else "32'd0"
            t = self.tmp(ctx, BITS(32), 'ur')
            ctx.pre += [f"{t.t} = {s}(1'b1);",
                        f'if (!(({t.t} >= (({lo}) < ({hi}) ? ({lo}) : ({hi}))) && ({t.t} <= (({lo}) < ({hi}) ? ({hi}) : ({lo})))))'
                        f" fv_bad = 1'b1;"]
            return V(t.t, BITS(32))
        if nm in ('$urandom', '$random'):
            sg = nm == '$random'
            s = self.site(32, sg, f'{nm} at {self.where()}', ctx, meta={'kind': nm[1:]})
            return V(f"{s}(1'b1)", BITS(32, sg))
        raise Unsupported(nm)

    def randomize_lines(self, obj, cls, with_items, ctx, all_fields=False, only=None):
        saved, self.rd = self.rd, None
        try:
            return self._randomize_lines(obj, cls, with_items, ctx, all_fields, only)
        finally:
            self.rd = saved

    def _randomize_lines(self, obj, cls, with_items, ctx, all_fields=False, only=None):
        """obj.randomize() [with {...}]: free rand fields, constraints as assumptions, post_randomize"""
        lines = []
        for f, ty, pr in self.struct_fields(cls):
            rand = (pr.get('rand') or 'None_') != 'None_'
            if only is not None: rand = f in only
            if not (rand or all_fields): continue
            if ty.kind == 'bits':
                s = self.site(ty.w, ty.s, f'{cls.split("::")[-1]}.{f} ({"rand" if rand else "free: set by the sequence"})', ctx,
                              meta={'kind': 'field', 'class': cls, 'field': f, 'rand': rand})
                lines.append(f"{obj.t}.{f} = {s}(1'b1);")
            elif ty.kind == 'array' and ty.elem.kind in ('bits', 'none'):
                for i in (range(ty.n) if ty.elem.kind == 'bits' else []):
                    s = self.site(ty.elem.w, ty.elem.s, f'{cls.split("::")[-1]}.{f}[{i}]', ctx)
                    lines.append(f"{obj.t}.{f}[{i}] = {s}(1'b1);")
                if ty.akind == 'queue':
                    s = self.site(cw(ty.n), False, f'{cls.split("::")[-1]}.{f}.size()', ctx)
                    lines.append(f"{obj.t}.{f}__n = {s}(1'b1);")
                    lines.append(f"if ({obj.t}.{f}__n > {cw(ty.n)}'d{ty.n}) fv_bad = 1'b1;")
            else:
                self.note(f'randomize: {cls.split("::")[-1]}.{f} ({ty!r}) left as it was')
        c = ctx.sub(this=V(obj.t, Ty('struct', cls=cls)))
        c.env = dict(ctx.env)
        c.outer = ctx.this
        c.cond_depth = 1
        for p in self.D.bases(cls):
            for blk in self.D.classes[p].get('constraints') or []:
                if blk.get('static'): pass
                for it in blk.get('items') or []:
                    lines += self.constraint(it, c, f'{cls.split("::")[-1]}::{blk["name"]}')
        for it in with_items or []:
            lines += self.constraint(it, c, 'with {}')
        pr_ = self.D.method(cls, 'post_randomize')
        if pr_ is not None and not pr_['path'].startswith('uvm_pkg'):
            fn = self.value_method(cls, pr_)
            lines.append(f'{obj.t} = {fn}({obj.t});')
        return lines

    def constraint(self, it, ctx, where):
        k = it.get('k')
        try:
            if k == 'Expression':
                if it.get('isSoft'):
                    self.note(f'{where}: soft constraint dropped (over-approximation)'); return []
                e = it['expr']
                if e.get('k') == 'Dist' or 'dist' in (e.get('k') or '').lower():
                    return self.dist(e, ctx, where)
                c = self.cond(e, ctx)
                return [f"if (!({c})) fv_bad = 1'b1;"]
            if k == 'Implication':
                p = self.cond(it['predicate'], ctx)
                out = []
                for b in self.items_of(it.get('body')):
                    out += [f'if ({p}) begin', *['  ' + x for x in self.constraint(b, ctx, where)], 'end']
                return out
            if k == 'Conditional':
                p = self.cond(it['predicate'], ctx)
                t = [x for b in self.items_of(it.get('ifBody')) for x in self.constraint(b, ctx, where)]
                f = [x for b in self.items_of(it.get('elseBody')) for x in self.constraint(b, ctx, where)]
                return [f'if ({p}) begin', *['  ' + x for x in t], 'end else begin', *['  ' + x for x in f], 'end']
            if k == 'List':
                return [x for b in it.get('list') or [] for x in self.constraint(b, ctx, where)]
            if k in ('SolveBefore',):
                return []
            self.note(f'{where}: constraint item {k} dropped (over-approximation)')
            return []
        except Unsupported as e:
            self.note(f'{where}: constraint "{(it.get("src") or "")[:50]}" dropped ({e}; over-approximation)')
            return []

    def dist(self, e, ctx, where):
        self.note(f'{where}: dist weights ignored (the value set is kept)')
        x = self.ex(e['left'], ctx)
        terms = []
        for it in e.get('items') or []:
            v = it.get('value') or it
            terms.append(self.inside_term(x, v, ctx))
        return [f"if (!({' || '.join(terms) or '1'})) fv_bad = 1'b1;"]

    def items_of(self, b):
        if b is None: return []
        if b.get('k') == 'List': return b.get('list') or []
        return [b]

    def value_method(self, cls, sub):
        """a method of a value class as an SV function: self in, (modified) self out"""
        key = ('vm', cls, sub['path'])
        if key in self.funcs: return self.funcs[key]
        nm = f'{ident(cls.split("::")[-1])}__{ident(sub["name"])}'
        self.funcs[key] = nm
        st = self.struct(cls)
        ctx = Ctx(self, None, V('self', Ty('struct', cls=cls), lv=True), nm, mode='func')
        ctx.fdecls = []
        if (sub.get('returns') or 'void') != 'void':
            raise Unsupported(f'{sub["name"]}(): value-class method with a return value')
        args = []
        for f in sub.get('args') or []:
            ty = self.ty_ti(f.get('ti'), f['name']) if f.get('ti') else self.ty_str(f.get('type'), f.get('w'), f.get('signed'))
            an = ident(f['name'])
            args.append(f'input {self.decl(ty, an)}')
            ctx.env[f.get('path') or f'{sub["path"]}.{f["name"]}'] = V(an, ty, lv=True)
        saved, saved_sub = getattr(self, 'cur_file', None), getattr(self, 'cur_sub', None)
        self.cur_file, self.cur_sub = sub.get('file'), sub['path']
        body = self.stmt(sub['body'], ctx)
        self.cur_file, self.cur_sub = saved, saved_sub
        decls = [f'    {self.decl(t, n)};' for n, t in ctx.fdecls]
        self.func_text.append('\n'.join([f'  function automatic {st} {nm}(input {st} self{"".join(", " + a for a in args)});',
                                         *decls, *['    ' + x for x in body], '    return self;', '  endfunction']))
        return nm

    def comp_method(self, inst, sub):
        """a wait-free method of a static object as an SV function on the module's variables"""
        key = ('cm', inst.name, sub['path'])
        if key in self.funcs:
            if self.acc is not None: self.acc |= self.func_acc.get(key, set())
            return self.funcs[key]
        outer, self.acc = self.acc, set()
        try:
            return self._comp_method(inst, sub, key)
        finally:
            self.func_acc[key] = self.acc
            self.acc = outer
            if outer is not None: outer |= self.func_acc[key]

    def _comp_method(self, inst, sub, key):
        nm = f'{inst.name}__{ident(sub["name"])}'
        self.funcs[key] = nm
        ctx = Ctx(self, None, inst, nm, mode='func')
        ctx.fdecls = []
        rt = sub.get('returns') or 'void'
        rty = None
        if rt != 'void':
            rty = self.ty_ti(sub.get('ret_ti'), sub['name']) if sub.get('ret_ti') else self.ty_str(rt, sub.get('ret_w'), sub.get('ret_signed'))
            ctx.env[f'{sub["path"]}.{sub["name"]}'] = V(nm, rty, lv=True)
            ctx.fret = rty
        args = []
        for f in sub.get('args') or []:
            ty = self.ty_ti(f.get('ti'), f['name']) if f.get('ti') else self.ty_str(f.get('type'), f.get('w'), f.get('signed'))
            d = (f.get('dir') or f.get('direction') or 'In').lower()
            d = {'in': 'input', 'out': 'output', 'inout': 'inout', 'ref': 'inout'}.get(d, 'input')
            an = ident(f['name'])
            args.append(f'{d} {self.decl(ty, an)}')
            ctx.env[f.get('path') or f'{sub["path"]}.{f["name"]}'] = V(an, ty, lv=True)
        saved, saved_sub = getattr(self, 'cur_file', None), getattr(self, 'cur_sub', None)
        self.cur_file, self.cur_sub = sub.get('file'), sub['path']
        body = self.stmt(sub['body'], ctx)
        self.cur_file, self.cur_sub = saved, saved_sub
        decls = [f'    {self.decl(t, n)};' for n, t in ctx.fdecls]
        head = f'  function automatic {"void" if rty is None else self.decl(rty, "")} {nm}({", ".join(args)});'
        self.func_text.append('\n'.join([head.replace(' ;', ';'), *decls, *['    ' + x for x in body], '  endfunction']))
        return nm

    # ------------------------------------------------------------ wait-free statements -> SV
    def stmt(self, n, ctx):
        """a statement without waits -> SV lines"""
        if n is None: return []
        k = n.get('k')
        if n.get('line'): self.cur_line = n['line']
        if n.get('macro') in UVM_MSG:
            return self.uvm_msg(n)
        if k == 'List':
            out, last = [], None
            for x in n.get('list') or []:
                m = x.get('macro')
                if m in UVM_MSG:
                    key = (m, x.get('line'))
                    if key == last: continue
                    last = key
                    out += self.uvm_msg(x)
                    continue
                last = None
                out += self.stmt(x, ctx)
            return out
        if k == 'Block':
            if n.get('blockKind') not in (None, 'Sequential'): raise Unsupported('fork in a function')
            return self.stmt(n.get('body'), ctx)
        if k in ('Empty', None): return []
        if k == 'VariableDeclaration':
            return self.declare(n, ctx)
        if k == 'ExpressionStatement':
            return self.expr_stmt(n['expr'], ctx)
        if k == 'Conditional':
            conds = n.get('conditions') or []
            if len(conds) != 1 or conds[0].get('pattern'): raise Unsupported('if with a pattern / &&&')
            c = self.cond(conds[0]['expr'], ctx)
            pre = ctx.pre; ctx.pre = []
            t = self.stmt(n.get('ifTrue'), ctx)
            f = self.stmt(n.get('ifFalse'), ctx)
            out = pre + [f'if ({c}) begin', *ind(t), 'end']
            if f: out[-1:] = ['end else begin', *ind(f), 'end']
            return out
        if k == 'Case':
            sel = self.ex(n['expr'], ctx)
            pre = ctx.pre; ctx.pre = []
            kw = {'Normal': 'case', None: 'case', 'WildcardXOrZ': 'casex', 'WildcardJustZ': 'casez'}.get(n.get('condition'))
            if kw is None or n.get('check') not in (None, 'None'): raise Unsupported(f'case {n.get("condition")}')
            out = pre + [f'{kw} ({sel.t})']
            for it in n.get('items') or []:
                es = [self.ex(e, ctx).t for e in it.get('expressions') or []]
                out += [f'  {", ".join(es)}: begin', *ind(self.stmt(it.get('stmt'), ctx), 2), '  end']
            if n.get('defaultCase') is not None:
                out += ['  default: begin', *ind(self.stmt(n['defaultCase'], ctx), 2), '  end']
            else:
                out += ['  default: ;']
            return out + ['endcase']
        if k in ('ForLoop', 'WhileLoop', 'DoWhileLoop', 'RepeatLoop', 'ForeverLoop', 'ForeachLoop'):
            return self.native_loop(n, ctx)
        if k == 'Break':
            if not ctx.nloops: raise Unsupported('break outside a loop')
            fl = ctx.nloops[-1]
            return ([f"{fl} = 1'b1;"] if fl else []) + ['break;']
        if k == 'Continue':
            return ['continue;']
        if k == 'Return':
            if ctx.mode != 'func': raise Unsupported('return in process code')
            if n.get('expr') is not None:
                v = self.ex(n['expr'], ctx)
                v = self.cast_to(v, ctx.fret) if ctx.fret is not None else v
                return self.flush(ctx) + [f'return {v.t};']
            return ['return;']
        if k == 'ImmediateAssertion':
            c = self.cond(n['cond'], ctx)
            kind = n.get('assertionKind', 'Assert')
            if kind == 'Assert':
                e = self.error(f'immediate assert at {self.where()}')
                return self.flush(ctx) + [f"if (!({c})) {e} = 1'b1;"]
            if kind == 'Assume':
                return self.flush(ctx) + [f"if (!({c})) fv_bad = 1'b1;"]
            return []
        if k == 'Timed':
            raise Unsupported('timing control in a function')
        raise Unsupported(f'statement {k}')

    def flush(self, ctx):
        pre = ctx.pre; ctx.pre = []
        return pre

    def uvm_msg(self, n):
        m = n.get('macro', '')
        if 'error' in m or 'fatal' in m:
            e = self.error(f'`{m} at {self.where()}')
            return [f"{e} = 1'b1;"]
        return []

    def error(self, desc):
        k = len(self.errors)
        self.errors.append((k, desc))
        return f'fv_err[{k}]'

    def declare(self, n, ctx):
        sym = n['symbol']
        path = sym.get('path') or sym['sym']
        ti = n.get('ti')
        ty = self.ty_ti(ti, sym['sym']) if ti else self.ty_str(n.get('ty'), n.get('w'), n.get('signed'))
        init = n.get('init')
        if ty.kind == 'obj':
            cp = self.D.cpath(ty.cls)
            if cp and self.D.is_a(cp, 'uvm_sequence_base'):
                ctx.env[path] = V(None, ty, inst=self.seq_inst(cp, f'{ctx.prefix}__{sym["sym"]}', None))
                return []
            if init is None: return []
            v = self.ex(init, ctx)
            if v.inst is None: raise Unsupported(f'{sym["sym"]}: handle to a static object from an expression')
            ctx.env[path] = v
            return []
        if ty.kind == 'opaque':
            ctx.env[path] = V(None, OPAQUE)
            return []
        if n.get('macro') in UVM_MSG: return []
        if ctx.mode == 'func':
            nm = f'{ident(sym["sym"])}_{len(ctx.fdecls)}'
            ctx.fdecls.append((nm, ty))
            v = V(nm, ty, lv=True)
        else:
            v = self.var(f'{ctx.prefix}__{sym["sym"]}', ty)
        ctx.env[path] = v
        if init is not None:
            rd_outer, self.rd = self.rd, set()
            try:
                iv = self.ex(init, ctx)
            finally:
                reads, self.rd = self.rd, rd_outer
            return self.flush(ctx) + self.kept(v, reads, self.assign_text(v, iv))
        return self.kept(v, set(), self.zero_text(v)) if v.ty.kind != 'struct' else [self.zero_text(v)]

    def reinit(self, v):
        ty, init, kind = self.vars.get(v.t, (v.ty, None, 'state'))
        if init: return f'{v.t} = {init};'
        return self.zero_text(v)

    def zero_text(self, v):
        if v.ty.kind == 'array' and v.ty.akind == 'queue': return f"{v.t}__n = '0;"
        if v.ty.kind == 'array' and v.ty.akind == 'assoc': return f"{v.t}__e = '0;"
        return f"{v.t} = '0;"

    def assign_text(self, lhs, rhs):
        if rhs.ty.kind == 'new':
            out = [f"{lhs.t} = '0;"]
            if lhs.ty.kind == 'struct':
                for f, ty, pr in self.struct_fields(lhs.ty.cls):
                    if ty.kind == 'bits' and pr.get('init_value'):
                        out.append(f'{lhs.t}.{f} = {sv_const(pr["init_value"], ty.w, ty.s)};')
            return ' '.join(out)
        if lhs.ty.kind == 'array' and lhs.ty.akind == 'queue':
            if rhs.ty.kind == 'empty': return f"{lhs.t}__n = '0;"
            if rhs.ty.kind == 'array' and rhs.ty.akind == 'queue':
                return f'{lhs.t} = {rhs.t}; {lhs.t}__n = {rhs.t}__n;'
            raise Unsupported(f'assignment of {rhs.ty!r} to a queue')
        if lhs.ty.kind == 'array' and lhs.ty.akind == 'assoc':
            if rhs.ty.kind == 'array' and rhs.ty.akind == 'assoc':
                return f'{lhs.t} = {rhs.t}; {lhs.t}__e = {rhs.t}__e;'
            raise Unsupported('assignment to an associative array')
        if lhs.ty.kind == 'bits':
            return f'{lhs.t} = {self.cast_to(rhs, lhs.ty).t};'
        return f'{lhs.t} = {rhs.t};'

    def cast_to(self, v, ty):
        if ty is None or ty.kind != 'bits' or v.ty.kind != 'bits': return v
        if v.ty.w == ty.w and v.ty.s == ty.s: return v
        t = f"{ty.w}'({v.t})" if v.ty.w != ty.w else v.t
        if v.ty.s != ty.s: t = f'$signed({t})' if ty.s else f'$unsigned({t})'
        return V(t, ty)

    def native_loop(self, n, ctx):
        """a loop without waits: an SV for loop with a static bound; an assertion fails when the bound is hit"""
        k = n['k']
        self.loop_n = getattr(self, 'loop_n', 0) + 1
        i = f'__i{self.loop_n}'
        out = []
        bound, exact = self.LOOP, False
        if k == 'ForeachLoop':
            arr = self.ex(n['arrayRef'], ctx)
            dims = n.get('loopDims') or []
            if len(dims) != 1 or not dims[0].get('loopVar'): raise Unsupported('foreach over several dimensions')
            lv = dims[0]['loopVar']
            ty = arr.ty
            if ty.kind != 'array': raise Unsupported('foreach over a non-array')
            iv = self.loop_var(ctx, lv.get('path') or lv.get('sym'), lv.get('sym') or 'i')
            size = self.size_of(arr)
            body = self.nested(ctx, None, lambda: self.stmt(n.get('body'), ctx), ty.n)
            return self.flush(ctx) + [f'for (int {i} = 0; {i} < {ty.n}; {i}++) begin',
                                      f'  if ({i} >= {size}) break;', f'  {iv.t} = {i};', *ind(body), 'end']
        if k == 'RepeatLoop':
            cnt = self.ex(n['count'], ctx)
            c = cnt.t
            if cnt.ty.kind == 'bits' and const_int(c.split("'")[-1].lstrip('sdhb')) is not None and "'" in c:
                pass
            kv = const_int(strip_conv(n['count']).get('const')) if strip_conv(n['count']).get('const') else None
            ct = self.tmp(ctx, BITS(32), 'rc')
            out = self.flush(ctx) + [f"{ct.t} = 32'({c});"]
            if kv is not None: bound, exact = kv, True
            fl = None if exact else self.tmp(ctx, BITS(1), 'x').t
            body = self.nested(ctx, fl, lambda: self.stmt(n.get('body'), ctx), bound)
            out += ([f"{fl} = 1'b0;"] if fl else []) + [f'for (int {i} = 0; {i} < {bound}; {i}++) begin',
                                                       f"  if ({i} >= {ct.t}) begin {fl + ' = 1' + chr(39) + 'b1; ' if fl else ''}break; end",
                                                       *ind(body), 'end']
            if fl: out.append(f"if (!{fl} && {ct.t} > {bound}) fv_ovf = 1'b1;")
            return out
        init, steps, cond = [], [], None
        if k == 'ForLoop':
            for lv in n.get('loopVars') or []:
                init += self.loop_decl(lv, ctx)
            for e in n.get('initializers') or []:
                init += self.expr_stmt(e, ctx)
            for e in n.get('steps') or []:
                steps += self.expr_stmt(e, ctx)
            if n.get('stopExpr') is not None:
                cond = self.cond(n['stopExpr'], ctx)
            b = self.const_bound(n, ctx)
            if b is not None: bound, exact = b, True
            else:
                b = self.stop_bound(n.get('stopExpr'), ctx)
                if b is not None: bound = b
            for lv in n.get('loopVars') or []:
                v = ctx.env.get(lv.get('path') or lv.get('sym'))
                if v is not None and v.t: self.vbound[v.t] = bound
        elif k in ('WhileLoop', 'DoWhileLoop'):
            cond = self.cond(n['cond'], ctx)
            b = self.size_bound(n['cond'], ctx)
            if b is not None: bound = b
        elif k == 'ForeverLoop':
            cond = None
        cpre = self.flush(ctx)
        fl = None if exact else self.tmp(ctx, BITS(1), 'x').t
        body = self.nested(ctx, fl, lambda: self.stmt(n.get('body'), ctx), bound + (0 if exact else 1))
        out = init + ([f"{fl} = 1'b0;"] if fl else []) + [f'for (int {i} = 0; {i} < {bound + (0 if exact else 1)}; {i}++) begin']
        if steps: out += [f'  if ({i} != 0) begin', *ind(steps, 2), '  end']
        exit_ = f"begin {fl} = 1'b1; break; end" if fl else 'break;'
        if cond is not None:
            if k == 'DoWhileLoop':
                out += [f'  if ({i} != 0) begin', *ind(cpre, 2), f'    if (!({cond})) {exit_}', '  end']
            else:
                out += [*ind(cpre), f'  if (!({cond})) {exit_}']
        if not exact:       # still going after `bound` iterations (the exit test first: bound iterations are fine)
            out += [f'  if ({i} == {bound}) begin fv_ovf = 1\'b1; break; end']
        out += [*ind(body), 'end']
        return out

    def loop_decl(self, lv, ctx):
        """for (int i = ...): slang declares i in the enclosing block (with its initializer); here only when not"""
        if lv.get('k') == 'VariableDeclaration': return self.declare(lv, ctx)
        path = lv.get('path') or lv.get('sym')
        if path in ctx.env: return []
        return self.declare({'k': 'VariableDeclaration', 'symbol': lv, 'ty': 'int', 'w': 32, 'signed': True}, ctx)

    def nested(self, ctx, fl, f, bound=None):
        ctx.nloops.append(fl)
        ctx.nmult.append(bound or self.LOOP)
        try:
            return f()
        finally:
            ctx.nloops.pop()
            ctx.nmult.pop()

    def loop_var(self, ctx, path, name):
        if ctx.mode == 'func':
            nm = f'{ident(name)}_{len(ctx.fdecls)}'
            ctx.fdecls.append((nm, BITS(32, True)))
            v = V(nm, BITS(32, True), lv=True)
        else:
            v = self.var(f'{ctx.prefix}__{name}', BITS(32, True))
        ctx.env[path] = v
        return v

    def const_bound(self, n, ctx):
        """for (i = c0; i < / <= c1; i++) with constants: the iteration count"""
        try:
            lv = (n.get('loopVars') or [None])[0]
            init = None
            if lv is not None and lv.get('init') is not None:
                init = const_int(strip_conv(lv['init']).get('const')) if strip_conv(lv['init']).get('const') else None
            for e in n.get('initializers') or []:
                e = strip_conv(e)
                if e.get('k') == 'Assignment' and strip_conv(e['right']).get('const') is not None:
                    init = const_int(strip_conv(e['right'])['const'])
            st = strip_conv(n.get('stopExpr') or {})
            if init is None or st.get('k') != 'BinaryOp': return None
            r = strip_conv(st['right'])
            hi = const_int(r.get('const')) if r.get('const') is not None else None
            if hi is None: return None
            op = st['op']
            steps = n.get('steps') or []
            if len(steps) != 1: return None
            s = strip_conv(steps[0])
            if not (s.get('k') == 'UnaryOp' and 'increment' in s.get('op', '').lower()): return None
            cnt = {'LessThan': hi - init, 'LessThanEqual': hi - init + 1}.get(op)
            return max(0, cnt) if cnt is not None else None
        except Exception:
            return None

    def stop_bound(self, st, ctx):
        """for (...; i < q.size(); ...) -> QDEPTH; for (...; j < i; ...) with i an enclosing loop's variable
        -> that loop's bound"""
        st = strip_conv(st or {})
        if st.get('k') != 'BinaryOp' or st['op'] not in ('LessThan', 'LessThanEqual', 'GreaterThan', 'GreaterThanEqual'):
            return None
        side = st['right'] if st['op'].startswith('Less') else st['left']
        inner = strip_conv(side)
        while inner.get('k') == 'Conversion': inner = inner['operand']     # int'(r.len): r.len's width bounds it
        if inner is not side and inner.get('w') and inner['w'] <= 6 and not inner.get('signed'):
            return (1 << inner['w']) + 1
        txt = json.dumps(side)
        if '"size"' in txt: return self.QD + 1
        try:
            v = self.ex(side, ctx)
        except Unsupported:
            return None
        ctx.pre = []
        if v.t in self.vbound: return self.vbound[v.t] + 1
        m = re.fullmatch(r"\$signed\(32'\((\w+)\.?(\w*)\)\)", v.t or '')
        if v.ty.kind == 'bits' and v.ty.w <= 4 and not v.ty.s: return (1 << v.ty.w) + 1
        return None

    def size_bound(self, c, ctx):
        """while (q.size() ...) / while (ch.try_get(x)): bounded by the queue depth"""
        s = json.dumps(c)
        if '"size"' in s or 'try_get' in s or 'try_peek' in s or 'pop_front' in s: return self.QD + 1
        return None

    def size_of(self, arr):
        ty = arr.ty
        if ty.kind != 'array': raise Unsupported('size() of a non-array')
        self.read(arr.key, 'n')
        if ty.akind == 'queue': return f'{arr.t}__n'
        if ty.akind == 'assoc': return f'$countones({arr.t}__e)'
        return f'{ty.n}'

    # ------------------------------------------------------------ expression statements
    def expr_stmt(self, e, ctx):
        e = strip_conv(e)
        k = e.get('k')
        if k == 'Assignment':
            return self.assignment(e, ctx)
        if k == 'UnaryOp' and e['op'] in ('Preincrement', 'Postincrement', 'Predecrement', 'Postdecrement'):
            rd_outer, self.rd = self.rd, set()
            try:
                lhs = self.lv(e['operand'], ctx)
                self.read(lhs.key)
            finally:
                reads, self.rd = self.rd, rd_outer
            op = '+' if 'increment' in e['op'] else '-'
            return self.flush(ctx) + self.kept(lhs, reads, f"{lhs.t} = {lhs.t} {op} 1'b1;")
        if k == 'Call':
            return self.call_stmt(e, ctx)
        if k == 'Conversion':
            return self.expr_stmt(e['operand'], ctx)
        raise Unsupported(f'expression statement {k}')

    def assignment(self, e, ctx):
        left = strip_conv(e['left'])
        if e.get('isCompound'):
            rd_outer, self.rd = self.rd, set()
            try:
                lhs = self.lv(left, ctx)
                ctx.env['#lvalue'] = lhs
                self.read(lhs.key)
                rhs = self.ex(e['right'], ctx)
                ctx.env.pop('#lvalue', None)
            finally:
                reads, self.rd = self.rd, rd_outer
            return self.flush(ctx) + self.kept(lhs, reads, self.assign_text(lhs, rhs))
        sig = self.vif_signal(left, ctx)
        if sig is not None:
            rhs = self.ex(e['right'], ctx)
            w = left.get('w') or 1
            self.port(sig, w, 'out')
            if not e.get('isNonBlocking'): self.note(f'blocking write to vif.{sig}: treated as a nonblocking drive')
            return self.flush(ctx) + [f'{sig}__nx = {self.cast_to(rhs, BITS(w)).t};']
        lhs = self.lv(left, ctx)
        if lhs.ty.kind == 'opaque' or lhs.t is None: return self.flush(ctx)
        if lhs.ty.kind == 'bits' and self.test_code():
            hint = f'{self.cur_sub.rsplit("::", 1)[-1]}_{lhs_name(left)}'
            lv_ = self.lift(hint, e['right'], lhs.ty)
            if lv_ is not None: return self.flush(ctx) + self.kept(lhs, set(), self.assign_text(lhs, lv_))
        if lhs.ty.kind == 'obj':
            rhs = self.ex(e['right'], ctx)
            if lhs.inst is not None and lhs.inst.kind == 'seq' and rhs.ty.kind == 'new':
                out = []
                for nm, m in lhs.inst.members.items():
                    if isinstance(m, V) and m.lv: out.append(self.reinit(m))
                return self.flush(ctx) + out
            if rhs.inst is not None and left.get('k') == 'NamedValue':
                ctx.env[left['symbol'].get('path')] = rhs
                return []
            return self.flush(ctx)
        rd_outer, self.rd = self.rd, set()
        ctx.assoc_set = None
        try:
            lhs = self.lv(left, ctx)        # (again: the indices of the target are reads of this statement)
            aset = ctx.assoc_set
            rhs = self.ex(e['right'], ctx)
        finally:
            reads, self.rd = self.rd, rd_outer
        if aset is not None:
            out = self.flush(ctx) + self.kept(lhs, reads, self.assign_text(lhs, rhs))
            return out + ([f"{aset[0]}__e[{aset[1]}] = 1'b1;"] if out else [])
        if rhs.ty.kind == 'opaque': return self.flush(ctx)
        if e.get('isNonBlocking'): self.note('nonblocking assignment to a testbench variable: treated as blocking')
        if lhs.ty.kind == 'array' and rhs.ty.kind == 'array' and lhs.key is not None and rhs.key is not None:
            # a whole-array copy: contents from contents, size from size
            def k_(key, pt): return key + (pt,) if key[0] == 'f' else key
            for pt in ('e', 'n'):
                self.deps.append((k_(lhs.key, pt), set(reads) | {k_(rhs.key, pt)}))
            text = self.assign_text(lhs, rhs)
            de, dn = self.is_dead(lhs.key, 'e'), self.is_dead(lhs.key, 'n')
            if self.live is not None and de and dn: return self.flush(ctx)
            if self.live is not None and de: return self.flush(ctx) + [f'{lhs.t}__n = {rhs.t}__n;']
            return self.flush(ctx) + [text]
        return self.flush(ctx) + self.kept(lhs, reads, self.assign_text(lhs, rhs))

    def kept(self, lhs, reads, text):
        """an assignment, unless its target never reaches anything observable (second pass)"""
        parts = ('e', 'n') if lhs.ty.kind == 'array' else ('e',)
        self.record(lhs, reads, parts)
        if self.live is not None and lhs.key is not None and all(self.is_dead(lhs.key, pt) for pt in parts):
            return []
        if self.live is not None and lhs.ty.kind == 'array' and lhs.key is not None and self.is_dead(lhs.key, 'e'):
            return [f'{lhs.t}__n = {text.split("__n = ")[-1]}'] if '__n = ' in text else []
        return [text]

    def call_stmt(self, e, ctx):
        nm = e.get('subroutineName') or ''
        if nm in DROP_CALLS or nm.startswith('uvm_report') or nm.startswith('$display'): return []
        if e.get('isSystemCall') and nm in ('push_back', 'push_front', 'delete', 'insert', 'pop_front', 'pop_back',
                                             'shuffle', 'sort', 'rsort', 'reverse'):
            return self.queue_method(e, ctx, stmt=True)
        if e.get('isSystemCall') and nm.startswith('$'):
            if nm in ('$cast',):
                a = e['arguments']
                lhs = self.lvalue_arg(a[0], ctx)
                rhs = self.ex(a[1], ctx)
                return self.flush(ctx) + [self.assign_text(lhs, rhs)]
            self.note(f'{nm}() dropped')
            return []
        v = self.call(e, ctx, stmt=True)
        return self.flush(ctx) + ([f'{v.t};'] if v is not None and v.t and v.ty.kind != 'void_done' and v.t.endswith(')') and
                                  not v.t.startswith('(') else [])

    def queue_method(self, e, ctx, stmt):
        nm = e['subroutineName']
        args = e.get('arguments') or []
        q = self.lv(args[0], ctx)
        ty = q.ty
        if ty.kind == 'opaque' and self.live is not None: return self.flush(ctx)     # a queue nothing reads
        if ty.kind != 'array': raise Unsupported(f'{nm}() on {ty!r}')
        n = ty.n
        c = f'{q.t}__n'
        if ty.akind == 'assoc':
            if nm == 'delete':
                if len(args) == 1: return self.flush(ctx) + [f"{q.t}__e = '0;"]
                key = self.ex(args[1], ctx)
                return self.flush(ctx) + [f"{q.t}__e[{key.t}] = 1'b0;"]
            raise Unsupported(f'associative array method {nm}()')
        if ty.akind != 'queue': raise Unsupported(f'{nm}() on a fixed array')
        cwn = cw(n)
        if self.live is not None and q.key is not None and self.is_dead(q.key, 'e') and self.is_dead(q.key, 'n'):
            if nm in ('pop_front', 'pop_back'): self.queue_pop(q, nm, ctx)
            return self.flush(ctx)
        dead_e = self.live is not None and q.key is not None and self.is_dead(q.key, 'e')
        if nm == 'push_back':
            rd_outer, self.rd = self.rd, set()
            try:
                v = self.ex(args[1], ctx)
            finally:
                reads, self.rd = self.rd, rd_outer
            self.record(q, reads, ('e',)); self.record(q, {q.key + ('n',)} if q.key and q.key[0] == 'f' else set(), ('n',))
            v = self.cast_to(v, ty.elem)
            if dead_e: return self.flush(ctx) + [f"if ({c} == {cwn}'d{n}) fv_ovf = 1'b1; else {c} = {c} + 1'b1;"]
            return self.flush(ctx) + [f"if ({c} == {cwn}'d{n}) fv_ovf = 1'b1; else begin {q.t}[{c}] = {v.t}; {c} = {c} + 1'b1; end"]
        if nm == 'push_front':
            v = self.cast_to(self.ex(args[1], ctx), ty.elem)
            return self.flush(ctx) + [f"if ({c} == {cwn}'d{n}) fv_ovf = 1'b1;",
                                      f'else begin for (int j = {n - 1}; j > 0; j--) {q.t}[j] = {q.t}[j - 1]; '
                                      f"{q.t}[0] = {v.t}; {c} = {c} + 1'b1; end"]
        if nm == 'delete':
            if len(args) == 1: return self.flush(ctx) + [f"{c} = '0;"]
            idx = self.ex(args[1], ctx)
            return self.flush(ctx) + [f'if ({idx.t} >= 0 && {idx.t} < $signed(32\'({c}))) begin',
                                      f'  for (int j = 0; j < {n - 1}; j++) if (j >= {idx.t}) {q.t}[j] = {q.t}[j + 1];',
                                      f"  {c} = {c} - 1'b1;", 'end']
        if nm == 'insert':
            idx = self.ex(args[1], ctx)
            v = self.cast_to(self.ex(args[2], ctx), ty.elem)
            return self.flush(ctx) + [f"if ({c} == {cwn}'d{n}) fv_ovf = 1'b1; else begin",
                                      f'  for (int j = {n - 1}; j > 0; j--) if (j > {idx.t}) {q.t}[j] = {q.t}[j - 1];',
                                      f"  {q.t}[{idx.t}] = {v.t}; {c} = {c} + 1'b1;", 'end']
        if nm in ('pop_front', 'pop_back'):
            r = self.queue_pop(q, nm, ctx)
            return self.flush(ctx)
        if nm in ('shuffle',):
            self.note('queue shuffle() not modelled (order kept)')
            return []
        raise Unsupported(f'queue method {nm}()')

    def queue_pop(self, q, nm, ctx):
        ty, n, c = q.ty, q.ty.n, f'{q.t}__n'
        if ty.elem.kind == 'none' or (self.live is not None and q.key is not None and self.is_dead(q.key, 'e')):
            if not (self.live is not None and q.key is not None and self.is_dead(q.key, 'n')):
                ctx.pre.append(f"if ({c} != 0) {c} = {c} - 1'b1;")
            return V("'0", ty.elem if ty.elem.kind != 'none' else BITS(1))
        self.read(q.key, 'e'); self.read(q.key, 'n')
        t = self.tmp(ctx, ty.elem, 'pop')
        if nm == 'pop_front':
            ctx.pre += [f'{t.t} = {q.t}[0];',
                        f'for (int j = 0; j < {n - 1}; j++) {q.t}[j] = {q.t}[j + 1];',
                        f"if ({c} != 0) {c} = {c} - 1'b1;"]
        else:
            ctx.pre += [f"{t.t} = {q.t}[({c} == 0) ? 0 : {c} - 1'b1];", f"if ({c} != 0) {c} = {c} - 1'b1;"]
        if ctx.cond_depth: raise Unsupported(f'{nm}() inside a condition operand')
        return t

    # ------------------------------------------------------------ calls in expressions
    def call(self, e, ctx, stmt=False):
        nm = e.get('subroutineName') or ''
        args = e.get('arguments') or []
        if nm == 'randomize' and (e.get('thisClass') is not None or e.get('arguments')):   # obj.randomize() [with {...}]
            return self.randomize_call(e, ctx)
        if e.get('isSystemCall'):
            return self.syscall(e, ctx)
        if nm in DROP_CALLS: return V("1'b0", BITS(1))
        if nm in ('randomize',):
            return self.randomize_call(e, ctx)
        if nm in ('uvm_report_enabled',): return V("1'b1", BITS(1))
        obj = None
        if e.get('thisClass') is not None:
            obj = self.ex(e['thisClass'], ctx)
        if nm == 'create' and e.get('cty'):
            return V('', Ty('new'))
        if obj is not None and obj.inst is not None:
            inst = obj.inst
            if inst.kind == 'chan':
                return self.chan_call(inst, nm, args, ctx)
            if inst.kind == 'port':
                return self.port_call(inst, nm, args, ctx)
            if inst.kind == 'ext':
                return self.ext_call(inst, e, ctx)
            if inst.kind == 'vif':
                raise Unsupported(f'interface method {nm}()')
            sub = self.D.method(inst.cls, nm) if inst.cls else None
            if sub is None: raise Unsupported(f'{inst.name}.{nm}(): no body')
            return self.fcall(self.comp_method(inst, sub), sub, args, ctx)
        if obj is not None and obj.ty.kind == 'struct':
            sub = self.D.method(obj.ty.cls, nm)
            if sub is None or sub['path'].startswith('uvm_pkg::'):
                if nm in ('set_id_info', 'get_type_name', 'convert2string', 'print', 'sprint'): return V("1'b0", BITS(1))
                raise Unsupported(f'{nm}() of {obj.ty.cls}: no body')
            if (sub.get('returns') or 'void') != 'void': raise Unsupported(f'{nm}(): value-class method with a result')
            fn = self.value_method(obj.ty.cls, sub)
            fa = [self.ex(self.arg_expr(a), ctx).t for a in args]
            if not obj.lv: raise Unsupported(f'{nm}() on a temporary object')
            ctx.pre.append(f'{obj.t} = {fn}({", ".join([obj.t] + fa)});')
            return V("1'b0", BITS(1), lv=False) if not stmt else None
        if obj is None:
            this = ctx.this
            sp = (e.get('subroutine') or {}).get('path')
            if isinstance(this, V) and this.ty.kind == 'struct' and self.D.method(this.ty.cls, nm) is None:
                other = self.D.subs.get(sp) if sp else None      # a static / package function (axi_utils::f)
                if other is None or other.get('body') is None: raise Unsupported(f'{nm}(): no body')
                return self.fcall(self.pkg_func(other), other, args, ctx)
            if isinstance(this, V) and this.ty.kind == 'struct':
                sub = self.D.method(this.ty.cls, nm)
                fn = self.value_method(this.ty.cls, sub)
                fa = [self.ex(self.arg_expr(a), ctx).t for a in args]
                ctx.pre.append(f'{this.t} = {fn}({", ".join([this.t] + fa)});')
                return None
            sp = (e.get('subroutine') or {}).get('path')
            sub = self.D.method(this.cls, nm) if isinstance(this, Inst) and this.cls else None
            sub = sub or (self.D.subs.get(sp) if sp else None)
            if sub is None or sub.get('body') is None:
                raise Unsupported(f'{nm}(): no body')
            if self.has_wait(sub['body']): raise Unsupported(f'{nm}(): a task with waits used in an expression')
            if sub.get('owner_is_class') and isinstance(this, Inst):
                return self.fcall(self.comp_method(this, sub), sub, args, ctx)
            return self.fcall(self.pkg_func(sub), sub, args, ctx)
        raise Unsupported(f'call {nm}()')

    def pkg_func(self, sub):
        """a package / static function: an SV function"""
        key = ('pf', sub['path'])
        if key in self.funcs: return self.funcs[key]
        inst = Inst('comp', ident(sub['path']), None)
        nm = self.comp_method(inst, sub)
        self.funcs[key] = nm
        return nm

    def fcall(self, fn, sub, args, ctx):
        saved, self.rd = self.rd, None
        try:
            return self._fcall(fn, sub, args, ctx)
        finally:
            self.rd = saved

    def _fcall(self, fn, sub, args, ctx):
        fa = []
        formals = sub.get('args') or []
        for i, f in enumerate(formals):
            a = args[i] if i < len(args) else None
            d = (f.get('dir') or f.get('direction') or 'In').lower()
            if a is None or strip_conv(a).get('k') == 'EmptyArgument':
                if f.get('default_ast') is not None: fa.append(self.ex(f['default_ast'], ctx).t); continue
                raise Unsupported(f'{sub["name"]}(): argument {f["name"]} missing')
            if d in ('out', 'output', 'inout', 'ref'):
                fa.append(self.lvalue_arg(a, ctx).t)
            else:
                fty = self.ty_ti(f.get('ti'), f['name']) if f.get('ti') else None
                fa.append(self.cast_to(self.ex(self.arg_expr(a), ctx), fty).t)
        rt = sub.get('returns') or 'void'
        if rt == 'void': return V(f'{fn}({", ".join(fa)})', Ty('bits', 1))
        rty = self.ty_ti(sub.get('ret_ti'), sub['name']) if sub.get('ret_ti') else self.ty_str(rt, sub.get('ret_w'), sub.get('ret_signed'))
        return V(f'{fn}({", ".join(fa)})', rty)

    def chan_call(self, inst, nm, args, ctx):
        ch = self.chan(inst)
        self.touch('w' if nm in ('try_put', 'write', 'flush') else 'r', 'ch', ch)
        if nm in ('try_get', 'try_peek'):
            lhs = self.lvalue_arg(args[0], ctx)
            return V(f'{ch}__{nm}({lhs.t})', BITS(1))
        if nm in ('try_put', 'write'):
            v = self.ex(self.arg_expr(args[0]), ctx)
            if nm == 'write':
                ctx.pre.append(f'{ch}__push({v.t});'); return None
            return V(f"(({ch}__n < {self.QD}) ? ({ch}__push({v.t}), 1'b1) : 1'b0)", BITS(1))
        if nm in ('used', 'size', 'num'): return V(f"32'({ch}__n)", BITS(32, nm == 'num'))
        if nm == 'is_empty': return V(f'({ch}__n == 0)', BITS(1))
        if nm == 'is_full': return V(f"({ch}__n == {self.QD})" if not inst.bound else f'({ch}__n == {inst.bound})', BITS(1))
        if nm == 'flush':
            ctx.pre.append(f"{ch}__n = '0;"); return None
        raise Unsupported(f'channel method {nm}()')

    def port_call(self, inst, nm, args, ctx):
        if nm == 'write':
            v = self.ex(self.arg_expr(args[0]), ctx)
            for t in self.analysis_targets(inst):
                ctx.pre.append(f'{self.chan(t)}__push({v.t});')
                self.touch('w', 'ch', self.chan(t))
            return None
        if nm in ('item_done', 'put_response', 'put'):
            sq = self.port_sqr(inst)
            pr = self.proto(sq)
            if nm == 'item_done':
                ctx.pre += [f"{pr['done']} = 1'b1;", f"{pr['gnt']} = 1'b0;"]
                self.touch('w', 'done', sq.name)
                if args and strip_conv(args[0]).get('k') not in ('EmptyArgument', 'NullLiteral', None):
                    nm = 'put_response'; args = args[:1]
                else:
                    return None
            if nm in ('put_response', 'put'):
                if pr.get('free'):
                    self.note(f'{sq.name}: responses to the free source are dropped')
                    return None
                v = self.ex(self.arg_expr(args[0]), ctx)
                ctx.pre.append(f'{pr["rsp"]}__push({v.t});')
                self.touch('w', 'ch', pr['rsp'])
                return None
        if nm in ('try_next_item', 'has_do_available'):
            raise Unsupported(f'{nm}() (non-blocking item fetch) is not modelled')
        if nm in ('connect',): return None
        raise Unsupported(f'port method {nm}()')

    def ext_call(self, inst, e, ctx):
        nm = e.get('subroutineName')
        w, s = e.get('w'), e.get('signed')
        if w:
            sid = self.site(w, s, f'{inst.cls.split("::")[-1]}::{nm}() at {self.where()}: any value '
                                  '(object outside the agent)', ctx, meta={'kind': 'external', 'class': inst.cls, 'method': nm})
            self.note(f'{inst.cls.split("::")[-1]}::{nm}(): outside the agent -- returns a free value')
            return V(f"{sid}(1'b1)", BITS(w, s))
        self.note(f'{inst.cls.split("::")[-1]}::{nm}(): outside the agent -- call dropped')
        return None

    def randomize_call(self, e, ctx):
        obj = None
        args = e.get('arguments') or []
        if e.get('thisClass') is not None: obj = self.ex(e['thisClass'], ctx)
        elif e.get('isSystemCall') and args:            # slang: obj.randomize() as randomize(obj)
            obj = self.ex(args[0], ctx); args = args[1:]
        elif isinstance(ctx.this, V): obj = ctx.this
        if obj is None or obj.ty.kind != 'struct':
            raise Unsupported('randomize() of something other than a sequence item / value object')
        withc = (e.get('inlineConstraints') or e.get('constraints') or e.get('with') or
                 ((e.get('subroutine') or {}).get('extraInfo') or {}).get('inlineConstraints'))
        items = self.items_of(withc) if isinstance(withc, dict) else (withc or [])
        only = None
        if args:
            only = set()
            for a in args:
                a = strip_conv(a)
                if a.get('k') == 'MemberAccess': only.add(a['member']['sym'])
                elif a.get('k') == 'NamedValue': only.add(a['symbol']['sym'])
        if ctx.cond_depth: raise Unsupported('randomize() inside a condition operand')
        ctx.pre += self.randomize_lines(obj, obj.ty.cls, items, ctx, only=only)
        return V("1'b1", BITS(1))

    def syscall(self, e, ctx):
        nm = e['subroutineName']
        args = e.get('arguments') or []
        if nm in ('$urandom_range', '$urandom', '$random'): return self.urandom(e, ctx)
        if nm == 'size' or nm == 'num':
            a = self.ex(args[0], ctx)
            if a.ty.kind == 'array': return V(f"$signed(32'({self.size_of(a)}))", BITS(32, True))
            raise Unsupported('size() of a non-array')
        if nm == 'exists':
            a = self.ex(args[0], ctx)
            key = self.ex(args[1], ctx)
            if a.ty.kind == 'array' and a.ty.akind == 'assoc': return V(f'{a.t}__e[{key.t}]', BITS(1))
            raise Unsupported('exists() on a non-associative array')
        if nm in ('pop_front', 'pop_back'):
            q = self.lv(args[0], ctx)
            return self.queue_pop(q, nm, ctx)
        if nm in ('$time', '$realtime', '$stime'):
            self.note(f'{nm}: the model counts clock cycles')
            return V('fv_cycle', BITS(64))
        if nm in ('$clog2', '$countones', '$onehot', '$onehot0', '$bits', '$signed', '$unsigned', '$countbits',
                  '$isunknown'):
            a = [self.ex(x, ctx).t for x in args]
            if nm == '$isunknown': return V("1'b0", BITS(1))
            return V(f'{nm}({", ".join(a)})', BITS(e.get('w') or 32, e.get('signed')))
        if nm in ('$sformatf', '$psprintf'): return V(None, OPAQUE)
        raise Unsupported(f'system call {nm}()')

    # ------------------------------------------------------------ names
    def vif_signal(self, e, ctx):
        """vif.sig (through any handle chain to the agent's virtual interface) -> 'sig', else None"""
        e = strip_conv(e)
        if e.get('k') != 'MemberAccess': return None
        mem = e['member']
        try:
            base = self.ex(e['value'], ctx)
        except Unsupported:
            return None
        if base.inst is not None and base.inst.kind == 'vif':
            nm = mem['sym']
            return nm
        if base.inst is not None and base.inst.kind == 'cb':
            return mem['sym']
        return None

    def port(self, sig, w, d):
        p = self.ports.get(sig)
        if p is None:
            init = None
            for iface, sigs in self.iface_inits.items():
                if sig in sigs: init = sigs[sig]
            self.ports[sig] = {'w': w, 'dir': d, 'init': init}
        else:
            if d == 'out': p['dir'] = 'out'
            p['w'] = max(p['w'], w)

    def resolve_sym(self, n, ctx):
        sym = n['symbol']
        path = sym.get('path') or sym['sym']
        if path in ctx.env: return ctx.env[path]
        sk = sym.get('skind')
        if n.get('const') is not None and sk in ('Parameter', 'EnumValue', 'Specparam'):
            return V(sv_const(n['const'], n.get('w'), n.get('signed')), BITS(n.get('w') or 32, n.get('signed')))
        if sk in ('ClassProperty',):
            this = ctx.this
            nm = sym['sym']
            owner = path.rsplit('::', 1)[0] if '::' in path else None
            if (isinstance(this, V) and this.ty.kind == 'struct' and owner and self.D.cpath(owner)
                    and self.D.cpath(owner) not in self.D.bases(this.ty.cls) and getattr(ctx, 'outer', None) is not None):
                # randomize() with {...}: a name that is not the object's resolves in the calling scope
                c2 = ctx.sub(this=ctx.outer)
                c2.env = ctx.env
                return self.resolve_sym(n, c2)
            if isinstance(this, Inst):
                if this.kind == 'ext': return self.member_of(this, nm)
                m = this.members.get(nm)
                if m is not None: return m if isinstance(m, V) else V(None, Ty('obj', cls=m.cls), inst=m)
                raise Unsupported(f'{this.name}.{nm}: member not modelled')
            if isinstance(this, V) and this.ty.kind == 'struct':
                fty = self.field_ty(this.ty.cls, nm)
                if fty is None and self.live is not None and self.is_pruned(this.ty.cls, nm):
                    return V("'0", BITS(n.get('w') or 1, n.get('signed')))
                if fty is None: raise Unsupported(f'{this.ty.cls}.{nm}: field not modelled')
                return V(f'{this.t}.{nm}', fty, lv=True, key=('f', this.ty.cls, nm))
        if sk == 'Subroutine' or sk == 'Variable':
            # the return variable of the function being translated (F = v;)
            if path in ctx.env: return ctx.env[path]
        raise Unsupported(f'name {sym["sym"]} ({sk}) not modelled')

    def member_of(self, inst, nm):
        if inst.kind == 'ext':
            m = self.ext_member(inst, nm)
        else:
            m = inst.members.get(nm)
            if m is None: raise Unsupported(f'{inst.name}.{nm}: member not modelled')
        if isinstance(m, Inst): return V(None, Ty('obj', cls=m.cls), inst=m)
        return m

    def lv(self, n, ctx):
        n = strip_conv(n)
        saved = getattr(ctx, 'lvm', False)
        ctx.lvm = True
        try:
            v = self.ex(n, ctx)
        finally:
            ctx.lvm = saved
        if not v.lv and v.inst is None and v.ty.kind != 'opaque':
            raise Unsupported(f'assignment target {n.get("k")}: {n.get("src", "")[:40]}')
        return v

    # ------------------------------------------------------------ expressions
    def cond(self, n, ctx):
        v = self.ex(n, ctx)
        if v.ty.kind != 'bits': raise Unsupported('condition on a non-integral value')
        return v.t if v.ty.w == 1 else f"({v.t} != {v.ty.w}'d0)"

    BIN = {'Add': '+', 'Subtract': '-', 'Multiply': '*', 'Divide': '/', 'Mod': '%', 'BinaryAnd': '&',
           'BinaryOr': '|', 'BinaryXor': '^', 'BinaryXnor': '~^', 'Equality': '==', 'Inequality': '!=',
           'CaseEquality': '==', 'CaseInequality': '!=', 'GreaterThanEqual': '>=', 'GreaterThan': '>',
           'LessThanEqual': '<=', 'LessThan': '<', 'WildcardEquality': '==?', 'WildcardInequality': '!=?',
           'LogicalAnd': '&&', 'LogicalOr': '||', 'LogicalShiftLeft': '<<', 'LogicalShiftRight': '>>',
           'ArithmeticShiftLeft': '<<<', 'ArithmeticShiftRight': '>>>', 'Power': '**'}
    UN = {'Plus': '+', 'Minus': '-', 'BitwiseNot': '~', 'BitwiseAnd': '&', 'BitwiseOr': '|', 'BitwiseXor': '^',
          'BitwiseNand': '~&', 'BitwiseNor': '~|', 'BitwiseXnor': '~^', 'LogicalNot': '!'}

    def ex(self, n, ctx):
        k = n.get('k')
        w, s = n.get('w'), bool(n.get('signed'))
        if n.get('const') is not None and k not in ('NamedValue', 'MemberAccess', 'Call', 'Assignment') and w:
            if not str(n['const']).startswith('"'):
                return V(sv_const(n['const'], w, s), BITS(w, s))
        lvm = getattr(ctx, 'lvm', False)
        if k == 'NamedValue':
            v = self.resolve_sym(n, ctx)
            # a read of the whole variable (a struct / array copied: finish_item(it), x = y) keeps its
            # assignments, the reset of a create() included
            if not lvm and v.key is not None and v.ty.kind in ('bits', 'struct', 'array'): self.read(v.key)
            return v
        if k == 'MemberAccess':
            base = self.ex(n['value'], ctx)
            nm = n['member']['sym']
            if base.inst is not None:
                inst = base.inst
                if inst.kind == 'vif':
                    self.port(nm, w or 1, 'in')
                    return V(nm, BITS(w or 1, s))
                return self.member_of(inst, nm)
            if base.ty.kind == 'struct':
                fty = self.field_ty(base.ty.cls, nm)
                if fty is None and self.live is not None and self.is_pruned(base.ty.cls, nm):
                    if lvm: return V(None, OPAQUE, lv=True)  # a write nothing observable reads: dropped
                    return V("'0", BITS(w or 1, s))          # a field nothing observable reads (dead code)
                if fty is None: raise Unsupported(f'{base.ty.cls.split("::")[-1]}.{nm}: field not modelled')
                v = V(f'{base.t}.{nm}', fty, lv=base.lv, key=('f', base.ty.cls, nm))
                if not lvm:
                    self.read(base.key)                 # reading x.f reads x (its copies stay live)
                    if fty.kind == 'bits': self.read(v.key)
                return v
            if base.ty.kind == 'bits':
                return V(f'{base.t}.{nm}', BITS(w or 1, s), lv=base.lv)      # packed struct member
            raise Unsupported(f'member {nm} of {base.ty!r}')
        if k == 'ElementSelect':
            base = self.ex(n['value'], ctx)
            ctx.lvm = False
            try:
                idx = self.ex(n['selector'], ctx)
            finally:
                ctx.lvm = lvm
            if not lvm: self.read(base.key)
            if base.ty.kind == 'array':
                et = base.ty.elem
                it = idx.t
                if lvm and base.ty.akind == 'assoc': ctx.assoc_set = (base.t, idx.t)
                if base.ty.akind == 'fixed' and base.ty.lo: it = f'(({it}) - {base.ty.lo})'
                t = f'{base.t}[{it}]'
                if et.kind == 'bits' and et.s and not lvm: return V(f'$signed({t})', et, lv=False, key=base.key)
                return V(t, et, lv=base.lv, key=base.key)
            if base.ty.kind == 'bits':
                return V(f'{base.t}[{idx.t}]', BITS(1), lv=base.lv, key=base.key)
            raise Unsupported(f'element select of {base.ty!r}')
        if k == 'RangeSelect':
            base = self.ex(n['value'], ctx)
            l, r = self.ex(n['left'], ctx), self.ex(n['right'], ctx)
            sk = n.get('selectionKind', 'Simple')
            op = {'Simple': ':', 'IndexedUp': '+:', 'IndexedDown': '-:'}.get(sk, ':')
            if base.ty.kind != 'bits': raise Unsupported('range select of a non-integral value')
            return V(f'{base.t}[{l.t}{op}{r.t}]', BITS(w or 1, False), lv=base.lv, key=base.key)
        if k == 'BinaryOp':
            op = n['op']
            if op in ('LogicalAnd', 'LogicalOr'):
                l = self.ex(n['left'], ctx)
                ctx.cond_depth += 1
                try:
                    r = self.ex(n['right'], ctx)
                finally:
                    ctx.cond_depth -= 1
                return V(f'({self.b(l)} {self.BIN[op]} {self.b(r)})', BITS(1))
            if op == 'LogicalImplication':
                l, r = self.ex(n['left'], ctx), self.ex(n['right'], ctx)
                return V(f'(!{self.b(l)} || {self.b(r)})', BITS(1))
            if op == 'LogicalEquivalence':
                l, r = self.ex(n['left'], ctx), self.ex(n['right'], ctx)
                return V(f'({self.b(l)} == {self.b(r)})', BITS(1))
            l, r = self.ex(n['left'], ctx), self.ex(n['right'], ctx)
            if l.ty.kind == 'struct' or r.ty.kind == 'struct':
                if op in ('Equality', 'Inequality', 'CaseEquality', 'CaseInequality'):
                    raise Unsupported('object handle comparison')
            if op not in self.BIN: raise Unsupported(f'operator {op}')
            if op in ('CaseEquality', 'CaseInequality'): self.note('=== / !== compare two-valued values')
            return V(f'({l.t} {self.BIN[op]} {r.t})', BITS(w or 1, s))
        if k == 'UnaryOp':
            op = n['op']
            if op in self.UN:
                x = self.ex(n['operand'], ctx)
                return V(f'({self.UN[op]}{x.t})', BITS(w or 1, s))
            if op in ('Preincrement', 'Postincrement', 'Predecrement', 'Postdecrement'):
                raise Unsupported('++/-- inside an expression')
            raise Unsupported(f'operator {op}')
        if k == 'ConditionalOp':
            conds = n.get('conditions') or []
            if len(conds) != 1: raise Unsupported('conditional operator with several conditions')
            c = self.cond(conds[0]['expr'], ctx)
            pre0, ctx.pre = ctx.pre, []
            ctx.cond_depth += 1
            try:
                a = self.ex(n['left'], ctx); pa, ctx.pre = ctx.pre, []
                b_ = self.ex(n['right'], ctx); pb = ctx.pre
            finally:
                ctx.cond_depth -= 1
                ctx.pre = pre0
            if pa or pb:     # c ? $urandom_range(..) : ..: only the operand chosen is evaluated (its draws happen)
                ctx.pre += [f'if ({c}) begin', *ind(pa), 'end else begin', *ind(pb), 'end']
            ty = BITS(w, s) if w else a.ty
            return V(f'({c} ? {a.t} : {b_.t})', ty)
        if k == 'Conversion':
            x = self.ex(n['operand'], ctx)
            if not w or x.ty.kind != 'bits':
                return x
            t = x.t
            if x.ty.w != w: t = f"{w}'({t})"
            if x.ty.s != s or x.ty.w != w: t = f'$signed({t})' if s else f'$unsigned({t})'
            return V(t, BITS(w, s))
        if k in ('IntegerLiteral', 'UnbasedUnsizedIntegerLiteral'):
            return V(sv_const(n.get('const', '0'), w, s), BITS(w or 32, s))
        if k == 'Concatenation':
            ops = [self.ex(o, ctx).t for o in n.get('operands') or []]
            return V('{' + ', '.join(ops) + '}', BITS(w or 1, False))
        if k == 'Replication':
            c = self.ex(n['count'], ctx)
            x = self.ex(n['concat'], ctx)
            return V('{' + c.t + '{' + x.t + '}}', BITS(w or 1, False))
        if k == 'Inside':
            x = self.ex(n['left'], ctx)
            terms = [self.inside_term(x, r, ctx) for r in n.get('rangeList') or []]
            return V('(' + (' || '.join(terms) or "1'b0") + ')', BITS(1))
        if k == 'Call':
            v = self.call(n, ctx)
            if v is None: return V("1'b0", BITS(1))
            return v
        if k == 'NewClass':
            return V('', Ty('new'))
        if k == 'Assignment' and n.get('isLValueArg'):
            return self.lv(n['left'], ctx)
        if k == 'LValueReference':
            v = ctx.env.get('#lvalue')
            if v is None: raise Unsupported('compound assignment target')
            return v
        if k == 'StringLiteral':
            return V(None, OPAQUE)
        if k == 'NullLiteral':
            return V(None, OPAQUE)
        if k == 'SimpleAssignmentPattern' or k == 'ReplicatedAssignmentPattern' or k == 'StructuredAssignmentPattern':
            if not (n.get('elements')): return V('', Ty('empty'))
        if k == 'EmptyArgument':
            return V(None, OPAQUE)
        raise Unsupported(f'expression {k}: {(n.get("src") or "")[:50]}')

    def inside_term(self, x, r, ctx):
        r = strip_conv(r)
        if r.get('k') == 'ValueRange':
            lo, hi = self.ex(r['left'], ctx), self.ex(r['right'], ctx)
            return f'({x.t} >= {lo.t} && {x.t} <= {hi.t})'
        v = self.ex(r, ctx)
        if v.ty.kind == 'array':
            return f'(|{{' + ', '.join(f'({i} < {self.size_of(v)} && {v.t}[{i}] == {x.t})' for i in range(v.ty.n)) + '})'
        return f'({x.t} == {v.t})'

    def b(self, v):
        if v.ty.kind != 'bits': raise Unsupported('logical operator on a non-integral value')
        return v.t if v.ty.w == 1 else f"({v.t} != {v.ty.w}'d0)"

    # ------------------------------------------------------------ code generation
    # ------------------------------------------------------------ RTL: state machines, one evaluation per edge
    KIND = {'clk': 'CLK', 'rep': 'DELAY', 'until': 'UNTIL', 'chan': 'GET', 'gni': 'GET_ITEM',
            'start_item': 'START_ITEM', 'finish_item': 'FINISH_ITEM', 'join': 'JOIN'}
    ZERO_TIME = ('chan', 'gni', 'start_item', 'finish_item', 'join')

    def states(self):
        for p in self.procs:
            used = set()
            for n in p.nodes:
                if n.kind != 'wait': continue
                kind = self.KIND.get(n.wk, n.wk.upper())
                if n.wk == 'chan' and n.desc.endswith('put()'): kind = 'PUT'
                base = f'L{n.line}_{kind}' if n.line else kind
                nm, k = base, 1
                while nm in used: k += 1; nm = f'{base}_{k}'
                used.add(nm)
                n.sname = nm
                n.state = f'{p.name}__{nm}'
            p.st_idle, p.st_entry, p.st_end = f'{p.name}__IDLE', f'{p.name}__ENTRY', f'{p.name}__END'
            p.nstates = sum(1 for n in p.nodes if n.kind == 'wait') + 3
            p.sw = iw(p.nstates)

    def fw(self, n):
        seen = 0
        while n is not None and n.kind == 'code' and not n.lines and n.nxt is not None and seen < 10000:
            n = n.nxt; seen += 1
        return n

    def succ(self, n):
        """zero-time successors: what runs in the same edge after n"""
        fw = self.fw
        if n.kind in ('code', 'fork'): return [fw(n.nxt)] if n.nxt is not None else []
        if n.kind == 'br': return [fw(x) for x in (n.t, n.f) if x is not None]
        if n.kind == 'wait' and (n.wk == 'rep' or n.imm): return [fw(n.nxt or n.g_end)]
        return []

    def immediate(self, p):
        """a blocking call returns at once when it can (get_next_item with an item there, start_item with the
        driver asking, a get from a non-empty channel): checked when reached, unless that closes a loop with no
        wait in it (then at the process's next pass)"""
        for n in p.nodes: n.g_end = p.end
        prio = {'start_item': 0, 'gni': 1, 'chan': 2, 'join': 3, 'finish_item': 4}
        for n in sorted((n for n in p.nodes if n.kind == 'wait' and n.wk in prio), key=lambda n: (prio[n.wk], n.id)):
            tgt = self.fw(n.nxt or p.end)
            seen, stack = set(), [tgt]
            while stack:
                m = stack.pop()
                if m is None or m.id in seen: continue
                seen.add(m.id); stack.extend(self.succ(m))
            if n.id not in seen: n.imm = True

    def topo(self, starts):
        reach, seen, stack = [], set(), list(starts)
        while stack:
            n = stack.pop()
            if n.id in seen: continue
            seen.add(n.id); reach.append(n)
            stack.extend(self.succ(n))
        indeg = {n.id: 0 for n in reach}
        for n in reach:
            for s_ in self.succ(n): indeg[s_.id] += 1
        byid = {n.id: n for n in reach}
        order, ready = [], sorted([i for i, d in indeg.items() if d == 0])
        while ready:
            i = ready.pop(0)
            order.append(byid[i])
            for s_ in self.succ(byid[i]):
                indeg[s_.id] -= 1
                if indeg[s_.id] == 0: ready.append(s_.id)
        if len(order) == len(reach): return order, False
        post, vis = [], set()       # a loop with no wait: reverse DFS postorder, back edges iterate (UNROLL)

        def dfs(n):
            vis.add(n.id)
            for s_ in self.succ(n):
                if s_.id not in vis: dfs(s_)
            post.append(n)
        for st in starts:
            if st.id not in vis: dfs(st)
        return post[::-1], True

    def node_code(self, p, n, a, ind_, entry):
        fw = self.fw
        A = lambda m: a(fw(m))
        out = []
        if n.kind == 'code':
            out += [ind_ + x for l in n.lines for x in l.split('\n')]
            out.append(f"{ind_}{A(n.nxt)} = 1'b1;")
        elif n.kind == 'br':
            out.append(f"{ind_}if ({n.cond}) {A(n.t)} = 1'b1; else {A(n.f)} = 1'b1;")
        elif n.kind == 'fork':
            for c in n.children:
                out.append(f'{ind_}{c.pc} = {c.st_entry};')
                # forked at time 0: takes the first edge; forked after an edge: waits for the next one
                took = f'fv_ck[{p.id}]' if entry else "1'b0"
                out.append(f'{ind_}fv_ck[{c.id}] = {took};')
            out.append(f"{ind_}{A(n.nxt)} = 1'b1;")
        elif n.kind == 'end':
            out.append(f'{ind_}{p.pc} = {p.st_end};')
        elif n.kind == 'wait':
            out += [ind_ + x for x in n.enter]
            if not entry: out += [ind_ + x for x in n.consume]
            nxt = n.nxt or p.end
            if n.wk == 'rep':
                c, ty = n.cnt.t, n.cnt.ty
                cwid = self.vars[p.cnt][0].w
                mx = (1 << cwid) - 1
                zero = f'$signed({c}) <= 0' if ty.s else f'{c} == 0'
                ovf = ([f"{ind_}if ({'$signed(' + c + ') > ' + str(mx) if ty.s else c + ' > ' + str(mx)}) fv_ovf = 1'b1;   // CNT_W too small"]
                       if cwid < 32 else [])
                out += [f"{ind_}{p.cnt} = {cwid}'({c});", *ovf,
                        f"{ind_}if ({zero}) {A(nxt)} = 1'b1; else {p.pc} = {n.state};"]
            elif n.imm:
                out += [f'{ind_}if ({n.ready}) begin', *[f'{ind_}  {x}' for x in n.resume],
                        f"{ind_}  {A(nxt)} = 1'b1;", f'{ind_}end else {p.pc} = {n.state};']
            else:
                out.append(f'{ind_}{p.pc} = {n.state};')
        return out

    def flag(self, p, n, entry):
        """the signal 'process p runs node n in this edge': <proc>__L<source line>[_k] (__t0_: at time 0)"""
        key = (p.id, n.id, entry)
        if key not in self.flags:
            base = f'{p.name}__{"t0_" if entry else ""}L{n.line or 0}'
            nm, k = base, 1
            while nm in self.flag_names: k += 1; nm = f'{base}_{k}'
            self.flag_names.add(nm)
            self.flags[key] = nm
        return self.flags[key]

    def graph_code(self, p, order, cyclic, entry, I):
        pos = {n.id: i for i, n in enumerate(order)}
        a = lambda n: self.flag(p, n, entry)
        b = (lambda n: a(n) + '__again') if cyclic else a
        for n in order:
            if a(n) not in self.act_set: self.act_set.add(a(n)); self.act_vars.append(a(n))
            if cyclic and b(n) not in self.act_set: self.act_set.add(b(n)); self.act_vars.append(b(n))
        body = []
        for n in order:
            act = (lambda m, src=n: a(m) if pos[m.id] > pos[src.id] else b(m)) if cyclic else a
            d = f'  // {n.desc}' if n.desc and n.kind != 'code' else ''
            body += [f'{I}if ({a(n)}) begin{d}', *self.node_code(p, n, act, I + '  ', entry), f'{I}end']
        if not cyclic: return body
        return ([f'{I}for (int u = 0; u < {self.UNROLL}; u++) begin', f'{I}  ' + ' '.join(f"{b(n)} = 1'b0;" for n in order)]
                + ['  ' + x for x in body]
                + [f'{I}  ' + ' '.join(f'{a(n)} = {b(n)};' for n in order), f'{I}end',
                   f"{I}if ({' || '.join(a(n) for n in order)}) fv_ovf = 1'b1;   // a loop with no wait ran past UNROLL"])

    def proc_code(self, p, k, gate=None):
        """pass k of process p in this edge's evaluation: time 0 (ENTRY: the code before its first wait), then
        the state it waits in, if that wait is over: the code up to its next wait"""
        fw = self.fw
        I = '    '
        out = [f'    // ---- {p.name} ({p.desc}){f", pass {k + 1}" if k else ""}'
               f'{f" -- runs here when {gate}" if gate else ""}']
        eorder, ecyc = self.topo([fw(p.entry or p.end)])
        starts = [fw(n.nxt or p.end) for n in p.nodes if n.kind == 'wait']
        morder, mcyc = self.topo(starts)
        flags = [self.flag(p, n, True) for n in eorder] + [self.flag(p, n, False) for n in morder]
        if flags: out.append(f'{I}' + ' '.join(f"{x} = 1'b0;" for x in flags))
        if eorder and (p.top or p.parent is not None):
            out.append(f"{I}if ({p.pc} == {p.st_entry}) begin")
            out.append(f"{I}  {self.flag(p, fw(p.entry or p.end), True)} = 1'b1;")
            out += ['  ' + x for x in self.graph_code(p, eorder, ecyc, True, I)]
            out.append(f'{I}end')
        cases = []
        for n in p.nodes:
            if n.kind != 'wait': continue
            go = f"{self.flag(p, fw(n.nxt or p.end), False)} = 1'b1;"
            if n.wk == 'rep':
                body = [f"if ({p.cnt} == {self.vars[p.cnt][0].w}'d1) {go} else {p.cnt} = {p.cnt} - 1'b1;"]
            elif n.wk == 'until':
                body = [f'if ({n.cond}) {go}']
            else:
                body = [go]
            cases += [f'{I}  {n.state}: if ({n.ready}) begin   // {n.desc}', *[f'{I}    {x}' for x in n.resume + body],
                      f'{I}  end']
        if cases:
            out += [f'{I}case ({p.pc})', *cases, f'{I}  default: ;', f'{I}endcase']
            out += self.graph_code(p, morder, mcyc, False, I)
        if gate: out = [out[0], f'    if ({gate}) begin', *['  ' + x for x in out[1:]], '    end']
        return out

    def can_end(self, p):
        seen, stack = set(), [p.entry or p.end]
        while stack:
            n = stack.pop()
            if n is None or n.id in seen: continue
            if n is p.end: return True
            seen.add(n.id)
            stack += [n.nxt, n.t, n.f] if n.kind in ('br', 'code', 'fork', 'wait') else []
        return False

    def schedule(self):
        """the order processes run in at an edge: whoever puts into a channel / hands over an item before whoever
        takes it; processes that hand work back and forth in one edge (driver <-> sequence) run SYNC passes"""
        W, R = {}, {}
        for p in self.procs:
            if not self.can_end(p): p.acc.discard(('w', 'end', p.name))     # forever: its join never wakes anyone
            for acc in p.acc:
                (W if acc[0] == 'w' else R).setdefault(acc[1:], set()).add(p.id)
        succ = {p.id: set() for p in self.procs}
        for key, ws in W.items():
            for w in ws:
                succ[w] |= {r for r in R.get(key, ()) if r != w}
        for p in self.procs:
            succ[p.id] |= {c.id for c in p.children}
        # strongly connected components (Tarjan)
        idx, low, on, st, comps, cnt = {}, {}, set(), [], [], [0]

        def strong(v):
            idx[v] = low[v] = cnt[0]; cnt[0] += 1
            st.append(v); on.add(v)
            for w in sorted(succ[v]):
                if w not in idx: strong(w); low[v] = min(low[v], low[w])
                elif w in on: low[v] = min(low[v], idx[w])
            if low[v] == idx[v]:
                c = []
                while True:
                    w = st.pop(); on.discard(w); c.append(w)
                    if w == v: break
                comps.append(sorted(c))
        for p in self.procs:
            if p.id not in idx: strong(p.id)
        comp_of = {v: i for i, c in enumerate(comps) for v in c}
        csucc = {i: set() for i in range(len(comps))}
        for v, ws in succ.items():
            for w in ws:
                if comp_of[v] != comp_of[w]: csucc[comp_of[v]].add(comp_of[w])
        indeg = {i: 0 for i in csucc}
        for i, ws in csucc.items():
            for w in ws: indeg[w] += 1
        ready = sorted((i for i, d in indeg.items() if d == 0), key=lambda i: comps[i][0])
        sched, self.coupled = [], []
        while ready:
            i = ready.pop(0)
            if len(comps[i]) > 1:
                sched += [v for _ in range(self.SYNC) for v in comps[i]]
                self.coupled.append(comps[i])
            else:
                sched.append(comps[i][0])
            for w in csucc[i]:
                indeg[w] -= 1
                if indeg[w] == 0: ready.append(w)
            ready.sort(key=lambda j: comps[j][0])
        return sched

    def shared_vars(self, text):
        """testbench variables (not channels / handshakes) that one process writes and another reads at the same
        edge: they run in the schedule's order, which is one of the orders the simulator may pick"""
        self.shared = []
        hs = {v for sq in self.sqrs if sq.proto for k, v in sq.proto.items() if isinstance(v, str)}
        for nm in self.var_order:
            ty, init, kind = self.vars[nm]
            if kind != 'state' or nm.endswith('__pc') or nm in hs or '__' not in nm: continue
            if any(nm.startswith(c.name + '__') for c in self.chans if getattr(c, '_decl', False)): continue
            rx = re.compile(r'\b' + re.escape(nm) + r'\b')
            wx = re.compile(r'\b' + re.escape(nm) + r'(\[[^\]]*\]|\.\w+)*\s*=[^=]')
            users = [pid for pid, t in text.items() if rx.search(t)]
            writers = [pid for pid in users if wx.search(text[pid])]
            if len(users) > 1 and writers:
                self.shared.append({'variable': nm, 'written_by': [self.procs[i].name for i in writers],
                                    'read_by': [self.procs[i].name for i in users if i not in writers]})
        if self.shared:
            self.note('variables shared by processes at one edge (' + ', '.join(x['variable'] for x in self.shared) +
                      '): a free bit per edge picks which process runs first (free_order)')

    def emit(self):
        for p in self.procs: self.immediate(p)
        self.states()
        self.act_vars, self.act_set = [], set()
        self.flags, self.flag_names = {}, set()
        sched = self.schedule()
        # variables two processes share at an edge: a free bit per pair picks which one runs first
        text = {p.id: '\n'.join(self.proc_code(p, 0)) for p in self.procs}
        self.shared_vars(text)
        gates = {}         # schedule position -> gate
        self.swaps = []
        coupled = {v for c in getattr(self, 'coupled', []) for v in c}
        pairs = set()
        for x in self.shared:
            ps = [self.procs_by_name[n] for n in x['written_by'] + x['read_by']]
            for i in range(len(ps)):
                for j in range(i + 1, len(ps)):
                    a, b = ps[i].id, ps[j].id
                    if a == b or a not in sched or b not in sched: continue
                    pairs.add((min(a, b), max(a, b)))
        for a, b in sorted(pairs):
            # X runs either where it is or on the other side of every pass of Y (a free bit picks)
            x, y = (b, a) if b not in coupled else (a, b)
            if x in coupled: continue
            xi = sched.index(x)
            ys = [k for k, v in enumerate(sched) if v == y]
            if xi in gates: continue        # one free order per process (more would need every interleaving)
            sw = f'fv_swap{len(self.swaps)}'
            at = ys[0] if xi > ys[-1] else ys[-1] + 1
            self.swaps.append((sw, self.procs[x].name, self.procs[y].name))
            sched = sched[:at] + [x] + sched[at:]
            gates = {(k if k < at else k + 1): g for k, g in gates.items()}
            gates[xi if xi < at else xi + 1] = f'!{sw}'
            gates[at] = sw
        self.sched = sched
        mod = f'fv_agent_{self.name}'
        np = len(self.procs)
        L = ['// generated by fvgen (fvproc.py): the UVM agent ' + self.agent_path + ' as a formal model.',
             '// See fv_agent_' + self.name + '.json for what each part of the testbench became and every approximation.',
             '//',
             '// Every testbench process is a state machine clocked by clk: a state per wait (named after its source',
             '// line), counters for repeat (n) @(posedge clk), registers for the variables it keeps across waits. At',
             '// each edge the processes run once, in the order ' + ' -> '.join(self.procs[i].name for i in sched),
             '`include "fv_tool.svh"', '']
        params = []
        for nm, (ty, dflt, src) in sorted(self.params.items()):
            pt = 'int' if ty.w == 32 and ty.s else 'int unsigned' if ty.w == 32 else self.decl(ty, '').strip()
            params.append((f'  parameter {pt} {nm} = {dflt}', f'{src}'))
        ports = ['  input  logic clk']
        for sig, pi in sorted(self.ports.items()):
            if sig == self.clock: continue
            if pi['dir'] == 'in':
                ports.append(f"  input  logic [{pi['w'] - 1}:0] {sig}")
            else:
                init = sv_const(pi['init'], pi['w']) if pi.get('init') else "'0"
                ports.append(f"  output logic [{pi['w'] - 1}:0] {sig} = {init}")
        ports.append('  output logic fv_bound_hit')
        ports.append(f'  output logic [{max(1, len(self.errors)) - 1}:0] fv_uvm_error')
        L.append(f'module {mod}' + (' #(' if params else ' ('))
        if params:
            L += [f'{d}{"," if i < len(params) - 1 else ""}   // {c}' for i, (d, c) in enumerate(params)]
            L.append(') (')
        L += [x + (',' if i < len(ports) - 1 else '') for i, x in enumerate(ports)]
        L += [');', '  `include "prim_assert.sv"', '']
        for nm in list(self.var_order):       # register every struct a variable uses before the typedefs
            try: self.decl(self.vars[nm][0], nm)
            except Unsupported: pass
        for cls in list(self.struct_order):
            fs = self.structs[cls]
            L.append(f'  // {cls}')
            L.append('  typedef struct packed {')
            if not fs: L.append("    bit fv_empty;")
            for f, ty, _ in fs:
                if not (ty.kind == 'array' and ty.elem.kind == 'none'): L.append(f'    {self.decl(ty, f)};')
                if ty.kind == 'array' and ty.akind == 'queue': L.append(f'    bit [{cw(ty.n) - 1}:0] {f}__n;')
                if ty.kind == 'array' and ty.akind == 'assoc': L.append(f'    bit [{ty.n - 1}:0] {f}__e;')
            L.append(f'  }} {self.struct(cls)};')
        L.append('')
        # process states
        for p in self.procs:
            sts = [(p.st_idle, 'not started'), (p.st_entry, 'time 0 / just forked'), (p.st_end, 'finished')]
            sts += [(n.state, f'{n.desc} (line {n.line})' if n.line else n.desc) for n in p.nodes if n.kind == 'wait']
            L.append(f'  // process {p.name}: {p.desc}')
            for i, (s_, d) in enumerate(sts):
                L.append(f"  localparam logic [{p.sw - 1}:0] {s_} = {p.sw}'d{i};   // {d}")
            init = p.st_entry if p.top else p.st_idle
            self.vars[p.pc] = (BITS(p.sw), init, 'state')
            if p.pc not in self.var_order: self.var_order.append(p.pc)
        L.append('')
        # variables: x_q is the register, x its value during (and at the end of) this edge's evaluation
        for nm in self.var_order:
            ty, init, kind = self.vars[nm]
            if kind in ('temp', 'snap'):
                L.append(f'  {self.decl(ty, nm)};')
            elif kind == 'prev':
                pass
            else:
                L.append(f'  {self.decl(ty, nm)};')
                L.append(f"  {self.decl(ty, nm + '_q')} = {init or chr(39) + '0'};")
        for nm in self.var_order:
            ty, init, kind = self.vars[nm]
            if kind == 'prev':
                L.append(f'  {self.decl(ty, nm)} = 1\'b0;')
        for sig, pi in sorted(self.ports.items()):
            if pi['dir'] == 'out': L.append(f"  bit [{pi['w'] - 1}:0] {sig}__nx;")
        L += [f'  bit [{np - 1}:0] fv_ck;   // the process has not used this edge yet',
              "  bit [63:0] fv_cycle = '0;", '  bit fv_bad, fv_ovf;',
              f'  bit [{max(1, len(self.errors)) - 1}:0] fv_err;']
        for nm in self.lates:
            L.append(f'  `FV_ANYSEQ logic fv_late_{nm};   // a try_get of {nm} at this edge ran before its put')
        for sw, a, b in self.swaps:
            L.append(f'  `FV_ANYSEQ logic {sw};   // {a} runs on the other side of {b} at this edge (they share variables)')
        for sid, w, desc, depth in self.sites:
            if depth == 1:
                L.append(f'  `FV_ANYSEQ logic [{w - 1}:0] fv_ch{sid}_v;   // {desc}')
                L.append(f'  (* keep *) bit fv_ch{sid}_u;   // taken at this edge (kept: traces show when, for replay)')
            else:
                L.append(f'  `FV_ANYSEQ logic [{depth - 1}:0][{w - 1}:0] fv_ch{sid}_v;   // {desc}')
                L.append(f'  (* keep *) bit [{iw(depth) - 1}:0] fv_ch{sid}_u;   // how many were taken at this edge')
        L.append('')
        L += self.site_funcs
        L += self.func_text
        L.append('')
        body, text = [], {}
        for k_, pid in enumerate(sched):
            p = self.procs[pid]
            code = self.proc_code(p, sched[:k_].count(pid) if k_ not in gates else 0, gates.get(k_))
            body += code
        L.append('  logic ' + ', '.join(self.act_vars) + ';' if self.act_vars else '')
        L.append('  always_comb begin')
        for nm in self.var_order:
            ty, init, kind = self.vars[nm]
            if kind == 'state': L.append(f'    {nm} = {nm}_q;')
            elif kind == 'temp': L.append(f"    {nm} = '0;")
            elif kind == 'snap': L.append(f'    {nm} = {init};')
        for sig, pi in sorted(self.ports.items()):
            if pi['dir'] == 'out': L.append(f'    {sig}__nx = {sig};')
        for sid, w, desc, depth in self.sites: L.append(f"    fv_ch{sid}_u = '0;")
        if self.act_vars: L.append('    ' + ' '.join(f"{v} = 1'b0;" for v in self.act_vars))
        L.append("    fv_bad = 1'b0; fv_ovf = 1'b0; fv_err = '0;")
        for p in self.procs:
            L.append(f'    fv_ck[{p.id}] = ({p.pc}_q != {p.st_idle});')
        L += body
        # processes that hand work to each other: nothing may be left ready to run after SYNC passes
        chk = []
        for comp in getattr(self, 'coupled', []):
            for pid in comp:
                p = self.procs[pid]
                for n in p.nodes:
                    if n.kind == 'wait' and n.wk in self.ZERO_TIME:
                        chk.append(f"    if ({p.pc} == {n.state} && {n.ready}) fv_ovf = 1'b1;   // SYNC too small")
        L += chk
        L += ['  end', '']
        L.append('  always_ff @(posedge clk) begin')
        for nm in self.var_order:
            ty, init, kind = self.vars[nm]
            if kind == 'state': L.append(f'    {nm}_q <= {nm};')
        for sig, pi in sorted(self.ports.items()):
            if pi['dir'] == 'out': L.append(f'    {sig} <= {sig}__nx;')
        for nm in self.var_order:
            ty, init, kind = self.vars[nm]
            if kind == 'prev': L.append(f'    {nm} <= {nm[len("fv_prev_"):]};')
        L += ["    fv_cycle <= fv_cycle + 1'b1;", '  end', '']
        L += ['  assign fv_bound_hit = fv_ovf;', '  assign fv_uvm_error = fv_err;', '',
              '  // free choices are taken from their allowed ranges',
              "  `ASSUME(fv_choices_valid, !fv_bad, clk, 1'b0)",
              '`ifndef FV_MODEL_NO_ASSERT',
              '  // the modelled bounds (QDEPTH, CNT_W, loop bounds, choices per edge, SYNC) are never exceeded',
              "  `ASSERT(fv_bounds_ok, !fv_ovf, clk, 1'b0)"]
        for k, desc in self.errors:
            L.append(f'  // {desc}')
            L.append(f"  `ASSERT(fv_uvm_error_{k}, !fv_err[{k}], clk, 1'b0)")
        L += ['`endif', 'endmodule', '']
        return '\n'.join(L)

    def report(self):
        procs = []
        for p in self.procs:
            procs.append({'name': p.name, 'source': p.desc, 'parent': p.parent.name if p.parent else None,
                          'states': [{'state': n.state, 'wait': n.desc, 'line': n.line, 'kind': n.wk,
                                      'immediate': n.imm} for n in p.nodes if n.kind == 'wait'],
                          'delay_counter': getattr(p, 'cnt', None)})
        overrides = {}
        for o in (self.D.config.get('overrides') or []):
            for pnm, (ty, dflt, src) in self.params.items():
                if src.split('.')[-1] == o.get('field') and o.get('owner'):
                    overrides.setdefault(o['owner'], {})[pnm] = o.get('value')
        return {'agent': self.agent_path, 'module': f'fv_agent_{self.name}', 'clock': self.clock,
                'ports': self.ports, 'parameters': {k: {'default': v[1], 'from': v[2]} for k, v in self.params.items()},
                'test_overrides': overrides, 'processes': procs,
                'channels': [c.name for c in self.chans if getattr(c, '_decl', False)],
                'sequencers': [{'name': s.name, 'free_source': bool(s.proto and s.proto.get('free')),
                                'sequences': len(s.proto['seqs']) if s.proto else 0} for s in self.sqrs],
                'schedule': [self.procs[i].name for i in getattr(self, 'sched', [])],
                'coupled': [[self.procs[i].name for i in c] for c in getattr(self, 'coupled', [])],
                'shared_variables': getattr(self, 'shared', []),
                'test_constants': self.test_args,
                'free_order': [{'bit': sw, 'processes': [a, b]} for sw, a, b in getattr(self, 'swaps', [])],
                'free_choices': [{'id': sid, 'width': w, 'what': d, 'per_edge': dp, **self.site_meta.get(sid, {})}
                                 for sid, w, d, dp in self.sites],
                'uvm_errors_as_assertions': [{'id': k, 'what': d} for k, d in self.errors],
                'approximations': self.notes, 'unsupported': self.unsupported,
                'not_modelled_unobservable': self.pruned,
                'bounds': {'QDEPTH': self.QD, 'SYNC': self.SYNC, 'CNT_W': self.CNT_W, 'LOOP': self.LOOP,
                           'CHOICES': self.CHOICES}}


def ind(lines, n=1):
    return [('  ' * n) + x for x in lines]


def live_set(g):
    live = set(g.roots)
    changed = True
    while changed:
        changed = False
        for lhs, reads in g.deps:
            if lhs in live and not reads <= live:
                live |= reads; changed = True
    return live


def build(D, agent, opts):
    """the model of one agent / test: (generator, SV text, report)"""
    g = AgentGen(D, agent, opts)        # first pass: what each statement reads
    g.discover()
    g.processes()
    g.emit()
    live = live_set(g)
    g = AgentGen(D, agent, dict(opts, live=live))       # second pass: without what reaches nothing observable
    g.discover()
    g.processes()
    sv = g.emit()
    return g, sv, g.report()


def write_tool_header(out):
    """fv_tool.svh next to the models (what differs between yosys and the commercial tools)"""
    import fvtools
    p = os.path.join(out, 'fv_tool.svh')
    if not os.path.exists(p): open(p, 'w').write(fvtools.TOOL_HEADER)


def generate(d, agent, out, opts):
    g, sv, rep = build(Design(d), agent, opts)
    os.makedirs(out, exist_ok=True)
    write_tool_header(out)
    path = os.path.join(out, f'fv_agent_{g.name}.sv')
    open(path, 'w').write(sv)
    json.dump(rep, open(os.path.join(out, f'fv_agent_{g.name}.json'), 'w'), indent=1)
    return path, rep


def model_key(sv, name):
    """what makes two test models the same model: the text without comments, parameter defaults and the source
    line numbers in state / step names"""
    t = re.sub(r'//[^\n]*', '', sv).replace(f'fv_agent_{name}', 'fv_agent__')
    t = re.sub(r'(parameter\s[^=]*=)[^,\n)]*', r'\1?', t)
    t = re.sub(r'(__(?:t0_)?L)\d+', r'\1#', t)
    return hashlib.sha1(re.sub(r'\s+', ' ', t).encode()).hexdigest()


def test_config(D, test_path):
    """the cfg fields a test's configure() (and its base tests') set: {field: value}, base classes first"""
    out = {}
    for p in reversed(D.bases(test_path)):
        nm = D.classes[p]['name']
        for o in D.config.get('overrides') or []:
            if o.get('owner') in (nm, p) and o.get('field') is not None and o.get('value') is not None:
                out[o['field']] = o['value']
    return out


def generate_tests(d, out, opts, pattern=None):
    """every uvm_test of the testbench (name matching `pattern`): its model; tests whose models differ only in
    parameters (cfg knobs set by configure(), constants of their own code) share one: OUT/fv_tests_g<k>.sv, and
    OUT/fv_tests.json says which group each test is in and its parameter values"""
    D = Design(d)
    tests = sorted(c['path'] for c in d['classes'] if D.is_a(c['path'], 'uvm_test') and not c.get('virtual')
                   and c['path'] in D.classes and (pattern is None or re.search(pattern, c['name'])))
    groups, failed = {}, {}
    for t in tests:
        tn = D.classes[t]['name']
        try:
            g, sv, rep = build(D, t, opts)
        except Unsupported as e:
            failed[tn] = str(e); continue
        cfg = test_config(D, t)
        params = {pn: cfg[src.split('.')[-1]] for pn, (_, _, src) in g.params.items()
                  if '.' in src and not src.startswith('test constant') and src.split('.')[-1] in cfg}
        params.update(rep.get('test_constants') or {})
        gr = groups.setdefault(model_key(sv, g.name), {'sv': sv, 'rep': rep, 'name': g.name, 'tests': {}})
        gr['tests'][tn] = params
    os.makedirs(out, exist_ok=True)
    write_tool_header(out)
    index = {'groups': {}, 'tests': {}, 'failed': failed}
    for i, gr in enumerate(sorted(groups.values(), key=lambda x: -len(x['tests']))):
        mod = f'fv_tests_g{i}'
        open(os.path.join(out, f'{mod}.sv'), 'w').write(gr['sv'].replace(f'fv_agent_{gr["name"]}', mod))
        rep = dict(gr['rep'], module=mod, tests=sorted(gr['tests']))
        json.dump(rep, open(os.path.join(out, f'{mod}.json'), 'w'), indent=1)
        index['groups'][mod] = {'tests': gr['tests'], 'parameters': gr['rep']['parameters']}
        for tn in gr['tests']: index['tests'][tn] = mod
    json.dump(index, open(os.path.join(out, 'fv_tests.json'), 'w'), indent=1)
    return index


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('json')
    ap.add_argument('--agent', required=True, action='append')
    ap.add_argument('-o', '--out', default='fv')
    ap.add_argument('--sync', type=int, default=2, help='passes of processes that hand work to each other in one edge')
    ap.add_argument('--cnt-w', type=int, default=8, help='width of repeat (n) @(posedge clk) counters')
    ap.add_argument('--qdepth', type=int, default=8)
    a = ap.parse_args()
    d = json.load(open(a.json))
    for ag in a.agent:
        path, rep = generate(d, ag, a.out, {'sync': a.sync, 'cnt_w': a.cnt_w, 'qdepth': a.qdepth})
        print(f'{path}: {len(rep["processes"])} processes, {len(rep["free_choices"])} free choices, '
              f'{len(rep["approximations"])} approximations, {len(rep["unsupported"])} unsupported')


if __name__ == '__main__':
    main()
