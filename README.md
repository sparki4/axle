# AXLE 0.4

[![Tests](https://github.com/sparki4/axle/actions/workflows/tests.yml/badge.svg)](https://github.com/sparki4/axle/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A native language for coding agents that adopts existing code instead of replacing it. AXLE can call functions in your current Python, JavaScript, C and Rust-ABI code through typed declarations, scan a codebase to generate those declarations, report errors as structured diagnostics with codes and fix hints, and answer "what can this line actually do?" for any line of code. It also includes an extensible polyglot source builder. AXLE provides exact scalar types, checked effects, explicit raw memory, and higher level checked buffers. The polyglot builder lets different host languages contain blocks written in other languages.

This is an experimental working implementation, not a replacement for Rust on every project. The core compiles through Rust. The polyglot bridge uses separate processes and installed runtimes; it is not a unified shared-memory runtime.

**Help shape AXLE.** Contributors are welcome to work on language adapters, compiler diagnostics, portability, examples, and reproducible performance improvements. Small fixes and documentation improvements are welcome too. Start with the [contribution guide](CONTRIBUTING.md), [roadmap](ROADMAP.md), or [good first issues](https://github.com/sparki4/axle/labels/good%20first%20issue). Open an issue to discuss a larger change, or send a focused pull request.

```sh
git clone https://github.com/sparki4/axle.git
cd axle
python axle.py run examples/hello.axl
```

## Run it

Python 3.10+ runs the tools. Rust 1.82+ and its platform linker build AXLE programs. Node.js runs JavaScript blocks. C/C++ requires GCC, Clang, or Visual Studio C++ Build Tools plus a Windows SDK. No Python packages or Cargo dependencies are required. Verified here on Windows with Python 3.14, Rust 1.98.1, Node.js, and MSVC; other platforms are untested.

```powershell
python axle.py run examples/adaptive.axl
# 499542
python axle.py build examples/adaptive.axl --profile native -o adaptive.exe
python polyglot.py run examples/polyglot/python.mix --host py
python polyglot.py run examples/polyglot/c.mix --host c
python polyglot.py run examples/polyglot/nested.mix --host py
python -m unittest discover -s tests -v
```

## Adapt to the abstraction level

High-level checked storage and inferred local types:

```text
fn main() -> unit ![mem, io] {
    let data = buf_iota(1000u64);
    buf_set(data, 0u64, 42u64);
    print(buf_sum(data));
}
```

Low-level storage remains available in the same language:

```text
fn main() -> unit ![mem, io] {
    unsafe {
        let data: *u64 = alloc_u64(4u64);
        store(offset(data, 0u64), 42u64);
        print(load(data));
        free_u64(data, 4u64);
    }
}
```

`let` fixes the binding; `var` permits reassignment. Control flow has `if`/`else if`/`else`, `while`, `break` and `continue`; `~` is bitwise not. Local type annotations are optional, with exact types inferred from initializers. Parameters, returns, and effects remain explicit. No implicit numeric conversion is performed. Unsuffixed integers are `i64`; use `u64` for unsigned values. Arithmetic wraps with defined 64-bit semantics.

Safe `buf_u64` values are shared handles to checked storage. Assigning or passing one clones the handle and shares the same contents. The last handle automatically releases storage. A `let` buffer can still have its contents changed with `buf_set`; the binding cannot be reassigned. This uses Rust `Rc<RefCell<Vec<u64>>>` internally and carries reference-count/borrow-check overhead. It is single-threaded. Raw pointers have no bounds, lifetime, or ownership tracking and can cause undefined behavior when misused.

`import "lib/math.axl";` loads relative source files once, rejects import cycles, and makes their functions available in one global namespace. Forward calls and recursion work. Modules do not provide namespaces, separate compilation, or a package system yet.

## Adopt an existing codebase

You do not rewrite your project in AXLE. Point `adopt` at it and import what it finds:

```sh
python axle.py adopt path/to/project        # writes .axle/bindings.axl and .axle/axle.map.json
```

```text
import ".axle/bindings.axl";

fn main() -> unit ![io] {
    print(with_tax(1000, 17));   // an existing Python function
    print(is_prime(97));         // an existing JavaScript function
    print(sum_squares(1000u64)); // an existing C function, compiled and linked in
}
```

`adopt` reads Python (type annotations), JavaScript (JSDoc types), C and Rust `extern "C"` functions. It binds only functions whose signatures AXLE can represent exactly, and `axle.map.json` records every function it skipped with the reason (for example "parameter `x` needs an `int` annotation"), so an agent knows what to annotate. Nothing in your project is modified. See `examples/adopt/`.

The generated file uses three declarations that you can also write by hand:

```text
native "fast.c";                                   // compile and link this C file
link "m";                                          // link a library (-L with --link-search)
extern "c" fn labs(x: c_long) -> c_long;           // C ABI, called inside unsafe
extern "py:legacy/pricing.py" fn with_tax(c: i64, p: i64) -> i64;   // Python function
extern "js:legacy/checks.js" fn is_prime(n: i64) -> bool;           // JavaScript function
extern "py:pkg.module@src" fn run(n: i64) -> i64;                   // importable module under root `src`
```

C calls are direct native calls, at full speed. `c_int`, `c_uint`, `c_long`, `c_ulong` and `c_size` are C ABI widths valid only in `extern "c"` signatures: AXLE values stay 64-bit and arguments are range-checked on the way in. Python and JavaScript calls go to one persistent worker per module, not a new process per call. The foreign module is imported as it is, so its own imports keep working, its prints reach your terminal, and exceptions abort with the original message. Bridges and C calls carry `i64`, `u64`, `f64`, `str`, `bool` and `unit`; JavaScript promises are awaited and 64-bit integers stay exact. Structs cross Python and JavaScript bridges as a dict or object (Python receives `{"x": 1}`, and must return a dict with the same fields); they cannot cross C yet. Byte buffers and mutable `char *` buffers are not supported yet. Strings cross bridges as UTF-8 and cross C as `c_str` (a NUL-terminated `const char *`): arguments are copied in, a returned pointer is copied out and never freed, and a NULL result aborts. `f64` is IEEE 754: `1.0 / 0.0` is `inf`, never an abort, and there are no implicit conversions (`f64_from_i64`, `f64_to_i64`, ...). See `examples/floats.axl`.

`python axle.py api program.axl` prints a JSON description of every function, signature, effect and extern. See [AGENTS.md](AGENTS.md) for a short guide aimed at coding agents.

## Know what your code can do

In most languages a line like `foo()` tells you nothing about what happens underneath. AXLE records, for every operation, whether it can allocate, abort, do I/O, mutate shared storage, create a second handle to shared storage, use raw memory, loop without a proof of termination, or cross into foreign code. Three commands expose this, always including everything the callees can do:

```text
$ python axle.py lens examples/demo.axl --line 27
examples/demo.axl:27  in fn main() -> unit ![io, mem]
  report(1u64, average(data, 10u64));
  - calls `average`: can abort (division by zero; index out of bounds), may not terminate (loop or recursion)
  - calls `report`: can abort (standard output can fail (for example a closed pipe)), does I/O
```

A line with nothing to report says so explicitly ("only local arithmetic, comparisons and bindings; cannot allocate, abort, do I/O, call anything, touch shared storage or loop"), so a missing warning is an answer, not an omission.

```sh
python axle.py lens  prog.axl --line 27     # what can this line do?
python axle.py audit prog.axl               # every function: footprint, unused effects, guarantees it could carry
python axle.py slice prog.axl --fn average  # the minimum context to change one function
python axle.py slice prog.axl --fn main --depth 1   # ...plus the bodies of what it calls
```

All three take `--json`. `slice` prints the function's source (with its leading comment), the signature and total footprint of everything it calls, and who calls it, which is the context needed to change it safely without reading the rest of the program. `audit` also flags effects a function declares but never uses, functions that would be accepted with a guarantee, and places where one buffer gets a second handle.

**Guarantees are checked, not documented.** Write them in the signature:

```text
fn mix(state: u64, value: u64) -> u64 nothrow noalloc { ... }
```

`nothrow` means the function and everything it can reach cannot abort: no division by a value that might be zero, no buffer indexing, no `assert`, no printing, no unsafe memory access, no bridge calls. `noalloc` means nothing reachable allocates. If either is false, the build fails and names the chain:

```text
fn guarded is declared nothrow but can abort: division by zero (line 3, in fn divide; call chain: guarded -> mid -> divide)
```

`nothrow` does not cover running out of memory or stack, and it says nothing about termination (`audit` reports loops and recursion separately). A foreign C function can be trusted with `extern "c" fn f(...) -> T nothrow noalloc;`; AXLE records that as a claim it cannot verify. Python and JavaScript bridge functions can never carry either guarantee. The checker is tested against randomly generated programs: anything it accepts as `nothrow` must run without aborting.

## Use it from an AI agent

```
axle mcp          # Model Context Protocol server on stdin/stdout, no dependencies
axle brief DIR    # the same orientation as plain text or --json
```

Add the server to any MCP-capable agent or editor (`examples/mcp.json` shows the shape). The agent then has `axle_brief` (start here), `axle_check` (errors carry the failing source line, a caret and a fix hint), `axle_slice`, `axle_lens`, `axle_audit`, `axle_map`, `axle_test`, `axle_run`, `axle_adopt`, `axle_characterize`, `axle_explain` and `axle_manifest`. Every call runs in a separate process with a time limit. `axle_characterize` runs the project's own code with sample inputs; `axle_adopt` does not.

Type mismatches name the exact conversion (`expected f64, got i64` suggests `f64_from_i64(x)`), and `as_u64` / `as_i64` convert between integer signedness.

## Legacy code and drift

```
axle adopt legacy/            # also reads COBOL: writes .axle/legacy-notes.md
axle adopt --check legacy/    # exit 1 if the bindings no longer match the source
axle characterize legacy/     # record what adopted functions do today as tests
axle test legacy/.axle/characterization.test.axl
```

- COBOL is analysed structurally only (data items, files, paragraphs, what each performs, reads, writes, decides, and the hard-coded numbers). It is not callable from AXLE, and nothing is inferred about intent: paragraphs without a comment get a `TODO why` line.
- `characterize` runs sample inputs through the real Python, JavaScript or C code and writes asserts for the observed results. It records behavior, not correctness; a failing assert after a change means behavior changed.
- Only functions with `i64`, `u64` or `bool` signatures are covered.

## Mix host and guest languages

A Python host can contain JavaScript, Rust, or C:

```text
print("Python host")
js { console.log("JavaScript guest"); }
rust { println!("Rust guest"); }
c { printf("C guest\n"); }
print("Back in Python")
```

Use `--host py`, `js`, `rust`, `c`, `cpp`, or `axle`. Every one of those six languages can also be a guest. The tests execute all 36 host/guest pairs, as well as nested guests, inherited environment values, and guest-failure handling.

Ruby, Lua, PHP, Bash, and PowerShell guest launch adapters are also included. PowerShell is tested here; the other four runtimes are not installed or verified here. Plugins can register additional host and guest adapters with their own source syntax, invocation, and build steps. Extensibility is not a claim that every existing language works out of the box.

See [POLYGLOT.md](POLYGLOT.md) for the plugin interface, syntax details, data exchange, deployment, and limits. Brace syntax is a convenience for a documented lexical subset. Fenced blocks support guest source containing complex or unmatched braces.

## Speed, measured

`benchmark-v02.json` contains nine interleaved samples per executable with matching Rust build flags. Both AXLE and Rust get the same `release` or `native` profile. Results are checked against each other and against independent reference calculations on small workloads.

| Workload/profile | AXLE median | Rust median |
| --- | ---: | ---: |
| 50M-step integer recurrence, release | 61.49 ms | 61.31 ms |
| 50M-step integer recurrence, native | 60.91 ms | 60.80 ms |
| 4M-element reduction ×32, release | 54.13 ms | 55.24 ms |
| 4M-element reduction ×32, native | 43.69 ms | 45.09 ms |

AXLE is effectively tied on the integer workload and measured about 3.1% lower time on the native reduction in this run. The native AXLE reduction measured about 19% less time than its portable build. These are small microbenchmarks on one machine, without statistical confidence intervals. They do not establish general superiority. The Rust reduction baseline uses an owned Vec while AXLE provides shared buffer handles; both produce the same result for this workload. A tuned Rust implementation could match the generated code.

Timing includes process startup, allocation, and printing. Native tuning also helps Rust. The older `benchmark-results.json` preserves the original 0.1 measurement; `benchmark_suite.py` is the current comparison. Polyglot subprocess calls are deliberately outside the native-kernel benchmark and add substantial startup/communication overhead.

```powershell
python benchmark_suite.py --samples 9 --output benchmark-v02.json
```

## Profiles and agent commands

| Profile | Behavior |
| --- | --- |
| release | Optimization level 3, abort on panic, one codegen unit |
| native | Release options plus current-CPU tuning and fat LTO |
| size | Optimization for size, fat LTO, stripped symbols |

`native` output may require instructions unavailable on another CPU. These profiles apply to AXLE and Rust compilation. C/C++ polyglot adapters currently use their own fixed optimized settings; they do not map these profiles.

```powershell
python axle.py check examples/adaptive.axl --json
python axle.py api examples/adaptive.axl
python axle.py audit examples/guarantees.axl
python axle.py adopt path/to/project
python axle.py emit examples/adaptive.axl -o adaptive.rs
python axle.py build examples/adaptive.axl --profile size -o adaptive.exe
python axle.py run examples/hash.axl --args 1000
python polyglot.py list
```

`check` checks the core language without Rust. `emit` exposes generated Rust for review. `build` writes a native executable. `run` uses temporary outputs. `--args` must come last. Core diagnostics stop at the first error and report line/column; imported language errors also include their source path. `--json` emits structured language diagnostics on stderr, including a stable `code` (such as `E011`), a `hint`, and a `suggestion` when a close name exists. Successful check/build status is emitted on stdout; emit status goes to stderr if Rust occupies stdout. Native program output, guest failures, and backend diagnostics are not converted to JSON. There is no AXLE source map for backend errors.

The core tests include 510 reproducible arithmetic comparisons with Python, rejection cases, native examples, pointer operations, buffer aliasing and bounds failures, imports, and all build profiles. Polyglot tests cover the six-by-six language matrix. Passing tests cannot prove zero bugs.

## Current boundary

AXLE still lacks generics, methods, references and recursive types, mutable strings and byte buffers, concurrency, a package manager, and a language server. Direct FFI covers the C ABI with integer, boolean and pointer arguments only. The native frontend is Python and the native backend is Rust. Guest-language features are provided by their own runtimes rather than becoming AXLE types. Existing multi-file projects and special build systems need build integration; the built-in polyglot tool operates on one mixed host source and generated guest units.

The next substantial improvements would be byte buffers across the FFI boundary, records with specified layouts, generic containers, persistent embedded runtimes, and broader application-level benchmarks. They are not implemented features.

See [SPEC.md](SPEC.md) for the language reference and [POLYGLOT.md](POLYGLOT.md) for mixed-language builds. License: MIT.
