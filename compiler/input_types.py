from compiler.errors import LatticeTypeError, format_type, format_type_expr, static_memory_hint
from compiler.parser import TypeExpr


def _struct_string_max_len(resolved_type):
    name = getattr(resolved_type, "name", "")
    if name.startswith("String_"):
        suffix = name[len("String_") :]
        if suffix.isdigit():
            return int(suffix)
    return None


def _is_input_resolved_type(resolved_type):
    kind = type(resolved_type).__name__
    return kind == "UnionResolvedType" and getattr(resolved_type, "name", "").startswith("Input_")


def unwrap_input_inner(resolved_type):
    if not _is_input_resolved_type(resolved_type):
        return None
    for variant in resolved_type.variants:
        vname = getattr(variant, "name", "")
        if vname.startswith("Some_"):
            for fname, ftype in variant.fields:
                if fname == "Value":
                    return ftype
    return None


def _require_input_inner(resolved_type, callee, line):
    inner = unwrap_input_inner(resolved_type)
    if inner is None:
        raise LatticeTypeError(
            f"{callee}(...) requires an Input[T] annotation",
            line,
            hint=(
                f"Use e.g. let value: Input[Integer] = {callee}(...); then match on Some and None. "
                + static_memory_hint()
            ),
        )
    return inner


def validate_input_target_type(resolved_type, type_expr, line):
    if resolved_type is None:
        raise LatticeTypeError("Cannot infer the result type of input(...)", line, hint=_input_hint())

    inner = _require_input_inner(resolved_type, "input", line)
    _validate_input_inner_type(inner, line)


def _validate_input_inner_type(resolved_type, line):
    kind = type(resolved_type).__name__

    if kind == "PrimitiveResolvedType":
        if resolved_type.name in ("Integer", "Bool"):
            return
        raise LatticeTypeError(
            f"input(...) does not support type {format_type(resolved_type)}",
            line,
            hint="Supported inner types: Integer, Bool, String(N), Rational, List(N)[T] (JSON array).",
        )

    if kind == "RefinedResolvedType":
        _validate_input_inner_type(resolved_type.base_type, line)
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
                hint="Use List(N)[T] with an explicit N, e.g. let xs: Input[List(5)[Integer]] = input(\"values: \");",
            )
        _validate_input_inner_type(resolved_type.elem_type, line)
        return

    raise LatticeTypeError(
        f"input(...) does not support type {format_type(resolved_type)}",
        line,
        hint=_input_hint(),
    )


def _input_hint():
    return (
        "Annotate the binding as Input[T], e.g. "
        "let n: Input[Integer] = input(\"n: \"); then match on Some and None. "
        + static_memory_hint()
    )


def _typed_io_hint(callee):
    return (
        f"Annotate the binding as Input[T], e.g. "
        f"let data: Input[MyStruct] = {callee}(...); or "
        f"let users: Input[List(100)[User]] = {callee}(...); then match on Some and None. "
        + static_memory_hint()
    )


def _read_file_hint():
    return _typed_io_hint("read_file")


def _http_get_hint():
    return _typed_io_hint("http_get")


def _is_list_resolved_type(resolved_type):
    return type(resolved_type).__name__ == "ListResolvedType"


def _validate_input_inner_json_type(inner, callee, line):
    if _is_list_resolved_type(inner):
        if inner.length is None:
            raise LatticeTypeError(
                f"{callee} requires a fixed List capacity for JSON array parsing",
                line,
                hint="Use Input[List(N)[T]] with an explicit N.",
            )
        validate_json_record_type(inner.elem_type, line)
        return

    if type(inner).__name__ == "StructResolvedType":
        validate_json_record_type(inner, line)
        return

    max_len = _struct_string_max_len(inner)
    if max_len is not None:
        return

    raise LatticeTypeError(
        f"{callee} does not support Input[{format_type(inner)}]",
        line,
        hint=_typed_io_hint(callee),
    )


def validate_read_file_target_type(resolved_type, type_expr, line):
    if resolved_type is None:
        raise LatticeTypeError(
            "Cannot infer the result type of read_file(...)",
            line,
            hint=_read_file_hint(),
        )
    inner = _require_input_inner(resolved_type, "read_file", line)
    _validate_input_inner_json_type(inner, "read_file", line)


