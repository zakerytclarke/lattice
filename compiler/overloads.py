# Compile-time function overload registry and resolution

from compiler.errors import format_type_expr, format_type, LatticeTypeError, type_mismatch_hint
from compiler.parser import FuncDecl, Param, TypeExpr, Literal

# Operator symbols desugar to named functions when a matching overload exists.
# Integer/Char/Bool keep selected WASM builtins (see resolver) so stdlib bodies
# can use operators without infinite recursion.
BINOP_TO_FUNCTION = {
    "+": "add",
    "-": "sub",
    "*": "mul",
    "/": "div",
    "%": "mod",
    "==": "equal",
    "!=": "not_equal",
    "&&": "and",
    "||": "or",
    "^": "xor",
}

UNOP_TO_FUNCTION = {
    "!": "not",
}

INTEGER_BINOPS = {"+", "-", "*", "/", "%", "==", "!=", "<", ">", "<=", ">="}
CHAR_BINOPS = {"==", "!="}
BOOL_BINOPS = {"&&", "||"}
PRIMITIVE_TYPE_NAMES = {"Integer", "Bool", "Char"}


def get_overloads(functions, name):
    decls = functions.get(name)
    if decls is None:
        return []
    if isinstance(decls, list):
        return decls
    return [decls]


def iter_all_functions(functions):
    for name, decls in functions.items():
        for decl in get_overloads(functions, name):
            yield name, decl


def function_signature_key(decl):
    generics = tuple(gname for gname, _ in (decl.generics or []))
    params = tuple(
        format_type_expr(p.type_expr) if p.type_expr else "?"
        for p in decl.params
    )
    ret = format_type_expr(decl.ret_type) if decl.ret_type else "void"
    return (len(decl.params), generics, params, ret)


def ensure_mangled_name(decl):
    if not getattr(decl, "mangled_name", None):
        index = getattr(decl, "overload_index", 0)
        decl.mangled_name = f"{decl.name}#{index}"
    return decl.mangled_name


def register_function(functions, decl):
    overloads = functions.setdefault(decl.name, [])
    if not isinstance(overloads, list):
        overloads = [overloads]
        functions[decl.name] = overloads
    sig = function_signature_key(decl)
    for existing in overloads:
        if function_signature_key(existing) == sig:
            raise LatticeTypeError(
                f"Duplicate overload of function '{decl.name}' with signature ({', '.join(function_signature_key(decl)[2])})",
                decl.line,
                hint="Overload functions must differ in parameter or return types.",
            )
    decl.overload_index = len(overloads)
    decl.mangled_name = f"{decl.name}#{decl.overload_index}"
    overloads.append(decl)
    return decl


def merge_function_maps(target, source):
    for name, decls in source.items():
        for decl in get_overloads(source, name):
            try:
                register_function(target, decl)
            except LatticeTypeError as e:
                existing = get_overloads(target, name)
                if any(function_signature_key(d) == function_signature_key(decl) for d in existing):
                    continue
                raise


def _build_call_generic_map(resolver, callee, expr, generic_map):
    call_generic_map = {}
    if callee.generics:
        for gname, _ in callee.generics:
            call_generic_map[gname] = None
        if expr.generic_args:
            for i, (gname, _) in enumerate(callee.generics):
                if i < len(expr.generic_args):
                    garg = expr.generic_args[i]
                    if isinstance(garg, Literal) and garg.val_type == "Integer":
                        call_generic_map[gname] = garg.value
                    else:
                        call_generic_map[gname] = garg
        else:
            for idx, arg in enumerate(expr.args):
                if idx < len(callee.params):
                    arg_t = resolver.infer_expr_type(arg, generic_map)
                    resolver.infer_call_generics(
                        callee.params[idx].type_expr, arg_t, call_generic_map
                    )
        resolver.finalize_generic_map(
            call_generic_map, callee.generics, expr.line, callee_name=callee.name
        )
    return call_generic_map


