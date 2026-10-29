import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HAVE_NODE = bool(shutil.which('node'))


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


HEAD = 'struct Point { x: i64, y: f64 }\nstruct Person { name: str, home: Point }\n'


class StructTests(unittest.TestCase):
    def test_values_fields_assignment(self):
        out = lines(HEAD + '''
fn shift(p: Point, dx: i64) -> Point { return Point { x: p.x + dx, y: p.y }; }
fn main() -> unit ![io] {
    var a = Point { x: 1, y: 2.5 };
    let b = shift(a, 10);
    a.x = 100;
    var who = Person { name: "ann", home: b };
    who.home.y = 9.5;
    print(a.x); print(b.x); print(a == b); print(who.home.y); print(b.y); print(who.name);
}''')
        self.assertEqual(out, ['100', '11', 'false', '9.5', '2.5', 'ann'])

    def test_assignment_copies(self):
        out = lines(HEAD + 'fn main() -> unit ![io] { var a = Point { x: 1, y: 0.0 }; let b = a; a.x = 5; print(b.x); }')
        self.assertEqual(out, ['1'])

    def test_condition_is_not_a_literal(self):
        out = lines(HEAD + 'fn main() -> unit ![io] { let p = Point { x: 1, y: 0.0 }; var k = 1; if p.x == k { print(7); } }')
        self.assertEqual(out, ['7'])

    def test_errors_have_hints(self):
        for source, text in [(HEAD + 'fn main() -> unit { let p = Point { x: 1 }; }', 'missing field y'),
                             (HEAD + 'fn main() -> unit { let p = Point { x: 1, y: 2.0, z: 3 }; }', 'no field z'),
                             (HEAD + 'fn main() -> unit { let p = Point { x: 1.5, y: 2.0 }; }', 'expected i64'),
                             (HEAD + 'fn main() -> unit { let p = Point { x: 1, y: 2.0 }; let q = p.xx; }', 'did you mean `x`'),
                             (HEAD + 'fn f(p: Pint) -> unit { }\nfn main() -> unit { }', 'did you mean `Point`'),
                             ('struct A { b: A }\nfn main() -> unit { }', 'contains itself'),
                             ('struct A { b: buf_u64 }\nfn main() -> unit { }', 'cannot have type'),
                             (HEAD + 'fn main() -> unit { let p = Point { x: 1, y: 2.0 }; let q = p < p; }', 'operator'),
                             (HEAD + 'fn main() -> unit { let p = Point { x: 1, y: 2.0 }; print(p); }'.replace('-> unit {', '-> unit ![io] {'), 'print requires')]:
            result = run(source, 'check')
            self.assertNotEqual(result.returncode, 0, source)
            self.assertIn(text, result.stderr, source)

    def test_strings_in_structs_are_allocations(self):
        result = run(HEAD + 'fn name(p: Person) -> str noalloc { return p.name; }\nfn main() -> unit { }', 'check')
        self.assertNotEqual(result.returncode, 0)

    def test_scalar_struct_is_noalloc(self):
        result = run(HEAD + 'fn sum(p: Point) -> i64 noalloc nothrow { return p.x; }\nfn main() -> unit { }', 'check')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_python_bridge(self):
        out = lines(HEAD + 'extern "py:m.py" fn move(p: Person) -> Person;\n'
                    'fn main() -> unit ![io] { let r = move(Person { name: "a \\"q\\" ü", home: Point { x: 2, y: 0.5 } }); print(r.name); print(r.home.x); print(r.home.y); }',
                    extra={'m.py': 'def move(p):\n    p["name"] = p["name"].upper()\n    p["home"]["x"] += 40\n    p["home"]["y"] *= 3\n    return p\n'})
        self.assertEqual(out, ['A "Q" Ü', '42', '1.5'])

    @unittest.skipUnless(HAVE_NODE, 'node not installed')
    def test_js_bridge(self):
        out = lines(HEAD + 'extern "js:m.js" fn area(p: Point) -> f64;\nfn main() -> unit ![io] { print(area(Point { x: 3, y: 1.5 })); }',
                    extra={'m.js': 'exports.area = (p) => p.x * p.y;\n'})
        self.assertEqual(out, ['4.5'])

    def test_bridge_result_with_wrong_shape_aborts(self):
        result = run(HEAD + 'extern "py:m.py" fn bad(p: Point) -> Point;\nfn main() -> unit ![io] { let r = bad(Point { x: 1, y: 2.0 }); }',
                     extra={'m.py': 'def bad(p):\n    return {"x": "no"}\n'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('expected a Point object', result.stderr)

    def test_c_cannot_take_structs(self):
        result = run(HEAD + 'extern "c" fn f(p: Point) -> i64;\nfn main() -> unit { }', 'check')
        self.assertIn('cannot take a struct', result.stderr)


if __name__ == '__main__':
    unittest.main()
