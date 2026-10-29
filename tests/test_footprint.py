import atexit
import json
import pathlib
import random
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import axle
import lens

ROOT = pathlib.Path(__file__).resolve().parents[1]


_DIRECTORIES = []
atexit.register(lambda: [d.cleanup() for d in _DIRECTORIES])


def analyze(source):
    directory = tempfile.TemporaryDirectory()
    _DIRECTORIES.append(directory)
    path = pathlib.Path(directory.name) / 'p.axl'
    path.write_text(source, encoding='utf-8')
    return lens.Analysis(path)


class GuaranteeTests(unittest.TestCase):
    def rejects(self, source, message):
        with self.assertRaisesRegex(axle.Error, message):
            axle.compile_source(source)

    def test_chain_is_reported(self):
        self.rejects('fn d(a: i64, b: i64) -> i64 { return a / b; } fn m(a: i64) -> i64 { return d(a, a); } '
                     'fn g(a: i64) -> i64 nothrow { return m(a); } fn main() -> unit {}',
                     r'declared nothrow but can abort: division by zero .*g -> m -> d')

    def test_literal_nonzero_divisor_is_accepted(self):
        axle.compile_source('fn g(a: i64) -> i64 nothrow noalloc { return a / 2 + a % 3; } fn main() -> unit {}')

    def test_zero_literal_divisor_is_rejected(self):
        self.rejects('fn g(a: i64) -> i64 nothrow { return a / 0; } fn main() -> unit {}', 'division by zero')

    def test_each_abort_source_blocks_nothrow(self):
        cases = {
            'index': 'let b = buf_new(1u64); return buf_get(b, 5u64) > 0u64;',
            'assert': 'assert(x > 0); return true;',
            'print': 'print(x); return true;',
            'arg': 'return arg_u64(1u64) > 0u64;',
            'rawalloc': 'unsafe { let p = alloc_u64(0u64); free_u64(p, 1u64); } return true;',
        }
        for name, body in cases.items():
            with self.subTest(name):
                source = f'fn f(x: i64) -> bool ![io, mem] nothrow {{ {body} }} fn main() -> unit {{}}'
                self.rejects(source, 'declared nothrow')

    def test_unsafe_blocks_nothrow_but_not_noalloc(self):
        self.rejects('fn f() -> unit ![mem] nothrow { var x = 1u64; unsafe { let p = addr(x); store(p, 2u64); } } fn main() -> unit {}',
                     'unsafe memory access')
        axle.compile_source('fn f() -> unit ![mem] noalloc { var x = 1u64; unsafe { let p = addr(x); store(p, 2u64); } } fn main() -> unit {}')

    def test_noalloc(self):
        self.rejects('fn f() -> u64 ![mem] noalloc { let b = buf_new(1u64); return buf_len(b); } fn main() -> unit {}',
                     'declared noalloc but allocates buffer')
        self.rejects('fn h() -> u64 ![mem] { return buf_len(buf_new(1u64)); } fn f() -> u64 ![mem] noalloc { return h(); } fn main() -> unit {}',
                     r'f -> h')

    def test_recursion_is_handled(self):
        axle.compile_source('fn f(n: u64) -> u64 nothrow noalloc { if n == 0u64 { return 0u64; } return f(n - 1u64); } fn main() -> unit {}')
        self.rejects('fn f(n: u64) -> u64 nothrow { if n == 0u64 { return 1u64 / n; } return f(n - 1u64); } fn main() -> unit {}', 'division')

    def test_extern_claims_are_trusted_and_bridges_cannot_claim(self):
        self.rejects('extern "c" fn abs(x: c_int) -> c_int; fn f(x: i64) -> i64 nothrow { unsafe { return abs(x); } } fn main() -> unit {}',
                     'foreign function abs, which AXLE cannot verify')
        axle.compile_source('extern "c" fn abs(x: c_int) -> c_int nothrow noalloc; fn f(x: i64) -> i64 nothrow noalloc { unsafe { return abs(x); } } fn main() -> unit {}')
        self.rejects('extern "py:x.py" fn f() -> i64 nothrow; fn main() -> unit {}', 'cannot claim')

    def test_duplicate_guarantee(self):
        self.rejects('fn f() -> unit nothrow nothrow {} fn main() -> unit {}', 'duplicate guarantee')


