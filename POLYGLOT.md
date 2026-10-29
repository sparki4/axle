# AXLE Polyglot

Use one host language with statement blocks written in other languages. The host does not have to be C or AXLE. The built-in host adapters are AXLE, Python, JavaScript (CommonJS), Rust, C, and C++; all six are also guest adapters. Tests execute every one of the 36 pairs on this Windows machine.

```text
print("Python host")
js { console.log("JavaScript"); }
c { printf("C\n"); }
rust { println!("Rust"); }
print("Python again")
```

```powershell
python polyglot.py run examples/polyglot/python.mix --host py
python polyglot.py run examples/polyglot/c.mix --host c
python polyglot.py run examples/polyglot/axle.mix --host axle
python polyglot.py run examples/polyglot/nested.mix --host py
python polyglot.py list
```

The source is a mixed-language dialect processed before its host compiler/interpreter. Native Python, C, or Rust tools cannot parse the mixed source directly. Any-language-to-any-language is an adapter design, not a promise of support for all languages out of the box.

## Syntax and execution

A marker must begin a statement: at the start of a line or after `{`, `}`, `;`, or `:`. Use `py { ... }`, `js { ... }`, `rust { ... }`, `c { ... }`, `cpp { ... }`, or `axle { ... }`. Marker and opening brace must be on the same line, separated by optional spaces/tabs. The block is a statement; it cannot be used as an expression or return value.

The brace scanner recognizes balanced braces, ordinary quoted strings, backslash escapes, Python triple-quoted strings, and the relevant basic line/block comments. It is not a complete parser for every embedded language. JavaScript regular expressions containing braces, nested template literals, Rust raw strings, shell/Ruby heredocs, Lua long strings, and other complex lexical forms should use fenced syntax. Existing host code with these complex forms can also confuse the scanner; use a custom host adapter/scanner extension or simplify that source. This is an explicit implementation limit, not full compatibility with arbitrary existing source files.

Fenced syntax terminates only at a line containing the chosen identifier, with optional indentation:

```text
js<<<END_JAVASCRIPT
console.log(/}/.test("}"));
END_JAVASCRIPT
```

Choose a delimiter absent from the guest source. The body is dedented. Fences can nest with different tags, allowing Rust inside JavaScript inside Python:

```text
js<<<END_JS
console.log("JavaScript guest");
rust<<<END_RUST
println!("Rust nested guest");
END_RUST
END_JS
```

When a guest has a registered host adapter, its body is processed recursively for nested blocks, up to depth 32. Additional runtime-only guest adapters do not support nested mixed syntax until their host adapter is registered. Fences determine the guest body boundary but do not implement an arbitrary host language's lexical rules.

Each guest invocation starts a separate process. Repeating a block inside a host loop starts a process on every iteration. Blocks execute synchronously; execution resumes in the host after successful completion. A guest failure stops the host under the generated calls' normal error handling. In languages that support catching those errors, user code can catch them. Exact exit codes are not preserved uniformly across adapters. There is no rollback of previous output, file changes, or other side effects.

Python/JavaScript guests are script bodies. C/C++ guests are statement snippets wrapped in `main` with basic standard headers. Rust guests are wrapped in `fn main()`. AXLE guests are wrapped in `fn main() -> unit ![io, mem]`. To embed a full translation unit or use special compilation settings, register a guest adapter without those wrappers. Syntax and behavior inside a block are supplied by its own language, not translated into AXLE semantics.

## Data and state

Local variables, types, heaps, and interpreter state do not automatically cross process boundaries. Separate blocks have separate interpreter state. They inherit the environment, working directory, stdin, stdout, and stderr of their caller. Pass simple string values through environment variables using the host's normal environment API:

```text
import os
os.environ["AXLE_MESSAGE"] = "hello from Python"
js { console.log(process.env.AXLE_MESSAGE); }
```

For typed or bidirectional exchange, explicitly serialize data, for example JSON files at a path placed in an environment variable. Child environment changes cannot modify the parent's environment. No automatic typed bridge, shared objects, return-value capture, IPC protocol, or direct FFI has been implemented. A future direct-FFI/embedded-runtime adapter would need explicit ownership and representation contracts.

