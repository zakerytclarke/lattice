# Embedded Lattice Standard Library

STDLIB_CODE = """
// 1. Rational Numbers
// Represents a mathematical fraction with a non-zero denominator constraint.
type Rational(Numerator: Integer, Denominator: Integer) {
    Denominator != 0
}

// Rational addition
function add(a: Rational, b: Rational) -> Rational {
    return Rational(
        a.Numerator * b.Denominator + b.Numerator * a.Denominator,
        a.Denominator * b.Denominator
    );
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


// 2. Input / Maybe Type
// An algebraic union type representing either a present value (Some) or absence of value (None).
type Input[T: Type] = Union[Some[T], None]

type Some[T: Type](Value: T)

type None


// 3. Statically Sized List Definition
// Under the hood, this allocates a static contiguous Group in memory.
type List[LenList: Integer, Elem: Type](data: Group[LenList, Elem]) {
    LenList >= 0
}


// 4. Statically Sized String Definition
// A String wraps a List of Characters with a length check.
// We put len first so that its offset is constant (0) and doesn't depend on MaxLen.
type String[MaxLen: Integer](len: Integer, data: List[MaxLen, Char]) {
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

// High-level safe wrapper functions
function print_string[MaxLen: Integer](s: String[MaxLen]) -> IO[void] {
    print_raw_string(s.data.data, s.len);
}

function read_int() -> IO[Input[Integer]] {
    let result: Input[Integer] = None();
    read_int_raw(result);
    return result;
}

function read_string[MaxLen: Integer]() -> IO[Input[String[MaxLen]]] {
    let result: Input[String[MaxLen]] = None();
    read_string_raw(result, MaxLen);
    return result;
}

function read_file[MaxLen: Integer, PathLen: Integer](path: String[PathLen]) -> IO[Input[String[MaxLen]]] {
    path.len >= 0 && path.len <= PathLen;
} {
    let result: Input[String[MaxLen]] = None();
    read_file_raw(path, path.len, result, MaxLen);
    return result;
}

function http_get[MaxLen: Integer, UrlLen: Integer](url: String[UrlLen]) -> IO[Input[String[MaxLen]]] {
    url.len >= 0 && url.len <= UrlLen;
} {
    let result: Input[String[MaxLen]] = None();
    http_get_raw(url, url.len, result, MaxLen);
    return result;
}
"""