class LensTests(unittest.TestCase):
    SOURCE = '''// helper
fn fetch(data: buf_u64, i: u64) -> u64 ![mem] {
    return buf_get(data, i);
}

fn pure(x: i64) -> i64 {
    let y = x * 2;
    return y + 1;
}

fn main() -> unit ![io, mem] {
    let data = buf_iota(4u64);
    let again = data;
    buf_set(again, 0u64, 9u64);
    print(fetch(data, 2u64));
    print(pure(3));
}
'''

    def kinds(self, analysis, line):
        return [f['kind'] for f in analysis.lens(line)['facts']]

    def test_line_facts(self):
        a = analyze(self.SOURCE)
        self.assertEqual(self.kinds(a, 3), ['abort', 'read'])
        self.assertEqual(self.kinds(a, 12), ['alloc'])
        self.assertEqual(self.kinds(a, 13), ['alias'])
        self.assertEqual(self.kinds(a, 14), ['abort', 'write'])
        self.assertIn('call', self.kinds(a, 15))
        self.assertTrue(a.lens(7)['nothing_hidden'])
        self.assertTrue(a.lens(8)['nothing_hidden'])

    def test_call_reports_callee_footprint(self):
        a = analyze(self.SOURCE)
        text = [f['message'] for f in a.lens(15)['facts'] if f['kind'] == 'call'][0]
        self.assertIn('index out of bounds', text)
        pure_line = [f['message'] for f in a.lens(16)['facts'] if f['kind'] == 'call'][0]
        self.assertIn('pure', pure_line)

    def test_outside_function_and_blank(self):
        a = analyze(self.SOURCE)
        self.assertIsNone(a.lens(1)['function'])
        self.assertEqual(a.lens(4)['note'], 'no executable code on this line')

    def test_local_assignment_is_not_external_mutation(self):
        a = analyze('fn f(n: u64) -> u64 { var i = 0u64; while i < n { i = i + 1u64; } return i; } fn main() -> unit {}')
        self.assertEqual(a.summary('f')['writes'], [])
        self.assertTrue(a.summary('f')['may_not_terminate'])

    def test_audit_findings(self):
        a = analyze(self.SOURCE).audit()
        kinds = {(f['kind'], f['function']) for f in a['findings']}
        self.assertIn(('shared-buffer-aliasing', 'main'), kinds)
        pure = [r for r in a['functions'] if r['name'] == 'pure'][0]
        self.assertEqual(set(pure['could_declare']), {'nothrow', 'noalloc'})
        over = analyze('fn f() -> unit ![io, mem] { print(1); } fn main() -> unit ![io, mem] { f(); }').audit()
        self.assertIn(('over-declared-effects', 'f'), {(x['kind'], x['function']) for x in over['findings']})

    def test_suggested_guarantees_are_actually_accepted(self):
        a = analyze(self.SOURCE)
        lines = self.SOURCE.splitlines()
        tried = 0
        for row in a.audit()['functions']:
            for guarantee in row['could_declare']:
                edited = list(lines)
                edited[row['line'] - 1] = edited[row['line'] - 1].replace(' {', f' {guarantee} {{', 1)
                axle.compile_source('\n'.join(edited))
                tried += 1
        self.assertGreaterEqual(tried, 3)

    def test_unsuggested_guarantees_are_really_rejected(self):
        a = analyze(self.SOURCE)
        lines = self.SOURCE.splitlines()
        checked = 0
        for row in a.audit()['functions']:
            if row['kind'] != 'fn' or row['name'] == 'main': continue
            for guarantee in ('nothrow', 'noalloc'):
                if guarantee in row['could_declare'] or guarantee in row['guarantees']: continue
                edited = list(lines)
                edited[row['line'] - 1] = edited[row['line'] - 1].replace(' {', f' {guarantee} {{', 1)
                with self.assertRaises(axle.Error):
                    axle.compile_source('\n'.join(edited))
                checked += 1
        self.assertGreater(checked, 0)

    def test_slice(self):
        a = analyze(self.SOURCE)
        s = a.slice('main', depth=1)
        names = [b['name'] for b in s['bodies']]
        self.assertEqual(names[0], 'main')
        self.assertIn('fetch', names)
        self.assertTrue(s['bodies'][1]['source'].startswith('// helper'))
        self.assertEqual(a.slice('fetch')['callers'], ['main'])
        with self.assertRaises(axle.Error) as caught:
            a.slice('fetsh')
        self.assertEqual(caught.exception.suggestion, 'fetch')

    def test_cli_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / 'p.axl'
            path.write_text(self.SOURCE)
            out = subprocess.run([sys.executable, str(ROOT / 'axle.py'), 'lens', str(path), '--line', '14', '--json'],
                                 capture_output=True, text=True)
            data = json.loads(out.stdout)
            self.assertEqual(data['function'], 'main')
            out = subprocess.run([sys.executable, str(ROOT / 'axle.py'), 'audit', str(path)], capture_output=True, text=True)
            self.assertIn('findings:', out.stdout)
            out = subprocess.run([sys.executable, str(ROOT / 'axle.py'), 'slice', str(path), '--fn', 'main'], capture_output=True, text=True)
            self.assertIn('## called by:', out.stdout)


