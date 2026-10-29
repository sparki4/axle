# Contributing to AXLE

AXLE is early, and there is plenty of useful work to do. You do not need to understand the entire compiler to contribute. A small reproducible bug report, a clearer example, or a tested language adapter can be a useful first contribution.

## Choose a starting point

- [Good first issues](https://github.com/sparki4/axle/labels/good%20first%20issue) have a bounded goal and acceptance criteria.
- [Help wanted](https://github.com/sparki4/axle/labels/help%20wanted) covers broader work.
- [ROADMAP.md](ROADMAP.md) explains current priorities and the features still missing.

If you have an idea that changes syntax, memory rules, or architecture, open a proposal before spending time on a large implementation. Include a short code example and describe the behavior you want. For a small correction, you can send a pull request directly. There is no contributor agreement or sign-off requirement; contributions are accepted under the project's MIT license.

## Set up a development copy

Fork the repository on GitHub, then clone your fork and create a branch:

```sh
git clone https://github.com/YOUR_USERNAME/axle.git
cd axle
git switch -c my-change
python axle.py check examples/adaptive.axl --json
```

The tools use Python 3.10+ and the standard library. Native AXLE tests need Rust 1.82+ with a working linker. The full polyglot suite also needs Node.js, a C/C++ compiler, and PowerShell (`pwsh`). Windows is the currently verified platform; portability work is welcome. The CI job installs Python and Node and uses the Windows runner's compiler tools and PowerShell.

Run checks relevant to the change:

```sh
# Compiler semantics and arithmetic
python -m unittest discover -s tests -p test_axle.py -v

# Externs, bridges, adopt, diagnostics, loop control
python -m unittest discover -s tests -p test_v03.py -v

# Buffers, imports, inference, and build profiles
python -m unittest discover -s tests -p test_v02.py -v

# Mixed-language scanner, adapters, and the 36 host/guest pairs
python -m unittest discover -s tests -p test_polyglot.py -v

# Full suite, when all toolchains are available
python -m unittest discover -s tests -v
```

Documentation-only changes do not need a new test. For behavior changes, add a regression case that demonstrates a meaningful failure or verifies the promised semantics. Say which checks you ran and which dependencies were unavailable. Do not claim an untested platform or runtime is supported.

## Find the right file

| Area | Entry point |
| --- | --- |
| Lexer, parser, types, effects, Rust lowering, CLI | `axle.py` |
| Mixed-language scanner, host/guest adapters, project builds | `polyglot.py` |
| `adopt` scanners (Python, JavaScript, C, Rust) | `adopt.py` |
| Python/JavaScript bridge workers and Rust runtime | `bridge_runtime.py` |
| Plugin example | `adapters/ruby_host.py` |
| Core language contracts | `SPEC.md` |
| Bridge contracts and limitations | `POLYGLOT.md` |
| Runtime comparisons | `benchmark_suite.py` |
| Small runnable examples | `examples/` |

## Send your change

Keep each pull request focused. Describe the problem, resulting behavior, relevant validation, and any remaining limitation. Update the reference when syntax or semantics change. Update adapter documentation when its runtime requirements or tested status changes. Include a minimal source example for a compiler fix.

Commit to your branch, push it to your fork, and open a pull request against `main`:

```sh
git add PATHS_YOU_CHANGED
git commit -m "Describe the change"
git push -u origin my-change
```

Use your own Git author identity. Contributors receive credit through their commits and pull requests. Maintainer review is required before a contribution is merged; opening a pull request does not guarantee acceptance or a response time.

## Performance changes

Run `python benchmark_suite.py --samples 9 --output my-results.json` when a change makes a runtime-speed claim. Give AXLE and the Rust baseline matching build profiles and equivalent work. Include raw samples, machine/toolchain information, and correctness checks. Explain allocation, process startup, and data-exchange costs. Report regressions as well as wins. One fast microbenchmark does not establish that AXLE is faster than Rust in general.

For new workloads, prevent constant folding or loop hoisting without changing the work only one implementation performs. Keep baseline implementations reviewable. Do not overwrite historical benchmark reports just to show better numbers; attach a new result and explain the comparison.

## Working together

Be respectful and focus feedback on the code and its behavior. Ask questions when a contract is unclear. Share minimal reproductions rather than personal files, credentials, or unrelated project data. If a change exposes a limitation, documenting it honestly is useful work.
