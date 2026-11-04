#!/usr/bin/env python3
"""sniper: dump a SystemVerilog testbench's verification constructs to JSON, using pyslang.

Purpose: give a later step everything needed to turn a constrained-random testbench into a
formal environment -- constraints become assumptions, drivers become RTL-style logic, coverage
becomes cover properties. Nothing here is specific to one testbench or methodology; UVM base
classes are recognised only to label each class's role.

Usage
    sniper.py --own DIR [--own DIR ...] [options] -- <slang arguments>

    <slang arguments> are what you would give the `slang` binary: source files, -f/-F filelists,
    +incdir+/-I, +define+/-D, --top, -G, etc.
    --own DIR       a directory (or file) holding the testbench's own sources. Constructs are
                    reported in detail only when they are written in an --own path. Repeatable.
                    Default: the current directory.
    --pkg NAME      also report parameters/typedefs/enums of this package (e.g. the DUT's package).
    --skip-pkg NAME never report this package's types even if imported (default: uvm_pkg, ovm_pkg).
                    Packages imported by --own code are included automatically. Repeatable.
    --hier-depth N  how deep to describe the instance tree below each top (default 2).
    -o FILE         output JSON (default sniper_out.json).

Output sections
    meta           tool versions, command, timing, diagnostics (all, and those in --own files)
    types          parameters, typedefs, enums (own files + packages they import + --pkg)
    classes        lineage, UVM role, members (type, width, rand/randc, initializer), constraint
                   blocks with a typed expression AST per item, methods
    subroutines    every task/function in --own files: args, body AST, derived facts
    procedures     initial/always/final blocks in --own files: body AST, derived facts
    randomization  every source of randomness: rand class members + constraint blocks,
                   randomize() calls (with inline constraints), std::randomize, $urandom*,
                   $random/$dist_*, randcase, randsequence
    stimulus       per class/procedure: which interface signals it drives and samples
    config         property defaults, and constant writes made through object handles
                   (the per-test knob overrides)
    coverage       covergroups (coverpoints, bins, crosses, options, sampling), cover/assume/
                   assert property statements, and ad-hoc counter coverage (heuristic)
    hierarchy      top instances: parameters, ports, port connections, interface signals, modports

Derived facts per routine/procedure: calls, timing controls (@, #, wait), loops, fork/join,
randomization sites, virtual-interface signal writes/reads, class field writes/reads.
Line numbers are of the file the construct is written in (include files keep their own numbering);
code expanded from a macro is reported at the macro's use site with "macro": <name>.
"""
import argparse, json, os, re, sys, time
import pyslang

A = pyslang.ast
SK = pyslang.syntax.SyntaxKind
RAND_SYSCALLS = {'$urandom', '$urandom_range', '$random', '$dist_uniform', '$dist_normal',
                 '$dist_exponential', '$dist_poisson', '$dist_chi_square', '$dist_t', '$dist_erlang'}
UVM_ROLES = ['uvm_test', 'uvm_env', 'uvm_agent', 'uvm_driver', 'uvm_monitor', 'uvm_sequencer',
             'uvm_scoreboard', 'uvm_subscriber', 'uvm_sequence', 'uvm_sequence_item', 'uvm_reg_block',
             'uvm_reg', 'uvm_component', 'uvm_object']


# ======================================================================== compile
def compile_design(slang_args):
    cmd = 'slang ' + ' '.join(a if ' ' not in a else f'"{a}"' for a in slang_args)
    d = pyslang.driver.Driver()
    d.addStandardArgs()
    if not d.parseCommandLine(cmd, pyslang.driver.CommandLineOptions()):
        sys.exit('slang rejected the command line: ' + cmd)
    if not d.processOptions():
        sys.exit('slang could not process the options')
    t0 = time.time(); d.parseAllSources(); t1 = time.time()
    comp = d.createCompilation()
    diags = comp.getAllDiagnostics()          # forces full elaboration
    t2 = time.time()
    return d, comp, diags, cmd, t1 - t0, t2 - t1


# ======================================================================== locations
class Loc:
    def __init__(self, sm, own_paths, base):
        self.sm = sm
        self.own = [os.path.realpath(p) for p in own_paths]
        self.base = base
        self.cache = {}

    def _path(self, loc):
        b = loc.buffer
        if b in self.cache: return self.cache[b]
        p = ''
        for get in (lambda: self.sm.getFullPath(b), lambda: self.sm.getFileName(loc)):
            try:
                p = str(get() or '')
                if p: break
            except Exception:
                pass
        p = os.path.realpath(p) if p else ''
        self.cache[b] = p
        return p

    def resolve(self, loc):
        """(abs path, line, macro name or None), at the macro's use site for macro-expanded code."""
        macro = None
        sm = self.sm
        if sm.isMacroLoc(loc):
            # walk out through nested expansions; the last name is the macro written in the source
            l, guard = loc, 0
            while sm.isMacroLoc(l) and guard < 64:
                try:
                    n = sm.getMacroName(l)
                    if n: macro = str(n)
                    l = sm.getExpansionLoc(l)
                except Exception:
                    break
                guard += 1
            loc = sm.getFullyExpandedLoc(loc)
        return self._path(loc), sm.getLineNumber(loc), macro

    def is_own(self, path):
        return any(path == o or path.startswith(o + os.sep) for o in self.own)

    def rel(self, path):
        return os.path.relpath(path, self.base) if path else None

    def span(self, rng):
        p, a, m = self.resolve(rng.start)
        _, b, _ = self.resolve(rng.end)
        out = {'file': self.rel(p), 'start': a, 'end': b}
        if m: out['macro'] = m
        return out

    def line(self, rng):
        return self.resolve(rng.start)[1]


SM = None          # source manager, set in main(); used to recover text for nodes without syntax


# comments, whitespace and preprocessor directives that str(syntax) carries as the first token's trivia
LEADING_TRIVIA = re.compile(r'(?:\s+|//[^\n]*|/\*.*?\*/|`(?:ifdef|ifndef|elsif|else|endif|define|undef|include|'
                            r'timescale|line|pragma|resetall|default_nettype|celldefine|endcelldefine)\b[^\n]*)*',
                            re.S)


def text(node, limit=300):
    """Source text of a node, whitespace-collapsed. Elaborated nodes created by slang (implicit
    conversions, folded constants) have no syntax, so fall back to slicing the source buffer."""
    syn = getattr(node, 'syntax', None)
    s = ''
    if syn is not None:
        s = str(syn)
        s = ' '.join(s[LEADING_TRIVIA.match(s).end():].split())
    if not s and SM is not None:
        rng = getattr(node, 'sourceRange', None)
        try:
            a, b = rng.start, rng.end
            if a.buffer == b.buffer and b.offset > a.offset:
                s = ' '.join(str(SM.getSourceText(a.buffer))[a.offset:b.offset].split())
        except Exception:
            pass
    return s if len(s) <= limit else s[:limit] + ' ...'


def nkey(x):
    """Stable identity for an AST node (pyslang hands out a new Python wrapper on every access)."""
    r = x.sourceRange
    return (x.kind.name, r.start.offset, r.end.offset)


def enum_name(v):
    t = type(v)
    if hasattr(t, '__members__') and hasattr(v, 'name'):
        return v.name
    return None


def mode_call(x, name):
    """obj.blk.constraint_mode(v) / obj.fld.rand_mode(v) / obj.constraint_mode(v): receiver object,
    block or field (None: every one of the object's), new mode. The getter forms take no argument."""
    v = x.arguments[-1]
    out = {'receiver': None, 'member': None, 'value': const_str(v) or text(v)}   # literals are not always folded
    if x.isSystemCall:              # block/field form: the first argument is the block or field
        a0 = x.arguments[0]
        if isinstance(a0, A.MemberAccessExpression):
            out['receiver'], out['member'] = text(a0.value), a0.member.name
        elif getattr(a0, 'symbol', None) is not None:     # blk.constraint_mode(v) inside the class
            out['receiver'], out['member'] = 'this', a0.symbol.name
    else:                           # object form: built-in class method
        head = text(x).split('(')[0]
        out['receiver'] = head.rsplit('.', 1)[0] if '.' in head else 'this'
    return out


