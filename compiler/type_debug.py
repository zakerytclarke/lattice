# Type and memory layout reporting for --inspect compiler flag

import os
import shutil
import sys

from compiler.errors import format_expr, format_type, format_type_expr
from compiler.overloads import get_overloads
from compiler.parser import (
    BinaryExpr,
    CallExpr,
    ExprStmt,
    FieldExpr,
    ForStmt,
    FuncDecl,
    IfStmt,
    IndexExpr,
    MatchStmt,
    UnaryExpr,
    VarDecl,
)
from compiler.resolver import IOResolvedType

# Scratch buffer sizes the emitter reserves when converting a number to a string
# inside a concatenation (see emitter string-concat handling): capacity chars
# (each 4 bytes) plus the 8-byte String struct header.
_INT_TO_STRING_TEMP = 16 * 4 + 8
_RATIONAL_TO_STRING_TEMP = 32 * 4 + 8


WASM_DATA_BASE = 1024
WASM_MEMORY_SLACK = 4096
WASM_PAGE_SIZE = 65536
DEFAULT_TERMINAL_WIDTH = 80
BYTES_FIELD_WIDTH = 10
LINE_FIELD_WIDTH = 6
MIN_DESC_WIDTH = 24


def _terminal_width():
    try:
        return shutil.get_terminal_size(fallback=(DEFAULT_TERMINAL_WIDTH, 24)).columns
    except Exception:
        return DEFAULT_TERMINAL_WIDTH


def _wrap_desc(text, width):
    if width < MIN_DESC_WIDTH:
        width = MIN_DESC_WIDTH
    if len(text) <= width:
        return [text]

    lines = []
    rest = text
    while rest:
        if len(rest) <= width:
            lines.append(rest)
            break
        chunk = rest[: width + 1]
        break_at = chunk.rfind(", ")
        if break_at == -1:
            break_at = chunk.rfind(" ")
        if break_at == -1:
            break_at = width
        line = rest[:break_at].rstrip()
        if line.endswith(","):
            line = line[:-1].rstrip()
        lines.append(line)
        rest = rest[break_at:].lstrip(", ")
    return lines


def _fmt_bytes(n):
    if n is None:
        return ""
    return f"{n:,} B"


class _TreeNode:
    __slots__ = ("desc", "bytes", "suffix", "line", "children")

    def __init__(self, desc, children=None, nbytes=None, suffix="", line=""):
        self.desc = desc
        self.bytes = nbytes
        # suffix: inline annotation (e.g. "(specified)", "[external]") rendered
        # right after the type so it wraps together with the description.
        self.suffix = suffix
        # line: source location string (e.g. ":11") rendered in its own column.
        self.line = line
        self.children = list(children or [])


def _type_size(resolver, resolved_type):
    if resolved_type is None:
        return None
    try:
        return resolved_type.get_size(resolver)
    except Exception:
        return None


def _expr_snippet(expr):
    from compiler.parser import Identifier, Literal, StringLiteral

    if expr is None:
        return "?"
    if isinstance(expr, StringLiteral):
        s = (
            expr.value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        )
        return f'"{s}"'
    if isinstance(expr, Literal):
        return repr(expr.value) if expr.val_type == "Char" else str(expr.value)
    if isinstance(expr, Identifier):
        return expr.name
    if isinstance(expr, BinaryExpr):
        return f"{_expr_snippet(expr.left)} {expr.op} {_expr_snippet(expr.right)}"
    if isinstance(expr, UnaryExpr):
        return f"{expr.op}{_expr_snippet(expr.operand)}"
    if isinstance(expr, FieldExpr):
        return f"{_expr_snippet(expr.expr)}.{expr.field}"
    if isinstance(expr, IndexExpr):
        return f"{_expr_snippet(expr.expr)}[{_expr_snippet(expr.index)}]"
    if isinstance(expr, CallExpr) and isinstance(expr.func, Identifier):
        return f"{expr.func.name}({', '.join(_expr_snippet(a) for a in expr.args)})"
    return format_expr(expr)


