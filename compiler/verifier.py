# Lattice SMT Safety Verifier & Recursion Bounds Checker

import sys
import z3

from compiler.parser import *
from compiler.errors import (
    SafetyError,
    format_expr,
    format_type,
    generic_inference_hint,
    static_memory_hint,
)
from compiler.resolver import *
from compiler.overloads import get_overloads, resolve_call, ensure_mangled_name

class SMTVerifier:
    def __init__(self, resolver):
        self.resolver = resolver
        if 'z3' not in sys.modules:
            raise SafetyError("Z3 solver is required for safety verification", 0)
        self.solver = z3.Solver()
        self.variables = {}
        self.temp_count = 0
        self.current_func_name = None
        self.current_func_decl = None

    def _record_error(self, error):
        if self.resolver.diagnostics:
            self.resolver.diagnostics.add(
                error,
                file_name=self.resolver._current_file_name,
                source_lines=self.resolver._current_source_lines,
            )
        else:
            raise error

    def _require(self, z3_expr, description, line, hint=None):
        if z3_expr is None:
            raise SafetyError(
                f"Cannot verify {description}",
                line,
                hint=hint or "Rewrite using integer comparisons and arithmetic that the solver can reason about.",
            )
        return z3_expr

    def get_z3_var(self, name, resolved_type):
        if name in self.variables:
            return self.variables[name]
            
        if isinstance(resolved_type, RefinedResolvedType):
            var = self.get_z3_var(name, resolved_type.base_type)
            if resolved_type.constraint is not None:
                local_var_map = {resolved_type.constraint_var: var}
                local_type_map = {resolved_type.constraint_var: resolved_type.base_type}
                z3_constraint = self.translate_expr(
                    resolved_type.constraint, 
                    local_var_map=local_var_map, 
                    local_type_map=local_type_map
                )
                if z3_constraint is not None:
                    self.solver.add(z3_constraint)
            return var
            
        if isinstance(resolved_type, PrimitiveResolvedType):
            if resolved_type.name == 'Integer':
                var = z3.Int(name)
                self.variables[name] = var
                return var
            if resolved_type.name == 'Bool':
                var = z3.Bool(name)
                self.variables[name] = var
                return var
                
        if isinstance(resolved_type, StructResolvedType):
            fields_vars = {}
            fields_tuple = []
            for fname, ftype in resolved_type.fields:
                fvar = z3.Int(f"{name}_{fname}")
                fields_vars[fname] = fvar
                fields_tuple.append(fvar)
            
            if len(fields_tuple) == 0:
                val = z3.Int(name)
            else:
                val = tuple(fields_tuple) if len(fields_tuple) > 1 else fields_tuple[0]
            self.variables[name] = val
            
            if resolved_type.name == 'Rational':
                self.solver.add(fields_vars['Denominator'] != 0)
                
            if resolved_type.invariants:
                for inv in resolved_type.invariants:
                    z3_inv = self.translate_expr(
                        inv, 
                        generic_map=getattr(resolved_type, 'generic_map', None), 
                        local_var_map=fields_vars
                    )
                    if z3_inv is not None:
                        self.solver.add(z3_inv)
            return val
            
        return None

    def translate_expr(self, expr, generic_map=None, local_var_map=None, local_type_map=None):
        generic_map = generic_map or self.resolver.current_generic_map
        local_var_map = local_var_map or {}
        local_type_map = local_type_map or {}
        
        if isinstance(expr, Literal):
            if expr.val_type == 'Integer':
                return expr.value
            if expr.val_type == 'Bool':
                return expr.value
            if expr.val_type == 'Char':
                return expr.value
                
        if isinstance(expr, Identifier):
            if expr.name in local_var_map:
                return local_var_map[expr.name]
            # Check if it maps to generic arg
            if expr.name in generic_map:
                val = generic_map[expr.name]
                if isinstance(val, int):
                    return val
                if val is None:
                    return z3.Int(expr.name)
            # Local/Global variable
            if expr.name in self.resolver.locals:
                t = self.resolver.locals[expr.name][0]
                return self.get_z3_var(expr.name, t)
            if expr.name in self.resolver.globals:
                t = self.resolver.globals[expr.name][0]
                return self.get_z3_var(expr.name, t)
            return None

        if isinstance(expr, BinaryExpr):
            left = self.translate_expr(expr.left, generic_map, local_var_map, local_type_map)
            right = self.translate_expr(expr.right, generic_map, local_var_map, local_type_map)
            if left is None or right is None:
                return None
                
            if expr.op == '+': return left + right
            if expr.op == '-': return left - right
            if expr.op == '*': return left * right
            if expr.op == '/': return left / right
            if expr.op == '%': return left % right
            if expr.op == '==': return left == right
            if expr.op == '!=': return left != right
            if expr.op == '<': return left < right
            if expr.op == '>': return left > right
            if expr.op == '<=': return left <= right
            if expr.op == '>=': return left >= right
            if expr.op == '&&': return z3.And(left, right)
            if expr.op == '||': return z3.Or(left, right)
            if expr.op == '^': return z3.Xor(left, right)

        if isinstance(expr, UnaryExpr):
            operand = self.translate_expr(expr.operand, generic_map, local_var_map, local_type_map)
            if operand is None:
                return None
            if expr.op == '!':
                return z3.Not(operand)

        if isinstance(expr, FieldExpr):
            base = self.translate_expr(expr.expr, generic_map, local_var_map, local_type_map)
            if base is not None:
                struct_t = None
                if isinstance(expr.expr, Identifier) and expr.expr.name in local_type_map:
                    struct_t = local_type_map[expr.expr.name]
                else:
                    try:
                        struct_t = self.resolver.infer_expr_type(expr.expr)
                    except Exception:
                        pass
                
                if isinstance(struct_t, RefinedResolvedType):
                    struct_t = struct_t.base_type
                if isinstance(struct_t, StructResolvedType):
                    for idx, (fname, _) in enumerate(struct_t.fields):
                        if fname == expr.field:
                            if isinstance(base, tuple):
                                return base[idx]
                            return base
                    
        if isinstance(expr, CallExpr):
            if isinstance(expr.func, Identifier):
                if expr.func.name == 'len' and expr.args:
                    arg_t = self.resolver.infer_expr_type(expr.args[0], generic_map)
                    if isinstance(arg_t, ListResolvedType) and arg_t.length:
                        return arg_t.length
                    for gname, val in (generic_map or {}).items():
                        if val is None:
                            return z3.Int(gname)
            if expr.func.name in self.resolver.type_decls or expr.func.name in self.resolver.types:
                args_z3 = [self.translate_expr(arg, generic_map, local_var_map, local_type_map) for arg in expr.args]
                if any(a is None for a in args_z3):
                    return None
                if len(args_z3) == 0:
                    return z3.Int(f"null_constructor_{id(expr)}")
                return tuple(args_z3) if len(args_z3) > 1 else args_z3[0]
                
        if isinstance(expr, ListLiteral):
            return z3.Int(f"list_lit_{id(expr)}")
            
        return None

    def assert_constraint(self, expr, generic_map=None, line=0, required=True):
        z3_expr = self.translate_expr(expr, generic_map)
        if z3_expr is None:
            if required:
                raise SafetyError(
                    f"Cannot verify constraint '{expr}': expression is not translatable to SMT",
                    line,
                )
            return
        self.solver.add(z3_expr)

    def verify_bounds(self, arr_len, index_expr, line, generic_map=None):
        z3_index = self._require(
            self.translate_expr(index_expr, generic_map),
            "index expression for bounds check",
            line,
        )
            
        # We want to check if index is guaranteed to be in range: 0 <= index < arr_len.
        # So we prove: Constraints => (0 <= index < arr_len)
        # To do this, we check if: Constraints AND NOT (0 <= index < arr_len) is unsatisfiable.
        self.solver.push()
        self.solver.add(z3.Or(z3_index < 0, z3_index >= arr_len))
        res = self.solver.check()
        self.solver.pop()
        
        if res == z3.sat:
            model = self.solver.model()
            val = model.eval(z3_index)
            raise SafetyError(
                f"Out of bounds access: index may be {val} but valid range is 0..{arr_len - 1} (capacity {arr_len})",
                line,
                hint="Guard the index before access, e.g. if (idx >= 0 && idx < capacity) { ... }",
            )

    def verify_bounds_symbolic(self, index_expr, line, generic_map=None):
        z3_index = self._require(
            self.translate_expr(index_expr, generic_map),
            "index expression for symbolic bounds check",
            line,
        )
        z3_lengths = []
        for gname, val in self.resolver.current_generic_map.items():
            if val is None:
                z3_lengths.append(z3.Int(gname))
        if not z3_lengths:
            raise SafetyError(
                "Cannot verify index bounds: list length is unknown and no generic size parameter is in scope",
                line,
                hint=static_memory_hint(),
            )
        z3_len = z3_lengths[0]
        self.solver.push()
        self.solver.add(z3.Or(z3_index < 0, z3_index >= z3_len))
        res = self.solver.check()
        self.solver.pop()
        if res == z3.sat:
            model = self.solver.model()
            val = model.eval(z3_index)
            bound = model.eval(z3_len)
            raise SafetyError(
                f"Out of bounds access: index may be {val} but capacity is {bound}",
                line,
                hint="Guard the index before access, e.g. if (idx >= 0 && idx < len) { ... }",
            )

    def verify_program_safety(self, prog):
        for decl in prog.decls:
            if isinstance(decl, FuncDecl):
                try:
                    self.verify_function(decl)
                except SafetyError as e:
                    self._record_error(e)

    def verify_function(self, func):
        func_symbol = ensure_mangled_name(func)
        if func_symbol not in self.resolver.func_locals:
            return
        self.current_func_name = func.name
        self.current_func_decl = func
        
        # Build local generic map for function's own generic parameters
        local_generic_map = {}
        if func.generics:
            for gname, gtype_expr in func.generics:
                gtype_name = gtype_expr.name if hasattr(gtype_expr, 'name') else str(gtype_expr)
                if gtype_name == 'Type':
                    local_generic_map[gname] = self.resolver.types['Integer']
                else:
                    local_generic_map[gname] = None
        self.resolver.current_generic_map = local_generic_map
        
        self.resolver.locals = self.resolver.func_locals[ensure_mangled_name(func)]
        self.solver.push()
        
        # 1. Reset variable mappings for Z3
        self.variables = {}
        
        # Pre-populate parameters at root level
        for param in func.params:
            param_t = self.resolver.resolve_type_expr(param.type_expr)
            if param_t:
                self.get_z3_var(param.name, param_t)
        
        # 2. Assert server validation constraints / preconditions
        for constraint in func.constraints:
            self.assert_constraint(constraint, line=func.line)
            
        # 3. Check function statements for bounds checks and invariants
        for stmt in func.body:
            self.verify_statement(stmt)
            
        self.solver.pop()
        self.resolver.current_generic_map = {}

    def verify_statement(self, stmt):
        try:
            self._verify_statement(stmt)
        except SafetyError as e:
            self._record_error(e)

    def _verify_statement(self, stmt):
        if isinstance(stmt, VarDecl):
            if stmt.name not in self.resolver.locals:
                return
            # Verify expression contents first
            self.verify_expr_safety(stmt.value, stmt.line)
            # Assert variable equality if it can be represented
            z3_var = self.get_z3_var(stmt.name, self.resolver.locals[stmt.name][0])
            z3_val = self.translate_expr(stmt.value)
            if z3_var is not None and z3_val is not None:
                # If Rational, match fields
                if isinstance(z3_var, tuple):
                    if isinstance(z3_val, tuple):
                        for idx_tuple in range(min(len(z3_var), len(z3_val))):
                            self.solver.add(z3_var[idx_tuple] == z3_val[idx_tuple])
                else:
                    self.solver.add(z3_var == z3_val)
            
        elif isinstance(stmt, Assign):
            # If target is indexing or field, check safety
            self.verify_expr_safety(stmt.target, stmt.line)
            self.verify_expr_safety(stmt.value, stmt.line)
            
        elif isinstance(stmt, IfStmt):
            self.verify_expr_safety(stmt.cond, stmt.line)
            
            # Verify then branch
            self.solver.push()
            self.assert_constraint(stmt.cond, line=stmt.line, required=False)
            for s in stmt.then_branch:
                self.verify_statement(s)
            self.solver.pop()
            
            # Verify else branch
            self.solver.push()
            z3_cond = self.translate_expr(stmt.cond)
            if z3_cond is not None:
                self.solver.add(z3.Not(z3_cond))
            for s in stmt.else_branch:
                self.verify_statement(s)
            self.solver.pop()
            
        elif isinstance(stmt, ForStmt):
            # Loop verification: loop index starts at start, ends at end
            z3_index = self.get_z3_var(stmt.var_name, self.resolver.types['Integer'])
            z3_start = self.translate_expr(stmt.start)
            z3_end = self.translate_expr(stmt.end)
            
            # Inside loop body, we assume index satisfies: start <= index < end
            self.solver.push()
            if z3_index is not None:
                if z3_start is not None:
                    self.solver.add(z3_index >= z3_start)
                if z3_end is not None:
                    self.solver.add(z3_index < z3_end)
            for s in stmt.body:
                self.verify_statement(s)
            self.solver.pop()
            
        elif isinstance(stmt, MatchStmt):
            # Verify match cases
            p_type = self.resolver.infer_expr_type(stmt.expr)
            self.verify_expr_safety(stmt.expr, stmt.line)
            
            # Exhaustiveness Check
            if isinstance(p_type, UnionResolvedType):
                variants_matched = set()
                for case in stmt.cases:
                    variants_matched.add(case.pattern.name)

                def pattern_base_name(type_name):
                    if '_' in type_name:
                        return type_name.split('_')[0]
                    return type_name

                expected = {
                    pattern_base_name(v.name)
                    for v in p_type.variants
                    if hasattr(v, 'name')
                }
                missing = expected - variants_matched
                if missing:
                    raise SafetyError(
                        f"Non-exhaustive match: missing case(s) {sorted(missing)}",
                        stmt.line,
                        hint=f"Handle every variant of {format_type(p_type)}.",
                    )
            
            # Verify case bodies
            for case in stmt.cases:
                self.solver.push()
                # Assert matching pattern constraint (e.g. Some vs None)
                for s in case.body:
                    self.verify_statement(s)
                self.solver.pop()
                
        elif isinstance(stmt, ReturnStmt):
            if stmt.expr:
                self.verify_expr_safety(stmt.expr, stmt.line)
                
                # Check return type constraint
                func_decl = self.current_func_decl
                if func_decl.ret_type:
                    ret_t = self.resolver.resolve_type_expr(func_decl.ret_type)
                    if isinstance(ret_t, IOResolvedType):
                        ret_t = ret_t.inner
                    if isinstance(ret_t, RefinedResolvedType):
                        z3_ret = self._require(
                            self.translate_expr(stmt.expr),
                            "return value for refined return type check",
                            stmt.line,
                        )
                        z3_constraint = self._require(
                            self.translate_expr(
                                ret_t.constraint,
                                local_var_map={ret_t.constraint_var: z3_ret},
                                local_type_map={ret_t.constraint_var: ret_t.base_type},
                            ),
                            f"return type constraint for '{ret_t}'",
                            stmt.line,
                        )
                        self.solver.push()
                        self.solver.add(z3.Not(z3_constraint))
                        res = self.solver.check()
                        self.solver.pop()
                        if res == z3.sat:
                            raise SafetyError(
                                f"Return value does not satisfy type constraint {format_type(ret_t)}",
                                stmt.line,
                                hint="Adjust the returned expression or narrow the return type constraint.",
                            )
                
        elif isinstance(stmt, ExprStmt):
            self.verify_expr_safety(stmt.expr, stmt.line)

    def simulate_termination(self, func, start_val, max_steps=200):
        if not func.params:
            return False
        param_name = func.params[0].name
        current_val = start_val
        visited = set()
        
        for step in range(max_steps):
            if current_val in visited:
                return False
            visited.add(current_val)
            
            next_val = None
            terminated = True
            
            def eval_expr(expr):
                if isinstance(expr, Literal):
                    return expr.value
                if isinstance(expr, Identifier):
                    if expr.name == param_name:
                        return current_val
                if isinstance(expr, BinaryExpr):
                    l = eval_expr(expr.left)
                    r = eval_expr(expr.right)
                    if l is None or r is None: return None
                    if expr.op == '+': return l + r
                    if expr.op == '-': return l - r
                    if expr.op == '*': return l * r
                    if expr.op == '/': return l // r
                    if expr.op == '==': return l == r
                    if expr.op == '!=': return l != r
                    if expr.op == '<': return l < r
                    if expr.op == '>': return l > r
                    if expr.op == '<=': return l <= r
                    if expr.op == '>=': return l >= r
                if isinstance(expr, FieldExpr):
                    if expr.field == 'value':
                        return eval_expr(expr.expr)
                return None
                
            def find_call_in_expr(expr, fname):
                if isinstance(expr, CallExpr):
                    if expr.func.name == fname:
                        return expr
                if isinstance(expr, BinaryExpr):
                    c = find_call_in_expr(expr.left, fname)
                    if c: return c
                    return find_call_in_expr(expr.right, fname)
                return None

            def scan_body(stmts):
                nonlocal next_val, terminated
                for stmt in stmts:
                    if isinstance(stmt, IfStmt):
                        cond_val = eval_expr(stmt.cond)
                        if cond_val is True:
                            scan_body(stmt.then_branch)
                            return
                        elif cond_val is False:
                            scan_body(stmt.else_branch)
                            return
                    elif isinstance(stmt, ReturnStmt) and stmt.expr:
                        rec_call = find_call_in_expr(stmt.expr, func.name)
                        if rec_call:
                            next_val = eval_expr(rec_call.args[0])
                            terminated = False
                            return
                        else:
                            terminated = True
                            return
                            
            scan_body(func.body)
            if terminated:
                return True
            if next_val is None:
                return False
            current_val = next_val
            
        return False

    def verify_expr_safety(self, expr, line):
        if isinstance(expr, UnaryExpr):
            self.verify_expr_safety(expr.operand, line)

        elif isinstance(expr, BinaryExpr):
            self.verify_expr_safety(expr.left, line)
            self.verify_expr_safety(expr.right, line)
            # Prevent division by zero
            if expr.op in ['/', '%']:
                z3_right = self.translate_expr(expr.right)
                if z3_right is not None:
                    self.solver.push()
                    self.solver.add(z3_right == 0)
                    res = self.solver.check()
                    self.solver.pop()
                    if res == z3.sat:
                        raise SafetyError("Division by zero is possible", line, hint="Guard the divisor, e.g. if (b != 0) { ... }")
                else:
                    raise SafetyError(
                        "Cannot verify division safety — divisor is not expressible to the solver",
                        line,
                    )

        elif isinstance(expr, IndexExpr):
            self.verify_expr_safety(expr.expr, line)
            self.verify_expr_safety(expr.index, line)
            arr_t = self.resolver.infer_expr_type(expr.expr)
            if isinstance(arr_t, ListResolvedType):
                if arr_t.length:
                    self.verify_bounds(arr_t.length, expr.index, line)
                else:
                    self.verify_bounds_symbolic(expr.index, line)

        elif isinstance(expr, FieldExpr):
            self.verify_expr_safety(expr.expr, line)

        elif isinstance(expr, CallExpr):
            for arg in expr.args:
                self.verify_expr_safety(arg, line)
                
            func_name = expr.func.name

            if func_name == "input":
                return

            # 1. Parameter constraint verification for normal function calls at call site
            if get_overloads(self.resolver.functions, func_name):
                resolution = self.resolver.resolved_calls.get(id(expr))
                if resolution:
                    callee_decl = resolution["decl"]
                    call_generic_map = resolution["generic_map"]
                else:
                    callee_decl, call_generic_map = resolve_call(
                        self.resolver, func_name, expr, self.resolver.current_generic_map
                    )
                                
                for idx, param in enumerate(callee_decl.params):
                    if idx < len(expr.args):
                        param_t = self.resolver.resolve_type_expr(param.type_expr, call_generic_map)
                        if isinstance(param_t, RefinedResolvedType):
                            z3_arg = self._require(
                                self.translate_expr(expr.args[idx]),
                                f"argument {idx} of call to '{func_name}'",
                                line,
                            )
                            z3_constraint = self._require(
                                self.translate_expr(
                                    param_t.constraint,
                                    generic_map=call_generic_map,
                                    local_var_map={param_t.constraint_var: z3_arg},
                                    local_type_map={param_t.constraint_var: param_t.base_type},
                                ),
                                f"refined parameter constraint for '{param.name}'",
                                line,
                            )
                            self.solver.push()
                            self.solver.add(z3.Not(z3_constraint))
                            res = self.solver.check()
                            self.solver.pop()
                            if res == z3.sat:
                                raise SafetyError(
                                    f"Argument {idx + 1} ('{param.name}') to '{func_name}' "
                                    f"violates type constraint {format_type(param_t)}",
                                    line,
                                    hint="Pass a value that satisfies the refined type, or guard before the call.",
                                )
                                        
                if callee_decl.constraints:
                    pre_var_map = {}
                    pre_type_map = {}
                    for idx, param in enumerate(callee_decl.params):
                        if idx < len(expr.args):
                            z3_arg = self.translate_expr(expr.args[idx])
                            if z3_arg is not None:
                                pre_var_map[param.name] = z3_arg
                                param_t = self.resolver.resolve_type_expr(param.type_expr, call_generic_map)
                                if param_t:
                                    pre_type_map[param.name] = param_t.base_type if isinstance(param_t, RefinedResolvedType) else param_t

                    for constraint in callee_decl.constraints:
                        z3_pre = self.translate_expr(
                            constraint,
                            generic_map=call_generic_map,
                            local_var_map=pre_var_map,
                            local_type_map=pre_type_map,
                        )
                        if z3_pre is None:
                            raise SafetyError(
                                f"Cannot verify precondition for '{func_name}': {format_expr(constraint)}",
                                line,
                            )
                        self.solver.push()
                        self.solver.add(z3.Not(z3_pre))
                        res = self.solver.check()
                        self.solver.pop()
                        if res == z3.sat:
                            raise SafetyError(
                                f"Precondition not met for call to '{func_name}': {format_expr(constraint)}",
                                line,
                                hint=(
                                    f"Function '{func_name}' requires: {format_expr(constraint)}. "
                                    "Establish this with a guard or refined types before calling."
                                ),
                            )
                                        
            # 2. Recursive call termination checks (same overload only)
            resolution = self.resolver.resolved_calls.get(id(expr))
            same_overload = False
            if func_name == self.current_func_name and self.current_func_decl is not None:
                if resolution:
                    same_overload = (
                        ensure_mangled_name(resolution["decl"])
                        == ensure_mangled_name(self.current_func_decl)
                    )
                else:
                    overloads = get_overloads(self.resolver.functions, func_name)
                    same_overload = len(overloads) == 1
            if same_overload:
                func_decl = self.current_func_decl
                decrease_proven = False
                for idx, param in enumerate(func_decl.params):
                    param_t = self.resolver.resolve_type_expr(param.type_expr)
                    base_t = param_t.base_type if isinstance(param_t, RefinedResolvedType) else param_t
                    if base_t and base_t.name == 'Integer' and idx < len(expr.args):
                        z3_arg = self.translate_expr(expr.args[idx])
                        z3_param = self.get_z3_var(param.name, param_t)
                        if z3_arg is not None and z3_param is not None:
                            try:
                                self.solver.push()
                                self.solver.add(z3.Not(z3_arg < z3_param))
                                res = self.solver.check()
                                self.solver.pop()
                            except Exception:
                                res = z3.unknown
                            if res == z3.unsat:
                                # Also check if the argument is bounded below (e.g. z3_arg >= 0)
                                self.solver.push()
                                self.solver.add(z3_arg < 0)
                                bound_res = self.solver.check()
                                self.solver.pop()
                                if bound_res == z3.unsat:
                                    decrease_proven = True
                                    break
                        
                        try:
                            val = self.resolver.evaluate_constant(expr.args[idx])
                            if isinstance(val, int):
                                if self.simulate_termination(func_decl, val):
                                    decrease_proven = True
                                    break
                        except Exception:
                            pass
                if not decrease_proven:
                    raise SafetyError(
                        f"Recursive call to '{func_name}' may not terminate",
                        line,
                        hint=(
                            "Each recursive call must decrease a positive integer argument "
                            "(e.g. factorial(n - 1) with n > 0), or be tail-recursive."
                        ),
                    )
            if func_name in self.resolver.type_decls or func_name in self.resolver.types:
                struct_t = self.resolver.infer_expr_type(expr)
                
                if isinstance(struct_t, RefinedResolvedType):
                    if struct_t.constraint is not None and len(expr.args) == 1:
                        z3_arg = self._require(
                            self.translate_expr(expr.args[0]),
                            f"constructor argument for '{struct_t}'",
                            line,
                        )
                        z3_constraint = self._require(
                            self.translate_expr(
                                struct_t.constraint,
                                local_var_map={struct_t.constraint_var: z3_arg},
                                local_type_map={struct_t.constraint_var: struct_t.base_type},
                            ),
                            f"type constraint for '{struct_t}'",
                            line,
                        )
                        self.solver.push()
                        self.solver.add(z3.Not(z3_constraint))
                        res = self.solver.check()
                        self.solver.pop()
                        if res == z3.sat:
                            raise SafetyError(
                                f"Type constraint verification failed for '{struct_t}': "
                                f"expected {struct_t.constraint_var} to satisfy constraint",
                                line,
                            )

                elif isinstance(struct_t, StructResolvedType):
                    if struct_t.invariants:
                        fields_map = {}
                        for idx, (fname, ftype) in enumerate(struct_t.fields):
                            if idx < len(expr.args):
                                z3_arg = self._require(
                                    self.translate_expr(expr.args[idx]),
                                    f"field '{fname}' of '{struct_t.name}' constructor",
                                    line,
                                )
                                fields_map[fname] = z3_arg

                        for inv in struct_t.invariants:
                            z3_inv = self._require(
                                self.translate_expr(
                                    inv,
                                    generic_map=getattr(struct_t, 'generic_map', None),
                                    local_var_map=fields_map,
                                ),
                                f"invariant '{inv}' for '{struct_t.name}'",
                                line,
                            )
                            self.solver.push()
                            self.solver.add(z3.Not(z3_inv))
                            res = self.solver.check()
                            self.solver.pop()
                            if res == z3.sat:
                                raise SafetyError(
                                    f"Type invariant check failed for '{struct_t.name}': "
                                    f"constraint '{inv}' not satisfied",
                                    line,
                                )

        elif isinstance(expr, ListLiteral):
            for el in expr.elements:
                self.verify_expr_safety(el, line)
