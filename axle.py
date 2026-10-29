#!/usr/bin/env python3
"""AXLE 0.2: a typed, effect-checked native language. Python 3.10+, Rust 1.82+."""
import argparse
import difflib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

import bridge_runtime

VERSION = '0.4.0'
EDITION = '0.4'
TYPES = {'i64', 'u64', 'bool', 'unit', 'buf_u64', 'f64', 'str'}
NUMERIC = {'i64', 'u64', 'f64'}
# name: (parameter types, result, Rust template with $0, $1, ..., allocates, can abort, why it can abort)
STR_FUNCS = {
    'str_len': (['str'], 'u64', '(($0).len() as u64)', False, None),
    'str_byte': (['str', 'u64'], 'u64', '(($0).as_bytes()[($1) as usize] as u64)', False, 'index past the end of the string'),
    'str_slice': (['str', 'u64', 'u64'], 'str', 'String::from(&($0)[($1) as usize..($2) as usize])', True, 'range outside the string or inside a multi-byte character'),
    'str_find': (['str', 'str'], 'i64', '(($0).find(&*($1)).map(|i| i as i64).unwrap_or(-1))', False, None),
    'str_starts_with': (['str', 'str'], 'bool', '($0).starts_with(&*($1))', False, None),
    'str_ends_with': (['str', 'str'], 'bool', '($0).ends_with(&*($1))', False, None),
    'str_from_i64': (['i64'], 'str', '($0).to_string()', True, None),
    'str_from_u64': (['u64'], 'str', '($0).to_string()', True, None),
    'str_from_f64': (['f64'], 'str', 'format!("{:?}", $0)', True, None),
    'str_parse_i64': (['str', 'i64'], 'i64', '($0).trim().parse::<i64>().unwrap_or($1)', False, None),
    'str_parse_f64': (['str', 'f64'], 'f64', '($0).trim().parse::<f64>().unwrap_or($1)', False, None),
    'str_upper': (['str'], 'str', '($0).to_uppercase()', True, None),
    'str_lower': (['str'], 'str', '($0).to_lowercase()', True, None),
    'str_trim': (['str'], 'str', '($0).trim().to_string()', True, None),
}
STR_ESCAPES = {'n': '\\n', 't': '\\t', 'r': '\\r', '0': '\\0', '\\': '\\\\', '"': '\\"'}
# name: (parameter types, result, Rust template). f64 never aborts: IEEE 754 gives inf/NaN instead.
F64_FUNCS = {
    'f64_from_i64': (['i64'], 'f64', '(({0}) as f64)'), 'f64_from_u64': (['u64'], 'f64', '(({0}) as f64)'),
    'f64_to_i64': (['f64'], 'i64', '(({0}) as i64)'), 'f64_to_u64': (['f64'], 'u64', '(({0}) as u64)'),
    'f64_is_nan': (['f64'], 'bool', '({0}).is_nan()'), 'f64_pow': (['f64', 'f64'], 'f64', '({0}).powf({1})'),
    'f64_to_bits': (['f64'], 'u64', '({0}).to_bits()'), 'f64_from_bits': (['u64'], 'f64', 'f64::from_bits({0})'),
}
for _n in ('sqrt', 'abs', 'floor', 'ceil', 'round', 'sin', 'cos', 'tan', 'exp', 'ln'):
    F64_FUNCS['f64_' + _n] = (['f64'], 'f64', '({0}).' + _n + '()')

# Stable diagnostic codes and generic repair hints, matched against the message.
DIAGNOSTICS = [
    (r'^unexpected character', 'E001', 'remove the character; AXLE has no such symbol'),
    (r'^expected a non-reserved identifier', 'E003', 'pick a name that is not a keyword, type or builtin'),
    (r'^expected ', 'E002', None),
    (r'^unknown variable', 'E010', 'declare it first with let/var, or check the spelling'),
    (r'^unknown function', 'E011', 'declare it with fn, import its module, or declare it with extern'),
    (r'must declare (io|mem) effect', 'E020', 'add the effect to the enclosing fn signature, e.g. `-> unit ![io, mem]`'),
    (r'requires unsafe block', 'E021', 'wrap the call in `unsafe { ... }`; prefer a safe AXLE wrapper function'),
    (r'immutable', 'E030', 'declare the binding with `var` instead of `let`'),
    (r'duplicate', 'E031', 'rename one of them'),
    (r'unreachable', 'E032', 'delete the statements after return/break/continue'),
    (r'not all paths return', 'E033', 'end every path with `return value;`'),
    (r'out of range', 'E034', 'use a literal that fits the type, or the u64 suffix'),
    (r'(break|continue) outside', 'E035', 'use break/continue only inside a while loop'),
    (r'^expected (i64|u64|bool|unit|buf_u64|\*)', 'E040', 'AXLE has no implicit conversions; make both sides the same type (literals take u64/i64 suffixes)'),
    (r'^arguments must have types', 'E041', 'pass arguments with exactly the declared types'),
    (r'^edition', 'E070', 'run `axle manifest` for the current language description, then update the code or its edition line'),
    (r'AXLE has no ', 'E071', None),
    (r'declared (nothrow|noalloc)', 'E060', 'remove the guarantee, or fix the call chain named in the message'),
    (r'extern|bridge|native|link', 'E050', None),
]

# Constructs people and models bring from other languages, mapped to what AXLE actually has.
IDIOMS = {
    'println': 'use `print(x);` (one i64, u64 or bool per call)', 'printf': 'use `print(x);`', 'puts': 'use `print(x);`',
    'echo': 'use `print(x);`', 'log': 'use `print(x);`', 'console': 'use `print(x);`',
    'len': 'use `buf_len(buffer)`', 'length': 'use `buf_len(buffer)`', 'size': 'use `buf_len(buffer)`', 'count': 'use `buf_len(buffer)`',
    'malloc': 'use `alloc_u64(count)` inside `unsafe`, or the checked `buf_new(count)`', 'calloc': 'use `buf_new(count)` (zero-initialised)',
    'free': 'use `free_u64(pointer, count)` inside `unsafe`; checked buffers free themselves',
    'new': 'use `buf_new(count)`', 'push': 'buffers have a fixed length; use `buf_new(n)` and `buf_set`', 'append': 'buffers have a fixed length; use `buf_new(n)` and `buf_set`',
    'get': 'use `buf_get(buffer, index)`', 'set': 'use `buf_set(buffer, index, value)`', 'sum': 'use `buf_sum(buffer)`',
    'null': 'there is no null; a missing value is a failure: use `assert(...)` or `abort()`', 'nil': 'there is no null; use `assert(...)` or `abort()`',
    'None': 'there is no null; use `assert(...)` or `abort()`', 'NULL': 'there is no null; use `assert(...)` or `abort()`', 'undefined': 'there is no undefined value',
    'panic': 'use `abort()`', 'exit': 'use `abort()` to stop with a failure', 'todo': 'use `unimplemented()`', 'unreachable': 'use `abort()`',
    'min': 'write `if a < b { ... }`', 'max': 'write `if a > b { ... }`', 'abs': 'write `if x < 0 { return -x; }`', 'sqrt': 'there are no floats yet', 'pow': 'write a `while` loop',
}
FOREIGN_KEYWORDS = {
    'for': 'AXLE has no for loops; write `var i = 0; while i < n { ...; i = i + 1; }`',
    'foreach': 'AXLE has no foreach; use `while` with an index', 'do': 'AXLE has no do-while; use `while`',
    'loop': 'AXLE has no `loop`; use `while true { ... break; }`', 'repeat': 'AXLE has no repeat; use `while`', 'until': 'AXLE has no until; use `while !(cond)`',
    'switch': 'AXLE has no switch; use an `if` / `else if` chain', 'match': 'AXLE has no match; use an `if` / `else if` chain',
    'try': 'AXLE has no exceptions: a failure aborts the program; use `assert(cond)` or `abort()`', 'catch': 'AXLE has no exceptions; failures abort the program',
    'throw': 'AXLE has no exceptions; use `abort()`', 'raise': 'AXLE has no exceptions; use `abort()`',
    'elif': 'write `else if`', 'elseif': 'write `else if`', 'const': 'use `let`', 'final': 'use `let`', 'auto': 'use `let`', 'val': 'use `let` (or `var` to reassign)',
    'class': 'AXLE has no classes or structs yet; pass scalars and buffers', 'struct': 'AXLE has no structs yet; pass scalars and buffers',
    'enum': 'AXLE has no enums yet; use integer constants', 'def': 'declare functions as `fn name(a: i64) -> i64 { ... }`',
    'function': 'declare functions as `fn name(a: i64) -> i64 { ... }`', 'func': 'declare functions as `fn name(a: i64) -> i64 { ... }`',
    'fun': 'declare functions as `fn name(a: i64) -> i64 { ... }`', 'pub': 'everything is public; just write `fn`', 'public': 'everything is public; just write `fn`',
    'static': 'there are no globals; pass values as parameters', 'global': 'there are no globals; pass values as parameters',
}
TYPE_IDIOMS = {'int': 'use `i64` (signed) or `u64` (unsigned)', 'long': 'use `i64`', 'uint': 'use `u64`', 'usize': 'use `u64`', 'size_t': 'use `u64`',
               'i32': 'use `i64`; narrower widths exist only as C ABI types in extern declarations (c_int)', 'u32': 'use `u64`', 'u8': 'use `u64`',
               'float': 'there are no floats yet', 'double': 'there are no floats yet', 'f32': 'there are no floats yet', 'f64': 'there are no floats yet',
               'string': 'there are no strings yet', 'str': 'there are no strings yet', 'void': 'use `unit`', 'boolean': 'use `bool`', 'char': 'use `u64`',
               'Vec': 'use `buf_u64`', 'Array': 'use `buf_u64`', 'list': 'use `buf_u64`'}
