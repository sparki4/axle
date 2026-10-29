"""Footprint analysis for AXLE programs: what can this line / function / program do?

The compiler records a fact for every operation that can allocate, abort, perform I/O,
alias shared storage, touch raw memory, cross into foreign code, or loop.  This module
turns those facts into three views:

  lens   one source line: what it can do, including everything its callees can do
  audit  every function: declared vs. actually used effects, hidden behavior, suggested guarantees
  slice  the minimum context needed to understand or change one function

Everything here is derived from the same facts the compiler uses to enforce `nothrow`
and `noalloc`, so the report cannot drift from what is actually checked.
"""
import json
import pathlib
import re

import axle

# ---------------------------------------------------------------- syntax-tree helpers

def walk_stmts(statements):
    for st in statements:
        yield st
        kind = st[0]
        if kind == 'if':
            yield from walk_stmts(st[3])
            if st[4]: yield from walk_stmts(st[4])
        elif kind == 'while':
            yield from walk_stmts(st[3])
        elif kind == 'unsafe':
            yield from walk_stmts(st[2])


def stmt_exprs(st):
    kind = st[0]
    if kind == 'bind': return [st[5]]
    if kind == 'assign': return [st[2], st[3]]
    if kind == 'return': return [st[2]] if st[2] is not None else []
    if kind in {'if', 'while'}: return [st[2]]
    if kind == 'expr': return [st[2]]
    return []


def walk_expr(e):
    yield e
    kind = e[0]
    if kind == 'unary': yield from walk_expr(e[2])
    elif kind == 'binary':
        yield from walk_expr(e[2])
        yield from walk_expr(e[3])
    elif kind == 'call':
        for a in e[2]: yield from walk_expr(a)


def all_exprs(body):
    for st in walk_stmts(body):
        for e in stmt_exprs(st):
            yield from walk_expr(e)


def is_literal(e):
    return e is not None and e[0] in {'number', 'bool'}


def is_zero(e):
    return e[0] == 'number' and int(re.match(r'[0-9]+', e[1].value)[0]) == 0


def returns_made_up_value(block):
    """True if the block returns a literal (or nothing) instead of failing."""
    return any(st[0] == 'return' and (st[2] is None or is_literal(st[2])) for st in walk_stmts(block))


BUCKETS = {'abort': 'aborts', 'alloc': 'allocs', 'io': 'io', 'write': 'writes', 'loop': 'loops', 'alias': 'aliases'}