def _truncate_snippet(text, limit=48):
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _unnamed_expr_node(expr, nbytes, stmt):
    snippet = _truncate_snippet(_expr_snippet(expr))
    return _TreeNode(
        f"unnamed expression: {snippet}",
        nbytes=nbytes,
        line=_line_str(stmt),
    )


def _expr_temp_bytes(expr, resolver):
    """Static data-section bytes the emitter reserves for temporaries produced
    while evaluating an expression.

    String concatenation allocates a result-string header per '+', and a
    number-to-string scratch buffer whenever an Integer/Rational is spliced into
    a string. These are separate from any named variable the result is bound to.
    (The concatenated character data itself is allocated on the runtime heap and
    so is not part of the static layout reported here.)
    """
    total = 0
    if isinstance(expr, BinaryExpr):
        is_fn_binop = id(expr) in getattr(resolver, "resolved_binops", {})
        if expr.op == "+" and not is_fn_binop:
            left_t = right_t = None
            try:
                left_t = resolver.infer_expr_type(expr.left)
                right_t = resolver.infer_expr_type(expr.right)
            except Exception:
                left_t = right_t = None
            left_max = resolver._struct_string_max_len(left_t) if left_t else None
            if left_max is not None:
                total += _type_size(resolver, left_t) or 0
                right_max = resolver._struct_string_max_len(right_t) if right_t else None
                if right_max is not None:
                    pass
                elif right_t is not None and resolver._is_rational_type(right_t):
                    total += _RATIONAL_TO_STRING_TEMP
                elif right_t is not None and getattr(right_t, "name", None) == "Integer":
                    total += _INT_TO_STRING_TEMP
        total += _expr_temp_bytes(expr.left, resolver)
        total += _expr_temp_bytes(expr.right, resolver)
    elif isinstance(expr, CallExpr):
        for arg in expr.args:
            total += _expr_temp_bytes(arg, resolver)
    elif isinstance(expr, UnaryExpr):
        total += _expr_temp_bytes(expr.operand, resolver)
    elif isinstance(expr, IndexExpr):
        total += _expr_temp_bytes(expr.expr, resolver)
        total += _expr_temp_bytes(expr.index, resolver)
    elif isinstance(expr, FieldExpr):
        total += _expr_temp_bytes(expr.expr, resolver)
    return total


def _func_locals_key(resolver, func_decl):
    index = getattr(func_decl, "overload_index", 0)
    preferred = getattr(func_decl, "mangled_name", None) or f"{func_decl.name}#{index}"
    if preferred in resolver.func_locals:
        return preferred
    if func_decl.name in resolver.func_locals:
        return func_decl.name
    for key in resolver.func_locals:
        if key.startswith(f"{func_decl.name}#"):
            return key
    return preferred


def _frame_bytes(resolver, func_key):
    locals_map = resolver.func_locals.get(func_key, {})
    total = 0
    for _, (rtype, _) in locals_map.items():
        size = _type_size(resolver, rtype)
        if size is not None:
            total += size
    return total


def _sum_leaf_bytes(nodes):
    total = 0
    for node in nodes:
        if node.children:
            total += _sum_leaf_bytes(node.children)
        elif node.bytes is not None:
            total += node.bytes
    return total


def _desc_col_width(term_width):
    # Reserve fixed right-hand columns for bytes and line number:
    #   <desc>  <bytes col>  <line col>
    reserved = 2 + BYTES_FIELD_WIDTH + 2 + LINE_FIELD_WIDTH
    return max(MIN_DESC_WIDTH, term_width - reserved)


def _right_columns(nbytes, line):
    bytes_col = f"{_fmt_bytes(nbytes):>{BYTES_FIELD_WIDTH}}"
    line_col = f"{line:>{LINE_FIELD_WIDTH}}"
    return f"  {bytes_col}  {line_col}"


