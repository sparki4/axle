import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HAVE_NODE = bool(shutil.which('node'))
HAVE_CC = any(shutil.which(c) for c in ('gcc', 'clang', 'cc'))


def run(source, command='run', extra=None):
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / 'main.axl'
        path.write_text(source, encoding='utf-8')
        for name, text in (extra or {}).items():
            (pathlib.Path(d) / name).write_text(text, encoding='utf-8')
        env = dict(os.environ, AXLE_PYTHON=sys.executable)
        return subprocess.run([sys.executable, str(ROOT / 'axle.py'), command, str(path)], capture_output=True, text=True, env=env)


def lines(source, extra=None):
    result = run(source, extra=extra)
    assert result.returncode == 0, result.stderr
    return result.stdout.split('\n')[:-1]


class StrTests(unittest.TestCase):
    def test_literals_escapes_concat_compare(self):
        out = lines('fn main() -> unit ![io, mem] { let a = "x\\ty"; print(a + "\\"q\\""); print(a == "x\\ty"); print("a" < "b"); print(str_len("héllo")); }')
        self.assertEqual(out, ['x\ty"q"', 'true', 'true', '6'])

    def test_functions(self):
        out = lines('fn main() -> unit ![io, mem] { print(str_slice("hello", 1u64, 3u64)); print(str_find("hello", "ll")); print(str_find("hello", "z")); '
                    'print(str_upper("ab")); print(str_from_i64(-5) + str_from_f64(1.5)); print(str_parse_i64(" 12 ", 0)); print(str_parse_i64("zz", 9)); print(str_trim("  x ")); }')
        self.assertEqual(out, ['el', '2', '-1', 'AB', '-51.5', '12', '9', 'x'])

    def test_strings_are_values_not_shared(self):
        out = lines('fn main() -> unit ![io, mem] { var a = "x"; let b = a; a = a + "y"; print(a); print(b); }')
        self.assertEqual(out, ['xy', 'x'])

    def test_concat_needs_mem_effect(self):
        result = run('fn f() -> str { return "a" + "b"; }\nfn main() -> unit { }', 'check')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('mem', result.stderr)

    def test_noalloc_rejects_string_literals(self):
        result = run('fn f() -> u64 noalloc { return str_len("abc"); }\nfn main() -> unit { }', 'check')
        self.assertNotEqual(result.returncode, 0)

    def test_slice_out_of_range_aborts(self):
        result = run('fn main() -> unit ![io, mem] { print(str_slice("ab", 0u64, 5u64)); }')
        self.assertNotEqual(result.returncode, 0)

    def test_bad_escape_and_operators(self):
        self.assertIn('unknown escape', run('fn main() -> unit { let a = "\\q"; }', 'check').stderr)
        self.assertIn('not defined for str', run('fn main() -> unit { let a = "a" - "b"; }', 'check').stderr)

    def test_python_bridge_unicode_and_spaces(self):
        out = lines('extern "py:m.py" fn shout(s: str) -> str;\nextern "py:m.py" fn n(s: str) -> i64;\n'
                    'fn main() -> unit ![io] { print(shout("ünï cödé  x")); print(n("")); print(n("a b")); }',
                    extra={'m.py': 'def shout(s: str) -> str:\n    return s.upper() + "!"\ndef n(s: str) -> int:\n    return len(s)\n'})
        self.assertEqual(out, ['ÜNÏ CÖDÉ  X!', '0', '3'])

    @unittest.skipUnless(HAVE_NODE, 'node not installed')
    def test_js_bridge(self):
        out = lines('extern "js:m.js" fn rev(s: str) -> str;\nfn main() -> unit ![io] { print(rev("abc d")); }',
                    extra={'m.js': 'exports.rev = (s) => s.split("").reverse().join("");\n'})
        self.assertEqual(out, ['d cba'])

    def test_c_strings(self):
        out = lines('extern "c" fn strlen(s: c_str) -> c_size;\nfn len(s: str) -> u64 ![io] { unsafe { return strlen(s); } }\n'
                    'fn main() -> unit ![io] { print(len("hello")); }')
        self.assertEqual(out, ['5'])

    def test_c_rejects_plain_str(self):
        result = run('extern "c" fn strlen(s: str) -> c_size;\nfn main() -> unit { }', 'check')
        self.assertIn('c_str', result.stderr)

    @unittest.skipUnless(HAVE_CC, 'no C compiler')
    def test_adopt_and_characterize_strings(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / 't.py').write_text('def slug(t: str) -> str:\n    return t.strip().lower().replace(" ", "-")\n')
            (root / 't.c').write_text('int count_a(const char *s) { int n = 0; for (; *s; s++) if (*s == \'a\') n++; return n; }\nvoid fill(char *b) { b[0] = 0; }\n')
            env = dict(os.environ, AXLE_PYTHON=sys.executable)
            cli = lambda *a: subprocess.run([sys.executable, str(ROOT / 'axle.py'), *a], capture_output=True, text=True, env=env)
            self.assertEqual(cli('adopt', d).returncode, 0)
            bindings = (root / '.axle' / 'bindings.axl').read_text()
            self.assertIn('fn count_a(s: str) -> i64', bindings)
            self.assertNotIn('fill', bindings)           # a mutable char* is never adopted
            self.assertEqual(cli('characterize', d).returncode, 0)
            tests = root / '.axle' / 'characterization.test.axl'
            self.assertIn('assert(slug("Hello, World") == "hello,-world");', tests.read_text())
            result = cli('test', str(tests))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
