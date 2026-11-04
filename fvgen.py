#!/usr/bin/env python3
"""fvgen: formal collateral from a sniper extraction -- constraints become assumes, covergroups become
cover properties -- for Yosys (read_verilog -sv -formal) and SymbiYosys.

Usage
    fvgen.py SNIPER_JSON -o OUTDIR [options]

Generated (OUTDIR)
    fv_constraints.sv  per class with constraints:
                         fv_c_<class>   constraint checker: rand fields and the state they read are
                                        inputs; every constraint item is a wire and an ASSUME while `en`
                         fv_r_<class>   randomize() model: anyseq next values, latched on `randomize`,
                                        constrained by fv_c_<class> in that cycle
                       per randomize() call site with inline constraints or an argument list:
                         fv_c_<class>__<site> / fv_r_<class>__<site>
    fv_covers.sv       per covergroup: fv_cg_<owner>__<cg> with one COVER per bin, sampled on
                       `sample` (or the covergroup's clocking event), illegal bins as ASSERTs
    fv_bind.sv         instantiation templates for covergroups declared in modules/interfaces
    smoke/             one formal top + .sby per generated module (constraints satisfiable, every
                       bin reachable in a free environment); `--run` runs them
    report.json/.md    every constraint item and bin: what it became, exact/approx/unsupported, why
    prim_assert.sv, prim_assert_yosys_macros.svh   the assertion macros the modules use

Semantics follow IEEE 1800-2017 clauses 18 (constraints) and 19 (coverage); the report states every
place where the formal model differs from simulation and in which direction.
"""
import argparse, json, math, os, re, shutil, signal, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.realpath(__file__))


class Unsupported(Exception):
    pass


# ======================================================================== constants
class K:
    """A constant: value (int, two's complement interpretation applied when signed), width, signed,
    xmask (bits written as x/z/?: don't-care in wildcard comparisons)."""
    __slots__ = ('v', 'w', 's', 'x')

    def __init__(self, v, w=None, s=False, x=0):
        self.v, self.w, self.s, self.x = v, w, s, x

    def __repr__(self):
        return f'K({self.v},{self.w},{self.s},{self.x:#x})'


def parse_const(s):
    """slang's constant text ("32'd80", "2'b1x", "8'sh_ff", "-5", "\"str\"") -> K, or None."""
    if s is None: return None
    s = str(s).strip()
    if s.startswith('"'): return None
    neg = s.startswith('-')
    if neg: s = s[1:]
    m = re.fullmatch(r"(\d+)?'(s?)([dDbBoOhH])([0-9a-fA-FxXzZ?_]+)", s)
    if m:
        w = int(m.group(1)) if m.group(1) else None
        sg = bool(m.group(2))
        base = {'d': 10, 'b': 2, 'o': 8, 'h': 16}[m.group(3).lower()]
        digs = m.group(4).replace('_', '')
        x = 0
        if re.search(r'[xXzZ?]', digs):
            if base == 10:
                if not re.fullmatch(r'[xXzZ?]', digs): return None
                return K(0, w, sg, (1 << (w or 1)) - 1)
            bpd = {2: 1, 8: 3, 16: 4}[base]
            v = 0
            for ch in digs:
                v <<= bpd; x <<= bpd
                if ch in 'xXzZ?': x |= (1 << bpd) - 1
                else: v |= int(ch, base)
        else:
            v = int(digs, base)
        if neg: v = -v
        if w is not None:
            v &= (1 << w) - 1; x &= (1 << w) - 1
            if sg and v >> (w - 1): v -= 1 << w
        return K(v, w, sg, x)
    try:
        v = int(s)
        return K(-v if neg else v, None, True)
    except ValueError:
        return None


def lit(v, w, signed):
    """Verilog literal of value v at width w (two's complement digits for negative values)."""
    if w is None or w <= 0: w = 32
    u = v & ((1 << w) - 1)
    return f"{w}'{'s' if signed else ''}h{u:x}"


def kval(k, w, signed):
    """value of constant k after conversion to w bits / signedness (SV extension rules)"""
    v = k.v
    if k.w is not None and w is not None and w < k.w: v &= (1 << w) - 1
    if w is not None:
        v &= (1 << w) - 1
        if signed and v >> (w - 1): v -= 1 << w
    return v


# ======================================================================== naming
SV_KEYWORDS = set('''accept_on alias always always_comb always_ff always_latch and assert assign assume
automatic before begin bind bins binsof bit break buf bufif0 bufif1 byte case casex casez cell chandle
checker class clocking cmos config const constraint context continue cover covergroup coverpoint cross
deassign default defparam design disable dist do edge else end endcase endchecker endclass endclocking
endconfig endfunction endgenerate endgroup endinterface endmodule endpackage endprimitive endprogram
endproperty endspecify endsequence endtable endtask enum event eventually expect export extends extern
final first_match for force foreach forever fork forkjoin function generate genvar global highz0 highz1
if iff ifnone ignore_bins illegal_bins implements implies import incdir include initial inout input
inside instance int integer interconnect interface intersect join join_any join_none large let liblist
library local localparam logic longint macromodule matches medium modport module nand negedge nettype new
nexttime nmos nor noshowcancelled not notif0 notif1 null or output package packed parameter pmos posedge
primitive priority program property protected pull0 pull1 pulldown pullup pulsestyle_ondetect
pulsestyle_onevent pure rand randc randcase randsequence rcmos real realtime ref reg reject_on release
repeat restrict return rnmos rpmos rtran rtranif0 rtranif1 s_always s_eventually s_nexttime s_until
s_until_with scalared sequence shortint shortreal showcancelled signed small soft solve specify
specparam static string strong strong0 strong1 struct super supply0 supply1 sync_accept_on sync_reject_on
table tagged task this throughout time timeprecision timeunit tran tranif0 tranif1 tri tri0 tri1 triand
trior trireg type typedef union unique unique0 unsigned until until_with untyped use uwire var vectored
virtual void wait wait_order wand weak weak0 weak1 while wildcard wire with within wor xnor xor
clk reset en sample randomize'''.split())


def ident(s, maxlen=96):
    s = re.sub(r'[^A-Za-z0-9_]', '_', str(s))
    s = re.sub(r'_{3,}', '__', s).strip('_') or 'x'
    if s[0].isdigit(): s = 'n' + s
    if s in SV_KEYWORDS: s += '_'
    if len(s) > maxlen:
        h = format(abs(hash(s)) % (1 << 24), '06x')
        s = s[:maxlen - 7] + '_' + h
    return s


class Namer:
    def __init__(self, taken=()):
        self.used = set(taken)

    def __call__(self, base):
        b = ident(base)
        n, i = b, 1
        while n in self.used:
            i += 1; n = f'{b}_{i}'
        self.used.add(n)
        return n


# ======================================================================== variable model
class Var:
    """A bit vector in the formal model."""
    def __init__(self, name, w, signed, role, origin, ti=None):
        self.name, self.w, self.signed, self.role, self.origin, self.ti = name, w, signed, role, origin, ti or {}
        self.init = None

    def decl(self):
        return f"logic {'signed ' if self.signed else ''}[{self.w - 1}:0] {self.name}"


class Arr:
    """An unpacked array of bit vectors, stored packed: element at flat position p is
    name[p*ew +: ew]. dims: [(lo, hi)] per dimension; the first may be dynamic (size var, max)."""
    def __init__(self, name, dims, ew, esigned, role, origin, ti=None, size_var=None, enum=None):
        self.name, self.dims, self.ew, self.esigned, self.role, self.origin = name, dims, ew, esigned, role, origin
        self.ti, self.size_var, self.enum = ti or {}, size_var, enum
        self.trunc = False     # dynamic array whose size() can exceed the modelled maximum

    @property
    def n(self):
        return math.prod(abs(a - b) + 1 for a, b in self.dims)

    @property
    def w(self):
        return self.n * self.ew

    @property
    def signed(self):
        return False

    def decl(self):
        return f'logic [{self.w - 1}:0] {self.name}'


class Obj:
    """A class object or unpacked struct: named children (Var / Arr / Obj)."""
    def __init__(self, cls, prefix, role, origin):
        self.cls, self.prefix, self.role, self.origin = cls, prefix, role, origin
        self.kids = {}


# ======================================================================== typed expressions
class S:
    """translated scalar: Verilog text, width, signedness; k = constant value when known;
    sel = text can be bit/part-selected directly (an identifier)"""
    __slots__ = ('t', 'w', 's', 'k', 'sel')

    def __init__(self, t, w, s, k=None, sel=False):
        self.t, self.w, self.s, self.k, self.sel = t, w, s, k, sel


class ARef:
    """a (sub)array: arr with the leading `fixed` dimensions already indexed (flat offset text)"""
    def __init__(self, arr, depth=0, off='0', guard=None):
        self.arr, self.depth, self.off, self.guard = arr, depth, off, guard


class ORef:
    def __init__(self, obj):
        self.obj = obj


def ty_packed_dims(ty):
    """packed dimensions from slang's type text: 'logic[7:0]' -> [(7,0)]; '$' starts unpacked ones"""
    if not ty: return []
    ty = ty.split('$')[0]
    m = re.search(r'((?:\[-?\d+:-?\d+\])+)\s*$', ty)
    if not m: return []
    return [(int(a), int(b)) for a, b in re.findall(r'\[(-?\d+):(-?\d+)\]', m.group(1))]


def rng_pos(i, l, r):
    """position (0 = LSB side) of index i within a packed/unpacked range [l:r]"""
    return (i - r) if l >= r else (r - i)


BINOP = {'Add': '+', 'Subtract': '-', 'Multiply': '*', 'Divide': '/', 'Mod': '%', 'Power': '**',
         'BinaryAnd': '&', 'BinaryOr': '|', 'BinaryXor': '^', 'BinaryXnor': '~^',
         'Equality': '==', 'Inequality': '!=', 'CaseEquality': '==', 'CaseInequality': '!=',
         'GreaterThanEqual': '>=', 'GreaterThan': '>', 'LessThanEqual': '<=', 'LessThan': '<',
         'LogicalAnd': '&&', 'LogicalOr': '||',
         'LogicalShiftLeft': '<<', 'LogicalShiftRight': '>>', 'ArithmeticShiftLeft': '<<<',
         'ArithmeticShiftRight': '>>>'}
UNOP = {'Plus': '+', 'Minus': '-', 'BitwiseNot': '~', 'BitwiseAnd': '&', 'BitwiseOr': '|',
        'BitwiseXor': '^', 'BitwiseNand': '~&', 'BitwiseNor': '~|', 'BitwiseXnor': '~^', 'LogicalNot': '!'}


def strip_conv(n):
    while isinstance(n, dict) and n.get('k') == 'Conversion': n = n['operand']
    return n


def nconst(n):
    """constant of an AST node, if slang folded it (or it is a literal / enum value / parameter)"""
    if not isinstance(n, dict) or 'const' not in n: return None
    return parse_const(n['const'])


