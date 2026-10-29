#!/usr/bin/env python3
"""AXLE 0.2: a typed, effect-checked native language. Python 3.10+, Rust 1.82+."""
import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

VERSION = '0.2.0'
TYPES = {'i64', 'u64', 'bool', 'unit', 'buf_u64'}

class Error(Exception):
    def __init__(self, message, token=None):
        self.message = message
        self.line = token.line if token else 1
        self.column = token.column if token else 1
        self.source = token.source if token else None
        super().__init__(message)

@dataclass
class Token:
    value: str
    line: int
    column: int
    source: str | None = None

def lex(source, source_name=None):
    pattern = re.compile(r'\s+|//[^\n]*|"[^"\n]*"|[0-9]+(?:u64|i64)?|[A-Za-z_][A-Za-z_0-9]*|->|==|!=|<=|>=|&&|\|\||<<|>>|[{}()\[\],:;!+*/%<>=&|^\-]')
    tokens, pos, line, column = [], 0, 1, 1
    while pos < len(source):
        match = pattern.match(source, pos)
        if not match:
            raise Error('unexpected character ' + repr(source[pos]), Token('', line, column, source_name))
        value = match.group()
        if not value.isspace() and not value.startswith('//'):
            tokens.append(Token(value, line, column, source_name))
        if '\n' in value:
            line += value.count('\n')
            column = len(value.rsplit('\n', 1)[1]) + 1
        else:
            column += len(value)
        pos = match.end()
    tokens.append(Token('<eof>', line, column, source_name))
    return tokens

RESERVED = TYPES | {'import', 'fn', 'let', 'var', 'return', 'if', 'else', 'while', 'unsafe', 'true', 'false', 'io', 'mem'}
BUILTINS = {'print', 'assert', 'addr', 'load', 'store', 'arg_u64', 'alloc_u64', 'free_u64', 'offset',
            'buf_new', 'buf_iota', 'buf_len', 'buf_get', 'buf_set', 'buf_sum', 'black_box', 'poly_exec'}