def _row_layout(prefix, connector, desc, suffix, nbytes, line, term_width):
    base = f"{prefix}{connector}"
    # The specified/inferred (or kind) annotation sits right after the type so it
    # wraps together with a long function name / typedef.
    left_text = f"{desc}  {suffix}" if suffix else desc
    desc_col = max(len(base) + MIN_DESC_WIDTH, _desc_col_width(term_width))
    max_desc = max(MIN_DESC_WIDTH, desc_col - len(base))
    desc_lines = _wrap_desc(left_text, max_desc)
    cont_prefix = prefix + "   "

    lines = []
    last = len(desc_lines) - 1
    for i, desc_line in enumerate(desc_lines):
        if i == 0:
            left = f"{base}{desc_line}"
        else:
            left = f"{cont_prefix}{desc_line}"
        if i == last:
            line_out = f"{left.ljust(desc_col)}{_right_columns(nbytes, line)}"
        else:
            line_out = left
        lines.append(line_out.rstrip())
    return lines


def _render_tree(node, prefix="", is_last=True, term_width=DEFAULT_TERMINAL_WIDTH):
    connector = "|_ "
    lines = _row_layout(
        prefix,
        connector,
        node.desc,
        node.suffix,
        node.bytes,
        node.line,
        term_width,
    )
    child_prefix = prefix + ("   " if is_last else "|  ")
    for i, child in enumerate(node.children):
        lines.extend(_render_tree(child, child_prefix, i == len(node.children) - 1, term_width))
    return lines


def _render_nodes(nodes, term_width=DEFAULT_TERMINAL_WIDTH):
    if not nodes:
        return []
    lines = []
    for i, node in enumerate(nodes):
        lines.extend(_render_tree(node, "", i == len(nodes) - 1, term_width))
    return lines


def _format_total_line(label, nbytes, term_width):
    desc_col = _desc_col_width(term_width)
    desc_lines = _wrap_desc(label, desc_col)
    lines = []
    last = len(desc_lines) - 1
    for i, desc_line in enumerate(desc_lines):
        if i == last:
            lines.append(f"{desc_line.ljust(desc_col)}{_right_columns(nbytes, '')}".rstrip())
        else:
            lines.append(desc_line)
    return lines


def _line_str(node):
    line = getattr(node, "line", None)
    return f":{line}" if line else ""


def _binding_label(func_name, var_name, resolver):
    inferred = resolver.inferred_bindings.get(func_name, set())
    return "inferred" if var_name in inferred else "specified"


def _format_return_type(func, resolver):
    generic_map = getattr(func, "monomorph_generics", None) or {}
    if func.generics and not generic_map:
        if func.ret_type:
            return format_type_expr(func.ret_type), "specified", None
        return "void", "specified", 0
    try:
        if func.name not in resolver.inferred_return_types:
            if func.ret_type:
                resolved = resolver.resolve_type_expr(func.ret_type, generic_map)
                return format_type_expr(func.ret_type), "specified", _type_size(resolver, resolved)
            return "void", "specified", 0
        resolved = resolver.resolve_type_expr(func.ret_type, generic_map)
        return format_type(resolved), "inferred", _type_size(resolver, resolved)
    except Exception:
        if func.ret_type:
            return format_type_expr(func.ret_type), "specified", None
        return "void", "specified", 0


def _format_func_signature(func, resolver):
    parts = []
    if func.generics:
        gparams = ", ".join(
            f"{gname}: {format_type_expr(gtype)}" for gname, gtype in func.generics
        )
        parts.append(f"[{gparams}]")
    params = []
    locals_key = _func_locals_key(resolver, func)
    locals_map = resolver.func_locals.get(locals_key, resolver.func_locals.get(func.name, {}))
    for param in func.params:
        if param.name in locals_map:
            ptype = format_type(locals_map[param.name][0])
        elif param.type_expr:
            ptype = format_type_expr(param.type_expr)
        else:
            ptype = param.name
        params.append(f"{param.name}: {ptype}")
    ret_t, _ = _format_return_type(func, resolver)[:2]
    header = f"function {func.name}"
    if parts:
        header += "".join(parts)
    header += f"({', '.join(params)}) -> {ret_t}"
    return header


def _local_type(resolver, func_key, var_name, type_expr):
    for key in (func_key, func_key.split("#")[0] if "#" in func_key else None):
        if not key:
            continue
        locals_map = resolver.func_locals.get(key, {})
        if var_name in locals_map:
            return locals_map[var_name][0]
    if type_expr:
        try:
            return resolver.resolve_type_expr(type_expr)
        except Exception:
            return None
    return None