# ======================================================================== translator
class Tx:
    """AST (sniper JSON) -> Verilog text, reproducing SV width/signedness: slang's tree carries an
    explicit Conversion wherever SV widens or re-signs an operand, and each becomes a size cast
    plus $signed/$unsigned here, so Verilog's own context rules never change a width."""

    def __init__(self, mod, resolver, gen):
        self.mod, self.res, self.gen = mod, resolver, gen
        self.env = {}          # symbol path -> S / ARef / ORef (foreach iterators, `item`, inlined args)
        self.refs = set()      # model variables referenced by the current atom
        self.notes = []        # approximations met while translating the current atom
        self.trunc = set()     # truncated arrays the current atom reads beyond fixed positions
        self.depth = 0
        self.this_stack = []   # (class name, ORef) of methods being inlined

    # ---- helpers
    def temp(self, s):
        """s as a selectable identifier (Verilog cannot part-select an expression)"""
        if s.sel: return s
        nm = self.mod.temp(s.t, s.w, s.s)
        return S(nm, s.w, s.s, s.k, True)

    def cast(self, x, w, signed):
        if x.k is not None and x.k.x == 0:
            v = kval(K(x.k.v, x.w, x.s), w, signed) if w != x.w or signed != x.s else x.k.v
            return S(lit(v, w, signed), w, signed, K(v, w, signed))
        t = x.t
        if w > x.w and not x.sel and not self_determined(t):
            # Verilog would evaluate a context-determined operand (a + b) at the new width; SV
            # computed it at x.w: freeze it there first (an inner size cast stops propagation)
            t = f"{x.w}'({t})"
        if w != x.w: t = f"{w}'({t})"          # extension follows the operand's signedness
        if signed != x.s or w != x.w:
            t = f"$signed({t})" if signed else f"$unsigned({t})"
        return S(t, w, signed)

    def boolean(self, x):
        if x.k is not None: return S("1'b1" if x.k.v else "1'b0", 1, False, K(1 if x.k.v else 0, 1))
        return x if x.w == 1 else S(f"({x.t} != {x.w}'d0)", 1, False)

    def scalar(self, n):
        r = self.ex(n)
        if not isinstance(r, S): raise Unsupported(f'aggregate used as a value: {n.get("src") or n.get("k")}')
        return r

    def width(self, n):
        w = n.get('w')
        if w is None:
            raise Unsupported(f"no width for {n.get('k')} '{n.get('src', '')}' (type {n.get('ty')}): "
                              "non-integral, or sniper output predates width info (rerun sniper)")
        return w, bool(n.get('signed'))

    # ---- expressions
    def ex(self, n):
        k = n['k']
        c = nconst(n)
        if c is not None and k not in ('Inside', 'Dist'):
            if c.x and k != 'Conversion':
                w, s = self.width(n)
                return S(lit(c.v, w, s), w, s, K(c.v, w, s, c.x))
            w, s = n.get('w') or c.w or 32, bool(n.get('signed', c.s))
            v = kval(c, w, s)
            return S(lit(v, w, s), w, s, K(v, w, s, c.x))
        fn = getattr(self, 'x_' + k, None)
        if fn is None: raise Unsupported(f'expression kind {k}: {n.get("src", "")}')
        return fn(n)

    def x_Conversion(self, n):
        w, s = self.width(n)
        return self.cast(self.scalar(n['operand']), w, s)

    def x_IntegerLiteral(self, n):
        raise Unsupported('literal without value')

    x_UnbasedUnsizedIntegerLiteral = x_IntegerLiteral

    def x_NamedValue(self, n):
        sym = n['symbol']
        b = self.env.get(sym.get('path')) or self.env.get('#' + sym['sym'])
        if b is not None: return b
        return self.res.resolve(n, self)

    x_HierarchicalValue = x_NamedValue

    def x_MemberAccess(self, n):
        base = self.ex(n['value'])
        if isinstance(base, ORef):
            return self.res.member(base, n, self)
        if isinstance(base, S) and 'offset' in n:      # packed struct / union field
            w, s = self.width(n)
            b = self.temp(base)
            t = f"{b.t}[{n['offset']} +: {w}]"
            return S(f'$signed({t})' if s else t, w, s, None, not s)
        raise Unsupported(f'member access {n.get("src", "")}')

    def x_ElementSelect(self, n):
        base = self.ex(n['value'])
        if isinstance(base, ARef): return self.arr_index(base, n['selector'], n)
        if isinstance(base, S):                     # packed select: bit, or element of multi-dim packed
            w, s = self.width(n)
            dims = ty_packed_dims(n['value'].get('ty'))
            l, r = dims[0] if dims else (base.w - 1, 0)
            sel = self.scalar(n['selector'])
            b = self.temp(base)
            if sel.k is not None:
                p = rng_pos(sel.k.v, l, r)
                if not 0 <= p * w < base.w: return S(lit(0, w, s), w, s, K(0, w, s))
                t = f'{b.t}[{p * w} +: {w}]' if w > 1 else f'{b.t}[{p}]'
            else:
                pos = self.idx_pos(sel, l, r)
                t = f'{b.t}[({pos}) * {w} +: {w}]'
            return S(f'$signed({t})' if s else t, w, s, None, not s)
        raise Unsupported(f'element select {n.get("src", "")}')

    def idx_pos(self, sel, l, r):
        x = self.cast(sel, 32, True).t
        return f"({x} - {lit(r, 32, True)})" if l >= r else f"({lit(r, 32, True)} - {x})"

    def x_RangeSelect(self, n):
        base = self.ex(n['value'])
        if not isinstance(base, S): raise Unsupported(f'range select of an unpacked array: {n.get("src", "")}')
        w, s = self.width(n)
        dims = ty_packed_dims(n['value'].get('ty'))
        l, r = dims[0] if dims else (base.w - 1, 0)
        ew = base.w // (abs(l - r) + 1) if dims else 1
        b = self.temp(base)
        kind = n.get('selectionKind', 'Simple')
        a = self.scalar(n['left'])
        if kind == 'Simple':
            bb = self.scalar(n['right'])
            if a.k is None or bb.k is None: raise Unsupported('non-constant [a:b] select')
            p0, p1 = sorted((rng_pos(a.k.v, l, r), rng_pos(bb.k.v, l, r)))
            t = f'{b.t}[{(p1 + 1) * ew - 1}:{p0 * ew}]'
        else:
            cnt = self.scalar(n['right'])
            if cnt.k is None: raise Unsupported('indexed select width not constant')
            m = cnt.k.v
            if kind == 'IndexedUp':
                lo_idx = a if l >= r else None
                if l >= r: pos = self.idx_pos(a, l, r)
                else: pos = f"({lit(r, 32, True)} - {self.cast(a, 32, True).t} - {m - 1})"
            else:   # IndexedDown: [base -: m]
                if l >= r: pos = f"({self.idx_pos(a, l, r)} - {m - 1})"
                else: pos = self.idx_pos(a, l, r)
            t = f'{b.t}[({pos}) * {ew} +: {w}]'
        return S(f'$signed({t})' if s else t, w, s, None, not s)

    def start(self):
        """begin translating a new atom"""
        self.refs, self.notes, self.trunc = set(), [], set()

    def trunc_cond(self):
        """true when a truncated array read by the current atom has elements past the model: the
        atom is then not modelled and must not constrain anything (approx: over)"""
        return ' || '.join(f'({a.size_var.name} > {lit(a.max, 32, True)})'
                           for a in sorted(self.trunc, key=lambda a: a.name))

    def arr_index(self, a, seln, n):
        arr = a.arr
        dl, dr = arr.dims[a.depth]
        inner = math.prod(abs(x - y) + 1 for x, y in arr.dims[a.depth + 1:])
        sel = self.scalar(seln)
        lo = min(dl, dr)
        size = abs(dl - dr) + 1
        guard = a.guard
        if sel.k is not None:
            p = sel.k.v - lo
            if not 0 <= p < size: raise Unsupported(f'constant index {sel.k.v} outside {arr.origin}')
            off = f'({a.off}) + {p * inner}' if a.off != '0' else str(p * inner)
            if a.depth == 0 and arr.size_var is not None:
                g = f'({lit(sel.k.v, 32, True)} < {arr.size_var.name})'
                self.refs.add(arr.size_var.name)
                guard = g if guard is None else f'({guard} && {g})'
        else:
            st = self.cast(sel, 32, True).t
            p = f'({st} - {lit(lo, 32, True)})'
            off = f'({a.off}) + {p} * {inner}' if a.off != '0' else f'{p} * {inner}'
            lim = arr.size_var.name if (a.depth == 0 and arr.size_var is not None) else lit(size, 32, True)
            if a.depth == 0 and arr.size_var is not None:
                self.refs.add(arr.size_var.name)
                if arr.trunc:
                    self.trunc.add(arr); self.refs.add(arr.size_var.name)
                    lim = f'(({lim}) < {lit(arr.max, 32, True)} ? ({lim}) : {lit(arr.max, 32, True)})'
            g = f'({p} >= 0 && {p} < {lim})'
            guard = g if guard is None else f'({guard} && {g})'
        if a.depth + 1 < len(arr.dims):
            return ARef(arr, a.depth + 1, off, guard)
        w, s = arr.ew, arr.esigned
        try: offk = eval_int(off)
        except Exception: offk = None
        t = f'{arr.name}[{offk * w} +: {w}]' if offk is not None else f'{arr.name}[({off}) * {w} +: {w}]'
        if w == 1 and offk is not None: t = f'{arr.name}[{offk}]'
        if s: t = f'$signed({t})'
        if guard is not None:           # out-of-range reads return the default value (0), LRM 7.4.6
            if offk is None or a.guard is not None:
                t = f'(({guard}) ? {t} : {lit(0, w, s)})'
                return S(t, w, s)
        return S(t, w, s, None, not s and guard is None)

    def x_BinaryOp(self, n):
        op = n['op']
        w, s = self.width(n)
        if op in ('WildcardEquality', 'WildcardInequality'):
            l, r = self.scalar(n['left']), self.scalar(n['right'])
            if r.k is None: raise Unsupported('==? with a non-constant right operand')
            mask = ((1 << r.w) - 1) & ~r.k.x
            t = f"(({l.t} & {lit(mask, l.w, False)}) == {lit(r.k.v & mask, l.w, False)})"
            return S(t if op == 'WildcardEquality' else f'!{t}', 1, False)
        if op == 'LogicalImplication':
            l, r = self.boolean(self.scalar(n['left'])), self.boolean(self.scalar(n['right']))
            return S(f'(!{l.t} || {r.t})', 1, False)
        if op == 'LogicalEquivalence':
            l, r = self.boolean(self.scalar(n['left'])), self.boolean(self.scalar(n['right']))
            return S(f'({l.t} == {r.t})', 1, False)
        if op not in BINOP: raise Unsupported(f'operator {op}')
        l, r = self.scalar(n['left']), self.scalar(n['right'])
        if op == 'Power':
            return self.power(l, r, w, s)
        if op in ('Divide', 'Mod') and (r.k is None or r.k.v == 0):
            self.notes.append('division/modulo by a value that can be 0: Verilog gives x, the SV solver '
                              'never picks a zero divisor (approx: formal allows the x result)')
        if op in ('CaseEquality', 'CaseInequality') and (l.k and l.k.x or r.k and r.k.x):
            raise Unsupported('=== against x/z')
        f = fold_bin(op, l, r, w, s)
        if f is not None: return f
        return S(f'({l.t} {BINOP[op]} {r.t})', w, s)

    def power(self, l, r, w, s):
        """a ** b without a $pow cell (the SMT backends reject it): constant exponent -> products,
        constant power-of-two base -> shift; LRM table 11-4 for negative exponents"""
        if l.k is not None and r.k is not None:
            v = l.k.v ** r.k.v if r.k.v >= 0 else (1 if l.k.v == 1 else (0 if abs(l.k.v) > 1 else 0))
            return S(lit(v, w, s), w, s, K(kval(K(v), w, s), w, s))
        if r.k is not None:
            e = r.k.v
            if e < 0:       # integer base, negative exponent: 0 unless base is 1 or -1 (then +-1)
                raise Unsupported('** with a negative constant exponent and a variable base')
            if e == 0: return S(lit(1, w, s), w, s, K(1, w, s))
            if e > 16: raise Unsupported(f'** with exponent {e}')
            return S('(' + ' * '.join([l.t] * e) + ')', w, s)
        if l.k is not None and l.k.v > 0 and l.k.v & (l.k.v - 1) == 0:
            m = l.k.v.bit_length() - 1
            rt = self.cast(r, 32, r.s).t
            sh = f'({lit(1, w, s)} << ({rt} * {m}))' if m != 1 else f'({lit(1, w, s)} << {rt})'
            if r.s:          # negative exponent: 1 for base 1, else 0
                sh = f"(({rt} < 0) ? {lit(1 if m == 0 else 0, w, s)} : {sh})"
            return S(sh, w, s)
        raise Unsupported('** with variable base and exponent (no SMT encoding)')

    def x_UnaryOp(self, n):
        op = n['op']
        if op not in UNOP: raise Unsupported(f'unary operator {op}')
        w, s = self.width(n)
        x = self.scalar(n['operand'])
        f = fold_un(op, x, w, s)
        if f is not None: return f
        return S(f'({UNOP[op]}{x.t})', w, s)

    def x_ConditionalOp(self, n):
        w, s = self.width(n)
        conds = n['conditions']
        if len(conds) != 1 or 'pattern' in conds[0]: raise Unsupported('conditional with a pattern')
        c = self.boolean(self.scalar(conds[0]['expr']))
        a, b = self.scalar(n['left']), self.scalar(n['right'])
        if c.k is not None: return a if c.k.v else b
        return S(f'({c.t} ? {a.t} : {b.t})', w, s)

    def x_Concatenation(self, n):
        w, s = self.width(n)
        parts = [self.scalar(o) for o in n['operands']]
        parts = [p for p in parts if p.w]
        return S('{' + ', '.join(p.t for p in parts) + '}', w, s)

    def x_Replication(self, n):
        w, s = self.width(n)
        cnt = self.scalar(n['count'])
        if cnt.k is None: raise Unsupported('replication count not constant')
        inner = self.scalar(n['concat'])
        return S(f'{{{cnt.k.v}{{{inner.t}}}}}', w, s)

    def x_Inside(self, n):
        left = n['left']
        L = self.ex(left)
        alts = []
        for it in n['rangeList']:
            alts.extend(self.member_conds(L, it))
        return S('(' + ' || '.join(alts) + ')' if alts else "1'b0", 1, False)

    def member_conds(self, L, it, wild=True):
        """conditions for L inside one item of an open range list (value, range, or unpacked array)"""
        itk = strip_conv(it)
        if itk.get('k') == 'ValueRange':
            if not isinstance(L, S): raise Unsupported('inside on an aggregate')
            parts = []
            for side, op in (('left', '>='), ('right', '<=')):
                b = it if it.get('k') == 'ValueRange' else itk
                bn = b[side]
                if strip_conv(bn).get('k') == 'UnboundedLiteral': continue     # '$': no bound
                bv = self.cast(self.scalar(bn), L.w, L.s)
                parts.append(f'({L.t} {op} {bv.t})')
            return ['(' + ' && '.join(parts) + ')' if parts else "1'b1"]
        R = self.ex(it)
        if isinstance(R, ARef):                     # x inside {arr}: any element
            return [f'({L.t} == {e.t})' if not g else f'({g} && {L.t} == {e.t})'
                    for e, g in self.elements(R)]
        if not isinstance(L, S) or not isinstance(R, S): raise Unsupported('inside on an aggregate')
        if R.k is not None and R.k.x and wild:      # inside uses ==? : x/z/? bits are don't-care
            mask = ((1 << L.w) - 1) & ~R.k.x
            return [f"(({L.t} & {lit(mask, L.w, False)}) == {lit(R.k.v & mask, L.w, False)})"]
        R = self.cast(R, L.w, L.s) if R.w != L.w else R
        return [f'({L.t} == {R.t})']

    def elements(self, a):
        """(element S, guard text or None) for every element of an (sub)array"""
        arr = a.arr
        dims = arr.dims[a.depth:]
        out = []
        if arr.trunc and a.depth == 0: self.trunc.add(arr)
        for idx in iter_dims(dims):
            x = a
            g = None
            for d, i in enumerate(idx):
                lo = min(arr.dims[a.depth + d])
                x = self.arr_index(x, {'k': 'IntegerLiteral', 'const': str(i + lo), 'w': 32, 'signed': True}, None)
            if isinstance(x, S):
                if arr.size_var is not None and a.depth == 0:
                    g = f'({lit(idx[0] + min(arr.dims[0]), 32, True)} < {arr.size_var.name})'
                    x = S(x.t.split(' ? ')[1].split(' : ')[0] if x.t.startswith('((') else x.t, x.w, x.s)
                out.append((x, g))
        return out

    def x_Dist(self, n):
        L = self.ex(n['left'])
        alts = []
        for it in n['items']:
            wk = None
            wt = it.get('weight')
            if wt and wt.get('expr') is not None:
                wk = nconst(wt['expr'])
                if wk is None:
                    self.notes.append('dist weight not constant: item kept')
            if wk is not None and wk.v == 0:
                continue                         # weight 0: the value is never generated
            alts.extend(self.member_conds(L, it['value']))
        return S('(' + ' || '.join(alts) + ')' if alts else "1'b0", 1, False)

    def x_Call(self, n):
        name = n.get('subroutineName')
        args = n.get('arguments') or []
        if n.get('isSystemCall'):
            info = (n.get('subroutine') or {})
            extra = info.get('extraInfo') or {}
            if name in ('size', 'num'):
                a = self.ex(args[0])
                if not isinstance(a, ARef): raise Unsupported(f'{name}() of a non-array')
                arr = a.arr
                if a.depth == 0 and arr.size_var is not None:
                    self.refs.add(arr.size_var.name)
                    return self.cast(S(arr.size_var.name, 32, True, None, True), *self.width(n))
                l, r = arr.dims[a.depth]
                return S(lit(abs(l - r) + 1, *self.width(n)), *self.width(n), K(abs(l - r) + 1, 32, True))
            if name in ('sum', 'product', 'and', 'or', 'xor'):
                return self.reduce(n, name, args, extra)
            if name in ('$countones', '$onehot', '$onehot0'):
                x = self.scalar(args[0])
                w, s = self.width(n)
                if name == '$countones':
                    cw = max(1, x.w.bit_length())
                    return self.cast(S(f'$countones({x.t})', cw, False), w, s)
                return S(f'{name}({x.t})', 1, False)
            if name in ('$signed', '$unsigned'):
                x = self.scalar(args[0]); w, s = self.width(n)
                return self.cast(x, w, s)
            if name == '$isunknown':
                return S("1'b0", 1, False, K(0, 1))
            raise Unsupported(f'system call {name}')
        return self.inline_call(n, name, args)

    def reduce(self, n, name, args, extra):
        a = self.ex(args[0])
        if not isinstance(a, ARef): raise Unsupported(f'{name}() of a non-array')
        w, s = self.width(n)
        it_expr = extra.get('iterExpr')
        it_var = (extra.get('iterVar') or {}).get('path')
        ident_v = {'sum': 0, 'product': 1, 'and': -1, 'or': 0, 'xor': 0}[name]
        op = {'sum': '+', 'product': '*', 'and': '&', 'or': '|', 'xor': '^'}[name]
        terms = []
        for e, g in self.elements(a):
            if it_expr is not None:
                saved = self.env.get(it_var)
                self.env[it_var] = e
                try: t = self.scalar(it_expr)
                finally:
                    if saved is None: self.env.pop(it_var, None)
                    else: self.env[it_var] = saved
            else:
                t = e
            t = self.cast(t, w, s)
            terms.append(f'({g} ? {t.t} : {lit(ident_v, w, s)})' if g else t.t)
        if not terms: return S(lit(ident_v, w, s), w, s, K(ident_v, w, s))
        return S('(' + f' {op} '.join(terms) + ')', w, s)

    def inline_call(self, n, name, args):
        """a user function whose body is a single `return <expr>;`: substitute the arguments"""
        if self.depth > 6: raise Unsupported(f'call nesting too deep: {name}')
        sub = self.gen.find_function(n, self.res)
        if sub is None: raise Unsupported(f'call to {name}(): body not available or not a single return')
        body, formals, sub_path = sub
        this = None
        if n.get('thisClass') is not None:          # obj.method(): members of obj inside the body
            this = self.ex(n['thisClass'])
            if not isinstance(this, ORef): raise Unsupported(f'{name}(): object not modelled')
        saved = dict(self.env)
        cls_of = sub_path.rsplit('::', 1)[0] if '::' in sub_path else None     # class path
        self.this_stack.append((cls_of, this))
        try:
            for i, f in enumerate(formals):
                if i < len(args) and args[i] is not None and args[i].get('k') != 'EmptyArgument':
                    self.env[f['path']] = self.ex(args[i])
                elif f.get('default_ast') is not None:
                    self.env[f['path']] = self.ex(f['default_ast'])
                else:
                    raise Unsupported(f'{name}(): argument {f["name"]} has no value')
            self.depth += 1
            r = self.ex(body)
            self.notes.append(f'function {name}() inlined (statically resolved; a virtual override in a '
                              'derived class would replace it)')
            return r
        finally:
            self.depth -= 1
            self.env = saved
            self.this_stack.pop()


def kconst(v, w, s):
    v = kval(K(v), w, s)
    return S(lit(v, w, s), w, s, K(v, w, s))


def fold_bin(op, l, r, w, s):
    """constant result of l op r with SV semantics, or None (operands carry slang's common type)"""
    if l.k is None or r.k is None or l.k.x or r.k.x: return None
    a, b = l.k.v, r.k.v
    m = (1 << max(l.w, 1)) - 1
    if op in ('Add', 'Subtract', 'Multiply'):
        return kconst({'Add': a + b, 'Subtract': a - b, 'Multiply': a * b}[op], w, s)
    if op in ('Divide', 'Mod'):
        if b == 0: return None                      # x
        q = abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1)   # truncates toward zero
        return kconst(q if op == 'Divide' else a - q * b, w, s)
    if op in ('BinaryAnd', 'BinaryOr', 'BinaryXor', 'BinaryXnor'):
        ua, ub = a & m, b & m
        v = {'BinaryAnd': ua & ub, 'BinaryOr': ua | ub, 'BinaryXor': ua ^ ub, 'BinaryXnor': ~(ua ^ ub) & m}[op]
        return kconst(v, w, s)
    if op in ('Equality', 'CaseEquality', 'Inequality', 'CaseInequality', 'LessThan', 'LessThanEqual',
              'GreaterThan', 'GreaterThanEqual'):
        v = {'Equality': a == b, 'CaseEquality': a == b, 'Inequality': a != b, 'CaseInequality': a != b,
             'LessThan': a < b, 'LessThanEqual': a <= b, 'GreaterThan': a > b, 'GreaterThanEqual': a >= b}[op]
        return kconst(int(v), 1, False)
    if op in ('LogicalAnd', 'LogicalOr'):
        return kconst(int(bool(a) and bool(b)) if op == 'LogicalAnd' else int(bool(a) or bool(b)), 1, False)
    if op in ('LogicalShiftLeft', 'ArithmeticShiftLeft'):
        return kconst((a & m) << (b & ((1 << r.w) - 1)), w, s) if b >= 0 else None
    if op == 'LogicalShiftRight':
        return kconst((a & m) >> (b & ((1 << r.w) - 1)), w, s) if b >= 0 else None
    if op == 'ArithmeticShiftRight':
        return kconst((a if l.s else a & m) >> (b & ((1 << r.w) - 1)), w, s) if b >= 0 else None
    return None


def fold_un(op, x, w, s):
    if x.k is None or x.k.x: return None
    a, m = x.k.v, (1 << max(x.w, 1)) - 1
    ua = a & m
    if op == 'Plus': return kconst(a, w, s)
    if op == 'Minus': return kconst(-a, w, s)
    if op == 'BitwiseNot': return kconst(~ua & m, w, s)
    if op == 'LogicalNot': return kconst(int(a == 0), 1, False)
    if op == 'BitwiseAnd': return kconst(int(ua == m), 1, False)
    if op == 'BitwiseOr': return kconst(int(ua != 0), 1, False)
    if op == 'BitwiseNand': return kconst(int(ua != m), 1, False)
    if op == 'BitwiseNor': return kconst(int(ua == 0), 1, False)
    if op in ('BitwiseXor', 'BitwiseXnor'):
        p = bin(ua).count('1') & 1
        return kconst(p if op == 'BitwiseXor' else 1 - p, 1, False)
    return None


def self_determined(t):
    """text whose width cannot be changed by its context: identifier, literal, select, concatenation,
    system call, cast"""
    t = t.strip()
    if re.fullmatch(r"[A-Za-z_]\w*(\[[^\]]*\])?", t): return True
    if re.fullmatch(r"\d+'s?[hdbo][0-9a-fA-F_xz?]+", t): return True
    for opener in ('{', '$signed(', '$unsigned(', '$countones(', '$onehot(', '$onehot0('):
        if t.startswith(opener) and matching_close(t, len(opener) - 1) == len(t) - 1: return True
    m = re.match(r"\d+'\(", t)
    if m and matching_close(t, m.end() - 1) == len(t) - 1: return True
    return False


def matching_close(t, i):
    """index of the bracket closing the one at t[i]"""
    pairs = {'(': ')', '{': '}', '[': ']'}
    stack = []
    for j in range(i, len(t)):
        c = t[j]
        if c in pairs: stack.append(pairs[c])
        elif c in ')}]':
            if not stack or stack.pop() != c: return -1
            if not stack: return j
    return -1


def eval_int(t):
    if not re.fullmatch(r'[\d\s+*()]+', t): raise ValueError
    return eval(t, {'__builtins__': {}})


def iter_dims(dims):
    sizes = [abs(a - b) + 1 for a, b in dims]
    idx = [0] * len(sizes)
    if not sizes or 0 in sizes: return
    while True:
        yield tuple(idx)
        for d in range(len(sizes) - 1, -1, -1):
            idx[d] += 1
            if idx[d] < sizes[d]: break
            idx[d] = 0
        else:
            return


# ======================================================================== module builder
class Module:
    def __init__(self, name, kind, origin):
        self.name, self.kind, self.origin = name, kind, origin
        self.params = []         # (name, default, comment)
        self.ports = {}          # name -> (dir, decl, comment)
        self.port_order = []
        self.body = []           # lines
        self.temps = []
        self.names = Namer(['clk', 'reset', 'en', 'sample', 'randomize'])
        self.n_temp = 0

    def port(self, d, decl, name, comment=''):
        if name not in self.ports:
            self.ports[name] = (d, decl, comment)
            self.port_order.append(name)
            self.names.used.add(name)

    def temp(self, text, w, signed):
        self.n_temp += 1
        nm = self.names(f'_t{self.n_temp}')
        self.temps.append(f"  wire {'signed ' if signed else ''}[{w - 1}:0] {nm} = {text};")
        return nm

    def emit(self):
        out = [f'// {self.origin}']
        def lines(items):       # comma before the trailing comment
            return '\n'.join(f'  {t}{"," if i + 1 < len(items) else ""}' + (f'  // {c}' if c else '')
                             for i, (t, c) in enumerate(items))
        hdr = f'module {self.name}'
        if self.params:
            hdr += ' #(\n' + lines([(f'parameter {(x[3:] or ("bit",))[0]} {x[0]} = {x[1]}', x[2]) for x in self.params]) + '\n)'
        ports = [(f'{self.ports[nm][0]} {self.ports[nm][1]}', self.ports[nm][2]) for nm in self.port_order]
        out.append(hdr + ' (\n' + lines(ports) + '\n);')
        out.append('  `include "prim_assert.sv"')
        out += self.temps
        out += self.body
        out.append(f'endmodule : {self.name}\n')
        return '\n'.join(out)


# ======================================================================== resolvers
class ObjResolver:
    """Names inside a constraint: the randomized object's fields (rand or state), the caller's
    variables (inline constraints), foreach iterators (bound in Tx.env)."""

    def __init__(self, gen, mod, root, cls_chain, caller=None, rand_filter=None, std_vars=None):
        self.gen, self.mod, self.root, self.cls_chain = gen, mod, root, cls_chain
        self.caller = caller             # (owner class chain, subroutine path) of an inline site
        self.rand_filter = rand_filter   # randomize(a, b): only these are random
        self.std_vars = std_vars or {}   # std::randomize(x): path -> name
        self.ctx_vars = {}

    def owns(self, path, chain):
        """True when a ClassProperty path belongs to one of the classes in chain"""
        if not path or '::' not in path: return False
        cls = re.sub(r'#.*$', '', path.split('#')[0].rsplit('::', 1)[0].split('::')[-1] if '#' not in path
                     else path.split('#')[0].split('::')[-1])
        return cls in chain

    def resolve(self, n, tx):
        sym = n['symbol']
        sk, path, nm = sym.get('skind'), sym.get('path', ''), sym['sym']
        if sk == 'ClassProperty' and tx.this_stack and tx.this_stack[-1][1] is not None:
            cls, this = tx.this_stack[-1]
            c0 = self.gen.cls(cls)
            chain = [c['name'] for c in self.gen.chain(c0)] if c0 else [sev_key(cls)]
            if self.owns(path, chain):
                return self.member(this, {**n, 'member': sym}, tx)
        if sk == 'ClassProperty' and self.owns(path, self.cls_chain):
            x = self.root.kids.get(nm)
            if x is None: x = self.gen.state_field(self.mod, self.root, nm, n)
            return self.ref(x, tx)
        if path in self.std_vars or (sk in ('Variable', 'FormalArgument') and self.std_vars and nm in self.std_vars.values()):
            v = self.std_vars.get(path) or next(v for v in self.std_vars.values() if v == nm)
            return self.ref(self.root.kids[v], tx)
        if sk in ('ClassProperty', 'Variable', 'FormalArgument', 'Net') or sk == 'Iterator':
            if sk == 'Iterator': raise Unsupported(f'iterator {nm} outside its foreach')
            # caller's scope (inline constraints) or a static/other object's member: state input
            return self.ref(self.gen.ctx_field(self.mod, self, nm, n), tx)
        raise Unsupported(f'reference to {sk} {nm}')

    def ref(self, x, tx):
        if isinstance(x, Var):
            tx.refs.add(x.name)
            return S(x.name, x.w, x.signed, None, not x.signed)
        if isinstance(x, Arr):
            tx.refs.add(x.name)
            return ARef(x)
        return ORef(x)

    def member(self, base, n, tx):
        obj = base.obj
        nm = n['member']['sym']
        x = obj.kids.get(nm)
        if x is None: x = self.gen.state_field(self.mod, obj, nm, n)
        return self.ref(x, tx)


# ======================================================================== generator
def sev_key(cls_name):
    return re.sub(r'#.*$', '', cls_name.split('::')[-1])