def const_str(e):
    """Folded constant of an expression as text; string literals come back quoted."""
    try:
        if e.kind.name == 'StringLiteral':      # slang does not fold string literals
            return e.rawValue
        c = e.constant
    except Exception:
        return None
    return None if c is None else str(c)


# ======================================================================== generic AST serializer
NODE_BASES = tuple(getattr(A, n) for n in
                   ('Expression', 'Statement', 'TimingControl', 'Constraint', 'AssertionExpr',
                    'Pattern', 'BinsSelectExpr') if hasattr(A, n))
BASE_ATTRS = {b: set(dir(b)) for b in NODE_BASES}
LITERALS = ('IntegerLiteral', 'StringLiteral', 'RealLiteral', 'UnbasedUnsizedIntegerLiteral', 'TimeLiteral')
SKIP = {'syntax', 'sourceRange', 'bad', 'kind', 'compilation', 'parentScope', 'declaringDefinition',
        'eval', 'evalLValue', 'visit', 'check', 'isEquivalentTo', 'getSymbolReference',
        'effectiveWidth', 'hasHierarchicalReference', 'isImplicitString', 'isImplicitlyAssignableTo',
        'isUnsizedInteger', 'constant', 'type', 'scope', 'containingSymbol'}


def type_info(t, depth=0):
    """Layout of a type for consumers that re-model it as bit vectors: width, signedness, enum
    values, struct fields (with bit offsets when packed), array element type and size."""
    ct = t.canonicalType
    k = ct.kind.name
    ti = {'kind': k}
    if t.isIntegral:
        ti['w'], ti['signed'] = t.bitWidth, bool(t.isSigned)
    if ct.isEnum:
        ti['enum'] = [{'name': m.name, 'value': str(m.value)} for m in ct if m.kind.name == 'EnumValue']
    if depth > 4: return ti
    if ct.isStruct or k in ('PackedUnionType', 'UnpackedUnionType'):
        ti['fields'] = [{'name': f.name, 'offset': f.bitOffset if t.isIntegral else None,
                         'type': type_info(f.type, depth + 1)} for f in ct if f.kind.name == 'Field']
    elif ct.isUnpackedArray or ct.isQueue or ct.isAssociativeArray:
        try: ti['elem'] = type_info(ct.arrayElementType, depth + 1)
        except Exception: pass
        if ct.hasFixedRange:
            r = ct.fixedRange
            ti['range'] = [r.left, r.right]
            ti['size'] = abs(r.left - r.right) + 1
    elif ct.isClass:
        ti['class'] = ct.name
        ti['class_path'] = ct.hierarchicalPath      # names are not unique across packages
        try:
            if ct.genericClass is not None:
                ti['generic'] = ct.genericClass.name
                ti['generic_path'] = ct.genericClass.hierarchicalPath
        except Exception: pass
    return ti


def sym_ref(s):
    r = {'sym': s.name, 'skind': s.kind.name}
    try: r['path'] = s.hierarchicalPath
    except Exception: pass
    return r


class Ser:
    """Turns pyslang AST nodes (expressions, statements, timing controls, constraints, assertion
    expressions, patterns) into nested dicts by reflection, so new node kinds need no code here."""
    MAX_STRUCT_NESTING = 3

    def __init__(self, loc):
        self.loc = loc

    def node(self, n, top=True):
        return self._node(n, top, set())

    def _node(self, n, top, seen):
        if id(n) in seen:
            return {'k': n.kind.name, 'cycle': True}
        seen = seen | {id(n)}
        out = {'k': n.kind.name}
        base = next(b for b in NODE_BASES if isinstance(n, b))
        is_expr = isinstance(n, A.Expression)
        rng = getattr(n, 'sourceRange', None)
        if rng is None and getattr(n, 'syntax', None) is not None:   # constraints carry no range
            rng = n.syntax.sourceRange
        if rng is not None and (top or not is_expr):
            sp = self.loc.span(rng)
            if not sp['start'] and getattr(n, 'syntax', None) is not None:   # invalid nodes: empty range
                sp = self.loc.span(n.syntax.sourceRange)
            out['line'] = sp['start']
            if sp['end'] != sp['start']: out['end'] = sp['end']
            if 'macro' in sp: out['macro'] = sp['macro']
            out['src'] = text(n)
        if is_expr:
            try:
                t = n.type
                out['ty'] = str(t)
                if t.isIntegral: out['w'], out['signed'] = t.bitWidth, bool(t.isSigned)
            except Exception: pass
            if n.kind.name == 'MemberAccess':
                try:
                    if n.value.type.canonicalType.kind.name in ('PackedStructType', 'PackedUnionType'):
                        out['offset'] = n.member.bitOffset
                except Exception: pass
            c = const_str(n)
            if n.kind.name in LITERALS:
                if c is None:       # not folded outside a full expression (dist weights, ...)
                    try: c = str(n.value)
                    except Exception: c = text(n)
                out['const'] = c
                return out          # 'const' carries the value; the raw literal object adds nothing
            if c is None and n.kind.name == 'NamedValue':      # references are not folded at the use site
                try:
                    if n.symbol.kind.name in ('EnumValue', 'Parameter'): c = str(n.symbol.value)
                except Exception: pass
            if c is not None: out['const'] = c
        for a in sorted(set(dir(type(n))) - BASE_ATTRS[base] - SKIP):
            if a.startswith('_') or a[0].isupper(): continue
            try: v = getattr(n, a)
            except Exception: continue
            if callable(v) and not isinstance(v, NODE_BASES): continue
            sv = self._val(v, seen, 0)
            if sv not in (None, [], {}, ''): out[a] = sv
        return out

    def _val(self, v, seen, nest):
        if v is None or isinstance(v, (bool, int, float, str)): return v
        if isinstance(v, NODE_BASES): return self._node(v, False, seen)
        if isinstance(v, A.Symbol): return sym_ref(v)
        if isinstance(v, A.Type): return str(v)
        e = enum_name(v)
        if e is not None: return e
        tn = type(v).__name__
        if tn == 'ConstantValue': return str(v)
        if isinstance(v, (list, tuple)) or tn.endswith(('List', 'Span', 'span', 'Vector')):
            try: return [self._val(x, seen, nest) for x in v]
            except TypeError: pass
        if nest >= self.MAX_STRUCT_NESTING: return tn
        # small pybind helper structs: Condition, ItemGroup, LoopDim, RandomizeCallInfo, ...
        if id(v) in seen: return tn
        seen = seen | {id(v)}
        fields = {}
        for a in dir(v):
            # 'kind' is a field worth keeping on helper structs (DistWeight := vs :/, ...)
            if a.startswith('_') or (a in SKIP and a != 'kind') or a[0].isupper(): continue
            try: x = getattr(v, a)
            except Exception: continue
            if callable(x) and not isinstance(x, NODE_BASES): continue
            sx = self._val(x, seen, nest + 1)
            if sx not in (None, [], {}, ''): fields[a] = sx
        return fields or tn


# ======================================================================== body facts
def walk(n, fn):
    """Pre-order walk of expressions/statements/timing controls under n. Also descends into
    local declaration initializers (int r = $urandom();), which pyslang's visitor skips."""
    def cb(x):
        r = fn(x)
        if r is False: return A.VisitAction.Skip
        if x.kind.name == 'VariableDeclaration':
            init = x.symbol.initializer
            if init is not None: init.visit(cb)
        return A.VisitAction.Advance
    n.visit(cb)


def strip_lvalue(e):
    """Base named value of an lvalue/rvalue plus the member chain: a.b[i].c -> (a, ['b','c'])."""
    chain = []
    while True:
        k = e.kind.name
        if k == 'MemberAccess': chain.append(e.member.name); e = e.value
        elif k in ('ElementSelect', 'RangeSelect'): e = e.value
        elif k == 'Conversion': e = e.operand
        else: break
    return e, list(reversed(chain))


def is_vif_type(t):
    try: return bool(t.isVirtualInterface)
    except Exception: return str(t).startswith('virtual ')


