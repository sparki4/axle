# AXLE for coding agents

AXLE is small on purpose: explicit types, explicit effects, one obvious way to write each thing, and errors you can act on without guessing.

## Workflow

0. Orient first: `python axle.py brief <file-or-dir>` (or the `axle_brief` tool over `axle mcp`). One call gives modules, signatures, doc comments, what needs attention, or for a foreign project what is callable and why the rest is not.
1. Understand what exists: `python axle.py adopt <project>` then read `.axle/axle.map.json`. `bound` lists callable functions with their AXLE signatures; `skipped` lists what was left out and why (usually a missing type annotation). Add the annotation to the source and run `adopt` again.
2. Write AXLE that imports `.axle/bindings.axl`. Do not edit that file; regenerate it.
3. Check cheaply: `python axle.py check main.axl --json`. Errors carry `code`, `line`, `column`, `hint`, and `suggestion` when a near-match name exists. Apply the hint, do not guess.
4. Before changing a function, get its context instead of reading the program: `python axle.py slice main.axl --fn NAME` (add `--depth 1` for callee bodies). It prints the function, the signature and total footprint of everything it calls, and its callers.
5. Before relying on a line, ask what it can do: `python axle.py lens main.axl --line N`. An explicit "no hidden behavior" means the line only does local arithmetic and bindings.
6. Review a change with `python axle.py audit main.axl`: it lists unused effects, shared-buffer aliasing, and guarantees (`nothrow`, `noalloc`) the compiler would accept. Add them; they are proven, and a later edit that breaks one fails the build with the call chain.
7. Inspect the surface: `python axle.py api main.axl` returns every function, parameter type, result type and effect as JSON.
8. Run: `python axle.py run main.axl`.

9. Test: `python axle.py test file.axl` runs every `fn test_*() -> unit` in isolation. Add `--strict` to reject stubs, `unimplemented()`, masked failures and asserts that cannot fail.
10. Learn the project: `python axle.py map main.axl` (modules and dependencies), `python axle.py manifest` (every command, code and builtin, machine-readable), `python axle.py explain E001`.
11. Legacy code: `python axle.py characterize <dir>` after `adopt` records current behavior as tests; run `adopt --check` to detect drift. COBOL gets `.axle/legacy-notes.md` (structure only).

## Rules that cause most errors

- Types are exact. `1` is `i64`, `1u64` is `u64`; there are no implicit conversions.
- Declare effects on the signature of every function that prints or reads arguments (`![io]`), or touches buffers or raw memory (`![mem]`). A caller must declare every effect its callees declare.
- `let` is immutable, `var` is mutable. Annotations on locals are optional.
- Parameters and return types are always written. `main` is `fn main() -> unit`.
- `else if`, `while`, `break` and `continue` exist. There is no `for`.
- `extern "c"` calls need `unsafe { }`. Calls through generated safe wrappers do not.
- Integer overflow wraps. Division by a non-literal, out-of-range buffer access, `assert`, and `print` can abort; `nothrow` is only accepted when none of them is reachable.
- `let b = a;` on a buffer does not copy: both names share storage. `lens` and `audit` flag it.

## Foreign code

Bridges and C calls carry `i64`, `u64`, `f64`, `bool` and `unit` (plus C integer widths and scalar pointers for C). Python `float` and C `double` are adopted as `f64`. Strings (`str`) work on both: write `c_str` in `extern "c"` signatures. Adoption of C only takes `const char *` parameters, because a plain `char *` may be written to. Structs work on bridges only, as a Python dict or JavaScript object with the same field names; the foreign function must take and return that shape. `adopt` does not infer structs, so declare those externs by hand. Byte buffers cannot be passed yet. Wrap them in a small function that takes and returns integers.