class Gen:
    def __init__(self, d, a):
        self.d, self.a = d, a
        # class names are not unique across packages: look classes up with cls()
        self.classes_by_path = {c['path']: c for c in d['classes'] if c.get('path')}
        self.classes_by_name = {}
        for c in d['classes']:
            self.classes_by_name.setdefault(c['name'], []).append(c)
        self.subs_by_path = {s['path']: s for s in d['subroutines']}
        self.report = []
        self.mods_c, self.mods_cg = [], []
        self.smokes = []
        # every module goes into one file: same-named classes/covergroups of different packages, or
        # randomize() sites on the same line of different files, get _2, _3, ...
        self.mod_names = Namer()

    # ---- class model
    def cls(self, name, ctx=None):
        """the class a name refers to: an exact path, else the one in the package of the class `ctx`
        (path) the reference is made from, else the only class of that name"""
        if not name: return None
        full = re.sub(r'#.*$', '', name)
        if full in self.classes_by_path: return self.classes_by_path[full]
        cands = self.classes_by_name.get(sev_key(name), [])
        if len(cands) > 1 and ctx:
            same = [c for c in cands if '::' in c['path'] and ctx.startswith(c['path'].rsplit('::', 1)[0] + '::')]
            if same: return same[0]
        return cands[0] if cands else None

    def class_at(self, file, line):
        """the innermost class whose source span contains file:line"""
        best = None
        for c in self.d['classes']:
            if c.get('file') == file and (c.get('start') or 0) <= line <= (c.get('end') or -1):
                if best is None or c['start'] >= best['start']: best = c
        return best

    def chain(self, cls):
        """the class and its ancestors (own entries in the extraction), most-derived first"""
        out, c = [], cls
        while c is not None and c['path'] not in [x['path'] for x in out]:
            out.append(c)
            b = c.get('base')
            c = self.cls(b, c['path']) if b else None
        return out

    def find_function(self, n, res):
        tgt = n.get('subroutine')
        path = tgt.get('path') if isinstance(tgt, dict) else None
        cands = []
        if path and path in self.subs_by_path: cands.append(self.subs_by_path[path])
        name = n.get('subroutineName')
        # dynamic dispatch on the randomized object: the most-derived override in its chain wins
        for c in getattr(res, 'cls_objs', []):
            for s in self.d['subroutines']:
                if s['name'] == name and s.get('owner') == c['name'] and s not in cands:
                    cands.insert(0, s)
                    break
        for s in cands:
            body = s.get('body')
            r = single_return(body)
            if r is None: continue
            formals = []
            for a in s.get('args', []):
                formals.append({'name': a['name'], 'path': f"{s['path']}.{a['name']}", 'default_ast': None})
            return r, formals, s['path']
        return None

    def max_size(self, cls_chain_objs, prop_name):
        """upper bound of a dynamic array's size() from unconditional constraints in the class"""
        best = None
        for c in cls_chain_objs:
            for b in c['constraints']:
                for it in b['items']:
                    if it['k'] != 'Expression' or it.get('isSoft'): continue
                    hi = size_bound(it['expr'], prop_name)
                    if hi is not None: best = hi if best is None else min(best, hi)
        return best

    def build_obj(self, mod, cls, prefix, role, origin, depth=0, notes=None):
        """flatten a class's fields into model variables: rand fields get `role`, others 'state'
        (created on demand when a constraint reads them)"""
        obj = Obj(cls, prefix, role, origin)
        chain = self.chain(cls)
        # ancestors outside the extraction still own fields (their names come from the lineage)
        obj.chain = [c['name'] for c in chain] + [sev_key(x) for c in chain for x in c.get('lineage', [])]
        obj.chain_objs = chain
        seen = set()
        for c in chain:
            for p in c['properties']:
                if p['name'] in seen or p.get('static') and p['rand'] not in ('Rand', 'RandC'): continue
                seen.add(p['name'])
                if p['rand'] not in ('Rand', 'RandC'): continue
                if p['rand'] == 'RandC' and notes is not None:
                    notes.append(f"{p['name']}: randc modelled as rand (cyclic order across calls is not "
                                 "modelled; approx: over)")
                x = self.make_var(mod, prefix + p['name'], p.get('ti') or {}, role,
                                  f"{c['name']}.{p['name']}", depth, notes, chain, p)
                if x is not None: obj.kids[p['name']] = x
        return obj

    def make_var(self, mod, name, ti, role, origin, depth, notes, chain=None, prop=None):
        k = ti.get('kind')
        if 'w' in ti:
            v = Var(ident(name), ti['w'], ti.get('signed', False), role, origin, ti)
            if prop is not None and prop.get('init_value') is not None:
                v.init = parse_const(prop['init_value'])
            return v
        if k in ('FixedSizeUnpackedArrayType', 'DynamicArrayType', 'QueueType'):
            dims, t = [], ti
            dyn = False
            while t.get('kind') in ('FixedSizeUnpackedArrayType', 'DynamicArrayType', 'QueueType'):
                if t['kind'] == 'FixedSizeUnpackedArrayType':
                    dims.append(tuple(t['range']))
                else:
                    if dims:
                        if notes is not None: notes.append(f'{origin}: dynamic inner array dimension not modelled')
                        return None
                    dyn = True
                    mx = None
                    if chain and prop is not None:
                        mx = self.max_size(chain, prop['name'])
                    cap = self.a.max_array
                    trunc = mx is None or mx > cap
                    if trunc and notes is not None:
                        notes.append(f'{origin}: size() ' + ('unbounded by unconditional constraints'
                                                             if mx is None else f'can reach {mx}') +
                                     f'; elements modelled up to {cap}, later ones free, constraints reading '
                                     'the whole array hold only while size() <= ' + f'{cap} (approx: over; '
                                     'raise --max-array)')
                    if trunc: mx = cap
                    dims.append((0, max(mx - 1, 0)))
                t = t.get('elem') or {}
            if 'w' not in t:
                if notes is not None: notes.append(f'{origin}: array element type {t.get("kind")} not modelled')
                return None
            sv = Var(ident(name + '__size'), 32, True, role, origin + '.size()') if dyn else None
            arr = Arr(ident(name), dims, t['w'], t.get('signed', False), role, origin, ti, sv,
                      t.get('enum'))
            if dyn: arr.max, arr.trunc = dims[0][1] + 1, trunc
            return arr
        if k == 'UnpackedStructType':
            o = Obj(None, name + '__', role, origin)
            for f in ti.get('fields', []):
                x = self.make_var(mod, name + '__' + f['name'], f['type'], role, origin + '.' + f['name'],
                                  depth, notes)
                if x is not None: o.kids[f['name']] = x
            return o
        if k == 'ClassType':
            cls = self.cls(ti.get('generic_path') or ti.get('class_path') or ti.get('generic') or ti.get('class', ''),
                           chain[0]['path'] if chain else None)
            if cls is None or depth > 3:
                if notes is not None and role == 'rand':
                    notes.append(f'{origin}: rand object of class {ti.get("class")} not in the extraction; '
                                 'its fields are unconstrained state (approx: over)')
                return Obj(None, name + '__', 'state', origin)
            if role == 'rand':          # nested rand object: its rand fields and constraints join
                o = self.build_obj(mod, cls, name + '__', role, origin, depth + 1, notes)
                o.nested_rand = True
                return o
            return Obj(cls, name + '__', 'state', origin)
        if notes is not None and role == 'rand':
            notes.append(f'{origin}: type {k} not modelled')
        return None

    def state_field(self, mod, obj, nm, n):
        """a field read by a constraint but not randomized: a state input, width from the AST"""
        x = None
        if obj.cls is not None:
            for c in self.chain(obj.cls):
                p = next((p for p in c['properties'] if p['name'] == nm), None)
                if p is not None:
                    x = self.make_var(mod, obj.prefix + nm, p.get('ti') or {}, 'state', f"{c['name']}.{nm}", 4,
                                      None)
                    break
        if x is None:
            if n.get('w') is not None:
                x = Var(ident(obj.prefix + nm), n['w'], bool(n.get('signed')), 'state', obj.origin + '.' + nm)
            else:
                x = Obj(None, obj.prefix + nm + '__', 'state', obj.origin + '.' + nm)
        obj.kids[nm] = x
        return x

    def ctx_field(self, mod, res, nm, n):
        key = nm
        if key not in res.ctx_vars:
            sym = n['symbol']
            if n.get('w') is not None:
                res.ctx_vars[key] = Var(ident('ctx__' + nm), n['w'], bool(n.get('signed')), 'state',
                                        f"caller {sym.get('path', nm)}")
            else:
                cls = self.cls(n.get('ty', ''), sym.get('path'))
                o = Obj(cls, 'ctx__' + nm + '__', 'state', f"caller {sym.get('path', nm)}")
                res.ctx_vars[key] = o
        return res.ctx_vars[key]

    # ---- constraints
    def constraint_items(self, chain):
        """(class, block, item index, item) in effect: a block in a derived class replaces a
        same-named block of its ancestors (LRM 18.5.2)"""
        seen, out = set(), []
        for c in chain:
            for b in c['constraints']:
                if b['name'] in seen: continue
                seen.add(b['name'])
                out.append((c, b))
        return list(reversed(out))

    def build_constraints(self, cls, site=None):
        """fv_c_ module for a class, or for one randomize() call site of it"""
        chain = self.chain(cls)
        suffix = ''
        if site is not None: suffix = '__' + site['tag']
        mname = self.mod_names('fv_c_' + cls['name'] + suffix)
        mod = Module(mname, 'constraints', f"constraints of class {cls['name']} ({cls['file']}:{cls['start']})"
                     + (f"; randomize() at {site['where']}: {site['src']}" if site else ''))
        notes = []
        root = self.build_obj(mod, cls, '', 'rand', cls['name'], 0, notes)
        mod.cls, mod.site, mod.root = cls, site, root
        rand_filter = site.get('args') if site else None
        cm_mode, rand_mode = self.modes(chain, site)
        res = ObjResolver(self, mod, root, root.chain)
        res.cls_objs = chain
        atoms, entries = [], []
        tx = Tx(mod, res, self)

        def gather(o, prefix_obj):
            """(object, constraint items) for the object and its nested rand objects"""
            yield o
            for x in o.kids.values():
                if isinstance(x, Obj) and getattr(x, 'nested_rand', False):
                    yield from gather(x, o)

        blocks = []
        for o in gather(root, None):
            if o.cls is None: continue
            for c, b in self.constraint_items(self.chain(o.cls)):
                blocks.append((o, c, b))
        cm_params = {}
        for o, c, b in blocks:
            pname = ident('CM_' + (o.prefix + b['name'] if o is not root else b['name']))
            if pname not in cm_params:
                cm_params[pname] = True
                on, why = (True, None)
                if o is root: on, why = cm_mode.get(b['name'], cm_mode.get(None, (True, None)))
                mod.params.append((pname, "1'b1" if on else "1'b0", f"constraint_mode of {c['name']}::{b['name']}" +
                                   (f": {why}" if why else '')))
            sub_res = res if o is root else ObjResolver(self, mod, o, o.chain)
            sub_res.cls_objs = self.chain(o.cls)
            sub_tx = tx if o is root else Tx(mod, sub_res, self)
            for i, it in enumerate(b['items']):
                ent = {'kind': 'constraint', 'class': cls['name'], 'block': b['name'], 'defined_in': c['name'],
                       'item': i, 'file': b.get('body', b).get('file', b['file']) if isinstance(b.get('body'), dict) else b['file'],
                       'line': it.get('line'), 'src': it.get('src'), 'object': o.prefix.rstrip('_') or None,
                       'site': site['where'] if site else None}
                self.constraint_item(sub_tx, it, ent, atoms, pname, b)
                entries.append(ent)
        # inline constraints of the call site
        if site is not None and site.get('inline'):
            mod.params.append(('INLINE_EN', "1'b1", f"inline constraints of randomize() at {site['where']}"))
            caller_res = ObjResolver(self, mod, root, root.chain, caller=site)
            caller_res.cls_objs = chain
            ctx_tx = Tx(mod, caller_res, self)
            res.ctx_vars = caller_res.ctx_vars      # the caller's state becomes ports below
            inl = site['inline']
            items = inl.get('list', []) if inl.get('k') == 'List' else [inl]
            disabled_soft = set()
            for i, it in enumerate(items):
                ent = {'kind': 'constraint', 'class': cls['name'], 'block': '<inline>', 'item': i,
                       'file': site['file'], 'line': it.get('line'), 'src': it.get('src'), 'site': site['where']}
                self.constraint_item(ctx_tx, it, ent, atoms, 'INLINE_EN', None, disabled_soft)
                entries.append(ent)
            # LRM 18.5.13: a soft constraint loses to any hard constraint it conflicts with. Inline
            # constraints usually target the same field to override a soft default: drop class soft
            # constraints on fields the inline constraints constrain (or name in disable soft).
            inl_refs = set().union(*[a['refs'] for a in atoms if a['param'] == 'INLINE_EN' and not a['soft']] or [set()])
            for a in atoms:
                if a['soft'] and a['param'] != 'INLINE_EN' and a['refs'] & disabled_soft:
                    a['dropped'] = 'disable soft at the call site'
                elif a['soft'] and a['param'] != 'INLINE_EN' and a['refs'] & inl_refs:
                    a['inline_override'] = True     # fallback when resolve_soft cannot decide
        # randomize(a, b): only the listed fields are random, the rest are held state (LRM 18.11)
        rand_names = None
        if rand_filter is not None:
            rand_names = set()
            for a in rand_filter:
                x = root.kids.get(a)
                if x is None: continue
                for v in leaves(x): rand_names.add(v.name)
                if isinstance(x, Arr) and x.size_var is not None: rand_names.add(x.size_var.name)
        # rand_mode(0): the field keeps its value, the constraints still apply to it (LRM 18.8)
        off = {nm for nm, (on, _) in rand_mode.items() if not on and nm is not None}
        if rand_mode.get(None, (True,))[0] is False:
            off |= {k for k in root.kids if (rand_mode.get(k) or (False,))[0] is False}
        off_vars = set()
        for nm in off:
            x = root.kids.get(ident(nm))
            if x is None: continue
            for v in leaves(x): off_vars.add(v.name)
            if isinstance(x, Arr) and x.size_var is not None: off_vars.add(x.size_var.name)
        if off_vars:
            if rand_names is None:
                rand_names = {v.name for v in leaves(root) if v.role == 'rand'}
                rand_names |= {x.size_var.name for x in leaves(root) if isinstance(x, Arr) and x.size_var is not None}
            rand_names -= off_vars
            notes.append('rand_mode(0) before the call: ' + ', '.join(sorted(off)) + ' held (state)')
        # implicit constraints of the rand fields
        for v in leaves(root):
            if v.role != 'rand' or (rand_names is not None and v.name not in rand_names): continue
            enum = v.ti.get('enum') if isinstance(v, Var) else v.enum
            if enum:
                vals = sorted({parse_const(e['value']).v & ((1 << (v.w if isinstance(v, Var) else v.ew)) - 1)
                               for e in enum})
                ent = {'kind': 'constraint', 'class': cls['name'], 'block': '<implicit>', 'item': v.name,
                       'src': f'{v.origin}: rand enum takes only its named values (LRM 18.4)', 'file': cls['file'],
                       'line': None, 'site': site['where'] if site else None}
                if isinstance(v, Var):
                    cond = ' || '.join(f"({v.name} == {lit(x, v.w, False)})" for x in vals)
                    self.add_atom(atoms, ent, '(' + cond + ')', {v.name}, False, None, 'enum')
                else:
                    for p in range(v.n):
                        el = f'{v.name}[{p * v.ew} +: {v.ew}]'
                        cond = ' || '.join(f"({el} == {lit(x, v.ew, False)})" for x in vals)
                        g = f"({lit(p, 32, True)} >= {v.size_var.name}) || " if v.size_var is not None and p % (v.n // (v.dims[0][1] - v.dims[0][0] + 1)) == 0 else ''
                        self.add_atom(atoms, ent, f'({g}{cond})', {v.name}, False, None, 'enum')
                ent['status'] = 'exact'
                entries.append(ent)
            if isinstance(v, Arr) and v.size_var is not None:
                # size() <= max is implied by the constraints unless the model is truncated; a truncated
                # model must not cap size(), or the constraints that need more elements become unsat
                ent = {'kind': 'constraint', 'class': cls['name'], 'block': '<implicit>', 'item': v.size_var.name,
                       'src': f'{v.origin}: 0 <= size()' + (f' (elements past {v.max} not modelled)' if v.trunc
                                                            else f' <= {v.max}'),
                       'file': cls['file'], 'line': None, 'site': site['where'] if site else None}
                bound = f"{v.size_var.name} >= 0" + ('' if v.trunc else f" && {v.size_var.name} <= {lit(v.max, 32, True)}")
                self.add_atom(atoms, ent, f"({bound})", {v.size_var.name}, False, None, 'bound')
                ent['status'] = 'approx' if v.trunc else 'exact'
                entries.append(ent)
        # ports
        mod.port('input', 'logic clk', 'clk')
        mod.port('input', 'logic reset', 'reset', 'assumes hold while reset is low')
        mod.port('input', 'logic en', 'en', 'randomize() happens this cycle: the constraints apply')
        used = set().union(*[a['refs'] for a in atoms]) if atoms else set()
        all_vars = list(leaves(root)) + [v for o in res.ctx_vars.values() for v in leaves_any(o)]
        for x in list(leaves(root)):
            if isinstance(x, Arr) and x.size_var is not None: all_vars.append(x.size_var)
        seen = set()
        for v in all_vars:
            if v.name in seen: continue
            seen.add(v.name)
            is_rand = v.role == 'rand' and (rand_names is None or v.name in rand_names)
            if not is_rand and v.name not in used: continue
            mod.port('input', v.decl() if isinstance(v, Var) else v.decl(), v.name,
                     ('rand ' if is_rand else 'state ') + v.origin +
                     (f' [{"][".join(f"{a}:{b}" for a, b in v.dims)}] x {v.ew}b' if isinstance(v, Arr) else ''))
            v.is_rand = is_rand
        self.resolve_soft(mod, atoms)
        # assumes
        for a in atoms:
            if a.get('dropped'): continue
            nm = mod.names('c_' + a['name'])
            a['wire'] = nm
            mod.body.append(f"  wire {nm} = {a['cond']};  // {a['entry'].get('file')}:{a['entry'].get('line')}")
            en = 'en'
            if a['param']: en = f"(en && {a['param']})"
            if a['soft']:
                if 'SOFT_EN' not in [p[0] for p in mod.params]:
                    mod.params.append(('SOFT_EN', "1'b1", 'soft constraints the solver keeps (LRM 18.5.13; see report); '
                                       '0: none'))
                en = f'({en} && SOFT_EN)'
            an = mod.names('a_' + a['name'])
            a['assume'] = an
            mod.body.append(f"  `ASSUME({an}, `IMPLIES({en}, {nm}), clk, reset)")
            a['entry'].setdefault('assumes', []).append(an)
        kept = [a for a in atoms if a.get('wire')]
        if self.a.expose and kept:
            # test hook: every atom as an output bit (LSB = first atom), order in mod.atom_order
            mod.port('output', f'logic [{len(kept) - 1}:0] fv_atoms', 'fv_atoms', 'atom values (--expose)')
            mod.body.append('  assign fv_atoms = {' + ', '.join(a['wire'] for a in reversed(kept)) + '};')
        mod.atom_order = kept
        live = [a for a in atoms if a.get('guard') and not a.get('dropped')]
        if live:
            # a guard (implication / if condition) that can never hold under the other constraints is
            # dead constraint code; the smoke runs check every guard can be reached
            mod.body.append('`ifdef FV_LIVENESS_COVERS')
            seen_g = set()
            for a in live:
                if a['guard'] in seen_g: continue
                seen_g.add(a['guard'])
                ln = mod.names('live_' + a['name'])
                a['live'] = ln
                mod.body.append(f"  always_ff @(posedge clk) if (reset === 1'b0 && {en_of(a)}) {ln}: cover ({a['guard']});")
            mod.body.append('`endif')
        for ent in entries:
            ent.setdefault('status', 'exact')
            if notes and ent['block'] == '<implicit>': pass
        mod.notes = notes
        mod.entries = entries
        mod.root = root
        mod.rand_names = rand_names
        mod.atoms = atoms
        mod.ctx_vars = res.ctx_vars
        return mod

    def resolve_soft(self, mod, atoms):
        """LRM 18.5.13: the solver keeps a soft constraint unless it conflicts with the hard
        constraints and the higher-priority soft constraints it kept (priority: inline over class,
        derived over base, later over earlier; `atoms` is in increasing priority). When the atoms
        involved read only rand variables that is a constant, decided here with yosys `sat`; when
        they read testbench state it depends on the state: kept as hard (approx: under) unless no
        state allows it, or an inline constraint of the call site constrains the same field.
        Decided with every constraint_mode on."""
        softs = [a for a in atoms if a['soft'] and not a.get('dropped')]
        if not softs: return
        rand = {nm for nm in mod.ports if (mod.ports[nm][2] or '').startswith('rand ')}
        live = [a for a in atoms if not a.get('dropped')]
        # connected components over shared rand variables
        parent = {}
        def find(x):
            while parent.setdefault(x, x) != x: x = parent[x]
            return x
        for a in live:
            rs = sorted(a['refs'] & rand)
            for r in rs[1:]: parent[find(r)] = find(rs[0])
        def comp(a):
            rs = a['refs'] & rand
            return find(min(rs)) if rs else None
        groups = {}
        for a in live: groups.setdefault(comp(a) or id(a), []).append(a)
        for grp in groups.values():
            gs = [a for a in grp if a['soft']]
            if not gs: continue
            hard = [a for a in grp if not a['soft']]
            stateful = any(a['refs'] - rand for a in grp)
            def note(a, msg, status=None):
                a['entry'].setdefault('notes', []).append(msg)
                if status and a['entry'].get('status', 'exact') == 'exact': a['entry']['status'] = status
            if not stateful and self.sat(mod, hard + gs):
                for a in gs: note(a, 'soft constraint kept: consistent with all other constraints (LRM 18.5.13)')
                continue
            kept = []
            for a in reversed(gs):                       # highest priority first
                r = self.sat(mod, hard + kept + [a])
                if r is False:
                    a['dropped'] = 'soft constraint conflicts with higher-priority constraints (LRM 18.5.13)'
                    note(a, a['dropped'] + ('' if not stateful else ' for every value of the state it reads'))
                elif r is True and not stateful:
                    kept.append(a)
                    note(a, 'soft constraint kept: consistent with the higher-priority constraints (LRM 18.5.13)')
                elif a.get('inline_override'):
                    a['dropped'] = 'soft constraint overridden by the call site\'s inline constraints'
                    note(a, a['dropped'] + ' (approx: exact when they conflict)', 'approx')
                else:
                    kept.append(a)
                    note(a, 'soft constraint kept as hard: whether the solver drops it depends on testbench '
                            'state' + (' (sat check timed out)' if r is None else '') + ' (approx: under)', 'approx')

    def sat(self, mod, atoms):
        """are the atoms' conditions satisfiable together (all inputs free)? None: undecided"""
        if not atoms: return True
        key = tuple(sorted(a['cond'] for a in atoms))
        cache = self.__dict__.setdefault('sat_cache', {})
        if key in cache: return cache[key]
        ports = [f"  input {mod.ports[nm][1]}," for nm in mod.port_order if nm not in ('clk', 'reset', 'en')]
        params = ''.join(f'  localparam {(x[3:] or ("bit",))[0]} {x[0]} = {x[1]};\n' for x in mod.params)
        src = ('module fv_sat_chk (\n' + '\n'.join(ports) + '\n  output ok\n);\n' + params + '\n'.join(mod.temps) +
               '\n  assign ok = ' + ' && '.join(f"({a['cond']})" for a in atoms) + ';\nendmodule\n')
        with tempfile.TemporaryDirectory(prefix='fvsat_') as d:
            with open(os.path.join(d, 'chk.sv'), 'w') as f: f.write(src)
            try:
                r = subprocess.run(['yosys', '-q', '-p', 'read_verilog -sv chk.sv; prep -top fv_sat_chk; '
                                    f'tee -o sat.log sat -set ok 1 -timeout {self.a.sat_timeout}'],
                                   cwd=d, capture_output=True, text=True, timeout=self.a.sat_timeout + 30)
                log = open(os.path.join(d, 'sat.log')).read() if os.path.exists(os.path.join(d, 'sat.log')) else ''
            except subprocess.TimeoutExpired:
                log = ''
        res = True if 'model found:' in log else (False if 'no model found' in log else None)
        cache[key] = res
        return res

    def unsat_core(self, mod):
        """a minimal set of the module's assumes that cannot hold together (deletion-based), or None"""
        core = list(mod.atom_order)
        if self.sat(mod, core) is not False: return None
        i = 0
        while i < len(core):
            if self.sat(mod, core[:i] + core[i + 1:]) is False: del core[i]
            else: i += 1
        return core

    def add_atom(self, atoms, ent, cond, refs, soft, param, kind='item', guard=None):
        base = f"{ent['block']}_{ent['item']}".replace('<', '').replace('>', '')
        atoms.append({'name': base, 'cond': cond, 'refs': set(refs), 'soft': soft, 'param': param,
                      'entry': ent, 'kind': kind, 'guard': guard})

    def constraint_item(self, tx, it, ent, atoms, param, block, disabled_soft=None):
        """translate one constraint item into guarded atoms; records status on ent"""
        n0 = len(atoms)
        notes = []
        status = ['exact']

        def note(msg, level='approx'):
            notes.append(msg)
            if level == 'info': return
            if level == 'unsupported' or status[0] == 'exact': status[0] = level

        def trunc_guard(p):
            """a guard that reads a truncated array is false past the model, dropping what it guards"""
            tc = tx.trunc_cond()
            if not tc: return p
            note('guard reads a dynamic array past its modelled elements: the guarded constraints hold only '
                 'while size() <= the model maximum (approx: over)')
            return f'(!({tc}) && {p})'

        def walk(n, guards, soft):
            k = n['k']
            if k == 'List':
                for x in n.get('list', []): walk(x, guards, soft)
            elif k == 'Expression':
                s = soft or n.get('isSoft', False)
                tx.start()
                try:
                    e = tx.boolean(tx.scalar(n['expr']))
                except Unsupported as ex:
                    note(f"not translated: {ex} ({n.get('src')}); omitted (approx: over)", 'unsupported')
                    return
                for m in tx.notes: note(m)
                refs = set(tx.refs) | set().union(*[g[1] for g in guards] or [set()])
                cond = e.t
                if tx.trunc:
                    note('reads a dynamic array past its modelled elements: holds only while size() <= '
                         'the model maximum (approx: over)')
                    cond = f'({tx.trunc_cond()} || {cond})'
                g = None
                if guards:
                    g = ' && '.join(x[0] for x in guards)
                    cond = f'(!({g}) || {cond})'
                self.add_atom(atoms, ent, cond, refs, s, param, guard=g)
            elif k == 'Implication':
                tx.start()
                try: p = tx.boolean(tx.scalar(n['predicate']))
                except Unsupported as ex:
                    note(f"implication predicate not translated: {ex}; body omitted (approx: over)", 'unsupported')
                    return
                walk(n['body'], guards + [(trunc_guard(p.t), set(tx.refs))], soft)
            elif k == 'Conditional':
                tx.start()
                try: p = tx.boolean(tx.scalar(n['predicate']))
                except Unsupported as ex:
                    note(f"if predicate not translated: {ex}; both branches omitted (approx: over)", 'unsupported')
                    return
                r = set(tx.refs)
                tc = tx.trunc_cond()
                walk(n['ifBody'], guards + [(trunc_guard(p.t), r)], soft)
                if n.get('elseBody'):
                    walk(n['elseBody'], guards + [(f'(!({tc}) && !{p.t})' if tc else f'!{p.t}', r)], soft)
            elif k == 'Foreach':
                tx.start()
                try:
                    a = tx.ex(n['arrayRef'])
                except Unsupported as ex:
                    note(f'foreach array not modelled: {ex}; omitted (approx: over)', 'unsupported')
                    return
                if isinstance(a, S):            # packed vector: iterate its (first) packed dimension
                    pd = ty_packed_dims(n['arrayRef'].get('ty')) or [(a.w - 1, 0)]
                    dims = n.get('loopDims') or []
                    lv = dims[0].get('loopVar') if dims else None
                    if lv is None or len(dims) > len(pd):
                        note('foreach over a packed array with skipped/extra dimensions; omitted (approx: over)',
                             'unsupported'); return
                    l, r = pd[0]
                    for i in range(min(l, r), max(l, r) + 1):
                        tx.env[lv['path']] = S(lit(i, 32, True), 32, True, K(i, 32, True))
                        try: walk(n['body'], guards, soft)
                        finally: tx.env.pop(lv['path'], None)
                    return
                if not isinstance(a, ARef):
                    note('foreach over a non-array; omitted (approx: over)', 'unsupported'); return
                dims = n.get('loopDims') or []
                arr = a.arr
                if a.arr.size_var is not None:
                    note(f'foreach over a dynamic array unrolled to its modelled maximum ({arr.max})', 'info')
                self.unroll(tx, n, a, dims, 0, guards, soft, walk, note)
            elif k == 'Uniqueness':
                tx.start()
                try:
                    elems = []
                    for x in n['items']:
                        r = tx.ex(x)
                        if isinstance(r, ARef): elems += tx.elements(r)
                        else: elems.append((r, None))
                except Unsupported as ex:
                    note(f'unique not translated: {ex}; omitted (approx: over)', 'unsupported'); return
                refs = set(tx.refs)
                tc = tx.trunc_cond()
                if tc: note('unique over a dynamic array past its modelled elements: holds only while size() <= '
                            'the model maximum (approx: over)')
                for i in range(len(elems)):
                    for j in range(i + 1, len(elems)):
                        (a_, ga), (b_, gb) = elems[i], elems[j]
                        gs = [g for g in (ga, gb) if g] + [g[0] for g in guards]
                        cond = f'({a_.t} != {b_.t})'
                        if gs: cond = f"(!({' && '.join(gs)}) || {cond})"
                        if tc: cond = f'({tc} || {cond})'
                        self.add_atom(atoms, ent, cond, refs, soft, param)
            elif k == 'SolveBefore':
                note('solve...before only orders the solver (distribution); no effect on the legal set', 'info')
            elif k == 'DisableSoft':
                tgt = strip_conv(n.get('target') or {})
                nm = (tgt.get('symbol') or {}).get('sym')
                if disabled_soft is not None and nm: disabled_soft.add(ident(nm))
                note('disable soft: the named field\'s class soft constraints are dropped at this site')
            elif k == 'Invalid':
                note('slang could not bind this constraint; omitted (approx: over)', 'unsupported')
            else:
                note(f'constraint kind {k} not translated; omitted (approx: over)', 'unsupported')

        walk(it, [], False)
        if it['k'] == 'Expression' and strip_conv(it.get('expr', {})).get('k') == 'Dist':
            note('dist: weights only shape the distribution; the legal set (non-zero weights) is assumed', 'info')
        # soft items: kept or dropped (and the status) is decided by resolve_soft
        ent['status'] = status[0]
        if len(atoms) == n0 and status[0] == 'exact' and it['k'] not in ('SolveBefore',):
            if it['k'] not in ('SolveBefore', 'DisableSoft'): ent['status'] = 'exact'
        if notes: ent.setdefault('notes', []).extend(notes)
        ent['n_atoms'] = len(atoms) - n0

    def unroll(self, tx, n, a, dims, d, guards, soft, walk, note):
        if d >= len(dims):
            walk(n['body'], guards, soft); return
        dim = dims[d]
        lv = dim.get('loopVar')
        arr = a.arr
        l, r = arr.dims[a.depth + d] if a.depth + d < len(arr.dims) else (0, -1)
        rng = dim.get('range')
        if rng and isinstance(rng, dict) and 'left' in rng:
            l, r = rng['left'], rng['right']
        idxs = range(min(l, r), max(l, r) + 1)
        for i in idxs:
            g = list(guards)
            if d == 0 and a.depth == 0 and arr.size_var is not None:
                g = g + [(f"({lit(i, 32, True)} < {arr.size_var.name})", {arr.size_var.name})]
            if lv is None:          # skipped dimension: foreach (a[, j])
                self.unroll(tx, n, a, dims, d + 1, g, soft, walk, note); continue
            key = lv.get('path')
            tx.env[key] = S(lit(i, 32, True), 32, True, K(i, 32, True))
            try:
                self.unroll(tx, n, a, dims, d + 1, g, soft, walk, note)
            finally:
                tx.env.pop(key, None)

    def build_randomizer(self, cm, cls, site=None):
        """fv_r_: object fields as registers; `randomize` loads anyseq values that satisfy the
        constraints (fv_c_ with en = randomize); fields not randomized hold (LRM 18.11)"""
        mname = 'fv_r_' + cm.name[len('fv_c_'):]
        mod = Module(mname, 'randomizer', cm.origin.replace('constraints of', 'randomize() model of'))
        mod.params = list(cm.params)
        mod.port('input', 'logic clk', 'clk')
        mod.port('input', 'logic reset', 'reset')
        mod.port('input', 'logic randomize', 'randomize', 'obj.randomize() is called this cycle')
        conns = ['.clk(clk)', '.reset(reset)', '.en(randomize)']
        body = []
        for nm in cm.port_order:
            d, decl, c = cm.ports[nm]
            if nm in ('clk', 'reset', 'en'): continue
            if c.startswith('rand '):
                mod.port('output', decl, nm, c)
                nxt = mod.names('nxt_' + nm)
                body.append(f'  (* anyseq *) {decl.replace(" " + nm, " " + nxt)};')
                init = self.init_of(cm.root, nm)
                body.append(f'  initial {nm} = {init};' if init else f"  initial {nm} = '0;")
                body.append(f'  always_ff @(posedge clk) if (randomize) {nm} <= {nxt};')
                conns.append(f'.{nm}({nxt})')
            else:
                if c.startswith('state ') and self.is_own_field(cm, nm):
                    # a field of the object itself that is not randomized here: held state
                    mod.port('input', decl, nm, c)
                else:
                    mod.port('input', decl, nm, c)
                conns.append(f'.{nm}({nm})')
        params = ', '.join(f'.{p}({p})' for p, _, _ in cm.params)
        body.append(f"  {cm.name} {'#(' + params + ') ' if params else ''}u_c (\n    " +
                    ',\n    '.join(conns) + '\n  );')
        mod.body = body
        return mod

    def init_of(self, root, nm):
        for v in leaves(root):
            if v.name == nm and isinstance(v, Var) and v.init is not None and v.init.x == 0:
                return lit(v.init.v, v.w, v.signed)
        return None

    def is_own_field(self, cm, nm):
        return any(v.name == nm for v in leaves(cm.root))

    # ---- call sites
    def mode_calls(self, routine, receiver, before=None):
        """constraint_mode()/rand_mode() setter calls of a routine on `receiver`, in source order"""
        out = []
        for c in (routine or {}).get('facts', {}).get('calls', []):
            if c.get('name') not in ('constraint_mode', 'rand_mode') or c.get('value') is None: continue
            if c.get('receiver') != receiver: continue
            if before is not None and (c.get('line') or 0) >= before: continue
            try: on = parse_const(c['value']).v != 0
            except Exception: continue
            out.append((c['name'], c.get('member'), on, f"{c['src']} ({routine.get('file')}:{c.get('line')})"))
        return out

    def routine_at(self, file, line):
        for s in self.d['subroutines'] + self.d.get('procedures', []):
            if s.get('file') == file and (s.get('start') or 0) <= line <= (s.get('end') or -1):
                return s
        return None

    def modes(self, chain, site):
        """(constraint_mode, rand_mode): {block/field name or None (all): (on, why)} in effect at
        the call: the class's own new()/pre_randomize() (base first), then the calls made on the object
        before the call in the calling routine (assumed to run before it: source order)"""
        calls = []
        for c in reversed(chain):
            for s in self.d['subroutines']:
                parts = (s.get('path') or '').split('::')
                if len(parts) >= 2 and parts[-2] == c['name'] and parts[-1] in ('new', 'pre_randomize'):
                    calls += self.mode_calls(s, 'this')
        if site is not None:
            calls += site.get('modes', [])
        cm, rm = {}, {}
        for kind, member, on, why in calls:
            d = cm if kind == 'constraint_mode' else rm
            if member is None: d.clear()        # obj.constraint_mode(v): every block
            d[member] = (on, why)
        return cm, rm

    def sites(self):
        """randomize() calls with inline constraints or an argument list; std::randomize"""
        out = []
        for r in self.d['randomization']:
            if r['kind'] not in ('randomize', 'std::randomize') or 'ast' not in r: continue
            ast = r['ast']
            info = ast.get('subroutine') or {}
            inline = (info.get('extraInfo') or {}).get('inlineConstraints')
            args = ast.get('arguments') or []
            where = f"{r.get('owner')}::{r.get('subroutine') or r.get('procedure')} {r['file']}:{r['line']}"
            tag = ident(f"{r.get('subroutine') or r.get('procedure') or 'p'}_L{r['line']}")
            if r['kind'] == 'std::randomize':
                out.append({'kind': 'std', 'cls': None, 'inline': inline, 'args': args, 'where': where,
                            'tag': tag, 'file': r['file'], 'src': r['src'], 'owner': r.get('owner')})
                continue
            tgt_cls = None
            names = []
            encl = self.class_at(r['file'], r['line'])
            if r.get('target') == 'this' or not args or (args and args[0].get('k') != 'NamedValue' and
                                                          args[0].get('k') != 'MemberAccess'):
                tgt_cls = encl or self.cls(r.get('owner'))
                names = [strip_conv(a).get('symbol', {}).get('sym') for a in args if a.get('k') != 'EmptyArgument']
            else:
                tgt_cls = self.cls(args[0].get('ty', ''), encl['path'] if encl else None)
                names = [strip_conv(a).get('symbol', {}).get('sym') for a in args[1:]]
                # `obj.randomize(x)`: slang lists the target first
            if r.get('target') == 'this':
                names = [strip_conv(a).get('left', a).get('symbol', {}).get('sym') if strip_conv(a).get('k') == 'Assignment'
                         else strip_conv(a).get('symbol', {}).get('sym') for a in args]
            names = [x for x in names if x and x != 'this']     # this.randomize(): slang lists `this` first
            modes = self.mode_calls(self.routine_at(r['file'], r['line']), r.get('target') or 'this', r['line'])
            if inline is None and not names and not modes: continue
            if tgt_cls is None:
                self.report.append({'kind': 'site', 'site': where, 'src': r['src'], 'status': 'unsupported',
                                    'notes': ['class of the randomized object not in the extraction']})
                continue
            out.append({'kind': 'obj', 'cls': tgt_cls, 'inline': inline, 'args': names or None, 'where': where,
                        'tag': tag, 'file': r['file'], 'src': r['src'], 'modes': modes})
        return out

    def build_std(self, site):
        """std::randomize(v...) with {...}: the listed variables are random, everything else state"""
        mname = self.mod_names(f"fv_c_std__{site['owner'] or 'scope'}__{site['tag']}")
        mod = Module(mname, 'constraints', f"std::randomize at {site['where']}: {site['src']}")
        root = Obj(None, '', 'rand', site['where'])
        std_vars = {}
        notes = []
        for a in site['args']:
            a0 = a
            if a0.get('k') == 'Assignment': a0 = a0['left']
            a0 = strip_conv(a0)
            sym = a0.get('symbol') or {}
            if not sym: continue
            nm = sym['sym']
            if a0.get('w') is not None:
                v = Var(ident(nm), a0['w'], bool(a0.get('signed')), 'rand', sym.get('path', nm))
                root.kids[ident(nm)] = v
                std_vars[sym.get('path')] = ident(nm)
            else:
                notes.append(f'{nm}: non-integral std::randomize variable not modelled')
        res = ObjResolver(self, mod, root, [], std_vars=std_vars)
        res.cls_objs = []
        tx = Tx(mod, res, self)
        atoms, entries = [], []
        inl = site['inline']
        if inl is not None:
            items = inl.get('list', []) if inl.get('k') == 'List' else [inl]
            for i, it in enumerate(items):
                ent = {'kind': 'constraint', 'class': None, 'block': '<std::randomize>', 'item': i,
                       'file': site['file'], 'line': it.get('line'), 'src': it.get('src'), 'site': site['where']}
                self.constraint_item(tx, it, ent, atoms, None, None)
                entries.append(ent)
        mod.port('input', 'logic clk', 'clk'); mod.port('input', 'logic reset', 'reset')
        mod.port('input', 'logic en', 'en', 'std::randomize() happens this cycle')
        used = set().union(*[a['refs'] for a in atoms]) if atoms else set()
        for v in list(root.kids.values()) + [v for o in res.ctx_vars.values() for v in leaves_any(o)]:
            if v.role != 'rand' and v.name not in used: continue
            mod.port('input', v.decl(), v.name, ('rand ' if v.role == 'rand' else 'state ') + v.origin)
        for a in atoms:
            nm = mod.names('c_' + a['name']); a['wire'] = nm
            mod.body.append(f"  wire {nm} = {a['cond']};  // {a['entry'].get('file')}:{a['entry'].get('line')}")
            an = mod.names('a_' + a['name'])
            mod.body.append(f"  `ASSUME({an}, `IMPLIES(en, {nm}), clk, reset)")
            a['entry'].setdefault('assumes', []).append(an)
        mod.atom_order = [a for a in atoms if a.get('wire')]
        if self.a.expose and mod.atom_order:
            mod.port('output', f'logic [{len(mod.atom_order) - 1}:0] fv_atoms', 'fv_atoms', 'atom values (--expose)')
            mod.body.append('  assign fv_atoms = {' + ', '.join(a['wire'] for a in reversed(mod.atom_order)) + '};')
        mod.notes, mod.entries, mod.root, mod.rand_names, mod.atoms = notes, entries, root, None, atoms
        mod.ctx_vars = res.ctx_vars
        return mod

    # ---- run
    def run(self):
        want = re.compile(self.a.classes) if self.a.classes else None
        for cls in self.d['classes']:
            if want and not want.search(cls['name']): continue
            if cls.get('macro_generated') and not cls['constraints']: continue
            chain = self.chain(cls)
            has_rand = any(p['rand'] in ('Rand', 'RandC') for c in chain for p in c['properties'])
            has_cons = any(c['constraints'] for c in chain)
            if not (has_rand and has_cons) and not cls['constraints']: continue
            if not cls['constraints'] and not self.a.inherited: continue
            self.emit_constraint_module(cls)
        for site in self.sites():
            if site['kind'] == 'std':
                if want and not want.search(site['owner'] or ''): continue
                try: mod = self.build_std(site)
                except Unsupported as e:
                    self.report.append({'kind': 'site', 'site': site['where'], 'status': 'unsupported', 'notes': [str(e)]})
                    continue
                self.finish_c(mod, None)
                continue
            if want and not want.search(site['cls']['name']): continue
            self.emit_constraint_module(site['cls'], site)
        for cg in self.d['coverage']['covergroups']:
            if cg.get('uninstantiated'): continue
            if want and not want.search(cg.get('owner') or cg['path']): continue
            CoverGen(self, cg).build()
        if self.a.witness: self.plan_witness()

    def plan_witness(self):
        """for each covergroup whose sampled values are fields of a randomized class: that class's
        randomize() model (fv_r_) drives the covergroup, sampled the cycle after randomize. A port is
        `pin` (a rand field: a replay can force it), `state` (a non-rand field the constraints read:
        free, held between calls) or `free` (anything else: monitor data, signals, other objects)."""
        def class_mod(c):
            for cm, rm in self.mods_c:
                if getattr(cm, 'site', 1) is None and rm is not None and cm.cls['path'] == c['path']:
                    return cm, rm
            return None
        for m in self.mods_cg:
            m.wit = None
            srcs = getattr(m.res, 'src', {})
            tally = {}
            for port, (c, path) in srcs.items(): tally.setdefault(c['path'], [c, 0])[1] += 1
            for c, _ in sorted(tally.values(), key=lambda x: -x[1]):
                if class_mod(c) is None and any(p['rand'] in ('Rand', 'RandC')
                                                for x in self.chain(c) for p in x['properties']):
                    self.emit_constraint_module(c)
                pair = class_mod(c)
                if pair is None: continue
                cm, rm = pair
                outs = {nm for nm in rm.port_order if rm.ports[nm][0] == 'output'}
                ins = {nm for nm in rm.port_order if rm.ports[nm][0] == 'input'} - {'clk', 'reset', 'randomize'}
                props = {p['name']: p for x in reversed(self.chain(c)) for p in x['properties']}
                ports = {}
                for port in m.port_order:
                    if port in ('clk', 'reset', 'sample'): continue
                    sc = srcs.get(port)
                    if sc is None or sc[0]['path'] != c['path']:
                        ports[port] = ('free', None, 'not a field of ' + c['name']); continue
                    leaf = ident('__'.join(sc[1]))
                    vis = (props.get(sc[1][0]) or {}).get('visibility', '')
                    if leaf in outs and 'local' not in str(vis).lower():
                        ports[port] = ('pin', leaf, '.'.join(sc[1]))
                    elif leaf in outs:
                        ports[port] = ('free', leaf, f"{'.'.join(sc[1])} is local to {c['name']}: a derived class cannot constrain it")
                    elif leaf in ins:
                        ports[port] = ('state', leaf, f"{'.'.join(sc[1])} is not rand (testbench state)")
                    else:
                        ports[port] = ('free', None, f"{'.'.join(sc[1])} is not rand and no constraint reads it")
                if any(k == 'pin' for k, _, _ in ports.values()):
                    m.wit = {'cls': c, 'cm': cm, 'rm': rm, 'ports': ports}
                break

    def emit_constraint_module(self, cls, site=None):
        try:
            cm = self.build_constraints(cls, site)
        except Unsupported as e:
            self.report.append({'kind': 'class', 'class': cls['name'], 'status': 'unsupported', 'notes': [str(e)],
                                'site': site['where'] if site else None})
            return
        rm = self.build_randomizer(cm, cls, site)
        self.finish_c(cm, rm)

    def finish_c(self, cm, rm):
        self.mods_c.append((cm, rm))
        for e in cm.entries: self.report.append(e)
        if cm.notes:
            self.report.append({'kind': 'model', 'module': cm.name, 'status': 'approx', 'notes': cm.notes})


def en_of(a):
    en = 'en' if not a['param'] else f"en && {a['param']}"
    return f'({en} && SOFT_EN)' if a['soft'] else f'({en})'


def leaves(o):
    if isinstance(o, (Var, Arr)):
        yield o; return
    for x in o.kids.values():
        if isinstance(x, (Var, Arr)): yield x
        elif isinstance(x, Obj): yield from leaves(x)


def leaves_any(o):
    if isinstance(o, (Var, Arr)): yield o
    else: yield from leaves(o)


def single_return(body):
    """the expression of a body that is just `return <expr>;` (possibly in a block)"""
    b = body
    while isinstance(b, dict) and b.get('k') in ('Block', 'List'):
        items = b.get('list') if b['k'] == 'List' else [b.get('body')]
        items = [x for x in (items or []) if x and x.get('k') not in ('Empty',)]
        if len(items) != 1: return None
        b = items[0]
    if isinstance(b, dict) and b.get('k') == 'Return' and b.get('expr') is not None:
        return b['expr']
    return None


def size_bound(e, prop):
    """upper bound on prop.size() stated by expression e (<, <=, ==, inside with constant bounds)"""
    e = strip_conv(e)
    k = e.get('k')

    def is_size(x):
        x = strip_conv(x)
        return (x.get('k') == 'Call' and x.get('subroutineName') in ('size', 'num') and x.get('arguments')
                and strip_conv(x['arguments'][0]).get('symbol', {}).get('sym') == prop)
    if k == 'BinaryOp':
        op = e['op']
        if op == 'LogicalAnd':
            a, b = size_bound(e['left'], prop), size_bound(e['right'], prop)
            return min(x for x in (a, b) if x is not None) if a is not None or b is not None else None
        l, r = e['left'], e['right']
        kr, kl = nconst(r) or nconst(strip_conv(r)), nconst(l) or nconst(strip_conv(l))
        if is_size(l) and kr is not None:
            return {'LessThan': kr.v - 1, 'LessThanEqual': kr.v, 'Equality': kr.v}.get(op)
        if is_size(r) and kl is not None:
            return {'GreaterThan': kl.v - 1, 'GreaterThanEqual': kl.v, 'Equality': kl.v}.get(op)
    if k in ('Inside', 'Dist') and is_size(e['left']):
        hi = None
        items = e['rangeList'] if k == 'Inside' else [i['value'] for i in e['items']]
        for it in items:
            it = strip_conv(it)
            if it.get('k') == 'ValueRange':
                kk = nconst(it['right']) or nconst(strip_conv(it['right']))
            else:
                kk = nconst(it)
            if kk is None: return None
            hi = kk.v if hi is None else max(hi, kk.v)
        return hi
    return None


# ======================================================================== coverage
class Bin:
    def __init__(self, name, cond, kind, values=None, entry=None):
        self.name, self.cond, self.kind, self.values, self.entry = name, cond, kind, values, entry


class CoverGen:
    """One covergroup -> fv_cg_ module (LRM clause 19)."""

    def __init__(self, gen, cg):
        self.gen, self.cg = gen, cg
        owner = cg.get('owner') or cg['path'].rsplit('.', 1)[0]
        self.owner = owner
        self.mod = Module(gen.mod_names(f"fv_cg_{owner.split('::')[-1].split('.')[-1]}__{cg['name']}"), 'covergroup',
                          f"covergroup {cg['name']} ({cg['file']}:{cg['start']}) of {owner}")
        self.res = CgResolver(gen, self.mod, cg)
        self.tx = Tx(self.mod, self.res, gen)
        self.cg_opts = parse_opt_strings(cg.get('options', []))
        self.entries = []
        self.hits = []
        self.n_cover = self.n_assert = 0

    def opt(self, cp, name, default):
        for o in cp.get('options', []) if cp else []:
            if o['name'] == name and o.get('value') is not None:
                k = parse_const(o['value'])
                if k is not None: return k.v
        if name in self.cg_opts: return self.cg_opts[name]
        return default

    def build(self):
        cg, mod = self.cg, self.mod
        mod.port('input', 'logic clk', 'clk')
        mod.port('input', 'logic reset', 'reset', 'covers/asserts are off while reset is high')
        ev = parse_event(cg.get('sample_event'))
        self.edge = 'posedge'
        if ev is None:
            mod.port('input', 'logic sample', 'sample', 'the covergroup\'s sample() is called this cycle')
            self.smp = 'sample'
            self.mod.clock_src = None
        else:
            self.edge, clk_sig, iff = ev
            self.mod.clock_src = clk_sig
            mod.port('input', 'logic sample', 'sample', f'sampling event {cg["sample_event"]}: tie to 1 when clk is {clk_sig}')
            self.smp = 'sample'
            if iff:
                try:
                    self.smp = None
                    self.mod.event_iff = iff
                except Exception:
                    pass
                self.smp = 'sample'
                self.entry_note(None, f'sampling event has an iff condition ({iff}); drive `sample` with it')
        cps = {}
        for cp in cg['coverpoints']:
            # unlabelled: named after its expression, as simulators report it
            cp = {**cp, 'name': ident((cp.get('name') or '').strip() or (cp.get('expr') or {}).get('src') or 'cp')}
            try:
                cps[cp['name']] = self.coverpoint(cp)
            except Unsupported as e:
                self.entries.append({'kind': 'coverpoint', 'covergroup': cg['name'], 'owner': self.owner,
                                     'coverpoint': cp['name'], 'file': cp['file'], 'line': cp['start'],
                                     'status': 'unsupported', 'notes': [str(e)]})
        for cx in cg['crosses']:
            try: self.cross(cx, cps)
            except Unsupported as e:
                self.entries.append({'kind': 'cross', 'covergroup': cg['name'], 'owner': self.owner,
                                     'cross': cx['name'], 'file': cx['file'], 'line': cx['start'],
                                     'status': 'unsupported', 'notes': [str(e)]})
        for nm, (w, s, c) in self.res.inputs.items():
            mod.port('input', f"logic {'signed ' if s else ''}[{w - 1}:0] {nm}", nm, c)
        if self.gen.a.expose and self.hits:
            mod.port('output', f'logic [{len(self.hits) - 1}:0] fv_hits', 'fv_hits', 'bin hits (--expose)')
            mod.body.append('  assign fv_hits = {' + ', '.join(f'({w})' for _, w in reversed(self.hits)) + '};')
        mod.hit_order = [n for n, _ in self.hits]
        mod.hit_wires = dict(self.hits)
        if self.hits:
            mod.params += [('FV_COVER_LO', '0', 'first cover enabled (formal runs split covergroups)', 'int'),
                           ('FV_COVER_HI', str(len(self.hits)), 'covers enabled: index < FV_COVER_HI', 'int')]
        self.gen.mods_cg.append(mod)
        mod.entries = self.entries
        mod.sample_event = cg.get('sample_event')
        mod.res = self.res
        self.gen.report.extend(self.entries)

    def entry_note(self, ent, msg):
        if ent is not None: ent.setdefault('notes', []).append(msg)

    # ---- emitters
    def cover(self, name, cond, ent, at_least=1):
        mod = self.mod
        w = mod.names('h_' + name)
        mod.body.append(f'  wire {w} = {self.smp} && {cond};')
        cn = mod.names('cov_' + name)
        if at_least > 1:            # option.at_least: the bin counts once hit N times
            cw = max(1, (at_least).bit_length())
            cnt = mod.names('n_' + name)
            mod.body.append(f"  logic [{cw - 1}:0] {cnt} = '0;")
            mod.body.append(f"  always_ff @({self.edge} clk) if (!reset && {w} && {cnt} != {lit(at_least, cw, False)}) "
                            f"{cnt} <= {cnt} + 1'b1;")
            w = f'({w} && {cnt} == {lit(at_least - 1, cw, False)})'
        mod.body.append(self.stmt('COVER', cn, w, len(self.hits)))
        ent.setdefault('covers', []).append(cn)
        self.hits.append((cn, w))
        self.n_cover += 1

    def illegal(self, name, cond, ent):
        an = self.mod.names('ill_' + name)
        self.mod.body.append('`ifndef FV_NO_ILLEGAL_BINS')
        self.mod.body.append(self.stmt('ASSERT', an, f'!({self.smp} && {cond})'))
        self.mod.body.append('`endif')
        ent.setdefault('asserts', []).append(an)
        self.n_assert += 1

    def stmt(self, kind, name, prop, idx=None):
        # same reset gating as prim_assert's COVER/ASSERT, but the label is on the statement itself
        # so SymbiYosys reports each cover by its bin name. Cover idx is on only inside
        # [FV_COVER_LO, FV_COVER_HI): formal runs split large covergroups into parallel jobs
        if kind == 'ASSERT' and self.edge == 'posedge':
            return f'  `ASSERT({name}, {prop}, clk, reset)'
        k = {'COVER': 'cover', 'ASSERT': 'assert'}[kind]
        sel = f' && FV_COVER_LO <= {idx} && {idx} < FV_COVER_HI' if idx is not None else ''
        return f"  always_ff @({self.edge} clk) if (reset === 1'b0{sel}) {name}: {k} ({prop});"

    # ---- coverpoints
    def coverpoint(self, cp):
        mod, tx = self.mod, self.tx
        ti = cp.get('ti') or {}
        w = ti.get('w') or cp.get('width')
        if not w: raise Unsupported(f"coverpoint {cp['name']}: type {cp.get('type')} not integral")
        sg = bool(ti.get('signed'))
        tx.start()
        x = tx.scalar(cp['expr'])
        x = tx.cast(x, w, sg)
        cpv = mod.names('cp_' + cp['name'])
        mod.body.append(f"  wire {'signed ' if sg else ''}[{w - 1}:0] {cpv} = {x.t};  // {cp['file']}:{cp['start']} coverpoint {cp['name']}")
        iff = "1'b1"
        if cp.get('iff'):
            iff_s = tx.boolean(tx.scalar(cp['iff']))
            iffn = mod.names('iff_' + cp['name'])
            mod.body.append(f'  wire {iffn} = {iff_s.t};')
            iff = iffn
        at_least = self.opt(cp, 'at_least', 1)
        cpinfo = {'name': cp['name'], 'v': cpv, 'w': w, 's': sg, 'iff': iff, 'bins': [], 'ti': ti}
        regular, ignore, illegal, default, trans = [], [], [], [], []
        for b in cp['bins']:
            if b.get('default'): default.append(b)
            elif b.get('transition') or b.get('trans'): trans.append(b)
            elif b['kind'] == 'IgnoreBins': ignore.append(b)
            elif b['kind'] == 'IllegalBins': illegal.append(b)
            else: regular.append(b)
        base_ent = lambda b: {'kind': 'bin', 'covergroup': self.cg['name'], 'owner': self.owner,
                              'coverpoint': cp['name'], 'bin': b['name'], 'bin_kind': b['kind'],
                              'file': b.get('file'), 'line': b.get('start'), 'src': b.get('src'),
                              'status': 'exact'}
        # value sets of ignore / illegal bins are removed from every other bin (LRM 19.5.5/19.5.6)
        excl, excl_vals = [], set()
        for b in ignore + illegal:
            ent = base_ent(b)
            try:
                c, vals = self.bin_set(cp, cpinfo, b)
            except Unsupported as e:
                ent['status'] = 'unsupported'; ent.setdefault('notes', []).append(str(e))
                self.entries.append(ent); continue
            excl.append(c)
            if vals is not None and b.get('iffExpr') is None and b.get('withExpr') is None:
                excl_vals |= set(vals)
            if b['kind'] == 'IllegalBins':
                self.illegal(f"{cp['name']}__{b['name']}", f'{iff} && {c}', ent)
                ent['notes'] = ['illegal bin: an assertion (simulation reports an error when it is hit)']
            else:
                ent['notes'] = ['ignore bin: its values are removed from the other bins; no cover']
            self.entries.append(ent)
        ex_cond = ' && '.join(f'!{e}' for e in excl) if excl else None

        def emit_bin(name, cond, ent, values=None):
            ivs = to_ivs(values)
            if ivs is not None and excl_vals:
                size = sum(h - l + 1 for l, h in ivs)
                if size <= len(excl_vals) and all(v in excl_vals for l, h in ivs for v in range(l, h + 1)):
                    ent.setdefault('notes', []).append(f'{name}: all values ignored/illegal, bin removed')
                    return
            values = ivs
            full = f'{iff} && {cond}' + (f' && {ex_cond}' if ex_cond else '')
            self.cover(f"{cp['name']}__{name}", full, ent, at_least)
            cpinfo['bins'].append(Bin(name, f'({cond}' + (f' && {ex_cond})' if ex_cond else ')'), 'Bins', values, ent))

        if regular:
            for b in regular:
                ent = base_ent(b)
                try:
                    self.value_bin(cp, cpinfo, b, ent, emit_bin)
                except Unsupported as e:
                    ent['status'] = 'unsupported'; ent.setdefault('notes', []).append(str(e))
                self.entries.append(ent)
        elif not trans or ignore or illegal:
            self.auto_bins(cp, cpinfo, emit_bin, base_ent)
        for b in trans:
            ent = base_ent(b)
            try:
                self.trans_bin(cp, cpinfo, b, ent, iff, at_least)
            except Unsupported as e:
                ent['status'] = 'unsupported'; ent.setdefault('notes', []).append(str(e))
            self.entries.append(ent)
        for b in default:
            ent = base_ent(b)
            if b.get('default_sequence'):
                ent['status'] = 'unsupported'
                ent['notes'] = ['default sequence bin: not modelled (excluded from coverage by the LRM)']
            else:
                others = [x.cond for x in cpinfo['bins']] + excl
                cond = '!(' + ' || '.join(others) + ')' if others else "1'b1"
                self.cover(f"{cp['name']}__{b['name']}", f'{iff} && {cond}', ent, at_least)
                ent['notes'] = ['default bin: values in no other bin; informational (not part of coverage, LRM 19.5)']
            self.entries.append(ent)
        return cpinfo

    def values_of(self, cp, cpinfo, b):
        """the bin's value list: [('v', K)] / [('r', K|None, K|None)] / [('e', S)] in source order"""
        out = []
        tx = self.tx
        for v in b.get('values') or []:
            vk = strip_conv(v)
            if vk.get('k') == 'ValueRange':
                ends = []
                for side in ('left', 'right'):
                    e = vk[side]
                    if strip_conv(e).get('k') == 'UnboundedLiteral': ends.append(None); continue
                    c = nconst(e) or nconst(strip_conv(e))
                    if c is None: ends.append(('e', tx.cast(tx.scalar(e), cpinfo['w'], cpinfo['s'])))
                    else: ends.append(K(kval(c, cpinfo['w'], cpinfo['s']), cpinfo['w'], cpinfo['s'], c.x))
                out.append(('r', ends[0], ends[1]))
            else:
                c = nconst(v)
                if c is not None:
                    out.append(('v', K(kval(c, cpinfo['w'], cpinfo['s']), cpinfo['w'], cpinfo['s'],
                                       c.x & ((1 << cpinfo['w']) - 1))))
                else:
                    r = tx.ex(v)
                    if isinstance(r, ARef):
                        for e, g in tx.elements(r): out.append(('e', e))
                    else:
                        out.append(('e', tx.cast(r, cpinfo['w'], cpinfo['s'])))
        return out

    def tmin_max(self, cpinfo):
        w, s = cpinfo['w'], cpinfo['s']
        return (-(1 << (w - 1)), (1 << (w - 1)) - 1) if s else (0, (1 << w) - 1)

    def item_cond(self, cpinfo, it, wildcard):
        v = cpinfo['v']
        w = cpinfo['w']
        if it[0] == 'v':
            k = it[1]
            if k.x and wildcard:
                mask = ((1 << w) - 1) & ~k.x
                return f"(({v} & {lit(mask, w, False)}) == {lit(k.v & mask, w, False)})"
            if k.x: raise Unsupported('x/z value in a non-wildcard bin')
            return f'({v} == {lit(k.v, w, cpinfo["s"])})'
        if it[0] == 'e':
            return f'({v} == {it[1].t})'
        lo, hi = it[1], it[2]
        parts = []
        if lo is not None: parts.append(f"({v} >= {lo[1].t if isinstance(lo, tuple) else lit(lo.v, w, cpinfo['s'])})")
        if hi is not None: parts.append(f"({v} <= {hi[1].t if isinstance(hi, tuple) else lit(hi.v, w, cpinfo['s'])})")
        return '(' + ' && '.join(parts) + ')' if parts else "1'b1"

    def bin_set(self, cp, cpinfo, b):
        """(condition text, constant value list or None) of a whole (non-array) value bin"""
        items = self.values_of(cp, cpinfo, b)
        conds = [self.item_cond(cpinfo, it, b.get('wildcard')) for it in items]
        c = '(' + ' || '.join(conds) + ')' if conds else "1'b0"
        if b.get('withExpr') is not None:
            c = f'({c} && {self.with_cond(b, cpinfo, cpinfo["v"])})'
        if b.get('iffExpr') is not None:
            c = f"({c} && {self.tx.boolean(self.tx.scalar(b['iffExpr'])).t})"
        return c, self.const_values(cpinfo, items, b.get('wildcard'))

    def with_cond(self, b, cpinfo, value_text, value_k=None):
        """bins ... with (expr): `item` is the candidate value"""
        tx = self.tx
        path = next((p for p in find_iter_paths(b['withExpr'])), None)
        val = S(value_text, cpinfo['w'], cpinfo['s'], value_k, True)
        if path: tx.env[path] = val
        tx.env['#item'] = val
        try:
            return tx.boolean(tx.scalar(b['withExpr'])).t
        finally:
            if path: tx.env.pop(path, None)
            tx.env.pop('#item', None)

    def const_values(self, cpinfo, items, wildcard, cap=None):
        """expand a value list into individual constants (None when not constant / too many)"""
        cap = cap or self.gen.a.max_bins
        lo_t, hi_t = self.tmin_max(cpinfo)
        out = []
        for it in items:
            if it[0] == 'e': return None
            if it[0] == 'v':
                k = it[1]
                if k.x:
                    if not wildcard: return None
                    xb = [i for i in range(cpinfo['w']) if k.x >> i & 1]
                    if (1 << len(xb)) > cap: return None
                    for m in range(1 << len(xb)):
                        v = k.v & ~k.x
                        for j, bit in enumerate(xb):
                            if m >> j & 1: v |= 1 << bit
                        out.append(to_signed(v, cpinfo))
                else:
                    out.append(k.v)
            else:
                lo, hi = it[1], it[2]
                if isinstance(lo, tuple) or isinstance(hi, tuple): return None
                a = lo.v if lo is not None else lo_t
                b = hi.v if hi is not None else hi_t
                if b - a + 1 > cap: return None
                out.extend(range(a, b + 1))
            if len(out) > cap: return None
        return out

    def value_bin(self, cp, cpinfo, b, ent, emit_bin):
        items = self.values_of(cp, cpinfo, b)
        wild = b.get('wildcard')
        iff_b = None
        if b.get('iffExpr') is not None:
            iff_b = self.tx.boolean(self.tx.scalar(b['iffExpr'])).t
        nbins = None
        if b.get('numberOfBinsExpr') is not None:
            k = nconst(b['numberOfBinsExpr'])
            if k is None: raise Unsupported('bins [N]: N not constant')
            nbins = k.v
        if not b['array']:
            c, _ = self.bin_set(cp, cpinfo, b)
            emit_bin(ident(b['name']), c, ent)
            return
        if nbins is not None and b.get('withExpr') is None:
            ivs = self.intervals(cpinfo, items, wild)
            if ivs is not None:
                self.fixed_bins(cpinfo, b, ent, emit_bin, ivs, nbins, iff_b)
                return
        vals = self.const_values(cpinfo, items, wild)
        if vals is None:
            raise Unsupported(f"array bin {b['name']}: values not constant or more than --max-bins "
                              f"({self.gen.a.max_bins})")
        if b.get('withExpr') is not None:     # filter: keep values for which `with` is true
            kept = []
            for v in vals:
                c = self.with_cond(b, cpinfo, lit(v, cpinfo['w'], cpinfo['s']), K(v, cpinfo['w'], cpinfo['s']))
                if c == "1'b0": continue
                kept.append((v, None if c == "1'b1" else c))
        else:
            kept = [(v, None) for v in vals]
        if nbins is None:
            # bins b[] = {...}: one bin per value (duplicates collapse)
            seen = set()
            for v, wc in kept:
                if v in seen: continue
                seen.add(v)
                c = f"({cpinfo['v']} == {lit(v, cpinfo['w'], cpinfo['s'])})"
                if wc: c = f'({c} && {wc})'
                if iff_b: c = f'({c} && {iff_b})'
                emit_bin(f"{ident(b['name'])}__{v if v >= 0 else 'm' + str(-v)}", c, ent, [v])
            ent.setdefault('notes', []).append(f'{len(seen)} bins (one per value)')
        else:
            # bins b[N]: values distributed in order, |values|/N per bin, remainder in the last (LRM 19.5.1)
            vs = [v for v, _ in kept]
            if any(wc for _, wc in kept): raise Unsupported('bins [N] with a non-constant `with` filter')
            n = len(vs)
            if n == 0: return
            if nbins > n:
                groups = [[v] for v in vs] + [[] for _ in range(nbins - n)]
            else:
                per = n // nbins
                groups = [vs[i * per:(i + 1) * per] for i in range(nbins - 1)] + [vs[(nbins - 1) * per:]]
            for i, g in enumerate(groups):
                if not g:
                    ent.setdefault('notes', []).append(f'{b["name"]}[{i}] is empty (more bins than values)')
                    continue
                c = self.values_cond(cpinfo, g)
                if iff_b: c = f'({c} && {iff_b})'
                emit_bin(f"{ident(b['name'])}__{i}", c, ent, g)
            ent.setdefault('notes', []).append(f'{nbins} bins over {n} values')

    def intervals(self, cpinfo, items, wild):
        """constant value list as (lo, hi) intervals in source order (duplicates kept), or None"""
        lo_t, hi_t = self.tmin_max(cpinfo)
        out = []
        for it in items:
            if it[0] == 'e': return None
            if it[0] == 'v':
                if it[1].x:
                    vs = self.const_values(cpinfo, [it], wild)
                    if vs is None: return None
                    out += [(v, v) for v in vs]
                else:
                    out.append((it[1].v, it[1].v))
            else:
                lo, hi = it[1], it[2]
                if isinstance(lo, tuple) or isinstance(hi, tuple): return None
                a = lo.v if lo is not None else lo_t
                b = hi.v if hi is not None else hi_t
                if a <= b: out.append((a, b))      # [hi:lo] with hi > lo is empty
        return out

    def fixed_bins(self, cpinfo, b, ent, emit_bin, ivs, nbins, iff_b):
        """bins b[N] = {...}: the n listed values (in order) split into N bins of n/N, the last one
        taking the remainder; with more bins than values each value gets one and the rest are empty
        (LRM 19.5.1)"""
        n = sum(h - l + 1 for l, h in ivs)
        if n == 0: return
        if nbins > n:
            groups = [[(v, v)] for l, h in ivs for v in range(l, h + 1)]
            ent.setdefault('notes', []).append(f'{nbins - n} of {nbins} bins are empty (more bins than values)')
        else:
            per = n // nbins
            groups, cur, need = [], [], per
            stream = list(ivs)
            i = 0
            while i < len(stream):
                l, h = stream[i]
                if len(groups) == nbins - 1:          # the last bin takes everything left
                    cur.append((l, h)); i += 1; continue
                take = min(need, h - l + 1)
                cur.append((l, l + take - 1))
                need -= take
                if take < h - l + 1: stream[i] = (l + take, h)
                else: i += 1
                if need == 0:
                    groups.append(cur); cur, need = [], per
            if cur: groups.append(cur)
        for i, g in enumerate(groups):
            c = self.ranges_cond(cpinfo, g)
            if iff_b: c = f'({c} && {iff_b})'
            emit_bin(f"{ident(b['name'])}__{i}", c, ent, g)
        ent.setdefault('notes', []).append(f'{nbins} bins over {n} values')

    def ranges_cond(self, cpinfo, ivs):
        w, s, x = cpinfo['w'], cpinfo['s'], cpinfo['v']
        lo_t, hi_t = self.tmin_max(cpinfo)
        parts = []
        for a, b in merge_ivs(ivs):
            if a == b: parts.append(f'({x} == {lit(a, w, s)})')
            elif a == lo_t and b == hi_t: parts.append("1'b1")
            elif a == lo_t: parts.append(f'({x} <= {lit(b, w, s)})')
            elif b == hi_t: parts.append(f'({x} >= {lit(a, w, s)})')
            else: parts.append(f'({x} >= {lit(a, w, s)} && {x} <= {lit(b, w, s)})')
        return '(' + ' || '.join(parts) + ')'

    def values_cond(self, cpinfo, vals):
        """membership in a constant value list, as ranges"""
        vs = sorted(set(vals))
        runs, start = [], None
        for i, v in enumerate(vs):
            if start is None: start = v
            if i + 1 == len(vs) or vs[i + 1] != v + 1:
                runs.append((start, v)); start = None
        w, s, x = cpinfo['w'], cpinfo['s'], cpinfo['v']
        parts = [f'({x} == {lit(a, w, s)})' if a == b else f'({x} >= {lit(a, w, s)} && {x} <= {lit(b, w, s)})'
                 for a, b in runs]
        return '(' + ' || '.join(parts) + ')'

    def auto_bins(self, cp, cpinfo, emit_bin, base_ent):
        """no explicit bins: one per enum value, or 2^w values spread over auto_bin_max bins (LRM 19.5.3)"""
        ti = cpinfo['ti']
        ent = {'kind': 'bin', 'covergroup': self.cg['name'], 'owner': self.owner, 'coverpoint': cp['name'],
               'bin': '<auto>', 'bin_kind': 'Auto', 'file': cp['file'], 'line': cp['start'], 'status': 'exact'}
        w = cpinfo['w']
        if ti.get('enum'):
            for e in ti['enum']:
                k = parse_const(e['value'])
                v = kval(k, w, cpinfo['s'])
                emit_bin(f"auto_{e['name']}", f"({cpinfo['v']} == {lit(v, w, cpinfo['s'])})", ent, [v])
            ent['notes'] = [f"{len(ti['enum'])} automatic bins, one per enum value"]
        else:
            mx = self.opt(cp, 'auto_bin_max', 64)
            lo, hi = self.tmin_max(cpinfo)
            n = hi - lo + 1
            if n <= mx:
                for v in range(lo, hi + 1):
                    emit_bin(f"auto_{v if v >= 0 else 'm' + str(-v)}",
                             f"({cpinfo['v']} == {lit(v, w, cpinfo['s'])})", ent, [v])
                ent['notes'] = [f'{n} automatic bins, one per value']
            else:
                per = n // mx
                for i in range(mx):
                    a = lo + i * per
                    b = hi if i == mx - 1 else a + per - 1
                    emit_bin(f'auto_{i}', f"({cpinfo['v']} >= {lit(a, w, cpinfo['s'])} && {cpinfo['v']} <= {lit(b, w, cpinfo['s'])})",
                             ent, (a, b))
                ent['notes'] = [f'{mx} automatic bins of {per} values (last takes the remainder), auto_bin_max={mx}']
        self.entries.append(ent)

    # ---- transitions
    def trans_bin(self, cp, cpinfo, b, ent, iff, at_least):
        """transition bin -> a monitor over the sampled values (NFA in flops), one cover per sequence;
        `b[] = (a, b => c)` expands to one bin per value combination"""
        if not b.get('trans'): raise Unsupported('transition bin without parsed transitions (rerun sniper)')
        seqs = []
        for st in b['trans']:
            steps = []
            for step in st:
                vals = []
                for v in step['values']:
                    if isinstance(v, list):
                        a, c = (parse_const(x) if isinstance(x, str) else None for x in v)
                        if a is None or c is None: raise Unsupported(f'transition range not constant: {v}')
                        vals.append(('r', K(kval(a, cpinfo['w'], cpinfo['s']), cpinfo['w'], cpinfo['s']),
                                     K(kval(c, cpinfo['w'], cpinfo['s']), cpinfo['w'], cpinfo['s'])))
                    else:
                        k = parse_const(v) if isinstance(v, str) else None
                        if k is None: raise Unsupported(f'transition value not constant: {v}')
                        vals.append(('v', K(kval(k, cpinfo['w'], cpinfo['s']), cpinfo['w'], cpinfo['s'], k.x)))
                rep = step.get('repeat')
                if rep:
                    lo = parse_const(rep['from']) if isinstance(rep['from'], str) else None
                    hi = parse_const(rep['to']) if isinstance(rep['to'], str) else None
                    if lo is None or hi is None:
                        raise Unsupported(f"transition repeat bound not constant ({rep})")
                    lo, hi = lo.v, hi.v
                    if hi > self.gen.a.max_rep:
                        raise Unsupported(f'repeat count {hi} above --max-rep {self.gen.a.max_rep}')
                    steps.append((vals, rep['kind'], lo, hi))
                else:
                    steps.append((vals, '*', 1, 1))
            seqs.append(steps)
        if b['array']:
            expanded = []
            for steps in seqs:
                per_step = []
                for vals, kind, lo, hi in steps:
                    cv = self.const_values(cpinfo, vals, b.get('wildcard'))
                    if cv is None: raise Unsupported('array transition bin with too many values')
                    per_step.append([([('v', K(v, cpinfo['w'], cpinfo['s']))], kind, lo, hi, v) for v in cv])
                for combo in product(per_step):
                    expanded.append(([(c[0], c[1], c[2], c[3]) for c in combo],
                                     '_'.join(str(c[4]) if c[4] >= 0 else 'm' + str(-c[4]) for c in combo)))
            if len(expanded) > self.gen.a.max_bins:
                raise Unsupported(f'{len(expanded)} transition bins exceed --max-bins')
            targets = [([s], f"{ident(b['name'])}__{tag}") for s, tag in expanded]
        else:
            targets = [(seqs, ident(b['name']))]
        for alts, nm in targets:
            match = ' || '.join(self.seq_monitor(cpinfo, steps, f"{cp['name']}__{nm}_{i}", iff, b.get('wildcard'))
                                for i, steps in enumerate(alts))
            if b['kind'] == 'IllegalBins':
                self.illegal(f"{cp['name']}__{nm}", f'{iff} && ({match})', ent)
            elif b['kind'] == 'IgnoreBins':
                ent.setdefault('notes', []).append('ignore transition bin: matches are not removed from other '
                                                   'transition bins (approx: over-reports those bins)')
                ent['status'] = 'approx'
                return
            else:
                self.cover(f"{cp['name']}__{nm}", f'{iff} && ({match})', ent, at_least)
        ent.setdefault('notes', []).append('transition: monitor over consecutive samples (sample && iff)')

    def seq_monitor(self, cpinfo, steps, base, iff, wildcard):
        """flops tracking partial matches of a value sequence; returns the 'matches now' condition.
        Step i with set S, repetition kind/lo/hi:
          *  : C[i][k] = the last k samples were in S, the step started after step i-1 completed
          -> : G[i][k] = k occurrences, last sample in S;  N[i][k] = k occurrences, last not in S
          =  : same states; completion may end on non-occurrences (they precede the next step)"""
        mod = self.mod
        upd = mod.names(f'st_{base}')
        mod.body.append(f'  wire {upd} = {self.smp} && {iff};   // a sample is taken')
        regs, nxt_assign = [], []
        done_prev = "1'b1"          # the first step may start at any sample
        done_prev_reg = "1'b1"
        match_now = None
        for i, (vals, kind, lo, hi) in enumerate(steps):
            occ = mod.names(f'o_{base}_{i}')
            mod.body.append(f"  wire {occ} = {'(' + ' || '.join(self.item_cond(cpinfo, v, wildcard) for v in vals) + ')'};")
            comp_next = []          # this step complete after the current sample
            comp_reg = []           # ... as of the previous sample (registered)
            if kind == '*':
                C = [None] + [mod.names(f'c_{base}_{i}_{k}') for k in range(1, hi + 1)]
                for k in range(1, hi + 1):
                    regs.append(C[k])
                    nx = f'({occ} && {done_prev_reg})' if k == 1 else f'({occ} && {C[k - 1]})'
                    nn = mod.names(f'nx_{C[k]}')
                    mod.body.append(f'  wire {nn} = {nx};')
                    nxt_assign.append((C[k], nn))
                    if k >= lo:
                        comp_next.append(nn); comp_reg.append(C[k])
            else:
                G = {k: mod.names(f'g_{base}_{i}_{k}') for k in range(1, hi + 1)}
                N = {k: mod.names(f'w_{base}_{i}_{k}') for k in range(0, hi + 1)}
                for k in range(0, hi + 1):
                    # N[k]: k occurrences so far, last sample not an occurrence
                    src = [f'{G[k]}' if k >= 1 else None, N[k]] + ([done_prev_reg] if k == 0 else [])
                    src = [x for x in src if x]
                    nn = mod.names(f'nx_{N[k]}')
                    mod.body.append(f"  wire {nn} = !{occ} && ({' || '.join(src)});")
                    regs.append(N[k]); nxt_assign.append((N[k], nn))
                for k in range(1, hi + 1):
                    src = [N[k - 1]] + ([G[k - 1]] if k >= 2 else [])
                    if k == 1: src.append(done_prev_reg)
                    nn = mod.names(f'nx_{G[k]}')
                    mod.body.append(f"  wire {nn} = {occ} && ({' || '.join(src)});")
                    regs.append(G[k]); nxt_assign.append((G[k], nn))
                for k in range(max(lo, 1), hi + 1):
                    comp_next.append(f'nx_{G[k]}' if f'nx_{G[k]}' in mod.names.used else None)
                    comp_reg.append(G[k])
                comp_next = [f'nx_{G[k]}' for k in range(max(lo, 1), hi + 1)]
                if kind == '=':
                    comp_next += [f'nx_{N[k]}' for k in range(max(lo, 1), hi + 1)]
                    comp_reg += [N[k] for k in range(max(lo, 1), hi + 1)]
            if lo == 0:             # the step may be empty: the previous completion carries through
                comp_next.append(done_prev); comp_reg.append(done_prev_reg)
            dn = mod.names(f'd_{base}_{i}')
            mod.body.append(f"  wire {dn} = {' || '.join(comp_next) if comp_next else chr(49) + chr(39) + 'b0'};")
            dr = mod.names(f'dr_{base}_{i}')
            mod.body.append(f"  wire {dr} = {' || '.join(comp_reg) if comp_reg else chr(49) + chr(39) + 'b0'};")
            done_prev, done_prev_reg = dn, dr
            match_now = dn
        for r in regs:
            mod.body.append(f"  logic {r} = 1'b0;")
        mod.body.append(f'  always_ff @({self.edge} clk) begin')
        mod.body.append('    if (reset) begin')
        for r in regs: mod.body.append(f"      {r} <= 1'b0;")
        mod.body.append(f'    end else if ({upd}) begin')
        for r, nn in nxt_assign: mod.body.append(f'      {r} <= {nn};')
        mod.body.append('    end')
        mod.body.append('  end')
        return match_now

    # ---- crosses
    def cross(self, cx, cps):
        tgt = []
        for t in cx['targets']:
            if t not in cps:
                raise Unsupported(f'cross target {t} is not a modelled coverpoint (implicit coverpoints of '
                                  'variables are not supported)')
            if not cps[t]['bins']: raise Unsupported(f'cross target {t} has no value bins')
            tgt.append(cps[t])
        ent0 = lambda b: {'kind': 'cross_bin', 'covergroup': self.cg['name'], 'owner': self.owner,
                          'cross': cx['name'], 'bin': b, 'file': cx['file'], 'line': cx['start'], 'status': 'exact'}
        total = math.prod(len(c['bins']) for c in tgt)
        iffs = [c['iff'] for c in tgt if c['iff'] != "1'b1"]
        xiff = None
        if cx.get('iff'):
            xiff = self.tx.boolean(self.tx.scalar(cx['iff'])).t
        base_cond = ' && '.join(iffs + ([xiff] if xiff else [])) or "1'b1"
        tuples = list(product([list(range(len(c['bins']))) for c in tgt])) if total <= self.gen.a.max_cross * 8 else None
        ignored, illegal, user = set(), set(), []
        partial = {}            # product -> filter conditions of ignore/illegal bins that remove part of it
        at_least = self.opt(cx, 'at_least', 1)
        for b in cx['bins']:
            ent = {'kind': 'cross_bin', 'covergroup': self.cg['name'], 'owner': self.owner, 'cross': cx['name'],
                   'bin': b['name'], 'bin_kind': b['kind'], 'file': b.get('file'), 'line': b.get('start'),
                   'src': b.get('src'), 'status': 'exact'}
            if tuples is None:
                ent['status'] = 'unsupported'
                ent['notes'] = [f'cross has {total} products (> 8 x --max-cross): select expressions not evaluated']
                self.entries.append(ent); continue
            try:
                sel, filt = self.select_filtered(b.get('crossSelectExpr'), tgt, tuples, cps)
            except Unsupported as e:
                ent['status'] = 'unsupported'; ent['notes'] = [str(e)]
                self.entries.append(ent); continue
            if b['kind'] in ('IgnoreBins', 'IllegalBins') and filt is not None:
                # `with` selects value combinations, not whole products: the products stay, those
                # combinations are removed from them (LRM 19.6.1.2)
                for t in sel: partial.setdefault(t, []).append(filt)
            if b['kind'] == 'IgnoreBins':
                if filt is None: ignored |= sel
                ent['notes'] = [f'{len(sel)} products ignored' + (' where the with filter holds' if filt else '')]
            elif b['kind'] == 'IllegalBins':
                if filt is None: illegal |= sel
                if sel:
                    c = ' || '.join(self.tuple_cond(tgt, t) for t in sorted(sel))
                    if filt: c = f'({c}) && {filt}'
                    self.illegal(f"{cx['name']}__{b['name']}", f'{base_cond} && ({c})', ent)
                ent['notes'] = [f'{len(sel)} products: illegal (assertion)' + (' with filter' if filt else '')]
            else:
                user.append((b, sel, ent, filt))
            self.entries.append(ent) if b['kind'] != 'Bins' else None
        # user bins: a hit of any selected product (ignore / illegal products removed)
        covered = set()
        for b, sel, ent, filt in user:
            keep = sel - ignored - illegal
            covered |= sel
            if not keep:
                ent['notes'] = ['empty after ignore/illegal products are removed']
            else:
                c = ' || '.join(self.tuple_cond(tgt, t, partial) for t in sorted(keep))
                if filt: c = f'({c}) && {filt}'
                self.cover(f"{cx['name']}__{ident(b['name'])}", f'{base_cond} && ({c})', ent, at_least)
                ent.setdefault('notes', []).append(f'{len(keep)} products')
            self.entries.append(ent)
        # automatic cross bins: every remaining product (LRM 19.6)
        ent = ent0('<auto>')
        ent['bin_kind'] = 'Auto'
        if tuples is None:
            ent['status'] = 'unsupported'
            ent['notes'] = [f'{total} automatic cross bins exceed 8 x --max-cross ({self.gen.a.max_cross})']
        else:
            auto = [t for t in tuples if t not in covered and t not in ignored and t not in illegal]
            if len(auto) > self.gen.a.max_cross:
                ent['status'] = 'approx'
                ent['notes'] = [f'{len(auto)} automatic cross bins; first {self.gen.a.max_cross} emitted (raise --max-cross)']
                auto = auto[:self.gen.a.max_cross]
            for t in auto:
                nm = '__'.join(tgt[i]['bins'][j].name for i, j in enumerate(t))
                self.cover(f"{cx['name']}__{nm}", f'{base_cond} && {self.tuple_cond(tgt, t, partial)}', ent, at_least)
            ent.setdefault('notes', []).append(f'{len(auto)} automatic cross bins')
        self.entries.append(ent)

    def tuple_cond(self, tgt, t, partial=None):
        c = '(' + ' && '.join(tgt[i]['bins'][j].cond for i, j in enumerate(t)) + ')'
        if partial and t in partial:
            c = f"({c} && !({' || '.join(partial[t])}))"
        return c

    def select_filtered(self, e, tgt, tuples, cps):
        """(products, filter condition or None): `select with (expr)` keeps the products of the select
        and restricts them to value combinations where expr holds; coverpoint names in expr stand for
        their values"""
        if e is not None and e.get('k') == 'WithFilter':
            if e.get('matchesExpr') is not None:
                raise Unsupported('cross select `with ... matches`')
            sel = self.select(e['expr'], tgt, tuples, cps)
            tx = self.tx
            saved = dict(tx.env)
            for c in tgt: tx.env['#' + c['name']] = S(c['v'], c['w'], c['s'], None, True)
            try:
                f = tx.boolean(tx.scalar(e['filter'])).t
            finally:
                tx.env = saved
            return sel, f'({f})'
        return self.select(e, tgt, tuples, cps), None

    def select(self, e, tgt, tuples, cps):
        """set of products (tuples of bin indices) selected by a cross select expression (LRM 19.6.1)"""
        if e is None: raise Unsupported('cross bin without a select expression')
        k = e.get('k')
        names = [c['name'] for c in tgt]
        if k == 'Condition':
            target = e.get('target') or {}
            sk, sym = target.get('skind'), target.get('sym')
            if sk == 'Coverpoint':
                ci = names.index(sym) if sym in names else None
                if ci is None: raise Unsupported(f'binsof({sym}): not a cross target')
                bins_ok = set(range(len(tgt[ci]['bins'])))
            elif sk == 'CoverageBin':
                cpn = target.get('path', '').rsplit('.', 2)[-2] if target.get('path') else None
                ci = names.index(cpn) if cpn in names else None
                if ci is None: raise Unsupported(f'binsof(..{sym}): coverpoint not found')
                bins_ok = {j for j, b in enumerate(tgt[ci]['bins']) if b.name == ident(sym) or
                           b.name.startswith(ident(sym) + '__')}
            else:
                raise Unsupported(f'binsof target {sk}')
            if e.get('intersects'):
                cpinfo = tgt[ci]
                items = []
                for v in e['intersects']:
                    vk = strip_conv(v)
                    if vk.get('k') == 'ValueRange':
                        ends = []
                        for side in ('left', 'right'):
                            x = vk[side]
                            if strip_conv(x).get('k') == 'UnboundedLiteral': ends.append(None); continue
                            c = nconst(x) or nconst(strip_conv(x))
                            if c is None: raise Unsupported('intersect range not constant')
                            ends.append(kval(c, cpinfo['w'], cpinfo['s']))
                        items.append(ends)
                    else:
                        c = nconst(v)
                        if c is None: raise Unsupported('intersect value not constant')
                        v_ = kval(c, cpinfo['w'], cpinfo['s'])
                        items.append([v_, v_])
                lo_t, hi_t = self.tmin_max(cpinfo)
                items = [[lo_t if a is None else a, hi_t if b is None else b] for a, b in items]
                keep = set()
                for j in bins_ok:
                    ivs = tgt[ci]['bins'][j].values
                    if ivs is None: raise Unsupported(f'bin {tgt[ci]["bins"][j].name} has no constant value set')
                    if any(not (b < lo or a > hi) for lo, hi in ivs for a, b in items): keep.add(j)
                bins_ok = keep
            return {t for t in tuples if t[ci] in bins_ok}
        if k == 'Unary':
            return set(tuples) - self.select(e['expr'], tgt, tuples, cps)
        if k == 'Binary':
            l, r = self.select(e['left'], tgt, tuples, cps), self.select(e['right'], tgt, tuples, cps)
            return l & r if e.get('op') == 'And' else l | r
        if k == 'CrossId':
            return set(tuples)
        raise Unsupported(f'cross select {k} ({e.get("src", "")})')


def to_ivs(values):
    """bin values as (lo, hi) intervals: from a value list, one (lo, hi) range, or intervals"""
    if values is None: return None
    if isinstance(values, tuple): return [values]
    return [v if isinstance(v, tuple) else (v, v) for v in values]


def merge_ivs(ivs):
    out = []
    for a, b in sorted(ivs):
        if out and a <= out[-1][1] + 1: out[-1] = (out[-1][0], max(out[-1][1], b))
        else: out.append((a, b))
    return out


def to_signed(v, cpinfo):
    w = cpinfo['w']
    if cpinfo['s'] and v >> (w - 1) & 1: return v - (1 << w)
    return v


def product(lists):
    if not lists: yield (); return
    for x in lists[0]:
        for rest in product(lists[1:]):
            yield (x,) + rest


def find_iter_paths(n):
    if isinstance(n, dict):
        if n.get('k') == 'NamedValue' and n.get('symbol', {}).get('skind') == 'Iterator':
            yield n['symbol'].get('path')
        for v in n.values(): yield from find_iter_paths(v)
    elif isinstance(n, list):
        for v in n: yield from find_iter_paths(v)


def parse_opt_strings(opts):
    out = {}
    for o in opts:
        m = re.match(r'(?:type_)?option\.(\w+)\s*=\s*([^;]+);', o)
        if m:
            k = parse_const(m.group(2).strip())
            if k is not None: out[m.group(1)] = k.v
    return out


def parse_event(ev):
    """'@(posedge clk iff en)' -> ('posedge', 'clk', 'en'); None for no event / not a clock edge"""
    if not ev: return None
    m = re.fullmatch(r'@\s*\(\s*(posedge|negedge)?\s*([\w.$\[\]]+)\s*(?:iff\s*\((.*)\))?\s*\)', ev.strip())
    if not m: return None
    return (m.group(1) or 'posedge', m.group(2), m.group(3))


class CgResolver:
    """names in coverpoint expressions become input ports: class fields, sample()/constructor
    arguments, module signals, hierarchical references"""

    def __init__(self, gen, mod, cg):
        self.gen, self.mod, self.cg = gen, mod, cg
        self.inputs = {}      # port -> (w, signed, comment)
        self.origin = {}      # port -> source expression (for binding)
        self.src = {}         # port -> (class, [field, ...]): the value is that field of a class object
        owner = cg.get('owner')
        self.owner_cls = gen.cls(owner, cg.get('path')) if owner else None

    def port(self, name, n, origin, src=None):
        nm = ident(name)
        if n.get('w') is None:
            raise Unsupported(f'{origin}: not an integral value')
        if nm not in self.inputs:
            self.inputs[nm] = (n['w'], bool(n.get('signed')), origin)
            self.origin[nm] = origin
            if src and src[0] is not None: self.src[nm] = src
            self.mod.names.used.add(nm)
        return S(nm, n['w'], bool(n.get('signed')), None, not n.get('signed'))

    def resolve(self, n, tx):
        sym = n['symbol']
        sk, nm = sym.get('skind'), sym['sym']
        if n['k'] == 'HierarchicalValue':
            src = n.get('src') or sym.get('path', nm)
            return self.port(src.replace('.', '__'), n, src)
        if n.get('w') is None:      # a handle or aggregate: members become ports
            o = Obj(None, nm + '__', 'state', nm)
            o.wit = (self.gen.cls(n.get('ty', ''), self.cg.get('path')), [])     # members: fields of that class
            return ORef(o)
        if sk == 'ClassProperty' and self.owner_cls is not None:
            return self.port(nm, n, nm, (self.owner_cls, [nm]))
        if sk in ('ClassProperty', 'FormalArgument', 'Variable', 'Net', 'Parameter'):
            return self.port(nm, n, nm)
        raise Unsupported(f'reference to {sk} {nm} in a coverpoint')

    def member(self, base, n, tx):
        nm = base.obj.prefix + n['member']['sym']
        w = getattr(base.obj, 'wit', None)
        src = (w[0], w[1] + [n['member']['sym']]) if w else None
        if n.get('w') is None:
            o = Obj(None, nm + '__', 'state', nm)
            o.wit = src
            return ORef(o)
        return self.port(nm, n, nm.replace('__', '.'), src)


# ======================================================================== output
def write_outputs(gen, out):
    os.makedirs(out, exist_ok=True)
    for f in ('prim_assert.sv', 'prim_assert_yosys_macros.svh'):
        src = os.path.join(gen.a.prim_assert_dir, f)
        if os.path.exists(src): shutil.copy(src, os.path.join(out, f))
        else: sys.exit(f'prim_assert files not found in {gen.a.prim_assert_dir} (--prim-assert-dir)')
    hdr = (f"// Generated by fvgen from {os.path.basename(gen.a.json)} -- do not edit.\n"
           "// Read with: read_verilog -sv -formal -DFORMAL (prim_assert.sv / prim_assert_yosys_macros.svh on the include path)\n\n")
    with open(os.path.join(out, 'fv_constraints.sv'), 'w') as f:
        f.write(hdr)
        for cm, rm in gen.mods_c:
            f.write(cm.emit() + '\n')
            if rm is not None: f.write(rm.emit() + '\n')
    with open(os.path.join(out, 'fv_covers.sv'), 'w') as f:
        f.write(hdr)
        for m in gen.mods_cg: f.write(m.emit() + '\n')
    with open(os.path.join(out, 'fv_bind.sv'), 'w') as f:
        f.write(hdr + '// Covergroups declared in modules/interfaces: instantiate inside that module (or bind it there);\n'
                '// ports are named after the signals the coverpoints read.\n\n')
        for m in gen.mods_cg:
            if '::' in m.origin.split(' of ')[-1]: continue          # class covergroup
            scope = m.origin.split(' of ')[-1]
            conns = [f'.clk({m.clock_src or "clk"})', ".reset(1'b0)", ".sample(1'b1)" if m.clock_src else '.sample(/* sample() strobe */)']
            conns += [f'.{p}({m.res.origin[p]})' for p in m.res.inputs]
            f.write(f'// in {scope}\n// {m.name} u_{m.name} (\n//   ' + ',\n//   '.join(conns) + '\n// );\n\n')
    smoke(gen, out)
    rep = summarize(gen)
    with open(os.path.join(out, 'report.json'), 'w') as f:
        json.dump(rep, f, indent=1, default=lambda o: sorted(o) if isinstance(o, set) else str(o))
    with open(os.path.join(out, 'report.md'), 'w') as f:
        f.write(report_md(rep))
    return rep


def cover_depth(m):
    """bound for a covergroup's formal runs: transition monitors and at_least counters need steps"""
    depth = 2 + max([len(re.findall(r'\bc_|\bg_|\bn_', l)) for l in m.body] + [0])
    return min(max(4, depth), 24)


def chunks(m, size):
    """cover index ranges of a covergroup module, at most `size` covers each"""
    n = len(m.hit_order)
    if n == 0: return [(0, 0)]
    return [(lo, min(lo + size, n)) for lo in range(0, n, size)]


def smoke(gen, out):
    """one formal top per generated module (large covergroups: per chunk of covers), every input
    free (anyseq): cover mode shows whether the constraints are satisfiable and whether every bin
    can be hit"""
    sd = os.path.join(out, 'smoke')
    os.makedirs(sd, exist_ok=True)
    gen.smokes = []
    for cm, rm in gen.mods_c:
        top = ident('smoke_' + cm.name)
        lines = [f'module {top} (input logic clk);']
        conns = ['.clk(clk)', ".reset(1'b0)", ".en(1'b1)"]
        for nm in cm.port_order:
            if nm in ('clk', 'reset', 'en'): continue
            lines.append(f'  (* anyseq *) {cm.ports[nm][1]};')
            conns.append(f'.{nm}({nm})')
        lines.append(f'  {cm.name} u_dut (' + ', '.join(conns) + ');')
        lines.append("  always_ff @(posedge clk) sat: cover (1'b1);   // the constraints have a solution")
        lines.append('endmodule')
        gen.smokes.append(Job(sd, top, '\n'.join(lines), [cm], 2, ' -DFV_LIVENESS_COVERS', cm))
    for m in gen.mods_cg:
        for lo, hi in chunks(m, gen.a.cover_chunk):
            top = ident('smoke_' + m.name + (f'__{lo}' if len(m.hit_order) > gen.a.cover_chunk else ''))
            lines = [f'module {top} (input logic clk);']
            conns = ['.clk(clk)', ".reset(1'b0)"]
            for nm in m.port_order:
                if nm in ('clk', 'reset'): continue
                lines.append(f'  (* anyseq *) {m.ports[nm][1]};')
                conns.append(f'.{nm}({nm})')
            sel = f' #(.FV_COVER_LO({lo}), .FV_COVER_HI({hi}))' if m.hit_order else ''
            lines.append(f'  {m.name}{sel} u_dut (' + ', '.join(conns) + ');')
            lines.append('endmodule')
            gen.smokes.append(Job(sd, top, '\n'.join(lines), [m], cover_depth(m), ' -DFV_NO_ILLEGAL_BINS', m,
                                  m.hit_order[lo:hi]))
    for j in gen.smokes: j.write(gen)


class Job:
    """one SymbiYosys cover run: a top, the generated modules it needs (written next to it, so a job
    does not parse every module), and the covers it is responsible for"""

    def __init__(self, d, top, src, mods, depth, defs, m, names=None, traces=False):
        self.d, self.top, self.src, self.mods, self.depth, self.defs = d, top, src, mods, depth, defs
        self.m, self.names, self.traces = m, names, traces

    def write(self, gen):
        with open(os.path.join(self.d, self.top + '.sv'), 'w') as f: f.write(self.src + '\n')
        with open(os.path.join(self.d, self.top + '_lib.sv'), 'w') as f:
            f.write(''.join(x.emit() + '\n' for x in self.mods))
        vcd = '' if (gen.a.keep or self.traces) else 'vcd off\n'
        sby = (f'[options]\nmode cover\ndepth {self.depth}\n{vcd}\n[engines]\nsmtbmc {gen.a.solver}\n\n[script]\n'
               f'read_verilog -sv -formal -DFORMAL{self.defs} {self.top}_lib.sv {self.top}.sv\nprep -top {self.top}\n\n'
               f'[files]\n{self.top}_lib.sv\n{self.top}.sv\n../prim_assert.sv\n../prim_assert_yosys_macros.svh\n')
        with open(os.path.join(self.d, self.top + '.sby'), 'w') as f: f.write(sby)


def guard_text(cond):
    """'(!(G) || E)' -> G"""
    if not cond.startswith('(!('): return None
    depth = 0
    for i, ch in enumerate(cond[2:], 2):
        if ch == '(': depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0: return cond[3:i]
    return None


def qualify(text, inst):
    """prefix identifiers of the module's ports/wires with the instance (hierarchical refs)"""
    return re.sub(r"(?<![\w'$.])([A-Za-z_]\w*)\b(?!\s*\()", lambda m: m.group(1) if m.group(1) in SV_KEYWORDS
                  else f'{inst}.{m.group(1)}', re.sub(r"\d+'s?[hdbo][0-9a-fA-F_xz?]+", lambda m: m.group(0), text))


def renamed(decl, old, new):
    """'logic signed [7:0] a' -> 'logic signed [7:0] r_a'"""
    return re.sub(r'\b%s$' % re.escape(old), new, decl)


def decl_width(decl):
    m = re.search(r'\[(\d+):0\]', decl)
    return int(m.group(1)) + 1 if m else 1


def witness(gen, out):
    """reach every bin of a covergroup from the randomize() model of the class it samples
    (plan_witness): the covergroup is sampled the cycle after each randomize(), with that call's
    field values. Each reached bin's trace gives the field values of every randomize() up to the hit."""
    wd = os.path.join(out, 'witness')
    os.makedirs(wd, exist_ok=True)
    jobs = []
    for m in gen.mods_cg:
        w = getattr(m, 'wit', None)
        if not w or not m.hit_order: continue
        rm, cm = w['rm'], w['cm']
        for lo, hi in chunks(m, gen.a.cover_chunk):
            top = ident('wit_' + m.name + (f'__{lo}' if len(m.hit_order) > gen.a.cover_chunk else ''))
            lines = [f'module {top} (input logic clk);',
                     '  (* anyseq *) logic randomize;     // randomize() is called this cycle',
                     "  logic sample = 1'b0;               // the covergroup samples the new values next cycle",
                     '  always_ff @(posedge clk) sample <= randomize;']
            rconns = ['.clk(clk)', ".reset(1'b0)", '.randomize(randomize)']
            for nm in rm.port_order:
                if nm in ('clk', 'reset', 'randomize'): continue
                dirn, decl, _ = rm.ports[nm]
                if dirn == 'output':
                    lines.append(f'  {renamed(decl, nm, "r_" + nm)};')
                    rconns.append(f'.{nm}(r_{nm})')
                else:   # testbench state: any value at the call, held until the covergroup samples it
                    lines += [f'  (* anyseq *) {renamed(decl, nm, "s_" + nm)};', f'  {renamed(decl, nm, "q_" + nm)};',
                              f"  initial q_{nm} = '0;", f'  always_ff @(posedge clk) if (randomize) q_{nm} <= s_{nm};']
                    rconns.append(f'.{nm}(s_{nm})')
            lines.append(f'  {rm.name} u_r (' + ', '.join(rconns) + ');')
            conns = ['.clk(clk)', ".reset(1'b0)", '.sample(sample)']
            for port in m.port_order:
                if port in ('clk', 'reset', 'sample'): continue
                kind, leaf, _ = w['ports'][port]
                if kind == 'pin': conns.append(f'.{port}(r_{leaf})')
                elif kind == 'state': conns.append(f'.{port}(q_{leaf})')
                else:
                    lines.append(f'  (* anyseq *) {renamed(m.ports[port][1], port, "f_" + port)};')
                    conns.append(f'.{port}(f_{port})')
            lines.append(f'  {m.name} #(.FV_COVER_LO({lo}), .FV_COVER_HI({hi})) u_cg (' + ', '.join(conns) + ');')
            lines.append('endmodule')
            jobs.append(Job(wd, top, '\n'.join(lines), [cm, rm, m], min(cover_depth(m) + 2, 30),
                            ' -DFV_NO_ILLEGAL_BINS', m, m.hit_order[lo:hi], traces=True))
    for j in jobs: j.write(gen)
    res = run_jobs(gen, jobs, gen.a.jobs, on_done=parse_witness)
    gen.witness_results = {}
    for job, status, rch, unr, err, extra in res:
        r = gen.witness_results.setdefault(job.m.name, {'reached': {}, 'unreached': set(), 'problems': []})
        r['reached'].update(extra or {})
        r['unreached'] |= unr
        if status in ('ERROR', 'TIMEOUT'): r['problems'].append(f'{job.top}: {status}: {err}')
    return write_replay(gen, out)


def read_vcd(path):
    """{step: {top-level signal: int}} of a yosys-smtbmc trace (one step per 10 time units)"""
    ids, depth, steps, cur, t = {}, 0, {}, {}, None
    with open(path) as f:
        for line in f:
            tok = line.split()
            if not tok: continue
            if tok[0] == '$scope': depth += 1
            elif tok[0] == '$upscope': depth -= 1
            elif tok[0] == '$var' and depth == 1: ids.setdefault(tok[3], []).append(tok[4])
            elif tok[0].startswith('#'):
                if t is not None and t % 10 == 0: steps[t // 10] = dict(cur)
                t = int(tok[0][1:])
            elif tok[0][0] in 'bB' and len(tok) == 2:
                for nm in ids.get(tok[1], []):
                    try: cur[nm] = int(tok[0][1:], 2)
                    except ValueError: pass
            elif tok[0][0] in '01xz' and len(tok[0]) > 1:
                for nm in ids.get(tok[0][1:], []):
                    if tok[0][0] in '01': cur[nm] = int(tok[0][0])
    if t is not None and t % 10 == 0: steps[t // 10] = dict(cur)
    return steps


def parse_witness(job, jobdir, summary):
    """cover -> {'step', 'samples': [{port: value}]}: the covergroup's inputs at every sample
    (= every randomize() call) before the cover fired"""
    hits, trace = {}, None
    for body in summary:
        mm = re.match(r'\s*cover trace: (\S+)', body)
        if mm:
            # the PASS/FAIL file names traces relative to the job directory, the log relative to the
            # .sby directory, sometimes as the .yw witness next to the .vcd
            t = re.sub(r'\.yw$', '.vcd', mm.group(1))
            trace = next((x for x in (os.path.join(jobdir, t), os.path.join(job.d, t)) if os.path.exists(x)), None)
            continue
        mm = re.match(r'\s+reached cover statement (\S+) at .* step (\d+)', body)
        if mm and trace: hits[mm.group(1).split('.')[-1]] = (trace, int(mm.group(2)))
    w = job.m.wit
    cache, out = {}, {}
    for cover, (tr, step) in hits.items():
        if job.names is not None and cover not in job.names: continue
        if tr not in cache: cache[tr] = read_vcd(tr)
        vals = cache[tr]
        samples = []
        for st in sorted(vals):
            if st >= step or not vals[st].get('sample'): continue
            row = {}
            for port, (kind, leaf, _) in w['ports'].items():
                sig = {'pin': 'r_', 'state': 'q_'}.get(kind, 'f_') + (leaf if kind in ('pin', 'state') else port)
                if sig in vals[st]: row[port] = vals[st][sig]
            samples.append(row)
        out[cover] = {'step': step, 'samples': samples}
    return out


def cover_deps(m):
    """cover -> covergroup input ports its hit depends on (through cp_ wires, transition monitors
    and at_least counters)"""
    deps = {}
    def idents(t): return set(re.findall(r"(?<!['\w])([A-Za-z_]\w*)", t))
    block, targets = None, None
    for line in m.temps + m.body:
        mm = re.match(r'\s*wire\b[^=]*?\b(\w+)\s*=\s*(.*);', line)
        if mm: deps.setdefault(mm.group(1), set()).update(idents(mm.group(2))); continue
        if re.match(r'\s*always_ff\b', line) and line.rstrip().endswith('begin'):
            block, targets = set(), set(); continue
        if block is not None:
            block |= idents(line)
            targets |= set(re.findall(r'(\w+)\s*<=', line))
            if line.strip() == 'end' and not line.startswith('    '):
                for x in targets: deps.setdefault(x, set()).update(block)
                block = None
            continue
        mm = re.match(r'\s*always_ff @\(\w+ clk\) if \((.*)\) (\w+) <= (.*);', line)
        if mm: deps.setdefault(mm.group(2), set()).update(idents(mm.group(1)) | idents(mm.group(3)))
    ports = set(m.port_order) - {'clk', 'reset', 'sample'}
    out = {}
    for cn, w in m.hit_wires.items():
        seen, todo = set(), list(idents(w))
        while todo:
            x = todo.pop()
            if x in seen: continue
            seen.add(x)
            todo += list(deps.get(x, ()))
        out[cn] = seen & ports
    return out


def write_replay(gen, out):
    """fv_replay.sv: per randomized class, `<class>__fv_replay extends <class>`. With
    +FV_HIT=<covergroup module>.<cover> its first randomize() calls take the witness values of the
    fields that bin depends on (other fields stay random); later calls are unconstrained."""
    by_cls, rows = {}, []
    for m in gen.mods_cg:
        w = getattr(m, 'wit', None)
        r = getattr(gen, 'witness_results', {}).get(m.name)
        if not w or r is None: continue
        deps = cover_deps(m)
        for cn in m.hit_order:
            key = f'{m.name}.{cn}'
            row = {'key': key, 'covergroup': m.name, 'cover': cn, 'class': w['cls']['path']}
            if cn not in r['reached']:
                row['status'] = 'unreached' if cn in r['unreached'] else 'unknown'
                rows.append(row); continue
            hit = r['reached'][cn]
            dp = deps.get(cn, set())
            kinds = {p: w['ports'][p] for p in dp}
            pins = sorted(p for p, k in kinds.items() if k[0] == 'pin')
            others = sorted(p for p, k in kinds.items() if k[0] != 'pin')
            steps = [[(w['ports'][p][1], w['ports'][p][2], s[p], decl_width(m.ports[p][1])) for p in pins if p in s]
                     for s in hit['samples']]
            row.update({'status': 'replay' if not others else 'partial', 'randomize_calls': len(steps),
                        'steps': [{path: val for _, path, val, _ in st} for st in steps],
                        'needs': {p: {'kind': kinds[p][0], 'why': kinds[p][2],
                                      'values': [s.get(p) for s in hit['samples']]} for p in others}})
            rows.append(row)
            if pins: by_cls.setdefault(w['cls']['path'], (w['cls'], []))[1].append((key, steps))
    text = replay_sv(gen, by_cls)
    with open(os.path.join(out, 'fv_replay.sv'), 'w') as f: f.write(text)
    with open(os.path.join(out, 'replay.json'), 'w') as f: json.dump(rows, f, indent=1)
    gen.replay_rows = rows
    return rows


def replay_sv(gen, by_cls):
    hdr = (f'// Generated by fvgen from {os.path.basename(gen.a.json)} -- do not edit.\n'
           '// Override classes that replay formal witnesses: compile after the testbench packages, then run\n'
           '// any test (any seed) with the class overridden and +FV_HIT=<covergroup module>.<cover>\n'
           '// (replay.json / report.md list the keys). The first randomize() calls of the overriding class\n'
           '// take the witness values of the fields the bin depends on; the other fields, and later calls,\n'
           '// stay random under the class\'s own constraints.\n')
    if not by_cls: return hdr + '// (no covergroup samples fields of a randomized class)\n'
    uvm = any(any(str(x).startswith('uvm_') for x in c.get('lineage', [])) for c, _ in by_cls.values())
    pkgs = sorted({p.rsplit('::', 1)[0] for p in by_cls if '::' in p})
    body = [hdr, '`ifndef FV_REPLAY_SV', '`define FV_REPLAY_SV']
    if uvm: body.append('`include "uvm_macros.svh"')
    body.append('package fv_replay_pkg;')
    if uvm: body.append('  import uvm_pkg::*;')
    for path, (c, covers) in sorted(by_cls.items()):
        body.append(replay_class(gen, c, covers))
    body += ['endpackage', '`endif', '']
    return '\n'.join(body)


def replay_class(gen, c, covers):
    name = ident(c['name'] + '__fv_replay')
    base = c['path']
    params = c.get('parameters') or []
    if params:
        base += ' #(' + ', '.join(f".{p['name']}({p.get('value') or p.get('type_param')})" for p in params) + ')'
    lineage = [str(x) for x in c.get('lineage', [])]
    fields = {}
    for key, steps in covers:
        for st in steps:
            for leaf, path, val, w in st: fields[leaf] = (path, w)
    L = [f"  // {c['name']} ({c['file']}:{c['start']}): {len(covers)} bins",
         f'  class {name} extends {base};']
    if 'uvm_component' in lineage:
        L += [f'    `uvm_component_utils({name})',
              f'    function new(string name = "{name}", uvm_component parent = null); super.new(name, parent); endfunction']
    elif any(x.startswith('uvm_') for x in lineage):
        L += [f'    `uvm_object_utils({name})',
              f'    function new(string name = "{name}"); super.new(name); endfunction']
    else:
        ctor = next((s for s in gen.d['subroutines'] if s.get('path') == c['path'] + '::new'), None)
        args = (ctor or {}).get('args') or []
        formals = ', '.join(f"{a.get('type', '')} {a['name']}" + (f" = {a['default']}" if a.get('default') else '')
                            for a in args)
        L.append(f"    function new({formals}); super.new({', '.join(a['name'] for a in args)}); endfunction")
    L += ['    static int fv_id = -2;      // bin selected by +FV_HIT (-1: none)',
          '    static int fv_step = 0;     // randomize() calls made so far']
    for leaf, (path, w) in sorted(fields.items()):
        L.append(f'    bit fv_en_{leaf};  logic [{w - 1}:0] fv_v_{leaf};  // {path}')
    L.append('    constraint fv_replay_c {')
    for leaf, (path, w) in sorted(fields.items()):
        L.append(f'      fv_en_{leaf} -> {path} == fv_v_{leaf};')
    L.append('    }')
    L += ['    static function int fv_id_of(string key);', '      case (key)']
    for i, (key, _) in enumerate(covers):
        L.append(f'        "{key}": return {i};')
    L += ['        default: return -1;', '      endcase', '    endfunction']
    L += ['    function void fv_load();', '      string key;',
          '      if (fv_id == -2) begin',
          '        fv_id = $value$plusargs("FV_HIT=%s", key) ? fv_id_of(key) : -1;',
          f'        if (fv_id == -1 && key != "") $display("fv_replay: +FV_HIT=%s is not a bin of {c["name"]}", key);',
          '      end']
    for leaf in sorted(fields): L.append(f'      fv_en_{leaf} = 0;')
    L += ['      case (fv_id)']
    for i, (key, steps) in enumerate(covers):
        L.append(f'        {i}: case (fv_step)  // {key}')
        for k, st in enumerate(steps):
            sets = ' '.join(f"fv_en_{leaf} = 1; fv_v_{leaf} = {w}'h{val:x};" for leaf, _, val, w in st)
            L.append(f'          {k}: begin {sets} end')
        L.append('          default: ;')
        L.append('        endcase')
    L += ['        default: ;', '      endcase', '    endfunction']
    L += ['    function void pre_randomize(); super.pre_randomize(); fv_load(); endfunction',
          '    function void post_randomize(); super.post_randomize(); if (fv_id >= 0) fv_step++; endfunction',
          '  endclass', '']
    return '\n'.join(L)


def run_jobs(gen, jobs, n_jobs=8, on_done=None):
    """run the sby jobs; (job, status, reached, unreached, err, extra) per job. on_done(job, jobdir,
    summary lines) runs before the job directory is removed and its result becomes `extra`."""

    def one(job):
        sd, top = job.d, job.top
        # own process group, so a timeout also kills the solver processes sby started
        p = subprocess.Popen(['sby', '-f', top + '.sby'], cwd=sd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, start_new_session=True)
        try:
            log = p.communicate(timeout=gen.a.timeout)[0]
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            log = p.communicate()[0] + '\nDONE (TIMEOUT)'
        # the job directory's PASS/FAIL file has the complete summary (the log truncates it)
        full = []
        for st in ('PASS', 'FAIL', 'UNKNOWN', 'ERROR', 'TIMEOUT'):
            fp = os.path.join(sd, top, st)
            if os.path.exists(fp): full = ['summary:' + l for l in open(fp).read().splitlines()]
        summary = [l.split('summary:', 1)[1] for l in (full or log.splitlines()) if 'summary:' in l]
        reached, unreached, in_unr = set(), set(), False
        for body in summary:
            mm = re.match(r'\s+reached cover statement (\S+) at', body)
            if mm: reached.add(mm.group(1).split('.')[-1]); in_unr = False; continue
            if 'unreached cover statements:' in body: in_unr = True; continue
            # listed indented under the header (two spaces in the PASS/FAIL file, three in the log)
            mm = re.match(r'\s{2,}(\S+) at ', body)
            if in_unr and mm: unreached.add(mm.group(1).split('.')[-1]); continue
            in_unr = in_unr and body.startswith('  ')
        if job.names is not None:          # covers outside the job's range are disabled, not unreachable
            keep = set(job.names)
            reached &= keep
            unreached = (unreached & keep) | (keep - reached if 'DONE (PASS' in log else set())
        status = next((st for st in ('PASS', 'FAIL', 'TIMEOUT') if f'DONE ({st}' in log), 'ERROR')
        err = None
        if status == 'ERROR':
            err = '\n'.join(l for l in log.splitlines() if 'ERROR' in l or 'rror' in l)[:800]
        elif status == 'TIMEOUT':
            err = f'sby did not finish in {gen.a.timeout}s'
        extra = None
        if on_done is not None:
            try: extra = on_done(job, os.path.join(sd, top), summary)
            except Exception as e: err = (err or '') + f' (trace parsing: {e})'
        if not gen.a.keep: shutil.rmtree(os.path.join(sd, top), ignore_errors=True)
        return job, status, reached, unreached, err, extra

    with ThreadPoolExecutor(n_jobs) as ex:
        return list(ex.map(one, jobs))


def summarize(gen):
    cons = [e for e in gen.report if e.get('kind') == 'constraint']
    bins = [e for e in gen.report if e.get('kind') in ('bin', 'cross_bin', 'coverpoint', 'cross')]
    def cnt(lst):
        c = {}
        for e in lst: c[e.get('status', 'exact')] = c.get(e.get('status', 'exact'), 0) + 1
        return c
    return {'source': gen.a.json,
            'atom_order': {cm.name: [{'wire': a['wire'], 'block': a['entry'].get('block'), 'item': a['entry'].get('item'),
                                      'class': a['entry'].get('class'), 'guard': a.get('guard'), 'kind': a['kind'],
                                      'soft': a['soft']}
                                     for a in cm.atom_order] for cm, _ in gen.mods_c},
            'hit_order': {m.name: m.hit_order for m in gen.mods_cg},
            'modules': {'constraints': [cm.name for cm, _ in gen.mods_c],
                        'randomizers': [rm.name for _, rm in gen.mods_c if rm is not None],
                        'covergroups': [m.name for m in gen.mods_cg]},
            'counts': {'constraint_items': cnt(cons), 'bins': cnt(bins),
                       'assumes': sum(len(e.get('assumes', [])) for e in cons),
                       'covers': sum(len(e.get('covers', [])) for e in bins),
                       'asserts': sum(len(e.get('asserts', [])) for e in bins)},
            'entries': gen.report, 'smoke': getattr(gen, 'smoke_results', None),
            'replay': getattr(gen, 'replay_rows', None)}


def report_md(rep):
    c = rep['counts']
    out = [f"# fvgen report: {os.path.basename(rep['source'])}\n",
           f"- constraint modules: {len(rep['modules']['constraints'])}, covergroup modules: {len(rep['modules']['covergroups'])}",
           f"- constraint items: {c['constraint_items']} -> {c['assumes']} assumes",
           f"- bins: {c['bins']} -> {c['covers']} covers, {c['asserts']} illegal-bin asserts\n",
           'Status: **exact** = same legal set / same bin as simulation; **approx** = differs, direction in the note '
           '(over = formal allows more than simulation: safe for proofs, may give spurious failures; under = formal '
           'allows less: can hide bugs); **unsupported** = not generated (constraint omitted = over).\n']
    if rep.get('smoke'):
        s = rep['smoke']
        out.append('## Smoke runs (free inputs, cover mode)\n')
        out.append(f"- modules: {s['modules']}, sby errors: {s['errors']}, timeouts: {s.get('timeouts', 0)}, "
                   f"unsatisfiable constraint sets: {s['unsat']}")
        out.append(f"- covers reached: {s['reached']}/{s['covers']}\n")
        for line in s.get('problems', [])[:200]: out.append(f'- {line}')
        out.append('')
    if rep.get('replay') is not None:
        rows = rep['replay']
        out.append('## Hitting a bin in simulation (witness replay)\n')
        out.append('Each bin below was reached formally from the `randomize()` model of the class its covergroup '
                   'samples (sampled once per call). To hit it in simulation with any seed: compile `fv_replay.sv` '
                   'after the testbench packages and run with the class overridden by `fv_replay_pkg::<class>__fv_replay` '
                   '(UVM: `+uvm_set_type_override=<class>,<class>__fv_replay`; parameterized classes: '
                   '`<class>#(..)::type_id::set_type_override(fv_replay_pkg::<class>__fv_replay::get_type())`) plus '
                   '`+FV_HIT=<key>`. **replay**: the bin depends only on fields the override pins. **partial**: it also '
                   'depends on values the override cannot set (listed: testbench state, monitor data). **unreached**: no '
                   'sequence of randomize() calls within the bound reaches it under the constraints.\n')
        cnt = {}
        for r in rows: cnt[r['status']] = cnt.get(r['status'], 0) + 1
        out.append('- ' + ', '.join(f'{k}: {v}' for k, v in sorted(cnt.items())) + '\n')
        for r in rows[:3000]:
            if r['status'] in ('replay', 'partial'):
                st = ' ; '.join('{' + ', '.join(f'{k}={v}' for k, v in x.items()) + '}' for x in r['steps'])
                out.append(f"- **{r['status']}** `+FV_HIT={r['key']}` ({r['class']}, {r['randomize_calls']} randomize() "
                           f"calls): {st}")
                for p, n in r.get('needs', {}).items():
                    out.append(f"  - also needs `{p}` = {n['values']} ({n['why']})")
            else:
                out.append(f"- **{r['status']}** `{r['key']}` ({r['class']})")
        out.append('')
    def sect(title, kinds):
        rows = [e for e in rep['entries'] if e.get('kind') in kinds and e.get('status', 'exact') != 'exact']
        if not rows: return
        out.append(f'## {title}: not exact ({len(rows)})\n')
        for e in rows[:2000]:
            where = f"{e.get('file')}:{e.get('line')}"
            what = e.get('class') or e.get('covergroup') or e.get('module')
            sub = e.get('block') or e.get('coverpoint') or e.get('cross') or ''
            out.append(f"- **{e.get('status')}** `{what}` {sub} {e.get('bin', '')} ({where}) `{(e.get('src') or '')[:120]}`")
            for n in e.get('notes', []): out.append(f'  - {n}')
        out.append('')
    sect('Constraints', ('constraint', 'class', 'site', 'model'))
    sect('Coverage', ('bin', 'cross_bin', 'coverpoint', 'cross'))
    # every item and bin, with what it became
    out.append('## Mapping\n')
    out.append('| source | item | status | generated |')
    out.append('|---|---|---|---|')
    n = 0
    for e in rep['entries']:
        if e.get('kind') not in ('constraint', 'bin', 'cross_bin'): continue
        n += 1
        if n > 20000: out.append('| ... | truncated | | |'); break
        where = f"{e.get('file')}:{e.get('line')}" if e.get('line') else (e.get('file') or '')
        if e['kind'] == 'constraint':
            item = f"{e.get('class') or ''}::{e.get('block')} `{(e.get('src') or '').replace('|', '\\|')[:90]}`"
            if e.get('site'): item += f" (at {e['site']})"
            gen = ', '.join(e.get('assumes', [])[:6]) + (' ...' if len(e.get('assumes', [])) > 6 else '')
        else:
            item = f"{e.get('covergroup')}.{e.get('coverpoint') or e.get('cross')}.{e.get('bin')}"
            gen = ', '.join((e.get('covers', []) + e.get('asserts', []))[:6]) + \
                (' ...' if len(e.get('covers', []) + e.get('asserts', [])) > 6 else '')
        out.append(f"| {where} | {item} | {e.get('status', 'exact')} | {gen or '-'} |")
    return '\n'.join(out) + '\n'


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('json', help='sniper output')
    ap.add_argument('-o', '--out', default='fv_out')
    ap.add_argument('--classes', help='regex: only classes/covergroup owners matching it')
    ap.add_argument('--inherited', action='store_true', help='also emit classes that only inherit constraints')
    ap.add_argument('--max-array', type=int, default=8, help='elements modelled for a dynamic array/queue')
    ap.add_argument('--max-bins', type=int, default=256, help='values expanded for one array bin')
    ap.add_argument('--max-cross', type=int, default=4096, help='automatic cross bins emitted per cross')
    ap.add_argument('--max-rep', type=int, default=16, help='largest transition repeat count modelled')
    ap.add_argument('--prim-assert-dir', default=os.path.expanduser(
        '~/work/sniper_corpus/qs-ques/tutorials/formal_verification'), help='dir with prim_assert.sv and the yosys macros')
    ap.add_argument('--solver', default='yices', help='smtbmc solver for the smoke runs')
    ap.add_argument('--run', action='store_true', help='run the smoke .sby jobs and add the results to the report')
    ap.add_argument('--expose', action='store_true', help='test hook: atoms / bin hits as output ports')
    ap.add_argument('-j', '--jobs', type=int, default=8)
    ap.add_argument('--timeout', type=int, default=600)
    ap.add_argument('--sat-timeout', type=int, default=20, help='seconds per soft-constraint resolution query')
    ap.add_argument('--cover-chunk', type=int, default=200, help='covers per formal job (large covergroups are split)')
    ap.add_argument('--witness', action='store_true', help='reach every bin from the randomize() model of the class '
                    'whose fields the covergroup samples, and write fv_replay.sv: override classes that make a '
                    'simulation hit a chosen bin with any seed')
    ap.add_argument('--keep', action='store_true', help='keep the smoke sby job directories and their traces')
    a = ap.parse_args()
    d = json.load(open(a.json))
    gen = Gen(d, a)
    gen.run()
    rep = write_outputs(gen, a.out)
    if a.run:
        res = run_jobs(gen, gen.smokes, a.jobs)
        probs, reached, total, errs, unsat, tmo = [], 0, 0, 0, 0, 0
        for job, status, rch, unr, err, _ in res:
            m = job.m
            where = m.name + (f' [covers {job.names[0]}..{job.names[-1]}]' if job.names and len(job.names) < len(m.hit_order) else '')
            if status == 'ERROR':
                errs += 1; probs.append(f'{where}: sby error: {err}'); continue
            if status == 'TIMEOUT':
                tmo += 1; probs.append(f'{where}: {err} (covers reached so far: {len(rch)})'); continue
            if m.kind == 'constraints':
                if 'sat' not in rch:
                    unsat += 1
                    core = gen.unsat_core(m)
                    why = '' if not core else '; conflicting: ' + ' | '.join(
                        f"{a['entry'].get('file')}:{a['entry'].get('line')} {a['entry'].get('src') or a['entry'].get('item')}"
                        for a in core)
                    probs.append(f'{m.name}: constraints unsatisfiable for every input{why}')
                dead = sorted(x for x in unr if x.startswith('live_'))
                for x in dead: probs.append(f'{m.name}: guard never true under the constraints: {x[5:]}')
            else:
                total += len(rch) + len(unr); reached += len(rch)
                for x in sorted(unr): probs.append(f'{m.name}: bin unreachable with free inputs: {x}')
        gen.smoke_results = {'modules': len({j.m.name for j in gen.smokes}), 'jobs': len(res), 'errors': errs,
                             'timeouts': tmo, 'unsat': unsat, 'covers': total, 'reached': reached, 'problems': probs}
    if a.witness:
        witness(gen, a.out)
    if a.run or a.witness:
        rep = summarize(gen)
        with open(os.path.join(a.out, 'report.json'), 'w') as f:
            json.dump(rep, f, indent=1, default=lambda o: sorted(o) if isinstance(o, set) else str(o))
        with open(os.path.join(a.out, 'report.md'), 'w') as f:
            f.write(report_md(rep))
    c = rep['counts']
    print(json.dumps(c))
    if rep.get('smoke'):
        s = rep['smoke']
        print(f"smoke: {s['modules']} modules, {s['errors']} errors, {s['timeouts']} timeouts, {s['unsat']} unsat, "
              f"covers {s['reached']}/{s['covers']}")
    if rep.get('replay') is not None:
        rows = rep['replay']
        cnt = {}
        for r in rows: cnt[r['status']] = cnt.get(r['status'], 0) + 1
        print('witness: ' + ', '.join(f'{k} {v}' for k, v in sorted(cnt.items())) + ' -> fv_replay.sv, replay.json')
    print(f'-> {a.out}')


if __name__ == '__main__':
    main()
