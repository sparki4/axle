"""axle adopt: scan an existing codebase and generate typed AXLE bindings.

The scanner reads Python, JavaScript (CommonJS or .mjs), C and Rust FFI sources,
finds the functions AXLE can call today (64-bit integers, booleans, unit, and the
C integer widths), and writes two files:

  bindings.axl   importable extern declarations (plus safe wrappers for C)
  axle.map.json  a machine-readable map of everything found and everything skipped

Nothing in the scanned project is modified.
"""
import ast
import json
import keyword
import os
import pathlib
import re

SKIP_DIRS = {'.git', 'node_modules', '__pycache__', 'venv', '.venv', 'env', 'target', 'build', 'dist',
             '.axle', 'site-packages', '.tox', '.mypy_cache', '.pytest_cache', 'vendor'}
MAX_FILES = 5000
MAX_BYTES = 2_000_000

RESERVED = {'i64', 'u64', 'bool', 'unit', 'buf_u64', 'import', 'fn', 'let', 'var', 'return', 'if', 'else', 'while',
            'unsafe', 'true', 'false', 'io', 'mem', 'extern', 'link', 'native', 'break', 'continue', 'main',
            'c_int', 'c_uint', 'c_long', 'c_ulong', 'c_size',
            'print', 'assert', 'addr', 'load', 'store', 'arg_u64', 'alloc_u64', 'free_u64', 'offset', 'buf_new',
            'buf_iota', 'buf_len', 'buf_get', 'buf_set', 'buf_sum', 'black_box', 'poly_exec'}

C_SCALARS = {
    'int': 'c_int', 'signed': 'c_int', 'signed int': 'c_int', 'unsigned': 'c_uint', 'unsigned int': 'c_uint',
    'long': 'c_long', 'long int': 'c_long', 'signed long': 'c_long', 'unsigned long': 'c_ulong',
    'unsigned long int': 'c_ulong', 'long long': 'i64', 'long long int': 'i64', 'signed long long': 'i64',
    'int64_t': 'i64', 'unsigned long long': 'u64', 'unsigned long long int': 'u64', 'uint64_t': 'u64',
    'size_t': 'c_size', 'bool': 'bool', '_Bool': 'bool', 'void': 'unit',
}
C_POINTEES = {'int64_t': '*i64', 'long long': '*i64', 'long long int': '*i64', 'uint64_t': '*u64',
              'unsigned long long': '*u64', 'unsigned long long int': '*u64', 'bool': '*bool', '_Bool': '*bool'}
RUST_SCALARS = {'i64': 'i64', 'u64': 'u64', 'bool': 'bool', 'i32': 'c_int', 'u32': 'c_uint', 'usize': 'c_size',
                'c_int': 'c_int', 'c_uint': 'c_uint', 'c_long': 'c_long', 'c_ulong': 'c_ulong', 'c_longlong': 'i64',
                'c_ulonglong': 'u64'}
JS_SCALARS = {'number': 'i64', 'bigint': 'i64', 'boolean': 'bool', 'void': 'unit', 'undefined': 'unit', 'null': 'unit'}


class Found:
    def __init__(self, lang, file, name, params, result, **extra):
        self.lang, self.file, self.name, self.params, self.result = lang, file, name, params, result
        self.extra = extra


def ident(name, fallback):
    name = re.sub(r'\W', '_', name or '') or fallback
    if name[0].isdigit() or name in RESERVED or keyword.iskeyword(name):
        name = 'p_' + name
    return name


def read(path):
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('file larger than 2 MB')
    return path.read_text(encoding='utf-8')


# ---------------------------------------------------------------- Python

