import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import axle
import adopt
import cobol

ROOT = pathlib.Path(__file__).resolve().parents[1]


def cli(*args, cwd=None):
    return subprocess.run([sys.executable, str(ROOT / 'axle.py'), *map(str, args)], capture_output=True, text=True, cwd=cwd)


def write(directory, name, text):
    path = pathlib.Path(directory) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


class FailLoudTests(unittest.TestCase):
    def test_foreign_idioms_get_hints(self):
        with tempfile.TemporaryDirectory() as d:
            for source, word in [('fn main() -> unit { for i in 0..3 { } }', 'while'),
                                 ('fn main() -> unit { var x = 1; x += 1; }', 'x = x')]:
                result = cli('check', write(d, 'a.axl', source), '--json')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(word, json.loads(result.stderr).get('hint', ''))

    def test_unimplemented_is_caught_by_strict(self):
        with tempfile.TemporaryDirectory() as d:
            path = write(d, 'a.axl', 'fn todo() -> i64 { unimplemented(); }\nfn main() -> unit ![io] { print(todo()); }')
            self.assertEqual(cli('check', path).returncode, 0)
            self.assertNotEqual(cli('check', path, '--strict').returncode, 0)


class TestRunnerTests(unittest.TestCase):
    def test_pass_and_fail_isolated(self):
        with tempfile.TemporaryDirectory() as d:
            path = write(d, 't.axl', 'fn test_ok() -> unit { assert(1 + 1 == 2); }\nfn test_bad() -> unit { assert(1 == 2); }')
            result = cli('test', path)
            self.assertEqual(result.returncode, 1)
            self.assertIn('PASS  test_ok', result.stdout)
            self.assertIn('FAIL  test_bad', result.stdout)


class ManifestTests(unittest.TestCase):
    def test_manifest_lists_every_command(self):
        manifest = json.loads(cli('manifest', '--json').stdout)
        for name in axle.COMMAND_CHOICES:
            self.assertIn(name, manifest['commands'])

    def test_explain_known_code(self):
        result = cli('explain', 'E001')
        self.assertEqual(result.returncode, 0, result.stderr)


class MapTests(unittest.TestCase):
    def test_map_reports_modules(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, 'tax.axl', 'fn rate() -> i64 { return 17; }')
            main = write(d, 'main.axl', 'import "tax.axl";\nfn main() -> unit ![io] { print(rate()); }')
            result = cli('map', main)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('tax', result.stdout)


class LegacyTests(unittest.TestCase):
    def test_cobol_structure(self):
        program = cobol.scan(ROOT / 'examples/cobol/PAYROLL.cbl', 'PAYROLL.cbl')
        self.assertEqual(program['program_id'], 'PAYROLL')
        names = [p['name'] for p in program['paragraphs']]
        self.assertEqual(names, ['MAIN-PARA', 'READ-EMP', 'CALC-PAY'])
        calc = program['paragraphs'][2]
        self.assertIn('40', calc['constants'])
        self.assertIn('WS-PAY', calc['writes'])
        self.assertEqual(program['paragraphs'][0]['performs'], ['READ-EMP'])
        self.assertIn('READ-EMP', program['undocumented_paragraphs'])

    def test_adopt_writes_legacy_notes(self):
        with tempfile.TemporaryDirectory() as d:
            shutil.copy(ROOT / 'examples/cobol/PAYROLL.cbl', d)
            report = adopt.run(d)
            self.assertEqual(len(report['legacy']), 1)
            self.assertTrue((pathlib.Path(d) / '.axle' / 'legacy-notes.md').exists())

    def test_adopt_check_detects_drift(self):
        with tempfile.TemporaryDirectory() as d:
            src = write(d, 'p.py', 'def a(x: int) -> int:\n    return x\n')
            adopt.run(d)
            self.assertTrue(adopt.check(d)[0])
            src.write_text(src.read_text() + 'def b(x: int) -> int:\n    return x\n')
            ok, added, removed, _ = adopt.check(d)
            self.assertFalse(ok)
            self.assertEqual(len(added), 1)
            self.assertEqual(removed, [])
            self.assertEqual(cli('adopt', '--check', d).returncode, 1)

    def test_characterize_replays(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, 'p.py', 'def double(x: int) -> int:\n    return x * 2\n')
            self.assertEqual(cli('adopt', d).returncode, 0)
            result = cli('characterize', d)
            self.assertEqual(result.returncode, 0, result.stderr)
            tests = pathlib.Path(d) / '.axle' / 'characterization.test.axl'
            self.assertIn('assert(double(7) == 14)', tests.read_text())
            self.assertEqual(cli('test', tests).returncode, 0)
            # a behavior change is caught
            (pathlib.Path(d) / 'p.py').write_text('def double(x: int) -> int:\n    return x * 3\n')
            self.assertEqual(cli('test', tests).returncode, 1)


if __name__ == '__main__':
    unittest.main()
