# Lattice

**Write proven programs.**

Lattice is a statically typed language that compiles to WebAssembly. Every program has fixed memory bounds, and the compiler uses an SMT solver to reject out-of-bounds access, constraint violations, non-terminating recursion, and incomplete pattern matches **before** the program runs.

The language specification and reference programs live in [`spec.md`](spec.md) and [`tests/`](tests/).

---

## Principles

1. **Safety is a compile-time property.** If Lattice accepts your program, index accesses are in bounds, invariants hold, and recursion terminates — or you have explicitly guarded the operation so the solver can prove it.

2. **Memory is bounded and static.** Lists, strings, and structs have sizes fixed at compile time. There is no heap and no garbage collector.

3. **Types carry proofs.** Refined types like `Integer(x){x > 0}` and struct invariants like `Denominator != 0` are checked by Z3, not at runtime.

4. **Unions require exhaustive handling.** `match` must cover every variant. Partial matches are compile errors.

5. **Modules have clear boundaries.** Only `external` functions and types can be imported. `main` must be `external` and live in the entry file.

---

## Quick Start

### Prerequisites

- Python 3.10+
- [Node.js](https://nodejs.org/) (for running compiled WASM)
- `curl` (only needed for `http_get` in the stdlib)

### Install

```bash
git clone https://github.com/zakerytclarke/lattice.git
cd lattice
pip install -e ".[dev]"
```

### Run a program

```bash
./lattice tests/factorial.lattice 5
# Output: 120
```

### Run tests

```bash
python3 -m pytest tests/test_lattice.py -v
```

---

## Project Layout

```
lattice                 # CLI: compile + run
spec.md                 # Language specification (canonical)
compiler/
  main.py               # Compiler driver and module loader
  parser.py             # Lexer, parser, AST
  resolver.py           # Type inference and monomorphization
  verifier.py           # Z3 safety and termination checks
  emitter.py            # WASM code generation
  stdlib.py             # Embedded standard library
run_wasm.js             # Node.js WASM host (IO imports)
tests/
  *.lattice             # Reference programs and regression tests
  tests.json            # Test manifest
  test_lattice.py       # Pytest harness
```

---

## Example

From `tests/factorial.lattice`:

```lattice
function factorial(n: Integer(x){x > 0}) -> Integer {
    if (n == 1) {
        return 1;
    } else {
        return n * factorial(n - 1);
    }
}

external function main(n) {
    return factorial(n);
}
```

The refined type `x > 0` lets the verifier prove that `n - 1` remains valid and that recursion terminates.

---

## What the Compiler Rejects

| Violation | Example test |
|-----------|--------------|
| Out-of-bounds list access | `list_unsafe.lattice` |
| Out-of-bounds string access | `string_unsafe.lattice` |
| Arithmetic exceeding refined bounds | `arithmetic_unsafe_bounds.lattice` |
| Division with zero numerator | `arithmetic_unsafe_div.lattice` |
| Non-terminating recursion | `factorial_incorrect_termination.lattice` |
| Non-exhaustive `match` | `option_unsafe.lattice` |
| Importing internal symbols | `import_test_unsafe_internal.lattice` |
| `main` not external or imported | `main_not_external.lattice`, `main_imported.lattice` |

See [`tests/tests.json`](tests/tests.json) for the full list.

---

## License

MIT — see [LICENSE](LICENSE).
