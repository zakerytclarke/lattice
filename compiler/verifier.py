# Lattice SMT Safety Verifier & Recursion Bounds Checker

import sys
import z3

from compiler.parser import *
from compiler.resolver import *

class SafetyError(Exception):
    def __init__(self, msg, line):
        super().__init__(f"Safety/Constraint Verification Error at line {line}: {msg}")
        self.line = line

# ============================================================================
# SMT Constraint Solver Engine
# ============================================================================

class SMTVerifier:
    def __init__(self, resolver):
        self.resolver = resolver
        self.solver = z3.Solver() if 'z3' in sys.modules else None
        self.variables = {}
        self.temp_count = 0
        self.current_func_name = None

    def get_z3_var(self, name, resolved_type):
        if not self.solver:
            return None
            
        if name in self.variables:
            return self.variables[name]
            
        if isinstance(resolved_type, RefinedResolvedType):
            var = self.get_z3_var(name, resolved_type.base_type)
            if resolved_type.constraint is not None:
                local_var_map = {resolved_type.constraint_var: var}
                z3_constraint = self.translate_expr(resolved_type.constraint, local_var_map=local_var_map)
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
            
            val = tuple(fields_tuple) if len(fields_tuple) > 1 else fields_tuple[0]
            self.variables[name] = val
            
            if resolved_type.name == 'Rational':
                self.solver.add(fields_vars['Denominator'] != 0)
                
            if resolved_type.invariants:
                for inv in resolved_type.invariants:
                    z3_inv = self.translate_expr(inv, local_var_map=fields_vars)
                    if z3_inv is not None:
                        self.solver.add(z3_inv)
            return val
            
        return None

    def translate_expr(self, expr, generic_map=None, local_var_map=None):
        if not self.solver:
            return None
        generic_map = generic_map or {}
        local_var_map = local_var_map or {}
        
        if isinstance(expr, Literal):
            if expr.val_type == 'Integer':
                return expr.value
            if expr.val_type == 'Bool':
                return expr.value
                
        if isinstance(expr, Identifier):
            if expr.name in local_var_map:
                return local_var_map[expr.name]
            # Check if it maps to generic arg
            if expr.name in generic_map:
                val = generic_map[expr.name]
                if isinstance(val, int):
                    return val
            # Local/Global variable
            if expr.name in self.resolver.locals:
                t = self.resolver.locals[expr.name][0]
                return self.get_z3_var(expr.name, t)
            if expr.name in self.resolver.globals:
                t = self.resolver.globals[expr.name][0]
                return self.get_z3_var(expr.name, t)
            # Fallback to local name
            return z3.Int(expr.name)

        if isinstance(expr, BinaryExpr):
            left = self.translate_expr(expr.left, generic_map, local_var_map)
            right = self.translate_expr(expr.right, generic_map, local_var_map)
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

        if isinstance(expr, FieldExpr):
            base = self.translate_expr(expr.expr, generic_map, local_var_map)
            if base is not None:
                struct_t = self.resolver.infer_expr_type(expr.expr)
                if isinstance(struct_t, RefinedResolvedType):
                    struct_t = struct_t.base_type
                if isinstance(struct_t, StructResolvedType):
                    for idx, (fname, _) in enumerate(struct_t.fields):
                        if fname == expr.field:
                            if isinstance(base, tuple):
                                return base[idx]
                            return base
                    
        return None

    def assert_constraint(self, expr, generic_map=None):
        if not self.solver:
            return
        z3_expr = self.translate_expr(expr, generic_map)
        if z3_expr is not None:
            self.solver.add(z3_expr)

    def verify_bounds(self, arr_len, index_expr, line, generic_map=None):
        if not self.solver:
            return # Skip if z3 not loaded
            
        z3_index = self.translate_expr(index_expr, generic_map)
        if z3_index is None:
            return # Can't represent index in SMT
            
        # We want to check if index is guaranteed to be in range: 0 <= index < arr_len.
        # So we prove: Constraints => (0 <= index < arr_len)
        # To do this, we check if: Constraints AND NOT (0 <= index < arr_len) is unsatisfiable.
        self.solver.push()
        self.solver.add(z3.Or(z3_index < 0, z3_index >= arr_len))
        res = self.solver.check()
        self.solver.pop()
        
        if res == z3.sat:
            # Found a counterexample where bounds check fails!
            model = self.solver.model()
            val = model.eval(z3_index)
            raise SafetyError(
                f"Out of bounds index check failed. Index evaluated to {val} (array cap is {arr_len})", 
                line
            )

    def verify_program_safety(self, prog):
        for decl in prog.decls:
            if isinstance(decl, FuncDecl):
                self.verify_function(decl)

    def verify_function(self, func):
        if not self.solver:
            return
        self.current_func_name = func.name
        self.resolver.locals = self.resolver.func_locals[func.name]
        self.solver.push()
        
        # 1. Reset variable mappings for Z3
        self.variables = {}
        
        # 2. Assert server validation constraints / preconditions
        for constraint in func.constraints:
            self.assert_constraint(constraint)
            
        # 3. Check function statements for bounds checks and invariants
        for stmt in func.body:
            self.verify_statement(stmt)
            
        self.solver.pop()

    def verify_statement(self, stmt):
        if isinstance(stmt, VarDecl):
            # Assert variable equality if it can be represented
            z3_var = self.get_z3_var(stmt.name, self.resolver.locals[stmt.name][0])
            z3_val = self.translate_expr(stmt.value)
            if z3_var is not None and z3_val is not None:
                # If Rational, match fields
                if isinstance(z3_var, tuple):
                    if isinstance(z3_val, tuple):
                        self.solver.add(z3_var[0] == z3_val[0])
                        self.solver.add(z3_var[1] == z3_val[1])
                else:
                    self.solver.add(z3_var == z3_val)
            # Verify expression contents
            self.verify_expr_safety(stmt.value, stmt.line)
            
        elif isinstance(stmt, Assign):
            # If target is indexing or field, check safety
            self.verify_expr_safety(stmt.target, stmt.line)
            self.verify_expr_safety(stmt.value, stmt.line)
            
        elif isinstance(stmt, IfStmt):
            self.verify_expr_safety(stmt.cond, stmt.line)
            
            # Verify then branch
            self.solver.push()
            self.assert_constraint(stmt.cond)
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
                # Map variants to expected variant names
                expected = {v.name for v in p_type.variants if hasattr(v, 'name')}
                if 'Some' in expected or 'None' in expected:
                    expected = {'Some', 'None'}
                if 'Node' in expected or 'Empty' in expected:
                    expected = {'Node', 'Empty'}
                missing = expected - variants_matched
                if missing:
                    raise SafetyError(f"Pattern matching is not exhaustive. Missing cases: {list(missing)}", stmt.line)
            
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
        if isinstance(expr, BinaryExpr):
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
                        raise SafetyError("Division by zero check failed", line)

        elif isinstance(expr, IndexExpr):
            self.verify_expr_safety(expr.expr, line)
            self.verify_expr_safety(expr.index, line)
            # Verify list index bounds
            arr_t = self.resolver.infer_expr_type(expr.expr)
            if isinstance(arr_t, ListResolvedType):
                self.verify_bounds(arr_t.length, expr.index, line)

        elif isinstance(expr, FieldExpr):
            self.verify_expr_safety(expr.expr, line)

        elif isinstance(expr, CallExpr):
            for arg in expr.args:
                self.verify_expr_safety(arg, line)
                
            func_name = expr.func.name
            if func_name == self.current_func_name:
                func_decl = self.resolver.functions[func_name]
                decrease_proven = False
                for idx, param in enumerate(func_decl.params):
                    param_t = self.resolver.resolve_type_expr(param.type_expr)
                    base_t = param_t.base_type if isinstance(param_t, RefinedResolvedType) else param_t
                    if base_t and base_t.name == 'Integer' and idx < len(expr.args):
                        z3_arg = self.translate_expr(expr.args[idx])
                        z3_param = self.get_z3_var(param.name, param_t)
                        if z3_arg is not None and z3_param is not None:
                            self.solver.push()
                            self.solver.add(z3.Not(z3_arg < z3_param))
                            res = self.solver.check()
                            self.solver.pop()
                            if res == z3.unsat:
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
                    raise SafetyError(f"Recursive call to '{func_name}' is unsafe: could not prove termination (infinite loop or cyclic execution path detected)", line)
            if func_name in self.resolver.type_decls or func_name in self.resolver.types:
                struct_t = self.resolver.infer_expr_type(expr)
                
                if isinstance(struct_t, RefinedResolvedType):
                    if struct_t.constraint is not None and len(expr.args) == 1:
                        z3_arg = self.translate_expr(expr.args[0])
                        if z3_arg is not None:
                            z3_constraint = self.translate_expr(struct_t.constraint, local_var_map={struct_t.constraint_var: z3_arg})
                            if z3_constraint is not None:
                                self.solver.push()
                                self.solver.add(z3.Not(z3_constraint))
                                res = self.solver.check()
                                self.solver.pop()
                                if res == z3.sat:
                                    raise SafetyError(f"Type constraint verification failed for '{struct_t}': expected {struct_t.constraint_var} to satisfy constraint", line)
                                    
                elif isinstance(struct_t, StructResolvedType):
                    if struct_t.invariants:
                        fields_map = {}
                        for idx, (fname, ftype) in enumerate(struct_t.fields):
                            if idx < len(expr.args):
                                z3_arg = self.translate_expr(expr.args[idx])
                                if z3_arg is not None:
                                    fields_map[fname] = z3_arg
                                    
                        for inv in struct_t.invariants:
                            z3_inv = self.translate_expr(inv, local_var_map=fields_map)
                            if z3_inv is not None:
                                self.solver.push()
                                self.solver.add(z3.Not(z3_inv))
                                res = self.solver.check()
                                self.solver.pop()
                                if res == z3.sat:
                                    raise SafetyError(f"Type invariant check failed for '{struct_t.name}': constraint '{inv}' not satisfied", line)

        elif isinstance(expr, ListLiteral):
            for el in expr.elements:
                self.verify_expr_safety(el, line)

