"""`axle test`: run every `fn test_*() -> unit` in its own process.

A test passes when the process exits normally.  An abort (a failed assert, an out-of-range
index, a division by zero, a foreign exception) is a failure and its message is reported.
Each test runs isolated, so one abort cannot hide the others.
"""
import json
import pathlib
import subprocess
import sys
import tempfile
import time

import axle


def run(options):
    loaded = axle.load_program(options.source)
    compiler = axle.Compiler(functions=loaded.functions, externs=loaded.externs, links=loaded.links, natives=loaded.natives,
                             guarantees=loaded.guarantees, require_main=False, test_mode=True)
    rust = compiler.compile()
    if not compiler.test_names:
        raise axle.Error('no tests found', hint='define tests as `fn test_name() -> unit ![effects] { assert(...); }`')
    if options.strict:
        import lens
        lens.enforce_strict(options.source)
    names = [n for n in compiler.test_names if not options.filter or options.filter in n]
    results = []
    with tempfile.TemporaryDirectory(prefix='axle-test-') as directory:
        exe = axle.build(rust, pathlib.Path(directory) / ('tests.exe' if sys.platform == 'win32' else 'tests'),
                         options.profile, options.link_search, options.link_arg)
        for name in names:
            started = time.time()
            try:
                process = subprocess.run([str(exe), name], capture_output=True, text=True, timeout=60)
                ok, detail = process.returncode == 0, ''
                if not ok:
                    lines = [l for l in process.stderr.strip().splitlines() if l.strip() and not l.startswith('note:')]
                    detail = f'exited with status {process.returncode}'
                    for i, line in enumerate(lines):
                        if 'panicked at' in line:
                            detail = lines[i + 1].strip() if i + 1 < len(lines) else line
                            break
                    else:
                        if lines: detail = lines[0]
            except subprocess.TimeoutExpired:
                ok, detail = False, 'timed out after 60 seconds'
            results.append(dict(name=name, passed=ok, detail=detail, seconds=round(time.time() - started, 3)))
    failed = [r for r in results if not r['passed']]
    if options.json:
        print(json.dumps(dict(passed=len(results) - len(failed), failed=len(failed), results=results), indent=2))
    else:
        for r in results:
            print(('PASS  ' if r['passed'] else 'FAIL  ') + r['name'] + ('' if r['passed'] else f'\n      {r["detail"]}'))
        print(f'{len(results) - len(failed)} passed, {len(failed)} failed')
    return 1 if failed else 0