def scan_python(path, rel, found, skipped):
    try:
        tree = ast.parse(read(path))
    except (SyntaxError, ValueError, UnicodeError, OSError) as error:
        skipped.append(dict(language='py', file=rel, name=None, reason=f'cannot parse: {error}'))
        return
    package_root, module = None, None
    chain, directory = [path.stem], path.parent
    while (directory / '__init__.py').is_file():
        chain.insert(0, directory.name)
        package_root = directory.parent
        directory = directory.parent
    if package_root is not None:
        module = '.'.join(chain)
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef):
            skipped.append(dict(language='py', file=rel, name=node.name, reason='async functions are not supported by the Python bridge'))
            continue
        if not isinstance(node, ast.FunctionDef) or node.name.startswith('_'):
            continue
        reason, params = None, []
        args = node.args
        if args.vararg or args.kwarg or args.kwonlyargs:
            reason = 'uses *args, **kwargs or keyword-only parameters'
        positional = args.posonlyargs + args.args
        defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
        for arg, default in zip(positional, defaults):
            typ = {'int': 'i64', 'bool': 'bool'}.get(ast.unparse(arg.annotation)) if arg.annotation else None
            if typ is None and arg.annotation is None and isinstance(default, ast.Constant):
                typ = 'bool' if isinstance(default.value, bool) else 'i64' if isinstance(default.value, int) else None
            if typ is None:
                reason = reason or f'parameter {arg.arg!r} needs an `int` or `bool` annotation'
            params.append((arg.arg, typ))
        if node.returns is not None:
            result = {'int': 'i64', 'bool': 'bool', 'None': 'unit'}.get(ast.unparse(node.returns))
            if result is None:
                reason = reason or f'return type {ast.unparse(node.returns)} is not int, bool or None'
        else:
            returns_value = any(isinstance(n, ast.Return) and n.value is not None for n in ast.walk(node))
            result = None if returns_value else 'unit'
            if result is None:
                reason = reason or 'needs a return annotation (-> int, -> bool or -> None)'
        if reason:
            skipped.append(dict(language='py', file=rel, name=node.name, reason=reason))
        else:
            found.append(Found('py', rel, node.name, params, result, module=module,
                               package_root=package_root and str(package_root)))


# ---------------------------------------------------------------- JavaScript

def js_param_list(raw):
    names = []
    for part in [p.strip() for p in raw.split(',') if p.strip()]:
        if part.startswith(('...', '{', '[')):
            return None
        names.append(part.split('=')[0].strip())
    return names


def scan_js(path, rel, found, skipped):
    try:
        text = read(path)
    except (ValueError, UnicodeError, OSError) as error:
        skipped.append(dict(language='js', file=rel, name=None, reason=f'cannot read: {error}'))
        return
    names = {}
    for m in re.finditer(r'(?:module\.)?exports\.(\w+)\s*=', text):
        names[m[1]] = m.start()
    for m in re.finditer(r'module\.exports\s*=\s*\{([^}]*)\}', text):
        for item in m[1].split(','):
            key = item.split(':')[0].strip()
            if re.fullmatch(r'\w+', key):
                names.setdefault(key, None)
    esm = {m[1] for m in re.finditer(r'export\s+(?:async\s+)?function\s+(\w+)', text)}
    esm |= {m[1] for m in re.finditer(r'export\s+(?:const|let|var)\s+(\w+)\s*=', text)}
    if esm and path.suffix != '.mjs':
        for name in sorted(esm):
            skipped.append(dict(language='js', file=rel, name=name, reason='ES module syntax in a .js file; rename to .mjs or use CommonJS exports'))
        esm = set()
    for name in sorted(set(names) | esm):
        patterns = [
            rf'(?:export\s+)?(?:async\s+)?function\s*\*?\s*{name}\s*\((?P<p>[^)]*)\)',
            rf'(?:(?:export\s+)?(?:const|let|var)\s+|(?:module\.)?exports\.){name}\s*=\s*(?:async\s+)?'
            rf'(?:function\s*\*?\s*\w*\s*\((?P<p>[^)]*)\)|\((?P<q>[^)]*)\)\s*=>|(?P<r>\w+)\s*=>)',
        ]
        match = next((m for m in (re.search(p, text) for p in patterns) if m), None)
        if not match:
            skipped.append(dict(language='js', file=rel, name=name, reason='definition not found in this file (re-exported or computed)'))
            continue
        groups = match.groupdict()
        raw = groups.get('p') or groups.get('q') or groups.get('r') or ''
        params_names = js_param_list(raw)
        if params_names is None:
            skipped.append(dict(language='js', file=rel, name=name, reason='destructured or rest parameters'))
            continue
        before = text[:match.start()].rstrip()
        doc = ''
        if before.endswith('*/') and before.rfind('/**') != -1:
            doc = before[before.rfind('/**'):]
        types = {m[2]: m[1] for m in re.finditer(r'@param\s+\{([^}]+)\}\s+\[?(\w+)', doc)}
        ret = re.search(r'@returns?\s+\{([^}]+)\}', doc)

        def conv(t):
            t = re.sub(r'^Promise<(.+)>$', r'\1', (t or '').strip())
            return JS_SCALARS.get(t)
        params, reason = [], None
        for p in params_names:
            typ = conv(types.get(p))
            if typ is None or typ == 'unit':
                reason = reason or f'parameter {p!r} needs a JSDoc `@param {{number}}` or `{{boolean}}` type'
            params.append((p, typ))
        result = conv(ret[1]) if ret else None
        if result is None:
            reason = reason or 'needs a JSDoc `@returns {number|boolean|void}` type'
        if reason:
            skipped.append(dict(language='js', file=rel, name=name, reason=reason))
        else:
            found.append(Found('js', rel, name, params, result))


