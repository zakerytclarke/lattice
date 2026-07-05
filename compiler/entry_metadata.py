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
    if getattr(te, "constraint", None) is not None:
        entry["constraint_var"] = getattr(te, "constraint_var", "x")
        entry["constraint"] = serialize_constraint_expr(te.constraint)
    return entry


def build_main_metadata(main_decl):
    return {
        "main": {
            "params": [serialize_param(p) for p in main_decl.params],
        }
    }


def write_main_metadata(main_decl, output_path):
    metadata = build_main_metadata(main_decl)
    meta_path = output_path + ".meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
        f.write("\n")
