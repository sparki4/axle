# AXLE 0.3 language reference

This describes the implemented core. Source is UTF-8; identifiers use ASCII letters, digits, and `_`, and cannot start with a digit. Whitespace separates tokens. `//` comments extend to the end of the line. Keywords and type names cannot be identifiers. Builtin names cannot be declared as functions. Identifiers are case-sensitive. Quoted paths are supported only in imports; there are no runtime strings, character literals, preprocessor directives, or implicit semicolons. Mixed-language source is handled by the separate polyglot builder described in POLYGLOT.md.

## Grammar

```ebnf
program    = { function | extern | "import", quoted_relative_axl_path, ";"
             | "link", quoted_library_name, ";" | "native", quoted_relative_path, ";" } ;
extern     = "extern", quoted_kind, "fn", identifier, "(", [params], ")", "->", type,
             ["!", "[", [effect, {",", effect}], "]"], ["=", quoted_foreign_name], ";" ;
function   = "fn", identifier, "(", [params], ")", "->", type,
             ["!", "[", [effect, {",", effect}], "]"], block ;
params     = identifier, ":", type, {",", identifier, ":", type} ;
effect     = "io" | "mem" ;
type       = "i64" | "u64" | "bool" | "unit" | "buf_u64" | "*i64" | "*u64" | "*bool" ;
block      = "{", {statement}, "}" ;
statement  = ("let" | "var"), identifier, [":", type], "=", expression, ";"
           | identifier, "=", expression, ";"
           | "return", [expression], ";"
           | "if", expression, block, ["else", block]
           | "while", expression, block
           | "break", ";" | "continue", ";"
           | "unsafe", block
           | expression, ";" ;
expression = integer | "true" | "false" | identifier
           | identifier, "(", [expression, {",", expression}], ")"
           | "(", expression, ")"
           | ("!" | "-" | "~"), expression
           | expression, binary_operator, expression ;
```

Pointer type `*` and its scalar name may have whitespace between them. Trailing commas are not accepted. `else if` is accepted and is sugar for an `else` block holding one `if`. Integers are decimal, optionally suffixed `i64` or `u64`. Literal values must fit their type. The special negative literal `-9223372036854775808` (optionally suffixed `i64`) denotes the signed minimum, including with parentheses around its positive magnitude.

## Types and bindings

`i64`: signed 64-bit two's-complement integer. `u64`: unsigned 64-bit integer. `bool`: logical value. `unit`: absence of a value, represented by `()` in the backend. Pointers have the target's native pointer width and address a scalar of their stated type; pointer-to-pointer types are not supported. Layout of aggregate data is not specified because aggregates do not exist yet. Heap `u64` elements are contiguous with the backend's `u64` size and alignment; integer byte order follows the target.

Every binding must have an initializer. An omitted annotation is inferred exactly from the initializer; an explicit annotation must match exactly. `unit` variables and parameters are rejected. Parameters are immutable values. To mutate a parameter's local copy, initialize a `var` from it. A block introduces a scope. Duplicate bindings in that scope are rejected; shadowing outer bindings is allowed. A shadowing initializer sees the previous binding. There is no definite-assignment ambiguity because uninitialized declarations are unavailable.

`buf_u64` is a shared, automatically released handle to a fixed-length, checked buffer of `u64`. Passing, returning, or assigning a buffer clones its handle and aliases the same storage. A `let` buffer permits changing its contents but prohibits rebinding the handle. The backend representation is `Rc<RefCell<Vec<u64>>>`, with reference-count and runtime borrow-check costs. It cannot be addressed using `addr` or converted to a raw pointer. Buffers cannot be compared or printed directly and are single-threaded.

Imports use quoted relative `.axl` paths with forward slashes and no escapes. Dependencies are visited depth-first and loaded once by resolved path; cyclic imports are rejected. Imported functions share one global namespace, and duplicate names are rejected. Relative paths are resolved against the importing file. The CLI resolves imports; the source-only `compile_source` API rejects them with an instruction to use `compile_file`. Diagnostics from imported code carry their original path. Imports are not separately compiled, namespaced, or sandboxed to the project directory.

All functions have explicit return types and optional explicit effect sets; omitted effects mean the empty set. Duplicate functions and duplicate parameters are rejected. Function order does not affect visibility. A program requires `fn main() -> unit` with zero parameters and any supported effect set. There are no global variables. User calls require exact argument count and types; there is no overloading. Recursive calls are permitted, without stack depth guarantees.

## Operators

All binary operators associate left. Precedence, highest first:

| Group | Operators |
| --- | --- |
| Unary | `!`, unary `-` |
| Multiplicative | `* / %` |
| Additive | `+ -` |
| Shift | `<< >>` |
| Ordered comparison | `< > <= >=` |
| Equality | `== !=` |
| Bitwise AND | `&` |
| Bitwise XOR | `^` |
| Bitwise OR | `\|` |
| Logical AND | `&&` |
| Logical OR | `\|\|` |

Operands must have identical types. Arithmetic, shifts, bitwise operations, and ordered comparisons require integers. Logical operations and `!` require booleans. Equality accepts integers, booleans, and same-type pointers, but not `unit`. Unary minus wraps on both integer types. `~` is bitwise not on integers.

`+`, `-`, `*`, and unary minus wrap modulo 2^64. Signed results use two's-complement interpretation. `/` truncates toward zero; `%` has the dividend's sign. Signed minimum divided by `-1` wraps to signed minimum; its remainder is zero. Division or remainder by zero aborts the program with a runtime diagnostic. Both behaviors are identical in optimized and unoptimized generated Rust.

Shift counts use the low six bits, including negative `i64` counts. Left shift wraps; unsigned right shift fills with zeros; signed right shift extends the sign. Parenthesize bitwise expressions before equality tests: `(a ^ b) == c`. Comparison chaining is parsed according to the table, not as mathematical chained comparisons.

Expressions evaluate left-to-right, including function arguments and the pointer/value arguments of `store`. `&&` and `||` short-circuit. Conditions require `bool`. There are no assignment expressions, expression-valued blocks, or implicit returns. An expression statement must have type `unit` so computed values cannot be silently discarded.

## Control flow and effects

`return;` returns `unit`. `return expression;` requires the declared result type. A non-unit function must return on all paths recognized by the checker: an explicit return, an `unsafe` block that returns, or an `if` with returning branches on both sides. Loops are conservatively considered fallthrough paths, even `while true`. Statements following a proven unconditional return are rejected. `break` and `continue` apply to the innermost `while` and are rejected outside loops. They end the current path for unreachable-code checks but do not count as returns.

Each function's declared effect set must contain the effect set of every called user function and every called builtin. This enforces propagation through recursive and forward calls because all signatures are known before body checking. Unused declared effects are allowed. There is no inference of a narrower effect set from a callee's body. All syntactic branches are checked, even unreachable constant branches. `unsafe` does not add any effect by itself.

`io` covers output, command-line input, and polyglot subprocess invocation. `mem` covers raw address acquisition, raw loads/stores, pointer offsets, allocation, release, and all buffer builtins. Local scalar assignment does not need `mem`. Pointer parameters, returns, assignment, and equality do not access the pointed-to memory and do not need `mem`. Buffer-handle copies and automatic release do not require an additional declared effect. Pure functions can still assert, abort on arithmetic failure, recurse indefinitely, or exhaust the stack. Effects are not exceptions, a termination proof, or security permissions.

## Builtins

| Builtin | Type | Effect | Requires `unsafe` |
| --- | --- | --- | --- |
| `print(x)` | `i64/u64/bool -> unit` | io | no |
| `assert(condition)` | `bool -> unit` | none | no |
| `arg_u64(index)` | `u64 -> u64` | io | no |
| `buf_new(count)` | `u64 -> buf_u64` | mem | no |
| `buf_iota(count)` | `u64 -> buf_u64` | mem | no |
| `buf_len(data)` | `buf_u64 -> u64` | mem | no |
| `buf_get(data, index)` | `(buf_u64, u64) -> u64` | mem | no |
| `buf_set(data, index, value)` | `(buf_u64, u64, u64) -> unit` | mem | no |
| `buf_sum(data)` | `buf_u64 -> u64` | mem | no |
| `black_box(value)` | non-unit T -> T | none | no |
| `poly_exec(id)` | literal u64 -> unit, generated polyglot calls only | io | no |
| `addr(variable)` | mutable scalar lvalue -> corresponding pointer | mem | yes |
| `load(pointer)` | `*T -> T` | mem | yes |
| `store(pointer, value)` | `(*T, T) -> unit` | mem | yes |
| `offset(pointer, index)` | `(*T, u64) -> *T` | mem | yes |
| `alloc_u64(count)` | `u64 -> *u64` | mem | yes |
| `free_u64(pointer, count)` | `(*u64, u64) -> unit` | mem | yes |

`print` prints one decimal integer or lowercase boolean with a newline. `assert(false)` aborts. `arg_u64(0u64)` addresses the executable name; normally use index 1 for the first argument. A missing argument, an argument that Rust's `u64` parser rejects, or an index that cannot fit `usize` aborts.

`addr` accepts only an in-scope mutable `i64`, `u64`, or `bool` variable, not a temporary or pointer variable. `load` and `store` are ordinary non-atomic, non-volatile scalar accesses. `offset` advances in units of the pointee type, rather than bytes. The resulting pointer must remain in the same live allocation or one past its end; only in-bounds initialized elements may be dereferenced. A zero offset can preserve a valid pointer. Offset conversion overflow aborts, but address bounds are not checked.

