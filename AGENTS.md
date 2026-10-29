# AXLE for coding agents

AXLE is small on purpose: explicit types, explicit effects, one obvious way to write each thing, and errors you can act on without guessing.

## Workflow

1. Understand what exists: `python axle.py adopt <project>` then read `.axle/axle.map.json`. `bound` lists callable functions with their AXLE signatures; `skipped` lists what was left out and why (usually a missing type annotation). Add the annotation to the source and run `adopt` again.
2. Write AXLE that imports `.axle/bindings.axl`. Do not edit that file; regenerate it.
3. Check cheaply: `python axle.py check main.axl --json`. Errors carry `code`, `line`, `column`, `hint`, and `suggestion` when a near-match name exists. Apply the hint, do not guess.
4. Inspect the surface: `python axle.py api main.axl` returns every function, parameter type, result type and effect as JSON.
5. Run: `python axle.py run main.axl`.

## Rules that cause most errors

- Types are exact. `1` is `i64`, `1u64` is `u64`; there are no implicit conversions.
- Declare effects on the signature of every function that prints or reads arguments (`![io]`), or touches buffers or raw memory (`![mem]`). A caller must declare every effect its callees declare.
- `let` is immutable, `var` is mutable. Annotations on locals are optional.
- Parameters and return types are always written. `main` is `fn main() -> unit`.
- `else if`, `while`, `break` and `continue` exist. There is no `for`.
- `extern "c"` calls need `unsafe { }`. Calls through generated safe wrappers do not.
- Integer overflow wraps. Division by zero and out-of-range buffer access abort.

## Foreign code

Bridges and C calls carry `i64`, `u64`, `bool` and `unit` only (plus C integer widths and scalar pointers for C). Functions using floats, strings or structs cannot be called yet. Wrap them in a small function that takes and returns integers.