def _binding_node(kind, name, type_expr, func_key, label_func_name, resolver, line=None, children=None):
    label = _binding_label(label_func_name, name, resolver)
    resolved = _local_type(resolver, func_key, name, type_expr)
    if resolved is not None:
        rtype = format_type(resolved)
        nbytes = _type_size(resolver, resolved)
    elif type_expr:
        rtype = format_type_expr(type_expr)
        nbytes = None
    else:
        rtype = "?"
        nbytes = None
    return _TreeNode(
        f"{kind} {name}: {rtype}",
        children,
        nbytes=nbytes,
        suffix=f"({label})",
        line=f":{line}" if line else "",
    )


def _collect_binding_nodes(stmts, func_key, label_func_name, resolver):
    """Collect the variable bindings a function body introduces.

    The inspect report cares about the types of variables and how much space
    they occupy, not control flow per se. But every construct that opens a new
    variable name space (a for loop, the branches of an if, and each match arm)
    is shown as an indented group so the scope of each binding is visible.
    Scopes that introduce no bindings are omitted to avoid clutter.
    """
    nodes = []
    for stmt in stmts:
        if isinstance(stmt, VarDecl):
            kind = "const" if stmt.is_const else "let"
            nodes.append(
                _binding_node(
                    kind,
                    stmt.name,
                    stmt.type_expr,
                    func_key,
                    label_func_name,
                    resolver,
                    getattr(stmt, "line", None),
                )
            )
            temp = _expr_temp_bytes(stmt.value, resolver)
            if temp:
                nodes.append(_unnamed_expr_node(stmt.value, temp, stmt))
        elif isinstance(stmt, ExprStmt):
            temp = _expr_temp_bytes(stmt.expr, resolver)
            if temp:
                nodes.append(_unnamed_expr_node(stmt.expr, temp, stmt))
        elif isinstance(stmt, ForStmt):
            body_nodes = _collect_binding_nodes(stmt.body, func_key, label_func_name, resolver)
            nodes.append(
                _binding_node(
                    "for",
                    stmt.var_name,
                    None,
                    func_key,
                    label_func_name,
                    resolver,
                    getattr(stmt, "line", None),
                    children=body_nodes,
                )
            )
        elif isinstance(stmt, IfStmt):
            then_nodes = _collect_binding_nodes(stmt.then_branch, func_key, label_func_name, resolver)
            if then_nodes:
                nodes.append(
                    _TreeNode("if", then_nodes, line=_line_str(stmt))
                )
            if stmt.else_branch:
                else_nodes = _collect_binding_nodes(stmt.else_branch, func_key, label_func_name, resolver)
                if else_nodes:
                    nodes.append(
                        _TreeNode("else", else_nodes, line=_line_str(stmt))
                    )
        elif isinstance(stmt, MatchStmt):
            for case in stmt.cases:
                arm_nodes = []
                for arg in case.pattern.args:
                    arm_nodes.append(
                        _binding_node(
                            "match",
                            arg,
                            case.pattern.type_bind,
                            func_key,
                            label_func_name,
                            resolver,
                            getattr(case, "line", None),
                        )
                    )
                arm_nodes.extend(_collect_binding_nodes(case.body, func_key, label_func_name, resolver))
                if arm_nodes:
                    nodes.append(
                        _TreeNode(case.pattern.name, arm_nodes, line=_line_str(case))
                    )
    return nodes


