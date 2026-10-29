import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import axle
import adopt

ROOT = pathlib.Path(__file__).resolve().parents[1]
HAVE_CC = any(shutil.which(c) for c in ('gcc', 'clang', 'cc'))
HAVE_NODE = bool(shutil.which('node'))


def run_source(directory, source, name='main.axl'):
    path = pathlib.Path(directory) / name
    path.write_text(source, encoding='utf-8')
    env = dict(os.environ, AXLE_PYTHON=sys.executable)
    return subprocess.run([sys.executable, str(ROOT / 'axle.py'), 'run', str(path)],
                          capture_output=True, text=True, env=env)


class LanguageTests(unittest.TestCase):
    def test_else_if_break_continue_bitnot(self):
        with tempfile.TemporaryDirectory() as d:
            result = run_source(d, '''
fn main() -> unit ![io] {
    var i = 0;
    while true {
        i = i + 1;
        if i < 3 { continue; } else if i == 5 { break; } else { print(i); }
    }
    print(~0);
    print(~0u64);
}''')
            self.assertEqual(result.stdout.split(), ['3', '4', '-1', '18446744073709551615'], result.stderr)

    def test_loop_control_rejections(self):
        for body, message in [('break;', 'outside of a while'), ('continue;', 'outside of a while'),
                              ('while true { break; print(1); }', 'unreachable')]:
            with self.subTest(body=body), self.assertRaisesRegex(axle.Error, message):
                axle.compile_source('fn main() -> unit ![io] { ' + body + ' }')

    def test_return_through_loop_does_not_leak(self):
        with self.assertRaisesRegex(axle.Error, 'not all paths return'):
            axle.compile_source('fn f() -> i64 { while true { break; } } fn main() -> unit {}')

    def test_diagnostic_codes_and_hints(self):
        with self.assertRaises(axle.Error) as caught:
            axle.compile_source('fn helper() -> unit {} fn main() -> unit { helpr(); }')
        self.assertEqual((caught.exception.code, caught.exception.suggestion), ('E011', 'helper'))
        with self.assertRaises(axle.Error) as caught:
            axle.compile_source('fn main() -> unit { print(1); }')
        self.assertEqual(caught.exception.code, 'E020')
        self.assertIn('![io]', caught.exception.hint)

    def test_json_diagnostic_fields(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / 'bad.axl'
            path.write_text('fn main() -> unit { let count = 1; print(cuont); }')
            result = subprocess.run([sys.executable, str(ROOT / 'axle.py'), 'check', str(path), '--json'],
                                    capture_output=True, text=True)
            data = json.loads(result.stderr)
            self.assertEqual((data['code'], data['suggestion']), ('E010', 'count'))


class ExternTests(unittest.TestCase):
    def test_extern_validation(self):
        cases = [
            ('extern "c" fn f(x: i64) -> i64; fn main() -> unit ![io] { print(f(1)); }', 'requires unsafe'),
            ('extern "c" fn f(x: i64) -> i64 ![mem]; fn main() -> unit { unsafe { f(1); } }', 'declare'),
            ('extern "py:x.py" fn f(p: *u64) -> i64; fn main() -> unit {}', 'not supported'),
            ('extern "c" fn f(b: buf_u64) -> i64; fn main() -> unit {}', 'not supported'),
            ('extern "ruby:x" fn f() -> i64; fn main() -> unit {}', 'extern kind'),
            ('extern "c" fn print(x: i64) -> i64; fn main() -> unit {}', 'reserved'),
            ('fn main() -> unit { let x: c_int = 1; }', 'expected'),
            ('native "/abs.c"; fn main() -> unit {}', 'relative'),
        ]
        for source, message in cases:
            with self.subTest(source=source), self.assertRaisesRegex(axle.Error, message):
                axle.compile_source(source)

    def test_missing_native_and_module(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / 'a.axl').write_text('native "nope.c"; fn main() -> unit {}')
            with self.assertRaisesRegex(axle.Error, 'native file not found'):
                axle.compile_file(root / 'a.axl')
            (root / 'b.axl').write_text('extern "py:nope.py" fn f() -> i64; fn main() -> unit ![io] { print(f()); }')
            with self.assertRaisesRegex(axle.Error, 'extern module not found'):
                axle.compile_file(root / 'b.axl')

    def test_libc_and_abi_widths(self):
        with tempfile.TemporaryDirectory() as d:
            result = run_source(d, '''
extern "c" fn labs(x: c_long) -> c_long;
extern "c" fn abs(x: c_int) -> c_int;
fn main() -> unit ![io] { unsafe { print(labs(-7)); print(abs(-9)); } }''')
            self.assertEqual(result.stdout.split(), ['7', '9'], result.stderr)

    def test_c_int_argument_range_is_checked(self):
        with tempfile.TemporaryDirectory() as d:
            result = run_source(d, '''
extern "c" fn abs(x: c_int) -> c_int;
fn main() -> unit ![io] { unsafe { print(abs(9999999999)); } }''')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('does not fit c_int', result.stderr)

    @unittest.skipUnless(HAVE_CC, 'needs a C compiler')
    def test_native_c_source_with_pointer(self):
        with tempfile.TemporaryDirectory() as d:
            (pathlib.Path(d) / 'm.c').write_text(
                '#include <stdint.h>\nint64_t add3(int64_t a,int64_t b,int64_t c){return a+b+c;}\n'
                'void fill(uint64_t *p, uint64_t n){for(uint64_t i=0;i<n;i++)p[i]=i*i;}\n')
            result = run_source(d, '''
native "m.c";
extern "c" fn add3(a: i64, b: i64, c: i64) -> i64;
extern "c" fn fill(p: *u64, n: u64) -> unit ![mem];
fn main() -> unit ![io, mem] {
    unsafe {
        print(add3(1, 2, 3));
        let p = alloc_u64(5u64);
        fill(p, 5u64);
        print(load(offset(p, 4u64)));
        free_u64(p, 5u64);
    }
}''')
            self.assertEqual(result.stdout.split(), ['6', '16'], result.stderr)


class BridgeTests(unittest.TestCase):
    def project(self, root):
        legacy = pathlib.Path(root) / 'legacy'
        (legacy / 'pkg').mkdir(parents=True)
        (legacy / 'helper.py').write_text('def half(x):\n    return x // 2\n')
        (legacy / 'stats.py').write_text(
            'import helper\n'
            'def mean2(a: int, b: int) -> int:\n    print("legacy output")\n    return helper.half(a + b)\n'
            'def is_even(n: int) -> bool:\n    return n % 2 == 0\n'
            'def boom(n: int) -> int:\n    return 1 // n\n'
            'def ratio(n: int):\n    return n / 3\n')
        (legacy / 'pkg' / '__init__.py').write_text('')
        (legacy / 'pkg' / 'sib.py').write_text('def g(n):\n    return n * 10\n')
        (legacy / 'pkg' / 'mod.py').write_text('from . import sib\ndef f(n):\n    return sib.g(n) + 1\n')
        (legacy / 'calc.js').write_text(
            'exports.cube = (n) => n * n * n;\nexports.big = () => 2n ** 60n;\n'
            'exports.later = async (n) => { await new Promise(r => setTimeout(r, 2)); return n + 1; };\n')

    def test_python_bridge(self):
        with tempfile.TemporaryDirectory() as d:
            self.project(d)
            result = run_source(d, '''
extern "py:legacy/stats.py" fn mean2(a: i64, b: i64) -> i64;
extern "py:legacy/stats.py" fn is_even(n: i64) -> bool;
extern "py:pkg.mod@legacy" fn f(n: i64) -> i64;
fn main() -> unit ![io] { print(mean2(10, 20)); print(is_even(7)); print(f(4)); }''')
            self.assertEqual(result.stdout.split('\n')[:4], ['legacy output', '15', 'false', '41'], result.stderr)

    def test_python_errors_and_bad_types_abort_clearly(self):
        with tempfile.TemporaryDirectory() as d:
            self.project(d)
            for call, text in [('boom(0)', 'ZeroDivisionError'), ('ratio(3)', 'unsupported return type float')]:
                result = run_source(d, f'''
extern "py:legacy/stats.py" fn boom(n: i64) -> i64;
extern "py:legacy/stats.py" fn ratio(n: i64) -> i64;
fn main() -> unit ![io] {{ print({call}); }}''')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(text, result.stderr)

    def test_unsigned_result_rejects_negative(self):
        with tempfile.TemporaryDirectory() as d:
            self.project(d)
            result = run_source(d, '''
extern "py:legacy/helper.py" fn half(x: i64) -> u64;
fn main() -> unit ![io] { print(half(-10)); }''')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('expected a u64 result', result.stderr)

    @unittest.skipUnless(HAVE_NODE, 'needs Node.js')
    def test_js_bridge_exact_64_bit_and_async(self):
        with tempfile.TemporaryDirectory() as d:
            self.project(d)
            result = run_source(d, '''
extern "js:legacy/calc.js" fn cube(n: i64) -> i64;
extern "js:legacy/calc.js" fn big() -> i64;
extern "js:legacy/calc.js" fn later(n: u64) -> u64;
fn main() -> unit ![io] { print(cube(3)); print(big()); print(later(41u64)); }''')
            self.assertEqual(result.stdout.split(), ['27', '1152921504606846976', '42'], result.stderr)

    def test_worker_is_persistent(self):
        with tempfile.TemporaryDirectory() as d:
            self.project(d)
            (pathlib.Path(d) / 'legacy' / 'counter.py').write_text(
                'count = 0\ndef bump() -> int:\n    global count\n    count += 1\n    return count\n')
            result = run_source(d, '''
extern "py:legacy/counter.py" fn bump() -> i64;
fn main() -> unit ![io] { print(bump()); print(bump()); print(bump()); }''')
            self.assertEqual(result.stdout.split(), ['1', '2', '3'], result.stderr)


class AdoptTests(unittest.TestCase):
    def make_project(self, root):
        root = pathlib.Path(root)
        (root / 'src').mkdir()
        (root / 'node_modules').mkdir()
        (root / 'node_modules' / 'ignored.py').write_text('def nope(a: int) -> int:\n    return a\n')
        (root / 'stats.py').write_text(
            'def mean2(a: int, b: int) -> int:\n    return (a + b) // 2\n'
            'def no_hint(a):\n    return a\n'
            'def _private(a: int) -> int:\n    return a\n'
            'def ratio(a: int) -> float:\n    return a / 2\n'
            'def print(a: int) -> int:\n    return a\n')
        (root / 'util.js').write_text(
            '/** @param {number} a\n * @param {number} b\n * @returns {number} */\n'
            'function add(a, b) { return a + b; }\nconst loose = (x) => x;\nmodule.exports = { add, loose };\n')
        (root / 'src' / 'fast.c').write_text(
            '#include <stdint.h>\nuint64_t sumsq(uint64_t n){uint64_t t=0;for(uint64_t i=0;i<n;i++)t+=i*i;return t;}\n'
            'int clamp(int x,int lo,int hi){return x<lo?lo:(x>hi?hi:x);}\ndouble half(double x){return x/2;}\n'
            'static int hidden(int x){return x;}\n')
        (root / 'src' / 'prog.c').write_text('int main(void){return 0;}\n')
        (root / 'src' / 'lib.rs').write_text('#[no_mangle]\npub extern "C" fn rust_add(a: i64, b: i64) -> i64 { a + b }\n')
        return root

    def test_report_contents(self):
        with tempfile.TemporaryDirectory() as d:
            root = self.make_project(d)
            report = adopt.run(root)
            names = {e['foreign_name'] for e in report['bindings']}
            self.assertTrue({'mean2', 'add', 'sumsq', 'clamp', 'rust_add'} <= names, names)
            self.assertNotIn('nope', names)
            self.assertNotIn('_private', names)
            self.assertNotIn('hidden', names)
            reasons = {(s['name'], s['reason']) for s in report['skipped']}
            skipped_names = {n for n, _ in reasons}
            self.assertTrue({'no_hint', 'ratio', 'loose', 'half', 'main'} <= skipped_names, skipped_names)
            renamed = [e for e in report['bindings'] if e['foreign_name'] == 'print']
            self.assertEqual(renamed[0]['name'], 'py_print')
            statuses = {e['foreign_name']: e['status'] for e in report['bindings']}
            self.assertEqual(statuses['rust_add'], 'needs-link')
            self.assertTrue((root / '.axle' / 'axle.map.json').is_file())

    @unittest.skipUnless(HAVE_CC and HAVE_NODE, 'needs a C compiler and Node.js')
    def test_generated_bindings_compile_and_run(self):
        with tempfile.TemporaryDirectory() as d:
            root = self.make_project(d)
            adopt.run(root)
            result = run_source(root, '''
import ".axle/bindings.axl";
fn main() -> unit ![io] {
    print(mean2(10, 20));
    print(add(40, 2));
    print(sumsq(1000u64));
    print(clamp(99, 0, 10));
}''')
            self.assertEqual(result.stdout.split(), ['15', '42', '332833500', '10'], result.stderr)

    def test_rerun_is_idempotent_and_ignores_own_output(self):
        with tempfile.TemporaryDirectory() as d:
            root = self.make_project(d)
            first = adopt.run(root)
            second = adopt.run(root)
            self.assertEqual(first['bound'], second['bound'])
            self.assertEqual(first['skipped_count'], second['skipped_count'])

    def test_api_report(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / 'p.axl'
            path.write_text('extern "c" fn abs(x: c_int) -> c_int;\nfn twice(x: i64) -> i64 { return x * 2; }\n'
                            'fn main() -> unit ![io] { print(twice(2)); }')
            report = axle.api_report(path)
            by_name = {f['name']: f for f in report['functions']}
            self.assertEqual(by_name['twice']['params'], [{'name': 'x', 'type': 'i64'}])
            self.assertEqual(by_name['main']['effects'], ['io'])
            self.assertTrue(by_name['abs']['unsafe_to_call'])


if __name__ == '__main__':
    unittest.main()
