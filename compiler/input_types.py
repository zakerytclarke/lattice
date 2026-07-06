from compiler.errors import LatticeTypeError, format_type, format_type_expr, static_memory_hint
from compiler.parser import TypeExpr


def _struct_string_max_len(resolved_type):
    name = getattr(resolved_type, "name", "")
    if name.startswith("String_"):
        suffix = name[len("String_") :]
        if suffix.isdigit():
            return int(suffix)
    return None


def validate_input_target_type(resolved_type, type_expr, line):
    if resolved_type is None:
        raise LatticeTypeError("Cannot infer the result type of input(...)", line, hint=_input_hint())

    kind = type(resolved_type).__name__

    if kind == "PrimitiveResolvedType":
        if resolved_type.name in ("Integer", "Bool"):
            return
        raise LatticeTypeError(
            f"input(...) does not support type {format_type(resolved_type)}",
            line,
            hint="Supported types: Integer, Bool, String(N), Rational, List(N)[T] (JSON array).",
        )

    if kind == "RefinedResolvedType":
        validate_input_target_type(resolved_type.base_type, type_expr, line)
        return

    if kind == "StructResolvedType":
        if resolved_type.name == "Rational":
            return
        max_len = _struct_string_max_len(resolved_type)
        if max_len is not None:
            return
        raise LatticeTypeError(
            f"input(...) does not support struct type {format_type(resolved_type)}",
            line,
            hint="For JSON objects, define an explicit struct type with fixed fields, or use String(N) for raw JSON text.",
        )

    if kind == "ListResolvedType":
        if resolved_type.length is None:
            raise LatticeTypeError(
                "input(...) requires a fixed List capacity for JSON array parsing",
                line,
                hint="Use List(N)[T] with an explicit N, e.g. let xs: List(5)[Integer] = input(\"values: \");",
            )
        validate_input_target_type(resolved_type.elem_type, None, line)
        return

    raise LatticeTypeError(
        f"input(...) does not support type {format_type(resolved_type)}",
        line,
        hint=_input_hint(),
    )


def _input_hint():
    return (
        "Annotate the binding with an explicit type, e.g. "
        "let n: Integer = input(\"n: \"); or let s: String(64) = input(\"name: \"); "
        + static_memory_hint()
    )


def serialize_input_type(resolved_type, type_expr):
    entry = {"type": format_type_expr(type_expr) if type_expr else format_type(resolved_type)}

    if type(resolved_type).__name__ == "RefinedResolvedType":
        from compiler.entry_metadata import serialize_constraint_expr

        entry["base"] = resolved_type.base_type.name
        entry["constraint_var"] = resolved_type.constraint_var
        entry["constraint"] = serialize_constraint_expr(resolved_type.constraint)
        return entry

    if type(resolved_type).__name__ == "PrimitiveResolvedType":
        entry["base"] = resolved_type.name
        return entry

    if type(resolved_type).__name__ == "StructResolvedType":
        if resolved_type.name == "Rational":
            entry["base"] = "Rational"
            return entry
        max_len = _struct_string_max_len(resolved_type)
        if max_len is not None:
            entry["base"] = "String"
            entry["max_len"] = max_len
            return entry
        entry["base"] = resolved_type.name
        return entry

    if type(resolved_type).__name__ == "ListResolvedType":
        entry["base"] = "List"
        entry["length"] = resolved_type.length
        entry["elem"] = serialize_input_type(resolved_type.elem_type, None)
        return entry

    entry["base"] = format_type(resolved_type)
    return entry