def classify_ref(e):
    """What an expression refers to: a virtual-interface signal, a class field, a module-level
    signal, a local variable, or None."""
    # collect the access chain innermost-first: a.b.c -> [a, a.b, a.b.c] with member names
    nodes, x = [], e
    while True:
        k = x.kind.name
        if k == 'MemberAccess': nodes.append((x, x.member.name)); x = x.value
        elif k in ('ElementSelect', 'RangeSelect'): x = x.value
        elif k == 'Conversion': x = x.operand
        else: break
    root = x
    if root.kind.name not in ('NamedValue', 'HierarchicalValue'):
        return None
    nodes.reverse()
    s = root.symbol
    sk = s.kind.name
    chain = [n for _, n in nodes]
    # the first point along the chain whose type is a virtual interface: handle, cfg.vif, ...
    steps = [(root, s.name)] + nodes
    for i, (node, _) in enumerate(steps):
        try: ty = node.type
        except Exception: continue
        if is_vif_type(ty):
            itype = str(ty).replace('virtual ', '', 1)        # ifc#(P=1,...).modport
            name, _, params = itype.partition('#')
            r = {'what': 'vif', 'handle': '.'.join([s.name] + chain[:i]),
                 'iface': name.split('.')[0], 'signal': '.'.join(chain[i:])}
            if params: r['iface_params'] = params
            return r
    # a member access goes through a handle only when the object is a class (not a struct/union)
    def is_cls(x):
        try: return bool(x.type.isClass)
        except Exception: return False
    via = any(is_cls(node.value) for node, _ in nodes)
    if chain and sk == 'ClassProperty' and via:
        return {'what': 'field', 'path': '.'.join([s.name] + chain), 'field': chain[-1], 'via_handle': True}
    if sk == 'ClassProperty':              # own property, or a member of an own struct property
        return {'what': 'field', 'path': '.'.join([s.name] + chain), 'field': s.name, 'via_handle': False}
    if sk in ('Variable', 'Net') and root.kind.name == 'HierarchicalValue':
        return {'what': 'signal', 'path': text(e)}
    if chain and sk in ('Variable', 'FormalArgument') and via:
        # req.mem_id = id : write through a local/argument handle
        return {'what': 'field', 'path': '.'.join([s.name] + chain), 'field': chain[-1], 'via_handle': True,
                'handle_kind': 'local' if sk == 'Variable' else 'arg'}
    if sk in ('Variable', 'Net'):
        try: procedural = s.parentScope.isProceduralContext
        except Exception: procedural = False
        return {'what': 'local' if procedural else 'signal',
                'path': s.name + ('.' + '.'.join(chain) if chain else '')}
    if sk == 'FormalArgument':
        return {'what': 'arg', 'path': '.'.join([s.name] + chain), 'field': chain[-1] if chain else None,
                'via_handle': False}
    return None


def uninstantiated(sym):
    """True for code in a scope slang did not elaborate (a generate branch not taken, ...)."""
    try: return bool(sym.parentScope.isUninstantiated)
    except Exception: return False


def invalid_nodes(d, out=None):
    """Code slang could not bind (an error in meta.diagnostics), found in a serialized body. The
    visitor does not enter these subtrees, so nothing inside them appears in the facts."""
    out = [] if out is None else out
    if isinstance(d, dict):
        if d.get('k') == 'Invalid':
            out.append({'line': d.get('line'), 'src': d.get('src')})
            return out
        for v in d.values(): invalid_nodes(v, out)
    elif isinstance(d, list):
        for v in d: invalid_nodes(v, out)
    return out


def analyse(body, loc, ser):
    facts = {k: [] for k in ('calls', 'timing', 'loops', 'forks', 'rand_sites', 'vif_writes',
                             'vif_reads', 'signal_writes', 'signal_reads', 'field_writes', 'field_reads')}
    if body is None: return facts
    lhs_ids = set()

    def ln(x): return loc.line(x.sourceRange)

    def fn(x):
        k = x.kind.name
        if k == 'Assignment':
            node = x.left
            while True:          # mark the lvalue chain so the read pass skips it
                lhs_ids.add(nkey(node))
                if node.kind.name == 'MemberAccess': node = node.value
                elif node.kind.name in ('ElementSelect', 'RangeSelect'): node = node.value
                elif node.kind.name == 'Conversion': node = node.operand
                else: break
            r = classify_ref(x.left)
            entry = {'line': ln(x), 'src': text(x, 200), 'nonblocking': x.isNonBlocking,
                     'rhs_const': const_str(x.right)}
            if r is None: return
            if r['what'] == 'vif' and not r['signal']:      # vif = cfg.vif : handle assignment
                facts['field_writes'].append({'what': 'field', 'path': r['handle'], 'field': r['handle'],
                                              'via_handle': False, 'vif_handle': True, **entry})
            elif r['what'] == 'vif': facts['vif_writes'].append({**r, **entry})
            elif r['what'] == 'signal': facts['signal_writes'].append({**r, **entry})
            elif r['what'] in ('field', 'arg') and (r['what'] == 'field' or r.get('via_handle')):
                facts['field_writes'].append({**r, **entry})
        elif k == 'Call':
            name = x.subroutineName
            c = {'name': name, 'system': x.isSystemCall, 'line': ln(x), 'src': text(x, 200)}
            sr = x.subroutine
            if isinstance(sr, A.Symbol):
                c['target'] = sr.hierarchicalPath
            if name in ('constraint_mode', 'rand_mode') and x.arguments:
                c.update(mode_call(x, name))
            facts['calls'].append(c)
            if name in RAND_SYSCALLS:
                facts['rand_sites'].append({'kind': 'syscall', 'name': name, 'line': c['line'], 'src': text(x),
                                            'args': [text(a) for a in x.arguments]})
            elif name == 'randomize':
                # slang treats both forms as built-ins, so classify by how the call is written
                src = text(x)
                if src.startswith('std::'): rk, tgt = 'std::randomize', None
                elif '.randomize' in src.split('(')[0]: rk, tgt = 'randomize', src.split('.randomize')[0]
                else: rk, tgt = 'randomize', 'this'
                facts['rand_sites'].append({'kind': rk, 'target': tgt,
                                            'line': c['line'], 'src': text(x), 'ast': ser.node(x)})
        elif k == 'Timed':
            facts['timing'].append({'line': ln(x), 'timing': x.timing.kind.name, 'src': text(x.timing),
                                    'stmt': text(x, 120)})
        elif k in ('Wait', 'WaitFork', 'WaitOrder', 'EventTrigger'):
            facts['timing'].append({'line': ln(x), 'timing': k, 'src': text(x, 160)})
        elif k in ('DoWhileLoop', 'WhileLoop', 'ForLoop', 'ForeverLoop', 'RepeatLoop', 'ForeachLoop'):
            facts['loops'].append({'line': ln(x), 'loop': k, 'src': text(x, 120)})
        elif k == 'Block':
            bk = enum_name(x.blockKind)
            if bk and bk != 'Sequential':
                facts['forks'].append({'line': ln(x), 'join': bk, 'src': text(x, 120)})
        elif k in ('RandCase', 'RandSequence'):
            facts['rand_sites'].append({'kind': k.lower(), 'line': ln(x), 'src': text(x), 'ast': ser.node(x)})
        elif k in ('NamedValue', 'HierarchicalValue', 'MemberAccess'):
            if nkey(x) in lhs_ids: return
            r = classify_ref(x)
            if r is None: return
            e = {**r, 'line': ln(x)}
            if r['what'] == 'vif' and r['signal']: facts['vif_reads'].append(e)
            elif r['what'] == 'signal': facts['signal_reads'].append(e)
            elif r['what'] == 'field': facts['field_reads'].append(e)

    walk(body, fn)

    def dedupe(lst, key):
        seen, out = set(), []
        for r in lst:
            kk = key(r)
            if kk in seen: continue
            seen.add(kk); out.append(r)
        return out
    # a.b.c fires for a.b.c, a.b and a; keep the longest reference per line
    for k in ('vif_reads', 'field_reads', 'signal_reads'):
        lst = sorted(facts[k], key=lambda r: -len(r.get('path') or r.get('signal') or ''))
        kept = []
        for r in lst:
            p = r.get('path') or (r.get('handle', '') + '.' + r.get('signal', ''))
            if any(o['line'] == r['line'] and (o.get('path') or (o.get('handle', '') + '.' + o.get('signal', ''))).startswith(p)
                   for o in kept):
                continue
            kept.append(r)
        facts[k] = sorted(kept, key=lambda r: r['line'])
    facts['vif_reads'] = dedupe(facts['vif_reads'], lambda r: (r['handle'], r['signal'], r['line']))
    return facts


# ======================================================================== constant-loop evaluator
class Unknown(Exception):
    pass


