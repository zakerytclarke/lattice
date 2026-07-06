from compiler.errors import LatticeTypeError
from compiler.parser import CallExpr, Identifier, ListLiteral, Literal


def lower_string_literal(lit, capacity):
    s = lit.value
    n = len(s)
    if capacity < n:
        raise LatticeTypeError(
            f"String literal length {n} exceeds declared capacity {capacity}",
            lit.line,
            hint="Use a larger String(N) type annotation or shorten the literal.",
        )

    chars = [Literal(c, "Char", lit.line) for c in s]
    while len(chars) < capacity:
        chars.append(Literal("\0", "Char", lit.line))

    list_lit = ListLiteral(chars, lit.line)
    list_call = CallExpr(Identifier("List", lit.line), [list_lit], [], lit.line)

    return CallExpr(
        Identifier("String", lit.line),
        [Literal(n, "Integer", lit.line), list_call],
        [capacity],
        lit.line,
    )