class Analysis:
    def __init__(self, path):
        self.entry = str(pathlib.Path(path).resolve())
        self.loaded = axle.load_program(path)
        loaded = self.loaded
        self.compiler = axle.Compiler(functions=loaded.functions, externs=loaded.externs, links=loaded.links,
                                      natives=loaded.natives, guarantees=loaded.guarantees, require_main=False, structs=loaded.structs)
        self.compiler.compile()
        self.facts = self.compiler.facts
        self.graph = self.compiler.call_graph()
        self.info = {}
        for name, params, result, effects, _ in loaded.functions:
            start, end = loaded.spans[name.value][1:]
            self.info[name.value] = dict(name=name.value, kind='fn', file=name.source, start=start, end=end,
                                         params=[(p.value, t) for p, t in params], result=result,
                                         effects=sorted(effects), guarantees=sorted(loaded.guarantees.get(name.value, ())))
        for kind_token, kind, name, params, result, effects, symbol in loaded.externs:
            start, end = loaded.spans[name.value][1:]
            declared = set(effects) | ({'io'} if kind != 'c' else set())
            self.info[name.value] = dict(name=name.value, kind='extern', abi=kind, symbol=symbol, file=name.source,
                                         start=start, end=end, params=[(p.value, t) for p, t in params], result=result,
                                         effects=sorted(declared), guarantees=sorted(loaded.guarantees.get(name.value, ())))
        self._used = self._used_effects()

    # ------------------------------------------------------------ call graph
    def reach(self, name):
        parent, queue = {name: None}, [name]
        while queue:
            current = queue.pop(0)
            for nxt in self.graph.get(current, []):
                if nxt not in parent:
                    parent[nxt] = current
                    queue.append(nxt)
        return parent

    @staticmethod
    def chain(parent, node):
        path = []
        while node is not None:
            path.append(node)
            node = parent[node]
        return list(reversed(path))

    def _used_effects(self):
        """Effects a function really uses, transitively (fixpoint over the call graph)."""
        used = {n: set() for n in self.info}
        for n, facts in self.facts.items():
            for fact in facts:
                if fact.get('effect'): used[n].add(fact['effect'])
        for n, i in self.info.items():
            if i['kind'] == 'extern': used[n] = set(i['effects'])
        changed = True
        while changed:
            changed = False
            for n in self.info:
                for callee in self.graph.get(n, []):
                    if not used[callee] <= used[n]:
                        used[n] |= used[callee]
                        changed = True
        return used

    # ------------------------------------------------------------ summaries
    def summary(self, name):
        """Everything `name` can do when called, including all transitive callees."""
        parent = self.reach(name)
        out = {key: [] for key in ('aborts', 'allocs', 'io', 'writes', 'loops', 'aliases', 'foreign', 'unsafe')}
        for fn in parent:
            where = ' -> '.join(self.chain(parent, fn))
            if self.info[fn]['kind'] == 'extern':
                out['foreign'].append(dict(function=fn, abi=self.info[fn]['abi'], symbol=self.info[fn]['symbol'], via=where))
                if self.info[fn]['abi'] != 'c':
                    lang = self.info[fn]['abi'].split(':')[0]
                    out['aborts'].append(dict(function=fn, line=self.info[fn]['start'], via=where, what=f'{lang} code can raise'))
                continue
            for fact in self.facts.get(fn, []):
                item = dict(function=fn, line=fact['line'], via=where)
                kind = fact['kind']
                if kind in BUCKETS:
                    item['what'] = fact.get('why') or fact.get('what') or fact.get('target', '')
                    out[BUCKETS[kind]].append(item)
                elif kind == 'unsafe' and not fact.get('block'):
                    item['what'] = fact['what']
                    out['unsafe'].append(item)
                elif kind == 'foreign' and 'callee' not in fact:
                    out['foreign'].append(dict(function=fn, abi=fact['abi'], symbol=fact['symbol'], via=where))
        callees = self.graph.get(name, [])
        out['recursive'] = any(name in self.reach(c) for c in callees)
        used = self._used[name]
        declared = set(self.info[name]['effects'])
        out['effects_declared'] = sorted(declared)
        out['effects_used'] = sorted(used)
        out['effects_unused'] = sorted(declared - used)
        out['guarantees'] = self.info[name]['guarantees']
        suggestions = []
        if self.info[name]['kind'] == 'fn':
            for g in ('nothrow', 'noalloc'):
                if g not in self.info[name]['guarantees'] and self.compiler.violation(name, g, self.graph) is None:
                    suggestions.append(g)
        out['could_declare'] = suggestions
        out['may_not_terminate'] = bool(out['loops']) or out['recursive']
        return out

    @staticmethod
    def phrase(summary):
        """One short clause describing a callee's total footprint."""
        parts = []
        if summary['aborts']: parts.append('can abort (' + '; '.join(sorted({a['what'] for a in summary['aborts']})) + ')')
        if summary['allocs']: parts.append('allocates (' + ', '.join(sorted({a['what'] for a in summary['allocs']})) + ')')
        if summary['io']: parts.append('does I/O')
        if summary['writes']: parts.append('mutates state (' + ', '.join(sorted({w['what'] for w in summary['writes']})) + ')')
        if summary['foreign']:
            names = {'c': 'C', 'py': 'Python', 'js': 'JavaScript', 'polyglot': 'a polyglot guest'}
            labels = sorted({f"{names.get(f['abi'].split(':')[0], f['abi'])} `{f['symbol']}`" + (' (opaque to AXLE)' if f['abi'] == 'c' else '') for f in summary['foreign']})
            parts.append('calls foreign code: ' + ', '.join(labels))
        if summary['unsafe']: parts.append('uses unsafe memory access')
        if summary['may_not_terminate']: parts.append('may not terminate (loop or recursion)')
        return ', '.join(parts) if parts else 'pure: no aborts, allocations, I/O, mutation or foreign calls'

    # ------------------------------------------------------------ risky patterns
    def docs(self, name):
        info = self.info[name]
        lines = pathlib.Path(info['file']).read_text(encoding='utf-8').splitlines()
        out, i = [], info['start'] - 2
        while i >= 0 and lines[i].lstrip().startswith('///'):
            out.insert(0, lines[i].lstrip()[3:].strip())
            i -= 1
        return out

    def risks(self):
        """Patterns that usually hide a failure instead of surfacing it."""
        out = []
        bodies = {name.value: (name, params, body) for name, params, _, _, body in self.loaded.functions}

        def add(kind, fn, line, detail, strict):
            out.append(dict(kind=kind, function=fn, line=line, detail=detail, strict=strict))

        for fn, (name, params, body) in bodies.items():
            exprs = list(all_exprs(body))
            names = {e[1].value for e in exprs if e[0] == 'name'}
            returns = [st for st in walk_stmts(body) if st[0] == 'return']
            if params and not any(p.value in names for p, _ in params) and returns and all(is_literal(r[2]) for r in returns):
                add('stub-like', fn, name.line, 'ignores every parameter and only returns constants: a stub, or made-up data', True)
            for p, _ in params:
                if p.value not in names and any(p2.value in names for p2, _ in params):
                    add('unused-parameter', fn, p.line, f'parameter `{p.value}` is never read', False)
            divisors = {e[3][1].value for e in exprs if e[0] == 'binary' and e[1].value in {'/', '%'} and e[3][0] == 'name'}
            indexed = {(e[2][0][1].value, e[2][1][1].value) for e in exprs
                       if e[0] == 'call' and e[1].value in {'buf_get', 'buf_set'} and len(e[2]) >= 2 and e[2][0][0] == 'name' and e[2][1][0] == 'name'}
            for st in walk_stmts(body):
                if st[0] != 'if': continue
                cond = st[2]
                if cond[0] != 'binary': continue
                sides = (cond[2], cond[3])
                if cond[1].value == '==':
                    for a, b in (sides, sides[::-1]):
                        if a[0] == 'name' and a[1].value in divisors and b[0] == 'number' and is_zero(b) and returns_made_up_value(st[3]):
                            add('masked-failure', fn, st[1].line,
                                f'when `{a[1].value}` is 0 this returns a made-up value instead of failing; use assert({a[1].value} != 0) or handle it explicitly', True)
                if cond[1].value in {'>=', '>', '<', '<='}:
                    for a, b in (sides, sides[::-1]):
                        if a[0] == 'name' and b[0] == 'call' and b[1].value == 'buf_len' and b[2] and b[2][0][0] == 'name' and \
                                (b[2][0][1].value, a[1].value) in indexed and returns_made_up_value(st[3]):
                            add('masked-failure', fn, st[1].line,
                                f'an out-of-range `{a[1].value}` returns a made-up value instead of failing; use assert', True)
            for e in exprs:
                if e[0] == 'call' and e[1].value == 'assert' and len(e[2]) == 1:
                    a = e[2][0]
                    trivial = (a[0] == 'bool' and a[1].value == 'true') or \
                              (a[0] == 'binary' and a[1].value == '==' and ((a[2][0] == 'name' and a[3][0] == 'name' and a[2][1].value == a[3][1].value) or
                                                                           (a[2][0] == 'number' and a[3][0] == 'number' and a[2][1].value == a[3][1].value)))
                    if trivial:
                        add('tautological-assert', fn, e[1].line, 'this assertion can never fail, so it checks nothing', True)
            for fact in self.facts.get(fn, []):
                if fact['kind'] == 'unimplemented':
                    add('unimplemented', fn, fact['line'], 'placeholder: this code path is not implemented', True)
            if fn.startswith('test_') and not bodies[fn][1]:
                parent = self.reach(fn)
                checks = any(f['kind'] == 'abort' and f.get('via') in {'assert', 'abort'} for n in parent for f in self.facts.get(n, []))
                if not checks:
                    add('test-without-assert', fn, name.line, 'this test never asserts anything, so it cannot fail', True)
            risky = any(f['kind'] == 'unsafe' or f['kind'] == 'foreign' for f in self.facts.get(fn, [])) or self.info[fn]['guarantees']
            if risky and not self.docs(fn):
                add('undocumented-risky-function', fn, name.line, 'uses unsafe memory, foreign code or a guarantee but has no `///` documentation saying why', False)
        return out

    # ------------------------------------------------------------ architecture
    def module_purpose(self, file):
        out = []
        for line in pathlib.Path(file).read_text(encoding='utf-8').splitlines():
            text = line.strip()
            if text.startswith('//!'): out.append(text[3:].strip())
            elif text and not text.startswith('//') and not text.startswith('edition'): break
        return out

    def architecture(self):
        entry_dir = pathlib.Path(self.entry).parent
        rel = lambda f: pathlib.Path(f).resolve().relative_to(entry_dir).as_posix() if entry_dir in pathlib.Path(f).resolve().parents else f
        modules = {}
        for file in self.loaded.modules:
            modules[file] = dict(path=rel(file), purpose=self.module_purpose(file), imports=[], functions=[], calls_into={})
        for importer, imported in self.loaded.import_edges:
            if importer in modules and imported in modules and rel(imported) not in modules[importer]['imports']:
                modules[importer]['imports'].append(rel(imported))
        undocumented = []
        for name, info in self.info.items():
            if info['file'] not in modules: continue
            docs = self.docs(name)
            why = [line[4:].strip() for line in docs if line.lower().startswith('why:')]
            summary = self.summary(name)
            entry = dict(name=name, kind=info['kind'], signature=self.signature(name), line=info['start'],
                         doc=[line for line in docs if not line.lower().startswith('why:')], why=why, footprint=self.phrase(summary),
                         callers=sorted(n for n, targets in self.graph.items() if name in targets))
            modules[info['file']]['functions'].append(entry)
            if not docs and info['kind'] == 'fn' and name != 'main' and not name.startswith('test_'): undocumented.append(name)
            for callee in self.graph.get(name, []):
                target = self.info[callee]['file']
                if target != info['file'] and target in modules:
                    counts = modules[info['file']]['calls_into']
                    counts[rel(target)] = counts.get(rel(target), 0) + 1
        boundaries = []
        for name, info in self.info.items():
            if info['kind'] == 'extern':
                boundaries.append(dict(name=name, abi=info['abi'], symbol=info['symbol'], declared_in=rel(info['file']),
                                       callers=sorted(n for n, targets in self.graph.items() if name in targets)))
        entries = [n for n in self.info if n == 'main' or n.startswith('test_')]
        return dict(entry=self.entry, modules=list(modules.values()), foreign_boundaries=boundaries, entry_points=entries,
                    undocumented=undocumented, guarantees={n: i['guarantees'] for n, i in self.info.items() if i['guarantees']})

    # ------------------------------------------------------------ views
    def function_at(self, file, line):
        for info in self.info.values():
            if info['kind'] == 'fn' and info['file'] == file and info['start'] <= line <= info['end']:
                return info
        return None

    def signature(self, name):
        i = self.info[name]
        text = f'fn {name}(' + ', '.join(f'{p}: {t}' for p, t in i['params']) + f') -> {i["result"]}'
        if i['kind'] == 'extern':
            declared = [e for e in i['effects'] if not (i['abi'] != 'c' and e == 'io')]
            if declared: text += ' ![' + ', '.join(declared) + ']'
        elif i['effects']:
            text += ' ![' + ', '.join(i['effects']) + ']'
        if i['guarantees']: text += ' ' + ' '.join(i['guarantees'])
        return text

    def lens(self, line, file=None):
        file = str(pathlib.Path(file).resolve()) if file else self.entry
        function = self.function_at(file, line)
        source_lines = pathlib.Path(file).read_text(encoding='utf-8').splitlines()
        result = dict(file=file, line=line, source=source_lines[line - 1] if 0 < line <= len(source_lines) else '')
        if not function:
            result.update(function=None, facts=[], nothing_hidden=False,
                          note='this line is outside any function body (a declaration, import, comment or blank line)')
            return result
        facts = [f for f in self.facts.get(function['name'], []) if f['line'] == line and f['source'] == file]
        described = []
        for fact in facts:
            entry = dict(kind=fact['kind'], column=fact['column'])
            kind = fact['kind']
            if kind == 'call':
                callee = fact['callee']
                total = self.summary(callee)
                entry.update(callee=callee, callee_signature=self.signature(callee), callee_footprint=total,
                             message=f'calls `{callee}`: {self.phrase(total)}')
            elif kind == 'foreign' and 'callee' in fact:
                entry['message'] = f'crosses into foreign code: {fact["abi"]} `{fact["symbol"]}`'
            elif kind == 'abort':
                entry['message'] = f'can abort the program: {fact["why"]}'
            elif kind == 'alloc':
                entry['message'] = f'allocates {fact["what"]} (can abort if memory is exhausted)'
            elif kind in {'io', 'write', 'unsafe', 'loop', 'assign'}:
                entry['message'] = fact['what']
            elif kind == 'alias':
                entry['message'] = fact['what']
            elif kind == 'read':
                entry['message'] = f'reads {fact["target"]}'
            else:
                entry['message'] = kind
            described.append(entry)
        text = result['source'].strip()
        if described:
            note = None
        elif text in {'', '{', '}', '};'} or text.startswith('//'):
            note = 'no executable code on this line'
        elif text.startswith('fn ') and text.endswith('{'):
            note = 'function header: for the footprint of the whole function use `axle audit` or `axle slice --fn ' + function['name'] + '`'
        else:
            note = ('no hidden behavior: only local arithmetic, comparisons and bindings; '
                    'this line cannot allocate, abort, do I/O, call anything, touch shared storage or loop')
        result.update(function=function['name'], signature=self.signature(function['name']), facts=described,
                      nothing_hidden=not described, note=note)
        return result

    def audit(self):
        rows = []
        for name, info in self.info.items():
            s = self.summary(name)
            rows.append(dict(name=name, kind=info['kind'], signature=self.signature(name), file=info['file'], line=info['start'],
                             footprint=self.phrase(s), **{k: s[k] for k in ('effects_declared', 'effects_used', 'effects_unused', 'could_declare',
                                                                           'guarantees', 'recursive', 'may_not_terminate')},
                             counts={k: len(s[k]) for k in ('aborts', 'allocs', 'io', 'writes', 'loops', 'aliases', 'foreign', 'unsafe')}))
        findings = []
        for r in rows:
            if r['kind'] == 'fn' and r['effects_unused']:
                findings.append(dict(kind='over-declared-effects', function=r['name'], detail=f'declares {r["effects_unused"]} but nothing it calls uses them'))
            if r['could_declare']:
                findings.append(dict(kind='could-declare', function=r['name'], detail=' '.join(r['could_declare']) + ' would be accepted and checked'))
            if r['kind'] == 'fn' and r['counts']['aliases']:
                findings.append(dict(kind='shared-buffer-aliasing', function=r['name'], detail=f'{r["counts"]["aliases"]} place(s) create a second handle to shared storage'))
        for risk in self.risks():
            findings.append(dict(kind=risk['kind'], function=risk['function'], line=risk['line'], detail=risk['detail'],
                                 severity='strict' if risk['strict'] else 'advice'))
        findings.sort(key=lambda f: (f.get('severity') != 'strict', f.get('severity') != 'advice'))
        return dict(entry=self.entry, functions=rows, findings=findings)

    def slice(self, name, depth=0):
        if name not in self.info:
            raise axle.Error('unknown function ' + name, hint='run `axle api` to list functions',
                             suggestion=axle.did_you_mean(name, self.info))
        seen, order = set(), []

        def visit(n, d):
            if n in seen: return
            seen.add(n)
            order.append((n, d))
            if d < depth:
                for callee in self.graph.get(n, []):
                    if self.info[callee]['kind'] == 'fn': visit(callee, d + 1)
        visit(name, 0)
        bodies = []
        for n, d in order:
            info = self.info[n]
            lines = pathlib.Path(info['file']).read_text(encoding='utf-8').splitlines()
            start = info['start']
            while start > 1 and lines[start - 2].lstrip().startswith('//'): start -= 1
            bodies.append(dict(name=n, file=info['file'], start=start, end=info['end'], depth=d,
                               source='\n'.join(lines[start - 1:info['end']])))
        context = []
        for callee in self.graph.get(name, []):
            context.append(dict(name=callee, signature=self.signature(callee), kind=self.info[callee]['kind'],
                                footprint=self.phrase(self.summary(callee)), file=self.info[callee]['file'], line=self.info[callee]['start']))
        callers = sorted(n for n, targets in self.graph.items() if name in targets)
        docs = self.docs(name) if self.info[name]['kind'] == 'fn' else []
        return dict(function=name, signature=self.signature(name), footprint=self.phrase(self.summary(name)),
                    doc=docs, bodies=bodies, callees=context, callers=callers, depth=depth)