BINOPS = {'Add': lambda a, b: a + b, 'Subtract': lambda a, b: a - b, 'Multiply': lambda a, b: a * b,
          'Divide': lambda a, b: a // b, 'Mod': lambda a, b: a % b, 'Power': lambda a, b: a ** b,
          'LessThan': lambda a, b: a < b, 'LessThanEqual': lambda a, b: a <= b,
          'GreaterThan': lambda a, b: a > b, 'GreaterThanEqual': lambda a, b: a >= b,
          'Equality': lambda a, b: a == b, 'Inequality': lambda a, b: a != b,
          'CaseEquality': lambda a, b: a == b, 'CaseInequality': lambda a, b: a != b,
          'LogicalAnd': lambda a, b: bool(a and b), 'LogicalOr': lambda a, b: bool(a or b),
          'LogicalShiftLeft': lambda a, b: a << b, 'ArithmeticShiftLeft': lambda a, b: a << b,
          'LogicalShiftRight': lambda a, b: a >> b, 'ArithmeticShiftRight': lambda a, b: a >> b,
          'BinaryAnd': lambda a, b: a & b, 'BinaryOr': lambda a, b: a | b, 'BinaryXor': lambda a, b: a ^ b}


def sv_format(fmt, args):
    it = iter(args)

    def rep(m):
        spec = m.group(0)
        if spec == '%%': return '%'
        try: v = next(it)
        except StopIteration: raise Unknown('format args')
        c = spec[-1].lower()
        if c == 's': return str(v)
        if c == 'd': return str(int(v))
        if c == 'b': return format(int(v), 'b')
        if c in 'hx': return format(int(v), 'x')
        if c == 'o': return format(int(v), 'o')
        return str(v)
    return re.sub(r'%%|%0?\d*[sdbhxoSDBHXO]', rep, fmt)


def parse_const(s):
    if s is None: raise Unknown('non-constant')
    if s.startswith('"'): return s[1:-1]
    m = re.fullmatch(r"(?:\d+)?'s?([dhbo])(.+)", s)
    if m:
        base = {'d': 10, 'h': 16, 'b': 2, 'o': 8}[m.group(1)]
        digits = m.group(2).replace('_', '')
        if re.search(r'[xzXZ?]', digits): raise Unknown('x/z')
        return int(digits, base)
    try: return int(s)
    except ValueError: raise Unknown('const ' + s)


def ev(e, env):
    k = e.kind.name
    if k == 'NamedValue' and e.symbol.name in env: return env[e.symbol.name]
    if k == 'NamedValue' and e.symbol.kind.name in ('Parameter', 'EnumValue'):
        return parse_const(str(e.symbol.value))       # references are not folded at the use site
    if k in ('IntegerLiteral', 'UnbasedUnsizedIntegerLiteral'):
        return parse_const(str(e.value))
    if k == 'StringLiteral':
        return e.rawValue[1:-1]
    if k == 'Conversion': return ev(e.operand, env)
    if k == 'BinaryOp':
        op = enum_name(e.op)
        if op not in BINOPS: raise Unknown(op)
        return BINOPS[op](ev(e.left, env), ev(e.right, env))
    if k == 'UnaryOp':
        op = enum_name(e.op)
        v = ev(e.operand, env)
        if op == 'LogicalNot': return not v
        if op == 'Minus': return -v
        if op == 'Plus': return v
        raise Unknown(op)
    if k == 'ConditionalOp':
        return ev(e.left, env) if ev(e.conditions[0].expr, env) else ev(e.right, env)
    if k == 'Concatenation' and all(isinstance(ev(o, env), str) for o in e.operands):
        return ''.join(ev(o, env) for o in e.operands)
    if k == 'Call' and e.subroutineName in ('$sformatf', '$psprintf'):
        a = [ev(x, env) for x in e.arguments]
        return sv_format(a[0], a[1:])
    return parse_const(const_str(e))


def try_ev(e, env=None):
    """Value of a constant expression (parameters resolved), or None."""
    try: return ev(e, env or {})
    except (Unknown, KeyError, TypeError, ValueError, ZeroDivisionError): return None


def run_const_loops(body, on_stmt, max_iter=1_000_000):
    """Execute the constant-bounded loops of a routine body, tracking assignments to locals,
    and call on_stmt(stmt, env) on every statement reached. Unknown values are left unset."""
    budget = [max_iter]

    def assign(e, env):
        if e.kind.name == 'Assignment' and e.left.kind.name == 'NamedValue':
            try: env[e.left.symbol.name] = ev(e.right, env)
            except (Unknown, KeyError, TypeError, ValueError): env.pop(e.left.symbol.name, None)

    def step(e, env):
        k = e.kind.name
        if k == 'UnaryOp' and e.operand.kind.name == 'NamedValue':
            nm = e.operand.symbol.name
            op = enum_name(e.op).lower()
            env[nm] = env[nm] + (1 if 'increment' in op else -1)
        elif k == 'Assignment':
            if e.isCompound:
                op = enum_name(e.op)
                env[e.left.symbol.name] = BINOPS[op](env[e.left.symbol.name], ev(e.right, env))
            else:
                assign(e, env)
        else:
            raise Unknown('step ' + k)

    def run(s, env):
        on_stmt(s, env)
        k = s.kind.name
        if k == 'Block': run(s.body, env)
        elif k == 'List':
            for x in s.list: run(x, env)
        elif k == 'VariableDeclaration':
            v = s.symbol
            if v.initializer is not None:
                try: env[v.name] = ev(v.initializer, env)
                except (Unknown, KeyError, TypeError, ValueError): pass
        elif k == 'ExpressionStatement':
            assign(s.expr, env)
        elif k == 'Conditional':
            try: c = ev(s.conditions[0].expr, env)
            except (Unknown, KeyError, TypeError, ValueError): c = None
            if c is None:
                run(s.ifTrue, dict(env))
                if s.ifFalse is not None: run(s.ifFalse, dict(env))
            elif c: run(s.ifTrue, env)
            elif s.ifFalse is not None: run(s.ifFalse, env)
        elif k == 'ForLoop':
            env = dict(env)
            try:
                for v in s.loopVars: env[v.name] = ev(v.initializer, env)
                for i in s.initializers: assign(i, env)
                while ev(s.stopExpr, env):
                    budget[0] -= 1
                    if budget[0] < 0: raise Unknown('iteration budget')
                    run(s.body, env)
                    for st in s.steps: step(st, env)
            except (Unknown, KeyError, TypeError, ValueError):
                return
        elif k == 'RepeatLoop':
            try: n = ev(s.count, env)
            except (Unknown, KeyError, TypeError, ValueError): return
            for _ in range(min(int(n), max_iter)): run(s.body, env)
    run(body, {})


# ======================================================================== extraction
def option_entries(sym):
    """option.x = v / type_option.x = v set on a coverpoint or cross"""
    out = []
    try:
        for o in sym.options:
            x = o.expression
            if x is not None and x.kind.name == 'Assignment': x = x.right
            v = const_str(x) if x is not None else None
            if v is None and x is not None and x.kind.name in LITERALS:
                v = str(x.value)
            out.append({'name': o.name, 'type_option': bool(o.isTypeOption), 'value': v,
                        'src': text(x) if x is not None else None})
    except Exception:
        pass
    return out


def syn_value(x, scope):
    """Constant value of a transition item written in syntax: a literal, or a name (enum value,
    parameter) looked up in the bin's scope. Unresolved items keep their source text."""
    src = ' '.join(str(x).split())
    if x.kind.name in ('IdentifierName', 'ScopedName'):
        try:
            sym = scope.lookupName(src)
            if sym is not None and sym.kind.name in ('EnumValue', 'Parameter'):
                return str(sym.value)
        except Exception:
            pass
        return {'src': src}
    if x.kind.name in ('IntegerLiteralExpression', 'IntegerVectorExpression', 'UnbasedUnsizedLiteralExpression'):
        return src
    return {'src': src}