class Parser:
    def __init__(self, source, source_name=None):
        self.tokens, self.i = lex(source, source_name), 0
        self.imports = []
    def peek(self):
        return self.tokens[self.i].value
    def pop(self):
        token = self.tokens[self.i]
        if token.value != '<eof>':
            self.i += 1
        return token
    def take(self, value):
        if self.peek() == value:
            self.pop()
            return True
        return False
    def need(self, value):
        token = self.pop()
        if token.value != value:
            raise Error(f'expected {value!r}, got {token.value!r}', token)
        return token
    def name(self):
        token = self.pop()
        if not re.fullmatch('[A-Za-z_][A-Za-z_0-9]*', token.value) or token.value in RESERVED:
            raise Error('expected a non-reserved identifier', token)
        return token
    def type(self):
        pointer = self.take('*')
        token = self.pop()
        if token.value not in TYPES or (pointer and token.value not in {'i64', 'u64', 'bool'}):
            raise Error('expected a value type or pointer to a scalar', token)
        return ('*' if pointer else '') + token.value
    def program(self):
        functions = []
        while self.peek() != '<eof>':
            if self.take('import'):
                path = self.pop()
                if not path.value.startswith('"') or '\\' in path.value or not path.value.endswith('.axl"'):
                    raise Error('import requires a quoted .axl path using forward slashes', path)
                self.need(';')
                self.imports.append(path)
                continue
            self.need('fn')
            name = self.name()
            self.need('(')
            params = []
            if self.peek() != ')':
                while True:
                    param = self.name()
                    self.need(':')
                    params.append((param, self.type()))
                    if not self.take(','): break
            self.need(')')
            self.need('->')
            result = self.type()
            effects = set()
            if self.take('!'):
                self.need('[')
                if self.peek() != ']':
                    while True:
                        effect = self.pop()
                        if effect.value not in {'io', 'mem'} or effect.value in effects:
                            raise Error('expected a unique io or mem effect', effect)
                        effects.add(effect.value)
                        if not self.take(','): break
                self.need(']')
            functions.append((name, params, result, effects, self.block()))
        return functions
    def block(self):
        self.need('{')
        statements = []
        while self.peek() not in {'}', '<eof>'}:
            statements.append(self.statement())
        self.need('}')
        return statements
    def statement(self):
        token = self.tokens[self.i]
        if self.peek() in {'let', 'var'}:
            mutable = self.pop().value == 'var'
            name = self.name()
            typ = self.type() if self.take(':') else None
            self.need('=')
            expr = self.expr()
            self.need(';')
            return ('bind', token, name, typ, mutable, expr)
        if self.take('return'):
            expr = None if self.peek() == ';' else self.expr()
            self.need(';')
            return ('return', token, expr)
        if self.take('if'):
            cond, yes = self.expr(), self.block()
            no = self.block() if self.take('else') else None
            return ('if', token, cond, yes, no)
        if self.take('while'):
            return ('while', token, self.expr(), self.block())
        if self.take('unsafe'):
            return ('unsafe', token, self.block())
        expr = self.expr()
        if self.take('='):
            rhs = self.expr()
            self.need(';')
            return ('assign', token, expr, rhs)
        self.need(';')
        return ('expr', token, expr)
    PRECEDENCE = {'||': 1, '&&': 2, '|': 3, '^': 4, '&': 5, '==': 6, '!=': 6,
                  '<': 7, '>': 7, '<=': 7, '>=': 7, '<<': 8, '>>': 8,
                  '+': 9, '-': 9, '*': 10, '/': 10, '%': 10}
    def expr(self, minimum=0):
        token = self.pop()
        if token.value in {'!', '-'}:
            left = ('unary', token, self.expr(11))
        elif token.value == '(':
            left = self.expr()
            self.need(')')
        elif re.fullmatch(r'[0-9]+(?:u64|i64)?', token.value):
            left = ('number', token)
        elif token.value in {'true', 'false'}:
            left = ('bool', token)
        elif re.fullmatch('[A-Za-z_][A-Za-z_0-9]*', token.value) and token.value not in RESERVED:
            if self.take('('):
                args = []
                if self.peek() != ')':
                    while True:
                        args.append(self.expr())
                        if not self.take(','): break
                self.need(')')
                left = ('call', token, args)
            else:
                left = ('name', token)
        else:
            raise Error('expected expression', token)
        while self.PRECEDENCE.get(self.peek(), -1) >= minimum:
            op = self.pop()
            left = ('binary', op, left, self.expr(self.PRECEDENCE[op.value] + 1))
        return left

def rust_type(typ):
    return '*mut ' + typ[1:] if typ.startswith('*') else '()' if typ == 'unit' else 'AxleBuffer' if typ == 'buf_u64' else typ

