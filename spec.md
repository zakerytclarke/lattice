# Lattice Language Specification

Lattice is a modern, statically typed, compiled systems programming language designed for predictability, safety, and high performance. It compiles directly to WebAssembly (WASM) and is designed to run in sandboxed, resource-constrained environments.

---

## 1. Core Philosophy & Guarantees

1. **Zero Dynamic Allocation:** Lattice does not have a runtime heap or garbage collector. All memory sizes, capacities, and layout bounds must be known or bounded at compile time.
2. **Compile-Time Termination Proofs:** Except for explicitly declared infinite loops driven by external runtime events, all recursion and loops must be provably terminating at compile time. The compiler utilizes an integrated SMT solver to enforce this.
3. **Memory Safety without Overhead:** Buffer bounds checks are validated at compile time by the SMT solver. Out-of-bounds access triggers a compile-time type error rather than a runtime panic or crash.
4. **Strong Typing & Ergonomic Inference:** Lattice is strongly typed but types can be omitted in most places (variables, parameter types where constraints allow, and return types) and are automatically inferred by the compiler from operation flow.

---

## 2. Syntax & Lexical Grammar

### 2.1 Variable Bindings
Variables are declared using only two keywords:
- **`let`**: A mutable variable. It can be re-assigned.
- **`const`**: An immutable variable. Its value is fixed upon initialization.

```rust
let x = 5;            // Mutable variable
x = x + 1;

const pi = 3.14159;   // Immutable variable
```

*Note on memory allocation:* Since there is no dynamic allocation, all variable sizes are determined at compile-time. The compiler automatically handles static storage placement in WASM memory for variables whose lifetimes or sizes require static allocation.

### 2.2 Function Declarations
Functions are declared using the `function` keyword.

```rust
function add(a, b) {
    return a + b;
}
```

Types can be explicitly written if desired:
```rust
function add(a: Int, b: Int) -> Int {
    return a + b;
}
```

#### Special Entry Points:
1. **`external function`**: Exposes the function to the host environment (e.g. CLI arguments or WASM imports).
   ```rust
   external function main(args: List[2, String[64]]) -> Int { ... }
   ```
2. **`server function`**: Compiles into a network service endpoint. The compiler generates input validation guards automatically based on type invariants.
   ```rust
   server function get_user(id) { ... }
   ```

---

## 3. Type System & Unions

### 3.1 Primitive Types
- `Int`: An integer value. The SMT solver evaluates range bounds to minimize WASM width representations (e.g., `i32`, `i64`).
- `Bool`: A boolean value (`true` or `false`).
- `Char`: A single Unicode code point.
- `String[MaxLen: Int]`: A fixed-capacity sequence of UTF-8 characters.
- `Type`: The metatype of all types, used in generic definitions.

### 3.2 Structs and Parameterized Types
Types are defined using the `type` keyword. Struct types can accept both compile-time value parameters and type parameters. Type constraints are declared in trailing braces `{ ... }`.

```rust
type List[LenList: Int, Elem: Type](data: BuiltinRawBuffer(LenList)[Elem]) {
    LenList >= 0
}

type Rational(Numerator: Int, Denominator: Int) {
    Denominator != 0
}
```

### 3.3 Native Union & Grouped Types
Union types represent structural sum types. They can be parameterized or plain.

```rust
type Number = Union[Int, Rational]
type Input[T: Type] = Union[Some[T], None]
```

### 3.4 Exhaustive Pattern Matching
Unwrapping union or grouped types is performed using the `match` expression. The compiler **exhaustively checks** that all potential types of the union are handled. If any potential type variant is omitted, the compiler throws an error.

```rust
match try_value {
    Some(val) => {
        print(val);
    }
    None => {
        log_error("Value not present");
    }
}
```

### 3.5 Multi-Dispatch Overloading & Union Resolution
Lattice supports ad-hoc polymorphism through multi-dispatch overloading. Functions can be defined multiple times with different type signatures:

```rust
function add(a: Int, b: Int) -> Int {
    return a + b;
}

function add(a: Rational, b: Rational) -> Rational {
    return Rational(
        a.Numerator * b.Denominator + b.Numerator * a.Denominator, 
        a.Denominator * b.Denominator
    );
}
```

#### Exhaustive Multi-Dispatch Verification:
If a variable of a union type `x: Union[Int, Rational]` is passed to `add(x, y)`:
1. The compiler checks every possible concrete type combination from the union arguments.
2. It verifies that a valid overloaded implementation of `add` (or a coercion rule, e.g. `Int` promoted to `Rational`) exists for every combination.
3. If any path is unhandled (e.g. if `add(Rational, Int)` cannot be resolved or promoted), it raises a compile-time type-resolution error.

---

## 4. Zero-Allocation SMT Verification

The core feature of Lattice is the verification of memory access and safety invariants at compile time.

### 4.1 Index Bounds Verification
Every array or list access `list[index]` is validated before compilation. If the compiler cannot prove that $0 \le \text{index} < \text{list.LenList}$, it fails with an index constraint error.

```rust
function get_element[N: Int, T: Type](list: List[N, T], index: Int) -> T {
    // SMT verifies index bounds
    return list.data[index];
}
```

To resolve bounds errors on runtime variables, the programmer must guard the index access:

```rust
if (index >= 0 && index < N) {
    let item = get_element(list, index); // Compiles successfully!
}
```

---

## 5. Recursion & Termination Constraints

Recursive functions must either be in tail-recursive form or accept a decreasing variant parameter (like `depth`) that guarantees termination.

```rust
function contains[Depth: Int](tree: Tree[Depth, Int], val: Int) -> Bool {
    match tree {
        Empty => {
            return false;
        }
        Node(node_val, left, right) => {
            if (val == node_val) {
                return true;
            } else if (val < node_val) {
                return contains[Depth - 1](left, val);
            } else {
                return contains[Depth - 1](right, val);
            }
        }
    }
}
```

---

## 6. Input / Output, Effects, & HTTP Communication

Side effects in Lattice are tracked using the `IO[T]` effect type.

### HTTP Response Parsing & Validation Example
Runtime network calls are performed via `http_get` which returns `IO[Response]`. To safely handle raw payload parsing without dynamic heap allocation, the response validation and parsing follow a strict unwrapping flow:

```rust
type Response(Status: Int, Body: String[1024])

type User(Id: Int, Name: String[64])

function parse_user(body: String[1024]) -> Union[Some[User], None] {
    // Parser validation logic...
}

function fetch_user(url: String[256]) -> IO[Union[Some[User], None]] {
    const response = http_get(url); // Returns Response struct
    
    // 1. Validate response status code
    if (response.Status == 200) {
        // 2. Parse response body and return Union[Some[User], None]
        return parse_user(response.Body);
    } else {
        return None;
    }
}
```
In this example:
- The network call status is verified.
- The payload is parsed within a fixed capacity string buffer.
- The result is wrapped in the structural union `Union[Some[User], None]`, forcing the caller to handle success and failure exhaustively using `match`.
