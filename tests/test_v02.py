import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import axle

ROOT = pathlib.Path(__file__).resolve().parents[1]

class AdaptivityTests(unittest.TestCase):
    def test_inference_still_checks_types(self):
        axle.compile_source('fn main() -> unit {var x = 1u64; x = 2u64; assert(x == 2u64);}')
        with self.assertRaisesRegex(axle.Error, 'expected u64'):
            axle.compile_source('fn main() -> unit {var x = 1u64; x = 2;}')
        with self.assertRaisesRegex(axle.Error, 'unit variables'):
            axle.compile_source('fn main() -> unit {let x = assert(true);}')
    def test_buffer_checks(self):
        for source, message in [
            ('fn main() -> unit {let b = buf_new(1u64);}', 'declare mem'),
            ('fn main() -> unit ![mem] {let b = buf_new(1);}', 'requires'),
            ('fn main() -> unit ![mem] {let b = buf_new(1u64); buf_set(b, 0u64, true);}', 'requires'),
            ('fn main() -> unit ![mem] {var b = buf_new(1u64); unsafe {let p = addr(b);}}', 'mutable scalar'),
            ('fn main() -> unit ![mem] {let b = buf_new(1u64); assert(b == b);}', 'cannot be compared'),
            ('fn f(p: *buf_u64) -> unit {} fn main() -> unit {}', 'pointer to a scalar'),
        ]:
            with self.subTest(source=source), self.assertRaisesRegex(axle.Error, message): axle.compile_source(source)
    def test_import_dedup_cycle_and_source_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root/'math.axl').write_text('fn f() -> u64 {return 7u64;}')
            (root/'middle.axl').write_text('import "math.axl";')
            main = root/'main.axl'
            main.write_text('import "math.axl"; import "middle.axl"; fn main() -> unit {assert(f() == 7u64);}')
            self.assertEqual(axle.compile_file(main).count('fn f_f('), 1)
            (root/'middle.axl').write_text('import "main.axl";')
            with self.assertRaisesRegex(axle.Error, 'cyclic import'): axle.compile_file(main)
            (root/'middle.axl').write_text('fn wrong() -> u64 {return true;}')
            try: axle.compile_file(main)
            except axle.Error as error:
                self.assertEqual(error.source, str((root/'middle.axl').resolve()))
            else: self.fail('expected imported type error')
            main.write_text('import "missing.axl"; fn main() -> unit {}')
            with self.assertRaisesRegex(axle.Error, 'cannot read module'): axle.compile_file(main)
    def test_source_only_api_explains_imports(self):
        with self.assertRaisesRegex(axle.Error, 'compile_file'):
            axle.compile_source('import "math.axl"; fn main() -> unit {}')
    def test_buffer_native_semantics(self):
        source = '''
fn make() -> buf_u64 ![mem] {return buf_new(3u64);}
fn write(b: buf_u64) -> unit ![mem] {buf_set(b, 0u64, 18446744073709551615u64);}
fn main() -> unit ![mem] {
    let b = make(); let alias = b; write(alias);
    buf_set(b, 1u64, buf_get(b, 1u64) + 1u64);
    assert(buf_sum(b) == 0u64);
    assert(buf_len(b) == 3u64);
    var replacement = b; replacement = buf_iota(5u64);
    assert(buf_sum(replacement) == 10u64);
    assert(buf_get(b, 0u64) == 18446744073709551615u64);
    assert(buf_len(buf_new(0u64)) == 0u64);
    assert(buf_sum(buf_iota(0u64)) == 0u64);
}
'''
        with tempfile.TemporaryDirectory() as directory:
            exe = axle.build(axle.compile_source(source), pathlib.Path(directory)/'buffer.exe')
            result = subprocess.run([str(exe)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
    def test_buffer_bounds_abort(self):
        with tempfile.TemporaryDirectory() as directory:
            for operation in ['buf_get(b, 1u64)', 'buf_get(b, 18446744073709551615u64)']:
                source = f'fn main() -> unit ![mem] {{let b = buf_new(1u64); let x = {operation}; assert(x == 0u64);}}'
                exe = axle.build(axle.compile_source(source), pathlib.Path(directory)/'bounds.exe')
                result = subprocess.run([str(exe)], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
    def test_profiles_and_adaptive_example(self):
        self.assertIn('target-cpu=native', axle.build_flags('native'))
        self.assertNotIn('target-cpu=native', axle.build_flags('release'))
        self.assertIn('opt-level=z', axle.build_flags('size'))
        with self.assertRaises(ValueError): axle.build_flags('invalid')
        rust = axle.compile_file(ROOT/'examples/adaptive.axl')
        with tempfile.TemporaryDirectory() as directory:
            for profile in ['release', 'native', 'size']:
                exe = axle.build(rust, pathlib.Path(directory)/(profile+'.exe'), profile)
                result = subprocess.run([str(exe)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), '499542')

if __name__ == '__main__': unittest.main()
