# AXLE roadmap

This is a direction for contributors, not a release schedule or a promise that every proposal will land. The priority is a small, precise language and useful interoperability with evidence for correctness and performance.

## Working now

- Exact 64-bit integer semantics, booleans, functions, control flow, and explicit effects.
- Raw pointers and explicit allocation, plus checked shared unsigned buffers.
- Local type inference and relative source imports.
- Optimized native builds through Rust, with release/native/size profiles.
- AXLE, Python, JavaScript, Rust, C, and C++ as mixed-language hosts and guests, including nested blocks.
- Typed `extern` calls into C (direct), Python and JavaScript (persistent workers), `native`/`link` inputs, and `axle adopt` binding generation with a skipped-function report.
- Structured diagnostics with stable codes, hints and name suggestions; `axle api` for tooling.
- An adapter API, runnable examples, regression tests, and matched-profile benchmarks.

These features still have the limits described in the references. Mixed blocks launch subprocesses and do not share local variables or types.

## Good places for a first contribution

| Task | A useful result | Where to start |
| --- | --- | --- |
| Diagnostic examples | Small invalid programs with documented expected errors | `tests/`, `SPEC.md` |
| Adapter smoke coverage | Run an existing unverified runtime and record requirements and results | `polyglot.py`, `POLYGLOT.md` |
| Path handling regressions | Cover spaces and quoting without changing argument boundaries | `tests/test_polyglot.py` |
| Small examples | A runnable example with a clear expected output | `examples/` |
| Contributor documentation | Verify setup instructions on a clean toolchain | `CONTRIBUTING.md` |

Look at the issue tracker before starting so work is not duplicated. Propose a bounded first step for a broader task.

## Near-term reliability

1. Extend automated verification to Linux and macOS. First separate truly required tools from optional adapter runtimes so missing tools are reported clearly rather than obscuring compiler failures.
2. Improve scanner handling and diagnostics for complex host syntax. Fenced guest blocks do not solve arbitrary host-language parsing.
3. Add source mapping for generated code and better diagnostics for guest-build failures.
4. Make polyglot outputs relocatable with an explicit bundle layout and runtime discovery.
5. Expand regression and differential testing around effect propagation, module paths, integer edge cases, and adapter failures.

## Language growth

Discuss each extension before implementing it:

- Records with specified layouts and predictable field access.
- Checked slices and generic containers with explicit ownership rules.
- Numeric conversions, additional scalar widths, floats, and useful string types.
- Module namespaces and build/project integration.

Syntax should stay easy to generate, review, and diagnose. Language growth should preserve defined integer behavior and visible resource/effect contracts.

## Interoperability and speed

- Extend the foreign-function interface beyond integers, booleans and scalar pointers: floats, strings and layout-specified records, with clear lifetime rules.
- Make bridge executables relocatable and add TypeScript/type-stub inputs to `adopt` (Go, Java and .NET scanners are open).
- Investigate persistent or embedded Python/JavaScript runtimes for workloads that cannot afford a process per call.
- Define explicit data-exchange contracts rather than pretending unrelated runtimes share memory automatically.
- Add application-level workloads and statistical summaries to the benchmark suite.
- Investigate optimizations using correctness checks and equally tuned baselines.

A direct ABI, embedded interpreter, or independent backend would be a substantial project. None is implemented today. Broad compatibility and general speed superiority remain goals to investigate, not current claims.
