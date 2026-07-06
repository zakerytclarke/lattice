# Embedded Lattice Standard Library

STDLIB_CODE = """
// 1. Rational Numbers
// Represents a mathematical fraction with a non-zero denominator constraint.
type Rational(Numerator: Integer, Denominator: Integer) {
    Denominator != 0
}

// Rational addition
function add(a: Integer, b: Integer) -> Integer {
    return a + b;
}

function add(a: Rational, b: Rational) -> Rational {
    return Rational(
        a.Numerator * b.Denominator + b.Numerator * a.Denominator,
        a.Denominator * b.Denominator
    );
}

function add(a: Rational, b: Integer) -> Rational {
    return add(a, Rational(b, 1));
}

function add(a: Integer, b: Rational) -> Rational {
    return add(Rational(a, 1), b);
}

// Rational subtraction
function sub(a: Rational, b: Rational) -> Rational {
    return Rational(
        a.Numerator * b.Denominator - b.Numerator * a.Denominator,
        a.Denominator * b.Denominator
    );
}

// Rational multiplication
function mul(a: Rational, b: Rational) -> Rational {
    return Rational(
        a.Numerator * b.Numerator,
        a.Denominator * b.Denominator
    );
}

// Rational division
function div(a: Rational, b: Rational) -> Rational {
    b.Numerator != 0
} {
    return Rational(
        a.Numerator * b.Denominator,
        a.Denominator * b.Numerator
    );
}

// Integer remainder
function mod(a: Integer, b: Integer) -> Integer {
    b != 0;
} {
    return a % b;
}

// Data equality (field/value comparison, not pointer identity)
function equal(a: Integer, b: Integer) -> Bool {
    return a == b;
}

function not_equal(a: Integer, b: Integer) -> Bool {
    return a != b;
}

function equal(a: Bool, b: Bool) -> Bool {
    return (a && b) || (!a && !b);
}

function not_equal(a: Bool, b: Bool) -> Bool {
    return !equal(a, b);
}

function equal(a: Char, b: Char) -> Bool {
    return a == b;
}

function not_equal(a: Char, b: Char) -> Bool {
    return a != b;
}

function equal(a: Rational, b: Rational) -> Bool {
    return a.Numerator == b.Numerator && a.Denominator == b.Denominator;
}

function not_equal(a: Rational, b: Rational) -> Bool {
    return !equal(a, b);
}

// Boolean logic (operators desugar to these; bodies use primitive &&, ||, !)
function and(a: Bool, b: Bool) -> Bool {
    return a && b;
}

function or(a: Bool, b: Bool) -> Bool {
    return a || b;
}

function not(a: Bool) -> Bool {
    return !a;
}

function xor(a: Bool, b: Bool) -> Bool {
    return (a || b) && !(a && b);
}


// 2. Input / Maybe Type
// An algebraic union type representing either a present value (Some) or absence of value (None).
type Input[T: Type] = Union[Some[T], None]

type Some[T: Type](Value: T)

type None


// 3. Statically Sized List Definition
// Under the hood, this allocates a static contiguous Group in memory.
type List[Elem: Type, LenList: Integer](data: Group(LenList)[Elem]) {
    LenList >= 0
}


// 4. Statically Sized String Definition
// A String wraps a List of Characters with a length check.
// We put len first so that its offset is constant (0) and doesn't depend on MaxLen.
type String[MaxLen: Integer](len: Integer, data: List(MaxLen)[Char]) {
    len >= 0 && len <= MaxLen
}


// 5. IO Functions and System Interaction
// Low-level raw external imports from Javascript
external function print_char(val: Char) -> IO[void] {}
external function print_raw_string(addr: Integer, len: Integer) -> IO[void] {}
external function read_int_raw(out_ptr: Integer) -> IO[void] {}
external function read_string_raw(out_ptr: Integer, max_len: Integer) -> IO[void] {}
external function read_file_raw(path_ptr: Integer, path_len: Integer, out_ptr: Integer, max_len: Integer) -> IO[void] {}
external function http_get_raw(url_ptr: Integer, url_len: Integer, out_ptr: Integer, max_len: Integer) -> IO[void] {}
external function input_typed(call_id: Integer, prompt_ptr: Integer, out_ptr: Integer) -> IO[void] {}
external function concat_strings(out_ptr: Integer, a_ptr: Integer, b_ptr: Integer, max_out: Integer) -> void {}
external function strings_equal(a_ptr: Integer, b_ptr: Integer) -> Integer {}

// High-level safe wrapper functions
function print[MaxLen: Integer](s: String(MaxLen)) -> IO[void] {
    print_string(s);
}

function print_string[MaxLen: Integer](s: String(MaxLen)) -> IO[void] {
    print_raw_string(s.data.data, s.len);
}

function read_int() -> IO[Input[Integer]] {
    let result: Input[Integer] = None();
    read_int_raw(result);
    return result;
}

function read_string[MaxLen: Integer]() -> IO[Input[String(MaxLen)]] {
    let result: Input[String(MaxLen)] = None();
    read_string_raw(result, MaxLen);
    return result;
}

function read_file[MaxLen: Integer, PathLen: Integer](path: String(PathLen)) -> IO[Input[String(MaxLen)]] {
    path.len >= 0 && path.len <= PathLen;
} {
    let result: Input[String(MaxLen)] = None();
    read_file_raw(path, path.len, result, MaxLen);
    return result;
}

function http_get[MaxLen: Integer, UrlLen: Integer](url: String(UrlLen)) -> IO[Input[String(MaxLen)]] {
    url.len >= 0 && url.len <= UrlLen;
} {
    let result: Input[String(MaxLen)] = None();
    http_get_raw(url, url.len, result, MaxLen);
    return result;
}

function concat[MaxOut: Integer, MaxA: Integer, MaxB: Integer](a: String(MaxA), b: String(MaxB)) -> String(MaxOut) {
    a.len + b.len <= MaxOut;
} {
    return a + b;
}

function equal[MaxA: Integer, MaxB: Integer](a: String(MaxA), b: String(MaxB)) -> Bool {
    return strings_equal(a, b) != 0;
}

function not_equal[MaxA: Integer, MaxB: Integer](a: String(MaxA), b: String(MaxB)) -> Bool {
    return !equal(a, b);
}

function equal[N: Integer, T: Type](a: List(N)[T], b: List(N)[T]) -> Bool {
    for i in 0..N {
        if (!equal(a.data[i], b.data[i])) {
            return false;
        }
    }
    return true;
}

function not_equal[N: Integer, T: Type](a: List(N)[T], b: List(N)[T]) -> Bool {
    return !equal(a, b);
}
"""
