# Serialize main entry-point metadata for the WASM runner

import json

from compiler.errors import format_type_expr
from compiler.parser import BinaryExpr, Identifier, Literal


def serialize_constraint_expr(expr):
    if isinstance(expr, Literal):
        return {"kind": "lit", "value": expr.value}
    if isinstance(expr, Identifier):
        return {"kind": "id", "name": expr.name}
    if isinstance(expr, BinaryExpr):
        return {
            "kind": "bin",
            "op": expr.op,
            "left": serialize_constraint_expr(expr.left),
            "right": serialize_constraint_expr(expr.right),
        }
    return None


def serialize_param(param):
    te = param.type_expr
    if te is None:
        return {
            "name": param.name,
            "type": "Integer",
            "base": "Integer",
        }

    entry = {
        "name": param.name,
        "type": format_type_expr(te),
        "base": te.name,
    }
    if te.name == "String":
        if getattr(te, "size", None) is not None:
            from compiler.parser import Literal

            if isinstance(te.size, Literal) and te.size.val_type == "Integer":
                entry["max_len"] = te.size.value
        else:
            entry["max_len"] = 64
    if getattr(te, "constraint", None) is not None:
        entry["constraint_var"] = getattr(te, "constraint_var", "x")
        entry["constraint"] = serialize_constraint_expr(te.constraint)
    return entry


def serialize_main_input_arg(arg):
    from compiler.input_types import serialize_input_type

    entry = {"name": arg["name"], "addr": arg["addr"]}
    entry.update(serialize_input_type(arg["inner_type"], None))
    return entry


def build_main_metadata(main_decl, input_args=None):
    metadata = {
        "main": {
            "params": [serialize_param(p) for p in main_decl.params],
        }
    }
    if input_args:
        metadata["main"]["input_args"] = [
            serialize_main_input_arg(a) for a in input_args
        ]
    return metadata


def write_main_metadata(main_decl, output_path, input_args=None):
    metadata = build_main_metadata(main_decl, input_args)
    meta_path = output_path + ".meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
        f.write("\n")