This bridge is suited to coarse tasks such as invoking a script, reusing a tool, or generating an asset. It adds process/interpreter startup and I/O overhead. It is unsuitable for calls on every element in a numerical hot loop. Run hot loops within a single native block.

## Build and deployment

```powershell
python polyglot.py build examples/polyglot/python.mix --host py -o mixed.py
python polyglot.py build examples/polyglot/c.mix --host c -o mixed.exe
python polyglot.py build examples/polyglot/axle.mix --host axle --profile native -o mixed.exe
```

`run` builds into temporary storage and removes that storage after the host exits. `build` writes the host output plus a sibling `.poly` asset directory containing generated guest files, guest executables, the generated host source, and a manifest. Existing asset directories are left in place; repeated builds create a new suffixed directory. Source files are protected from exact-path output overwrite. Inspect the manifest and generated sources to review what will execute.

Current output embeds absolute guest-file paths and the Python executable used for building. Keep outputs and assets at their build paths or rebuild after moving. Installed guest runtimes are required on the execution machine. This is not a standalone relocatable executable. C/C++ guest units and Rust/AXLE guest units are compiled ahead of time; interpreted guests launch installed engines at runtime. The standard-library-based adapters use no shell to invoke guests.

Guest working directories are inherited, not switched to source directories. Python/Node module lookup follows the generated file location, so project-local packages or relative imports can require additional setup or a custom adapter. C/C++ gets the original host source directory as an include path. AXLE imports in the mixed root source resolve relative to the original source directory; imported AXLE modules are ordinary AXLE source and are not themselves processed for mixed blocks. Rust relative `include!`/modules, external crates, multi-file C/C++ linking, and existing project build graphs require build-system integration. Built-in compilation is one host source plus generated guest units. Profiles affect Rust/AXLE; C/C++ uses fixed optimized compiler settings.

JavaScript hosts and guests use CommonJS, not ES module mode. POSIX C/C++ runtime launching is implemented but untested here. Windows C/C++ launching uses the narrow-character spawn API, so non-ASCII executable/asset paths are not fully supported. There is no sandbox, concurrent shared-state management, persistent interpreter, or security boundary between trusted host and guest code.

## Adapter interface

The registry ships guest commands for Python, JavaScript, Rust, C, C++, AXLE, Ruby, Lua, PHP, Bash, and PowerShell. Only Python, JavaScript, Rust, C, C++, AXLE, and PowerShell were executed here; the other four are configured but untested. A missing runtime is reported during the build. Aliases include `python`, `javascript`, `rs`, `cxx`, `axl`, and `powershell`.

A Python plugin exports `register(hosts, guests)` and receives mutable dictionaries. Load one or more with `--adapter path/to/plugin.py`. Plugins are normal executable Python code. The adapter contract is:

```python
Host(
    extension,             # generated host source extension
    lex,                   # py, js, c, hash, or generic lexical subset
    emit_call,             # function(argv, block_id) -> host statement text
    preamble,              # prepended only when mixed blocks exist
    builder,               # function(source_path, output_path, profile, include_root)
    launcher,              # function(output_path) -> argv list
)

Guest(
    extension,
    lex,
    command=None,          # argv list with {file} placeholders for script runtimes
    host=None,             # registered host used for nested blocks/native building
    prefix="",
    suffix="",
)
```

With `command`, the guest is invoked through that argv list. Without it, `host` builds a guest executable. Registering a host alone does not automatically create a guest adapter. Builders can call native compilers, an interpreter checker, or a project's build tooling. Host emitters must correctly quote argv for their language and implement the desired failure handling. A new syntax family can require extending the scanner beyond these built-in lexical subsets.

`adapters/ruby_host.py` demonstrates adding Ruby as a host, allowing Python or JavaScript inside Ruby as well as Ruby inside the built-in hosts. It requires an installed Ruby and is not executed in the current environment:

```powershell
python polyglot.py run ruby-source.mix --host ruby --adapter adapters/ruby_host.py
```
