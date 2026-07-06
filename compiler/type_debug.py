# Type map reporting for --optimal compiler flag

import sys

from compiler.errors import format_type, format_type_expr
from compiler.parser import (
    Assign,
    BinaryExpr,
    CallExpr,
    ExprStmt,
    FieldExpr,
    ForStmt,
    FuncDecl,
    Identifier,
    IfStmt,
    IndexExpr,
    ListLiteral,
    MatchCase,
    MatchStmt,
    ReturnStmt,
    StringLiteral,
    VarDecl,
)


class _TreeNode:
    __slots__ = ("text", "children")

    def __init__(self, text, children=None):
        self.text = text
        self.children = list(children or [])


def _binding_label(func_name, var_name, resolver):
    inferred = resolver.inferred_bindings.get(func_name, set())
    return "inferred" if var_name in inferred else "specified"


def _format_return_type(func, resolver):
    if func.name not in resolver.inferred_return_types:
        if func.ret_type:
            return format_type_expr(func.ret_type), "specified"
        return "void", "specified"
    resolved = resolver.resolve_type_expr(func.ret_type)
    return format_type(resolved), "inferred"


def _format_func_signature(func, resolver):
    parts = []
    if func.generics:
        gparams = ", ".join(
            f"{gname}: {format_type_expr(gtype)}" for gname, gtype in func.generics
        )
        parts.append(f"[{gparams}]")
    params = []
    locals_map = resolver.func_locals.get(func.name, {})
    for param in func.params:
        if param.type_expr:
            ptype = format_type_expr(param.type_expr)
        elif param.name in locals_map:
            ptype = format_type(locals_map[param.name][0])
        else:
            ptype = param.name
        params.append(f"{param.name}: {ptype}")
    ret_t, _ = _format_return_type(func, resolver)
    header = f"function {func.name}"
    if parts:
        header += "".join(parts)
    header += f"({', '.join(params)}) -> {ret_t}"
    if func.kind != "normal":
        header += f"  [{func.kind}]"
    if getattr(func, "line", None):
        header += f"  :{func.line}"
    return header


def _safe_type(resolver, expr):
    try:
        return format_type(resolver.infer_expr_type(expr))
    except Exception:
        return None


def _call_return_type(resolver, call):
    if isinstance(call.func, Identifier) and call.func.name == "input":
        site_id = resolver.input_call_exprs.get(id(call))
        if site_id is not None:
            site = resolver.input_call_sites[site_id]
            resolved = site["resolved_type"]
            from compiler.resolver import IOResolvedType
            if isinstance(resolved, IOResolvedType):
                return format_type(resolved.inner)
            return format_type(resolved)
    ret = _safe_type(resolver, call)
    if ret:
        from compiler.resolver import IOResolvedType
        try:
            rt = resolver.infer_expr_type(call)
            if isinstance(rt, IOResolvedType):
                return f"IO[{format_type(rt.inner)}]"
        except Exception:
            pass
        return ret
    return "?"


def _format_call_site(resolver, call):
    if not isinstance(call.func, Identifier):
        return "<?>"
    name = call.func.name
    gargs = ""
    if call.generic_args:
        gargs = "[" + ", ".join(str(g) for g in call.generic_args) + "]"
    arg_types = [_safe_type(resolver, arg) or "?" for arg in call.args]
    ret = _call_return_type(resolver, call)
    line = getattr(call, "line", None)
    loc = f"  :{line}" if line else ""
    decl = get_overloads(resolver.functions, name)
    decl = decl[0] if decl else None
    tag = ""
    if decl and decl.kind == "external" and not decl.body:
        tag = "  [host]"
    elif decl:
        tag = "  [import]"
    return f"{name}{gargs}({', '.join(arg_types)}) -> {ret}{tag}{loc}"


def _collect_calls(expr):
    calls = []

    def visit(node):
        if node is None:
            return
        if isinstance(node, CallExpr):
            calls.append(node)
            for arg in node.args:
                visit(arg)
        elif isinstance(node, BinaryExpr):
            visit(node.left)
            visit(node.right)
        elif isinstance(node, IndexExpr):
            visit(node.expr)
            visit(node.index)
        elif isinstance(node, FieldExpr):
            visit(node.expr)
        elif isinstance(node, ListLiteral):
            for el in node.elements:
                visit(el)
        elif isinstance(node, StringLiteral):
            pass

    visit(expr)
    return calls


def _is_use_site_callee(name, user_func_names, resolver):
    if name == "input":
        return True
    if name not in resolver.functions:
        return False
    return name not in user_func_names


def _call_nodes(resolver, expr, user_func_names):
    nodes = []
    for call in _collect_calls(expr):
        if not isinstance(call.func, Identifier):
            continue
        if _is_use_site_callee(call.func.name, user_func_names, resolver):
            nodes.append(_TreeNode(_format_call_site(resolver, call)))
    return nodes


