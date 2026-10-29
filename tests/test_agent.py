import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import mcp_server

ROOT = pathlib.Path(__file__).resolve().parents[1]


def cli(*args):
    return subprocess.run([sys.executable, str(ROOT / 'axle.py'), *map(str, args)], capture_output=True, text=True,
                          env=dict(os.environ, AXLE_PYTHON=sys.executable))


def source(directory, text, name='main.axl'):
    path = pathlib.Path(directory) / name
    path.write_text(text, encoding='utf-8')
    return path


def rpc(*messages):
    stdin = io.StringIO('\n'.join(json.dumps(m) for m in messages) + '\n')
    stdout = io.StringIO()
    mcp_server.serve(stdin, stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


class DiagnosticTests(unittest.TestCase):
    def test_error_shows_source_line_caret_and_conversion(self):
        with tempfile.TemporaryDirectory() as d:
            path = source(d, 'fn main() -> unit { let a = 1.5 + 2; }')
            result = cli('check', path)
            self.assertIn('| fn main() -> unit { let a = 1.5 + 2; }', result.stderr)
            self.assertIn('f64_from_i64', result.stderr)
            data = json.loads(cli('check', path, '--json').stderr)
            self.assertEqual(data['snippet'], 'fn main() -> unit { let a = 1.5 + 2; }')
            self.assertEqual(data['caret'].index('^') + 1, data['column'])

    def test_signedness_conversion_exists_and_wraps(self):
        with tempfile.TemporaryDirectory() as d:
            path = source(d, 'fn main() -> unit ![io] { print(as_u64(-1)); print(as_i64(18446744073709551615u64)); }')
            self.assertEqual(cli('run', path).stdout.split(), ['18446744073709551615', '-1'])

    def test_every_mismatch_hint_names_a_real_builtin(self):
        import axle
        for hint in axle.Compiler.CONVERT.values():
            name = hint.split('(')[0]
            if name.isidentifier() and '_' in name:
                self.assertIn(name, axle.BUILTINS, hint)


class BriefTests(unittest.TestCase):
    def test_file_brief(self):
        with tempfile.TemporaryDirectory() as d:
            source(d, '/// Doubles a number.\n/// why: marketing asked for it.\nfn twice(x: i64) -> i64 { return x * 2; }\nfn main() -> unit ![io] { print(twice(2)); }')
            result = cli('brief', pathlib.Path(d) / 'main.axl', '--json')
            data = json.loads(result.stdout)
            self.assertEqual(data['function_count'], 2)
            signatures = [f['signature'] for m in data['modules'] for f in m['functions']]
            self.assertTrue(any('twice' in s for s in signatures))
            text = cli('brief', pathlib.Path(d) / 'main.axl').stdout
            self.assertIn('Doubles a number.', text)
            self.assertIn('marketing asked for it', text)

    def test_directory_brief_has_no_side_effects(self):
        with tempfile.TemporaryDirectory() as d:
            (pathlib.Path(d) / 'a.py').write_text('def f(x: int) -> int:\n    return x\ndef g(x):\n    return x\n')
            text = cli('brief', d).stdout
            self.assertIn('fn f(x: i64) -> i64', text)
            self.assertIn('skipped', text)
            self.assertFalse((pathlib.Path(d) / '.axle').exists())


class McpTests(unittest.TestCase):
    def test_handshake_and_listing(self):
        replies = rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}},
                      {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                      {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                      {'jsonrpc': '2.0', 'id': 3, 'method': 'nope'})
        self.assertEqual([r['id'] for r in replies], [1, 2, 3])        # the notification gets no reply
        self.assertEqual(replies[0]['result']['protocolVersion'], '2025-03-26')
        names = {t['name'] for t in replies[1]['result']['tools']}
        self.assertTrue({'axle_brief', 'axle_check', 'axle_slice', 'axle_lens', 'axle_test'} <= names)
        for tool in replies[1]['result']['tools']:
            self.assertEqual(tool['inputSchema']['type'], 'object')
            self.assertTrue(set(tool['inputSchema']['required']) <= set(tool['inputSchema']['properties']), tool['name'])
        self.assertEqual(replies[2]['error']['code'], -32601)

    def test_tool_calls(self):
        with tempfile.TemporaryDirectory() as d:
            good = source(d, 'fn twice(x: i64) -> i64 { return x * 2; }\nfn test_twice() -> unit { assert(twice(2) == 4); }\nfn main() -> unit ![io] { print(twice(2)); }')
            bad = source(d, 'fn main() -> unit { let a = 1.5 + 2; }', 'bad.axl')
            call = lambda i, name, **args: {'jsonrpc': '2.0', 'id': i, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}}
            replies = rpc(call(1, 'axle_check', path=str(good)), call(2, 'axle_check', path=str(bad)), call(3, 'axle_slice', path=str(good), function='twice'),
                          call(4, 'axle_test', path=str(good)), call(5, 'axle_run', path=str(good)), call(6, 'axle_slice', path=str(good)), call(7, 'nothing', path='x'))
            results = [r['result'] for r in replies]
            self.assertFalse(results[0]['isError'])
            self.assertTrue(results[1]['isError'])
            self.assertEqual(json.loads(results[1]['content'][0]['text'])['code'], 'E002')
            self.assertIn('fn twice', results[2]['content'][0]['text'])
            self.assertFalse(results[3]['isError'])
            self.assertEqual(results[4]['content'][0]['text'], '4')
            self.assertIn('missing argument', results[5]['content'][0]['text'])
            self.assertTrue(results[6]['isError'])

    def test_cli_entry_point(self):
        process = subprocess.run([sys.executable, str(ROOT / 'axle.py'), 'mcp'], input='{"jsonrpc":"2.0","id":1,"method":"ping"}\n', capture_output=True, text=True)
        self.assertEqual(json.loads(process.stdout), {'jsonrpc': '2.0', 'id': 1, 'result': {}})