# ---------------------------------------------------------------- text rendering

def render_lens(r):
    head = f'{r["file"]}:{r["line"]}'
    if r['function'] is None:
        return f'{head}\n  {r["note"]}'
    out = [f'{head}  in {r["signature"]}', '  ' + r['source'].strip()]
    for fact in r['facts']:
        out.append('  - ' + fact['message'])
    if r['nothing_hidden']:
        out.append('  ' + r['note'])
    return '\n'.join(out)


def render_audit(a):
    out = []
    for r in a['functions']:
        out.append(f'{r["signature"]}')
        out.append(f'    {r["footprint"]}')
        if r['effects_unused']: out.append(f'    declared but unused effects: {r["effects_unused"]}')
        if r['could_declare']: out.append('    could also declare: ' + ' '.join(r['could_declare']))
    if a['findings']:
        out.append('')
        out.append('findings:')
        for f in a['findings']:
            where = f' (line {f["line"]})' if f.get('line') else ''
            tag = ' !' if f.get('severity') == 'strict' else ''
            out.append(f'  [{f["kind"]}]{tag} {f["function"]}{where}: {f["detail"]}')
    return '\n'.join(out)


def render_map(m):
    out = [f'# Architecture of {pathlib.Path(m["entry"]).name}', '']
    if m['entry_points']: out.append('Entry points: ' + ', '.join(m['entry_points']) + '\n')
    for mod in m['modules']:
        out.append(f'## {mod["path"]}')
        out.append(' '.join(mod['purpose']) if mod['purpose'] else '_No `//!` purpose line._')
        if mod['imports']: out.append('Imports: ' + ', '.join(mod['imports']))
        if mod['calls_into']: out.append('Calls into: ' + ', '.join(f'{k} ({v} call sites)' for k, v in sorted(mod['calls_into'].items())))
        out.append('')
        for f in mod['functions']:
            out.append(f'- `{f["signature"]}`')
            if f['doc']: out.append('    ' + ' '.join(f['doc']))
            for reason in f['why']: out.append(f'    Why: {reason}')
            out.append(f'    Footprint: {f["footprint"]}')
            if f['callers']: out.append('    Called by: ' + ', '.join(f['callers']))
        out.append('')
    if m['foreign_boundaries']:
        out.append('## Foreign boundaries')
        for b in m['foreign_boundaries']:
            out.append(f'- `{b["name"]}` -> {b["abi"]} `{b["symbol"]}` (declared in {b["declared_in"]}; used by {", ".join(b["callers"]) or "nothing"})')
        out.append('')
    if m['guarantees']:
        out.append('## Proven guarantees')
        out += [f'- `{n}`: {" ".join(g)}' for n, g in sorted(m['guarantees'].items())]
        out.append('')
    if m['undocumented']:
        out.append('## Documentation gaps')
        out.append('No `///` comment: ' + ', '.join(m['undocumented']) + '. Add what each is for, and `/// why: ...` for any non-obvious decision.')
    return '\n'.join(out).rstrip()


