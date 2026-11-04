#!/usr/bin/env python3
"""Lower concurrent SVA to immediate assertions plus flops, for tools that read only immediate
assertions (yosys-slang).

    svalower.py -o OUTDIR -- <slang args>        (writes OUTDIR/lowered.f and the rewritten files)

Every `assert/assume/cover property` in the sources (written directly or produced by a macro such as
OpenTitan's `ASSERT) is replaced, in a copy of its file, by an equivalent immediate assertion in an
always_ff on the property's clock:

  - boolean terms delayed by k cycles are shift registers that clear while `disable iff` holds, so an
    attempt that spans a disabled cycle is cancelled, as in SVA
  - A |-> C, A |=> C with A, C fixed-delay sequences (a ##n b ...): for each step of C, `!(A matched
    ending d cycles ago) || C_step`; a sequence alone is `1 |-> seq`; cover of a sequence is the
    conjunction of its delayed terms (cover of A |-> C: A matched and C)
  - $past(e[, n]), $rose, $fell, $stable, $changed through flops of e; $isunknown is 0 (2-state);
    $sampled(e) is e
  - named sequences / properties without arguments are inlined

Anything else (liveness: s_eventually / eventually / strong; ranged or unbounded delays; repetition;
throughout / within / until / intersect / and / or of sequences; local variables; arguments; no clock)
is removed from the copy and listed as unsupported, so the rest still reads.
"""
import argparse, json, os, re, sys
import pyslang

SK = pyslang.syntax.SyntaxKind
STMTS = {SK.AssertPropertyStatement: 'assert', SK.AssumePropertyStatement: 'assume',
         SK.CoverPropertyStatement: 'cover'}


class Unsupported(Exception):
    pass


class Skipped(Exception):
    """a kind of property the caller does not want (e.g. fvgen --rtl keeps only assumptions)"""


def kids(n):
    for c in n:
        if hasattr(c, 'kind') and not isinstance(c, pyslang.parsing.Token):
            yield c


def txt(n):
    """source text of a node without comments (trivia), on one line"""
    if isinstance(n, pyslang.parsing.Token): return n.rawText
    toks = []
    def walk(x):
        for c in x:
            if isinstance(c, pyslang.parsing.Token):
                if c.rawText: toks.append(c.rawText)
            elif c is not None and hasattr(c, 'kind'):
                walk(c)
    walk(n)
    return ' '.join(toks)


class Ctx:
    """one assertion being lowered: its clock, disable condition, and helper declarations"""

    def __init__(self, label, clk, dis):
        self.label, self.clk, self.dis = label, clk, dis
        self.decls, self.updates = [], []
        self.delayed = {}           # (text, k, gated) -> name
        self.n = 0

    def name(self, what):
        self.n += 1
        return f'fv_{self.label}_{what}{self.n}'

    def delay(self, e, k, gated=True):
        """e k cycles ago (1-bit, cleared by disable when gated)"""
        if k == 0: return f'({e})'
        key = (e, k, gated)
        if key in self.delayed: return self.delayed[key]
        prev = self.delay(e, k - 1, gated) if k > 1 else f'({e})'
        nm = self.name('d')
        self.decls.append(f"logic {nm} = 1'b0;")
        self.updates.append(f"{nm} <= ({self.dis}) ? 1'b0 : {prev};" if gated else f'{nm} <= {prev};')
        self.delayed[key] = nm
        return nm

    def past(self, e, k, width):
        """$past(e, k): a vector of e's width, not cleared by disable"""
        key = (e, k, 'past')
        if key in self.delayed: return self.delayed[key]
        prev = self.past(e, k - 1, width) if k > 1 else f'({e})'
        nm = self.name('p')
        self.decls.append(f"logic [{width}-1:0] {nm} = '0;")
        self.updates.append(f'{nm} <= {prev};')
        self.delayed[key] = nm
        return nm


