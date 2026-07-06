import json

from compiler.input_types import serialize_input_type


def build_program_metadata(main_decl, input_call_sites):
    from compiler.entry_metadata import build_main_metadata

    metadata = build_main_metadata(main_decl)
    metadata["input_calls"] = []
    for site in input_call_sites:
        metadata["input_calls"].append(
            {
                "id": site["id"],
                "line": site["line"],
                "column": site.get("column", 1),
                **serialize_input_type(site["resolved_type"], site.get("type_expr")),
            }
        )
    return metadata


def write_program_metadata(main_decl, input_call_sites, output_path):
    metadata = build_program_metadata(main_decl, input_call_sites)
    meta_path = output_path + ".meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
        f.write("\n")