CHAR_IDIOMS = {'.': 'AXLE has no ranges or method calls; use `while`, and call functions as `f(x)`; `p.field` reads a struct field', '"': 'strings are only valid in import/extern/link/native declarations',
               "'": 'there are no character literals; use a u64 number', '@': 'AXLE has no attributes or decorators',
               '#': 'comments start with `//`', '$': 'identifiers use letters, digits and `_`', '?': 'AXLE has no `?`; use `if`', '`': 'AXLE has no template strings'}
COMPOUND = {'+=', '-=', '*=', '/=', '%=', '&=', '|=', '^=', '<<=', '>>='}

def did_you_mean(name, candidates):
    match = difflib.get_close_matches(name, sorted(candidates), n=1, cutoff=0.6)
    return match[0] if match else None

class Error(Exception):
    def __init__(self, message, token=None, hint=None, suggestion=None):
        self.message = message
        self.code, generic = 'E000', None
        for pattern, code, text in DIAGNOSTICS:
            if re.search(pattern, message):
                self.code, generic = code, text
                break
        self.hint = hint or generic
        self.suggestion = suggestion
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
    bad: bool = False

def lex(source, source_name=None):
    pattern = re.compile(r'\s+|//[^\n]*|"(?:[^"\\\n]|\\.)*"|[0-9]+\.[0-9]+(?:[eE][+-]?[0-9]+)?(?:f64)?|[0-9]+[eE][+-]?[0-9]+(?:f64)?|[0-9]+f64|[0-9]+(?:u64|i64)?|[A-Za-z_][A-Za-z_0-9]*|->|==|!=|<=|>=|<<=|>>=|\+=|-=|\*=|/=|%=|&=|\|=|\^=|&&|\|\||<<|>>|[{}()\[\],:;!~+*/%<>=&|^.\-]')
    tokens, pos, line, column = [], 0, 1, 1
    while pos < len(source):
        match = pattern.match(source, pos)
        if not match:
            # Defer the error: the parser may find a more meaningful problem earlier (for example `for`).
            tokens.append(Token(source[pos], line, column, source_name, bad=True))
            pos += 1
            column += 1
            continue
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

ABI_TYPES = {'c_str': '*const std::ffi::c_char', 'c_int': 'std::ffi::c_int', 'c_uint': 'std::ffi::c_uint', 'c_long': 'std::ffi::c_long',
             'c_ulong': 'std::ffi::c_ulong', 'c_size': 'usize'}
ABI_VISIBLE = {'c_str': 'str', 'c_int': 'i64', 'c_long': 'i64', 'c_uint': 'u64', 'c_ulong': 'u64', 'c_size': 'u64'}
RESERVED = TYPES | set(ABI_TYPES) | {'import', 'fn', 'let', 'var', 'return', 'if', 'else', 'while', 'unsafe', 'true', 'false',
                                     'io', 'mem', 'extern', 'link', 'native', 'break', 'continue',
                                     'nothrow', 'noalloc', 'edition'}
BUILTINS = {'print', 'assert', 'addr', 'load', 'store', 'arg_u64', 'alloc_u64', 'free_u64', 'offset',
            'buf_new', 'buf_iota', 'buf_len', 'buf_get', 'buf_set', 'buf_sum', 'black_box', 'poly_exec',
            'abort', 'unimplemented', 'arg_str'} | set(F64_FUNCS) | set(STR_FUNCS)