def validate_http_get_target_type(resolved_type, type_expr, line):
    if resolved_type is None:
        raise LatticeTypeError(
            "Cannot infer the result type of http_get(...)",
            line,
            hint=_http_get_hint(),
        )
    inner = _require_input_inner(resolved_type, "http_get", line)
    _validate_input_inner_json_type(inner, "http_get", line)


def validate_json_record_type(resolved_type, line):
    kind = type(resolved_type).__name__

    if kind == "PrimitiveResolvedType":
        if resolved_type.name in ("Integer", "Bool"):
            return
        raise LatticeTypeError(
            f"read_file list elements do not support type {format_type(resolved_type)}",
            line,
            hint="Supported element types: Integer, Bool, and structs with Integer and StrSlice fields.",
        )

    if kind == "StructResolvedType":
        if resolved_type.name == "Rational":
            raise LatticeTypeError(
                "read_file list elements do not support Rational",
                line,
                hint="Use Integer, Bool, or a struct with Integer and StrSlice fields.",
            )
        max_len = _struct_string_max_len(resolved_type)
        if max_len is not None:
            return
        if not resolved_type.fields:
            raise LatticeTypeError(
                f"read_file list elements do not support empty struct {format_type(resolved_type)}",
                line,
            )
        for fname, ftype in resolved_type.fields:
            fk = type(ftype).__name__
            if fk == "PrimitiveResolvedType" and ftype.name in ("Integer", "Bool"):
                continue
            if fk == "StructResolvedType" and ftype.name == "StrSlice":
                continue
            if fk == "StructResolvedType" and ftype.name == "Rational":
                continue
            if fk == "StructResolvedType" and ftype.name.startswith("String_"):
                continue
            if fk == "StructResolvedType":
                validate_json_record_type(ftype, line)
                continue
            raise LatticeTypeError(
                f"read_file JSON object field '{fname}' has unsupported type {format_type(ftype)}",
                line,
                hint="Struct fields must be Integer, Bool, StrSlice, or nested structs with those field types.",
            )
        return

    raise LatticeTypeError(
        f"read_file list elements do not support type {format_type(resolved_type)}",
        line,
        hint="Supported element types: Integer, Bool, and structs with Integer and StrSlice fields.",
    )


def read_file_json_cap(list_length):
    # JSON source text for list parsing; independent of list slot count.
    return 65536


def serialize_input_site_type(resolved_type, type_expr):
    inner = unwrap_input_inner(resolved_type)
    if inner is not None:
        entry = {
            "type": format_type_expr(type_expr) if type_expr else format_type(resolved_type),
            "base": "Input",
            "inner": serialize_input_type(inner, None),
        }
        if _is_list_resolved_type(inner):
            entry["json_cap"] = read_file_json_cap(inner.length)
        return entry
    return serialize_input_type(resolved_type, type_expr)


def serialize_read_file_type(resolved_type, type_expr):
    return serialize_input_site_type(resolved_type, type_expr)


def serialize_http_get_type(resolved_type, type_expr):
    return serialize_input_site_type(resolved_type, type_expr)


def read_file_string_max_len(resolved_type):
    """Extract String(N) capacity from Input[String(N)] inner type."""
    inner = unwrap_input_inner(resolved_type)
    if inner is not None:
        return _struct_string_max_len(inner)
    kind = type(resolved_type).__name__
    if kind == "IOResolvedType":
        return read_file_string_max_len(resolved_type.inner)
    if kind == "StructResolvedType":
        return _struct_string_max_len(resolved_type)
    return None


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
        if resolved_type.fields:
            entry["fields"] = [
                {"name": fname, **serialize_input_type(ftype, None)}
                for fname, ftype in resolved_type.fields
            ]
        return entry

    if type(resolved_type).__name__ == "ListResolvedType":
        entry["base"] = "List"
        entry["length"] = resolved_type.length
        entry["elem"] = serialize_input_type(resolved_type.elem_type, None)
        return entry

    entry["base"] = format_type(resolved_type)
    return entry
