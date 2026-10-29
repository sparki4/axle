import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import polyglot as poly

GUEST_BLOCKS = '''
py{print("guest:py")}
js{console.log("guest:js");}
rust{println!("guest:rust");}
c{printf("guest:c\\n");}
cpp{std::cout << "guest:cpp" << std::endl;}
axle{print(42u64);}
'''

class PolyglotTests(unittest.TestCase):
    def test_scanner_strings_comments_and_fences(self):
        text = 'print("js{ignored}")\n# js{ignored}\njs{console.log("}"); /* } */ if (true) { console.log("ok"); }}\n'
        found = list(poly.blocks(text, 'py'))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][2], 'js')
        fenced = 'js<<<END_JS\nconsole.log(/}/.test("}"));\nEND_JS\n'
        self.assertIn('/}/', list(poly.blocks(fenced, 'py'))[0][3])
        self.assertEqual(list(poly.blocks('struct py { int x; };', 'c')), [])
        self.assertEqual(list(poly.blocks("char c = 'x'; // py{no}\n", 'c')), [])
        for text in ['py{print(1)', 'py<<<END\nprint(1)\n', 'py{print("unterminated)}']:
            with self.subTest(text=text), self.assertRaises(poly.PolyError): list(poly.blocks(text, 'py'))
    def test_six_by_six_host_guest_matrix(self):
        for host in ['py', 'js', 'rust', 'c', 'cpp', 'axle']:
            with self.subTest(host=host), tempfile.TemporaryDirectory(prefix='axle-matrix-') as directory:
                root = Path(directory)
                if host == 'py': text = 'print("host:py")\n' + GUEST_BLOCKS
                elif host == 'js': text = 'console.log("host:js");\n' + GUEST_BLOCKS
                elif host == 'rust': text = 'fn main() {\nprintln!("host:rust");\n' + GUEST_BLOCKS + '}\n'
                elif host == 'c': text = '#include <stdio.h>\nint main(void) {\nprintf("host:c\\n");\n' + GUEST_BLOCKS + 'return 0;\n}\n'
                elif host == 'cpp': text = '#include <iostream>\nint main() {\nstd::cout << "host:cpp" << std::endl;\n' + GUEST_BLOCKS + 'return 0;\n}\n'
                else: text = 'fn main() -> unit ![io] {\nprint(99u64);\n' + GUEST_BLOCKS + '}\n'
                source = root / 'source.mix'
                source.write_text(text)
                suffix = poly.HOSTS[host].extension if host in {'py', 'js'} else '.exe'
                argv, bundle = poly.build_project(source, host, root/('result'+suffix))
                result = subprocess.run(argv, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.splitlines(), ['99' if host == 'axle' else 'host:'+host, 'guest:py', 'guest:js', 'guest:rust', 'guest:c', 'guest:cpp', '42'])
                self.assertTrue((bundle/'manifest.json').is_file())
    def test_nested_guests_and_environment(self):
        text = '''
import os
os.environ["AXLE_MESSAGE"] = "hello from parent"
js<<<END_JS
console.log(process.env.AXLE_MESSAGE);
py<<<END_PY
print("nested Python")
END_PY
END_JS
print("parent finished")
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'nested.mix'
            source.write_text(text)
            argv, _ = poly.build_project(source, 'py', root/'nested.py')
            result = subprocess.run(argv, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ['hello from parent', 'nested Python', 'parent finished'])
    def test_guest_failure_stops_host(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'failure.mix'
            source.write_text('py{raise SystemExit(7)}\nprint("must not run")\n')
            argv, _ = poly.build_project(source, 'py', root/'failure.py')
            result = subprocess.run(argv, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('must not run', result.stdout)
    def test_plugins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin = root/'adapter.py'
            plugin.write_text('def register(hosts, guests):\n    guests["custom_python"] = guests["py"]\n    hosts["custom_host"] = hosts["py"]\n')
            try:
                poly.load_plugin(plugin)
                source = root/'custom.mix'
                source.write_text('custom_python{print("custom guest")}\n')
                argv, _ = poly.build_project(source, 'custom_host', root/'custom.py')
                result = subprocess.run(argv, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), 'custom guest')
            finally:
                poly.GUESTS.pop('custom_python', None)
                poly.HOSTS.pop('custom_host', None)
    def test_powershell_guest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'powershell.mix'
            source.write_text('pwsh<<<END_PS\nWrite-Output "PowerShell guest"\nEND_PS\n')
            argv, _ = poly.build_project(source, 'py', root/'powershell.py')
            result = subprocess.run(argv, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'PowerShell guest')
    def test_axle_host_imports_and_nested_guest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'math.axl').write_text('fn f() -> u64 {return 42u64;}')
            source = root/'source.mix'
            source.write_text('import "math.axl";\nfn main() -> unit ![io] {\nprint(f());\naxle<<<END_AXLE\npy{print("nested in AXLE")}\nEND_AXLE\n}\n')
            argv, _ = poly.build_project(source, 'axle', root/'program.exe')
            result = subprocess.run(argv, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ['42', 'nested in AXLE'])

if __name__ == '__main__': unittest.main()
