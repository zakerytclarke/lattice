# AGENTS.md

## Cursor Cloud specific instructions

Lattice is a single-product CLI toolchain (no long-running services, no ports, no databases, no UI):
- **Compiler** (`compiler/`, Python 3): parses `.lattice` → type-resolves → verifies safety/termination with Z3 → emits `.wasm`.
- **Runner** (`run_wasm.js`, Node.js): instantiates the `.wasm` and provides host I/O imports.
- **CLI** (`./lattice`): chains compile (`python3 compiler/main.py`) + run (`node run_wasm.js`).

Non-obvious notes:
- Dependencies are **undeclared** (no `requirements.txt`/`package.json`). The compiler hard-imports `z3` (`z3-solver` pip package) and will not start without it. `run_wasm.js` uses only Node built-ins (no npm install needed).
- Commands (run from repo root):
  - Test: `python3 -m pytest tests/` (43 tests; harness in `tests/test_lattice.py` shells out to `./lattice` per `tests/tests.json` case).
  - Compile + run end-to-end: `./lattice <source.lattice> [args...]` (e.g. `./lattice tests/factorial.lattice 5` → `120`). CLI args are parsed as numbers and passed to `main`.
  - Compile only: `python3 compiler/main.py <src.lattice> [out.wasm]` (default output `build/<name>.wasm`, auto-created, gitignored).
- Runnable programs live in `tests/`. `spec.lattice` is an illustrative spec/demo program and is not part of the test suite (not guaranteed to compile).
- No linter/formatter is configured.
- `curl` (system binary) is only used at runtime by programs that call `http_get`; not needed for compilation or the test suite.