class SoundnessTests(unittest.TestCase):
    """If the compiler accepts `nothrow`, the program must never abort at runtime."""
    BODIES = [
        'return a + b;',
        'return a / b;',
        'return a % b;',
        'return a / 2 + b % 3;',
        'assert(a != b); return a;',
        'let d = buf_new(4u64); buf_set(d, 3u64, 1u64); return a;',
        'let d = buf_new(4u64); buf_set(d, 7u64, 1u64); return a;',
        'let d = buf_iota(3u64); return a + buf_get(d, 1u64) as_i64;',
        'return a << b;',
        'var i = 0; while i < 3 { i = i + 1; } return a - i;',
        'if a < b { return a; } else { return b; }',
        'return -a;',
        'return ~a;',
        'unsafe { var x = 1u64; let p = addr(x); store(p, 2u64); } return a;',
        'print(a); return b;',
    ]

    def build_program(self, rng):
        functions, count = [], rng.randint(2, 4)
        for k in range(count):
            body = rng.choice(self.BODIES).replace(' as_i64', '').replace('buf_get(d, 1u64)', '0')
            if k > 0 and rng.random() < 0.5:
                body = f'return f{rng.randrange(k)}(a, b) + 1;'
            functions.append(f'fn f{k}(a: i64, b: i64) -> i64 ![io, mem] {{ {body} }}')
        a, b = rng.choice([0, 1, 2, 5]), rng.choice([0, 1, 3, 5])
        return '\n'.join(functions), count, a, b

    def test_accepted_nothrow_never_aborts(self):
        rng = random.Random(20261029)
        accepted = rejected = 0
        with tempfile.TemporaryDirectory() as d:
            for n in range(24):
                body, count, a, b = self.build_program(rng)
                last = count - 1
                source = f'{body}\nfn guarded(a: i64, b: i64) -> i64 ![io, mem] nothrow {{ return f{last}(a, b); }}\n' \
                         f'fn main() -> unit ![io, mem] {{ print(guarded({a}, {b})); }}'
                path = pathlib.Path(d) / f'p{n}.axl'
                path.write_text(source)
                try:
                    axle.compile_file(path)
                except axle.Error:
                    rejected += 1
                    continue
                accepted += 1
                run = subprocess.run([sys.executable, str(ROOT / 'axle.py'), 'run', str(path)], capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, f'accepted as nothrow but aborted:\n{source}\n{run.stderr}')
        self.assertGreater(accepted, 3)
        self.assertGreater(rejected, 3)


if __name__ == '__main__':
    unittest.main()