def _function_node(func, resolver):
    ret_t, ret_src, ret_size = _format_return_type(func, resolver)
    children = [_TreeNode(f"return: {ret_t}", nbytes=ret_size, suffix=f"({ret_src})")]

    locals_key = _func_locals_key(resolver, func)
    locals_map = resolver.func_locals.get(locals_key, resolver.func_locals.get(func.name, {}))
    for param in func.params:
        if param.name in locals_map:
            ptype = format_type(locals_map[param.name][0])
            psize = _type_size(resolver, locals_map[param.name][0])
        elif param.type_expr:
            try:
                generic_map = getattr(func, "monomorph_generics", None) or {}
                resolved = resolver.resolve_type_expr(param.type_expr, generic_map)
                ptype = format_type_expr(param.type_expr)
                psize = _type_size(resolver, resolved)
            except Exception:
                ptype = format_type_expr(param.type_expr)
                psize = None
        else:
            ptype = "?"
            psize = None
        label = _binding_label(func.name, param.name, resolver)
        children.append(_TreeNode(f"param {param.name}: {ptype}", nbytes=psize, suffix=f"({label})"))

    prev_locals = getattr(resolver, "locals", {})
    prev_gm = getattr(resolver, "current_generic_map", {})
    resolver.locals = resolver.func_locals.get(locals_key, {})
    resolver.current_generic_map = getattr(func, "monomorph_generics", None) or {}
    try:
        children.extend(_collect_binding_nodes(func.body or [], locals_key, func.name, resolver))
    finally:
        resolver.locals = prev_locals
        resolver.current_generic_map = prev_gm

    sig = _format_func_signature(func, resolver)
    suffix = f"[{func.kind}]" if func.kind != "normal" else ""
    line = f":{func.line}" if getattr(func, "line", None) else ""
    frame = _frame_bytes(resolver, locals_key)
    return _TreeNode(sig, children, nbytes=frame, suffix=suffix, line=line)


def _module_functions(ast):
    return [d for d in ast.decls if isinstance(d, FuncDecl)]


def build_inspect_modules(loader, main_path, stdlib_ast):
    main_abs = os.path.abspath(main_path)
    modules = []

    if main_abs in loader.modules:
        main_ast, _ = loader.modules[main_abs]
        modules.append((os.path.basename(main_abs), main_ast))

    for path in sorted(loader.modules):
        if path == main_abs:
            continue
        ast, _ = loader.modules[path]
        modules.append((os.path.basename(path), ast))

    modules.append(("stdlib", stdlib_ast))
    return modules


def _section_divider(term_width):
    return "─" * term_width


def format_inspect_report(resolver, loader, main_path, stdlib_ast, emitter=None):
    modules = build_inspect_modules(loader, main_path, stdlib_ast)
    title = os.path.basename(main_path)
    term_width = _terminal_width()
    lines = [f"Lattice inspect — {title}", ""]

    all_function_nodes = []
    binding_total = 0

    rendered_sections = []
    for module_name, ast in modules:
        funcs = _module_functions(ast)
        if not funcs:
            continue
        module_nodes = [_function_node(func, resolver) for func in funcs]
        all_function_nodes.extend(module_nodes)
        binding_total += _sum_leaf_bytes(module_nodes)
        rendered_sections.append((module_name, module_nodes))

    for i, (module_name, module_nodes) in enumerate(rendered_sections):
        if i > 0:
            lines.append(_section_divider(term_width))
        lines.append(module_name)
        lines.extend(_render_nodes(module_nodes, term_width))
        module_bytes = _sum_leaf_bytes(module_nodes)
        lines.extend(
            _format_total_line(f"{module_name} total", module_bytes, term_width)
        )

    if rendered_sections:
        lines.append(_section_divider(term_width))

    lines.extend(
        _format_total_line("total program bytes", binding_total, term_width)
    )

    if emitter is not None:
        wasm_total = (
            emitter.data_offset
            + getattr(resolver, "max_local_offset", 0)
            + WASM_MEMORY_SLACK
        )
        pages = max(1, (wasm_total + WASM_PAGE_SIZE - 1) // WASM_PAGE_SIZE)
        mem_label = f"total wasm memory  ({pages} × 64 KiB page{'s' if pages != 1 else ''})"
        lines.extend(_format_total_line(mem_label, wasm_total, term_width))

    return "\n".join(lines)


def print_inspect_report(resolver, loader, main_path, stdlib_ast, emitter=None, out=None):
    out = out or sys.stdout
    print(
        format_inspect_report(resolver, loader, main_path, stdlib_ast, emitter=emitter),
        file=out,
    )

