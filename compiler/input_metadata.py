import json

from compiler.input_types import serialize_input_site_type, serialize_read_file_type, serialize_http_get_type


def build_program_metadata(main_decl, input_call_sites, read_file_call_sites=None, http_get_call_sites=None, main_input_args=None):
    from compiler.entry_metadata import build_main_metadata

    metadata = build_main_metadata(main_decl, main_input_args)
    metadata["input_calls"] = []
    for site in input_call_sites:
        metadata["input_calls"].append(
            {
                "id": site["id"],
                "line": site["line"],
                "column": site.get("column", 1),
                **serialize_input_site_type(site["resolved_type"], site.get("type_expr")),
            }
        )
    metadata["read_file_calls"] = []
    for site in read_file_call_sites or []:
        entry = {
            "id": site["id"],
            "line": site["line"],
            "column": site.get("column", 1),
            **serialize_read_file_type(site["resolved_type"], site.get("type_expr")),
        }
        if site.get("json_cap") is not None:
            entry["json_cap"] = site["json_cap"]
        metadata["read_file_calls"].append(entry)
    metadata["http_get_calls"] = []
    for site in http_get_call_sites or []:
        entry = {
            "id": site["id"],
            "line": site["line"],
            "column": site.get("column", 1),
            **serialize_http_get_type(site["resolved_type"], site.get("type_expr")),
        }
        if site.get("json_cap") is not None:
            entry["json_cap"] = site["json_cap"]
        metadata["http_get_calls"].append(entry)
    return metadata


def write_program_metadata(main_decl, input_call_sites, output_path, read_file_call_sites=None, http_get_call_sites=None, main_input_args=None):
    metadata = build_program_metadata(main_decl, input_call_sites, read_file_call_sites, http_get_call_sites, main_input_args)
    meta_path = output_path + ".meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
        f.write("\n")
