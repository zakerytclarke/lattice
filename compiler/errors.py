# Lattice compiler error formatting and exception types

import re


class LatticeSyntaxError(SyntaxError):
    def __init__(self, message, line, column=1, hint=None):
        self.message = message
        self.line = line
        self.column = column
        self.hint = hint
        self.file_name = None
        self.source_lines = None
        super().__init__(message)

    def __str__(self):
        return self.message


class LatticeTypeError(Exception):
    def __init__(self, message, line, column=None, hint=None, span=1):
        self.message = message
        self.line = line
        self.column = column
        self.span = span
        self.hint = hint
        self.file_name = None
        self.source_lines = None

    def __str__(self):
        return self.message


class SafetyError(Exception):
    def __init__(self, message, line, column=None, hint=None, span=1):
        self.message = message
        self.line = line
        self.column = column
        self.span = span
        self.hint = hint
        self.file_name = None
        self.source_lines = None

    def __str__(self):
        return self.message


def _find_highlight(message, source_line):
    quoted = re.findall(r"'([^']*)'", message)
    for name in sorted(quoted, key=len, reverse=True):
        if not name:
            continue
        idx = source_line.find(name)
        if idx >= 0:
            return idx + 1, len(name)
    return None, 1


def format_source_snippet(source_lines, line, column=None, span=1, message=None):
    if not source_lines or line < 1 or line > len(source_lines):
        return ""

    src = source_lines[line - 1]
    if column is None and message:
        found_col, found_span = _find_highlight(message, src)
        if found_col is not None:
            column = found_col
            span = found_span
    column = column or 1
    span = max(span, 1)

    gutter = len(str(line))
    lines = [
        f"{' ' * gutter} |",
        f"{line:>{gutter}} | {src}",
        f"{' ' * gutter} | {' ' * (column - 1)}{'^' * span}",
    ]
    return "\n".join(lines)


def _error_kind(error):
    if isinstance(error, LatticeSyntaxError):
        return "Syntax Error"
    if isinstance(error, SafetyError):
        return "Safety Error"
    if isinstance(error, LatticeTypeError):
        return "Type Error"
    if isinstance(error, SyntaxError):
        return "Syntax Error"
    return "Error"


def format_compilation_error(error, file_name=None, source_lines=None):
    file_name = file_name or getattr(error, "file_name", None)
    source_lines = source_lines or getattr(error, "source_lines", None)

    line = getattr(error, "line", None)
    if line is None and isinstance(error, SyntaxError):
        line = getattr(error, "lineno", None)

    column = getattr(error, "column", None)
    if column is None and isinstance(error, SyntaxError):
        column = getattr(error, "offset", None)

    span = getattr(error, "span", 1)
    message = getattr(error, "message", None) or str(error)
    hint = getattr(error, "hint", None)
    kind = _error_kind(error)

    header = "Compilation Error"
    if file_name:
        if line is not None and column is not None:
            header += f" in {file_name}:{line}:{column}"
        elif line is not None:
            header += f" in {file_name}:{line}"
        else:
            header += f" in {file_name}"

    parts = [header, "", f"{kind}: {message}"]

    if line is not None:
        snippet = format_source_snippet(source_lines, line, column, span, message)
        if snippet:
            parts.extend(["", snippet])

    if hint:
        parts.extend(["", f"  hint: {hint}"])

    return "\n".join(parts)


def error_site_from_expr(expr):
    from compiler.parser import CallExpr, FieldExpr, Identifier

    line = getattr(expr, "line", 0)
    column = getattr(expr, "column", None)
    span = 1
    highlight = None

    if isinstance(expr, CallExpr) and isinstance(expr.func, Identifier):
        column = getattr(expr.func, "column", column)
        highlight = expr.func.name
        span = len(expr.func.name)
    elif isinstance(expr, Identifier):
        highlight = expr.name
        span = len(expr.name)
    elif isinstance(expr, FieldExpr):
        column = getattr(expr, "column", column)
        highlight = expr.field
        span = len(expr.field)

    return line, column, span


def format_expr(expr):
    from compiler.parser import (
        BinaryExpr,
        CallExpr,
        FieldExpr,
        Identifier,
        IndexExpr,
        Literal,
        TypeExpr,
    )

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
    from compiler.parser import Literal, TypeExpr

    if not te:
        return "void"

    def format_size(size):
        if isinstance(size, Literal):
            return str(size.value)
        if isinstance(size, TypeExpr):
            return size.name
        return str(size)

    size_part = ""
    if getattr(te, "size", None) is not None:
        size_part = f"({format_size(te.size)})"

    if te.name in ("List", "Group") and te.args:
        return f"{te.name}{size_part}[{format_type_expr(te.args[0])}]"
    if te.name == "String":
        return f"String{size_part}" if size_part else "String"

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
        base = f"{te.name}{size_part}" if size_part else te.name
        size_part = ""

    if getattr(te, 'constraint', None) is not None:
        var = getattr(te, 'constraint_var', 'x')
        return f"{base}({var}){{{format_expr(te.constraint)}}}"
    return base


def _format_struct_name(name):
    if name.startswith("String_"):
        suffix = name[len("String_"):]
        if suffix.isdigit():
            return f"String({suffix})"
    if name.startswith("List_"):
        parts = name[len("List_"):].split("_", 1)
        if len(parts) == 2 and parts[1].isdigit():
            return f"List({parts[1]})[{parts[0]}]"
        if len(parts) == 2 and parts[0].isdigit():
            return f"List({parts[0]})[{parts[1]}]"
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
        return f"{format_type(resolved_type.base_type)}({resolved_type.constraint_var}){{{format_expr(resolved_type.constraint)}}}"

    if kind == 'ListResolvedType':
        length = resolved_type.length
        elem = format_type(resolved_type.elem_type)
        if length is None:
            return f"List[{elem}]"
        return f"List({length})[{elem}]"

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
        "Specify capacities in types (e.g. List(5)[Integer], String(64)) or pass explicit generics "
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