`buf_new` zero-initializes a checked buffer; `buf_iota(n)` initializes it to `0, 1, ..., n-1`. Empty checked buffers are valid. `buf_len` returns their length. `buf_get` and `buf_set` bounds-check their indices and abort on invalid access or index conversion overflow. `buf_sum` returns a wrapping sum, zero for an empty buffer. Allocation/layout failures can abort. The last shared handle automatically releases storage. `black_box` is an optimization barrier intended for benchmarks; it preserves the value and does not make a constant-time or cryptographic guarantee. `poly_exec` requires a block ID provided by the polyglot builder; ordinary standalone AXLE code cannot use an unbound ID. Guest code can perform arbitrary operations through its own runtime and is not effect-checked by AXLE.

`alloc_u64` allocates a positive count of zero-initialized `u64` values using the system-backed Rust allocator. Count zero, counts that cannot fit the target size/layout, or allocation failure abort. `free_u64` requires exactly the original pointer and original count for one live allocation made by `alloc_u64`. After release, all its pointers are invalid. Stack addresses must never be passed to `free_u64`. Every raw allocated buffer needs exactly one release; raw storage has no automatic cleanup.

Unsafe code is responsible for memory validity, pointer provenance, alignment, valid boolean representations, no invalid alias use, and no use after free/return. The compiler does not verify these properties. Escaping a local pointer from a function is accepted and can lead to undefined behavior if subsequently used after the local's lifetime ends.

## Foreign declarations

`extern "<kind>" fn name(params) -> type [![effects]] [= "foreign_name"];` declares a function implemented elsewhere. The optional `= "foreign_name"` binds a different symbol or function name than the AXLE name. Extern names share the function namespace and cannot repeat or shadow builtins.

**Kind `"c"`** is a direct call through the platform C ABI. Calls require an `unsafe` block, because AXLE cannot verify the foreign code, and the declared effect list is a claim that AXLE trusts. Parameter and result types may be `i64`, `u64`, `bool`, `unit` (result only), scalar pointers, and the ABI widths `c_int`, `c_uint`, `c_long`, `c_ulong` and `c_size`. ABI widths are legal only in `extern "c"` signatures. AXLE sees them as `i64` (`c_int`, `c_long`) or `u64` (`c_uint`, `c_ulong`, `c_size`). Arguments are converted with a range check that aborts on overflow; results are widened exactly. `buf_u64` cannot cross the boundary. Symbols come from `native` objects, `link` libraries, or the platform C library.

**Kinds `"py:<target>[@root]"` and `"js:<target>[@root]"`** call a function in a persistent worker process. A target ending in `.py` (or `.js`, `.mjs`, `.cjs`) is a file path relative to the declaring source file; any other target is a module name resolved from `root` (default: the declaring file's directory). Only `i64`, `u64`, `bool` parameters and `i64`, `u64`, `bool`, `unit` results are allowed. Bridge calls implicitly carry `io` and need no `unsafe`. Arguments are sent as decimal text. A result that is not exactly the declared type (a float, a negative value for `u64`, a value outside 64 bits) aborts with the foreign message. Foreign exceptions abort with their original text. The worker loads the module once; state persists between calls and ends with the program. File targets are resolved to absolute paths when compiled, so a built executable is not relocatable. Set `AXLE_PYTHON` or `AXLE_NODE` to choose an interpreter. The JavaScript worker supports CommonJS and `.mjs`.

`native "path";` compiles a `.c`, `.cc`, `.cpp` or `.cxx` file with the system compiler (gcc, clang or cc; `cl` is attempted on Windows but unverified) and links it. A `.o` or `.a` is linked as is. C++ must export `extern "C"` symbols. The path is relative to the declaring file. `link "name";` adds `-l name`; use `--link-search DIR` for directories and `--link-arg ARG` for other linker flags. Both apply to `build` and `run`.

## Diagnostics

Each diagnostic has a message, line, column, and optionally a source path. `--json` adds a stable `code` (`E0xx`), a `hint` describing the repair, and a `suggestion` when a similarly named variable or function exists. Codes identify the category and stay stable; message wording may change.

## Implementation boundary

The compiler is a handwritten recursive-descent/precedence parser, followed by a typed traversal that emits Rust. There is no serialized intermediate representation or independent optimizer. The native compiler supplies code generation and optimization. Parser nesting is limited by Python's recursion limit; the CLI reports excessive nesting as a language diagnostic. Large files and hostile inputs have not been hardened as a public compiler service. Backend compilation errors are surfaced with Rust diagnostics and no AXLE source map.
