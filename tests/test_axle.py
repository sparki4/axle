import pathlib
import random
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import axle

class LanguageTests(unittest.TestCase):
    def good(self, body, effects=''):
        return axle.compile_source(f'fn main() -> unit {effects} {{ {body} }}')
    def bad(self, source, message):
        with self.assertRaisesRegex(axle.Error, message):
            axle.compile_source(source)
    def test_types_and_wrapping(self):
        self.assertIn('wrapping_add', self.good('let x: u64 = 18446744073709551615u64 + 1u64;'))
    def test_rejections(self):
        cases = [
            ('let x: u64 = 1;', 'expected u64, got i64'),
            ('let x: i64 = 1; x = 2;', 'immutable'),
            ('var x: i64 = 1; var x: i64 = 2;', 'duplicate variable'),
            ('print(1);', 'declare io'),
            ('let x: i64 = missing;', 'unknown variable'),
            ('if 1 {}', 'expected bool'),
            ('return; print(1);', 'unreachable'),
            ('let x: i64 = 9223372036854775808;', 'out of range'),
            ('let x: u64 = 18446744073709551616u64;', 'out of range'),
            ('let x: bool = true < false;', 'integer operands'),
            ('let x: i64 = 2 + true;', 'expected i64'),
            ('store(1, 2);', 'unsafe block'),
            ('let x: i64 = 1; unsafe { addr(x); }', 'declare mem'),
            ('1 + 2;', 'expected unit'),
            ('foo();', 'unknown function'),
            ('while true {', 'expected'),
            ('@', 'unexpected character'),
        ]
        for body, message in cases:
            with self.subTest(body=body):
                self.bad('fn main() -> unit { ' + body + ' }', message)
    def test_function_rules(self):
        cases = [
            ('fn other() -> unit {}', 'requires fn main'),
            ('fn main(x: i64) -> unit {}', 'requires fn main'),
            ('fn main() -> unit {} fn main() -> unit {}', 'duplicate'),
            ('fn f() -> i64 { if true { return 1; } } fn main() -> unit {}', 'not all paths'),
            ('fn f() -> unit ![io] {print(1);} fn main() -> unit {f();}', 'declare io'),
            ('fn f(x: i64) -> unit {} fn main() -> unit { f(1u64); }', 'arguments must'),
            ('fn main() -> unit ![io, io] {}', 'unique'),
            ('fn f(x: unit) -> unit {} fn main() -> unit {}', 'unit parameter'),
        ]
        for source, message in cases:
            with self.subTest(source=source): self.bad(source, message)
    def test_pointer_rules(self):
        cases = [
            ('var x: i64 = 1; let p: *i64 = addr(x);', 'unsafe block'),
            ('let x: i64 = 1; unsafe { let p: *i64 = addr(x); }', 'mutable scalar'),
            ('unsafe { load(1); }', 'typed pointer'),
            ('var x: i64 = 1; unsafe { let p: *i64 = addr(x); store(p, true); }', 'expected i64'),
            ('unsafe { var x: i64 = 1; } print(x);', 'unknown variable'),
            ('let p: *u64 = alloc_u64(1u64);', 'unsafe block'),
            ('unsafe { let p: *u64 = alloc_u64(1); }', 'u64 count'),
            ('unsafe { free_u64(1u64, 1u64); }', 'original u64 count'),
            ('unsafe { let p: *u64 = alloc_u64(1u64); let q: *u64 = offset(p, 1); }', 'u64 offset'),
        ]
        for body, message in cases:
            with self.subTest(body=body): self.bad('fn main() -> unit ![mem, io] {' + body + '}', message)
    def test_diagnostic_location(self):
        try: self.good('\n  print(1);')
        except axle.Error as error:
            self.assertEqual((error.line, error.column), (2, 3))
        else: self.fail('expected an error')
    def test_deterministic(self):
        self.assertEqual(self.good('assert(true);'), self.good('assert(true);'))
    def test_native_semantics_and_differential_arithmetic(self):
        rng = random.Random(719)
        checks = [
            'assert(18446744073709551615u64 + 1u64 == 0u64);',
            'assert(-9223372036854775808i64 / -1i64 == -9223372036854775808i64);',
            'assert(-9223372036854775808i64 % -1i64 == 0i64);',
            'assert(1u64 << 64u64 == 1u64);',
            'assert(-8i64 >> 2i64 == -2i64);',
            'assert(1u64 << 18446744073709551615u64 == 9223372036854775808u64);',
            'assert((false && touch()) == false);',
            'assert(true || touch());',
            'var x: u64 = 41u64; unsafe { let p: *u64 = addr(x); bump(p); } assert(x == 42u64);',
            'let before: i64 = 7; if true { let before: i64 = before + 1; assert(before == 8); } assert(before == 7);',
            'assert(choose(true) == 3); assert(choose(false) == 4);',
            'assert(fact(10u64) == 3628800u64);',
            'var value: u64 = 0u64; var count: u64 = 0u64; unsafe { let p: *u64 = addr(value); let c: *u64 = addr(count); store(locate(p, c), observe(c)); } assert(value == 1u64);',
        ]
        mask = 2**64 - 1
        for _ in range(60):
            a, b = rng.getrandbits(64), rng.getrandbits(64)
            for op, expected in [('+', (a+b)&mask), ('-', (a-b)&mask), ('*', (a*b)&mask), ('^', a^b), ('<<', (a << (b&63))&mask), ('>>', a >> (b&63))]:
                checks.append(f'assert(({a}u64 {op} {b}u64) == {expected}u64);')
        for _ in range(30):
            a, b = rng.randrange(-2**63, 2**63), rng.randrange(-2**63, 2**63)
            def signed(x): return ((x + 2**63) % 2**64) - 2**63
            quotient = (abs(a) // abs(b)) * (-1 if (a < 0) != (b < 0) else 1)
            for op, expected in [('+', signed(a+b)), ('-', signed(a-b)), ('*', signed(a*b)), ('/', quotient), ('%', a - quotient*b)]:
                checks.append(f'assert((({a}i64) {op} ({b}i64)) == ({expected}i64));')
        helper = '''
fn touch() -> bool { assert(false); return true; }
fn bump(p: *u64) -> unit ![mem] { unsafe {store(p, load(p) + 1u64);} }
fn choose(b: bool) -> i64 { if b {return 3;} else {return 4;} }
fn fact(n: u64) -> u64 { if n == 0u64 {return 1u64;} else {return n * fact(n - 1u64);} }
fn locate(p: *u64, c: *u64) -> *u64 ![mem] { unsafe {store(c, 1u64);} return p; }
fn observe(c: *u64) -> u64 ![mem] { unsafe {return load(c);} }
'''
        rust = axle.compile_source(helper + 'fn main() -> unit ![mem] {' + '\n'.join(checks) + '}')
        with tempfile.TemporaryDirectory() as directory:
            exe = axle.build(rust, pathlib.Path(directory) / 'test.exe')
            result = subprocess.run([str(exe)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
    def test_native_examples(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            for name, expected, args in [('hello', '144', []), ('memory', '42', []), ('hash', '1', ['0']), ('buffer', '499500', [])]:
                with self.subTest(name=name):
                    rust = axle.compile_source((root / 'examples' / (name + '.axl')).read_text())
                    exe = axle.build(rust, pathlib.Path(directory) / (name + '.exe'))
                    result = subprocess.run([str(exe), *args], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), expected)
    def test_division_by_zero_fails_at_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            rust = self.good('let zero: u64 = arg_u64(1u64); print(1u64 / zero);', '![io]')
            exe = axle.build(rust, pathlib.Path(directory) / 'zero.exe')
            result = subprocess.run([str(exe), '0'], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
    def test_cli_json_and_source_protection(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        import json
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / 'program.axl'
            text = 'fn main() -> unit { print(1); }'
            source.write_text(text)
            result = subprocess.run([sys.executable, str(root / 'axle.py'), 'check', str(source), '--json'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stderr)['message'], 'function must declare io effect')
            source.write_text('fn main() -> unit {}')
            for command in ['build', 'emit']:
                result = subprocess.run([sys.executable, str(root / 'axle.py'), command, str(source), '-o', str(source)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(source.read_text(), 'fn main() -> unit {}')

if __name__ == '__main__': unittest.main()