class Parser:
    def __init__(self, source, source_name=None):
        self.tokens, self.i = lex(source, source_name), 0
        self.imports, self.externs, self.links, self.natives = [], [], [], []
        self.spans, self.guarantees = {}, {}
        self.structs = []
    def peek(self):
        return self.tokens[self.i].value
    def pop(self):
        token = self.tokens[self.i]
        if token.bad:
            raise Error('unexpected character ' + repr(token.value), token, hint=CHAR_IDIOMS.get(token.value))
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
    def type(self, abi=False):
        pointer = self.take('*')
        token = self.pop()
        if abi and not pointer and token.value in ABI_TYPES:
            return token.value
        record = (not pointer and token.value not in TYPES and token.value not in TYPE_IDIOMS and token.value not in RESERVED
                  and re.fullmatch('[A-Za-z_][A-Za-z_0-9]*', token.value))
        if record:
            return token.value      # a struct name; the compiler checks that it exists
        if token.value not in TYPES or (pointer and token.value not in {'i64', 'u64', 'bool'}):
            raise Error('expected a value type or pointer to a scalar', token, hint=TYPE_IDIOMS.get(token.value))
        return ('*' if pointer else '') + token.value
    def header(self, abi=False):
        name = self.name()
        self.need('(')
        params = []
        if self.peek() != ')':
            while True:
                param = self.name()
                self.need(':')
                params.append((param, self.type(abi)))
                if not self.take(','): break
        self.need(')')
        self.need('->')
        result = self.type(abi)
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
        self.last_guarantees = set()
        while self.peek() in {'nothrow', 'noalloc'}:
            guarantee = self.pop()
            if guarantee.value in self.last_guarantees: raise Error('duplicate guarantee ' + guarantee.value, guarantee)
            self.last_guarantees.add(guarantee.value)
        return name, params, result, effects
    def quoted(self, what):
        token = self.pop()
        if not token.value.startswith('"') or '\\' in token.value:
            raise Error(f'expected a quoted {what} without escapes', token)
        return token, token.value[1:-1]
    def program(self):
        functions = []
        self.edition = self.edition_token = None
        if self.peek() == 'edition':
            self.edition_token = self.pop()
            _, self.edition = self.quoted('edition')
            self.need(';')
        while self.peek() != '<eof>':
            if self.take('import'):
                path = self.pop()
                if not path.value.startswith('"') or '\\' in path.value or not path.value.endswith('.axl"'):
                    raise Error('import requires a quoted .axl path using forward slashes', path)
                self.need(';')
                self.imports.append(path)
                continue
            if self.peek() == 'struct':
                self.pop()
                name = self.name()
                self.need('{')
                fields = []
                while self.peek() != '}':
                    field = self.name()
                    self.need(':')
                    fields.append((field, self.type()))
                    if not self.take(','): break
                self.need('}')
                if not fields: raise Error('a struct needs at least one field', name)
                self.structs.append((name, fields))
                continue
            if self.take('link'):
                token, library = self.quoted('library name')
                if not re.fullmatch(r'[A-Za-z0-9_.+-]+', library):
                    raise Error('link requires a plain library name such as "m"; use --link-search for directories', token)
                self.need(';')
                self.links.append(library)
                continue
            if self.take('native'):
                token, path = self.quoted('source path')
                if not path.endswith(('.c', '.cc', '.cpp', '.cxx', '.o', '.a')) or path.startswith('/') or ':' in path:
                    raise Error('native requires a relative .c/.cc/.cpp/.cxx/.o/.a path with forward slashes', token)
                self.need(';')
                self.natives.append((token, path))
                continue
            if self.take('extern'):
                self.externs.append(self.extern())
                continue
            start = self.tokens[self.i]
            if self.peek() in FOREIGN_KEYWORDS:
                raise Error(f'AXLE has no `{self.peek()}`', start, hint=FOREIGN_KEYWORDS[self.peek()])
            self.need('fn')
            name, params, result, effects = self.header()
            if self.last_guarantees: self.guarantees[name.value] = self.last_guarantees
            body = self.block()
            self.spans[name.value] = (name.source, start.line, self.tokens[self.i - 1].line)
            functions.append((name, params, result, effects, body))
        return functions
    def extern(self):
        kind_token, kind = self.quoted('foreign kind')
        if kind != 'c' and not re.fullmatch(r'(py|js):[^\s@"]+(@[^\s"]+)?', kind):
            raise Error('extern kind must be "c", "py:<file-or-module>[@root]" or "js:<file-or-module>[@root]"', kind_token)
        self.need('fn')
        name, params, result, effects = self.header(abi=(kind == 'c'))
        symbol = name.value
        if self.take('='):
            _, symbol = self.quoted('foreign name')
            if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', symbol):
                raise Error('foreign name must be an identifier', name)
        self.need(';')
        if self.last_guarantees: self.guarantees[name.value] = self.last_guarantees
        self.spans[name.value] = (name.source, kind_token.line, self.tokens[self.i - 1].line)
        return (kind_token, kind, name, params, result, effects, symbol)
    def block(self):
        self.need('{')
        statements = []
        while self.peek() not in {'}', '<eof>'}:
            statements.append(self.statement())
        self.need('}')
        return statements
    def statement(self):
        token = self.tokens[self.i]
        if token.value in FOREIGN_KEYWORDS and self.tokens[self.i + 1].value != '=':
            raise Error(f'AXLE has no `{token.value}`', token, hint=FOREIGN_KEYWORDS[token.value])
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
            no = None
            if self.take('else'):
                no = [self.statement()] if self.peek() == 'if' else self.block()
            return ('if', token, cond, yes, no)
        if self.peek() in {'break', 'continue'}:
            self.pop()
            self.need(';')
            return (token.value, token)
        if self.take('while'):
            return ('while', token, self.expr(), self.block())
        if self.take('unsafe'):
            return ('unsafe', token, self.block())
        expr = self.expr()
        if self.peek() in COMPOUND:
            op = self.peek()
            target = expr[1].value if expr[0] == 'name' else 'target'
            raise Error(f'AXLE has no `{op}`', self.tokens[self.i], hint=f'write `{target} = {target} {op[:-1]} value;`')
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
        if token.value in {'!', '-', '~'}:
            left = ('unary', token, self.expr(11))
        elif token.value == '(':
            left = self.expr()
            self.need(')')
        elif re.fullmatch(r'[0-9]+(?:u64|i64)?|' + FLOAT, token.value):
            left = ('number', token)
        elif token.value.startswith('"'):
            left = ('string', token)
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
            elif (self.peek() == '{' and self.tokens[self.i + 1].value != '}' and re.fullmatch('[A-Za-z_][A-Za-z_0-9]*', self.tokens[self.i + 1].value)
                  and self.tokens[self.i + 2].value == ':'):
                self.pop()
                values = []
                while True:
                    field = self.name()
                    self.need(':')
                    values.append((field, self.expr()))
                    if not self.take(','): break
                    if self.peek() == '}': break
                self.need('}')
                left = ('record', token, values)
            else:
                left = ('name', token)
        else:
            raise Error('expected expression', token)
        while self.peek() == '.':
            dot = self.pop()
            left = ('field', dot, left, self.name())
        while self.PRECEDENCE.get(self.peek(), -1) >= minimum:
            op = self.pop()
            left = ('binary', op, left, self.expr(self.PRECEDENCE[op.value] + 1))
        return left

FLOAT = r'[0-9]+\.[0-9]+(?:[eE][+-]?[0-9]+)?(?:f64)?|[0-9]+[eE][+-]?[0-9]+(?:f64)?|[0-9]+f64'

def rust_type(typ):
    return '*mut ' + typ[1:] if typ.startswith('*') else '()' if typ == 'unit' else 'AxleBuffer' if typ == 'buf_u64' else 'String' if typ == 'str' else typ if typ in TYPES else 'S_' + typ

def check_edition(parser):
    if parser.edition is not None and parser.edition != EDITION:
        raise Error(f'edition {parser.edition} is not supported: this compiler implements edition {EDITION}', parser.edition_token,
                    hint=f'run `axle manifest` for the current language, then write `edition \"{EDITION}\";` or remove the line')

class RustSource(str):
    """Generated Rust text that also remembers what the final link needs."""
    links, natives = (), ()

def rust_literal(text):
    if any(ord(ch) < 32 for ch in text): raise ValueError('control character in path')
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'