# ============================================================================
# Recursion & Termination Safety Checker
# ============================================================================

def verify_recursion_termination(func):
    """
    Checks if all recursive calls inside func are safe (terminating).
    A recursive call is safe if:
    1. It is a tail call.
    2. Or: it has a decreasing generic constraint parameter (e.g., Depth - 1).
    """
    recursive_calls = find_recursive_calls(func.body, func.name)
    if not recursive_calls:
        return # Not recursive, safe by default
        
    # Check if there is a Depth/size generic constraint
    depth_idx = -1
    for i, (gname, _) in enumerate(func.generics):
        if gname.lower() in ['depth', 'len', 'size']:
            depth_idx = i
            break
            
    for call, stmt_context in recursive_calls:
        # Case 1: Is it a tail call? (Directly returned)
        if isinstance(stmt_context, ReturnStmt) and stmt_context.expr == call:
            continue # Safe tail call
            
        # Case 2: Decreasing Depth constraint check
        if depth_idx != -1 and call.generic_args:
            # Check the generic argument at depth_idx
            g_arg = call.generic_args[depth_idx]
            # Must decrease strictly, e.g. Depth - 1
            if isinstance(g_arg, int) or (isinstance(g_arg, str) and '-' in g_arg):
                continue
            # Also check if decreasing literal expression
            if isinstance(g_arg, int) and g_arg < 0:
                raise SafetyError(f"Recursive call generic constraint must be non-negative: got {g_arg}", call.line)
            continue
            
        raise SafetyError(
            f"Recursive call to '{func.name}' is unsafe. Recursion must be tail-recursive "
            f"or have a structurally decreasing generic argument (e.g., Depth - 1)", 
            call.line
        )

