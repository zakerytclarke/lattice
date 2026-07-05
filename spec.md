# Lattice Language Specification

This document describes the **current** Lattice language as implemented by the compiler and validated by the test suite in `tests/`. When in doubt, treat the tests as the source of truth.

Lattice is a statically typed language that compiles to WebAssembly. Programs have fixed memory bounds known at compile time, and the compiler uses an SMT solver (Z3) to reject unsafe code before it runs.

---

## 1. Core Principles

1. **Fixed memory, no heap.** All data structures have compile-time-known sizes. There is no garbage collector and no dynamic allocation in generated WASM.
2. **Compile-time safety proofs.** Index bounds, refined type constraints, division-by-zero, and struct invariants are checked by Z3. Failures are compile errors, not runtime panics.
3. **Compile-time termination checks.** Recursive functions must use arguments that the verifier can prove decrease on each call.
4. **Strong typing with inference.** Types can be omitted in many places; the compiler infers them from usage.
5. **Explicit boundaries.** Only `external` functions and types can cross module boundaries. `main` must be `external` and defined in the entry file.

---

## 2. Getting Started

```bash
# Install dependencies
pip install z3-solver pytest

# Compile and run a program
./lattice tests/factorial.lattice 5

# Run the test suite
python3 -m pytest tests/test_lattice.py -v
```

A Lattice program is a `.lattice` file. The `./lattice` script compiles it to WASM and runs it with Node.js.

---

## 3. Lexical Syntax

- **Comments:** `//` to end of line
- **Identifiers:** letters, digits, underscores; must not start with a digit
- **String/char literals:** single-quoted characters, e.g. `'A'`, `'h'`
- **Integer literals:** decimal integers, e.g. `42`, `-1`
- **Boolean literals:** `true`, `false`
- **Semicolons:** optional in many places

---

## 4. Variables

```lattice
let x = 5;          // mutable
x = x + 1;

const y = 10;       // immutable after initialization
```

Function parameters can be marked `let` (mutable) or `const` (immutable):

```lattice
function set[N: Integer, T: Type](let list: List[N, T], idx: Integer, val: T) -> List[N, T] {
    // ...
}
```

---

## 5. Types

### 5.1 Primitive types

| Type | Description |
|------|-------------|
| `Integer` | Signed integer (represented as i32 in WASM) |
| `Bool` | `true` or `false` |
| `Char` | Single Unicode code point |
| `Type` | Metatype used in generic parameters |
| `void` | No value (used with `IO[void]`) |

### 5.2 Refined types

Attach a logical constraint to a type using `(var){constraint}`:

```lattice
function add_safe(
    a: Integer(x){x >= 0 && x <= 50},
    b: Integer(y){y >= 0 && y <= 50}
) -> Integer(z){z >= 0 && z <= 100} {
    return a + b;
}
```

The SMT solver must be able to prove that operations preserve these constraints. If `a + b` could exceed the return bound, compilation fails (see `tests/arithmetic_unsafe_bounds.lattice`).

### 5.3 Struct types

```lattice
type Rational(Numerator: Integer, Denominator: Integer) {
    Denominator != 0
}
```

- Fields are declared in parentheses.
- Invariants in trailing `{ ... }` are enforced by the SMT solver at construction and use sites.

Construct with named or positional arguments:

```lattice
const r = Rational(3, 4);
```

### 5.4 Union types

```lattice
type Input[T: Type] = Union[Some[T], None]

type Some[T: Type](Value: T)
type None
```

Construct variants:

```lattice
let present: Input[Integer] = Some(42);
let absent: Input[Integer] = None();
```

### 5.5 Generic types

Type parameters use square brackets with optional kind annotations:

```lattice
type List[LenList: Integer, Elem: Type](data: Group[LenList, Elem]) {
    LenList >= 0
}
```

`Group[Len, Elem]` is a compiler builtin representing a fixed-size contiguous buffer. It is used internally by `List` and is not typically referenced directly in user code.

### 5.6 `List[N, T]`

A fixed-capacity array of `N` elements of type `T`:

