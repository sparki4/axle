"""`axle mcp`: expose AXLE's analysis commands to coding agents over the Model Context Protocol.

Speaks JSON-RPC 2.0 on stdin/stdout, one message per line, and has no dependencies.  Each tool
runs the matching `axle` command in a subprocess with a time limit, so a crash or a hang in
one call cannot take the server down.
"""
import json
import pathlib
import subprocess
import sys

import axle

HERE = pathlib.Path(__file__).resolve().parent
TIMEOUT = 180
PATH = {'type': 'string', 'description': 'path to an .axl file, or a project directory'}
TOOLS = {
    'axle_brief': ('Orient in a codebase in one call: modules, function signatures with doc comments, what needs attention. Given a directory of Python/JS/C/COBOL it reports what AXLE can call and why the rest was skipped. Start here.',
                   {'path': PATH}, ['brief', '{path}']),
    'axle_check': ('Type- and effect-check an .axl file. On failure returns JSON with code, line, column, the offending source line, and a fix hint. Run after every edit.',
                   {'path': PATH, 'strict': {'type': 'boolean', 'description': 'also reject stubs, unimplemented(), masked failures and asserts that cannot fail'}},
                   ['check', '{path}', '--json']),
    'axle_slice': ('The minimum context to change one function: its source, doc, signature and footprint, the footprint of everything it calls, and its callers.',
                   {'path': PATH, 'function': {'type': 'string'}, 'depth': {'type': 'integer', 'description': 'also include callee bodies to this depth (default 0)'}},
                   ['slice', '{path}', '--fn', '{function}', '--json']),
    'axle_lens': ('What can one source line do, including everything its callees do. "No hidden behavior" means it only does local arithmetic and bindings.',
                  {'path': PATH, 'line': {'type': 'integer'}}, ['lens', '{path}', '--line', '{line}', '--json']),
    'axle_audit': ('Per-function footprint, unused effects, nothrow/noalloc guarantees the compiler would accept, aliasing and risky patterns.',
                   {'path': PATH}, ['audit', '{path}', '--json']),
    'axle_map': ('Architecture overview built from the code and its /// doc comments: modules, purposes, who calls whom, foreign boundaries.',
                 {'path': PATH}, ['map', '{path}', '--json']),
    'axle_test': ('Run every fn test_* in the file, each in its own process. Returns pass/fail per test with the failing assertion.',
                  {'path': PATH, 'filter': {'type': 'string'}, 'strict': {'type': 'boolean'}}, ['test', '{path}', '--json']),
    'axle_run': ('Build and run an .axl program; returns its output.', {'path': PATH}, ['run', '{path}']),
    'axle_adopt': ('Scan an existing project and write typed bindings (.axle/bindings.axl) and a machine-readable map (.axle/axle.map.json). Does not run the scanned code.',
                   {'path': PATH}, ['adopt', '{path}', '--json']),
    'axle_adopt_check': ('Check whether previously generated bindings still match the source tree.', {'path': PATH}, ['adopt', '{path}', '--check']),
    'axle_characterize': ('Record the current behavior of adopted functions as tests (axle test replays them). WARNING: this runs the project\'s own code with sample inputs.',
                          {'path': PATH}, ['characterize', '{path}', '--json']),
    'axle_explain': ('Explain a diagnostic code (E001...) with a failing and a corrected example.', {'code': {'type': 'string'}}, ['explain', '{code}']),
    'axle_manifest': ('The complete current language description generated from the compiler: types, builtins, effects, commands, codes.', {}, ['manifest']),
}
REQUIRED = {'axle_slice': ['path', 'function'], 'axle_lens': ['path', 'line'], 'axle_explain': ['code']}


def schema(name):
    properties = TOOLS[name][1]
    required = REQUIRED.get(name, ['path'] if 'path' in properties else [])
    return {'type': 'object', 'properties': properties, 'required': required}


def call(name, arguments):
    if name not in TOOLS:
        return f'unknown tool {name}', True
    template = TOOLS[name][2]
    argv = []
    for part in template:
        if part.startswith('{'):
            key = part[1:-1]
            if key not in arguments:
                return f'missing argument: {key}', True
            argv.append(str(arguments[key]))
        else:
            argv.append(part)
    if arguments.get('strict'): argv.append('--strict')
    if arguments.get('filter'): argv += ['--filter', str(arguments['filter'])]
    if arguments.get('depth'): argv += ['--depth', str(int(arguments['depth']))]
    try:
        process = subprocess.run([sys.executable, str(HERE / 'axle.py'), *argv], capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return f'timed out after {TIMEOUT} seconds', True
    text = process.stdout.strip()
    if process.stderr.strip():
        text = (text + '\n' + process.stderr.strip()).strip()
    return text or '(no output)', process.returncode != 0


def handle(message):
    method, ident, params = message.get('method'), message.get('id'), message.get('params') or {}
    if method == 'initialize':
        return dict(protocolVersion=params.get('protocolVersion', '2024-11-05'), capabilities={'tools': {}},
                    serverInfo={'name': 'axle', 'version': axle.VERSION},
                    instructions='Call axle_brief first. Use axle_slice before changing a function, axle_check after every edit.')
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': [dict(name=n, description=t[0], inputSchema=schema(n)) for n, t in TOOLS.items()]}
    if method == 'tools/call':
        text, failed = call(params.get('name'), params.get('arguments') or {})
        return {'content': [{'type': 'text', 'text': text}], 'isError': failed}
    if ident is None:
        return None                       # a notification such as notifications/initialized
    raise LookupError(method)


def serve(stdin=None, stdout=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
            result = handle(message)
            reply = None if message.get('id') is None else {'jsonrpc': '2.0', 'id': message['id'], 'result': result}
        except LookupError as error:
            reply = {'jsonrpc': '2.0', 'id': message.get('id'), 'error': {'code': -32601, 'message': f'method not found: {error}'}}
        except (ValueError, AttributeError):
            reply = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}}
        if reply is not None:
            stdout.write(json.dumps(reply) + '\n')
            stdout.flush()
    return 0


if __name__ == '__main__':
    sys.exit(serve())