def render_slice(s):
    out = [f'# context for `{s["function"]}`', f'signature: {s["signature"]}', f'footprint: {s["footprint"]}']
    out += [f'purpose: {line}' for line in s.get('doc', []) if not line.lower().startswith('why:')]
    out += [f'why: {line[4:].strip()}' for line in s.get('doc', []) if line.lower().startswith('why:')]
    out.append('')
    for body in s['bodies']:
        out.append(f'## {body["name"]}  ({body["file"]}:{body["start"]}-{body["end"]})')
        out.append(body['source'])
        out.append('')
    if s['callees']:
        out.append('## callees (signature and total footprint; bodies omitted)')
        shown = {b['name'] for b in s['bodies']}
        for c in s['callees']:
            if c['name'] not in shown:
                out.append(f'- {c["signature"]}\n    {c["footprint"]}')
        out.append('')
    out.append('## called by: ' + (', '.join(s['callers']) if s['callers'] else '(nothing; entry point or unused)'))
    return '\n'.join(out)


def run(command, options):
    analysis = Analysis(options.source)
    if command == 'lens':
        if options.line is None:
            raise axle.Error('lens requires --line N', hint='example: axle lens main.axl --line 12')
        result = analysis.lens(options.line, options.file)
        print(json.dumps(result, indent=2) if options.json else render_lens(result))
    elif command == 'map':
        result = analysis.architecture()
        print(json.dumps(result, indent=2) if options.json else render_map(result))
    elif command == 'audit':
        result = analysis.audit()
        print(json.dumps(result, indent=2) if options.json else render_audit(result))
    else:
        if not options.fn:
            raise axle.Error('slice requires --fn NAME', hint='example: axle slice main.axl --fn helper')
        result = analysis.slice(options.fn, options.depth)
        print(json.dumps(result, indent=2) if options.json else render_slice(result))
    return 0