class Compiler:
    def __init__(self, source=None, functions=None, foreign_commands=None):
        self.foreign_commands = foreign_commands or {}
        if functions is None:
            parser = Parser(source)
            self.functions = parser.program()
            if parser.imports: raise Error('imports require compile_file or the CLI', parser.imports[0])
        else:
            self.functions = functions
        self.signatures = {}
        for name, params, result, effects, body in self.functions:
            if name.value in self.signatures or name.value in BUILTINS:
                raise Error('duplicate or reserved function name', name)
            self.signatures[name.value] = ([t for _, t in params], result, effects)
        if 'main' not in self.signatures or self.signatures['main'][:2] != ([], 'unit'):
            raise Error('program requires fn main() -> unit')
    def require(self, actual, expected, token):
        if actual != expected:
            raise Error(f'expected {expected}, got {actual}', token)
    def effect(self, effect, token):
        if effect not in self.effects:
            raise Error(f'function must declare {effect} effect', token)
    def lookup(self, token):
        for scope in reversed(self.scopes):
            if token.value in scope: return scope[token.value]
        raise Error('unknown variable ' + token.value, token)
    def expression(self, node):
        kind, token, *rest = node
        value = token.value
        if kind == 'number':
            match = re.fullmatch(r'([0-9]+)(u64|i64)?', value)
            if len(match[1]) > 20: raise Error('integer literal out of range', token)
            number, typ = int(match[1]), match[2] or 'i64'
            if number > (2**64 - 1 if typ == 'u64' else 2**63 - 1):
                raise Error('integer literal out of range', token)
            return typ, str(number) + typ
        if kind == 'bool': return 'bool', value
        if kind == 'name':
            typ, mutable = self.lookup(token)
            code = 'v_' + value
            return typ, code + '.clone()' if typ == 'buf_u64' else code
        if kind == 'unary':
            if value == '-' and rest[0][0] == 'number' and rest[0][1].value in {'9223372036854775808', '9223372036854775808i64'}:
                return 'i64', 'i64::MIN'
            typ, code = self.expression(rest[0])
            if value == '!':
                self.require(typ, 'bool', token)
                return typ, f'(!({code}))'
            if typ not in {'i64', 'u64'}: raise Error('negation requires integer', token)
            return typ, f'({code}).wrapping_neg()'
        if kind == 'binary':
            lt, lc = self.expression(rest[0])
            rt, rc = self.expression(rest[1])
            self.require(rt, lt, token)
            if value in {'&&', '||'}:
                self.require(lt, 'bool', token)
            elif value in {'==', '!='}:
                if lt in {'unit', 'buf_u64'}: raise Error(lt + ' cannot be compared', token)
            elif lt not in {'i64', 'u64'}:
                raise Error('operator requires integer operands', token)
            methods = {'+': 'wrapping_add', '-': 'wrapping_sub', '*': 'wrapping_mul',
                       '/': 'wrapping_div', '%': 'wrapping_rem', '<<': 'wrapping_shl', '>>': 'wrapping_shr'}
            if value in methods:
                # Mask before narrowing: shifts use the low six bits on both integer types.
                rhs = f'(({rc}) & 63{rt}) as u32' if value in {'<<', '>>'} else rc
                code = f'({lc}).{methods[value]}({rhs})'
            else: code = f'(({lc}) {value} ({rc}))'
            return ('bool' if value in {'&&', '||', '==', '!=', '<', '>', '<=', '>='} else lt), code
        if kind == 'call':
            args = rest[0]
            if value == 'addr':
                if len(args) != 1 or args[0][0] != 'name': raise Error('addr requires one mutable variable', token)
                if not self.unsafe: raise Error('addr requires unsafe block', token)
                self.effect('mem', token)
                typ, mutable = self.lookup(args[0][1])
                if not mutable or typ not in {'i64', 'u64', 'bool'}:
                    raise Error('addr requires a mutable scalar variable', token)
                return '*' + typ, '(&raw mut v_' + args[0][1].value + ')'
            checked = [self.expression(arg) for arg in args]
            types, codes = [x[0] for x in checked], [x[1] for x in checked]
            if value == 'poly_exec':
                self.effect('io', token)
                if types != ['u64'] or args[0][0] != 'number':
                    raise Error('poly_exec requires a literal u64 block ID', token)
                index = int(args[0][1].value.removesuffix('u64'))
                if index not in self.foreign_commands:
                    raise Error('poly_exec requires a polyglot build with a known block ID', token)
                return 'unit', self.foreign_commands[index]
            if value == 'black_box':
                if len(types) != 1 or types[0] == 'unit': raise Error('black_box requires one value', token)
                return types[0], 'std::hint::black_box(' + codes[0] + ')'
            if value.startswith('buf_') and value in BUILTINS:
                self.effect('mem', token)
                signature = {
                    'buf_new': (['u64'], 'buf_u64'), 'buf_len': (['buf_u64'], 'u64'),
                    'buf_iota': (['u64'], 'buf_u64'),
                    'buf_get': (['buf_u64', 'u64'], 'u64'),
                    'buf_set': (['buf_u64', 'u64', 'u64'], 'unit'),
                    'buf_sum': (['buf_u64'], 'u64'),
                }
                expected, result = signature[value]
                if types != expected: raise Error(f'{value} requires {expected}, got {types}', token)
                return result, 'axle_' + value + '(' + ', '.join(codes) + ')'
            if value in {'alloc_u64', 'free_u64', 'offset'}:
                if not self.unsafe: raise Error(value + ' requires unsafe block', token)
                self.effect('mem', token)
                if value == 'alloc_u64':
                    if types != ['u64']: raise Error('alloc_u64 requires a u64 count', token)
                    return '*u64', 'axle_alloc(' + codes[0] + ')'
                if value == 'free_u64':
                    if types != ['*u64', 'u64']: raise Error('free_u64 requires *u64 and original u64 count', token)
                    return 'unit', 'unsafe { axle_free(' + ', '.join(codes) + ') }'
                if len(types) != 2 or not types[0].startswith('*') or types[1] != 'u64':
                    raise Error('offset requires a typed pointer and u64 offset', token)
                return types[0], f'unsafe {{ ({codes[0]}).add(usize::try_from({codes[1]}).expect("offset too large")) }}'
            if value in {'load', 'store'}:
                if not self.unsafe: raise Error(value + ' requires unsafe block', token)
                self.effect('mem', token)
                count = 1 if value == 'load' else 2
                if len(types) != count or not types[0].startswith('*'):
                    raise Error(value + ' requires a typed pointer' + (' and value' if count == 2 else ''), token)
                if value == 'load': return types[0][1:], f'unsafe {{ *({codes[0]}) }}'
                self.require(types[1], types[0][1:], token)
                return 'unit', f'{{ let axle_pointer = {codes[0]}; let axle_value = {codes[1]}; unsafe {{ *axle_pointer = axle_value; }} }}'
            if value == 'print':
                self.effect('io', token)
                if len(types) != 1 or types[0] not in {'i64', 'u64', 'bool'}: raise Error('print requires one scalar', token)
                return 'unit', 'println!("{}", ' + codes[0] + ')'
            if value == 'assert':
                if types != ['bool']: raise Error('assert requires one bool', token)
                return 'unit', 'assert!(' + codes[0] + ')'
            if value == 'arg_u64':
                self.effect('io', token)
                if types != ['u64']: raise Error('arg_u64 requires one u64 index', token)
                return 'u64', 'axle_arg(' + codes[0] + ')'
            if value not in self.signatures: raise Error('unknown function ' + value, token)
            expected, result, effects = self.signatures[value]
            if types != expected: raise Error(f'arguments must have types {expected}, got {types}', token)
            for effect in sorted(effects): self.effect(effect, token)
            return result, 'f_' + value + '(' + ', '.join(codes) + ')'
        raise Error('invalid expression', token)
    def block(self, statements):
        self.scopes.append({})
        lines, returned = [], False
        for node in statements:
            kind, token, *rest = node
            if returned: raise Error('unreachable statement', token)
            if kind == 'bind':
                name, typ, mutable, expr = rest
                if typ == 'unit': raise Error('unit variables are not supported', name)
                if name.value in self.scopes[-1]: raise Error('duplicate variable in this scope', name)
                actual, code = self.expression(expr)
                if typ is None: typ = actual
                if typ == 'unit': raise Error('unit variables are not supported', name)
                self.require(actual, typ, name)
                self.scopes[-1][name.value] = (typ, mutable)
                lines.append(f'let {"mut " if mutable else ""}v_{name.value}: {rust_type(typ)} = {code};')
            elif kind == 'assign':
                target, rhs = rest
                if target[0] != 'name': raise Error('assignment target must be a variable', token)
                typ, mutable = self.lookup(target[1])
                if not mutable: raise Error('cannot assign to immutable variable', token)
                actual, code = self.expression(rhs)
                self.require(actual, typ, token)
                lines.append('v_' + target[1].value + ' = ' + code + ';')
            elif kind == 'return':
                typ, code = ('unit', '()') if rest[0] is None else self.expression(rest[0])
                self.require(typ, self.result, token)
                lines.append('return ' + code + ';')
                returned = True
            elif kind in {'if', 'while'}:
                typ, code = self.expression(rest[0])
                self.require(typ, 'bool', token)
                body, yes_return = self.block(rest[1])
                line = f'{kind} {code} {{\n{body}\n}}'
                if kind == 'if' and rest[2] is not None:
                    no, no_return = self.block(rest[2])
                    line += ' else {\n' + no + '\n}'
                    returned = yes_return and no_return
                lines.append(line)
            elif kind == 'unsafe':
                previous = self.unsafe
                self.unsafe = True
                body, returned = self.block(rest[0])
                self.unsafe = previous
                lines.append('{\n' + body + '\n}')
            else:
                typ, code = self.expression(rest[0])
                self.require(typ, 'unit', token)
                lines.append(code + ';')
        self.scopes.pop()
        return '\n'.join(lines), returned
    def compile(self):
        chunks = ['// Generated by AXLE ' + VERSION,
                  '#![allow(unused_parens, unused_mut, dead_code, unused_variables)]',
                  'type AxleBuffer = std::rc::Rc<std::cell::RefCell<Vec<u64>>>;',
                  'fn axle_buf_new(count: u64) -> AxleBuffer { std::rc::Rc::new(std::cell::RefCell::new(vec![0; usize::try_from(count).expect("buffer too large")])) }',
                  'fn axle_buf_iota(count: u64) -> AxleBuffer { let _ = usize::try_from(count).expect("buffer too large"); std::rc::Rc::new(std::cell::RefCell::new((0..count).collect())) }',
                  'fn axle_buf_len(data: AxleBuffer) -> u64 { data.borrow().len() as u64 }',
                  'fn axle_buf_get(data: AxleBuffer, index: u64) -> u64 { data.borrow()[usize::try_from(index).expect("index too large")] }',
                  'fn axle_buf_set(data: AxleBuffer, index: u64, value: u64) { data.borrow_mut()[usize::try_from(index).expect("index too large")] = value; }',
                  'fn axle_buf_sum(data: AxleBuffer) -> u64 { data.borrow().iter().fold(0u64, |a, &b| a.wrapping_add(b)) }',
                  'fn axle_arg(index: u64) -> u64 { let i = usize::try_from(index).expect("argument index too large"); std::env::args().nth(i).expect("missing argument").parse().expect("argument must be u64") }',
                  'fn axle_layout(count: u64) -> std::alloc::Layout { assert!(count > 0, "allocation count must be positive"); std::alloc::Layout::array::<u64>(usize::try_from(count).expect("allocation too large")).expect("allocation too large") }',
                  'fn axle_alloc(count: u64) -> *mut u64 { let layout = axle_layout(count); let pointer = unsafe { std::alloc::alloc_zeroed(layout) }; if pointer.is_null() { std::alloc::handle_alloc_error(layout); } pointer.cast::<u64>() }',
                  'unsafe fn axle_free(pointer: *mut u64, count: u64) { std::alloc::dealloc(pointer.cast::<u8>(), axle_layout(count)); }']
        for name, params, result, effects, statements in self.functions:
            self.scopes, self.effects, self.result, self.unsafe = [{}], effects, result, False
            for param, typ in params:
                if typ == 'unit' or param.value in self.scopes[0]: raise Error('duplicate or unit parameter', param)
                self.scopes[0][param.value] = (typ, False)
            body, returned = self.block(statements)
            if result != 'unit' and not returned: raise Error('not all paths return a value', name)
            arguments = ', '.join('v_' + p.value + ': ' + rust_type(t) for p, t in params)
            chunks.append(f'fn f_{name.value}({arguments}) -> {rust_type(result)} {{\n{body}\n}}')
        chunks.append('fn main() { f_main(); }')
        return '\n\n'.join(chunks) + '\n'