class Lowerer:
    def __init__(self, tree, widths=None):
        self.tree = tree
        self.widths = widths or {}          # source position of an expression -> bit width
        self.seqs, self.props = {}, {}      # module-level declarations by name (no arguments)
        self.local_seqs = set()             # declarations with local variables: not lowered
        self.collect(tree.root)

    def collect(self, n):
        k = getattr(n, 'kind', None)
        if k == SK.SequenceDeclaration:
            if not getattr(n, 'portList', None) and not has_kind(n, LOCALS):
                self.seqs[str(n.name).strip()] = n.seqExpr
            elif has_kind(n, LOCALS): self.local_seqs.add(str(n.name).strip())
        elif k == SK.PropertyDeclaration:
            if not getattr(n, 'portList', None) and not has_kind(n, LOCALS):
                self.props[str(n.name).strip()] = n.propertySpec
            elif has_kind(n, LOCALS): self.local_seqs.add(str(n.name).strip())
        for c in kids(n): self.collect(c)

    # ---- booleans
    def boolean(self, e, ctx):
        """expression text with sampled-value functions replaced by flops"""
        text = txt(e)
        calls = []
        def find(n):
            if getattr(n, 'kind', None) == SK.InvocationExpression and \
                    getattr(n.left, 'kind', None) == SK.SystemName:
                calls.append(n); return
            for c in kids(n): find(c)
        find(e)
        for c in calls:
            fn = txt(c.left)
            args = [a for a in kids(c.arguments)] if c.arguments is not None else []
            args = [txt(a) for a in args if a.kind != SK.EmptyArgument]
            if fn == '$isunknown': r = "1'b0"
            elif fn == '$sampled': r = f'({args[0]})'
            elif fn in ('$past', '$rose', '$fell', '$stable', '$changed'):
                if not args: raise Unsupported(f'{fn} without an argument')
                k = 1
                if fn == '$past' and len(args) > 1:
                    try: k = int(args[1])
                    except ValueError: raise Unsupported(f'$past with a non-constant number of cycles')
                    if len(args) > 2: raise Unsupported('$past with a gating expression')
                a = args[0]
                an = next(x for x in kids(c.arguments) if x.kind != SK.EmptyArgument)
                w = self.width(an, a)
                if fn in ('$rose', '$fell'): w = 1      # the least significant bit only
                p = ctx.past(a, k, w)
                if fn == '$past': r = p
                else:
                    cur = ctx.name('e')
                    ctx.decls.append(f'wire [{w}-1:0] {cur} = {a};')
                    r = {'$rose': f'({cur}[0] && !{p}[0])', '$fell': f'(!{cur}[0] && {p}[0])',
                         '$stable': f'({cur} == {p})', '$changed': f'({cur} != {p})'}[fn]
            else:
                continue                    # $countones, $onehot, ...: yosys-slang reads them
            text = text.replace(txt(c), r)
        return f'({text})'

    def width(self, node, text):
        """bit width of an argument expression, from elaboration (largest over instances); $bits()
        is not allowed on hierarchical names, so it is only the fallback"""
        w = self.widths.get(srckey(node.sourceRange))
        return str(w) if w else f'$bits({text})'

    # ---- sequences: [(delay from the previous element, boolean text)]
    def seq(self, n, ctx):
        k = n.kind
        if has_kind(n, (SK.SequenceMatchList,)): raise Unsupported('local variable assignment in a sequence')
        names = {txt(x) for x in iter_kind(n, (SK.IdentifierName,))}
        if names & self.local_seqs: raise Unsupported('sequence with local variables')
        if k == SK.SimpleSequenceExpr:
            if getattr(n, 'repetition', None) is not None: raise Unsupported('sequence repetition [* / [= / [->')
            e = n.expr
            if e.kind == SK.IdentifierName and txt(e) in self.seqs:
                return self.seq(self.seqs[txt(e)], ctx)
            if e.kind == SK.ParenthesizedExpression and e.expression.kind == SK.IdentifierName and \
                    txt(e.expression) in self.seqs:
                return self.seq(self.seqs[txt(e.expression)], ctx)
            return [(0, self.boolean(e, ctx))]
        if k == SK.ParenthesizedSequenceExpr:
            if getattr(n, 'repetition', None) is not None: raise Unsupported('sequence repetition')
            return self.seq(n.expr, ctx)
        if k == SK.DelayedSequenceExpr:
            out = self.seq(n.first, ctx) if n.first is not None else []
            for el in n.elements:
                d = self.delay_value(el)
                rest = self.seq(el.expr, ctx)
                if not out:
                    out = [(d, rest[0][1])] + rest[1:]
                else:
                    out += [(d + rest[0][0], rest[0][1])] + rest[1:]
            return out
        if k in (SK.AndSequenceExpr, SK.OrSequenceExpr):
            l, r = self.seq(n.left, ctx), self.seq(n.right, ctx)
            if len(l) == 1 and len(r) == 1 and l[0][0] == 0 and r[0][0] == 0:   # single-cycle operands
                op = '&&' if k == SK.AndSequenceExpr else '||'
                return [(0, f'({l[0][1]} {op} {r[0][1]})')]
            raise Unsupported(f'{"and" if k == SK.AndSequenceExpr else "or"} of multi-cycle sequences')
        raise Unsupported(f'sequence {k.name}: {txt(n)[:60]}')

    def delay_value(self, el):
        if getattr(el, 'range', None) is not None or getattr(el, 'delayVal', None) is None:
            raise Unsupported(f'ranged or unbounded delay {txt(el)[:40]}')
        try: return int(txt(el.delayVal))
        except ValueError: raise Unsupported(f'non-constant delay {txt(el.delayVal)}')

    # ---- properties
    def prop(self, n, ctx):
        """('impl', A, C) with C relative to A's end, or ('seq', S)"""
        k = n.kind
        if k == SK.SimplePropertyExpr:
            e = n.expr
            if e.kind == SK.SimpleSequenceExpr and e.expr.kind == SK.IdentifierName and txt(e.expr) in self.props:
                return self.prop(self.props[txt(e.expr)].expr, ctx)
            return ('seq', self.seq(e, ctx))
        if k == SK.ParenthesizedPropertyExpr:
            return self.prop(n.expr, ctx)
        if k == SK.ImplicationPropertyExpr:
            op = txt(n.op)
            a = self.prop(n.left, ctx)
            c = self.prop(n.right, ctx)
            if a[0] != 'seq' or c[0] != 'seq': raise Unsupported('nested implication')
            cs = c[1]
            if op == '|=>': cs = [(cs[0][0] + 1, cs[0][1])] + cs[1:]
            elif op != '|->': raise Unsupported(f'operator {op}')
            return ('impl', a[1], cs)
        if k == SK.UnaryPropertyExpr and txt(n.op) == 'not':
            raise Unsupported('not of a property')
        raise Unsupported(f'property {k.name}: {txt(n)[:60]}')

    def lower(self, stmt, kind, label, defaults=(None, None)):
        spec = stmt.propertySpec
        if spec.clocking is not None:
            ev = spec.clocking
            clk = txt(ev.expr) if getattr(ev, 'expr', None) is not None else txt(ev)
        elif defaults[0]:
            clk = defaults[0]               # default clocking of the enclosing module
        else:
            raise Unsupported('no clock (and no default clocking)')
        clk = clk[1:-1] if clk.startswith('(') and clk.endswith(')') else clk
        if spec.disable is not None: dis = txt(spec.disable.expr)
        elif defaults[1]: dis = defaults[1]
        else: dis = "1'b0"
        ctx = Ctx(label, clk, f'({dis})')
        p = self.prop(spec.expr, ctx)
        def match(sq, end_ago):
            """the sequence matched ending end_ago cycles ago"""
            total = sum(d for d, _ in sq)
            terms, cum = [], 0
            for d, e in sq:
                cum += d
                terms.append(ctx.delay(e, end_ago + total - cum))
            return ' && '.join(terms)
        checks = []
        if p[0] == 'seq':
            if kind == 'cover': checks.append(match(p[1], 0))
            else: p = ('impl', [(0, "1'b1")], p[1])
        if p[0] == 'impl':
            a, c = p[1], p[2]
            if kind == 'cover':
                lc = sum(d for d, _ in c)
                checks.append(' && '.join([match(a, lc)] + [ctx.delay(e, lc - cum)
                                                            for cum, e in cumulative(c)]))
            else:
                # step j is checked only if the antecedent and the consequent's earlier steps matched in
                # this attempt: an attempt fails once, at its first unmatched step, as SVA reports it
                steps = cumulative(c)
                for j, (cum, e) in enumerate(steps):
                    pre = [match(a, cum)] + [ctx.delay(ei, cum - ci) for ci, ei in steps[:j]]
                    checks.append(f"!({' && '.join(pre)}) || {e}")
        lines = [f'  // svalower: {kind} property {label}: {txt(spec)[:200]}']
        lines += [f'  {d}' for d in ctx.decls]
        if ctx.updates:
            lines.append(f'  always_ff @({clk}) begin')
            lines += [f'    {u}' for u in ctx.updates]
            lines.append('  end')
        for i, ck in enumerate(checks):
            nm = label if len(checks) == 1 else f'{label}_{i}'
            lines.append(f'  always_ff @({clk}) if (!{ctx.dis}) {nm}: {kind} ({ck});')
        return '\n'.join(lines)