class Compiler:
    def __init__(self, source=None, functions=None, foreign_commands=None, externs=None, links=(), natives=(), guarantees=None, require_main=True, test_mode=False, structs=()):
        self.structs_raw = list(structs)
        self.foreign_commands = foreign_commands or {}
        self.test_mode = test_mode
        self.guarantees = dict(guarantees or {})
        self.facts, self.current = {}, None
        self.links, self.natives = list(links), list(natives)
        externs = list(externs or [])
        if functions is None:
            parser = Parser(source)
            self.functions = parser.program()
            self.structs_raw += parser.structs
            check_edition(parser)
            externs += parser.externs
            self.guarantees.update(parser.guarantees)
            self.links += parser.links
            if parser.natives: raise Error('native requires compile_file or the CLI', parser.natives[0][0])
            if parser.imports: raise Error('imports require compile_file or the CLI', parser.imports[0])
        else:
            self.functions = functions
        self.signatures = {}
        self.externs = {}
        self.structs = {}
        for name, fields in self.structs_raw:
            if name.value in self.structs or name.value in TYPES or name.value in ABI_TYPES:
                raise Error('duplicate or reserved struct name', name)
            self.structs[name.value] = fields
        for name, fields in self.structs_raw:
            seen = set()
            for field, typ in fields:
                if field.value in seen: raise Error('duplicate field ' + field.value, field)
                seen.add(field.value)
                if typ in {'unit', 'buf_u64'} or typ.startswith('*') or typ in ABI_TYPES:
                    raise Error(f'a struct field cannot have type {typ}', field, hint='fields hold i64, u64, f64, bool, str or another struct; buffers are shared handles and would break value semantics')
                self.check_type(typ, field)
        for name, fields in self.structs_raw:
            self.check_cycle(name.value, name, [])
        for name, params, result, effects, body in self.functions:
            for param, typ in params: self.check_type(typ, param)
            self.check_type(result, name)
            if name.value in self.signatures or name.value in BUILTINS:
                raise Error('duplicate or reserved function name', name)
            self.signatures[name.value] = ([t for _, t in params], result, effects)
        for kind_token, kind, name, params, result, effects, symbol in externs:
            if name.value in self.signatures or name.value in BUILTINS:
                raise Error('duplicate or reserved function name', name)
            lang = kind.split(':', 1)[0]
            for param, typ in params:
                self.check_type(typ, param)
                if (typ in self.structs) and lang == 'c':
                    raise Error('extern "c" cannot take a struct', param, hint='pass its fields as separate scalar arguments')
                if typ == 'str' and lang == 'c':
                    raise Error('extern "c" cannot take str directly', param, hint='declare the parameter as c_str (a NUL-terminated const char*)')
                if typ in {'unit', 'buf_u64'} or (lang != 'c' and typ.startswith('*')):
                    raise Error(f'extern parameter type {typ} is not supported for {lang!r} externs', param)
            self.check_type(result, name)
            if result in self.structs and lang == 'c': raise Error('extern "c" cannot return a struct', name, hint='return its fields through separate calls')
            if result == 'buf_u64' or (lang != 'c' and result.startswith('*')):
                raise Error(f'extern result type {result} is not supported for {lang!r} externs', name)
            seen = set()
            for param, _ in params:
                if param.value in seen: raise Error('duplicate or unit parameter', param)
                seen.add(param.value)
            visible = lambda t: ABI_VISIBLE.get(t, t)
            if lang != 'c' and self.guarantees.get(name.value):
                raise Error('extern bridge functions cannot claim nothrow or noalloc: foreign calls can fail and allocate', name)
            declared = set(effects) | ({'io'} if lang != 'c' else set())
            self.signatures[name.value] = ([visible(t) for _, t in params], visible(result), declared)
            self.externs[name.value] = (kind_token, kind, params, result, symbol)
        if 'main' in self.signatures and self.signatures['main'][:2] != ([], 'unit'):
            raise Error('program requires fn main() -> unit')
        if 'main' not in self.signatures and require_main and not test_mode:
            raise Error('program requires fn main() -> unit')
        self.test_names = sorted(n for n, (p, r, e) in self.signatures.items()
                                 if n.startswith('test_') and not p and r == 'unit' and n not in self.externs)
    def check_type(self, typ, token):
        if typ in TYPES or typ in ABI_TYPES or typ.startswith('*') or typ in self.structs: return
        close = did_you_mean(typ, set(self.structs) | TYPES)
        raise Error('unknown type ' + typ, token, hint=f'did you mean `{close}`?' if close else 'declare it with `struct Name { field: type }`', suggestion=close)
    def check_cycle(self, name, token, path):
        if name in path:
            raise Error('struct ' + name + ' contains itself', token, hint='a struct cannot hold itself directly; there are no references or recursive types yet')
        for _, typ in self.structs[name]:
            if typ in self.structs: self.check_cycle(typ, token, path + [name])
    def has_text(self, typ):
        return typ == 'str' or (typ in getattr(self, 'structs', {}) and any(self.has_text(t) for _, t in self.structs[typ]))
    def note(self, kind, token, **detail):
        """Record one footprint fact: something this source position can do."""
        if self.current is not None:
            self.facts.setdefault(self.current, []).append(
                dict(kind=kind, function=self.current, line=token.line, column=token.column, source=token.source, **detail))
    def alias_note(self, node, typ, token):
        if node[0] == 'name' and typ == 'buf_u64':
            self.note('alias', token, name=node[1].value, what=f'second handle to the same shared buffer as `{node[1].value}`; writes through either are visible to both')
    def require(self, actual, expected, token):
        if actual != expected:
            raise Error(f'expected {expected}, got {actual}', token)
    def effect(self, effect, token):
        if effect not in self.effects:
            raise Error(f'function must declare {effect} effect', token,
                        hint=f'add {effect} to the effect list of fn {self.current}, e.g. `![{effect}]`')
    def lookup(self, token):
        for scope in reversed(self.scopes):
            if token.value in scope: return scope[token.value]
        names = {name for scope in self.scopes for name in scope}
        close = did_you_mean(token.value, names)
        raise Error('unknown variable ' + token.value, token,
                    hint=IDIOMS.get(token.value) or (f'did you mean `{close}`?' if close else None), suggestion=close)
    def expression(self, node):
        kind, token, *rest = node
        value = token.value
        if kind == 'number' and re.fullmatch(FLOAT, value):
            text = value.removesuffix('f64')
            if float(text) == float('inf'): raise Error('float literal out of range', token)
            return 'f64', text + 'f64'
        if kind == 'number':
            match = re.fullmatch(r'([0-9]+)(u64|i64)?', value)
            if len(match[1]) > 20: raise Error('integer literal out of range', token)
            number, typ = int(match[1]), match[2] or 'i64'
            if number > (2**64 - 1 if typ == 'u64' else 2**63 - 1):
                raise Error('integer literal out of range', token)
            return typ, str(number) + typ
        if kind == 'record':
            fields = self.structs.get(value)
            if fields is None:
                close = did_you_mean(value, set(self.structs))
                raise Error('unknown struct ' + value, token, hint=f'did you mean `{close}`?' if close else 'declare it with `struct Name { field: type }`', suggestion=close)
            given = {}
            for field, expr in rest[0]:
                if field.value in given: raise Error('field ' + field.value + ' given twice', field)
                given[field.value] = (field, self.expression(expr))
            parts = []
            for field, typ in fields:
                if field.value not in given: raise Error(f'missing field {field.value} in {value}', token, hint='every field must be given')
                supplied, (actual, code) = given.pop(field.value)
                self.require(actual, typ, supplied)
                parts.append(f'f_{field.value}: {code}')
            if given:
                extra = next(iter(given.values()))[0]
                raise Error(f'{value} has no field {extra.value}', extra, hint='fields: ' + ', '.join(f.value for f, _ in fields))
            return value, f'S_{value} {{ ' + ', '.join(parts) + ' }'
        if kind == 'field':
            typ, code = self.expression(rest[0])
            fields = self.structs.get(typ)
            if fields is None: raise Error(f'{typ} has no fields', token, hint='only structs have fields')
            names = {f.value: t for f, t in fields}
            if rest[1].value not in names:
                close = did_you_mean(rest[1].value, set(names))
                raise Error(f'{typ} has no field {rest[1].value}', rest[1], hint=f'did you mean `{close}`?' if close else 'fields: ' + ', '.join(names), suggestion=close)
            field_type = names[rest[1].value]
            if self.has_text(field_type): self.note('alloc', token, what='copy of a string field', via='.' + rest[1].value)
            return field_type, f'({code}).f_{rest[1].value}' + ('.clone()' if field_type in self.structs or field_type == 'str' else '')
        if kind == 'bool': return 'bool', value
        if kind == 'string':
            out, i = [], 1
            while i < len(value) - 1:
                ch = value[i]
                if ch == '\\':
                    i += 1
                    if value[i] not in STR_ESCAPES:
                        raise Error(f'unknown escape \\{value[i]} in string', token, hint='supported escapes: \\n \\t \\r \\0 \\\\ \\"')
                    out.append(STR_ESCAPES[value[i]])
                elif ord(ch) < 32: out.append('\\u{%x}' % ord(ch))
                else: out.append(ch)
                i += 1
            self.note('alloc', token, what='string literal', via='"..."')
            return 'str', 'String::from("' + ''.join(out) + '")'
        if kind == 'name':
            typ, mutable = self.lookup(token)
            code = 'v_' + value
            if typ == 'buf_u64' or typ in self.structs or typ == 'str':
                if self.has_text(typ): self.note('alloc', token, what='copy of a string', via=f'use of `{value}`')
                return typ, code + '.clone()'
            return typ, code
        if kind == 'unary':
            if value == '-' and rest[0][0] == 'number' and rest[0][1].value in {'9223372036854775808', '9223372036854775808i64'}:
                return 'i64', 'i64::MIN'
            typ, code = self.expression(rest[0])
            if value == '!':
                self.require(typ, 'bool', token)
                return typ, f'(!({code}))'
            if value == '~':
                if typ not in {'i64', 'u64'}: raise Error('bitwise not requires integer', token)
                return typ, f'(!({code}))'
            if typ == 'f64': return typ, f'(-({code}))'
            if typ not in {'i64', 'u64'}: raise Error('negation requires a number', token)
            return typ, f'({code}).wrapping_neg()'
        if kind == 'binary':
            lt, lc = self.expression(rest[0])
            rt, rc = self.expression(rest[1])
            self.require(rt, lt, token)
            if value in {'&&', '||'}:
                self.require(lt, 'bool', token)
            elif value in {'==', '!='}:
                if lt in {'unit', 'buf_u64'}: raise Error(lt + ' cannot be compared', token)
            elif lt == 'f64' and value in {'&', '|', '^', '<<', '>>'}:
                raise Error(f'operator {value} is not defined for f64', token, hint='bit operations need integers; use f64_to_bits to inspect the bits')
            elif lt not in NUMERIC and lt != 'str':
                raise Error('operator requires integer operands', token)
            if lt == 'str':
                if value == '+':
                    self.effect('mem', token)
                    self.note('alloc', token, what='concatenated string', via='+', effect='mem')
                    return 'str', f'{{ let mut axle_text = {lc}; axle_text.push_str(&({rc})); axle_text }}'
                if value not in {'==', '!=', '<', '>', '<=', '>='}:
                    raise Error(f'operator {value} is not defined for str', token, hint='strings support + (concatenation) and comparisons')
                return 'bool', f'(({lc}) {value} ({rc}))'
            if lt == 'f64':
                return ('bool' if value in {'==', '!=', '<', '>', '<=', '>='} else 'f64'), f'(({lc}) {value} ({rc}))'
            methods = {'+': 'wrapping_add', '-': 'wrapping_sub', '*': 'wrapping_mul',
                       '/': 'wrapping_div', '%': 'wrapping_rem', '<<': 'wrapping_shl', '>>': 'wrapping_shr'}
            if value in methods:
                # Mask before narrowing: shifts use the low six bits on both integer types.
                rhs = f'(({rc}) & 63{rt}) as u32' if value in {'<<', '>>'} else rc
                code = f'({lc}).{methods[value]}({rhs})'
            else: code = f'(({lc}) {value} ({rc}))'
            if value in {'/', '%'}:
                divisor = rest[1]
                if not (divisor[0] == 'number' and int(re.match(r'[0-9]+', divisor[1].value)[0]) != 0):
                    self.note('abort', token, why='division by zero', via=value)
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
                self.note('unsafe', token, what=f'takes the raw address of `{args[0][1].value}`', effect='mem')
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
                self.note('foreign', token, abi='polyglot', symbol=f'block {index}', effect='io')
                self.note('abort', token, why='a failing guest block aborts the program', via='poly_exec')
                return 'unit', self.foreign_commands[index]
            if value in STR_FUNCS:
                want, result, template, allocates, abort_why = STR_FUNCS[value]
                if types != want: raise Error(f'{value} requires ({", ".join(want)}), got ({", ".join(types)})', token)
                if allocates:
                    self.effect('mem', token)
                    self.note('alloc', token, what='string', via=value, effect='mem')
                if abort_why: self.note('abort', token, why=abort_why, via=value)
                code = template
                for index in reversed(range(len(codes))): code = code.replace(f'${index}', codes[index])
                return result, code
            if value == 'arg_str':
                self.effect('io', token)
                self.effect('mem', token)
                if types != ['u64']: raise Error('arg_str requires one u64 index', token)
                self.note('abort', token, why='missing command-line argument', via='arg_str', effect='io')
                self.note('alloc', token, what='string', via='arg_str', effect='mem')
                return 'str', f'std::env::args().nth(({codes[0]}) as usize).unwrap_or_else(|| panic!("missing command-line argument"))'
            if value in F64_FUNCS:
                want, result, template = F64_FUNCS[value]
                if types != want: raise Error(f'{value} requires ({", ".join(want)}), got ({", ".join(types)})', token)
                return result, template.format(*codes)
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
                target = args[0][1].value if args and args[0][0] == 'name' else 'a buffer'
                if value in {'buf_new', 'buf_iota'}: self.note('alloc', token, what='buffer', via=value, effect='mem')
                elif value in {'buf_get', 'buf_set'}: self.note('abort', token, why='index out of bounds', via=value, effect='mem')
                if value == 'buf_set': self.note('write', token, target=target, what=f'mutates shared buffer `{target}`', effect='mem')
                elif value in {'buf_get', 'buf_len', 'buf_sum'}: self.note('read', token, target=target, effect='mem')
                return result, 'axle_' + value + '(' + ', '.join(codes) + ')'
            if value in {'alloc_u64', 'free_u64', 'offset'}:
                if not self.unsafe: raise Error(value + ' requires unsafe block', token)
                self.effect('mem', token)
                self.note('unsafe', token, what={'alloc_u64': 'raw allocation: you must free it exactly once', 'free_u64': 'raw release: pointer and count must match the allocation',
                                                 'offset': 'pointer arithmetic is not bounds-checked'}[value], effect='mem')
                if value == 'alloc_u64':
                    if types != ['u64']: raise Error('alloc_u64 requires a u64 count', token)
                    self.note('alloc', token, what='raw memory', via=value, effect='mem')
                    self.note('abort', token, why='allocation count is zero or too large', via=value, effect='mem')
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
                self.note('unsafe', token, what='raw ' + value + ': undefined behavior if the pointer is invalid', effect='mem')
                self.note('read' if value == 'load' else 'write', token, target='raw memory', what=('reads' if value == 'load' else 'writes') + ' raw memory', effect='mem')
                if value == 'load': return types[0][1:], f'unsafe {{ *({codes[0]}) }}'
                self.require(types[1], types[0][1:], token)
                return 'unit', f'{{ let axle_pointer = {codes[0]}; let axle_value = {codes[1]}; unsafe {{ *axle_pointer = axle_value; }} }}'
            if value == 'print':
                self.effect('io', token)
                if len(types) != 1 or types[0] not in {'i64', 'u64', 'bool', 'f64', 'str'}: raise Error('print requires one scalar or str', token)
                self.note('io', token, what='prints to standard output', effect='io')
                self.note('abort', token, why='standard output can fail (for example a closed pipe)', via='print', effect='io')
                return 'unit', 'println!("' + ('{:?}' if types[0] == 'f64' else '{}') + '", ' + codes[0] + ')'
            if value in {'abort', 'unimplemented'}:
                if types: raise Error(f'{value} takes no arguments', token)
                self.note('abort', token, why='explicit abort' if value == 'abort' else 'unimplemented code is reached', via=value)
                if value == 'unimplemented':
                    self.note('unimplemented', token, what='placeholder: this code path is not implemented yet')
                return 'unit', f'panic!("{value}()")'
            if value == 'assert':
                if types != ['bool']: raise Error('assert requires one bool', token)
                self.note('abort', token, why='assertion can fail', via='assert')
                where = (pathlib.Path(token.source).name if token.source else 'source') + f':{token.line}'
                return 'unit', 'assert!(' + codes[0] + f', "assertion failed at {where}")'
            if value == 'arg_u64':
                self.effect('io', token)
                if types != ['u64']: raise Error('arg_u64 requires one u64 index', token)
                self.note('io', token, what='reads a command-line argument', effect='io')
                self.note('abort', token, why='missing or non-numeric command-line argument', via='arg_u64', effect='io')
                return 'u64', 'axle_arg(' + codes[0] + ')'
            if value not in self.signatures:
                close = did_you_mean(value, set(self.signatures) | BUILTINS)
                raise Error('unknown function ' + value, token,
                            hint=IDIOMS.get(value) or (f'did you mean `{close}`?' if close else None), suggestion=close)
            expected, result, effects = self.signatures[value]
            if types != expected: raise Error(f'arguments must have types {expected}, got {types}', token)
            for effect in sorted(effects): self.effect(effect, token)
            self.note('call', token, callee=value, declared=sorted(effects))
            if value in self.externs:
                if self.externs[value][1] == 'c' and not self.unsafe:
                    raise Error(f'calling extern "c" function {value} requires unsafe block', token)
                self.note('foreign', token, callee=value, abi=self.externs[value][1], symbol=self.externs[value][4])
                if self.externs[value][1] != 'c':
                    self.note('abort', token, why=f'the {self.externs[value][1].split(":")[0]} function `{self.externs[value][4]}` can raise, which aborts the program', via=value, effect='io')
                return result, 'x_' + value + '(' + ', '.join(codes) + ')'
            return result, 'f_' + value + '(' + ', '.join(codes) + ')'
        raise Error('invalid expression', token)
    def block(self, statements):
        self.scopes.append({})
        lines, returned, ended = [], False, False
        for node in statements:
            kind, token, *rest = node
            if ended: raise Error('unreachable statement', token)
            if kind == 'bind':
                name, typ, mutable, expr = rest
                if typ == 'unit': raise Error('unit variables are not supported', name)
                if name.value in self.scopes[-1]: raise Error('duplicate variable in this scope', name)
                actual, code = self.expression(expr)
                self.alias_note(expr, actual, name)
                if typ is None: typ = actual
                if typ == 'unit': raise Error('unit variables are not supported', name)
                self.require(actual, typ, name)
                self.scopes[-1][name.value] = (typ, mutable)
                lines.append(f'let {"mut " if mutable else ""}v_{name.value}: {rust_type(typ)} = {code};')
            elif kind == 'assign':
                target, rhs = rest
                path = []
                root = target
                while root[0] == 'field':
                    path.insert(0, root[3])
                    root = root[2]
                if root[0] != 'name': raise Error('assignment target must be a variable or a field of one', token)
                typ, mutable = self.lookup(root[1])
                if not mutable: raise Error('cannot assign to immutable variable', token)
                place = 'v_' + root[1].value
                for field in path:
                    fields = self.structs.get(typ)
                    if fields is None: raise Error(f'{typ} has no fields', field)
                    names = {f.value: t for f, t in fields}
                    if field.value not in names:
                        close = did_you_mean(field.value, set(names))
                        raise Error(f'{typ} has no field {field.value}', field, hint=f'did you mean `{close}`?' if close else 'fields: ' + ', '.join(names), suggestion=close)
                    typ = names[field.value]
                    place += '.f_' + field.value
                actual, code = self.expression(rhs)
                self.require(actual, typ, token)
                self.note('assign', token, target=root[1].value, what=f'assigns local variable `{root[1].value}` (not visible outside this function)')
                self.alias_note(rhs, actual, token)
                lines.append(place + ' = ' + code + ';')
            elif kind == 'return':
                typ, code = ('unit', '()') if rest[0] is None else self.expression(rest[0])
                self.require(typ, self.result, token)
                if rest[0] is not None: self.alias_note(rest[0], typ, token)
                lines.append('return ' + code + ';')
                returned = ended = True
            elif kind in {'break', 'continue'}:
                if not self.loops: raise Error(f'{kind} outside of a while loop', token)
                lines.append(kind + ';')
                ended = True
            elif kind in {'if', 'while'}:
                typ, code = self.expression(rest[0])
                self.require(typ, 'bool', token)
                if kind == 'while':
                    self.note('loop', token, what='while loop: termination is not proven')
                    self.loops += 1
                body, yes_return, yes_end = self.block(rest[1])
                if kind == 'while': self.loops -= 1
                line = f'{kind} {code} {{\n{body}\n}}'
                if kind == 'if' and rest[2] is not None:
                    no, no_return, no_end = self.block(rest[2])
                    line += ' else {\n' + no + '\n}'
                    returned, ended = yes_return and no_return, yes_end and no_end
                lines.append(line)
            elif kind == 'unsafe':
                previous = self.unsafe
                self.note('unsafe', token, what='unsafe block: the compiler does not check memory validity inside', block=True)
                self.unsafe = True
                body, returned, ended = self.block(rest[0])
                self.unsafe = previous
                lines.append('{\n' + body + '\n}')
            else:
                typ, code = self.expression(rest[0])
                self.require(typ, 'unit', token)
                lines.append(code + ';')
                if rest[0][0] == 'call' and rest[0][1].value in {'abort', 'unimplemented'}:
                    returned = ended = True      # diverges: no value has to be invented after it
        self.scopes.pop()
        return '\n'.join(lines), returned, ended
    def json_impl(self, name, fields):
        encode, decode = [], []
        for f, t in fields:
            n = f.value
            if t in {'i64', 'u64', 'bool'}: value = f'self.f_{n}.to_string()'
            elif t == 'f64': value = f'axle_json_f64(self.f_{n})'
            elif t == 'str': value = f'axle_json_str(&self.f_{n})'
            else: value = f'self.f_{n}.to_json()'
            encode.append(f'parts.push(format!("\\"{n}\\":{{}}", {value}));')
            if t in {'i64', 'u64'}: got = f'match axle_json_get(object, "{n}")? {{ AxleJson::Num(text) => text.parse::<{t}>().ok()?, _ => return None }}'
            elif t == 'f64': got = f'match axle_json_get(object, "{n}")? {{ AxleJson::Num(text) => text.parse::<f64>().ok()?, _ => return None }}'
            elif t == 'bool': got = f'match axle_json_get(object, "{n}")? {{ AxleJson::Bool(b) => *b, _ => return None }}'
            elif t == 'str': got = f'match axle_json_get(object, "{n}")? {{ AxleJson::Str(text) => text.clone(), _ => return None }}'
            else: got = f'S_{t}::from_json(axle_json_get(object, "{n}")?)?'
            decode.append(f'f_{n}: {got}')
        return (f'impl S_{name} {{\n'
                f'    fn to_json(&self) -> String {{ let mut parts: Vec<String> = Vec::new(); {" ".join(encode)} format!("{{{{{{}}}}}}", parts.join(",")) }}\n'
                f'    fn from_json(json: &AxleJson) -> Option<Self> {{ let object = match json {{ AxleJson::Obj(fields) => fields, _ => return None }}; Some(S_{name} {{ {", ".join(decode)} }}) }}\n}}')
    def extern_wrapper(self, name, kind_token, kind, params, result, symbol):
        visible = lambda t: ABI_VISIBLE.get(t, t)
        arguments = ', '.join(f'v_{p.value}: {rust_type(visible(t))}' for p, t in params)
        head = f'fn x_{name}({arguments}) -> {rust_type(visible(result))}'
        if kind == 'c':
            lines, raw_args, raw_params = [], [], []
            for i, (p, t) in enumerate(params):
                raw_params.append(f'a{i}: {ABI_TYPES.get(t, rust_type(t))}')
                if t == 'c_str':
                    lines.append(f'let a{i} = std::ffi::CString::new(v_{p.value}.as_bytes()).unwrap_or_else(|_| panic!("argument {p.value} of {symbol} contains a NUL byte"));')
                    raw_args.append(f'a{i}.as_ptr()')
                elif t in ABI_TYPES:
                    lines.append(f'let a{i} = <{ABI_TYPES[t]}>::try_from(v_{p.value}).unwrap_or_else(|_| panic!("argument {p.value} of {symbol} does not fit {t}"));')
                    raw_args.append(f'a{i}')
                else:
                    raw_args.append(f'v_{p.value}')
            raw_result = '' if result == 'unit' else ' -> ' + ABI_TYPES.get(result, rust_type(result))
            call = f'unsafe {{ raw({", ".join(raw_args)}) }}'
            if result == 'c_str':
                tail = (f'let p = {call};\nif p.is_null() {{ panic!("{symbol} returned NULL") }}\n'
                        'unsafe { std::ffi::CStr::from_ptr(p) }.to_string_lossy().into_owned()')
            else:
                tail = f'({call}) as {rust_type(visible(result))}' if result in ABI_TYPES else call
            declaration = f'extern "C" {{ #[link_name = {rust_literal(symbol)}] fn raw({", ".join(raw_params)}){raw_result}; }}'
            return head + ' {\n' + declaration + '\n' + '\n'.join(lines) + '\n' + tail + '\n}'
        lang, spec = kind.split(':', 1)
        spec, _, root = spec.partition('@')
        base = pathlib.Path(kind_token.source).resolve().parent if kind_token.source else pathlib.Path.cwd()
        suffixes = ('.py',) if lang == 'py' else ('.js', '.mjs', '.cjs')
        if spec.endswith(suffixes):
            target = (base / spec).resolve()
            if not target.is_file(): raise Error(f'extern module not found: {spec}', kind_token)
            target = str(target)
        else:
            target = spec
        root_dir = str((base / root).resolve()) if root else (str(pathlib.Path(target).parent) if spec.endswith(suffixes) else str(base))
        values = ', '.join(f'format!("{{:?}}", v_{p.value})' if t == 'f64' else f'axle_hex_arg(&v_{p.value})' if t == 'str' else f'axle_rec_arg(&v_{p.value}.to_json())' if t in self.structs else f'v_{p.value}.to_string()' for p, t in params)
        call = f'axle_bridge_call({rust_literal(lang)}, {rust_literal(target)}, {rust_literal(root_dir)}, {rust_literal(symbol)}, &[{values}])'
        if result == 'unit': tail = f'let _ = {call};'
        elif result == 'bool':
            tail = f'let reply = {call};\nmatch reply.as_str() {{ "true" => true, "false" => false, _ => axle_bridge_fail({rust_literal(symbol)}, "expected a bool result, got", &reply) }}'
        elif result in self.structs:
            tail = (f'let reply = {call};\naxle_rec_decode(&reply).and_then(|text| axle_json_parse(&text)).and_then(|json| S_{result}::from_json(&json))'
                    f'.unwrap_or_else(|| axle_bridge_fail({rust_literal(symbol)}, "expected a {result} object, got", &reply))')
        elif result == 'str':
            tail = f'let reply = {call};\naxle_hex_decode(&reply).unwrap_or_else(|| axle_bridge_fail({rust_literal(symbol)}, "expected a str result, got", &reply))'
        else:
            tail = f'let reply = {call};\nreply.parse::<{result}>().unwrap_or_else(|_| axle_bridge_fail({rust_literal(symbol)}, "expected a {result} result, got", &reply))'
        return head + ' {\n' + tail + '\n}'
    def call_graph(self):
        graph = {name: [] for name in self.signatures}
        for name, facts in self.facts.items():
            for fact in facts:
                if fact['kind'] == 'call' and fact['callee'] not in graph[name]: graph[name].append(fact['callee'])
        return graph
    def violation(self, root, guarantee, graph=None):
        """First reason `root` cannot carry `guarantee`, with the call chain to it; None if it provably can."""
        graph = graph or self.call_graph()
        parent, queue = {root: None}, [root]
        def chain(node):
            path = []
            while node is not None: path.append(node); node = parent[node]
            return ' -> '.join(reversed(path))
        while queue:
            current = queue.pop(0)
            if current in self.externs:
                kind = self.externs[current][1]
                if kind != 'c':
                    return f'calls the {kind.split(":")[0]} bridge function {current}, which can fail and allocate (call chain: {chain(current)})'
                if guarantee not in self.guarantees.get(current, ()):
                    return (f'calls foreign function {current}, which AXLE cannot verify; add `{guarantee}` to its extern '
                            f'declaration if you trust it (call chain: {chain(current)})')
                continue
            for fact in self.facts.get(current, []):
                bad = None
                if guarantee == 'nothrow' and fact['kind'] == 'abort': bad = f'can abort: {fact["why"]}'
                elif guarantee == 'nothrow' and fact['kind'] == 'unsafe' and not fact.get('block'): bad = 'uses unsafe memory access, which can crash'
                elif guarantee == 'noalloc' and fact['kind'] == 'alloc': bad = f'allocates {fact["what"]}'
                if bad:
                    return f'{bad} (line {fact["line"]}, in fn {current}; call chain: {chain(current)})'
            for nxt in graph.get(current, []):
                if nxt not in parent:
                    parent[nxt] = current
                    queue.append(nxt)
        return None
    def verify_guarantees(self):
        graph = self.call_graph()
        names = {name.value: name for name, *_ in self.functions}
        for name, wanted in sorted(self.guarantees.items()):
            if name not in names: continue   # extern claims are trusted, not verified
            for guarantee in sorted(wanted):
                problem = self.violation(name, guarantee, graph)
                if problem:
                    raise Error(f'fn {name} is declared {guarantee} but {problem}', names[name],
                                hint=f'remove `{guarantee}` from fn {name}, or remove the operation named above')
    def compile(self):
        chunks = ['// Generated by AXLE ' + VERSION,
                  '#![allow(unused_parens, unused_mut, dead_code, non_camel_case_types, unused_variables, clashing_extern_declarations)]',
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
        if any(kind != 'c' for _, kind, *_ in self.externs.values()):
            chunks.append(bridge_runtime.rust_runtime())
        for sname, fields in self.structs.items():
            body = ', '.join(f'f_{f.value}: {rust_type(t)}' for f, t in fields)
            chunks.append(f'#[derive(Clone, Debug, PartialEq)]\nstruct S_{sname} {{ {body} }}')
            if any(kind != 'c' for _, kind, *_ in self.externs.values()):
                chunks.append(self.json_impl(sname, fields))
        for name, (kind_token, kind, params, result, symbol) in self.externs.items():
            chunks.append(self.extern_wrapper(name, kind_token, kind, params, result, symbol))
        for name, params, result, effects, statements in self.functions:
            self.scopes, self.effects, self.result, self.unsafe, self.loops, self.current = [{}], effects, result, False, 0, name.value
            for param, typ in params:
                if typ == 'unit' or param.value in self.scopes[0]: raise Error('duplicate or unit parameter', param)
                self.scopes[0][param.value] = (typ, False)
            body, returned, _ = self.block(statements)
            if result != 'unit' and not returned: raise Error('not all paths return a value', name)
            arguments = ', '.join('v_' + p.value + ': ' + rust_type(t) for p, t in params)
            chunks.append(f'fn f_{name.value}({arguments}) -> {rust_type(result)} {{\n{body}\n}}')
        if self.test_mode:
            arms = ''.join(f'"{n}" => f_{n}(), ' for n in self.test_names)
            chunks.append('fn main() { let name = std::env::args().nth(1).unwrap_or_default(); match name.as_str() { ' + arms +
                          '_ => { eprintln!("unknown test {}", name); std::process::exit(2); } } }')
        else:
            chunks.append('fn main() { f_main(); }' if 'main' in self.signatures else 'fn main() {}')
        self.verify_guarantees()
        result = RustSource('\n\n'.join(chunks) + '\n')
        result.links, result.natives = tuple(dict.fromkeys(self.links)), tuple(dict.fromkeys(self.natives))
        return result

def compile_source(source):
    return Compiler(source).compile()

class Loaded:
    """A fully resolved AXLE program: all modules, externs and link inputs."""
    def __init__(self):
        self.functions, self.externs, self.links, self.natives, self.modules = [], [], [], [], []
        self.guarantees, self.spans, self.import_edges = {}, {}, []
        self.structs = []

def load_program(path, *, import_base=None):
    """Resolve relative imports once; imported names share one global namespace."""
    loaded = Loaded()
    visited, active = set(), set()
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
        check_edition(parser)
        for token in parser.imports:
            relative = pathlib.Path(token.value[1:-1])
            if relative.is_absolute() or relative.drive or relative.root:
                raise Error('imports must be relative to their source file', token)
            base = pathlib.Path(import_base) if current == root_path and import_base is not None else current.parent
            visit(base / relative, token)
            loaded.import_edges.append((str(current), str((base / relative).resolve())))
        for token, relative in parser.natives:
            native = (current.parent / relative).resolve()
            if not native.is_file(): raise Error('native file not found: ' + relative, token)
            loaded.natives.append(str(native))
        loaded.functions.extend(own_functions)
        loaded.structs.extend(parser.structs)
        loaded.externs.extend(parser.externs)
        loaded.guarantees.update(parser.guarantees)
        loaded.spans.update(parser.spans)
        loaded.links.extend(parser.links)
        loaded.modules.append(str(current))
        active.remove(current)
        visited.add(current)
    visit(path)
    return loaded

def compile_file(path, *, foreign_commands=None, import_base=None, require_main=True):
    loaded = load_program(path, import_base=import_base)
    return Compiler(functions=loaded.functions, foreign_commands=foreign_commands, externs=loaded.externs,
                    links=loaded.links, natives=loaded.natives, guarantees=loaded.guarantees, require_main=require_main, structs=loaded.structs).compile()

def api_report(path):
    """Machine-readable summary of a program's callable surface, for tools and agents."""
    loaded = load_program(path)
    Compiler(functions=loaded.functions, externs=loaded.externs, links=loaded.links, natives=loaded.natives, guarantees=loaded.guarantees, require_main=False, structs=loaded.structs).compile()
    def params(items): return [{'name': p.value, 'type': t} for p, t in items]
    functions = [{'name': n.value, 'kind': 'fn', 'params': params(p), 'result': r, 'effects': sorted(e),
                  'file': n.source, 'line': n.line} for n, p, r, e, _ in loaded.functions]
    for kind_token, kind, name, p, r, e, symbol in loaded.externs:
        functions.append({'name': name.value, 'kind': 'extern', 'abi': kind, 'foreign_name': symbol, 'params': params(p),
                          'result': r, 'effects': sorted(set(e) | ({'io'} if kind != 'c' else set())),
                          'unsafe_to_call': kind == 'c', 'file': name.source, 'line': name.line})
    return {'version': VERSION, 'entry': str(pathlib.Path(path).resolve()), 'modules': loaded.modules,
            'functions': functions, 'links': sorted(set(loaded.links)), 'natives': sorted(set(loaded.natives))}

def build_flags(profile='release'):
    if profile not in {'release', 'native', 'size'}: raise ValueError('unknown build profile ' + profile)
    flags = ['--edition=2021', '-C', 'opt-level=' + ('z' if profile == 'size' else '3'), '-C', 'panic=abort', '-C', 'codegen-units=1']
    if profile == 'native': flags += ['-C', 'target-cpu=native', '-C', 'lto=fat']
    if profile == 'size': flags += ['-C', 'lto=fat', '-C', 'strip=symbols']
    return flags

def compile_native(source, directory):
    """Compile one `native` input to an object file (or pass .o/.a through)."""
    source = pathlib.Path(source)
    if source.suffix in {'.o', '.a'}: return str(source), False
    cpp = source.suffix in {'.cc', '.cpp', '.cxx'}
    obj = pathlib.Path(directory) / (source.stem + f'-{abs(hash(str(source))) % 10**8}' + ('.obj' if sys.platform == 'win32' else '.o'))
    compiler = [shutil.which(name) for name in (('clang++', 'g++', 'c++') if cpp else ('clang', 'gcc', 'cc'))]
    compiler = next((c for c in compiler if c), None)
    if compiler:
        command = [compiler, '-O2', '-c', str(source), '-o', str(obj)] + ([] if sys.platform == 'win32' else ['-fPIC'])
    elif shutil.which('cl'):
        command = ['cl', '/nologo', '/O2', '/c', str(source), '/Fo' + str(obj)]  # best effort; not verified
    else:
        raise RuntimeError(f'a {"C++" if cpp else "C"} compiler (gcc, clang, or cl) is required to build native "{source.name}"')
    process = subprocess.run(command, capture_output=True, text=True, cwd=directory)
    if process.returncode: raise RuntimeError(f'native compilation of {source.name} failed:\n' + process.stdout + process.stderr)
    return str(obj), cpp

def build(rust_source, output, profile='release', link_search=(), link_args=()):
    rustc = shutil.which('rustc')
    if not rustc: raise RuntimeError('rustc is required for build/run; install a Rust toolchain')
    output = pathlib.Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='axle-') as directory:
        path = pathlib.Path(directory) / 'program.rs'
        path.write_text(rust_source, encoding='utf-8')
        extra, needs_cpp = [], False
        for native in getattr(rust_source, 'natives', ()):
            obj, cpp = compile_native(native, directory)
            extra += ['-C', 'link-arg=' + obj]
            needs_cpp = needs_cpp or cpp
        if needs_cpp and sys.platform != 'win32': extra += ['-C', 'link-arg=-lstdc++']
        for directory_name in link_search: extra += ['-L', str(directory_name)]
        for library in getattr(rust_source, 'links', ()): extra += ['-l', library]
        for argument in link_args: extra += ['-C', 'link-arg=' + argument]
        process = subprocess.run([rustc, str(path), *build_flags(profile), *extra, '-o', str(output)], capture_output=True, text=True)
        if process.returncode: raise RuntimeError('native backend failed:\n' + process.stderr)
    return output