def trans_set(st, scope):
    """(a, b => c [*2:3] => d [-> 1]) as a list of steps {values, repeat}"""
    steps = []
    for r in st.ranges:
        if getattr(r, 'kind', None) is None or r.kind.name != 'TransRange': continue    # => tokens
        vals = []
        for it in r.items:
            if not hasattr(it, 'kind') or it.kind.name == 'Comma': continue
            if it.kind.name in ('ValueRangeExpression', 'OpenRangeExpression'):
                vals.append([syn_value(it.left, scope), syn_value(it.right, scope)])
            else:
                vals.append(syn_value(it, scope))
        step = {'values': vals}
        rp = r.repeat
        if rp is not None:
            kind = {'Star': '*', 'MinusArrow': '->', 'Equals': '='}.get(rp.specifier.kind.name, str(rp.specifier))
            sel = rp.selector
            if sel is None: lo = hi = None
            elif sel.kind.name == 'BitSelect': lo = hi = syn_value(sel.expr, scope)
            else: lo, hi = syn_value(sel.left, scope), syn_value(sel.right, scope)
            step['repeat'] = {'kind': kind, 'from': lo, 'to': hi}
        steps.append(step)
    return steps


class Extractor:
    def __init__(self, comp, loc, ser, extra_pkgs, hier_depth, skip_pkgs=()):
        self.comp, self.loc, self.ser = comp, loc, ser
        self.extra_pkgs = set(extra_pkgs)
        self.skip_pkgs = set(skip_pkgs)
        self.hier_depth = hier_depth
        self.classes, self.class_by_path = [], {}
        self.sub_syms, self.proc_syms = [], []
        self.covergroups, self.assertions = [], []
        self.types = {'parameters': [], 'typedefs': [], 'enums': []}
        self.imported_pkgs = set()
        self.default_specs = set()      # (path, line, name) of classes reported by default specialization
        self.callees = {}               # path -> subroutine called from an own constraint
        self.seen = set()

    # ---- symbol location helpers
    def where(self, sym):
        try: return self.loc.resolve(sym.location)
        except Exception: return ('', 0, None)

    def own_sym(self, sym):
        p, _, _ = self.where(sym)
        return self.loc.is_own(p)

    def span(self, sym):
        if sym.syntax is not None: return self.loc.span(sym.syntax.sourceRange)
        p, l, m = self.where(sym)
        return {'file': self.loc.rel(p), 'start': l, 'end': l, **({'macro': m} if m else {})}

    # ---- pass 1: find everything
    def collect(self):
        root = self.comp.getRoot()
        # packages imported by own code (syntax-level, so it works even for unused imports)
        for tree in self.comp.getSyntaxTrees():
            def sv(n):
                if getattr(n, 'kind', None) == SK.PackageImportItem:
                    try:
                        loc = n.sourceRange.start
                        if self.loc.is_own(self.loc.resolve(loc)[0]):
                            self.imported_pkgs.add(str(n.package).strip())
                    except Exception:
                        pass
                return A.VisitAction.Advance if hasattr(A, 'VisitAction') else None
            try: tree.root.visit(sv)
            except Exception: pass
        wanted_pkgs = (self.imported_pkgs - self.skip_pkgs) | self.extra_pkgs

        def visit(s):
            k = s.kind.name
            if k == 'Package' and s.name in wanted_pkgs:
                self.type_scope(s, s.name)
            sl = getattr(s, 'location', None)
            if not sl: return A.VisitAction.Advance
            path, line, macro = self.loc.resolve(sl)
            if not self.loc.is_own(path): return A.VisitAction.Advance
            key = (k, path, line, s.name)
            if k in ('Parameter', 'TypeAlias') and key not in self.seen and s.syntax is not None and not macro:
                self.seen.add(key)
                self.type_entry(s, path, line)
            elif k == 'ClassType':
                if key in self.seen: return A.VisitAction.Advance
                self.seen.add(key)
                self.classes.append(s)
            elif k == 'GenericClassDef':
                if key not in self.seen:
                    self.seen.add(key); generics.append((s, path, line))
            elif k == 'Subroutine' and s.syntax is not None:
                keep(self.sub_syms, key, s)
            elif k == 'ProceduralBlock':
                keep(self.proc_syms, key, s)
            elif k == 'CovergroupType':
                keep(self.covergroups, key, s)
            if k in ('ClassProperty', 'Variable', 'TypeAlias'):
                follow_specialization(s)
            return A.VisitAction.Advance

        def follow_specialization(s):
            # the visitor does not enter class specializations reached only through a type
            # (typedef pred_t = predictor#(cfg); pred_t pred;): visit own ones it would miss
            try:
                t = s.targetType.type if s.kind.name == 'TypeAlias' else s.type
                ct = t.canonicalType
                if ct.kind.name != 'ClassType' or ct.genericClass is None: return
                p, l, _ = self.loc.resolve(ct.location)
            except Exception:
                return
            if self.loc.is_own(p) and ('ClassType', p, l, ct.name) not in self.seen:
                ct.visit(visit)

        first, generics = {}, []

        def keep(lst, key, s):
            # one entry per source construct (generate loops, binds and classes repeat them); an
            # elaborated copy wins over one in an uninstantiated scope (a bind into an inactive branch)
            if key not in first:
                first[key] = len(lst); lst.append(s)
            elif uninstantiated(lst[first[key]]) and not uninstantiated(s):
                lst[first[key]] = s
        root.visit(visit)
        # a parameterized class nothing specializes has no members in the elaborated design: report
        # its default specialization instead (when every parameter has a default)
        default_spec = A.GenericClassDefSymbol.__dict__['defaultSpecialization'].fget
        for g, path, line in generics:
            if ('ClassType', path, line, g.name) in self.seen: continue
            try: ds = default_spec(g, g.parentScope)
            except Exception: ds = None
            if ds is None or ds.kind.name != 'ClassType': continue
            self.default_specs.add((path, line, g.name))
            ds.visit(visit)

    # ---- types
    def type_entry(self, s, path, line):
        rel = self.loc.rel(path)
        if s.kind.name == 'Parameter':
            self.types['parameters'].append({'name': s.name, 'path': s.hierarchicalPath, 'file': rel,
                                             'line': line, 'local': s.isLocalParam, 'type': str(s.type),
                                             'value': str(s.value)})
            return
        t = s.targetType.type
        self.types['typedefs'].append({'name': s.name, 'path': s.hierarchicalPath, 'file': rel, 'line': line,
                                       'type': str(t), 'width': t.bitWidth if t.isIntegral else None})
        ct = t.canonicalType
        if ct.isEnum:
            vals = [{'name': m.name, 'value': str(m.value)} for m in ct if m.kind.name == 'EnumValue']
            self.types['enums'].append({'name': s.name, 'base': str(ct.baseType), 'width': ct.bitWidth,
                                        'values': vals})

    def type_scope(self, pkg, pname):
        for m in pkg:
            if m.kind.name in ('Parameter', 'TypeAlias'):
                p, l, _ = self.where(m)
                key = (m.kind.name, p, l, m.name)
                if key in self.seen: continue
                self.seen.add(key)
                self.type_entry(m, p, l)

    # ---- classes
    def class_entry(self, s):
        chain, c = [], s
        while getattr(c, 'baseClass', None) is not None:
            c = c.baseClass
            chain.append(c.name)
        role = next((r for r in UVM_ROLES if r in chain), None)
        sp = self.span(s)
        gen = s.genericClass
        ent = {'name': s.name, 'path': s.hierarchicalPath, **sp,
               'specialization_of': gen.name if gen is not None else None,
               'base': chain[0] if chain else None,
               'base_type': str(s.baseClass) if s.baseClass is not None else None,
               'lineage': chain, 'uvm_role': role, 'abstract': s.isAbstract,
               'macro_generated': 'macro' in sp,
               'default_specialization': (self.where(s)[0], self.where(s)[1], s.name) in self.default_specs,
               'implements': [str(i) for i in s.implementedInterfaces] if hasattr(s, 'implementedInterfaces') else [],
               'parameters': [], 'properties': [], 'constraints': [], 'methods': [], 'covergroups': []}
        for m in s:
            mk = m.kind.name
            if mk == 'Parameter':
                ent['parameters'].append({'name': m.name, 'value': str(m.value), 'type': str(m.type)})
            elif mk == 'TypeParameter':
                ent['parameters'].append({'name': m.name, 'type_param': str(m.targetType.type)})
            elif mk == 'ClassProperty':
                ent['properties'].append(self.prop_entry(m))
            elif mk == 'ConstraintBlock':
                ent['constraints'].append(self.constraint_entry(m))
            elif mk == 'Subroutine' and m.syntax is not None:   # skip built-ins (randomize, pre_randomize...)
                ent['methods'].append(m.name)
            elif mk == 'CovergroupType':
                ent['covergroups'].append(m.name)
        return ent

    def prop_entry(self, m):
        p, l, mac = self.where(m)
        t = m.type
        prop = {'name': m.name, 'line': l, 'type': str(t), 'ti': type_info(t), 'rand': enum_name(m.randMode),
                'width': t.bitWidth if t.isIntegral else None,
                'static': enum_name(m.lifetime) == 'Static', 'visibility': enum_name(m.visibility)}
        ct = t.canonicalType
        if ct.isEnum: prop['enum_values'] = [e.name for e in ct if e.kind.name == 'EnumValue']
        if is_vif_type(t): prop['vif'] = True
        if t.isClass: prop['handle'] = True
        if t.isAssociativeArray: prop['assoc_index'] = str(t.associativeIndexType) if t.associativeIndexType else '*'
        if t.isQueue: prop['queue'] = True
        if t.isDynamicallySizedArray: prop['dynamic_array'] = True
        if t.isUnpackedArray and t.hasFixedRange: prop['unpacked_range'] = str(t.fixedRange)
        if m.initializer is not None:
            prop['init'] = text(m.initializer)
            c = const_str(m.initializer)
            if c is not None: prop['init_value'] = c
        if mac: prop['macro'] = mac
        return prop

    def note_callees(self, n):
        """user functions a constraint calls: their bodies are reported even outside the own
        sources, so a consumer can evaluate the call (get_n_bits() in an agent package, ...)"""
        def fn(x):
            if x.kind.name == 'Call' and not x.isSystemCall and isinstance(x.subroutine, A.Symbol):
                sr = x.subroutine
                self.callees.setdefault(sr.hierarchicalPath, sr)

        def con(c, depth=0):        # constraints have no visitor: walk them, visit their expressions
            if depth > 32 or c is None: return
            if isinstance(c, A.Expression):
                try: c.visit(lambda x: (fn(x), A.VisitAction.Advance)[1])
                except Exception: pass
                return
            if isinstance(c, A.Statement):
                try: walk(c, fn)
                except Exception: pass
                return
            for a in ('list', 'expr', 'predicate', 'body', 'ifBody', 'elseBody', 'items', 'arrayRef'):
                try: v = getattr(c, a)
                except Exception: continue
                if v is None or callable(v): continue
                if isinstance(v, (A.Expression, A.Constraint)): con(v, depth + 1)
                else:
                    try:
                        for x in v: con(x, depth + 1)
                    except TypeError: pass
        con(n)

    def constraint_entry(self, m):
        cs = m.constraints
        items = list(cs.list) if cs is not None and cs.kind.name == 'List' else ([cs] if cs is not None else [])
        ent = {'name': m.name, **self.span(m), 'items': []}
        flags = int(m.flags)
        F = A.ConstraintBlockFlags
        ent['static'] = bool(flags & int(F.Static))
        if flags & int(F.Pure): ent['pure'] = True
        if flags & int(F.Extern) and cs is not None and cs.syntax is not None:
            ent['extern'] = True            # span above is the prototype; the body is out of class
            ent['body'] = self.loc.span(cs.syntax.sourceRange)
        for it in items:
            d = self.ser.node(it)
            ent['items'].append(d)
            self.note_callees(it)
        ent['item_kinds'] = [it.kind.name for it in items]
        return ent

    # ---- subroutines / procedures
    def sub_entry(self, s):
        sp = self.span(s)
        path = s.hierarchicalPath
        owner = path.rsplit('::', 1)[0] if '::' in path else (path.rsplit('.', 1)[0] if '.' in path else '')
        owner_cls = self.class_by_path.get(owner)
        ent = {'name': s.name, 'owner': owner_cls['name'] if owner_cls else owner, 'path': path,
               'owner_is_class': owner_cls is not None, 'kind': enum_name(s.subroutineKind),
               'virtual': s.isVirtual, **sp, 'macro_generated': 'macro' in sp,
               'returns': str(s.returnType),
               'args': [{'name': a.name, 'dir': enum_name(a.direction), 'type': str(a.type),
                         'default': text(a.defaultValue) if getattr(a, 'defaultValue', None) is not None else None}
                        for a in s.arguments]}
        if uninstantiated(s):
            ent['uninstantiated'] = True
        elif not ent['macro_generated']:
            ent['facts'] = analyse(s.body, self.loc, self.ser)
            ent['body'] = self.ser.node(s.body) if s.body is not None else None
            ent['facts']['invalid'] = invalid_nodes(ent['body'])
        return ent

    def proc_entry(self, s):
        sp = self.span(s)
        try: scope = s.parentScope.containingInstance.hierarchicalPath
        except Exception: scope = ''
        ent = {'procedure': enum_name(s.procedureKind), 'scope': scope, **sp}
        if uninstantiated(s):           # e.g. inside a generate-if whose condition is false: not bound
            return {**ent, 'uninstantiated': True, 'facts': {**analyse(None, self.loc, self.ser), 'invalid': []}}
        body = self.ser.node(s.body)
        facts = analyse(s.body, self.loc, self.ser)
        facts['invalid'] = invalid_nodes(body)
        return {**ent, 'facts': facts, 'body': body}

    # ---- coverage
    def covergroup_entry(self, cg):
        sp = self.span(cg)
        name = cg.name                      # the covergroup type symbol is anonymous; the name is in the syntax
        if not name and cg.syntax is not None and getattr(cg.syntax, 'name', None) is not None:
            name = str(cg.syntax.name).strip()
        ent = {'name': name, 'path': cg.hierarchicalPath, **sp, 'uninstantiated': uninstantiated(cg),
               'sample_event': None,
               'sample_args': [], 'options': [], 'coverpoints': [], 'crosses': []}
        syn = cg.syntax
        if syn is not None:
            ev_syn = getattr(syn, 'event', None)
            if ev_syn is not None: ent['sample_event'] = ' '.join(str(ev_syn).split())
        body = getattr(cg, 'body', None)
        members = list(body) if body is not None else list(cg)
        for m in members:
            mk = m.kind.name
            if mk == 'FormalArgument':
                ent['sample_args'].append({'name': m.name, 'type': str(m.type), 'ti': type_info(m.type)})
            elif mk == 'Coverpoint':
                ent['coverpoints'].append(self.coverpoint_entry(m))
            elif mk == 'CoverCross':
                ent['crosses'].append(self.cross_entry(m))
        if cg.syntax is not None:
            for opt in re.findall(r'(?:type_)?option\.\w+\s*=\s*[^;]+;', str(cg.syntax)):
                if opt not in ent['options']: ent['options'].append(' '.join(opt.split()))
        return ent

    def bins_entry(self, b):
        e = {'name': b.name, 'kind': enum_name(b.binsKind), 'array': b.isArray, 'default': b.isDefault,
             'default_sequence': b.isDefaultSequence, 'wildcard': b.isWildcard, **self.span(b)}
        for a in ('values', 'numberOfBinsExpr', 'setCoverageExpr', 'withExpr', 'iffExpr', 'crossSelectExpr'):
            try: v = getattr(b, a)
            except Exception: continue
            sv = self.ser._val(v, set(), 0)
            if sv not in (None, [], {}): e[a] = sv
        e['src'] = text(b, 400)
        # compact value set: [lo, hi] for ranges, a value otherwise (None = not constant)
        try:
            vals = list(b.values or [])
        except Exception:
            vals = []
        if vals:
            def one(v):
                while v.kind.name == 'Conversion': v = v.operand
                if v.kind.name == 'ValueRange':      # '$' (unbounded) is not a constant
                    return [const_str(v.left) or text(v.left), const_str(v.right) or text(v.right)]
                return const_str(v) or text(v)
            e['ranges'] = [one(v) for v in vals]
        if '=>' in e['src']: e['transition'] = True
        init = getattr(b.syntax, 'initializer', None) if b.syntax is not None else None
        if init is not None and init.kind.name == 'TransListCoverageBinInitializer':
            e['transition'] = True
            e['trans'] = [trans_set(st, b.parentScope) for st in init.sets
                          if getattr(st, 'kind', None) is not None and st.kind.name == 'TransSet']
        return e

    def coverpoint_entry(self, cp):
        # the coverpoint's own type drops the enum; auto bins of an enum need its values
        x = cp.coverageExpr
        while x is not None and x.kind.name == 'Conversion': x = x.operand
        ti_src = x.type if x is not None and x.type.isEnum else cp.type
        # an unlabelled coverpoint's implicit name is its expression text, leading trivia included
        e = {'name': cp.name.strip(), **self.span(cp), 'type': str(cp.type), 'ti': type_info(ti_src),
             'width': cp.type.bitWidth if cp.type.isIntegral else None,
             'options': option_entries(cp),
             'expr': self.ser.node(cp.coverageExpr) if cp.coverageExpr is not None else None,
             'iff': self.ser.node(cp.iffExpr) if cp.iffExpr is not None else None,
             'bins': [], 'auto_bins': True}
        for b in cp:
            if b.kind.name == 'CoverageBin':
                e['bins'].append(self.bins_entry(b)); e['auto_bins'] = False
        return e

    def cross_entry(self, cx):
        e = {'name': cx.name, **self.span(cx), 'targets': [t.name for t in cx.targets],
             'iff': self.ser.node(cx.iffExpr) if cx.iffExpr is not None else None, 'bins': [],
             'options': option_entries(cx)}
        def bins_in(scope):          # cross bins live in a nested CoverCrossBody scope
            for b in scope:
                if b.kind.name == 'CoverageBin': e['bins'].append(self.bins_entry(b))
                elif b.kind.name == 'CoverCrossBody': bins_in(b)
        bins_in(cx)
        return e

    def assertion_items(self, procs, subs):
        """cover/assert/assume/restrict property statements and immediate assertions in own code."""
        out = []

        def collect(body, where):
            def fn(x):
                k = x.kind.name
                if k in ('ConcurrentAssertion', 'ImmediateAssertion'):
                    out.append({'statement': k, 'assertion_kind': enum_name(x.assertionKind), **where,
                                'line': self.loc.line(x.sourceRange), 'src': text(x, 400),
                                'ast': self.ser.node(x)})
            walk(body, fn)
        for s in procs:
            if s.body is not None and not uninstantiated(s): collect(s.body, {'in': 'procedure'})
        for s in subs:
            if s.body is not None and not uninstantiated(s): collect(s.body, {'in': s.hierarchicalPath})
        return out

    def counter_coverage(self, class_ents, sub_syms):
        """Heuristic: hand-rolled coverage kept in associative-array counters.
        Bins = keys tested with <counter>.exists(key) while executing constant-bounded loops;
        samples = <counter>[key]++ updates; totals = denominators printed as "%d/%d"."""
        by_owner = {}
        for s in sub_syms:
            by_owner.setdefault(s.hierarchicalPath.rsplit('::', 1)[0], []).append(s)
        out = []
        for c in class_ents:
            counters = {p['name'] for p in c['properties'] if p.get('assoc_index')}
            if not counters: continue
            ent = {'class': c['name'], 'file': c['file'], 'counters': [], 'updates': [], 'bins': {}, 'totals': []}
            for s in by_owner.get(c['path'], []):
                if s.body is None: continue

                def upd(x, s=s):
                    k = x.kind.name
                    if k == 'UnaryOp' and 'increment' in enum_name(x.op).lower() and x.operand.kind.name == 'ElementSelect':
                        root, _ = strip_lvalue(x.operand)
                        if root.kind.name == 'NamedValue' and root.symbol.name in counters:
                            ent['updates'].append({'subroutine': s.name, 'counter': root.symbol.name,
                                                   'key': self.ser.node(x.operand.selector),
                                                   'line': self.loc.line(x.sourceRange)})
                    if k == 'Call' and x.subroutineName in ('$sformatf', '$psprintf'):
                        args = list(x.arguments)
                        f = const_str(args[0]) if args else None
                        if f and re.search(r'%0?d\s*/\s*%0?d', f):
                            ent['totals'].append({'subroutine': s.name, 'format': f.strip('"'),
                                                  'hit_expr': text(args[-2]) if len(args) >= 3 else None,
                                                  'total_expr': text(args[-1]),
                                                  'total': try_ev(args[-1]),
                                                  'line': self.loc.line(x.sourceRange)})
                n_before = len(ent['updates'])
                walk(s.body, upd)
                if len(ent['updates']) > n_before:
                    # the sampling routine: keep its local derivations (key = f(sampled item))
                    locs = []

                    def decl(x):
                        if x.kind.name == 'VariableDeclaration' and x.symbol.initializer is not None:
                            v = x.symbol
                            locs.append({'name': v.name, 'type': str(v.type), 'src': text(v.initializer),
                                         'line': self.loc.line(x.sourceRange), 'ast': self.ser.node(v.initializer)})
                    walk(s.body, decl)
                    ent.setdefault('sample', []).append(
                        {'subroutine': s.name, 'args': [{'name': a.name, 'type': str(a.type)} for a in s.arguments],
                         'locals': locs})

                def on_stmt(st, env, s=s):
                    if st.kind.name != 'Conditional': return
                    cond = st.conditions[0].expr
                    if cond.kind.name == 'UnaryOp': cond = cond.operand
                    if cond.kind.name != 'Call' or cond.subroutineName != 'exists': return
                    args = list(cond.arguments)
                    if len(args) < 2: return
                    root, _ = strip_lvalue(args[0])
                    name = root.symbol.name if root.kind.name == 'NamedValue' else text(args[0])
                    try:
                        key = ev(args[-1], env)
                        ent['bins'].setdefault(name, []).append(key)
                    except (Unknown, KeyError, TypeError, ValueError): pass
                try: run_const_loops(s.body, on_stmt)
                except (Unknown, KeyError, TypeError, ValueError, RecursionError):
                    pass
            for p in c['properties']:
                if p['name'] in counters:
                    ent['counters'].append({'name': p['name'], 'index': p['assoc_index'], 'type': p['type'],
                                            'line': p['line']})
            ent['bins'] = {k: {'count': len(dict.fromkeys(v)), 'names': list(dict.fromkeys(v))}
                           for k, v in ent['bins'].items()}
            if ent['updates'] or ent['bins']:
                out.append(ent)
        return out

    # ---- hierarchy
    def instance(self, i, depth):
        own = self.loc.is_own(self.where(i.definition)[0])     # where the module is written, not instantiated
        ent = {'name': i.name, 'definition': i.definition.name,
               'kind': 'interface' if i.isInterface else 'module', 'own': own,
               'params': [], 'ports': [], 'connections': []}
        body = i.body
        signals, modports, children = [], [], []
        for m in body:
            mk = m.kind.name
            if mk == 'Parameter':
                ent['params'].append({'name': m.name, 'value': str(m.value), 'local': m.isLocalParam})
            elif mk == 'Port':
                t = m.type
                ent['ports'].append({'name': m.name, 'dir': enum_name(m.direction), 'type': str(t),
                                     'width': t.bitWidth if t.isIntegral else None})
            elif mk == 'InterfacePort':
                ent['ports'].append({'name': m.name, 'interface': str(m.interfaceDef.name) if m.interfaceDef else None,
                                     'modport': m.modport})
            elif mk in ('Variable', 'Net'):
                t = m.type
                signals.append({'name': m.name, 'type': str(t), 'width': t.bitWidth if t.isIntegral else None,
                                'init': text(m.initializer) if getattr(m, 'initializer', None) is not None else None})
            elif mk == 'Modport':
                modports.append({'name': m.name, 'ports': [{'name': p.name, 'dir': enum_name(p.direction)}
                                                           for p in m if p.kind.name == 'ModportPort']})
            elif mk == 'Instance' and depth < self.hier_depth:
                children.append(self.instance(m, depth + 1))
            elif mk == 'InstanceArray' and depth < self.hier_depth:
                for el in m:
                    if el.kind.name == 'Instance': children.append(self.instance(el, depth + 1))
        for pc in i.portConnections:
            e = pc.expression
            ent['connections'].append({'port': pc.port.name, 'expr': text(e) if e is not None else None})
        if i.isInterface or own:
            ent['signals'] = signals
        if modports: ent['modports'] = modports
        if children: ent['children'] = children
        return ent

    # ---- assemble
    def run(self):
        self.collect()
        class_ents = []
        for s in self.classes:
            ent = self.class_entry(s)
            class_ents.append(ent)
            self.class_by_path[s.hierarchicalPath] = ent
        subs = [self.sub_entry(s) for s in self.sub_syms]
        have = {s['path'] for s in subs}
        for _ in range(4):                  # functions called by constraints, and what they call
            extra = [sr for p, sr in list(self.callees.items()) if p not in have]
            if not extra: break
            for sr in extra:
                have.add(sr.hierarchicalPath)
                ent = self.sub_entry(sr)
                ent['external'] = not self.own_sym(sr)
                subs.append(ent)
                if sr.body is not None: self.note_callees(sr.body)
        procs = [self.proc_entry(s) for s in self.proc_syms]

        # randomization inventory
        rand = []
        for c in class_ents:
            rp = [p['name'] for p in c['properties'] if p['rand'] in ('Rand', 'RandC')]
            if rp or c['constraints']:
                rand.append({'kind': 'class', 'class': c['name'], 'file': c['file'], 'rand_properties': rp,
                             'constraint_blocks': [b['name'] for b in c['constraints']],
                             'pre_randomize': 'pre_randomize' in c['methods'],
                             'post_randomize': 'post_randomize' in c['methods']})
        for s in subs:
            for r in s.get('facts', {}).get('rand_sites', []):
                rand.append({**r, 'owner': s['owner'], 'subroutine': s['name'], 'file': s['file']})
        for p in procs:
            for r in p['facts']['rand_sites']:
                rand.append({**r, 'owner': p['scope'], 'procedure': p['procedure'], 'file': p['file']})

        # stimulus: who drives / samples which interface signals
        stim = {}
        for s in subs + procs:
            f = s.get('facts')
            if not f: continue
            who = s['owner'] if 'owner' in s else f"{s['scope']}:{s['procedure']}@{s['start']}"
            d = stim.setdefault(who, {'drives': {}, 'samples': {}, 'timing': 0, 'rand_sites': 0})
            for w in f['vif_writes']:
                d['drives'].setdefault(f"{w['iface']}.{w['signal']}", []).append(
                    {'in': s.get('name', s.get('procedure')), 'line': w['line'], 'nonblocking': w['nonblocking']})
            for w in f['signal_writes']:
                d['drives'].setdefault(w['path'], []).append(
                    {'in': s.get('name', s.get('procedure')), 'line': w['line'], 'nonblocking': w['nonblocking']})
            for r in f['vif_reads']:
                d['samples'].setdefault(f"{r['iface']}.{r['signal']}", []).append(
                    {'in': s.get('name', s.get('procedure')), 'line': r['line']})
            d['timing'] += len(f['timing']); d['rand_sites'] += len(f['rand_sites'])
        stim = {k: v for k, v in stim.items() if v['drives'] or v['samples']}

        # config: property defaults + constant writes made through handles (a.b.c = K)
        defaults = []
        for c in class_ents:
            for p in c['properties']:
                if 'init' in p and not p.get('macro') and not p.get('handle'):
                    defaults.append({'class': c['name'], 'name': p['name'], 'type': p['type'], 'rand': p['rand'],
                                     'init': p['init'], 'value': p.get('init_value'), 'file': c['file'],
                                     'line': p['line']})
        overrides = []
        for s in subs:
            for w in s.get('facts', {}).get('field_writes', []):
                if w.get('via_handle') and w['rhs_const'] is not None:
                    overrides.append({'owner': s['owner'], 'subroutine': s['name'], 'path': w['path'],
                                      'field': w.get('field'), 'value': w['rhs_const'], 'src': w['src'],
                                      'file': s['file'], 'line': w['line']})

        coverage = {'covergroups': [self.covergroup_entry(cg) for cg in self.covergroups],
                    'assertions': self.assertion_items(self.proc_syms, self.sub_syms),
                    'counter_coverage_heuristic': self.counter_coverage(class_ents, self.sub_syms)}
        for cg in coverage['covergroups']:
            # the owning class is the innermost class whose source span contains the covergroup
            owners = [c for c in class_ents if c['file'] == cg['file'] and c['start'] <= cg['start']
                      and cg['end'] <= c['end'] and not c['macro_generated']]
            cg['owner'] = min(owners, key=lambda c: c['end'] - c['start'])['name'] if owners else None

        hierarchy = [self.instance(t, 0) for t in self.comp.getRoot().topInstances]
        return {'types': self.types, 'imported_packages': sorted(self.imported_pkgs),
                'classes': class_ents, 'subroutines': subs, 'procedures': procs,
                'randomization': rand, 'stimulus': stim,
                'config': {'defaults': defaults, 'overrides': overrides},
                'coverage': coverage, 'hierarchy': hierarchy}