# ---------------------------------------------------------------- C

def strip_c(text):
    text = re.sub(r'/\*.*?\*/', ' ', text, flags=re.S)
    text = re.sub(r'//[^\n]*', ' ', text)
    return re.sub(r'(?m)^\s*#(?:[^\n\\]|\\\n|\\.)*', ' ', text)


def c_chunks(text):
    """Yield (head, is_definition) for each top-level declaration."""
    depth, buffer, i = 0, [], 0
    while i < len(text):
        ch = text[i]
        if ch == '{':
            if depth == 0:
                yield ''.join(buffer).strip(), True
                buffer = []
            depth += 1
        elif ch == '}':
            depth = max(0, depth - 1)
        elif depth == 0:
            if ch == ';':
                yield ''.join(buffer).strip(), False
                buffer = []
            else:
                buffer.append(ch)
        i += 1


def c_type(words, stars, pointer_ok=True):
    base = ' '.join(w for w in words if w not in {'const', 'volatile', 'extern', 'restrict', '__restrict'})
    if stars == 0:
        return C_SCALARS.get(base)
    if stars == 1 and pointer_ok:
        return C_POINTEES.get(base)
    return None


def parse_c_prototype(head):
    m = re.fullmatch(r'(?P<ret>[\w\s\*]+?)\s*\b(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^()]*)\)\s*', head, re.S)
    if not m:
        return None
    ret_tokens = re.findall(r'\w+|\*', m['ret'])
    words = [t for t in ret_tokens if t != '*']
    if not words or 'static' in words or 'inline' in words or '__inline' in words or 'typedef' in words:
        return ('skip', m['name'], 'static/inline/typedef declarations have no linkable symbol')
    result = c_type(words, ret_tokens.count('*'))
    params, reason = [], None
    raw = m['params'].strip()
    if raw not in {'', 'void'}:
        for index, part in enumerate(raw.split(',')):
            tokens = re.findall(r'\w+|\*|\.\.\.', part)
            if '...' in tokens:
                reason = reason or 'variadic function'
                continue
            stars = tokens.count('*')
            words = [t for t in tokens if t != '*']
            plain = [w for w in words if w not in {'const', 'volatile', 'restrict', '__restrict'}]
            name = None
            if len(plain) > 1 and ' '.join(plain) not in C_SCALARS and ' '.join(plain) not in C_POINTEES:
                name, words = plain[-1], plain[:-1]
            typ = c_type(words, stars)
            if typ is None or typ == 'unit':
                reason = reason or f'parameter type `{part.strip()}` has no AXLE equivalent yet (strings, structs, floats, void* are not supported)'
            params.append((ident(name, f'arg{index}'), typ))
    if result is None:
        reason = reason or f'return type `{m["ret"].strip()}` has no AXLE equivalent yet'
    return ('ok', m['name'], reason, params, result)