LOCALS = (SK.LocalVariableDeclaration, SK.SequenceMatchList)


def iter_kind(n, ks):
    if getattr(n, 'kind', None) in ks: yield n
    for c in kids(n): yield from iter_kind(c, ks)


def has_kind(n, ks):
    return next(iter_kind(n, ks), None) is not None


def cumulative(sq):
    out, cum = [], 0
    for d, e in sq:
        cum += d
        out.append((cum, e))
    return out


def module_name(n):
    m = n
    while m is not None and getattr(m, 'kind', None) not in (SK.ModuleDeclaration, SK.InterfaceDeclaration,
                                                              SK.ProgramDeclaration):
        m = getattr(m, 'parent', None)
    return m.header.name.valueText if m is not None else None


def module_defaults(n):
    """(default clocking event, default disable iff expression) of the enclosing module"""
    m = n
    while m is not None and getattr(m, 'kind', None) not in (SK.ModuleDeclaration, SK.InterfaceDeclaration,
                                                              SK.ProgramDeclaration):
        m = getattr(m, 'parent', None)
    if m is None: return (None, None)
    clk = dis = None
    def walk(x):
        nonlocal clk, dis
        k = getattr(x, 'kind', None)
        if k == SK.ClockingDeclaration and txt(x.globalOrDefault) == 'default' and x.event is not None:
            clk = txt(x.event)
        elif k == SK.DefaultDisableDeclaration and x.expr is not None:
            dis = txt(x.expr)
        if k in (SK.ModuleDeclaration, SK.InterfaceDeclaration, SK.ProgramDeclaration) and x is not m: return
        for c in kids(x): walk(c)
    walk(m)
    return (clk, dis)


