import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def run(source, command='run', extra=None):
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / 'main.axl'
        path.write_text(source, encoding='utf-8')
        for name, text in (extra or {}).items():
            (pathlib.Path(d) / name).write_text(text, encoding='utf-8')
        env = dict(os.environ, AXLE_PYTHON=sys.executable)
        return subprocess.run([sys.executable, str(ROOT / 'axle.py'), command, str(path)], capture_output=True, text=True, env=env)


def output(body, extra=None):
    decl = 'extern "py:m.py" fn scale(x: f64, n: i64) -> f64;\nextern "py:m.py" fn half(n: i64) -> f64;\nextern "py:m.py" fn whole() -> f64;\n' if extra else ''
    result = run(decl + 'fn main() -> unit ![io] {\n' + body + '\n}', extra=extra)
    assert result.returncode == 0, result.stderr
    return result.stdout.split()


class F64Tests(unittest.TestCase):
    def test_arithmetic_and_print(self):
        self.assertEqual(output('print(1.5 + 2f64 * 0.25); print(-(1.0 / 4.0)); print(7.5 % 2.0); print(1e3);'), ['2.0', '-0.25', '1.5', '1000.0'])

    def test_ieee_edge_cases_do_not_abort(self):
        self.assertEqual(output('print(1.0 / 0.0); print(f64_is_nan(0.0 / 0.0)); print(0.0 / 0.0 == 0.0 / 0.0);'), ['inf', 'true', 'false'])

    def test_comparisons_and_functions(self):
        self.assertEqual(output('print(1.5 < 2.5); print(f64_sqrt(9.0)); print(f64_pow(2.0, 10.0)); print(f64_floor(2.9));'), ['true', '3.0', '1024.0', '2.0'])

    def test_explicit_conversions(self):
        self.assertEqual(output('print(f64_to_i64(2.9)); print(f64_to_i64(-2.9)); print(f64_from_u64(3u64)); print(f64_to_u64(-1.0)); print(f64_to_i64(1e30));'),
                         ['2', '-2', '3.0', '0', '9223372036854775807'])

    def test_function_parameters_and_results(self):
        result = run('fn hyp(a: f64, b: f64) -> f64 { return f64_sqrt(a * a + b * b); }\nfn main() -> unit ![io] { print(hyp(3.0, 4.0)); }')
        self.assertEqual(result.stdout.split(), ['5.0'])

    def test_no_implicit_conversion(self):
        result = run('fn main() -> unit { let a = 1.5 + 2; }', 'check')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('expected f64, got i64', result.stderr)

    def test_bit_operators_rejected(self):
        result = run('fn main() -> unit { let a = 1.5 & 2.0; }', 'check')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not defined for f64', result.stderr)

    def test_literal_out_of_range(self):
        self.assertNotEqual(run('fn main() -> unit { let a = 1e999f64; }', 'check').returncode, 0)

    def test_python_bridge_carries_floats(self):
        out = output('print(scale(2.5, 3)); print(half(7)); print(whole());',
                     extra={'m.py': 'def scale(x: float, n: int) -> float:\n    return x * n\ndef half(n: int) -> float:\n    return n / 2\ndef whole() -> float:\n    return 4.0\n'})
        self.assertEqual(out, ['7.5', '3.5', '4.0'])


if __name__ == '__main__':
    unittest.main()