```lattice
let my_list: List[5, Integer] = List([0, 0, 0, 0, 0]);
```

Access the backing buffer via `.data`:

```lattice
my_list.data[idx]
```

List literals use square brackets: `[1, 2, 3]`.

### 5.7 `String[MaxLen]`

A bounded string with a runtime length no greater than `MaxLen`:

```lattice
type String[MaxLen: Integer](len: Integer, data: List[MaxLen, Char]) {
    len >= 0 && len <= MaxLen
}
```

Construct with length first, then the character list:

```lattice
let msg: String[12] = String(12, List(['H', 'e', 'l', 'l', 'o', ' ', 'w', 'o', 'r', 'l', 'd', '!']));
```

Access characters through `s.data.data[idx]` after proving `idx < s.len`.

### 5.8 `IO[T]`

Marks a computation that performs host side effects. The inner type `T` is the result. IO operations are provided by the standard library and host runtime.

---

## 6. Functions

### 6.1 Declaration syntax

```lattice
function name[Generics](params) -> ReturnType {
    // body
}
```

Return type and parameter types can be omitted when inferable.

### 6.2 Function kinds

| Kind | Syntax | Purpose |
|------|--------|---------|
| Normal | `function foo(...)` | Internal logic |
| External | `external function foo(...)` | Exported to host or other modules |
| Server | `server function foo(...)` | Parsed but not yet code-generated |

### 6.3 Preconditions

Place constraints between the signature and body. They must hold for the function to be called safely:

```lattice
function get[N: Integer, T: Type](list: List[N, T], idx: Integer) -> T {
    idx >= 0 && idx < N;
} {
    return list.data[idx];
}
```

The caller must establish these facts (via guards, refined types, or prior checks) or compilation fails.

### 6.4 Entry point: `main`

Every program must define `external function main` **in the entry source file**:

```lattice
external function main(n) {
    return factorial(n);
}
```

Rules enforced by the compiler:

- `main` cannot be imported from another module
- `main` must use the `external` modifier
- `main` must be defined in the file passed to the compiler

CLI arguments are passed as integers from the host.

---

## 7. Control Flow

### 7.1 Conditionals

```lattice
if (n == 1) {
    return 1;
} else {
    return n * factorial(n - 1);
}
```

Guards that establish bounds or constraints allow the verifier to accept otherwise unsafe operations:

```lattice
if (idx >= 0 && idx < N) {
    return list.data[idx];  // safe inside the guard
}
```

### 7.2 Pattern matching

`match` must be **exhaustive** over all union variants:

```lattice
match (opt) {
    Some(val) => { return val; }
    None => { return default_val; }
}
```

Omitting a variant is a compile error (see `tests/option_unsafe.lattice`).

---

## 8. Imports and Modules

Import symbols from other `.lattice` files:

```lattice
import Fraction, multiply_frac from 'math_utils.lattice';
import * from 'math_utils.lattice';
import add_five from 'dep_b.lattice';
```

Rules:

- Only `external function` and `external type` declarations are importable
- Internal functions and non-external types cannot be imported (see `tests/import_test_unsafe_internal.lattice`)
- Redefining stdlib or imported symbols is an error
- Transitive imports are supported (`dep_b` → `dep_c`)

Export from a module:

```lattice
external type Fraction(num: Integer, den: Integer) {
    den != 0
}

external function multiply_frac(a: Fraction, b: Fraction) -> Fraction {
    return Fraction(a.num * b.num, a.den * b.den);
}
```

---

## 9. Standard Library

The standard library is embedded in `compiler/stdlib.py` and loaded automatically. It provides:

**Types:** `Rational`, `Input`, `Some`, `None`, `List`, `String`

**Rational arithmetic:** `add`, `sub`, `mul`, `div` (with non-zero divisor checks)

**IO:**