def srckey(rng):
    try: return (rng.start.buffer.id, rng.start.offset, rng.end.offset)
    except Exception: return None


def arg_widths(d):
    """source position -> bit width of every $past/$rose/$fell/$stable/$changed argument, from an
    elaboration of the sources (largest over instances)"""
    widths = {}
    try:
        comp = d.createCompilation()
        comp.getAllDiagnostics()
    except Exception:
        return widths
    A = pyslang.ast
    def cb(n):
        if isinstance(n, A.CallExpression) and n.isSystemCall and n.subroutineName in (
                '$past', '$rose', '$fell', '$stable', '$changed') and n.arguments:
            a = n.arguments[0]
            try:
                k = srckey(a.sourceRange)
                if k and a.type.isIntegral: widths[k] = max(widths.get(k, 0), a.type.bitWidth)
            except Exception: pass
        return True
    try: comp.getRoot().visit(cb)
    except Exception: pass
    return widths


def original_range(sm, rng):
    """the source text a statement came from: the outermost macro call when it was expanded"""
    s, e = rng.start, rng.end
    while sm.isMacroLoc(s):
        r = sm.getExpansionRange(s)
        s = r.start
    while sm.isMacroLoc(e):
        r = sm.getExpansionRange(e)
        e = r.end
    return s, e


