#!/usr/bin/env python3
"""AXLE Polyglot: inline guest languages in Python, JavaScript, Rust, C and C++."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass

import axle

class PolyError(Exception): pass

def checked(argv, **kwargs):
    try:
        result = subprocess.run(list(map(str, argv)), capture_output=True, text=True, **kwargs)
    except OSError as error:
        raise PolyError(str(error)) from None
    if result.returncode:
        raise PolyError(f'command failed ({result.returncode}): {argv[0]}\n{result.stdout}{result.stderr}')
    return result

def c_quote(value):
    # UTF-8 bytes, fixed-width octal escapes, no shell involved.
    return '"' + ''.join(chr(b) if 32 <= b < 127 and chr(b) not in {'"', '\\'} else f'\\{b:03o}' for b in str(value).encode('utf-8')) + '"'

def rust_quote(value):
    return json.dumps(str(value), ensure_ascii=False)

def find_msvc():
    locator = Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)'))/'Microsoft Visual Studio/Installer/vswhere.exe'
    if not locator.is_file(): raise PolyError('C/C++ needs clang, gcc, or Visual Studio C++ Build Tools')
    installation = checked([locator, '-latest', '-products', '*', '-requires', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-property', 'installationPath']).stdout.strip()
    if not installation: raise PolyError('Visual Studio C++ tools are unavailable')
    candidates = sorted((Path(installation)/'VC/Tools/MSVC').glob('*/bin/Hostx64/x64/cl.exe'))
    if not candidates: raise PolyError('x64 MSVC compiler not found')
    compiler = candidates[-1]
    tools = compiler.parents[3]
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows Kits\Installed Roots') as key:
        sdk = Path(winreg.QueryValueEx(key, 'KitsRoot10')[0])
    versions = sorted(p for p in (sdk/'Lib').glob('*') if (p/'ucrt/x64').is_dir() and (p/'um/x64').is_dir())
    if not versions: raise PolyError('Windows SDK libraries not found')
    version = versions[-1].name
    environment = os.environ.copy()
    environment['PATH'] = str(compiler.parent) + os.pathsep + environment.get('PATH', '')
    environment['INCLUDE'] = ';'.join(map(str, [tools/'include', sdk/'Include'/version/'ucrt', sdk/'Include'/version/'shared', sdk/'Include'/version/'um', sdk/'Include'/version/'winrt']))
    environment['LIB'] = ';'.join(map(str, [tools/'lib/x64', sdk/'Lib'/version/'ucrt/x64', sdk/'Lib'/version/'um/x64']))
    return compiler, environment

def build_c(source, output, cpp=False, include=None):
    compiler = shutil.which('clang++' if cpp else 'clang') or shutil.which('g++' if cpp else 'gcc')
    if compiler:
        checked([compiler, '-O3', '-std=c++17' if cpp else '-std=c11', '-I', include or source.parent, source, '-o', output])
        return
    if os.name != 'nt': raise PolyError('C/C++ requires clang or gcc')
    compiler, environment = find_msvc()
    checked([compiler, '/nologo', '/O2', '/EHsc' if cpp else '/TC', '/std:c++17' if cpp else '/std:c11',
             '/utf-8', '/D_CRT_SECURE_NO_WARNINGS', '/I' + str(include or source.parent),
             '/Fo' + str(source.with_suffix('.obj')), '/Fe' + str(output), source], env=environment, cwd=source.parent)

@dataclass
class Host:
    extension: str
    lex: str
    emit_call: object
    preamble: str
    builder: object
    launcher: object

@dataclass
class Guest:
    extension: str
    lex: str
    command: list | None = None
    host: str | None = None
    prefix: str = ''
    suffix: str = ''

C_PREAMBLE = r'''
#ifndef _WIN32
#define _POSIX_C_SOURCE 200809L
#endif
#include <stdio.h>
#include <stdlib.h>
#ifdef _WIN32
#include <process.h>
#else
#include <unistd.h>
#include <sys/wait.h>
#include <errno.h>
#endif
static void axle_poly_exec(const char *const *args) {
    fflush(NULL);
#ifdef _WIN32
    intptr_t result = _spawnvp(_P_WAIT, args[0], args);
    if (result < 0) { perror(args[0]); exit(127); }
    if (result != 0) exit((int)result);
#else
    pid_t pid = fork();
    if (pid < 0) { perror("fork"); exit(127); }
    if (pid == 0) { execvp(args[0], (char *const *)args); perror(args[0]); _exit(127); }
    int status;
    while (waitpid(pid, &status, 0) < 0) { if (errno != EINTR) { perror("waitpid"); exit(127); } }
    if (WIFEXITED(status)) { if (WEXITSTATUS(status)) exit(WEXITSTATUS(status)); }
    else exit(128 + WTERMSIG(status));
#endif
}
'''

def emit_c(argv, index):
    return '{ const char *axle_args_' + str(index) + '[] = {' + ', '.join(c_quote(x) for x in argv) + ', NULL}; axle_poly_exec(axle_args_' + str(index) + '); }'

def emit_python(argv, index):
    # Explicitly flush the host before a child inherits the console.
    return '__import__("sys").stdout.flush(); __import__("sys").stderr.flush(); __import__("subprocess").run(' + repr(list(map(str, argv))) + ', check=True)'

def emit_js(argv, index):
    return 'require("node:child_process").execFileSync(' + json.dumps(str(argv[0])) + ', ' + json.dumps(list(map(str, argv[1:]))) + ', {stdio: "inherit"});'

def emit_rust(argv, index):
    arguments = '.args([' + ', '.join(rust_quote(x) for x in argv[1:]) + '])' if len(argv) > 1 else ''
    return '{ std::io::Write::flush(&mut std::io::stdout()).expect("flush failed"); std::io::Write::flush(&mut std::io::stderr()).expect("flush failed"); let status = std::process::Command::new(' + rust_quote(argv[0]) + ')' + arguments + '.status().expect("guest runtime failed"); if !status.success() { std::process::exit(status.code().unwrap_or(1)); } }'

def build_axle(source, output, profile, include):
    manifest = json.loads((source.parent/'manifest.json').read_text(encoding='utf-8'))
    commands = {entry['id']: emit_rust(entry['argv'], entry['id']) for entry in manifest['blocks']}
    rust = axle.compile_file(source, foreign_commands=commands, import_base=include)
    axle.build(rust, output, profile)

def script_builder(language):
    def build(source, output, profile, include):
        if language == 'py':
            try: compile(source.read_text(encoding='utf-8'), str(source), 'exec')
            except SyntaxError as error: raise PolyError(str(error)) from None
        else: checked(['node', '--check', source])
        if source != output: shutil.copyfile(source, output)
    return build

HOSTS = {
    'axle': Host('.axl', 'c', lambda argv, index: f'poly_exec({index}u64);', '', build_axle, lambda path: [str(path)]),
    'py': Host('.py', 'py', emit_python, '', script_builder('py'), lambda path: [sys.executable, str(path)]),
    'js': Host('.cjs', 'js', emit_js, '', script_builder('js'), lambda path: ['node', str(path)]),
    'rust': Host('.rs', 'c', emit_rust, '', lambda src, out, profile, include: axle.build(src.read_text(encoding='utf-8'), out, profile), lambda path: [str(path)]),
    'c': Host('.c', 'c', emit_c, C_PREAMBLE, lambda src, out, profile, include: build_c(src, out, include=include), lambda path: [str(path)]),
    'cpp': Host('.cpp', 'c', emit_c, C_PREAMBLE, lambda src, out, profile, include: build_c(src, out, cpp=True, include=include), lambda path: [str(path)]),
}
GUESTS = {
    'axle': Guest('.axl', 'c', host='axle', prefix='fn main() -> unit ![io, mem] {\n', suffix='\n}\n'),
    'py': Guest('.py', 'py', [sys.executable, '{file}'], host='py'),
    'js': Guest('.cjs', 'js', ['node', '{file}'], host='js'),
    'rust': Guest('.rs', 'c', host='rust', prefix='fn main() {\n', suffix='\n}\n'),
    'c': Guest('.c', 'c', host='c', prefix='#include <stdio.h>\n#include <stdlib.h>\nint main(void) {\n', suffix='\nreturn 0;\n}\n'),
    'cpp': Guest('.cpp', 'c', host='cpp', prefix='#include <iostream>\n#include <cstdlib>\nint main() {\n', suffix='\nreturn 0;\n}\n'),
    'ruby': Guest('.rb', 'hash', ['ruby', '{file}']),
    'lua': Guest('.lua', 'generic', ['lua', '{file}']),
    'php': Guest('.php', 'c', ['php', '{file}'], prefix='<?php\n'),
    'bash': Guest('.sh', 'hash', ['bash', '{file}']),
    'pwsh': Guest('.ps1', 'hash', ['pwsh', '-NoProfile', '-File', '{file}']),
}
ALIASES = {'axl': 'axle', 'python': 'py', 'javascript': 'js', 'rs': 'rust', 'cxx': 'cpp', 'powershell': 'pwsh'}

def skip_quote(text, start, language):
    quote = text[start]
    delimiter = quote * 3 if language == 'py' and text.startswith(quote*3, start) else quote
    i = start + len(delimiter)
    while i < len(text):
        if text[i] == '\\': i += 2
        elif text.startswith(delimiter, i): return i + len(delimiter)
        else: i += 1
    raise PolyError('unterminated string; use a fenced block for complex guest syntax')

def skip_comment(text, start, language):
    if text.startswith('//', start) and language in {'c', 'js', 'generic'}:
        end = text.find('\n', start)
        return len(text) if end < 0 else end
    if text.startswith('/*', start) and language in {'c', 'js', 'generic'}:
        end = text.find('*/', start+2)
        if end < 0: raise PolyError('unterminated comment')
        return end+2
    if text[start] == '#' and language in {'py', 'hash'}:
        end = text.find('\n', start)
        return len(text) if end < 0 else end
    return start

def closing_brace(text, start, language):
    depth, i = 1, start
    while i < len(text):
        end = skip_comment(text, i, language)
        if end != i: i = end; continue
        if text[i] in '\"\'`':
            i = skip_quote(text, i, language)
            continue
        if text[i] == '{': depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0: return i
        i += 1
    raise PolyError('unclosed guest block; use a fenced block if guest syntax contains unmatched braces')

def blocks(text, language):
    """Yield (start, end, adapter, body). Fences are the unrestricted syntax."""
    i = 0
    while i < len(text):
        end = skip_comment(text, i, language)
        if end != i: i = end; continue
        if text[i] in '\"\'`':
            # A Rust lifetime such as &'a is not a quoted character.
            if language == 'c' and text[i] == "'" and re.match(r"'[A-Za-z_][A-Za-z_0-9]*(?!')", text[i:]):
                match = re.match(r"'[A-Za-z_][A-Za-z_0-9]*", text[i:])
                if i + len(match[0]) >= len(text) or text[i+len(match[0])] != "'":
                    i += len(match[0]); continue
            i = skip_quote(text, i, language)
            continue
        match = re.match(r'[A-Za-z_][A-Za-z_0-9]*', text[i:])
        if not match: i += 1; continue
        name = ALIASES.get(match[0], match[0])
        after = i + len(match[0])
        cursor = after
        while cursor < len(text) and text[cursor] in ' \t': cursor += 1
        # Marker names are recognized only at a statement boundary.
        before = text[:i].rstrip(' \t')
        boundary = not before or before[-1] in '\n;{}:'
        if name in GUESTS and boundary:
            if text.startswith('<<<', cursor):
                tag = re.match(r'<<<([A-Za-z_][A-Za-z_0-9]*)[ \t]*\r?\n', text[cursor:])
                if not tag: raise PolyError('fence must be <<<TAG followed by a newline')
                begin = cursor + len(tag[0])
                terminator = re.search(r'(?m)^[ \t]*' + re.escape(tag[1]) + r'[ \t]*\r?$', text[begin:])
                if not terminator: raise PolyError('missing fence terminator ' + tag[1])
                finish = begin + terminator.end()
                yield i, finish, name, textwrap.dedent(text[begin:begin+terminator.start()])
                i = finish
                continue
            if cursor < len(text) and text[cursor] == '{':
                finish = closing_brace(text, cursor+1, GUESTS[name].lex)
                body = text[cursor+1:finish]
                # Remove a structural initial newline before dedenting Python.
                body = textwrap.dedent(body.lstrip('\r\n')).strip('\r\n')
                if '\n' not in body: body = body.strip()
                yield i, finish+1, name, body + '\n'
                i = finish+1
                continue
        i = after

def expand_command(command, file):
    return [part.replace('{file}', str(file)) for part in command]

def require_runtime(command):
    executable = command[0]
    if not shutil.which(executable) and not Path(executable).is_file():
        raise PolyError('guest runtime is unavailable: ' + executable)

class Project:
    def __init__(self, bundle, profile, include):
        self.bundle, self.profile, self.include = bundle, profile, include
        self.counter, self.manifest = 0, []
    def transform(self, text, host_name, depth=0):
        if depth > 32: raise PolyError('nested guest limit exceeded (32)')
        host = HOSTS[host_name]
        pieces, position, found = [], 0, False
        for start, end, name, body in blocks(text, host.lex):
            found = True
            index = self.counter
            self.counter += 1
            guest = GUESTS[name]
            source = self.bundle / (f'guest-{index:03}' + guest.extension)
            content = guest.prefix + body + guest.suffix
            if guest.host:
                if guest.host not in HOSTS: raise PolyError('unknown guest host adapter ' + guest.host)
                content = self.transform(content, guest.host, depth+1)
            source.write_text(content, encoding='utf-8')
            if guest.command:
                argv = expand_command(guest.command, source)
                require_runtime(argv)
                if guest.host:
                    HOSTS[guest.host].builder(source, source, self.profile, self.include)
            else:
                if not guest.host: raise PolyError('guest needs a command or host adapter: ' + name)
                executable = self.bundle / (f'guest-{index:03}' + ('.exe' if os.name == 'nt' else '.bin'))
                self.write_manifest()
                HOSTS[guest.host].builder(source, executable, self.profile, self.include)
                argv = HOSTS[guest.host].launcher(executable)
            self.manifest.append({'id': index, 'language': name, 'source': str(source), 'argv': argv})
            pieces += [text[position:start], host.emit_call(argv, index)]
            position = end
        pieces.append(text[position:])
        return (host.preamble if found else '') + ''.join(pieces)
    def write_manifest(self, **extra):
        (self.bundle/'manifest.json').write_text(json.dumps({'blocks': self.manifest, **extra}, indent=2), encoding='utf-8')

def load_plugin(path):
    specification = importlib.util.spec_from_file_location('axle_poly_user_adapter', path)
    if not specification or not specification.loader: raise PolyError('cannot load adapter plugin')
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    module.register(HOSTS, GUESTS)

def build_project(source, host_name, output, profile='release'):
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output: raise PolyError('output must not overwrite source')
    host_name = ALIASES.get(host_name, host_name)
    if host_name not in HOSTS: raise PolyError('unknown host adapter: ' + host_name)
    output.parent.mkdir(parents=True, exist_ok=True)
    bundle = output.parent / (output.name + '.poly')
    # Never reuse a bundle: this avoids overwriting a source or other preexisting file.
    if bundle.exists():
        import uuid
        bundle = output.parent / (output.name + '.poly-' + uuid.uuid4().hex[:8])
    bundle.mkdir()
    project = Project(bundle, profile, source.parent)
    transformed = project.transform(source.read_text(encoding='utf-8'), host_name)
    generated = bundle / ('host' + HOSTS[host_name].extension)
    generated.write_text(transformed, encoding='utf-8')
    project.write_manifest(host=host_name, source=str(source), output=str(output))
    HOSTS[host_name].builder(generated, output, profile, source.parent)
    return HOSTS[host_name].launcher(output), bundle

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['build', 'run', 'list'])
    parser.add_argument('source', nargs='?', type=Path)
    parser.add_argument('--host', default='py')
    parser.add_argument('-o', '--output', type=Path)
    parser.add_argument('--profile', choices=['release', 'native', 'size'], default='release')
    parser.add_argument('--adapter', action='append', default=[], help='Python plugin defining register(HOSTS, GUESTS)')
    parser.add_argument('--args', nargs=argparse.REMAINDER, default=[])
    options = parser.parse_args()
    try:
        for plugin in options.adapter: load_plugin(plugin)
        if options.command == 'list':
            print(json.dumps({'hosts': sorted(HOSTS), 'guests': sorted(GUESTS), 'aliases': ALIASES}, indent=2))
            return 0
        if options.source is None: parser.error('source required for build/run')
        host = ALIASES.get(options.host, options.host)
        if host not in HOSTS: raise PolyError('unknown host: ' + host)
        if options.command == 'run':
            with tempfile.TemporaryDirectory(prefix='axle-poly-') as directory:
                extension = HOSTS[host].extension if host in {'py', 'js'} else ('.exe' if os.name == 'nt' else '.bin')
                argv, _ = build_project(options.source, host, Path(directory)/('host'+extension), options.profile)
                return subprocess.run([*argv, *options.args]).returncode
        extension = HOSTS[host].extension if host in {'py', 'js'} else ('.exe' if os.name == 'nt' else '.bin')
        output = options.output or options.source.with_name(options.source.stem + '.mixed' + extension)
        _, bundle = build_project(options.source, host, output, options.profile)
        print('Output: ' + str(output.resolve()))
        print('Required guest assets: ' + str(bundle))
        return 0
    except (PolyError, axle.Error, OSError, RuntimeError, ValueError, RecursionError) as error:
        print('polyglot: ' + str(error), file=sys.stderr)
        return 1

if __name__ == '__main__': sys.exit(main())