def find_recursive_calls(body, func_name):
    calls = []
    
    def visit_stmt(stmt):
        if isinstance(stmt, VarDecl):
            visit_expr(stmt.value, stmt)
        elif isinstance(stmt, Assign):
            visit_expr(stmt.target, stmt)
            visit_expr(stmt.value, stmt)
        elif isinstance(stmt, IfStmt):
            visit_expr(stmt.cond, stmt)
            for s in stmt.then_branch: visit_stmt(s)
            for s in stmt.else_branch: visit_stmt(s)
        elif isinstance(stmt, ForStmt):
            visit_expr(stmt.start, stmt)
            visit_expr(stmt.end, stmt)
            for s in stmt.body: visit_stmt(s)
        elif isinstance(stmt, MatchStmt):
            visit_expr(stmt.expr, stmt)
            for case in stmt.cases:
                for s in case.body: visit_stmt(s)
        elif isinstance(stmt, ReturnStmt):
            if stmt.expr:
                visit_expr(stmt.expr, stmt)
        elif isinstance(stmt, ExprStmt):
            visit_expr(stmt.expr, stmt)

    def visit_expr(expr, stmt_context):
        if isinstance(expr, CallExpr):
            if isinstance(expr.func, Identifier) and expr.func.name == func_name:
                calls.append((expr, stmt_context))
            for arg in expr.args:
                visit_expr(arg, stmt_context)
        elif isinstance(expr, BinaryExpr):
            visit_expr(expr.left, stmt_context)
            visit_expr(expr.right, stmt_context)
        elif isinstance(expr, IndexExpr):
            visit_expr(expr.expr, stmt_context)
            visit_expr(expr.index, stmt_context)
        elif isinstance(expr, FieldExpr):
            visit_expr(expr.expr, stmt_context)
            
    for stmt in body:
        visit_stmt(stmt)
    return calls