def run(slang_args, out_dir, kinds=('assert', 'assume', 'cover'), keep=None, unguard=()):
    d = pyslang.driver.Driver()
    d.addStandardArgs()
    if not d.parseCommandLine('slang ' + ' '.join(slang_args), pyslang.driver.CommandLineOptions()):
        sys.exit('bad slang arguments')
    if not d.processOptions(): sys.exit('bad slang options')
    if not d.parseAllSources(): sys.exit('parse errors in the sources')
    sm = d.sourceManager
    os.makedirs(out_dir, exist_ok=True)
    edits, report = {}, []
    widths = arg_widths(d)
    for tree in d.syntaxTrees:
        low = Lowerer(tree, widths)
        stmts = []
        def find(n):
            k = getattr(n, 'kind', None)
            if k in STMTS: stmts.append(n); return
            for c in kids(n): find(c)
        find(tree.root)
        decls = []
        def find_decls(n):
            k = getattr(n, 'kind', None)
            if k in (SK.SequenceDeclaration, SK.PropertyDeclaration): decls.append(n); return
            for c in kids(n): find_decls(c)
        find_decls(tree.root)
        # default clocking / disable iff: applied to the lowered assertions; yosys-slang rejects them
        for dc in iter_kind(tree.root, (SK.DefaultDisableDeclaration,)):
            s, e = original_range(sm, dc.sourceRange)
            edits.setdefault(os.path.abspath(sm.getFileName(s)), {})[(s.offset, e.offset)] = '  // svalower: default disable iff applied'
        for dc in iter_kind(tree.root, (SK.ClockingDeclaration,)):
            if txt(dc.globalOrDefault) != 'default': continue
            s, e = original_range(sm, dc.sourceRange)
            edits.setdefault(os.path.abspath(sm.getFileName(s)), {})[(s.offset, e.offset)] = '  // svalower: default clocking applied'
        # a flop with an asynchronous load and no clocked branch (`if (!rst_n) q <= 0;`): yosys-slang
        # needs the if-else shape; an empty else keeps the meaning (hold)
        for ff in iter_kind(tree.root, (SK.AlwaysFFBlock, SK.AlwaysBlock)):
            ev = txt(ff.statement.timingControl) if getattr(ff.statement, 'timingControl', None) is not None else ''
            if ' or ' not in ev and ',' not in ev: continue
            body = ff.statement.statement
            while getattr(body, 'kind', None) == SK.SequentialBlockStatement and len(body.items) == 1:
                body = body.items[0]
            if getattr(body, 'kind', None) == SK.ConditionalStatement and body.elseClause is None:
                s, e = original_range(sm, body.sourceRange)
                if sm.isMacroLoc(body.sourceRange.start): continue
                edits.setdefault(os.path.abspath(sm.getFileName(s)), {})[(e.offset, e.offset)] = ' else begin end'
        for dc in decls:                    # inlined where used; yosys-slang rejects the declarations
            s, e = original_range(sm, dc.sourceRange)
            edits.setdefault(os.path.abspath(sm.getFileName(s)), {})[(s.offset, e.offset)] = \
                f'  // svalower: {"sequence" if dc.kind == SK.SequenceDeclaration else "property"} ' \
                f'{str(dc.name).strip()} inlined where it is used'
        for st in stmts:
            kind = STMTS[st.kind]
            lab = st.label
            label = lab.name.valueText if lab is not None else None       # without comment trivia
            s, e = original_range(sm, st.sourceRange)
            path = os.path.abspath(sm.getFileName(s))
            line = sm.getLineNumber(s)
            if not label: label = f'sva_{os.path.basename(path).split(".")[0]}_{line}'
            label = re.sub(r'\W', '_', label)
            module = module_name(st)
            try:
                if kind not in kinds:
                    raise Skipped(f'{kind} not kept (kept: {", ".join(kinds)})')
                if keep is not None:
                    why = keep(kind, module, os.path.abspath(path))
                    if why: raise Skipped(why)
                text = low.lower(st, kind, label, module_defaults(st))
                status, why = 'lowered', None
            except Skipped as ex:
                text = f'  // svalower: {kind} property {label} left out: {ex}'
                status, why = 'skipped', str(ex)
            except Unsupported as ex:
                text = f'  // svalower: {kind} property {label} removed: {ex}'
                status, why = 'unsupported', str(ex)
            report.append({'name': label, 'kind': kind, 'module': module, 'file': path, 'line': line, 'status': status,
                           'why': why, 'src': txt(st)[:200]})
            edits.setdefault(path, {})[(s.offset, e.offset)] = text
    files = {}
    unguard = {os.path.abspath(x) for x in unguard}
    for path in unguard: edits.setdefault(path, {})
    for path, eds in edits.items():
        src = open(path).read()
        # nested macro expansions can map several statements to one call: keep the widest edit
        spans = sorted(eds.items(), key=lambda kv: (kv[0][0], -kv[0][1]))
        kept, last_end = [], -1
        for (a, b), t in spans:
            if a >= last_end: kept.append(((a, b), t)); last_end = b
            else: kept[-1] = (kept[-1][0], kept[-1][1] + '\n' + t)
        for (a, b), t in sorted(kept, key=lambda kv: -kv[0][0]):
            src = src[:a] + t + src[b:]
        if os.path.abspath(path) in unguard:
            # a checker read next to synthesizable RTL: its `ifndef SYNTHESIS content is wanted, the RTL's
            # simulation-only code is not; SYNTHESIS is lifted for this file only
            src = ('`ifdef SYNTHESIS\n  `define SVALOWER_SYNTHESIS\n  `undef SYNTHESIS\n`endif\n' + src +
                   '\n`ifdef SVALOWER_SYNTHESIS\n  `define SYNTHESIS\n  `undef SVALOWER_SYNTHESIS\n`endif\n')
        dst = os.path.join(out_dir, 'src', os.path.basename(path))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, 'w') as f: f.write(src)
        files[os.path.abspath(path)] = dst
    return files, report