COMMAND_CHOICES = ['check', 'emit', 'build', 'run', 'test', 'api', 'adopt', 'characterize', 'lens', 'audit', 'slice', 'map', 'manifest', 'explain']

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=VERSION)
    parser.add_argument('command', choices=COMMAND_CHOICES)
    parser.add_argument('source', type=pathlib.Path, nargs='?', help='an .axl file (a directory for `adopt`, a code for `explain`)')
    parser.add_argument('-o', '--output', type=pathlib.Path)
    parser.add_argument('--json', action='store_true', help='machine-readable compiler diagnostics')
    parser.add_argument('--strict', action='store_true', help='also fail on patterns that usually hide failures (see `axle explain E080`)')
    parser.add_argument('--filter', help='test: only run tests whose name contains this text')
    parser.add_argument('--include-tests', action='store_true', help='adopt: also scan test, example, docs and benchmark directories')
    parser.add_argument('--check', action='store_true', help='adopt: fail if the existing bindings are out of date')
    parser.add_argument('--line', type=int, help='lens: source line to explain')
    parser.add_argument('--fn', help='slice: function to extract context for')
    parser.add_argument('--file', type=pathlib.Path, help='lens: module the line belongs to (default: the entry file)')
    parser.add_argument('--depth', type=int, default=0, help='slice: also include callee bodies to this depth')
    parser.add_argument('-L', '--link-search', action='append', default=[], metavar='DIR', help='directory searched for `link` libraries')
    parser.add_argument('--link-arg', action='append', default=[], metavar='ARG', help='extra linker argument')
    parser.add_argument('--profile', choices=['release', 'native', 'size'], default='release')
    parser.add_argument('--args', nargs=argparse.REMAINDER, default=[], help='arguments passed to run executable; must be last')
    options = parser.parse_args(argv)
    try:
        if options.output and options.output.resolve() == options.source.resolve():
            raise RuntimeError('output must not overwrite source')
        if options.command == 'adopt':
            import adopt
            if options.check:
                ok, added, removed, problem = adopt.check(options.source, options.output)
                if problem: raise Error(problem)
                if ok: print('bindings are up to date')
                else:
                    print('bindings are stale:')
                    for line in added: print('  + ' + line + '   (new in the source)')
                    for line in removed: print('  - ' + line + '   (gone or changed in the source)')
                    print('run `axle adopt` to refresh')
                return 0 if ok else 1
            report = adopt.run(options.source, options.output, options.include_tests)
            if options.json: print(json.dumps(report))
            else:
                langs = ', '.join(f'{n} {k}' for k, n in sorted(report['bound_by_language'].items())) or 'nothing'
                print(f'adopted {report["bound"]} functions ({langs}); skipped {report["skipped_count"]} (see reasons in axle.map.json)')
                print('bindings: ' + report['bindings_file'])
                print('map:      ' + str(pathlib.Path(report['bindings_file']).with_name('axle.map.json')))
            return 0
        if options.command == 'characterize':
            import characterize
            summary = characterize.run(options.source, options.output, options.link_search, options.link_arg)
            if options.json: print(json.dumps(summary))
            else:
                print(f'recorded {summary["recorded_assertions"]} assertions for {summary["functions"]} functions -> {summary["tests_file"]}')
                for a in summary['aborts']: print(f'  aborts: {a["function"]}({", ".join(a["inputs"])}): {a["detail"]}')
                print('run `axle test ' + summary['tests_file'] + '` to replay them')
            return 0
        try:
            if options.command in {'manifest', 'explain'}:
                import manifest
                return manifest.run(options.command, options)
            if options.source is None:
                raise Error(f'{options.command} requires a source argument', hint='see `axle manifest` for command usage')
            if options.command == 'test':
                import testing
                return testing.run(options)
            if options.command in {'lens', 'audit', 'slice', 'map'}:
                import lens
                return lens.run(options.command, options)
            if options.command == 'api':
                print(json.dumps(api_report(options.source), indent=2))
                return 0
            rust = compile_file(options.source, require_main=options.command in {'build', 'run', 'emit'})
            if options.strict and options.command in {'check', 'build', 'run', 'emit'}:
                import lens
                lens.enforce_strict(options.source)
        except RecursionError:
            raise Error('source nesting exceeds compiler limit') from None
        if options.command == 'emit':
            if options.output: options.output.write_text(rust, encoding='utf-8')
            else: print(rust, end='')
        elif options.command == 'build':
            output = options.output or options.source.with_suffix('.exe' if sys.platform == 'win32' else '.bin')
            if output.resolve() == options.source.resolve(): raise RuntimeError('output must not overwrite source')
            build(rust, output, options.profile, options.link_search, options.link_arg)
        elif options.command == 'run':
            with tempfile.TemporaryDirectory(prefix='axle-run-') as directory:
                executable = build(rust, pathlib.Path(directory) / ('program.exe' if sys.platform == 'win32' else 'program'), options.profile, options.link_search, options.link_arg)
                return subprocess.run([str(executable), *options.args]).returncode
        if options.json and options.command != 'run': print(json.dumps({'ok': True, 'version': VERSION}), file=sys.stderr if options.command == 'emit' and not options.output else sys.stdout)
        elif options.command == 'check': print('OK')
        return 0
    except (Error, OSError, RuntimeError, ValueError) as error:
        diagnostic = {'ok': False, 'message': str(error)}
        if isinstance(error, Error):
            diagnostic.update(line=error.line, column=error.column, code=error.code)
            if error.hint: diagnostic['hint'] = error.hint
            if error.suggestion: diagnostic['suggestion'] = error.suggestion
        if isinstance(error, Error) and error.source: diagnostic['source'] = error.source
        if options.json: print(json.dumps(diagnostic), file=sys.stderr)
        else:
            print(f'{diagnostic.get("source", options.source)}:{diagnostic.get("line", 1)}:{diagnostic.get("column", 1)}: {error}', file=sys.stderr)
            if diagnostic.get('hint'): print('  hint: ' + diagnostic['hint'], file=sys.stderr)
        return 1

if __name__ == '__main__':
    # Run the importable module, so helper modules (lens, manifest, ...) share one Error class with the CLI.
    import axle as _axle
    sys.exit(_axle.main())
