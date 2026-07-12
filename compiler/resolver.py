# Lattice Type Inference & Memory Resolution

import sys
from compiler.parser import *
from compiler.input_types import (
    validate_input_target_type,
    validate_read_file_target_type,
    validate_http_get_target_type,
    _input_hint,
    _read_file_hint,
    _http_get_hint,
    read_file_string_max_len,
    read_file_json_cap,
    _is_list_resolved_type,
    unwrap_input_inner,
    _struct_string_max_len,
)
from compiler.string_literal import lower_string_literal
from compiler.overloads import (
    BINOP_TO_FUNCTION,
    UNOP_TO_FUNCTION,
    INTEGER_BINOPS,
    CHAR_BINOPS,
    BOOL_BINOPS,
    get_overloads,
    iter_all_functions,
    register_function,
    resolve_call,
    ensure_mangled_name,
)
from compiler.errors import (
    LatticeTypeError,
    error_site_from_expr,
    format_expr,
    format_type,
    format_type_expr,
    generic_inference_hint,
    static_memory_hint,
    type_mismatch_hint,
    type_mismatch_message,
)

def clone_ast_node(node):
    if node is None:
        return None
    if isinstance(node, list):
        return [clone_ast_node(x) for x in node]
    if isinstance(node, dict):
        return {k: clone_ast_node(v) for k, v in node.items()}
    if not isinstance(node, ASTNode):
        return node
    import copy
    new_node = copy.copy(node)
    for k, v in list(new_node.__dict__.items()):
        new_node.__dict__[k] = clone_ast_node(v)
    return new_node

# ============================================================================
# Type Representation Nodes
# ============================================================================

class ResolvedType:
    def get_size(self, resolver):
        raise NotImplementedError()

class PrimitiveResolvedType(ResolvedType):
    def __init__(self, name):
        self.name = name # 'Integer', 'Bool', 'Char', 'Type', 'void'
    def __repr__(self):
        return self.name
    def get_size(self, resolver):
        if self.name in ['Integer', 'Char']:
            return 4
        if self.name == 'Bool':
            return 1
        return 0

class RefinedResolvedType(ResolvedType):
    def __init__(self, base_type, constraint, constraint_var):
        self.name = base_type.name
        self.base_type = base_type
        self.constraint = constraint
        self.constraint_var = constraint_var
    def __repr__(self):
        return f"{self.base_type.name}({self.constraint_var}){{{self.constraint}}}"
    def get_size(self, resolver):
        return self.base_type.get_size(resolver)

class StructResolvedType(ResolvedType):
    def __init__(self, name, fields, invariants):
        self.name = name
        self.fields = fields # list of (name, ResolvedType)
        self.invariants = invariants
    def __repr__(self):
        return self.name
    def get_size(self, resolver):
        return sum(f[1].get_size(resolver) for f in self.fields)

class UnionResolvedType(ResolvedType):
    def __init__(self, name, variants):
        self.name = name
        self.variants = variants # list of ResolvedType
    def __repr__(self):
        return f"Union[{', '.join(map(str, self.variants))}]"
    def get_size(self, resolver):
        # 1 byte for variant tag + max size of variants
        if not self.variants:
            return 1
        return 1 + max(v.get_size(resolver) for v in self.variants)

class ListResolvedType(ResolvedType):
    def __init__(self, length, elem_type):
        self.length = length # int (resolved length)
        self.elem_type = elem_type # ResolvedType
    def __repr__(self):
        return f"List({self.length})[{self.elem_type}]" if self.length is not None else f"List[{self.elem_type}]"
    def get_size(self, resolver):
        length = self.length if self.length is not None else 0
        return length * self.elem_type.get_size(resolver)

class StringResolvedType(ResolvedType):
    def __init__(self, max_len):
        self.max_len = max_len
    def __repr__(self):
        return f"String({self.max_len})"
    def get_size(self, resolver):
        # max_len chars + 4 bytes for current length field
        return (self.max_len * 4) + 4

class StaticMapResolvedType(ResolvedType):
    def __init__(self, value_type, entries):
        self.name = "StaticMap"
        self.value_type = value_type
        self.entries = entries  # list of (key: str, value_addr: int)
    def __repr__(self):
        return f"StaticMap[{self.value_type}]"
    def get_size(self, resolver):
        return 0

class MaterializedListResolvedType(ResolvedType):
    """A compile-time-materialized list of values produced by keys(map)/values(map).

    Represented at runtime as a handle: [count: i32][ptr_0: i32]...[ptr_{n-1}: i32]
    where each pointer refers to a heap-style value (e.g. a String struct). This
    is a lightweight intermediate type consumed by join(...); it is not a
    general first-class List.
    """
    def __init__(self, elem_type, length):
        self.name = "MaterializedList"
        self.elem_type = elem_type
        self.length = length
    def __repr__(self):
        return f"MaterializedList({self.length})[{self.elem_type}]"
    def get_size(self, resolver):
        return 4 + self.length * 4

class IOResolvedType(ResolvedType):
    def __init__(self, inner):
        self.inner = inner
    def __repr__(self):
        return f"IO[{self.inner}]"
    def get_size(self, resolver):
        return self.inner.get_size(resolver)

# ============================================================================
# Resolver Environment & Layout Manager
# ============================================================================