def compile_source(source):
    return Compiler(source).compile()

def compile_file(path, *, foreign_commands=None, import_base=None):
    """Resolve relative imports once; imported names share one global namespace."""
    visited, active, functions = set(), set(), []
    root_path = pathlib.Path(path).resolve()
    def visit(current, origin=None):
        current = pathlib.Path(current).resolve()
        if current in active: raise Error('cyclic import', origin)
        if current in visited: return
        active.add(current)
        try:
            source = current.read_text(encoding='utf-8')
        except (OSError, UnicodeError) as error:
            raise Error('cannot read module: ' + str(error), origin) from None
        parser = Parser(source, str(current))
        own_functions = parser.program()
        for token in parser.imports:
            relative = pathlib.Path(token.value[1:-1])
            if relative.is_absolute() or relative.drive or relative.root:
                raise Error('imports must be relative to their source file', token)
            base = pathlib.Path(import_base) if current == root_path and import_base is not None else current.parent
            visit(base / relative, token)
        functions.extend(own_functions)
        active.remove(current)
        visited.add(current)
    visit(path)
    return Compiler(functions=functions, foreign_commands=foreign_commands).compile()

def build_flags(profile='release'):
    if profile not in {'release', 'native', 'size'}: raise ValueError('unknown build profile ' + profile)
    flags = ['--edition=2021', '-C', 'opt-level=' + ('z' if profile == 'size' else '3'), '-C', 'panic=abort', '-C', 'codegen-units=1']
    if profile == 'native': flags += ['-C', 'target-cpu=native', '-C', 'lto=fat']
    if profile == 'size': flags += ['-C', 'lto=fat', '-C', 'strip=symbols']
    return flags