def main():
    argv = sys.argv[1:]
    if '--' in argv:
        i = argv.index('--'); ours, slang_args = argv[:i], argv[i + 1:]
    else:
        ours, slang_args = argv, []
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--own', action='append', default=[], help='testbench source dir/file (repeatable)')
    ap.add_argument('--pkg', action='append', default=[], help='extra package to report types from')
    ap.add_argument('--skip-pkg', action='append', default=None, help='package never to report (default uvm_pkg, ovm_pkg)')
    ap.add_argument('--hier-depth', type=int, default=2)
    ap.add_argument('--base', default=None, help='paths in the output are relative to this (default: first --own)')
    ap.add_argument('-o', '--output', default='sniper_out.json')
    a = ap.parse_args(ours)
    if not slang_args:
        ap.error('give the slang arguments after --')
    own = a.own or [os.getcwd()]
    base = os.path.realpath(a.base or (own[0] if os.path.isdir(own[0]) else os.path.dirname(own[0])))
    sys.setrecursionlimit(20000)

    T0 = time.time()
    drv, comp, diags, cmd, tparse, telab = compile_design(slang_args)
    sm = drv.sourceManager
    loc = Loc(sm, own, base)
    ser = Ser(loc)
    global SM
    SM = sm
    errs, own_diags = [], []
    for d in diags:
        if not d.location: continue
        p, l, _ = loc.resolve(d.location)
        rec = {'file': loc.rel(p), 'line': l, 'error': d.isError(), 'code': str(d.code),
               'args': [str(x) for x in d.args][:4]}
        if d.isError(): errs.append(rec)
        if loc.is_own(p): own_diags.append(rec)
    T1 = time.time()
    data = Extractor(comp, loc, ser, a.pkg, a.hier_depth,
                     a.skip_pkg if a.skip_pkg is not None else ['uvm_pkg', 'ovm_pkg']).run()
    T2 = time.time()
    subs = data['subroutines']
    meta = {'tool': 'sniper', 'pyslang': pyslang.__version__, 'command': cmd,
            'own': [os.path.realpath(o) for o in own], 'paths_relative_to': base,
            'parse_s': round(tparse, 3), 'elaborate_s': round(telab, 3), 'extract_s': round(T2 - T1, 3),
            'diagnostics': len(diags), 'errors': len(errs), 'error_list': errs[:50],
            'own_diagnostics': own_diags,
            'counts': {'classes': len(data['classes']),
                       'constraint_blocks': sum(len(c['constraints']) for c in data['classes']),
                       'rand_properties': sum(1 for c in data['classes'] for p in c['properties']
                                              if p['rand'] in ('Rand', 'RandC')),
                       'subroutines': len(subs),
                       'subroutines_with_body': sum(1 for s in subs if 'body' in s),
                       'procedures': len(data['procedures']),
                       'randomization_sites': len(data['randomization']),
                       'covergroups': len(data['coverage']['covergroups']),
                       'assertions': len(data['coverage']['assertions']),
                       'counter_coverage_classes': len(data['coverage']['counter_coverage_heuristic']),
                       'config_overrides': len(data['config']['overrides']),
                       'invalid_code': sum(len(x.get('facts', {}).get('invalid', []))
                                           for x in subs + data['procedures'])}}
    out = {'meta': meta, **data}
    with open(a.output, 'w') as f:
        json.dump(out, f, indent=1, default=str)
    print(json.dumps(meta['counts']))
    print(f"parse {tparse:.2f}s  elaborate {telab:.2f}s  extract {T2 - T1:.2f}s  "
          f"errors {len(errs)}  diagnostics in own files {len(own_diags)}  -> {a.output}")


if __name__ == '__main__':
    main()
