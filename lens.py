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

import axle

BUCKETS = {'abort': 'aborts', 'alloc': 'allocs', 'io': 'io', 'write': 'writes', 'loop': 'loops', 'alias': 'aliases'}


class Analysis:
    def __init__(self, path):
        self.entry = str(pathlib.Path(path).resolve())
        self.loaded = axle.load_program(path)
        loaded = self.loaded
        self.compiler = axle.Compiler(functions=loaded.functions, externs=loaded.externs, links=loaded.links,
                                      natives=loaded.natives, guarantees=loaded.guarantees)
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
        return dict(function=name, signature=self.signature(name), footprint=self.phrase(self.summary(name)),
                    bodies=bodies, callees=context, callers=callers, depth=depth)


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
            out.append(f'  [{f["kind"]}] {f["function"]}: {f["detail"]}')
    return '\n'.join(out)


def render_slice(s):
    out = [f'# context for `{s["function"]}`', f'signature: {s["signature"]}', f'footprint: {s["footprint"]}', '']
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
    elif command == 'audit':
        result = analysis.audit()
        print(json.dumps(result, indent=2) if options.json else render_audit(result))
    else:
        if not options.fn:
            raise axle.Error('slice requires --fn NAME', hint='example: axle slice main.axl --fn helper')
        result = analysis.slice(options.fn, options.depth)
        print(json.dumps(result, indent=2) if options.json else render_slice(result))
    return 0