| Function | Description |
|----------|-------------|
| `print_char(c: Char) -> IO[void]` | Print one character |
| `print_string(s: String[N]) -> IO[void]` | Print a string |
| `read_int() -> IO[Input[Integer]]` | Read integer from stdin |
| `read_string[N]() -> IO[Input[String[N]]]` | Read string from stdin |
| `read_file[N, P](path: String[P]) -> IO[Input[String[N]]]` | Read file contents |
| `http_get[N, U](url: String[U]) -> IO[Input[String[N]]]` | HTTP GET (host uses curl) |

---

## 10. Safety Verification

The compiler rejects programs it cannot prove safe. The test suite in `tests/tests.json` documents expected behavior.

Verification is **fail-closed**: if the SMT solver cannot translate or prove a required constraint, compilation fails rather than silently assuming safety.

### 10.1 Index bounds

List and string indexing requires a proof that `0 <= index < length`. Unguarded out-of-bounds access fails (see `tests/list_unsafe.lattice`, `tests/string_unsafe.lattice`).

### 10.2 Refined type constraints

Arithmetic results must satisfy declared output constraints (see `tests/arithmetic_unsafe_bounds.lattice`).

### 10.3 Division and invariants

Division by zero and violated struct invariants are rejected (see `tests/arithmetic_unsafe_div.lattice`).

### 10.4 Termination

Recursion must decrease a provably positive argument. Increasing recursion (`n + 1`) is rejected (see `tests/factorial_incorrect_termination.lattice`).

### 10.5 Match exhaustiveness

All union variants must be handled (see `tests/option_unsafe.lattice`).

### 10.6 Module boundaries

Importing non-external symbols fails. Redefining stdlib symbols fails.

### 10.7 Type checking and generics

- Function calls are checked for arity and structural type compatibility
- Generic size parameters must be inferable at call sites; silent fallback values are not used during verification
- `const` variables cannot be reassigned
- Union `match` exhaustiveness uses variant base names (`Some` matches `Some_Integer`)

### 10.8 Standard library verification

The embedded standard library is resolved and SMT-verified at compiler startup. IO wrappers (`read_file`, `http_get`) include preconditions that `url.len` / `path.len` stay within declared capacity.

### 10.9 Compiler diagnostics

Errors include a short `hint:` line when the compiler can suggest a fix:

- **Type mismatches** show expected and actual types using readable names (`String[20]`, `List[5, Integer]`)
- **Generic inference failures** name the callee and suggest explicit instantiation (e.g. `read_file[1024, 256](path)`)
- **Static size errors** explain that Lattice has no heap — capacities must appear in types or generic arguments
- **Safety failures** show the constraint that could not be proved (e.g. `idx >= 0 && idx < N`) and how to guard it

---

## 11. Compilation Pipeline

```
.lattice source
    → parse (parser.py)
    → resolve types & monomorphize (resolver.py)
    → verify safety with Z3 (verifier.py)
    → emit WASM binary (emitter.py)
    → run via Node.js host (run_wasm.js)
```

---

## 12. Reference Programs

The `tests/` directory contains canonical Lattice programs. Start with these:

| File | Demonstrates |
|------|--------------|
| `factorial.lattice` | Recursion with refined types |
| `arithmetic_safe.lattice` | Refined integers and rationals |
| `list_safe.lattice` | Fixed lists, preconditions, mutation |
| `option_safe.lattice` | Union types and exhaustive match |
| `string_safe.lattice` | Bounded strings |
| `io_safe.lattice` | Printing |
| `io_advanced_safe.lattice` | File I/O |
| `import_test_safe.lattice` | Module imports |
| `import_transitive_safe.lattice` | Transitive imports |

Files ending in `_unsafe` or listed with `"should_fail": true` in `tests/tests.json` show programs the compiler correctly rejects.

---

## 13. Known Limitations

These are **not** part of the current language, even if they appear in older drafts:

- No dynamic allocation or unbounded collections
- No general multi-dispatch overloading (stdlib defines explicit `add` for `Rational`)
- `server function` is parsed but not code-generated
- `IO[T]` is not enforced as an effect system; it documents host interactions
- WASM values are lowered primarily as i32; width optimization is not yet implemented
- CLI `main` arguments are untyped integers from the host