class Resolver:
    def __init__(self):
        self.types = {
            'Integer': PrimitiveResolvedType('Integer'),
            'Bool': PrimitiveResolvedType('Bool'),
            'Char': PrimitiveResolvedType('Char'),
            'Type': PrimitiveResolvedType('Type'),
            'void': PrimitiveResolvedType('void'),
        }
        self.functions = {}
        register_function(
            self.functions,
            FuncDecl(
                'external',
                'print_int',
                [],
                [Param('val', TypeExpr('Integer', [], 0), 0)],
                TypeExpr('IO', [TypeExpr('void', [], 0)], 0),
                [],
                [],
                0,
            ),
        )
        self.resolved_calls = {}
        self.resolved_binops = {}
        self.resolved_unops = {}
        self.type_decls = {}
        self.func_locals = {}
        self.globals = {} # name -> (ResolvedType, offset)
        self.global_offset = 1024 # start allocating static globals after offset 1024 (reserve bottom for stack / initial stuff)
        self.current_generic_map = {}
        self.current_func = None
        self._checking_call = None
        self.current_func_has_io = False
        self._input_expected_type = None
        self._read_file_expected_type = None
        self._http_get_expected_type = None
        self.input_call_sites = []
        self.input_call_exprs = {}
        self.read_file_call_sites = []
        self.read_file_call_exprs = {}
        self.http_get_call_sites = []
        self.http_get_call_exprs = {}
        self.static_map_bindings = {}
        self.memory_locals = {}
        self.main_string_args = []
        self.main_input_args = []
        self.diagnostics = None
        self.inferred_bindings = {}
        self.inferred_return_types = set()
        self.function_instantiations = {}
        self.pending_instantiation_resolve = []
        self._current_file_name = None
        self._current_source_lines = None
        
        # Local environments
        self.locals = {} # name -> (ResolvedType, offset)
        self.const_locals = set()
        self.immutable_locals = set()
        self.local_offset = 0
        self.max_local_offset = 0

    def _record_error(self, error):
        if self.diagnostics:
            self.diagnostics.add(
                error,
                file_name=self._current_file_name,
                source_lines=self._current_source_lines,
            )
        else:
            raise error

    def register_type(self, name, resolved_type):
        self.types[name] = resolved_type

    def _current_callee_name(self):
        if self._checking_call:
            return self._checking_call
        if self.current_func:
            return self.current_func.name
        return "<unknown>"

    def _build_type_generic_map(self, decl, te, generic_map):
        local_generic_map = {}
        type_arg_idx = 0
        size_for_int = te.size
        for gname, gtype in decl.generics:
            gtype_name = gtype.name if hasattr(gtype, 'name') else str(gtype)
            if gtype_name == 'Type':
                if type_arg_idx < len(te.args):
                    local_generic_map[gname] = self.resolve_type_expr(
                        te.args[type_arg_idx], generic_map
                    )
                    type_arg_idx += 1
                else:
                    local_generic_map[gname] = None
            elif gtype_name == 'Integer':
                if size_for_int is not None:
                    try:
                        local_generic_map[gname] = self.evaluate_constant(
                            size_for_int, generic_map
                        )
                    except LatticeTypeError:
                        if (
                            isinstance(size_for_int, TypeExpr)
                            and size_for_int.name in generic_map
                        ):
                            local_generic_map[gname] = generic_map[size_for_int.name]
                        else:
                            local_generic_map[gname] = None
                    size_for_int = None
                elif type_arg_idx < len(te.args):
                    arg_val = te.args[type_arg_idx]
                    type_arg_idx += 1
                    try:
                        local_generic_map[gname] = self.evaluate_constant(
                            arg_val, generic_map
                        )
                    except LatticeTypeError:
                        local_generic_map[gname] = None
                else:
                    local_generic_map[gname] = None
            else:
                local_generic_map[gname] = None
        return local_generic_map

    def _resolve_list_or_group(self, te, generic_map):
        if len(te.args) != 1:
            raise LatticeTypeError(
                "List/Group requires an element type: List[T] or List(N)[T]",
                te.line,
                hint="Use List[Integer] for inferred capacity, or List(10)[Integer] for a fixed length.",
            )
        elem_type = self.resolve_type_expr(te.args[0], generic_map)
        if elem_type is None:
            elem_type = self.types['Integer']
        length = None
        if te.size is not None:
            try:
                length = self.evaluate_constant(te.size, generic_map)
            except LatticeTypeError:
                if isinstance(te.size, TypeExpr) and te.size.name in generic_map:
                    val = generic_map[te.size.name]
                    length = None if val is None else val
                else:
                    raise
        return ListResolvedType(length, elem_type)

    def resolve_type_expr(self, te, generic_map=None):
        generic_map = generic_map or self.current_generic_map
        resolved = self._resolve_type_expr_impl(te, generic_map)
        if resolved is not None and hasattr(te, 'constraint') and te.constraint is not None:
            resolved = RefinedResolvedType(resolved, te.constraint, te.constraint_var)
        return resolved

    def _resolve_type_expr_impl(self, te, generic_map=None):
        if not te:
            return None
        generic_map = generic_map or {}
        
        name = te.name
        if name == 'String' and te.size is None and not te.args:
            raise LatticeTypeError(
                "Cannot infer the size of this String — every String needs a known capacity.",
                te.line,
                hint=(
                    "Give an explicit size, e.g. String(32) or Input[String(32)]. String "
                    "sizes are only inferred from the types of values (string literals, "
                    "concatenation, struct fields) — never from external/CLI input, whose "
                    "length is unknown at compile time."
                ),
            )
        # Substitute generic parameters if they are in the generic map
        if name in generic_map:
            val = generic_map[name]
            if val is None:
                return None
            if isinstance(val, ResolvedType):
                return val
            # If it's a number/value, it might parameterize a type
            name = str(val)

        if name == 'List' and (te.size is not None or len(te.args) <= 1):
            return self._resolve_list_or_group(te, generic_map)

        if name == 'Group' and (te.size is not None or len(te.args) <= 1):
            return self._resolve_list_or_group(te, generic_map)

        if name == 'IO':
            if len(te.args) != 1:
                raise LatticeTypeError("IO type requires 1 argument: IO[InnerType]", te.line)
            inner = self.resolve_type_expr(te.args[0], generic_map)
            return IOResolvedType(inner)

        if name == 'Union':
            variants = [self.resolve_type_expr(arg, generic_map) for arg in te.args]
            return UnionResolvedType('Union', variants)

        # Monomorphization Check
        if name in self.type_decls:
            decl = self.type_decls[name]
            if decl.generics:
                local_generic_map = self._build_type_generic_map(decl, te, generic_map)

                # Build monomorphized name
                arg_names = []
                for gname, _ in decl.generics:
                    val = local_generic_map.get(gname)
                    if val is None:
                        if (
                            gname in self.current_generic_map
                            and self.current_generic_map[gname] is None
                        ):
                            arg_names.append(gname)
                            continue
                        raise LatticeTypeError(
                            f"Cannot infer generic parameter '{gname}' for type '{name}'",
                            te.line,
                            hint=generic_inference_hint(name, decl.generics, local_generic_map),
                        )
                    arg_names.append(str(val))
                mono_name = f"{name}_{'_'.join(arg_names)}"
                
                if mono_name in self.types:
                    return self.types[mono_name]
                    
                # Create concrete resolved type
                if isinstance(decl, TypeDecl):
                    mono_fields = []
                    mono_type = StructResolvedType(mono_name, [], decl.invariants)
                    mono_type.generic_map = local_generic_map
                    self.register_type(mono_name, mono_type)
                    for fname, ftype_expr in decl.fields:
                        mono_fields.append((fname, self.resolve_type_expr(ftype_expr, local_generic_map)))
                    mono_type.fields = mono_fields
                    return mono_type
                else:
                    mono_variants = []
                    mono_type = UnionResolvedType(mono_name, [])
                    mono_type.generic_map = local_generic_map
                    self.register_type(mono_name, mono_type)
                    for vexpr in decl.variants:
                        mono_variants.append(self.resolve_type_expr(vexpr, local_generic_map))
                    mono_type.variants = mono_variants
                    return mono_type

        if name in self.types:
            return self.types[name]

        raise LatticeTypeError(
            f"Unknown type '{name}'",
            te.line,
            hint="Check spelling, imports, or provide explicit type parameters (e.g. List(5)[Integer], String(64)).",
        )

    def evaluate_constant(self, expr, generic_map=None):
        generic_map = generic_map or self.current_generic_map
        if isinstance(expr, int):
            return expr
        if isinstance(expr, Literal) and expr.val_type == 'Integer':
            return expr.value
        if isinstance(expr, Identifier):
            if expr.name in generic_map:
                val = generic_map[expr.name]
                if isinstance(val, int):
                    return val
            raise LatticeTypeError(
                f"Unbounded size parameter '{expr.name}' — compile-time constant required for static memory layout",
                expr.line,
                hint=static_memory_hint(),
            )
        if isinstance(expr, TypeExpr):
            if expr.name in generic_map:
                val = generic_map[expr.name]
                if isinstance(val, int):
                    return val
                if val is None and expr.name in self.current_generic_map:
                    return 0
        raise LatticeTypeError(
            f"Expected a compile-time integer constant, got {format_expr(expr)}",
            expr.line,
            hint=static_memory_hint(),
        )

    def _string_capacity_from_type_expr(self, type_expr, call_generic_map=None, generic_map=None):
        if not type_expr or type_expr.name != 'String':
            return None
        merged_map = dict(generic_map or self.current_generic_map)
        for key, val in (call_generic_map or {}).items():
            if isinstance(val, int):
                merged_map[key] = val
        if type_expr.size is not None:
            try:
                return self.evaluate_constant(type_expr.size, merged_map)
            except LatticeTypeError:
                return None
        return None

    def _maybe_lower_string_arg(self, arg, param_type_expr, call_generic_map, generic_map):
        if not isinstance(arg, StringLiteral):
            return arg
        cap = self._string_capacity_from_type_expr(param_type_expr, call_generic_map, generic_map)
        if cap is None:
            cap = len(arg.value)
        return lower_string_literal(arg, cap)

    def _struct_string_max_len(self, resolved_type):
        if isinstance(resolved_type, StructResolvedType) and resolved_type.name.startswith('String_'):
            suffix = resolved_type.name[len('String_'):]
            if suffix.isdigit():
                return int(suffix)
        return None

    def _compile_time_string(self, expr):
        if isinstance(expr, StringLiteral):
            return expr.value
        if isinstance(expr, Identifier):
            return self.string_consts.get(expr.name)
        return None

    def fold_string_concat(self, expr):
        if expr is None:
            return expr
        if isinstance(expr, UnaryExpr):
            expr.operand = self.fold_string_concat(expr.operand)
            return expr
        if isinstance(expr, BinaryExpr):
            expr.left = self.fold_string_concat(expr.left)
            expr.right = self.fold_string_concat(expr.right)
            if expr.op == '+':
                left_str = self._compile_time_string(expr.left)
                right_str = self._compile_time_string(expr.right)
                if left_str is not None and right_str is not None:
                    return StringLiteral(left_str + right_str, expr.line)
            return expr
        if isinstance(expr, CallExpr):
            expr.args = [self.fold_string_concat(arg) for arg in expr.args]
            return expr
        if isinstance(expr, IndexExpr):
            expr.expr = self.fold_string_concat(expr.expr)
            expr.index = self.fold_string_concat(expr.index)
            return expr
        if isinstance(expr, FieldExpr):
            expr.expr = self.fold_string_concat(expr.expr)
            return expr
        if isinstance(expr, ListLiteral):
            expr.elements = [self.fold_string_concat(el) for el in expr.elements]
            return expr
        return expr

    def _collect_string_consts(self, body):
        def visit_expr(expr):
            if isinstance(expr, CallExpr):
                for arg in expr.args:
                    visit_expr(arg)
            elif isinstance(expr, BinaryExpr):
                visit_expr(expr.left)
                visit_expr(expr.right)
            elif isinstance(expr, UnaryExpr):
                visit_expr(expr.operand)
            elif isinstance(expr, IndexExpr):
                visit_expr(expr.expr)
                visit_expr(expr.index)
            elif isinstance(expr, FieldExpr):
                visit_expr(expr.expr)
            elif isinstance(expr, ListLiteral):
                for el in expr.elements:
                    visit_expr(el)

        def visit_stmt(stmt):
            if isinstance(stmt, VarDecl):
                if isinstance(stmt.value, StringLiteral):
                    self.string_consts[stmt.name] = stmt.value.value
                visit_expr(stmt.value)
            elif isinstance(stmt, Assign):
                visit_expr(stmt.target)
                visit_expr(stmt.value)
            elif isinstance(stmt, IfStmt):
                visit_expr(stmt.cond)
                for s in stmt.then_branch:
                    visit_stmt(s)
                for s in stmt.else_branch:
                    visit_stmt(s)
            elif isinstance(stmt, ForStmt):
                visit_expr(stmt.start)
                visit_expr(stmt.end)
                for s in stmt.body:
                    visit_stmt(s)
            elif isinstance(stmt, MatchStmt):
                visit_expr(stmt.expr)
                for case in stmt.cases:
                    for s in case.body:
                        visit_stmt(s)
            elif isinstance(stmt, ReturnStmt):
                if stmt.expr:
                    visit_expr(stmt.expr)
            elif isinstance(stmt, ExprStmt):
                visit_expr(stmt.expr)

        for stmt in body:
            visit_stmt(stmt)

    def _fold_body_exprs(self, body):
        def fold_stmt(stmt):
            if isinstance(stmt, VarDecl):
                stmt.value = self.fold_string_concat(stmt.value)
            elif isinstance(stmt, Assign):
                stmt.target = self.fold_string_concat(stmt.target)
                stmt.value = self.fold_string_concat(stmt.value)
            elif isinstance(stmt, IfStmt):
                stmt.cond = self.fold_string_concat(stmt.cond)
                for s in stmt.then_branch:
                    fold_stmt(s)
                for s in stmt.else_branch:
                    fold_stmt(s)
            elif isinstance(stmt, ForStmt):
                stmt.start = self.fold_string_concat(stmt.start)
                stmt.end = self.fold_string_concat(stmt.end)
                for s in stmt.body:
                    fold_stmt(s)
            elif isinstance(stmt, MatchStmt):
                stmt.expr = self.fold_string_concat(stmt.expr)
                for case in stmt.cases:
                    for s in case.body:
                        fold_stmt(s)
            elif isinstance(stmt, ReturnStmt):
                if stmt.expr:
                    stmt.expr = self.fold_string_concat(stmt.expr)
            elif isinstance(stmt, ExprStmt):
                stmt.expr = self.fold_string_concat(stmt.expr)

        for stmt in body:
            fold_stmt(stmt)

    def finalize_generic_map(self, generic_map, generic_decls, line, callee_name=None, allow_function_generics=False):
        callee_name = callee_name or self._current_callee_name()
        for gname, gtype_expr in generic_decls:
            if generic_map.get(gname) is not None:
                val = generic_map[gname]
                gtype_name = gtype_expr.name if hasattr(gtype_expr, 'name') else str(gtype_expr)
                if gtype_name != 'Type' and isinstance(val, int) and val < 0:
                    raise LatticeTypeError(
                        f"Generic size parameter '{gname}' must be non-negative, got {val}",
                        line,
                    )
                continue
            if (
                allow_function_generics
                and gname in self.current_generic_map
                and self.current_generic_map[gname] is None
            ):
                continue
            raise LatticeTypeError(
                f"Cannot infer generic parameter '{gname}' for '{callee_name}'",
                line,
                hint=generic_inference_hint(callee_name, generic_decls, generic_map),
            )
        return generic_map

    def _unwrap_io_type(self, resolved_type):
        if isinstance(resolved_type, IOResolvedType):
            return resolved_type.inner
        return resolved_type

    def _body_has_io(self, body):
        saved_locals = dict(self.locals)
        found = False

        def expr_has_io(expr):
            if self._is_input_call(expr) or self._is_read_file_call(expr) or self._is_http_get_call(expr):
                return True
            try:
                t = self.infer_expr_type(expr)
            except LatticeTypeError:
                return False
            return isinstance(t, IOResolvedType)

        def walk(stmts):
            nonlocal found
            for stmt in stmts:
                if isinstance(stmt, VarDecl):
                    if expr_has_io(stmt.value):
                        found = True
                    try:
                        if stmt.type_expr:
                            bound_t = self.resolve_type_expr(stmt.type_expr)
                        else:
                            value_t = self.infer_expr_type(stmt.value)
                            bound_t = self._unwrap_io_type(value_t)
                        self.locals[stmt.name] = (bound_t, 0)
                    except LatticeTypeError:
                        pass
                elif isinstance(stmt, ExprStmt):
                    if expr_has_io(stmt.expr):
                        found = True
                elif isinstance(stmt, ReturnStmt):
                    if stmt.expr and expr_has_io(stmt.expr):
                        found = True
                elif isinstance(stmt, IfStmt):
                    walk(stmt.then_branch)
                    walk(stmt.else_branch)
                elif isinstance(stmt, ForStmt):
                    walk(stmt.body)
                elif isinstance(stmt, MatchStmt):
                    for case in stmt.cases:
                        walk(case.body)
                elif isinstance(stmt, Assign):
                    if expr_has_io(stmt.value):
                        found = True

        walk(body)
        self.locals = saved_locals
        return found

    def _require_io_allowed(self, line, context="IO operation"):
        if not self.current_func_has_io:
            raise LatticeTypeError(
                f"{context} in pure function",
                line,
                hint="Only functions with IO effects (or an explicit -> IO[...] return type) may perform host interaction.",
            )

    def _is_read_file_call(self, expr):
        return (
            isinstance(expr, CallExpr)
            and isinstance(expr.func, Identifier)
            and expr.func.name == "read_file"
        )

    def _infer_read_file_generics(self, expected_type, path_expr, line, generic_map=None):
        inner = unwrap_input_inner(expected_type)
        if inner is None:
            raise LatticeTypeError(
                f"read_file requires an Input[T] annotation, got {format_type(expected_type)}",
                line,
                hint=_read_file_hint(),
            )
        if _is_list_resolved_type(inner):
            path_t = self.infer_expr_type(path_expr, generic_map)
            path_len = self._struct_string_max_len(path_t)
            if path_len is None and isinstance(path_expr, StringLiteral):
                path_len = len(path_expr.value)
            if path_len is None:
                raise LatticeTypeError(
                    "read_file path must have a fixed String capacity",
                    line,
                    hint="Annotate the path, e.g. let path: String(64) = \"data.json\";",
                )
            return None, path_len

        max_len = _struct_string_max_len(inner)
        if max_len is None:
            raise LatticeTypeError(
                f"read_file requires Input[String(N)] or Input[List(N)[T]], got Input[{format_type(inner)}]",
                line,
                hint=_read_file_hint(),
            )
        path_t = self.infer_expr_type(path_expr, generic_map)
        path_len = self._struct_string_max_len(path_t)
        if path_len is None and isinstance(path_expr, StringLiteral):
            path_len = len(path_expr.value)
        if path_len is None:
            raise LatticeTypeError(
                "read_file path must have a fixed String capacity",
                line,
                hint="Annotate the path, e.g. let path: String(64) = \"data.json\";",
            )
        return max_len, path_len

    def _ensure_read_file_generics(self, expr, max_len, path_len):
        if max_len is None:
            return
        if not expr.generic_args:
            expr.generic_args = [
                Literal(max_len, "Integer", expr.line),
                Literal(path_len, "Integer", expr.line),
            ]

    def _check_read_file_call(self, expr, generic_map):
        if id(expr) in self.read_file_call_exprs:
            return
        if expr.generic_args and len(expr.generic_args) >= 2:
            return
        raise LatticeTypeError(
            "Cannot infer generic parameters for read_file(...)",
            expr.line,
            column=getattr(expr.func, "column", None),
            span=len("read_file"),
            hint=_read_file_hint(),
        )

    def _register_read_file_call(self, expr, resolved_type, type_expr=None):
        key = id(expr)
        if key in self.read_file_call_exprs:
            return self.read_file_call_exprs[key]
        validate_read_file_target_type(resolved_type, type_expr, expr.line)
        inner = unwrap_input_inner(resolved_type)
        site_id = len(self.read_file_call_sites)
        column = getattr(expr.func, "column", 1)
        site = {
            "id": site_id,
            "line": expr.line,
            "column": column,
            "resolved_type": resolved_type,
            "type_expr": type_expr,
        }
        if inner is not None and _is_list_resolved_type(inner):
            site["json_cap"] = read_file_json_cap(inner.length)
        self.read_file_call_sites.append(site)
        self.read_file_call_exprs[key] = site_id
        return site_id

    def _is_http_get_call(self, expr):
        return (
            isinstance(expr, CallExpr)
            and isinstance(expr.func, Identifier)
            and expr.func.name == "http_get"
        )

    def _check_http_get_call(self, expr, generic_map):
        if id(expr) in self.http_get_call_exprs:
            return
        if expr.generic_args and len(expr.generic_args) >= 2:
            return
        raise LatticeTypeError(
            "Cannot infer generic parameters for http_get(...)",
            expr.line,
            column=getattr(expr.func, "column", None),
            span=len("http_get"),
            hint=_http_get_hint(),
        )

    def _register_http_get_call(self, expr, resolved_type, type_expr=None):
        key = id(expr)
        if key in self.http_get_call_exprs:
            return self.http_get_call_exprs[key]
        validate_http_get_target_type(resolved_type, type_expr, expr.line)
        inner = unwrap_input_inner(resolved_type)
        site_id = len(self.http_get_call_sites)
        column = getattr(expr.func, "column", 1)
        site = {
            "id": site_id,
            "line": expr.line,
            "column": column,
            "resolved_type": resolved_type,
            "type_expr": type_expr,
        }
        if inner is not None and _is_list_resolved_type(inner):
            site["json_cap"] = read_file_json_cap(inner.length)
        self.http_get_call_sites.append(site)
        self.http_get_call_exprs[key] = site_id
        return site_id

    def _is_input_call(self, expr):
        return (
            isinstance(expr, CallExpr)
            and isinstance(expr.func, Identifier)
            and expr.func.name == "input"
        )

    def _register_input_call(self, expr, resolved_type, type_expr=None):
        key = id(expr)
        if key in self.input_call_exprs:
            return self.input_call_exprs[key]
        validate_input_target_type(resolved_type, type_expr, expr.line)
        site_id = len(self.input_call_sites)
        column = getattr(expr.func, "column", 1)
        self.input_call_sites.append(
            {
                "id": site_id,
                "line": expr.line,
                "column": column,
                "resolved_type": resolved_type,
                "type_expr": type_expr,
            }
        )
        self.input_call_exprs[key] = site_id
        return site_id

    def _check_input_call(self, expr, generic_map):
        if id(expr) in self.input_call_exprs:
            return

        if self._input_expected_type is None:
            raise LatticeTypeError(
                "Cannot infer the result type of input(...)",
                expr.line,
                column=getattr(expr.func, "column", None),
                span=len("input"),
                hint=_input_hint(),
            )
        if len(expr.args) != 1:
            raise LatticeTypeError(
                "Wrong number of arguments to 'input': expected 1 prompt string, "
                f"got {len(expr.args)}",
                expr.line,
                hint='Usage: let value: YourType = input("prompt text");',
            )
        self._register_input_call(expr, self._input_expected_type, None)
        arg_t = self.infer_expr_type(expr.args[0], generic_map)
        if isinstance(expr.args[0], StringLiteral):
            cap = len(expr.args[0].value)
            expr.args[0] = lower_string_literal(expr.args[0], cap)
            arg_t = self.infer_expr_type(expr.args[0], generic_map)
        prompt_param = get_overloads(self.functions, "input")[0].params[0]
        prompt_t = self.resolve_type_expr(prompt_param.type_expr)
        if isinstance(prompt_t, RefinedResolvedType):
            prompt_t = prompt_t.base_type
        if isinstance(prompt_t, StructResolvedType) and prompt_t.name.startswith("String_"):
            pass
        else:
            prompt_te = TypeExpr("String", [], expr.line, size=TypeExpr("PromptLen", [], expr.line))
            prompt_t = self.resolve_type_expr(prompt_te)
        try:
            self.types_compatible(prompt_t, arg_t, expr.line)
        except LatticeTypeError as e:
            raise LatticeTypeError(
                f"Argument 1 ('prompt') to 'input': {e.message}",
                expr.line,
                hint=e.hint,
            ) from e

    def types_compatible(self, expected, actual, line):
        if expected is None or actual is None:
            return

        if isinstance(expected, IOResolvedType) and not isinstance(actual, IOResolvedType):
            return self.types_compatible(expected.inner, actual, line)

        if isinstance(actual, IOResolvedType) and not isinstance(expected, IOResolvedType):
            raise LatticeTypeError(
                type_mismatch_message(expected, actual, "Expected a pure value, got an IO action"),
                line,
                hint="IO actions may only appear in IO functions. Use `let x = io_call(...)` to run the action and bind its result.",
            )

        if isinstance(expected, IOResolvedType) and isinstance(actual, IOResolvedType):
            return self.types_compatible(expected.inner, actual.inner, line)

        if isinstance(actual, RefinedResolvedType):
            actual = actual.base_type
        if isinstance(expected, RefinedResolvedType):
            expected = expected.base_type

        if isinstance(expected, PrimitiveResolvedType) and isinstance(actual, PrimitiveResolvedType):
            if expected.name != actual.name:
                raise LatticeTypeError(
                    type_mismatch_message(expected, actual, "Incompatible primitive types"),
                    line,
                    hint=type_mismatch_hint(expected, actual),
                )
            return

        if isinstance(expected, ListResolvedType) and isinstance(actual, ListResolvedType):
            if expected.length != actual.length:
                if (
                    expected.length is not None
                    and actual.length is not None
                    and actual.length < expected.length
                ):
                    self.types_compatible(expected.elem_type, actual.elem_type, line)
                    return
                raise LatticeTypeError(
                    f"List capacity mismatch: expected {expected.length} elements, got {actual.length}",
                    line,
                    hint=type_mismatch_hint(expected, actual),
                )
            self.types_compatible(expected.elem_type, actual.elem_type, line)
            return

        if isinstance(expected, StructResolvedType) and isinstance(actual, StructResolvedType):
            expected_max = self._struct_string_max_len(expected)
            actual_max = self._struct_string_max_len(actual)
            if (
                expected_max is not None
                and actual_max is not None
                and expected.name.startswith("String_")
                and actual.name.startswith("String_")
                and expected_max >= actual_max
            ):
                return
            if expected.name != actual.name:
                raise LatticeTypeError(
                    type_mismatch_message(expected, actual, "Incompatible struct types"),
                    line,
                    hint=type_mismatch_hint(expected, actual),
                )
            if len(expected.fields) != len(actual.fields):
                raise LatticeTypeError(
                    f"Struct '{format_type(expected)}' field count mismatch "
                    f"(expected {len(expected.fields)} fields, got {len(actual.fields)})",
                    line,
                )
            for (exp_name, exp_f), (act_name, act_f) in zip(expected.fields, actual.fields):
                if exp_name != act_name:
                    raise LatticeTypeError(
                        f"Struct field name mismatch on '{format_type(expected)}': "
                        f"expected '{exp_name}', got '{act_name}'",
                        line,
                    )
                self.types_compatible(exp_f, act_f, line)
            return

        if isinstance(expected, UnionResolvedType) and isinstance(actual, StructResolvedType):
            if any(
                hasattr(v, 'name') and v.name == actual.name
                for v in expected.variants
            ):
                return
            raise LatticeTypeError(
                type_mismatch_message(expected, actual, "Expected a union variant"),
                line,
                hint="Construct with a matching variant, e.g. Some(value) or None().",
            )

        if isinstance(expected, UnionResolvedType) and isinstance(actual, UnionResolvedType):
            if expected.name != actual.name:
                raise LatticeTypeError(
                    type_mismatch_message(expected, actual, "Incompatible union types"),
                    line,
                    hint=type_mismatch_hint(expected, actual),
                )
            return

        if type(expected) is not type(actual):
            raise LatticeTypeError(
                type_mismatch_message(expected, actual, "Incompatible types"),
                line,
                hint=type_mismatch_hint(expected, actual),
            )

    def check_call_types(self, expr, generic_map=None):
        if not isinstance(expr, CallExpr) or not isinstance(expr.func, Identifier):
            return

        func_name = expr.func.name
        generic_map = generic_map or self.current_generic_map
        prev_callee = self._checking_call
        self._checking_call = func_name

        try:
            self._check_call_types_impl(expr, generic_map, func_name)
        finally:
            self._checking_call = prev_callee

    def instantiate_generic_function(self, callee, call_generic_map, line):
        if not callee.generics:
            return callee
        int_bindings = {}
        for gname, gtype in callee.generics:
            gtype_name = gtype.name if hasattr(gtype, 'name') else str(gtype)
            if gtype_name != 'Integer':
                continue
            val = call_generic_map.get(gname)
            if not isinstance(val, int):
                return callee
            int_bindings[gname] = val
        if not int_bindings:
            return callee

        key = (callee.name, getattr(callee, 'overload_index', 0), tuple(sorted(int_bindings.items())))
        if key in self.function_instantiations:
            return self.function_instantiations[key]

        clone = clone_ast_node(callee)
        clone.monomorph_generics = dict(int_bindings)
        suffix = "_".join(str(int_bindings[gname]) for gname, _ in callee.generics if gname in int_bindings)
        clone.mangled_name = f"{callee.name}#{getattr(callee, 'overload_index', 0)}__{suffix}"

        overloads = get_overloads(self.functions, callee.name)
        if not isinstance(self.functions.get(callee.name), list):
            self.functions[callee.name] = list(overloads)
        self.functions[callee.name].append(clone)
        self.function_instantiations[key] = clone
        self.pending_instantiation_resolve.append(clone)
        return clone

    def _check_call_types_impl(self, expr, generic_map, func_name):
        if func_name == "input":
            self._check_input_call(expr, generic_map)
            return

        if func_name == "read_file":
            self._check_read_file_call(expr, generic_map)
            return
        if func_name == "http_get":
            self._check_http_get_call(expr, generic_map)
            return

        if func_name in ("keys", "values", "join"):
            # Validates arg types (raises on mismatch) via the shared inference path.
            self._infer_map_intrinsic(func_name, expr, generic_map)
            return

        if get_overloads(self.functions, func_name):
            callee, call_generic_map = resolve_call(self, func_name, expr, generic_map)
            callee = self.instantiate_generic_function(callee, call_generic_map, expr.line)
            if id(expr) in self.resolved_calls:
                self.resolved_calls[id(expr)]["decl"] = callee
            for idx, arg in enumerate(expr.args):
                if isinstance(arg, StringLiteral):
                    expr.args[idx] = self._maybe_lower_string_arg(
                        arg, callee.params[idx].type_expr, call_generic_map, generic_map
                    )

            for idx, arg in enumerate(expr.args):
                arg_t = self.infer_expr_type(arg, generic_map)
                param = callee.params[idx]
                param_t = self.resolve_type_expr(param.type_expr, call_generic_map or generic_map)
                if (
                    callee.kind == 'external'
                    and not callee.body
                    and isinstance(param_t, PrimitiveResolvedType)
                    and param_t.name == 'Integer'
                    and isinstance(arg_t, (StructResolvedType, UnionResolvedType, ListResolvedType))
                ):
                    continue
                try:
                    self.types_compatible(param_t, arg_t, expr.line)
                except LatticeTypeError as e:
                    raise LatticeTypeError(
                        f"Argument {idx + 1} ('{param.name}') to '{func_name}': {e.message}",
                        expr.line,
                        hint=e.hint or type_mismatch_hint(param_t, arg_t),
                    ) from e
            return

        if func_name in self.type_decls:
            decl = self.type_decls[func_name]
            if len(expr.args) != len(decl.fields):
                field_desc = ", ".join(
                    f"{fname}: {format_type_expr(ftype)}" for fname, ftype in decl.fields
                )
                raise LatticeTypeError(
                    f"Wrong number of arguments to '{func_name}' constructor: "
                    f"expected {len(decl.fields)}, got {len(expr.args)}",
                    expr.line,
                    hint=f"Expected: {func_name}({field_desc})",
                )

            call_generic_map = {}
            if decl.generics:
                for gname, _ in decl.generics:
                    call_generic_map[gname] = None
                if expr.generic_args:
                    for i, (gname, _) in enumerate(decl.generics):
                        if i < len(expr.generic_args):
                            call_generic_map[gname] = expr.generic_args[i]
                else:
                    for idx, arg in enumerate(expr.args):
                        if idx < len(decl.fields):
                            arg_t = self.infer_expr_type(arg, generic_map)
                            self.infer_call_generics(decl.fields[idx][1], arg_t, call_generic_map)
                self.finalize_generic_map(call_generic_map, decl.generics, expr.line, callee_name=func_name)

            for idx, arg in enumerate(expr.args):
                if isinstance(arg, StringLiteral):
                    expr.args[idx] = self._maybe_lower_string_arg(
                        arg, decl.fields[idx][1], call_generic_map, generic_map
                    )

            for idx, arg in enumerate(expr.args):
                arg_t = self.infer_expr_type(arg, generic_map)
                fname, field_type_expr = decl.fields[idx]
                field_t = self.resolve_type_expr(field_type_expr, call_generic_map or generic_map)
                try:
                    self.types_compatible(field_t, arg_t, expr.line)
                except LatticeTypeError as e:
                    raise LatticeTypeError(
                        f"Constructor field '{fname}' on '{func_name}': {e.message}",
                        expr.line,
                        hint=e.hint,
                    ) from e

    def _binding_to_type_expr(self, val, line):
        if isinstance(val, int):
            return Literal(val, 'Integer', line)
        if isinstance(val, ResolvedType):
            return self.resolved_to_type_expr(val, line)
        if isinstance(val, TypeExpr):
            return val
        return TypeExpr(str(val), [], line)

    def generic_bindings_to_type_expr(self, type_name, decl, bindings, line):
        args = []
        size = None
        for gname, gtype in decl.generics:
            val = bindings.get(gname)
            te_val = self._binding_to_type_expr(val, line)
            gtype_name = gtype.name if hasattr(gtype, 'name') else str(gtype)
            if gtype_name == 'Integer' and type_name in ('List', 'Group', 'String'):
                size = te_val
            elif gtype_name == 'Type':
                args.append(te_val)
            else:
                args.append(te_val)
        return TypeExpr(type_name, args, line, size=size)

    def resolved_to_type_expr(self, r_t, line):
        if not r_t:
            return TypeExpr('void', [], line)
        if isinstance(r_t, RefinedResolvedType):
            base_te = self.resolved_to_type_expr(r_t.base_type, line)
            return TypeExpr(base_te.name, base_te.args, line, r_t.constraint, r_t.constraint_var)
        if isinstance(r_t, int):
            return Literal(r_t, 'Integer', line)
        if isinstance(r_t, PrimitiveResolvedType):
            return TypeExpr(r_t.name, [], line)
        if isinstance(r_t, StructResolvedType) or isinstance(r_t, UnionResolvedType):
            return TypeExpr(r_t.name, [], line)
        if isinstance(r_t, ListResolvedType):
            elem_te = self.resolved_to_type_expr(r_t.elem_type, line)
            te = TypeExpr('List', [elem_te], line)
            if r_t.length is not None:
                te.size = Literal(r_t.length, 'Integer', line)
            return te
        if isinstance(r_t, StringResolvedType):
            return TypeExpr('String', [], line, size=Literal(r_t.max_len, 'Integer', line))
        if isinstance(r_t, IOResolvedType):
            return TypeExpr('IO', [self.resolved_to_type_expr(r_t.inner, line)], line)
        return TypeExpr('Integer', [], line)

    def infer_call_generics(self, param_type, arg_type, call_generic_map):
        if not param_type or not arg_type:
            return
        if isinstance(param_type, TypeExpr):
            if param_type.name in call_generic_map:
                call_generic_map[param_type.name] = arg_type
                return
            
            if isinstance(arg_type, ListResolvedType) and param_type.name in ['List', 'Group']:
                if len(param_type.args) == 1:
                    elem_arg = param_type.args[0]
                    if isinstance(elem_arg, TypeExpr) and elem_arg.name in call_generic_map:
                        call_generic_map[elem_arg.name] = arg_type.elem_type
                    if param_type.size is not None:
                        if (
                            isinstance(param_type.size, TypeExpr)
                            and param_type.size.name in call_generic_map
                        ):
                            call_generic_map[param_type.size.name] = arg_type.length
                return

            if (
                param_type.name == 'String'
                and param_type.size is not None
                and isinstance(param_type.size, TypeExpr)
                and param_type.size.name in call_generic_map
            ):
                max_len = self._struct_string_max_len(arg_type)
                if max_len is not None:
                    call_generic_map[param_type.size.name] = max_len
                return
                
            if hasattr(arg_type, 'name') and arg_type.name.startswith(param_type.name):
                if param_type.name in self.type_decls:
                    decl = self.type_decls[param_type.name]
                    parts = arg_type.name[len(param_type.name)+1:].split('_')
                    for i, (gname, _) in enumerate(decl.generics):
                        if i < len(param_type.args) and i < len(parts):
                            p_arg = param_type.args[i]
                            if isinstance(p_arg, TypeExpr):
                                part = parts[i]
                                if p_arg.name in call_generic_map:
                                    try:
                                        call_generic_map[p_arg.name] = int(part)
                                    except ValueError:
                                        try:
                                            call_generic_map[p_arg.name] = self.resolve_type_expr(TypeExpr(part, [], p_arg.line))
                                        except Exception:
                                            pass

    def _is_integer_builtin_binop(self, op, left_t, right_t):
        return (
            left_t.name == 'Integer'
            and right_t.name == 'Integer'
            and op in INTEGER_BINOPS
        )

    def _is_char_builtin_binop(self, op, left_t, right_t):
        return (
            left_t.name == 'Char'
            and right_t.name == 'Char'
            and op in CHAR_BINOPS
        )

    def _is_bool_builtin_binop(self, op, left_t, right_t):
        return (
            left_t.name == 'Bool'
            and right_t.name == 'Bool'
            and op in BOOL_BINOPS
        )

    def _is_bool_builtin_unop(self, op, operand_t):
        return op == '!' and operand_t.name == 'Bool'

    def _is_rational_type(self, resolved_type):
        return (
            isinstance(resolved_type, StructResolvedType)
            and resolved_type.name == 'Rational'
        )

    def _float_to_rational_expr(self, expr):
        from fractions import Fraction

        frac = Fraction(expr.value)
        return CallExpr(
            Identifier('Rational', expr.line),
            [
                Literal(int(frac.numerator), 'Integer', expr.line),
                Literal(int(frac.denominator), 'Integer', expr.line),
            ],
            [],
            expr.line,
        )

    def _lower_expr(self, expr):
        if isinstance(expr, Literal) and expr.val_type == 'Float':
            return self._float_to_rational_expr(expr)
        if isinstance(expr, StructLiteral):
            return StructLiteral(
                [(fname, self._lower_expr(fexpr)) for fname, fexpr in expr.fields],
                expr.line,
            )
        if isinstance(expr, MapLiteral):
            return MapLiteral(
                [(key, self._lower_expr(val)) for key, val in expr.entries],
                expr.line,
            )
        if isinstance(expr, BinaryExpr):
            return BinaryExpr(
                expr.op,
                self._lower_expr(expr.left),
                self._lower_expr(expr.right),
                expr.line,
            )
        if isinstance(expr, UnaryExpr):
            return UnaryExpr(expr.op, self._lower_expr(expr.operand), expr.line)
        if isinstance(expr, CallExpr):
            return CallExpr(
                expr.func,
                [self._lower_expr(arg) for arg in expr.args],
                expr.generic_args,
                expr.line,
            )
        if isinstance(expr, FieldExpr):
            return FieldExpr(self._lower_expr(expr.expr), expr.field, expr.line)
        if isinstance(expr, IndexExpr):
            return IndexExpr(
                self._lower_expr(expr.expr),
                self._lower_expr(expr.index),
                expr.line,
            )
        if isinstance(expr, ListLiteral):
            return ListLiteral(
                [self._lower_expr(el) for el in expr.elements],
                expr.line,
            )
        return expr

    def _resolve_struct_literal_type(self, expr, generic_map):
        if not expr.fields:
            raise LatticeTypeError("Struct literal cannot be empty", expr.line)
        resolved_fields = []
        sig_parts = []
        for fname, fexpr in expr.fields:
            lowered = self._lower_expr(fexpr)
            ftype = self.infer_expr_type(lowered, generic_map)
            resolved_fields.append((fname, ftype))
            sig_parts.append(f"{fname}_{ftype.name}")
        mono_name = "__struct_" + "_".join(sig_parts)
        if mono_name in self.types:
            return self.types[mono_name]
        struct_t = StructResolvedType(mono_name, resolved_fields, [])
        self.register_type(mono_name, struct_t)
        return struct_t

    def _input_type_for(self, inner_type, line):
        te = TypeExpr('Input', [self.resolved_to_type_expr(inner_type, line)], line)
        return self.resolve_type_expr(te)

    def _resolve_operator_overload(self, func_name, left, right, line, generic_map):
        fake_call = CallExpr(
            Identifier(func_name, line),
            [left, right],
            [],
            line,
        )
        return resolve_call(self, func_name, fake_call, generic_map)

    def _try_record_binop_overload(self, expr, op, left_t, right_t, generic_map):
        op_func = BINOP_TO_FUNCTION.get(op)
        if not op_func or not get_overloads(self.functions, op_func):
            return None
        callee, call_generic_map = self._resolve_operator_overload(
            op_func, expr.left, expr.right, expr.line, generic_map
        )
        self.resolved_binops[id(expr)] = {
            "decl": callee,
            "generic_map": call_generic_map,
        }
        if callee.ret_type:
            ret_t = self.resolve_type_expr(callee.ret_type, call_generic_map)
            if isinstance(ret_t, IOResolvedType):
                return ret_t
            return ret_t
        return self.types['void']

    def _infer_map_intrinsic(self, func_name, expr, generic_map):
        # keys(map) / values(map): materialize the compile-time dictionary into a
        # list of its keys or values. join(list, sep): concatenate a list of
        # strings with a separator.
        if func_name in ("keys", "values"):
            if len(expr.args) != 1:
                raise LatticeTypeError(
                    f"{func_name}(dictionary) takes exactly one argument",
                    expr.line,
                    hint=f"Use {func_name}(my_dict) where my_dict is a dictionary literal.",
                )
            map_t = self.infer_expr_type(expr.args[0], generic_map)
            if not isinstance(map_t, StaticMapResolvedType):
                raise LatticeTypeError(
                    f"{func_name}(...) expects a dictionary, got {format_type(map_t)}",
                    expr.line,
                    hint='Build a dictionary with a map literal, e.g. let d = { "a": 1, "b": 2 };',
                )
            n = len(map_t.entries)
            if func_name == "keys":
                max_key = max((len(k) for k, _ in map_t.entries), default=0)
                elem_t = self.resolve_type_expr(
                    TypeExpr('String', [], expr.line, size=Literal(max_key, 'Integer', expr.line)),
                    generic_map,
                )
            else:
                elem_t = map_t.value_type
            return MaterializedListResolvedType(elem_t, n)

        # join(list, separator)
        if len(expr.args) != 2:
            raise LatticeTypeError(
                "join(list, separator) takes exactly two arguments",
                expr.line,
                hint='Use join(keys(my_dict), ", ").',
            )
        list_t = self.infer_expr_type(expr.args[0], generic_map)
        if not isinstance(list_t, MaterializedListResolvedType):
            raise LatticeTypeError(
                f"join(...) expects keys(...)/values(...) of a dictionary, got {format_type(list_t)}",
                expr.line,
                hint='Use join(keys(my_dict), ", ") or join(values(my_dict), ", ").',
            )
        elem_max = self._struct_string_max_len(list_t.elem_type)
        if elem_max is None:
            raise LatticeTypeError(
                "join(...) can only join a list of strings",
                expr.line,
                hint="join(keys(map), sep) works because keys are strings; values must be strings too.",
            )
        sep_t = self.infer_expr_type(expr.args[1], generic_map)
        sep_max = self._struct_string_max_len(sep_t)
        if sep_max is None:
            raise LatticeTypeError(
                f"join separator must be a String, got {format_type(sep_t)}",
                expr.line,
            )
        n = list_t.length
        out_max = n * elem_max + max(0, n - 1) * sep_max
        return self.resolve_type_expr(
            TypeExpr('String', [], expr.line, size=Literal(out_max, 'Integer', expr.line)),
            generic_map,
        )

    def infer_expr_type(self, expr, generic_map=None):
        generic_map = generic_map or self.current_generic_map
        
        if isinstance(expr, Literal):
            if expr.val_type == 'Float':
                return self.types['Rational']
            return self.types[expr.val_type]

        if isinstance(expr, StructLiteral):
            return self._resolve_struct_literal_type(expr, generic_map)

        if isinstance(expr, MapLiteral):
            if not expr.entries:
                raise LatticeTypeError("Map literal cannot be empty", expr.line)
            first_val = self._lower_expr(expr.entries[0][1])
            value_type = self.infer_expr_type(first_val, generic_map)
            for _key, val in expr.entries[1:]:
                val_t = self.infer_expr_type(self._lower_expr(val), generic_map)
                self.types_compatible(value_type, val_t, expr.line)
            return StaticMapResolvedType(value_type, [])

        if isinstance(expr, StringLiteral):
            return self.infer_expr_type(lower_string_literal(expr, len(expr.value)), generic_map)

        if isinstance(expr, Identifier):
            # 1. Check local variable
            if expr.name in self.locals:
                return self.locals[expr.name][0]
            if expr.name in self.memory_locals:
                return self.memory_locals[expr.name][0]
            # 2. Check global variable
            if expr.name in self.globals:
                return self.globals[expr.name][0]
            # 3. Check if generic type argument or constant
            if expr.name in generic_map:
                val = generic_map[expr.name]
                if isinstance(val, ResolvedType):
                    return val
                if isinstance(val, int):
                    return self.types['Integer']
                if val is None and expr.name in self.current_generic_map:
                    return self.types['Integer']
            line, column, span = error_site_from_expr(expr)
            raise LatticeTypeError(
                f"Undefined variable '{expr.name}'",
                line,
                column=column,
                span=span,
            )

        if isinstance(expr, UnaryExpr):
            operand_t = self.infer_expr_type(expr.operand, generic_map)
            if self._is_bool_builtin_unop(expr.op, operand_t):
                return self.types['Bool']
            op_func = UNOP_TO_FUNCTION.get(expr.op)
            if op_func and get_overloads(self.functions, op_func):
                fake_call = CallExpr(
                    Identifier(op_func, expr.line),
                    [expr.operand],
                    [],
                    expr.line,
                )
                callee, call_generic_map = resolve_call(
                    self, op_func, fake_call, generic_map
                )
                self.resolved_unops[id(expr)] = {
                    "decl": callee,
                    "generic_map": call_generic_map,
                }
                if callee.ret_type:
                    return self.resolve_type_expr(callee.ret_type, call_generic_map)
                return self.types['void']
            if expr.op == '!':
                return self.types['Bool']
            return operand_t

        if isinstance(expr, BinaryExpr):
            left_t = self.infer_expr_type(expr.left, generic_map)
            right_t = self.infer_expr_type(expr.right, generic_map)

            if self._is_bool_builtin_binop(expr.op, left_t, right_t):
                return self.types['Bool']

            if self._is_integer_builtin_binop(expr.op, left_t, right_t):
                if expr.op == '/':
                    return self.types['Integer']
                if expr.op in ['==', '!=', '<', '>', '<=', '>=']:
                    return self.types['Bool']
                return self.types['Integer']

            if self._is_char_builtin_binop(expr.op, left_t, right_t):
                return self.types['Bool']

            left_max = self._struct_string_max_len(left_t)
            right_max = self._struct_string_max_len(right_t)
            if expr.op == '+' and left_max is not None and self._is_rational_type(right_t):
                return self.resolve_type_expr(
                    TypeExpr(
                        'String',
                        [],
                        expr.line,
                        size=Literal(left_max + 32, 'Integer', expr.line),
                    ),
                    generic_map,
                )
            if expr.op == '+' and left_max is not None and right_t.name == 'Integer':
                return self.resolve_type_expr(
                    TypeExpr(
                        'String',
                        [],
                        expr.line,
                        size=Literal(left_max + 16, 'Integer', expr.line),
                    ),
                    generic_map,
                )
            if left_max is not None and right_max is not None:
                if expr.op == '+':
                    out_max = left_max + right_max
                    ret_t = self.resolve_type_expr(
                        TypeExpr('String', [], expr.line, size=Literal(out_max, 'Integer', expr.line)),
                        generic_map,
                    )
                    try:
                        overload_ret = self._try_record_binop_overload(
                            expr, expr.op, left_t, right_t, generic_map
                        )
                        if overload_ret is not None:
                            return overload_ret
                    except LatticeTypeError:
                        pass
                    return ret_t
                if expr.op in ['==', '!=']:
                    try:
                        overload_ret = self._try_record_binop_overload(
                            expr, expr.op, left_t, right_t, generic_map
                        )
                        if overload_ret is not None:
                            return overload_ret
                    except LatticeTypeError:
                        pass
                    return self.types['Bool']

            try:
                overload_ret = self._try_record_binop_overload(
                    expr, expr.op, left_t, right_t, generic_map
                )
                if overload_ret is not None:
                    return overload_ret
            except LatticeTypeError:
                pass

            if expr.op in ['==', '!=', '<', '>', '<=', '>=']:
                return self.types['Bool']

            return left_t

        if isinstance(expr, ListLiteral):
            if not expr.elements:
                raise LatticeTypeError(
                    "Cannot infer type of empty list literal",
                    expr.line,
                    hint=(
                        "Give an explicit type, e.g. let xs: List(5)[Integer] = List([1, 2, 3]), "
                        "or List[Integer] with inferred length. " + static_memory_hint()
                    ),
                )
            elem_types = [self.infer_expr_type(el, generic_map) for el in expr.elements]

            # Special-case: lists of string literals.
            # String literals monomorphize into StructResolvedType instances like
            # String_5 / String_3 / ...
            # We can infer a safe capacity by widening to the maximum literal
            # length, then padding shorter literals.
            if elem_types:
                elem_max_lens = []
                for t in elem_types:
                    if isinstance(t, StringResolvedType):
                        elem_max_lens.append(t.max_len)
                    else:
                        elem_max_lens.append(self._struct_string_max_len(t))

                if all(m is not None for m in elem_max_lens):
                    max_len = max(elem_max_lens)
                    unified_elem_t = self.resolve_type_expr(
                        TypeExpr(
                            'String',
                            [],
                            expr.line,
                            size=Literal(max_len, 'Integer', expr.line),
                        ),
                        generic_map,
                    )
                    for i, el in enumerate(expr.elements):
                        if isinstance(el, StringLiteral):
                            expr.elements[i] = lower_string_literal(el, max_len)
                    first_t = unified_elem_t
                else:
                    first_t = elem_types[0]
                    for i, el_type in enumerate(elem_types[1:], start=1):
                        if repr(el_type) != repr(first_t):
                            raise LatticeTypeError(
                                f"List elements must have the same type: expected {format_type(first_t)}, "
                                f"got {format_type(el_type)} at position {i + 1}",
                                expr.line,
                            )
            else:
                first_t = elem_types[0]
                for i, el_type in enumerate(elem_types[1:], start=1):
                    # Compare structurally so nested lists/strings/structs work, not
                    # just simple named primitives.
                    if repr(el_type) != repr(first_t):
                        raise LatticeTypeError(
                            f"List elements must have the same type: expected {format_type(first_t)}, "
                            f"got {format_type(el_type)} at position {i + 1}",
                            expr.line,
                        )
            # The element size is inferred fine, but a List is stored as a handle to
            # its data, and the code generator can't yet nest that indirection inside
            # another List's inline storage. Fail clearly instead of silently
            # producing garbage.
            if isinstance(first_t, (ListResolvedType, MaterializedListResolvedType)):
                raise LatticeTypeError(
                    "Nested list literals (a List whose elements are themselves Lists) "
                    "are not supported.",
                    expr.line,
                    hint=(
                        "The size of "
                        f"{format_type(ListResolvedType(len(expr.elements), first_t))} "
                        "is fully inferred, but 2D storage isn't lowered yet. Model rows "
                        "with a struct instead, e.g. `type Row(a: Integer, b: Integer)` "
                        "then `List([Row(1, 2), Row(3, 4)])`."
                    ),
                )
            return ListResolvedType(len(expr.elements), first_t)

        if isinstance(expr, IndexExpr):
            arr_t = self.infer_expr_type(expr.expr, generic_map)
            if isinstance(arr_t, ListResolvedType):
                return arr_t.elem_type
            if isinstance(arr_t, StaticMapResolvedType):
                index_t = self.infer_expr_type(expr.index, generic_map)
                index_max = self._struct_string_max_len(index_t)
                if index_max is None:
                    raise LatticeTypeError(
                        "Map lookup key must be a String(N)",
                        expr.line,
                    )
                return self._input_type_for(arr_t.value_type, expr.line)
            raise LatticeTypeError(
                f"Cannot index into {format_type(arr_t)} — only List and map types support []",
                expr.line,
            )

        if isinstance(expr, FieldExpr):
            struct_t = self.infer_expr_type(expr.expr, generic_map)
            if isinstance(struct_t, StructResolvedType):
                for fname, ftype in struct_t.fields:
                    if fname == expr.field:
                        return ftype
                if struct_t.name == 'Rational':
                    if expr.field in ['Numerator', 'Denominator']:
                        return self.types['Integer']
                raise LatticeTypeError(f"Struct '{struct_t.name}' has no field '{expr.field}'", expr.line)
            if isinstance(struct_t, ListResolvedType) and expr.field == 'data':
                return struct_t
            raise LatticeTypeError("Field access only valid on Struct types", expr.line)

        if isinstance(expr, CallExpr):
            # Find function
            func_name = ""
            if isinstance(expr.func, Identifier):
                func_name = expr.func.name
            else:
                raise LatticeTypeError("Dynamic function calls not supported", expr.line)

            if func_name == "input":
                if self._input_expected_type is None:
                    raise LatticeTypeError(
                        "Cannot infer the result type of input(...)",
                        expr.line,
                        column=getattr(expr.func, "column", None),
                        span=len("input"),
                        hint=_input_hint(),
                    )
                return IOResolvedType(self._input_expected_type)

            if func_name == "read_file":
                if self._read_file_expected_type is None:
                    raise LatticeTypeError(
                        "Cannot infer the result type of read_file(...)",
                        expr.line,
                        column=getattr(expr.func, "column", None),
                        span=len("read_file"),
                        hint=_read_file_hint(),
                    )
                return IOResolvedType(self._read_file_expected_type)

            if func_name == "http_get":
                if self._http_get_expected_type is None:
                    raise LatticeTypeError(
                        "Cannot infer the result type of http_get(...)",
                        expr.line,
                        column=getattr(expr.func, "column", None),
                        span=len("http_get"),
                        hint=_http_get_hint(),
                    )
                return IOResolvedType(self._http_get_expected_type)

            if func_name in ("keys", "values", "join"):
                return self._infer_map_intrinsic(func_name, expr, generic_map)

            if not get_overloads(self.functions, func_name):
                if func_name in self.types or func_name in self.type_decls:
                    if func_name in self.type_decls:
                        decl = self.type_decls[func_name]
                        if decl.generics:
                            local_generic_map = {}
                            if expr.generic_args:
                                for i, (gname, gtype) in enumerate(decl.generics):
                                    if i < len(expr.generic_args):
                                        arg_val = expr.generic_args[i]
                                        gtype_name = gtype.name if hasattr(gtype, 'name') else str(gtype)
                                        if gtype_name == 'Type':
                                            local_generic_map[gname] = self.resolve_type_expr(arg_val, generic_map)
                                        else:
                                            local_generic_map[gname] = self.evaluate_constant(arg_val, generic_map)
                            else:
                                call_generic_map = {}
                                for gname, _ in decl.generics:
                                    call_generic_map[gname] = None
                                for idx, arg in enumerate(expr.args):
                                    if idx < len(decl.fields):
                                        try:
                                            arg_t = self.infer_expr_type(arg, generic_map)
                                            field_t_expr = decl.fields[idx][1]
                                            self.infer_call_generics(field_t_expr, arg_t, call_generic_map)
                                        except Exception:
                                            pass
                                for gname in call_generic_map:
                                    if call_generic_map[gname] is not None:
                                        local_generic_map[gname] = call_generic_map[gname]
                                self.finalize_generic_map(
                                    local_generic_map, decl.generics, expr.line,
                                    callee_name=func_name, allow_function_generics=True,
                                )
                            
                            arg_names = []
                            for gname, _ in decl.generics:
                                val = local_generic_map.get(gname, PrimitiveResolvedType('void'))
                                arg_names.append(str(val))
                            mono_name = f"{func_name}_{'_'.join(arg_names)}"
                            if mono_name in self.types:
                                return self.types[mono_name]
                            te = self.generic_bindings_to_type_expr(
                                func_name, decl, local_generic_map, expr.line
                            )
                            return self.resolve_type_expr(te, generic_map)
                    return self.resolve_type_expr(TypeExpr(func_name, [], expr.line), generic_map)
                line, column, span = error_site_from_expr(expr)
                raise LatticeTypeError(
                    f"Undefined function '{func_name}'",
                    line,
                    column=column,
                    span=span,
                    hint="Check spelling, imports, or use a stdlib function such as print_string or print_char.",
                )
                
            callee, call_generic_map = resolve_call(self, func_name, expr, generic_map)

            if callee.ret_type:
                return self.resolve_type_expr(callee.ret_type, call_generic_map)
            return self.types['void']

        raise LatticeTypeError("Could not infer type of expression", expr.line)

    # ============================================================================
    # Resolution Pass
    # ============================================================================
    def resolve_program(self, prog):
        # 1. First Pass: Collect all type declarations and function names
        for decl in prog.decls:
            if type(decl).__name__ == 'ImportDecl':
                continue
            try:
                if isinstance(decl, TypeDecl):
                    if decl.name in self.type_decls or decl.name in self.types:
                        raise LatticeTypeError(f"Redefinition of type '{decl.name}'", decl.line)
                    self.type_decls[decl.name] = decl
                    if not decl.generics:
                        # Non-generic Struct placeholder
                        self.register_type(decl.name, StructResolvedType(decl.name, [], decl.invariants))
                elif isinstance(decl, UnionTypeDecl):
                    if decl.name in self.type_decls or decl.name in self.types:
                        raise LatticeTypeError(f"Redefinition of type '{decl.name}'", decl.line)
                    self.type_decls[decl.name] = decl
                    if not decl.generics:
                        # Non-generic Union placeholder
                        self.register_type(decl.name, UnionResolvedType(decl.name, []))
                elif isinstance(decl, FuncDecl):
                    register_function(self.functions, decl)
            except LatticeTypeError as e:
                self._record_error(e)

        # 2. Second Pass: Populate field types and union variants for non-generic types
        for decl in prog.decls:
            try:
                if isinstance(decl, TypeDecl) and not decl.generics:
                    resolved = self.types[decl.name]
                    fields = []
                    for fname, ftype_expr in decl.fields:
                        fields.append((fname, self.resolve_type_expr(ftype_expr)))
                    resolved.fields = fields
                elif isinstance(decl, UnionTypeDecl) and not decl.generics:
                    resolved = self.types[decl.name]
                    variants = []
                    for vexpr in decl.variants:
                        variants.append(self.resolve_type_expr(vexpr))
                    resolved.variants = variants
            except (LatticeTypeError, KeyError) as e:
                if isinstance(e, KeyError):
                    continue
                self._record_error(e)

        # 3. Third Pass: Perform memory mapping for global & local variables
        for decl in prog.decls:
            if isinstance(decl, FuncDecl):
                try:
                    self.resolve_func_body(decl)
                except LatticeTypeError as e:
                    self._record_error(e)

        for clone in self.pending_instantiation_resolve:
            try:
                self.resolve_func_body(clone)
            except LatticeTypeError as e:
                self._record_error(e)

    def check_calls_in_body(self, body):
        def visit_expr(expr):
            if isinstance(expr, CallExpr):
                try:
                    self.check_call_types(expr)
                except LatticeTypeError as e:
                    self._record_error(e)
                for arg in expr.args:
                    visit_expr(arg)
            elif isinstance(expr, BinaryExpr):
                visit_expr(expr.left)
                visit_expr(expr.right)
            elif isinstance(expr, UnaryExpr):
                visit_expr(expr.operand)
            elif isinstance(expr, IndexExpr):
                visit_expr(expr.expr)
                visit_expr(expr.index)
            elif isinstance(expr, FieldExpr):
                visit_expr(expr.expr)
            elif isinstance(expr, ListLiteral):
                for el in expr.elements:
                    visit_expr(el)
            elif isinstance(expr, StringLiteral):
                pass

        def visit_stmt(stmt):
            if isinstance(stmt, VarDecl):
                visit_expr(stmt.value)
            elif isinstance(stmt, Assign):
                visit_expr(stmt.target)
                visit_expr(stmt.value)
            elif isinstance(stmt, IfStmt):
                visit_expr(stmt.cond)
                for s in stmt.then_branch:
                    visit_stmt(s)
                for s in stmt.else_branch:
                    visit_stmt(s)
            elif isinstance(stmt, ForStmt):
                visit_expr(stmt.start)
                visit_expr(stmt.end)
                for s in stmt.body:
                    visit_stmt(s)
            elif isinstance(stmt, MatchStmt):
                visit_expr(stmt.expr)
                for case in stmt.cases:
                    for s in case.body:
                        visit_stmt(s)
            elif isinstance(stmt, ReturnStmt):
                if stmt.expr:
                    visit_expr(stmt.expr)
            elif isinstance(stmt, ExprStmt):
                visit_expr(stmt.expr)

        for stmt in body:
            visit_stmt(stmt)

    def resolve_func_body(self, func):
        self.current_func = func
        self.locals = {}
        self.const_locals = set()
        self.immutable_locals = set()
        self.local_offset = 0
        self.max_local_offset = 0
        original_ret_type = func.ret_type
        self.inferred_bindings[func.name] = set()
        
        # Build local generic map for function's own generic parameters.
        # Size generics stay unresolved during body resolution and are inferred at call sites.
        local_generic_map = {}
        bound = getattr(func, 'monomorph_generics', None) or {}
        if func.generics:
            for gname, gtype_expr in func.generics:
                gtype_name = gtype_expr.name if hasattr(gtype_expr, 'name') else str(gtype_expr)
                if gname in bound:
                    local_generic_map[gname] = bound[gname]
                elif gtype_name == 'Type':
                    local_generic_map[gname] = self.types['Integer']
                else:
                    local_generic_map[gname] = None
        self.current_generic_map = local_generic_map
        
        self.infer_untyped_parameters(func)

        self.string_consts = {}
        self._collect_string_consts(func.body)
        self._fold_body_exprs(func.body)
        
        self.current_func_has_io = False
        
        # Add parameters to locals (they are laid out at the bottom of stack frame).
        # main() receives untrusted external arguments: every parameter must be
        # Input[T]. The runtime parses each CLI argument into T, producing Some on
        # success or None on failure, and the program must match to unwrap. Each
        # Input value is written into static memory before main runs.
        if func.name == 'main':
            # Body may be resolved more than once; rebuild the arg list each time.
            self.main_input_args = []
        for param in func.params:
            ptype = self.resolve_type_expr(param.type_expr)
            if func.name == 'main':
                inner = unwrap_input_inner(ptype)
                if inner is None:
                    self._record_error(LatticeTypeError(
                        f"Parameter '{param.name}' of main must be Input[T], got {format_type(ptype)}",
                        func.line,
                        hint=(
                            "Command-line arguments are untrusted external input. Declare "
                            "the parameter as Input[T] and match on Some/None, e.g. "
                            "main(x: Input[Integer]) { match (x) { Some(v) => {...} None => {...} } }"
                        ),
                    ))
                    self.locals[param.name] = (ptype, self.local_offset)
                    self.local_offset += ptype.get_size(self)
                    self.immutable_locals.add(param.name)
                    continue
                offset = self.global_offset
                self.global_offset += ptype.get_size(self)
                self.memory_locals[param.name] = (ptype, offset)
                self.main_input_args.append(
                    {
                        'name': param.name,
                        'addr': offset,
                        'inner_type': inner,
                    }
                )
            else:
                self.locals[param.name] = (ptype, self.local_offset)
                self.local_offset += ptype.get_size(self)
            self.immutable_locals.add(param.name)

        if func.ret_type:
            declared_ret = self.resolve_type_expr(func.ret_type)
            if isinstance(declared_ret, IOResolvedType):
                self.current_func_has_io = True

        if not self.current_func_has_io:
            self.current_func_has_io = self._body_has_io(func.body)

        if func.ret_type:
            declared_ret = self.resolve_type_expr(func.ret_type)
            if self.current_func_has_io and not isinstance(declared_ret, IOResolvedType):
                self._record_error(LatticeTypeError(
                    f"Function '{func.name}' performs IO but is declared to return {format_type(declared_ret)}",
                    func.line,
                    hint=f"Change the return type to IO[{format_type(declared_ret)}], or remove host interactions from the body.",
                ))
            elif not self.current_func_has_io and isinstance(declared_ret, IOResolvedType):
                self._record_error(LatticeTypeError(
                    f"Function '{func.name}' is declared to return {format_type(declared_ret)} but has no IO effects",
                    func.line,
                    hint="Remove the IO return type, or add host interactions such as print/read in the body.",
                ))
            
        # Parse statements to calculate local offsets
        for stmt in func.body:
            self.resolve_statement(stmt)

        self.check_calls_in_body(func.body)
            
        try:
            self.infer_return_type(func)
            if func.ret_type is not None and original_ret_type is None:
                self.inferred_return_types.add(func.name)
        except LatticeTypeError as e:
            self._record_error(e)
        self.max_local_offset = self.local_offset
        self.func_locals[ensure_mangled_name(func)] = self.locals
        self.current_generic_map = {}
        self.current_func = None

    def infer_return_type(self, func):
        has_io = self.current_func_has_io or self._body_has_io(func.body)
        inner_ret = self.types['void']
        return_types = []

        def find_returns(stmts):
            for s in stmts:
                if isinstance(s, ReturnStmt) and s.expr:
                    try:
                        t = self.infer_expr_type(s.expr)
                        if t:
                            return_types.append(self._unwrap_io_type(t))
                    except Exception:
                        pass
                elif isinstance(s, IfStmt):
                    find_returns(s.then_branch)
                    find_returns(s.else_branch)
                elif isinstance(s, ForStmt):
                    find_returns(s.body)
                elif isinstance(s, MatchStmt):
                    for case in s.cases:
                        find_returns(case.body)

        find_returns(func.body)
        if return_types:
            inner_ret = return_types[0]

        if func.ret_type is None:
            if has_io:
                func.ret_type = TypeExpr('IO', [self.resolved_to_type_expr(inner_ret, func.line)], func.line)
            else:
                func.ret_type = self.resolved_to_type_expr(inner_ret, func.line)
        elif has_io:
            declared = self.resolve_type_expr(func.ret_type)
            if isinstance(declared, IOResolvedType):
                for ret_t in return_types:
                    try:
                        self.types_compatible(declared.inner, ret_t, func.line)
                    except LatticeTypeError as e:
                        raise LatticeTypeError(
                            f"Return type of '{func.name}': {e.message}",
                            func.line,
                            hint=e.hint,
                        ) from e

    def resolve_statement(self, stmt):
        try:
            self._resolve_statement(stmt)
        except LatticeTypeError as e:
            self._record_error(e)

    def _resolve_statement(self, stmt):
        if isinstance(stmt, VarDecl):
            stmt.value = self._lower_expr(stmt.value)

            if isinstance(stmt.value, MapLiteral):
                if not stmt.value.entries:
                    raise LatticeTypeError("Map literal cannot be empty", stmt.line)
                value_type = self.infer_expr_type(
                    self._lower_expr(stmt.value.entries[0][1])
                )
                entries = []
                for key, val in stmt.value.entries:
                    lowered = self._lower_expr(val)
                    val_t = self.infer_expr_type(lowered)
                    self.types_compatible(value_type, val_t, stmt.line)
                    entries.append((key, lowered))
                map_type = StaticMapResolvedType(value_type, entries)
                self.static_map_bindings[stmt.name] = map_type
                self.locals[stmt.name] = (map_type, 0)
                self.immutable_locals.add(stmt.name)
                if stmt.is_const:
                    self.const_locals.add(stmt.name)
                return

            if isinstance(stmt.value, StringLiteral):
                cap = self._string_capacity_from_type_expr(stmt.type_expr)
                if cap is None:
                    cap = len(stmt.value.value)
                stmt.value = lower_string_literal(stmt.value, cap)

            if self._is_input_call(stmt.value) and not stmt.type_expr:
                raise LatticeTypeError(
                    "Cannot infer the result type of input(...)",
                    stmt.line,
                    hint=_input_hint(),
                )

            if self._is_read_file_call(stmt.value) and not stmt.type_expr:
                raise LatticeTypeError(
                    "Cannot infer generic parameters for read_file(...)",
                    stmt.line,
                    hint=_read_file_hint(),
                )

            if self._is_http_get_call(stmt.value) and not stmt.type_expr:
                raise LatticeTypeError(
                    "Cannot infer generic parameters for http_get(...)",
                    stmt.line,
                    hint=_http_get_hint(),
                )

            prev_expected = self._input_expected_type
            prev_read_file_expected = self._read_file_expected_type
            prev_http_get_expected = self._http_get_expected_type
            if self._is_input_call(stmt.value):
                expected = self.resolve_type_expr(stmt.type_expr)
                self._input_expected_type = expected
                self._register_input_call(stmt.value, expected, stmt.type_expr)

            if self._is_read_file_call(stmt.value):
                expected = self.resolve_type_expr(stmt.type_expr)
                self._read_file_expected_type = expected
                self._register_read_file_call(stmt.value, expected, stmt.type_expr)
                max_len, path_len = self._infer_read_file_generics(
                    expected, stmt.value.args[0], stmt.line
                )
                self._ensure_read_file_generics(stmt.value, max_len, path_len)

            if self._is_http_get_call(stmt.value):
                expected = self.resolve_type_expr(stmt.type_expr)
                self._http_get_expected_type = expected
                self._register_http_get_call(stmt.value, expected, stmt.type_expr)

            try:
                raw_value_t = self.infer_expr_type(stmt.value)
            finally:
                self._input_expected_type = prev_expected
                self._read_file_expected_type = prev_read_file_expected
                self._http_get_expected_type = prev_http_get_expected

            if isinstance(raw_value_t, IOResolvedType):
                self._require_io_allowed(stmt.line, "IO binding")
                bound_value_t = raw_value_t.inner
            else:
                bound_value_t = raw_value_t
            if stmt.type_expr:
                try:
                    t = self.resolve_type_expr(stmt.type_expr)
                except LatticeTypeError:
                    # For local bindings, permit a bare `String` annotation when the
                    # initializer fully determines the capacity (string literals
                    # and concatenations).
                    #
                    # We still reject `Input[String]` (external/CLI-bound strings)
                    # because that size cannot be derived at compile time.
                    te = stmt.type_expr
                    is_bare_string = (
                        getattr(te, "name", None) == "String"
                        and getattr(te, "size", None) is None
                        and not getattr(te, "args", None)
                    )
                    is_sized_string_val = (
                        isinstance(bound_value_t, StringResolvedType)
                        and bound_value_t.max_len is not None
                        or (
                            isinstance(bound_value_t, StructResolvedType)
                            and self._struct_string_max_len(bound_value_t) is not None
                        )
                    )
                    if is_bare_string and is_sized_string_val:
                        t = bound_value_t
                    else:
                        raise
            else:
                t = bound_value_t
            if not stmt.type_expr and self.current_func:
                self.inferred_bindings[self.current_func.name].add(stmt.name)
            if stmt.type_expr:
                try:
                    self.types_compatible(t, bound_value_t, stmt.line)
                except LatticeTypeError as e:
                    raise LatticeTypeError(
                        f"Variable '{stmt.name}': {e.message}",
                        stmt.line,
                        hint=e.hint,
                    ) from e
            # Allocate local storage
            self.locals[stmt.name] = (t, self.local_offset)
            self.immutable_locals.add(stmt.name)
            if stmt.is_const:
                self.const_locals.add(stmt.name)
            self.local_offset += t.get_size(self)
            
        elif isinstance(stmt, IfStmt):
            # Branches reuse local offsets since their stack frames are mutually exclusive!
            orig_offset = self.local_offset
            for s in stmt.then_branch:
                self.resolve_statement(s)
            then_max = self.local_offset
            
            self.local_offset = orig_offset
            for s in stmt.else_branch:
                self.resolve_statement(s)
            else_max = self.local_offset
            
            self.local_offset = max(then_max, else_max)
            
        elif isinstance(stmt, ForStmt):
            orig_offset = self.local_offset
            # Loop counter index variable
            self.locals[stmt.var_name] = (self.types['Integer'], self.local_offset)
            self.immutable_locals.add(stmt.var_name)
            if self.current_func:
                self.inferred_bindings[self.current_func.name].add(stmt.var_name)
            self.local_offset += 4
            for s in stmt.body:
                self.resolve_statement(s)
            self.local_offset = orig_offset # Loop scope ends
            
        elif isinstance(stmt, MatchStmt):
            orig_offset = self.local_offset
            max_case_offset = orig_offset
            match_type = self.infer_expr_type(stmt.expr)
            for case in stmt.cases:
                self.local_offset = orig_offset
                case_type = match_type
                if case.pattern.type_bind:
                    case_type = self.resolve_type_expr(case.pattern.type_bind)
                elif isinstance(match_type, UnionResolvedType):
                    for variant in match_type.variants:
                        vname = getattr(variant, 'name', '')
                        if vname == case.pattern.name or vname.startswith(f"{case.pattern.name}_"):
                            if isinstance(variant, StructResolvedType) and variant.fields:
                                case_type = variant.fields[0][1]
                            break
                for arg in case.pattern.args:
                    self.locals[arg] = (case_type, self.local_offset)
                    self.immutable_locals.add(arg)
                    self.local_offset += case_type.get_size(self)
                for s in case.body:
                    self.resolve_statement(s)
                max_case_offset = max(max_case_offset, self.local_offset)
            self.local_offset = max_case_offset
            
        elif isinstance(stmt, Assign):
            if isinstance(stmt.target, Identifier) and stmt.target.name in self.immutable_locals:
                raise LatticeTypeError(
                    f"Cannot assign to '{stmt.target.name}' — variables may only be assigned once",
                    stmt.line,
                    hint="Declare a new let binding for the updated value.",
                )
            target_t = self.infer_expr_type(stmt.target)
            value_t = self.infer_expr_type(stmt.value)
            self.types_compatible(target_t, value_t, stmt.line)
            
        elif isinstance(stmt, ReturnStmt):
            if stmt.expr:
                ret_t = self.infer_expr_type(stmt.expr)
                if isinstance(ret_t, IOResolvedType):
                    self._require_io_allowed(stmt.line, "IO action in return")
                if self.current_func and self.current_func.ret_type:
                    expected = self.resolve_type_expr(self.current_func.ret_type)
                    if isinstance(expected, IOResolvedType):
                        expected = expected.inner
                    try:
                        self.types_compatible(expected, self._unwrap_io_type(ret_t), stmt.line)
                    except LatticeTypeError as e:
                        raise LatticeTypeError(
                            f"Return type of '{self.current_func.name}': {e.message}",
                            stmt.line,
                            hint=e.hint,
                        ) from e
                
        elif isinstance(stmt, ExprStmt):
            expr_t = self.infer_expr_type(stmt.expr)
            if isinstance(expr_t, IOResolvedType):
                self._require_io_allowed(stmt.line, "IO statement")

    def infer_untyped_parameters(self, func):
        untyped_params = [p for p in func.params if p.type_expr is None]
        if not untyped_params:
            return

        inferred_types = {p.name: [] for p in untyped_params}

        def add_candidate(name, type_expr):
            if name in inferred_types and type_expr is not None:
                inferred_types[name].append(type_expr)

        def visit_stmt(stmt):
            if isinstance(stmt, VarDecl):
                if isinstance(stmt.value, Identifier):
                    if stmt.type_expr:
                        add_candidate(stmt.value.name, stmt.type_expr)
                visit_expr(stmt.value)
            elif isinstance(stmt, Assign):
                visit_expr(stmt.target)
                visit_expr(stmt.value)
            elif isinstance(stmt, IfStmt):
                visit_expr(stmt.cond)
                for s in stmt.then_branch: visit_stmt(s)
                for s in stmt.else_branch: visit_stmt(s)
            elif isinstance(stmt, ForStmt):
                visit_expr(stmt.start)
                visit_expr(stmt.end)
                for s in stmt.body: visit_stmt(s)
            elif isinstance(stmt, MatchStmt):
                visit_expr(stmt.expr)
                for case in stmt.cases:
                    for s in case.body: visit_stmt(s)
            elif isinstance(stmt, ReturnStmt):
                if stmt.expr:
                    if isinstance(stmt.expr, Identifier) and func.ret_type:
                        add_candidate(stmt.expr.name, func.ret_type)
                    visit_expr(stmt.expr)
            elif isinstance(stmt, ExprStmt):
                visit_expr(stmt.expr)

        def visit_expr(expr):
            if isinstance(expr, CallExpr):
                if isinstance(expr.func, Identifier):
                    callee_name = expr.func.name
                    if get_overloads(self.functions, callee_name):
                        overloads = get_overloads(self.functions, callee_name)
                        callee_decl = None
                        for cand in overloads:
                            if len(cand.params) == len(expr.args):
                                callee_decl = cand
                                break
                        if callee_decl is None and overloads:
                            callee_decl = overloads[0]
                        if callee_decl is not None:
                            call_generic_map = {}
                            if callee_decl.generics:
                                for gname, _ in callee_decl.generics:
                                    call_generic_map[gname] = None
                                for idx, arg in enumerate(expr.args):
                                    if idx < len(callee_decl.params):
                                        is_untyped = False
                                        if isinstance(arg, Identifier):
                                            for p in func.params:
                                                if p.name == arg.name and p.type_expr is None:
                                                    is_untyped = True
                                                    break
                                        if not is_untyped:
                                            try:
                                                arg_t = self.infer_expr_type(arg, generic_map)
                                                self.infer_call_generics(callee_decl.params[idx].type_expr, arg_t, call_generic_map)
                                            except Exception:
                                                pass
                            for idx, arg in enumerate(expr.args):
                                if isinstance(arg, Identifier) and idx < len(callee_decl.params):
                                    param_decl = callee_decl.params[idx]
                                    if param_decl.type_expr:
                                        resolved_t = self.resolve_type_expr(param_decl.type_expr, call_generic_map)
                                        if resolved_t:
                                            add_candidate(arg.name, self.resolved_to_type_expr(resolved_t, expr.line))
                    elif callee_name in self.type_decls:
                        decl = self.type_decls[callee_name]
                        call_generic_map = {}
                        if decl.generics:
                            for gname, _ in decl.generics:
                                call_generic_map[gname] = None
                            for idx, arg in enumerate(expr.args):
                                if idx < len(decl.fields):
                                    is_untyped = False
                                    if isinstance(arg, Identifier):
                                        for p in func.params:
                                            if p.name == arg.name and p.type_expr is None:
                                                is_untyped = True
                                                break
                                    if not is_untyped:
                                        try:
                                            arg_t = self.infer_expr_type(arg, generic_map)
                                            field_t_expr = decl.fields[idx][1]
                                            self.infer_call_generics(field_t_expr, arg_t, call_generic_map)
                                        except Exception:
                                            pass
                        for idx, arg in enumerate(expr.args):
                            if isinstance(arg, Identifier) and idx < len(decl.fields):
                                field_type_expr = decl.fields[idx][1]
                                if field_type_expr:
                                    resolved_t = self.resolve_type_expr(field_type_expr, call_generic_map)
                                    if resolved_t:
                                        add_candidate(arg.name, self.resolved_to_type_expr(resolved_t, expr.line))
                for arg in expr.args:
                    visit_expr(arg)
            elif isinstance(expr, BinaryExpr):
                if isinstance(expr.left, Identifier) and isinstance(expr.right, Literal):
                    if expr.right.val_type:
                        add_candidate(expr.left.name, TypeExpr(expr.right.val_type, [], expr.line))
                elif isinstance(expr.right, Identifier) and isinstance(expr.left, Literal):
                    if expr.left.val_type:
                        add_candidate(expr.right.name, TypeExpr(expr.left.val_type, [], expr.line))
                visit_expr(expr.left)
                visit_expr(expr.right)
            elif isinstance(expr, UnaryExpr):
                visit_expr(expr.operand)
            elif isinstance(expr, IndexExpr):
                visit_expr(expr.expr)
                visit_expr(expr.index)
            elif isinstance(expr, FieldExpr):
                visit_expr(expr.expr)

        for stmt in func.body:
            visit_stmt(stmt)

        for param in untyped_params:
            candidates = inferred_types[param.name]
            if candidates:
                refined = [c for c in candidates if getattr(c, 'constraint', None) is not None]
                if refined:
                    chosen = refined[0]
                else:
                    chosen = candidates[0]
                param.type_expr = clone_ast_node(chosen)
            else:
                param.type_expr = TypeExpr('Integer', [], func.line)
            self.inferred_bindings.setdefault(func.name, set()).add(param.name)


