# AXLE 0.2

[![Tests](https://github.com/sparki4/axle/actions/workflows/tests.yml/badge.svg)](https://github.com/sparki4/axle/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A native language for coding agents, plus an extensible polyglot source builder. AXLE provides exact scalar types, checked effects, explicit raw memory, and higher level checked buffers. The polyglot builder lets different host languages contain blocks written in other languages.

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

`let` fixes the binding; `var` permits reassignment. Local type annotations are optional, with exact types inferred from initializers. Parameters, returns, and effects remain explicit. No implicit numeric conversion is performed. Unsuffixed integers are `i64`; use `u64` for unsigned values. Arithmetic wraps with defined 64-bit semantics.

Safe `buf_u64` values are shared handles to checked storage. Assigning or passing one clones the handle and shares the same contents. The last handle automatically releases storage. A `let` buffer can still have its contents changed with `buf_set`; the binding cannot be reassigned. This uses Rust `Rc<RefCell<Vec<u64>>>` internally and carries reference-count/borrow-check overhead. It is single-threaded. Raw pointers have no bounds, lifetime, or ownership tracking and can cause undefined behavior when misused.

`import "lib/math.axl";` loads relative source files once, rejects import cycles, and makes their functions available in one global namespace. Forward calls and recursion work. Modules do not provide namespaces, separate compilation, or a package system yet.

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
python axle.py emit examples/adaptive.axl -o adaptive.rs
python axle.py build examples/adaptive.axl --profile size -o adaptive.exe
python axle.py run examples/hash.axl --args 1000
python polyglot.py list
```

`check` checks the core language without Rust. `emit` exposes generated Rust for review. `build` writes a native executable. `run` uses temporary outputs. `--args` must come last. Core diagnostics stop at the first error and report line/column; imported language errors also include their source path. `--json` emits structured language diagnostics on stderr. Successful check/build status is emitted on stdout; emit status goes to stderr if Rust occupies stdout. Native program output, guest failures, and backend diagnostics are not converted to JSON. There is no AXLE source map for backend errors.

The core tests include 510 reproducible arithmetic comparisons with Python, rejection cases, native examples, pointer operations, buffer aliasing and bounds failures, imports, and all build profiles. Polyglot tests cover the six-by-six language matrix. Passing tests cannot prove zero bugs.

## Current boundary

AXLE still lacks user-defined structs, generics, floats, runtime strings, concurrency, a package manager, a language server, and direct FFI. The native frontend is Python and the native backend is Rust. Guest-language features are provided by their own runtimes rather than becoming AXLE types. Existing multi-file projects and special build systems need build integration; the built-in polyglot tool operates on one mixed host source and generated guest units.

The next substantial improvements would be typed direct FFI, records with specified layouts, generic containers, persistent embedded runtimes, and broader application-level benchmarks. They are not implemented features.

See [SPEC.md](SPEC.md) for the language reference and [POLYGLOT.md](POLYGLOT.md) for mixed-language builds. License: MIT.