def scan_c(path, rel, found, skipped, defined_in, header_protos):
    try:
        text = strip_c(read(path))
    except (ValueError, UnicodeError, OSError) as error:
        skipped.append(dict(language='c', file=rel, name=None, reason=f'cannot read: {error}'))
        return
    is_header = path.suffix in {'.h', '.hpp'}
    if not is_header and re.search(r'\bint\s+main\s*\(', text):
        skipped.append(dict(language='c', file=rel, name='main', reason='file defines main(); it is a program, not a library'))
        return
    for head, is_def in c_chunks(text):
        if not head or '(' not in head or head.lstrip().startswith(('struct', 'enum', 'union', 'typedef')):
            continue
        if is_def and is_header:
            continue
        parsed = parse_c_prototype(re.sub(r'\s+', ' ', head))
        if not parsed:
            continue
        if parsed[0] == 'skip':
            continue
        _, name, reason, *rest = parsed
        if reason:
            skipped.append(dict(language='c', file=rel, name=name, reason=reason))
            continue
        params, result = rest
        entry = Found('c', rel, name, params, result)
        if is_def and not is_header:
            defined_in[name] = rel
            found.append(entry)
        elif is_header:
            header_protos.append(entry)


# ---------------------------------------------------------------- Rust

def scan_rust(path, rel, found, skipped):
    try:
        text = read(path)
    except (ValueError, UnicodeError, OSError) as error:
        skipped.append(dict(language='rust', file=rel, name=None, reason=f'cannot read: {error}'))
        return
    pattern = re.compile(r'#\[(?:unsafe\()?no_mangle\)?\]\s*(?:#\[[^\]]*\]\s*)*pub\s+(?:unsafe\s+)?extern\s+"C"\s+fn\s+(\w+)\s*\(([^)]*)\)\s*(?:->\s*([^{]+?))?\s*\{')
    for m in pattern.finditer(text):
        name, raw, ret = m[1], m[2], (m[3] or '').strip()

        def conv(t):
            t = t.strip().removeprefix('std::ffi::').removeprefix('core::ffi::').removeprefix('libc::')
            pointer = re.fullmatch(r'\*(?:mut|const)\s+(i64|u64|bool)', t)
            return '*' + pointer[1] if pointer else RUST_SCALARS.get(t)
        params, reason = [], None
        for index, part in enumerate(p for p in raw.split(',') if p.strip()):
            pname, _, ptype = part.partition(':')
            typ = conv(ptype)
            if typ is None:
                reason = reason or f'parameter type `{ptype.strip()}` has no AXLE equivalent yet'
            params.append((ident(pname.strip().lstrip('_'), f'arg{index}'), typ))
        result = conv(ret) if ret else 'unit'
        if result is None:
            reason = reason or f'return type `{ret}` has no AXLE equivalent yet'
        if reason:
            skipped.append(dict(language='rust', file=rel, name=name, reason=reason))
        else:
            found.append(Found('rust', rel, name, params, result))


# ---------------------------------------------------------------- driver

def walk(root):
    count = 0
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith('.'))
        for name in sorted(files):
            count += 1
            if count > MAX_FILES:
                return
            yield pathlib.Path(current) / name


def rel_posix(target, base):
    return pathlib.Path(os.path.relpath(target, base)).as_posix()


VISIBLE = {'c_int': 'i64', 'c_long': 'i64', 'c_uint': 'u64', 'c_ulong': 'u64', 'c_size': 'u64'}


def visible_sig(name, params, result, effects=None):
    return signature(name, [(p, VISIBLE.get(t, t)) for p, t in params], VISIBLE.get(result, result), effects)


def signature(name, params, result, effects=None):
    text = f'fn {name}({", ".join(f"{p}: {t}" for p, t in params)}) -> {result}'
    return text + (f' ![{", ".join(effects)}]' if effects else '')