def _local_type(func_name, var_name, type_expr, resolver):
    locals_map = resolver.func_locals.get(func_name, {})
    if var_name in locals_map:
        return format_type(locals_map[var_name][0])
    if type_expr:
        return format_type_expr(type_expr)
    return "?"


def _binding_node(kind, name, type_expr, func_name, resolver, line=None, children=None):
    label = _binding_label(func_name, name, resolver)
    rtype = _local_type(func_name, name, type_expr, resolver)
    loc = f"  :{line}" if line else ""
    text = f"{kind} {name}: {rtype} ({label}){loc}"
    return _TreeNode(text, children)


def _render_stmts(stmts, func_name, resolver, user_func_names):
    nodes = []
    for stmt in stmts:
        if isinstance(stmt, VarDecl):
            kind = "const" if stmt.is_const else "let"
            children = _call_nodes(resolver, stmt.value, user_func_names)
            nodes.append(
                _binding_node(
                    kind,
                    stmt.name,
                    stmt.type_expr,
                    func_name,
                    resolver,
                    getattr(stmt, "line", None),
                    children,
                )
            )
        elif isinstance(stmt, ForStmt):
            body = _render_stmts(stmt.body, func_name, resolver, user_func_names)
            children = [
                _binding_node(
                    "for",
                    stmt.var_name,
                    None,
                    func_name,
                    resolver,
                    getattr(stmt, "line", None),
                    body,
                )
            ]
            nodes.extend(children)
        elif isinstance(stmt, IfStmt):
            then_nodes = _render_stmts(stmt.then_branch, func_name, resolver, user_func_names)
            if then_nodes:
                nodes.append(_TreeNode(f"then  :{stmt.line}", then_nodes))
            if stmt.else_branch:
                else_nodes = _render_stmts(stmt.else_branch, func_name, resolver, user_func_names)
                if else_nodes:
                    nodes.append(_TreeNode("else", else_nodes))
        elif isinstance(stmt, MatchStmt):
            for case in stmt.cases:
                case_children = []
                if case.pattern.args:
                    for arg in case.pattern.args:
                        case_children.append(
                            _binding_node(
                                "match",
                                arg,
                                case.pattern.type_bind,
                                func_name,
                                resolver,
                                getattr(case, "line", None),
                            )
                        )
                case_children.extend(
                    _render_stmts(case.body, func_name, resolver, user_func_names)
                )
                if case_children:
                    pat = case.pattern.name
                    nodes.append(_TreeNode(f"match {pat}", case_children))
        elif isinstance(stmt, Assign):
            children = _call_nodes(resolver, stmt.value, user_func_names)
            if children:
                target = stmt.target.name if isinstance(stmt.target, Identifier) else "..."
                nodes.append(_TreeNode(f"assign {target}  :{stmt.line}", children))
        elif isinstance(stmt, ReturnStmt):
            children = _call_nodes(resolver, stmt.expr, user_func_names)
            if children:
                nodes.append(_TreeNode(f"return  :{getattr(stmt, 'line', '')}", children))
        elif isinstance(stmt, ExprStmt):
            children = _call_nodes(resolver, stmt.expr, user_func_names)
            if children:
                if len(children) == 1:
                    nodes.append(children[0])
                else:
                    nodes.append(_TreeNode(f"expr  :{stmt.line}", children))
    return nodes


def _function_node(func, resolver, user_func_names):
    ret_t, ret_src = _format_return_type(func, resolver)
    children = [_TreeNode(f"return: {ret_t} ({ret_src})")]

    locals_map = resolver.func_locals.get(func.name, {})
    for param in func.params:
        if param.type_expr:
            ptype = format_type_expr(param.type_expr)
        elif param.name in locals_map:
            ptype = format_type(locals_map[param.name][0])
        else:
            ptype = "?"
        label = _binding_label(func.name, param.name, resolver)
        children.append(_TreeNode(f"param {param.name}: {ptype} ({label})"))

    children.extend(_render_stmts(func.body or [], func.name, resolver, user_func_names))
    return _TreeNode(_format_func_signature(func, resolver), children)


def _render_tree(node, prefix="", is_last=True):
    connector = "|_ " if prefix or not is_last else "|_ "
    lines = [f"{prefix}{connector}{node.text}"]
    child_prefix = prefix + ("   " if is_last else "|  ")
    for i, child in enumerate(node.children):
        lines.extend(_render_tree(child, child_prefix, i == len(node.children) - 1))
    return lines


def format_type_report(resolver, program_decls, source_name=None):
    user_funcs = [d for d in program_decls if isinstance(d, FuncDecl)]
    user_func_names = {f.name for f in user_funcs}

    title = source_name or "program"
    root = _TreeNode(title, [_function_node(f, resolver, user_func_names) for f in user_funcs])

    lines = [f"Lattice types — {title}", ""]
    for i, child in enumerate(root.children):
        lines.extend(_render_tree(child, "", i == len(root.children) - 1))
    return "\n".join(lines)


def print_type_report(resolver, program_decls, source_name=None, out=None):
    out = out or sys.stdout
    print(format_type_report(resolver, program_decls, source_name), file=out)