def brief_file(path):
    analysis = Analysis(path)
    architecture = analysis.architecture()
    audit = analysis.audit()
    modules = []
    count = 0
    for module in architecture['modules']:
        functions = []
        for function in module['functions']:
            count += 1
            functions.append(dict(signature=function['signature'], line=function['line'], doc=(function.get('doc') or [''])[0],
                                  why=function.get('why') or []))
        modules.append(dict(path=module['path'], purpose=(module.get('purpose') or [''])[0], functions=functions))
    order = {'unimplemented': 0, 'silent-failure': 1}
    findings = sorted(audit['findings'], key=lambda f: (order.get(f['kind'], 2), f['function']))
    return dict(entry=architecture['entry'], entry_points=architecture.get('entry_points', []), module_count=len(modules), function_count=count,
                modules=modules, attention=findings[:12], attention_total=len(findings),
                guarantees=architecture.get('guarantees', []), doc_gaps=architecture.get('doc_gaps', []))


def brief_directory(root):
    import adopt
    import tempfile
    with tempfile.TemporaryDirectory(prefix='axle-brief-') as temp:
        report = adopt.run(root, temp)
    reasons = {}
    for item in report['skipped']:
        key = item['reason'].split(' (')[0]
        if 'parameter type' in key or "parameter '" in key:
            key = key.split('`')[0].split("'")[0].strip() + ' ...'
        reasons[key[:70]] = reasons.get(key[:70], 0) + 1
    top = sorted(reasons.items(), key=lambda kv: -kv[1])[:5]
    return dict(root=str(root), files_scanned=report['files_scanned'], bound=report['bound'], skipped=report['skipped_count'],
                by_language=report['bound_by_language'], callable=[b['signature'] for b in report['bindings'][:25]],
                top_skip_reasons=[dict(reason=k, count=v) for k, v in top],
                legacy=[dict(file=l['file'], program=l['program_id'], paragraphs=len(l['paragraphs']),
                             undocumented=len(l['undocumented_paragraphs'])) for l in report['legacy']])


