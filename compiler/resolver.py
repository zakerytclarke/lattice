# Lattice Type Inference & Memory Resolution

import sys
from compiler.parser import *

class LatticeTypeError(Exception):
    def __init__(self, msg, line):
        super().__init__(f"Type Error at line {line}: {msg}")
        self.line = line

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
        return f"List[{self.length}, {self.elem_type}]"
    def get_size(self, resolver):
        return self.length * self.elem_type.get_size(resolver)

class StringResolvedType(ResolvedType):
    def __init__(self, max_len):
        self.max_len = max_len
    def __repr__(self):
        return f"String[{self.max_len}]"
    def get_size(self, resolver):
        # max_len chars + 4 bytes for current length field
        return (self.max_len * 4) + 4

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
        self.functions = {
            'print_int': FuncDecl('external', 'print_int', [], [Param('val', TypeExpr('Integer', [], 0), 0)], TypeExpr('IO', [TypeExpr('void', [], 0)], 0), [], [], 0)
        }
        self.type_decls = {}
        self.func_locals = {}
        self.globals = {} # name -> (ResolvedType, offset)
        self.global_offset = 1024 # start allocating static globals after offset 1024 (reserve bottom for stack / initial stuff)
        self.current_generic_map = {}
        
        # Local environments
        self.locals = {} # name -> (ResolvedType, offset)
        self.local_offset = 0
        self.max_local_offset = 0

    def register_type(self, name, resolved_type):
        self.types[name] = resolved_type

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
        # Substitute generic parameters if they are in the generic map
        if name in generic_map:
            val = generic_map[name]
            if val is None:
                return None
            if isinstance(val, ResolvedType):
                return val
            # If it's a number/value, it might parameterize a type
            name = str(val)

        if name == 'List':
            # List[Length, ElemType]
            if len(te.args) != 2:
                raise LatticeTypeError("List type requires 2 arguments: List[Len, Elem]", te.line)
            
            # Resolve length
            len_arg = te.args[0]
            length = self.evaluate_constant(len_arg, generic_map)
            
            # Resolve element type
            elem_type = self.resolve_type_expr(te.args[1], generic_map)
            return ListResolvedType(length, elem_type)

        if name == 'Group':
            if len(te.args) != 2:
                raise LatticeTypeError("Group type requires 2 arguments", te.line)
            length = self.evaluate_constant(te.args[0], generic_map)
            elem_type = self.resolve_type_expr(te.args[1], generic_map)
            return ListResolvedType(length, elem_type)


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
                # Map generic parameters to arguments
                local_generic_map = {}
                for i, (gname, gtype) in enumerate(decl.generics):
                    if i < len(te.args):
                        arg_val = te.args[i]
                        gtype_name = gtype.name if hasattr(gtype, 'name') else str(gtype)
                        if gtype_name == 'Type':
                            local_generic_map[gname] = self.resolve_type_expr(arg_val, generic_map)
                        else:
                            local_generic_map[gname] = self.evaluate_constant(arg_val, generic_map)
                
                # Build monomorphized name
                arg_names = []
                for gname, _ in decl.generics:
                    val = local_generic_map.get(gname)
                    if val is None:
                        val = self.types.get('Integer')
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

        raise LatticeTypeError(f"Undefined type '{name}'", te.line)

    def evaluate_constant(self, expr, generic_map=None):
        generic_map = generic_map or self.current_generic_map
        if isinstance(expr, Literal) and expr.val_type == 'Integer':
            return expr.value
        if isinstance(expr, Identifier):
            if expr.name in generic_map:
                val = generic_map[expr.name]
                if isinstance(val, int):
                    return val
            raise LatticeTypeError(f"Unbounded generic constant '{expr.name}' at compile-time", expr.line)
        if isinstance(expr, TypeExpr):
            # Check if it was resolved to a number in generics
            if expr.name in generic_map:
                return generic_map[expr.name]
        raise LatticeTypeError("Expected compile-time integer constant", expr.line)

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
            return TypeExpr('List', [Literal(r_t.length, 'Integer', line), self.resolved_to_type_expr(r_t.elem_type, line)], line)
        return TypeExpr('Integer', [], line)

    def infer_call_generics(self, param_type, arg_type, call_generic_map):
        if not param_type or not arg_type:
            return
        if isinstance(param_type, TypeExpr):
            if param_type.name in call_generic_map:
                call_generic_map[param_type.name] = arg_type
                return
            
            if isinstance(arg_type, ListResolvedType) and param_type.name in ['List', 'Group']:
                if len(param_type.args) == 2:
                    len_arg = param_type.args[0]
                    if isinstance(len_arg, TypeExpr) and len_arg.name in call_generic_map:
                        call_generic_map[len_arg.name] = arg_type.length
                    elem_arg = param_type.args[1]
                    if isinstance(elem_arg, TypeExpr) and elem_arg.name in call_generic_map:
                        call_generic_map[elem_arg.name] = arg_type.elem_type
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

    def infer_expr_type(self, expr, generic_map=None):
        generic_map = generic_map or self.current_generic_map
        
        if isinstance(expr, Literal):
            return self.types[expr.val_type]
            
        if isinstance(expr, Identifier):
            # 1. Check local variable
            if expr.name in self.locals:
                return self.locals[expr.name][0]
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
            raise LatticeTypeError(f"Undefined variable '{expr.name}'", expr.line)

        if isinstance(expr, BinaryExpr):
            left_t = self.infer_expr_type(expr.left, generic_map)
            right_t = self.infer_expr_type(expr.right, generic_map)
            
            # Simple inference: promote Int and Rational to Rational
            if left_t.name == 'Integer' and right_t.name == 'Integer':
                if expr.op == '/':
                    return self.types['Integer'] # Default to Integer division, can promote later
                if expr.op in ['==', '!=', '<', '>', '<=', '>=']:
                    return self.types['Bool']
                return self.types['Integer']
                
            if 'Rational' in [left_t.name, right_t.name]:
                if expr.op in ['==', '!=', '<', '>', '<=', '>=']:
                    return self.types['Bool']
                return self.types['Rational']
                
            if expr.op in ['&&', '||']:
                return self.types['Bool']
                
            return left_t

        if isinstance(expr, ListLiteral):
            if not expr.elements:
                raise LatticeTypeError("Empty list literals are unconstrained. Specify bounds.", expr.line)
            first_t = self.infer_expr_type(expr.elements[0], generic_map)
            for el in expr.elements[1:]:
                elt = self.infer_expr_type(el, generic_map)
                if elt.name != first_t.name:
                    raise LatticeTypeError(f"Type mismatch in list elements: expected {first_t}, got {elt}", expr.line)
            te = TypeExpr('List', [Literal(len(expr.elements), 'Integer', expr.line), TypeExpr(first_t.name, [], expr.line)], expr.line)
            return self.resolve_type_expr(te, generic_map)

        if isinstance(expr, IndexExpr):
            arr_t = self.infer_expr_type(expr.expr, generic_map)
            if isinstance(arr_t, ListResolvedType):
                return arr_t.elem_type
            raise LatticeTypeError("Index operation only valid on List types", expr.line)

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
                
            if func_name not in self.functions:
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
                                    else:
                                        # Default mock
                                        local_generic_map[gname] = self.types['Integer']
                            
                            arg_names = []
                            for gname, _ in decl.generics:
                                val = local_generic_map.get(gname, PrimitiveResolvedType('void'))
                                arg_names.append(str(val))
                            mono_name = f"{func_name}_{'_'.join(arg_names)}"
                            if mono_name in self.types:
                                return self.types[mono_name]
                            te = TypeExpr(func_name, [self.resolved_to_type_expr(t, expr.line) for t in local_generic_map.values()], expr.line)
                            return self.resolve_type_expr(te, generic_map)
                    return self.resolve_type_expr(TypeExpr(func_name, [], expr.line), generic_map)
                raise LatticeTypeError(f"Undefined function '{func_name}'", expr.line)
                
            f_decl = self.functions[func_name]
            
            # Simple generic resolution logic
            call_generic_map = {}
            if f_decl.generics:
                for gname, _ in f_decl.generics:
                    call_generic_map[gname] = None
                if expr.generic_args:
                    for i, (gname, _) in enumerate(f_decl.generics):
                        if i < len(expr.generic_args):
                            call_generic_map[gname] = expr.generic_args[i]
                else:
                    for idx, param in enumerate(f_decl.params):
                        if idx < len(expr.args):
                            arg_t = self.infer_expr_type(expr.args[idx], generic_map)
                            self.infer_call_generics(param.type_expr, arg_t, call_generic_map)
                
                for gname, gtype_expr in f_decl.generics:
                    if call_generic_map[gname] is None:
                        gtype_name = gtype_expr.name if hasattr(gtype_expr, 'name') else str(gtype_expr)
                        if gtype_name == 'Type':
                            call_generic_map[gname] = self.types['Integer']
                        else:
                            call_generic_map[gname] = 100
            
            # Infer return type with solved generics
            if f_decl.ret_type:
                return self.resolve_type_expr(f_decl.ret_type, call_generic_map)
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
                if decl.name in self.functions:
                    raise LatticeTypeError(f"Redefinition of function '{decl.name}'", decl.line)
                self.functions[decl.name] = decl

        # 2. Second Pass: Populate field types and union variants for non-generic types
        for decl in prog.decls:
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

        # 3. Third Pass: Perform memory mapping for global & local variables
        for decl in prog.decls:
            if isinstance(decl, FuncDecl):
                self.resolve_func_body(decl)

    def resolve_func_body(self, func):
        self.locals = {}
        self.local_offset = 0
        self.max_local_offset = 0
        
        # Build local generic map for function's own generic parameters
        local_generic_map = {}
        if func.generics:
            for gname, gtype_expr in func.generics:
                gtype_name = gtype_expr.name if hasattr(gtype_expr, 'name') else str(gtype_expr)
                if gtype_name == 'Type':
                    local_generic_map[gname] = self.types['Integer']
                else:
                    local_generic_map[gname] = 100
        self.current_generic_map = local_generic_map
        
        self.infer_untyped_parameters(func)
        
        # Add parameters to locals (they are laid out at the bottom of stack frame)
        for param in func.params:
            ptype = self.resolve_type_expr(param.type_expr)
            self.locals[param.name] = (ptype, self.local_offset)
            self.local_offset += ptype.get_size(self)
            
        # Parse statements to calculate local offsets
        for stmt in func.body:
            self.resolve_statement(stmt)
            
        self.infer_return_type(func)
        self.max_local_offset = self.local_offset
        self.func_locals[func.name] = self.locals
        self.current_generic_map = {}

    def infer_return_type(self, func):
        if func.ret_type is None:
            return_types = []
            def find_returns(stmts):
                for s in stmts:
                    if isinstance(s, ReturnStmt) and s.expr:
                        try:
                            t = self.infer_expr_type(s.expr)
                            if t:
                                return_types.append(t)
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
                first_ret = return_types[0]
                func.ret_type = TypeExpr(first_ret.name, [], func.line)

    def resolve_statement(self, stmt):
        if isinstance(stmt, VarDecl):
            t = self.resolve_type_expr(stmt.type_expr) if stmt.type_expr else self.infer_expr_type(stmt.value)
            # Allocate local storage
            self.locals[stmt.name] = (t, self.local_offset)
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
            self.local_offset += 4
            for s in stmt.body:
                self.resolve_statement(s)
            self.local_offset = orig_offset # Loop scope ends
            
        elif isinstance(stmt, MatchStmt):
            orig_offset = self.local_offset
            max_case_offset = orig_offset
            for case in stmt.cases:
                self.local_offset = orig_offset
                # Add binding parameters of pattern to local scope
                p_type = self.infer_expr_type(stmt.expr)
                if case.pattern.type_bind:
                    p_type = self.resolve_type_expr(case.pattern.type_bind)
                for arg in case.pattern.args:
                    self.locals[arg] = (p_type, self.local_offset)
                    self.local_offset += p_type.get_size(self)
                for s in case.body:
                    self.resolve_statement(s)
                max_case_offset = max(max_case_offset, self.local_offset)
            self.local_offset = max_case_offset
            
        elif isinstance(stmt, Assign):
            self.infer_expr_type(stmt.target)
            self.infer_expr_type(stmt.value)
            
        elif isinstance(stmt, ReturnStmt):
            if stmt.expr:
                self.infer_expr_type(stmt.expr)
                
        elif isinstance(stmt, ExprStmt):
            self.infer_expr_type(stmt.expr)

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
                    if callee_name in self.functions:
                        callee_decl = self.functions[callee_name]
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