def run(root, out_dir=None):
    root = pathlib.Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f'{root} is not a directory')
    out_dir = pathlib.Path(out_dir).resolve() if out_dir else root / '.axle'
    out_dir.mkdir(parents=True, exist_ok=True)
    found, skipped, defined_in, header_protos, scanned = [], [], {}, [], 0
    for path in walk(root):
        try:
            path.relative_to(out_dir)
            continue
        except ValueError:
            pass
        rel, suffix = rel_posix(path, root), path.suffix
        handler = None
        if suffix == '.py':
            handler = lambda: scan_python(path, rel, found, skipped)
        elif suffix in {'.js', '.mjs', '.cjs'}:
            handler = lambda: scan_js(path, rel, found, skipped)
        elif suffix in {'.c', '.h'}:
            handler = lambda: scan_c(path, rel, found, skipped, defined_in, header_protos)
        elif suffix == '.rs':
            handler = lambda: scan_rust(path, rel, found, skipped)
        if handler:
            scanned += 1
            handler()
    for proto in header_protos:
        if proto.name not in defined_in:
            proto.extra['needs_link'] = True
            found.append(proto)

    used, lines, entries = set(RESERVED), [], []
    natives = []
    path_ok = lambda p: not re.search(r'[\s@"\\]', p)
    for item in found:
        file_path = root / item.file
        target = rel_posix(file_path, out_dir)
        if item.lang in {'py', 'js'} and not path_ok(target):
            skipped.append(dict(language=item.lang, file=item.file, name=item.name, reason='path contains spaces, quotes or "@"'))
            continue
        if item.lang == 'c' and not item.extra.get('needs_link'):
            native = rel_posix(root / defined_in[item.name], out_dir)
            if not path_ok(native) or native.startswith('/') or ':' in native:
                skipped.append(dict(language='c', file=item.file, name=item.name, reason='path cannot be expressed as a relative native path'))
                continue
            if native not in natives:
                natives.append(native)
        local = item.name if item.name not in used else f'{item.lang}_{item.name}'
        counter = 2
        while local in used:
            local = f'{item.lang}_{item.name}_{counter}'
            counter += 1
        used.add(local)
        alias = f' = "{item.name}"' if local != item.name and item.lang != 'c' else ''
        params = item.params
        entry = dict(name=local, foreign_name=item.name, language=item.lang, file=item.file, status='bound')
        if item.lang in {'py', 'js'}:
            module = item.extra.get('module')
            if module:
                spec = f'{item.lang}:{module}@{rel_posix(item.extra["package_root"], out_dir)}'
            else:
                spec = f'{item.lang}:{target}'
            lines.append(f'extern "{spec}" {signature(local, params, item.result)}{alias};')
            entry.update(signature=signature(local, params, item.result, ['io']), effects=['io'], call='safe')
        else:
            raw = f'c_{local}'
            used.add(raw)
            pointer = any(t.startswith('*') for _, t in params) or item.result.startswith('*')
            lines.append(f'extern "c" {signature(raw, params, item.result, ["io"])} = "{item.name}";')
            if item.extra.get('needs_link'):
                entry.update(status='needs-link', note='declaration only: add `link "<library>";` and pass --link-search, or compile the defining .c file with `native`')
            elif item.lang == 'rust':
                entry.update(status='needs-link', note='build this crate as a staticlib, then add `link "<name>";` and pass --link-search <dir>')
            if pointer:
                entry.update(name=raw, signature=visible_sig(raw, params, item.result, ['io']), effects=['io'], call='unsafe')
            else:
                args = ', '.join(p for p, _ in params)
                body = f'unsafe {{ return {raw}({args}); }}' if item.result != 'unit' else f'unsafe {{ {raw}({args}); }}'
                lines.append(f'{visible_sig(local, params, item.result, ["io"])} {{ {body} }}')
                entry.update(signature=visible_sig(local, params, item.result, ['io']), effects=['io'], call='safe')
        entries.append(entry)

    header = ['// Generated by `axle adopt`. Re-run it to refresh; edit effect lists here only to narrow them.',
              f'// Source tree: {root.name}',
              '// Safe wrappers carry ![io] because foreign code is not effect-checked by AXLE.']
    natives_lines = [f'native "{n}";' for n in natives]
    bindings = out_dir / 'bindings.axl'
    bindings.write_text('\n'.join(header + [''] + natives_lines + [''] + lines) + '\n', encoding='utf-8')
    by_lang = {}
    for e in entries:
        by_lang[e['language']] = by_lang.get(e['language'], 0) + 1
    report = {
        'tool': 'axle adopt', 'root': str(root), 'bindings_file': str(bindings), 'files_scanned': scanned,
        'bound': len(entries), 'skipped_count': len(skipped), 'bound_by_language': by_lang,
        'how_to_use': [f'import "{rel_posix(bindings, root)}";   // path relative to your .axl file', 'declare ![io] on callers',
                       'call `call: safe` entries directly; `call: unsafe` entries need an unsafe block'],
        'supported_types': 'i64, u64, bool, unit; C widths via c_int/c_uint/c_long/c_ulong/c_size; *i64/*u64/*bool pointers (C only)',
        'bindings': entries, 'skipped': skipped,
    }
    (out_dir / 'axle.map.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report