def flatten_args(args, base=None):
    """expand -f files into a flat argument list (paths made absolute; $VAR / ${VAR} from the
    environment, as slang does)"""
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a in ('-f', '-F') and i + 1 < len(args):
            fn = args[i + 1] if os.path.isabs(args[i + 1]) or base is None else os.path.join(base, args[i + 1])
            toks = []
            for line in open(fn):
                line = os.path.expandvars(line.split('//')[0]).strip()
                if line: toks += line.split()
            out += flatten_args(toks, os.path.dirname(os.path.abspath(fn)))
            i += 2; continue
        if a.startswith('+incdir+'):
            out += ['+incdir+' + os.path.abspath(os.path.join(base or '.', p)) for p in a[8:].split('+') if p]
        elif a.startswith(('+define+', '-D', '--')) or a.startswith('-'):
            out.append(a)
        else:
            out.append(os.path.abspath(os.path.join(base or '.', a)) if not os.path.isabs(a) else a)
        i += 1
    return out


def lower_filelists(filelists, out_dir, defines=(), kinds=('assert', 'assume', 'cover'), keep=None, unguard=()):
    """lowered copy of the sources named by slang argument files: (path of the new flat filelist,
    report). Properties of other kinds than `kinds` are left out of the copies."""
    out_dir = os.path.abspath(out_dir)      # callers run tools from other directories
    args = []
    for f in filelists: args += flatten_args(['-f', os.path.abspath(f)])
    args += [f'+define+{x}' for x in defines]
    files, report = run(args, out_dir, kinds, keep, unguard)
    flat = [files.get(a, a) for a in args]
    fl = os.path.join(out_dir, 'lowered.f')
    with open(fl, 'w') as f: f.write('\n'.join(flat) + '\n')
    with open(os.path.join(out_dir, 'sva_report.json'), 'w') as f: json.dump(report, f, indent=1)
    return fl, report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('-o', '--out', required=True)
    ap.add_argument('-f', dest='filelists', action='append', default=[], help='slang argument file')
    ap.add_argument('-D', dest='defines', action='append', default=[])
    ap.add_argument('--kinds', default='assert,assume,cover', help='property kinds to keep (others are left out)')
    a = ap.parse_args()
    fl, rep = lower_filelists(a.filelists, a.out, a.defines, tuple(a.kinds.split(',')))
    n = {}
    for r in rep: n[r['status']] = n.get(r['status'], 0) + 1
    print(f'{n} -> {fl}')


if __name__ == '__main__':
    main()