def build(rust_source, output, profile='release'):
    rustc = shutil.which('rustc')
    if not rustc: raise RuntimeError('rustc is required for build/run; install a Rust toolchain')
    output = pathlib.Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='axle-') as directory:
        path = pathlib.Path(directory) / 'program.rs'
        path.write_text(rust_source, encoding='utf-8')
        process = subprocess.run([rustc, str(path), *build_flags(profile), '-o', str(output)], capture_output=True, text=True)
        if process.returncode: raise RuntimeError('native backend failed:\n' + process.stderr)
    return output

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=VERSION)
    parser.add_argument('command', choices=['check', 'emit', 'build', 'run'])
    parser.add_argument('source', type=pathlib.Path)
    parser.add_argument('-o', '--output', type=pathlib.Path)
    parser.add_argument('--json', action='store_true', help='machine-readable compiler diagnostics')
    parser.add_argument('--profile', choices=['release', 'native', 'size'], default='release')
    parser.add_argument('--args', nargs=argparse.REMAINDER, default=[], help='arguments passed to run executable; must be last')
    options = parser.parse_args(argv)
    try:
        if options.output and options.output.resolve() == options.source.resolve():
            raise RuntimeError('output must not overwrite source')
        try:
            rust = compile_file(options.source)
        except RecursionError:
            raise Error('source nesting exceeds compiler limit') from None
        if options.command == 'emit':
            if options.output: options.output.write_text(rust, encoding='utf-8')
            else: print(rust, end='')
        elif options.command == 'build':
            output = options.output or options.source.with_suffix('.exe' if sys.platform == 'win32' else '.bin')
            if output.resolve() == options.source.resolve(): raise RuntimeError('output must not overwrite source')
            build(rust, output, options.profile)
        elif options.command == 'run':
            with tempfile.TemporaryDirectory(prefix='axle-run-') as directory:
                executable = build(rust, pathlib.Path(directory) / ('program.exe' if sys.platform == 'win32' else 'program'), options.profile)
                return subprocess.run([str(executable), *options.args]).returncode
        if options.json and options.command != 'run': print(json.dumps({'ok': True, 'version': VERSION}), file=sys.stderr if options.command == 'emit' and not options.output else sys.stdout)
        elif options.command == 'check': print('OK')
        return 0
    except (Error, OSError, RuntimeError, ValueError) as error:
        diagnostic = {'ok': False, 'message': str(error)}
        if isinstance(error, Error): diagnostic.update(line=error.line, column=error.column)
        if isinstance(error, Error) and error.source: diagnostic['source'] = error.source
        if options.json: print(json.dumps(diagnostic), file=sys.stderr)
        else: print(f'{diagnostic.get("source", options.source)}:{diagnostic.get("line", 1)}:{diagnostic.get("column", 1)}: {error}', file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())
