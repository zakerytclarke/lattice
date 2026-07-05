# Lattice compiler error formatting and exception types

from compiler.parser import (
    BinaryExpr,
    CallExpr,
    FieldExpr,
    Identifier,
    IndexExpr,
    Literal,
    TypeExpr,
)


class LatticeTypeError(Exception):
    def __init__(self, message, line, hint=None):
        self.message = message
        self.line = line
        self.hint = hint

    def __str__(self):
        text = f"Type Error at line {self.line}: {self.message}"
        if self.hint:
            text += f"\n  hint: {self.hint}"
        return text


class SafetyError(Exception):
    def __init__(self, message, line, hint=None):
        self.message = message
        self.line = line
        self.hint = hint

    def __str__(self):
        text = f"Safety Error at line {self.line}: {self.message}"
        if self.hint:
            text += f"\n  hint: {self.hint}"
        return text


def format_compilation_error(error, file_name=None):
    file_name = file_name or getattr(error, 'file_name', None)
    prefix = "Compilation Error"
    if file_name:
        prefix += f" in {file_name}"
    return f"{prefix}: {error}"


def format_expr(expr):
    if expr is None:
        return "<unknown>"
    if isinstance(expr, Literal):
        if expr.val_type == 'Char':
            return repr(expr.value)
        return str(expr.value)
    if isinstance(expr, Identifier):
        return expr.name
    if isinstance(expr, BinaryExpr):
        return f"{format_expr(expr.left)} {expr.op} {format_expr(expr.right)}"
    if isinstance(expr, FieldExpr):
        return f"{format_expr(expr.expr)}.{expr.field}"
    if isinstance(expr, IndexExpr):
        return f"{format_expr(expr.expr)}[{format_expr(expr.index)}]"
    if isinstance(expr, CallExpr) and isinstance(expr.func, Identifier):
        args = ", ".join(format_expr(arg) for arg in expr.args)
        if expr.generic_args:
            gargs = ", ".join(str(g) for g in expr.generic_args)
            return f"{expr.func.name}[{gargs}]({args})"
        return f"{expr.func.name}({args})"
    if isinstance(expr, TypeExpr):
        return format_type_expr(expr)
    return repr(expr)


def format_type_expr(te):
    if not te:
        return "void"
    if te.args:
        parts = []
        for arg in te.args:
            if isinstance(arg, TypeExpr):
                parts.append(format_type_expr(arg))
            elif isinstance(arg, Literal):
                parts.append(str(arg.value))
            else:
                parts.append(str(arg))
        base = f"{te.name}[{', '.join(parts)}]"
    else:
        base = te.name
    if getattr(te, 'constraint', None) is not None:
        var = getattr(te, 'constraint_var', 'x')
        return f"{base}({var}){{...}}"
    return base


def _format_struct_name(name):
    if name.startswith("String_"):
        suffix = name[len("String_"):]
        if suffix.isdigit():
            return f"String[{suffix}]"
    if name.startswith("List_"):
        parts = name[len("List_"):].split("_", 1)
        if len(parts) == 2 and parts[0].isdigit():
            return f"List[{parts[0]}, {parts[1]}]"
    if name.startswith("Some_"):
        return f"Some[{name[len('Some_'):]}]"
    if name.startswith("Input_"):
        return f"Input[{name[len('Input_'):]}]"
    return name


def format_type(resolved_type):
    if resolved_type is None:
        return "<unknown>"

    kind = type(resolved_type).__name__

    if kind == 'PrimitiveResolvedType':
        return resolved_type.name

    if kind == 'RefinedResolvedType':
        return f"{format_type(resolved_type.base_type)}({resolved_type.constraint_var}){{...}}"

    if kind == 'ListResolvedType':
        length = resolved_type.length
        if length is None:
            length = "?"
        return f"List[{length}, {format_type(resolved_type.elem_type)}]"

    if kind == 'IOResolvedType':
        return f"IO[{format_type(resolved_type.inner)}]"

    if kind == 'UnionResolvedType':
        if resolved_type.name and resolved_type.name != 'Union':
            if resolved_type.variants:
                inner = ", ".join(format_type(v) for v in resolved_type.variants)
                return f"{resolved_type.name}[{inner}]"
            return resolved_type.name
        variants = ", ".join(format_type(v) for v in resolved_type.variants)
        return f"Union[{variants}]"

    if kind == 'StructResolvedType':
        return _format_struct_name(resolved_type.name)

    return str(resolved_type)


def static_memory_hint():
    return (
        "Lattice has no heap or growable stack — every value needs a fixed size at compile time. "
        "Specify capacities in types (e.g. List[5, Integer], String[64]) or pass explicit generics "
        "(e.g. read_file[1024, 256](path))."
    )


def generic_inference_hint(callee_name, generic_decls, generic_map):
    unresolved = [gname for gname, _ in generic_decls if generic_map.get(gname) is None]
    if not unresolved:
        return None

    details = []
    for gname, gtype_expr in generic_decls:
        if gname not in unresolved:
            continue
        gtype_name = gtype_expr.name if hasattr(gtype_expr, 'name') else str(gtype_expr)
        if gtype_name == 'Type':
            details.append(f"{gname} (element/type parameter — infer from a typed argument)")
        else:
            details.append(f"{gname} (static size — number of elements or max string length)")

    example_args = ", ".join(
        "?" if gname in unresolved else str(generic_map[gname])
        for gname, _ in generic_decls
    )
    return (
        f"Cannot infer compile-time parameter(s) for '{callee_name}': {', '.join(unresolved)}. "
        f"Parameters: {'; '.join(details)}. "
        f"Try: {callee_name}[{example_args}](...). "
        + static_memory_hint()
    )


def type_mismatch_message(expected, actual, context):
    return f"{context}: expected {format_type(expected)}, got {format_type(actual)}"


def type_mismatch_hint(expected, actual):
    if type(expected).__name__ == 'ListResolvedType' and type(actual).__name__ == 'ListResolvedType':
        if expected.length != actual.length:
            return (
                f"List capacity must match exactly ({expected.length} vs {actual.length} elements). "
                + static_memory_hint()
            )
    if type(expected).__name__ == 'StructResolvedType' and type(actual).__name__ == 'StructResolvedType':
        if expected.name != actual.name:
            return "Struct and union types must match exactly — memory layout is fixed at compile time."
    return None