if __name__ == '__main__':
    unittest.main()


class MultiErrorTests(unittest.TestCase):
    def test_type_errors_in_several_functions_are_all_reported(self):
        with tempfile.TemporaryDirectory() as d:
            path = source(d, 'fn a() -> i64 { return 1.5; }\n\nfn ok() -> i64 { return 1; }\n\nfn c() -> unit {\n    print(1);\n}\n\nfn d() -> i64 { let q = nope; return q; }\n')
            result = cli('check', path)
            self.assertEqual(result.returncode, 1)
            self.assertIn('3 errors', result.stderr)
            data = json.loads(cli('check', path, '--json').stderr)
            self.assertEqual([e['line'] for e in data['errors']], [1, 6, 9])
            self.assertEqual(data['line'], 1)                    # the first error is also at the top level
            self.assertTrue(all('snippet' in e and 'code' in e for e in data['errors']))

    def test_syntax_errors_recover_at_the_next_declaration(self):
        with tempfile.TemporaryDirectory() as d:
            path = source(d, 'fn a() -> i64 { return 1 +; }\nfn ok() -> i64 { return 1; }\nfn b() -> i64 { return @; }\n')
            data = json.loads(cli('check', path, '--json').stderr)
            self.assertEqual([e['line'] for e in data['errors']], [1, 3])

    def test_single_error_output_is_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            result = cli('check', source(d, 'fn a() -> i64 { return 1.5; }'))
            self.assertNotIn('errors', result.stderr)

    def test_mcp_check_returns_every_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = source(d, 'fn a() -> i64 { return 1.5; }\nfn b() -> i64 { return nope; }\n')
            text, failed = mcp_server.call('axle_check', {'path': str(path)})
            self.assertTrue(failed)
            self.assertEqual(len(json.loads(text)['errors']), 2)


class DivOrTests(unittest.TestCase):
    def test_div_or_is_allowed_under_nothrow_and_plain_division_is_not(self):
        with tempfile.TemporaryDirectory() as d:
            ok = source(d, 'fn f(a: u64, b: u64) -> u64 nothrow noalloc { return u64_div_or(a, b, 0u64) + u64_rem_or(a, b, 1u64); }\nfn main() -> unit { }')
            self.assertEqual(cli('check', ok).returncode, 0)
            bad = source(d, 'fn f(a: u64, b: u64) -> u64 nothrow { if b == 0u64 { return 0u64; } return a / b; }\nfn main() -> unit { }', 'bad.axl')
            result = cli('check', bad)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('u64_div_or', result.stderr)

    def test_values(self):
        with tempfile.TemporaryDirectory() as d:
            path = source(d, 'fn main() -> unit ![io] { print(i64_div_or(-7, 2, 0)); print(i64_div_or(7, 0, 99)); print(u64_rem_or(7u64, 4u64, 0u64)); print(u64_rem_or(7u64, 0u64, 5u64)); }')
            self.assertEqual(cli('run', path).stdout.split(), ['-3', '99', '3', '5'])