def _overload_matches(resolver, callee, expr, generic_map):
    if len(expr.args) != len(callee.params):
        return None
    try:
        call_generic_map = _build_call_generic_map(resolver, callee, expr, generic_map)
    except LatticeTypeError:
        return None
    for idx, arg in enumerate(expr.args):
        arg_t = resolver.infer_expr_type(arg, generic_map)
        param = callee.params[idx]
        param_t = resolver.resolve_type_expr(
            param.type_expr, call_generic_map or generic_map
        )
        if (
            callee.kind == "external"
            and not callee.body
            and type(param_t).__name__ == "PrimitiveResolvedType"
            and param_t.name == "Integer"
            and type(arg_t).__name__ in ("StructResolvedType", "UnionResolvedType", "ListResolvedType")
        ):
            continue
        try:
            resolver.types_compatible(param_t, arg_t, expr.line)
        except LatticeTypeError:
            return None
    return call_generic_map


def resolve_call(resolver, name, expr, generic_map):
    from compiler.parser import CallExpr, Identifier

    if not isinstance(expr, CallExpr):
        expr = CallExpr(Identifier(name, expr.line), list(expr.args) if hasattr(expr, "args") else [], [], expr.line)

    candidates = get_overloads(resolver.functions, name)
    if not candidates:
        return None

    matches = []
    for callee in candidates:
        call_generic_map = _overload_matches(resolver, callee, expr, generic_map)
        if call_generic_map is not None:
            matches.append((callee, call_generic_map))

    if not matches:
        arity_matches = [c for c in candidates if len(c.params) == len(expr.args)]
        if arity_matches:
            last_generic_error = None
            for callee in arity_matches:
                try:
                    _build_call_generic_map(resolver, callee, expr, generic_map)
                except LatticeTypeError as e:
                    if "Cannot infer" in e.message:
                        last_generic_error = e
            if last_generic_error is not None:
                raise last_generic_error
        if not arity_matches:
            sigs = "; ".join(_format_overload_sig(c) for c in candidates)
            expected = ", ".join(
                str(len(c.params)) for c in candidates
            )
            raise LatticeTypeError(
                f"Wrong number of arguments to '{name}': got {len(expr.args)}",
                expr.line,
                hint=f"Available overloads of '{name}': {sigs}",
            )
        arg_types = []
        for arg in expr.args:
            try:
                arg_types.append(format_type(resolver.infer_expr_type(arg, generic_map)))
            except LatticeTypeError:
                arg_types.append("?")
        raise LatticeTypeError(
            f"No matching overload for '{name}'({', '.join(arg_types)})",
            expr.line,
            hint=f"Available overloads of '{name}': "
            + "; ".join(
                _format_overload_sig(c)
                for c in candidates
            ),
        )

    if len(matches) > 1:
        best = _pick_best_match(matches)
        if best is None:
            sigs = "; ".join(_format_overload_sig(c) for c, _ in matches)
            raise LatticeTypeError(
                f"Ambiguous call to '{name}' — multiple overloads match",
                expr.line,
                hint=f"Matching overloads: {sigs}",
            )
        callee, call_generic_map = best
    else:
        callee, call_generic_map = matches[0]

    resolver.resolved_calls[id(expr)] = {
        "decl": callee,
        "generic_map": call_generic_map,
    }
    return callee, call_generic_map


def _format_overload_sig(decl):
    params = ", ".join(
        format_type_expr(p.type_expr) if p.type_expr else "?"
        for p in decl.params
    )
    ret = format_type_expr(decl.ret_type) if decl.ret_type else "void"
    return f"{decl.name}({params}) -> {ret}"


def _specificity_score(resolver, callee, call_generic_map, expr, generic_map):
    score = 0
    for idx, arg in enumerate(expr.args):
        arg_t = resolver.infer_expr_type(arg, generic_map)
        param_t = resolver.resolve_type_expr(
            callee.params[idx].type_expr, call_generic_map or generic_map
        )
        if param_t.name == arg_t.name:
            score += 2
        elif type(param_t).__name__ == type(arg_t).__name__:
            score += 1
    return score


def _pick_best_match(matches):
    if len(matches) <= 1:
        return matches[0] if matches else None
    return matches[0]