def render_brief(result):
    if 'modules' not in result:
        out = [f'# Brief: {result["root"]}', '',
               f'Scanned {result["files_scanned"]} files. AXLE can call {result["bound"]} functions today and skipped {result["skipped"]}.']
        if result['by_language']: out.append('Callable by language: ' + ', '.join(f'{n} {k}' for k, n in sorted(result['by_language'].items())) + '.')
        if result['callable']:
            out += ['', '## Callable now'] + [f'- `{sig}`' for sig in result['callable']]
        if result['top_skip_reasons']:
            out += ['', '## Why the rest was skipped (most common first)'] + [f'- {r["count"]} x {r["reason"]}' for r in result['top_skip_reasons']]
        if result['legacy']:
            out += ['', '## Legacy programs (structure only, not callable)']
            out += [f'- {l["program"] or l["file"]}: {l["paragraphs"]} paragraphs, {l["undocumented"]} without a comment' for l in result['legacy']]
        out += ['', '## Next', '- `axle adopt DIR` writes `.axle/bindings.axl` and `.axle/axle.map.json`; import the bindings from your .axl file.',
                '- `axle characterize DIR` records current behavior as tests before you change anything.',
                '- Add type annotations (`int`, `str`, `float`, `bool`) in the source to make skipped Python functions callable.']
        return '\n'.join(out)
    out = [f'# Brief: {pathlib.Path(result["entry"]).name}', '',
           f'{result["module_count"]} module(s), {result["function_count"]} function(s). Entry points: {", ".join(result["entry_points"]) or "none (library)"}.']
    for module in result['modules']:
        out += ['', f'## {module["path"]}']
        if module['purpose']: out.append(module['purpose'])
        for function in module['functions']:
            line = f'- `{function["signature"]}` (line {function["line"]})'
            if function['doc']: line += ' - ' + function['doc']
            out.append(line)
            out += [f'    why: {w}' for w in function['why']]
    if result['guarantees']:
        out += ['', '## Proven guarantees'] + [f'- {g}' if isinstance(g, str) else f'- {g}' for g in result['guarantees']]
    out += ['', f'## Needs attention ({result["attention_total"]})']
    out += [f'- [{f["kind"]}] {f["function"]}: {f["detail"]}' for f in result['attention']] or ['- nothing flagged']
    if result['doc_gaps']:
        out += ['', 'No `///` comment: ' + ', '.join(result['doc_gaps'][:15])]
    out += ['', '## Work here', '- `axle slice FILE --fn NAME` before changing a function; `axle lens FILE --line N` before trusting a line.',
            '- `axle check FILE --json` after every edit; errors carry a fix hint.', '- `axle test FILE` runs `fn test_*` functions; `axle audit FILE` lists what to tighten.']
    return '\n'.join(out)


def brief(options):
    source = pathlib.Path(options.source)
    result = brief_directory(source) if source.is_dir() else brief_file(source)
    print(json.dumps(result, indent=2) if options.json else render_brief(result))
    return 0


def enforce_strict(path):
    """Raise E080 if any strict finding exists: patterns that hide a failure instead of surfacing it."""
    analysis = Analysis(path)
    strict = [r for r in analysis.risks() if r['strict']]
    if strict:
        first = strict[0]
        more = f' (and {len(strict) - 1} more; run `axle audit` to see all)' if len(strict) > 1 else ''
        token = axle.Token('', first['line'], 1, analysis.info[first['function']]['file'])
        raise axle.Error(f'strict: {first["kind"]} in fn {first["function"]}: {first["detail"]}{more}', token,
                         hint='make the failure explicit with assert(...) or abort(); do not return invented values')
