# Lattice WebAssembly Binary Generator

import sys
from compiler.parser import *
from compiler.resolver import *
from compiler.string_literal import lower_string_literal
from compiler.errors import LatticeTypeError
from compiler.overloads import iter_all_functions, ensure_mangled_name, get_overloads

# ============================================================================
# WASM Binary Encoding Helpers
# ============================================================================

def encode_leb128(val):
    result = bytearray()
    while True:
        byte = val & 0x7f
        val >>= 7
        if val == 0:
            result.append(byte)
            break
        else:
            result.append(byte | 0x80)
    return bytes(result)

def encode_sleb128(val):
    result = bytearray()
    while True:
        byte = val & 0x7f
        val >>= 7
        sign_bit_set = (byte & 0x40) != 0
        if (val == 0 and not sign_bit_set) or (val == -1 and sign_bit_set):
            result.append(byte)
            break
        else:
            result.append(byte | 0x80)
    return bytes(result)

def encode_string(s):
    encoded = s.encode('utf-8')
    return encode_leb128(len(encoded)) + encoded

def encode_vector(elements):
    return encode_leb128(len(elements)) + b''.join(elements)

# WASM Type Identifiers
TYPE_I32 = b'\x7f'
TYPE_VOID = b'\x40'

# Placeholder for unresolved size generics during WASM codegen. Verification uses
# strict inference; per-call-site monomorphization is not yet implemented in the emitter.
EMITTER_GENERIC_PLACEHOLDER = 100

# ============================================================================
# Code Emitter Class
# ============================================================================

class WASMEmitter:
    def __init__(self, resolver):
        self.resolver = resolver
        self.type_section = []    # List of encoded function types
        self.import_section = []  # List of imports
        self.func_section = []    # List of type indices for functions
        self.code_section = []    # List of function bodies
        self.export_section = []  # List of exports
        self.data_section = []    # List of data segments
        
        # Mappings
        self.func_type_indices = {}
        self.func_indices = {}
        self.wasm_locals = {}     # local name -> local index
        self.local_count = 0
        self.data_offset = 1024   # Start static data after memory offset 1024

    def compile(self, prog):
        # Export memory to JS
        self.export_section.append(encode_string("memory") + b'\x02\x00')
        
        # Emitter-only placeholder for unresolved size generics.
        # 1. Setup external imports dynamically
        for name, decl in iter_all_functions(self.resolver.functions):
            is_import = (decl.kind == 'external') and (len(decl.body) == 0 or name == 'print_int')
            if name == 'input':
                continue
            symbol = ensure_mangled_name(decl)
            if is_import:
                params = [TYPE_I32] * len(decl.params)
                ret = []
                if decl.ret_type:
                    temp_generic_map = {}
                    if decl.generics:
                        for gname, gtype_expr in decl.generics:
                            gtype_name = gtype_expr.name if hasattr(gtype_expr, 'name') else str(gtype_expr)
                            if gtype_name == 'Type':
                                temp_generic_map[gname] = self.resolver.types['Integer']
                            else:
                                temp_generic_map[gname] = EMITTER_GENERIC_PLACEHOLDER
                    ret_t = None
                    try:
                        ret_t = self.resolver.resolve_type_expr(decl.ret_type, temp_generic_map)
                    except LatticeTypeError:
                        pass
                    if isinstance(ret_t, IOResolvedType):
                        ret_t = ret_t.inner
                    if ret_t and not (isinstance(ret_t, PrimitiveResolvedType) and ret_t.name == 'void'):
                        ret = [TYPE_I32]
                self.add_import("env", name, b'\x00', params, ret)
                self.func_indices[symbol] = len(self.import_section) - 1
        
        # 2. Assign indices to declared functions
        for name, decl in iter_all_functions(self.resolver.functions):
            is_import = (decl.kind == 'external') and (len(decl.body) == 0 or name == 'print_int')
            if is_import or name == 'input':
                continue
            symbol = ensure_mangled_name(decl)
            params = [TYPE_I32] * len(decl.params)
            ret = []
            if decl.ret_type:
                temp_generic_map = {}
                if decl.generics:
                    for gname, gtype_expr in decl.generics:
                        gtype_name = gtype_expr.name if hasattr(gtype_expr, 'name') else str(gtype_expr)
                        if gtype_name == 'Type':
                            temp_generic_map[gname] = self.resolver.types['Integer']
                        else:
                            temp_generic_map[gname] = EMITTER_GENERIC_PLACEHOLDER
                ret_t = None
                try:
                    ret_t = self.resolver.resolve_type_expr(decl.ret_type, temp_generic_map)
                except LatticeTypeError:
                    pass
                if isinstance(ret_t, IOResolvedType):
                    ret_t = ret_t.inner
                if ret_t and not (isinstance(ret_t, PrimitiveResolvedType) and ret_t.name == 'void'):
                    ret = [TYPE_I32]
            t_idx = self.add_type_signature(params, ret)
            self.func_indices[symbol] = len(self.import_section) + len(self.func_section)
            self.func_section.append(encode_leb128(t_idx))

        # 3. Compile function bodies
        for name, decl in iter_all_functions(self.resolver.functions):
            is_import = (decl.kind == 'external') and (len(decl.body) == 0 or name == 'print_int')
            if decl.kind in ['normal', 'server', 'external'] and not is_import and name != 'input':
                body_bytes = self.compile_function(decl)
                self.code_section.append(body_bytes)
                
            symbol = ensure_mangled_name(decl)
            if decl.name in ['main', 'app_entry'] or (decl.kind == 'external' and not is_import):
                self.export_section.append(
                    encode_string(decl.name) + b'\x00' + encode_leb128(self.func_indices[symbol])
                )

        # 4. Build the final WASM file bytes
        return self.emit_binary()

    def add_type_signature(self, params, returns):
        sig = b'\x60' + encode_vector(params) + encode_vector(returns)
        if sig in self.func_type_indices:
            return self.func_type_indices[sig]
        idx = len(self.type_section)
        self.type_section.append(sig)
        self.func_type_indices[sig] = idx
        return idx

    def add_import(self, module, field, kind, params, returns):
        t_idx = self.add_type_signature(params, returns)
        imp = encode_string(module) + encode_string(field) + kind + encode_leb128(t_idx)
        self.func_indices[field] = len(self.import_section)
        self.import_section.append(imp)

    def _function_has_tail_calls(self, func):
        func_symbol = ensure_mangled_name(func)

        def is_tail_call(expr):
            if not isinstance(expr, CallExpr) or not isinstance(expr.func, Identifier):
                return False
            if expr.func.name != func.name:
                return False
            resolution = self.resolver.resolved_calls.get(id(expr))
            if resolution:
                return ensure_mangled_name(resolution["decl"]) == func_symbol
            overloads = get_overloads(self.resolver.functions, func.name)
            if len(overloads) > 1:
                return False
            return len(expr.args) == len(func.params)

        def visit_stmt(stmt):
            if isinstance(stmt, ReturnStmt) and stmt.expr and is_tail_call(stmt.expr):
                return True
            if isinstance(stmt, IfStmt):
                return any(visit_stmt(s) for s in stmt.then_branch) or any(
                    visit_stmt(s) for s in stmt.else_branch
                )
            if isinstance(stmt, ForStmt):
                return any(visit_stmt(s) for s in stmt.body)
            if isinstance(stmt, MatchStmt):
                return any(visit_stmt(s) for case in stmt.cases for s in case.body)
            return False

        return any(visit_stmt(stmt) for stmt in func.body)

    def _is_tail_call(self, expr):
        if not self.current_compiling_func_decl:
            return False
        if not isinstance(expr, CallExpr):
            return False
        if not isinstance(expr.func, Identifier):
            return False
        if expr.func.name != self.current_compiling_func_decl.name:
            return False
        resolution = self.resolver.resolved_calls.get(id(expr))
        if resolution:
            return (
                ensure_mangled_name(resolution["decl"])
                == ensure_mangled_name(self.current_compiling_func_decl)
            )
        overloads = get_overloads(self.resolver.functions, expr.func.name)
        if len(overloads) > 1:
            return False
        return len(expr.args) == len(self.current_compiling_func_decl.params)

    def _call_symbol(self, func_name, expr):
        resolution = self.resolver.resolved_calls.get(id(expr))
        if resolution:
            return ensure_mangled_name(resolution["decl"])
        overloads = get_overloads(self.resolver.functions, func_name)
        for decl in overloads:
            if len(decl.params) == len(expr.args):
                return ensure_mangled_name(decl)
        if len(overloads) == 1:
            return ensure_mangled_name(overloads[0])
        return func_name

    def compile_function(self, func):
        symbol = ensure_mangled_name(func)
        self.resolver.locals = self.resolver.func_locals[symbol]
        self.wasm_locals = {}
        
        # Build local generic map for function's own generic parameters
        local_generic_map = {}
        if func.generics:
            for gname, gtype_expr in func.generics:
                gtype_name = gtype_expr.name if hasattr(gtype_expr, 'name') else str(gtype_expr)
                if gtype_name == 'Type':
                    local_generic_map[gname] = self.resolver.types['Integer']
                else:
                    local_generic_map[gname] = EMITTER_GENERIC_PLACEHOLDER
        self.resolver.current_generic_map = local_generic_map

        # Parameters occupy local indices starting from 0
        for i, param in enumerate(func.params):
            self.wasm_locals[param.name] = i
        self.local_count = len(func.params)
        
        # Local variables map to subsequent local indices
        for name, (ltype, _) in self.resolver.locals.items():
            if name not in self.wasm_locals:
                self.wasm_locals[name] = self.local_count
                self.local_count += 1

        self._scratch_local = self.local_count
        self.local_count += 1
        self._scratch_local2 = self.local_count
        self.local_count += 1
                
        # Instructions bytecode
        code = bytearray()
        self.current_compiling_func = func.name
        self.current_compiling_func_decl = func
        use_tail_loop = self._function_has_tail_calls(func)
        
        if use_tail_loop:
            # Tail-call loop: self-recursive tail calls branch here instead of growing the stack.
            code.append(0x03) # loop
            code.append(0x40) # void
        
        # Compile parameter constraints checks for 'main'
        if func.name == 'main':
            for i, param in enumerate(func.params):
                param_t = self.resolver.resolve_type_expr(param.type_expr)
                curr_t = param_t
                while isinstance(curr_t, RefinedResolvedType):
                    if curr_t.constraint:
                        # Compile constraint check
                        old_local_map = self.wasm_locals.copy()
                        if curr_t.constraint_var:
                            self.wasm_locals[curr_t.constraint_var] = i
                        
                        constraint_code = self.compile_expr(curr_t.constraint)
                        self.wasm_locals = old_local_map
                        
                        code.extend(constraint_code)
                        code.append(0x45) # i32.eqz
                        code.append(0x04) # if
                        code.append(0x40) # void block
                        code.append(0x00) # unreachable
                        code.append(0x0b) # end if
                    curr_t = curr_t.base_type
                    
        for stmt in func.body:
            code.extend(self.compile_statement(stmt))
            
        has_ret = False
        if func.ret_type:
            ret_t = self.resolver.resolve_type_expr(func.ret_type)
            if isinstance(ret_t, IOResolvedType):
                ret_t = ret_t.inner
            if ret_t and not (isinstance(ret_t, PrimitiveResolvedType) and ret_t.name == 'void'):
                has_ret = True
        if has_ret:
            code.append(0x41) # i32.const
            code.extend(encode_sleb128(0))
            if use_tail_loop:
                code.append(0x0f) # return
            
        if use_tail_loop:
            code.append(0x0b) # end tail-call loop
        else:
            code.append(0x0b) # end function body expression
        self.current_compiling_func = None
        self.current_compiling_func_decl = None
        self.resolver.current_generic_map = {}
        
        # Local declarations vector
        num_locals_to_declare = self.local_count - len(func.params)
        local_decls = []
        if num_locals_to_declare > 0:
            local_decls.append(encode_leb128(num_locals_to_declare) + TYPE_I32)
            
        func_body = encode_vector(local_decls) + bytes(code)
        return encode_leb128(len(func_body)) + func_body

    def compile_statement(self, stmt):
        code = bytearray()
        if isinstance(stmt, VarDecl):
            # Evaluate initializer expression
            code.extend(self.compile_expr(stmt.value))
            # Store in local variable slot
            l_idx = self.wasm_locals[stmt.name]
            code.append(0x21) # local.set
            code.extend(encode_leb128(l_idx))
            
        elif isinstance(stmt, Assign):
            # Target could be local variable or list memory write
            if isinstance(stmt.target, Identifier):
                code.extend(self.compile_expr(stmt.value))
                l_idx = self.wasm_locals[stmt.target.name]
                code.append(0x21) # local.set
                code.extend(encode_leb128(l_idx))
            elif isinstance(stmt.target, IndexExpr):
                # Calculate list indexing address: base_addr + index * elem_size
                arr_t = self.resolver.infer_expr_type(stmt.target.expr)
                elem_size = arr_t.elem_type.get_size(self.resolver)
                
                # Base address (we look it up in locals/globals, or use static offset)
                code.extend(self.compile_expr(stmt.target.expr))
                # Add index * elem_size
                code.extend(self.compile_expr(stmt.target.index))
                code.append(0x41) # i32.const
                code.extend(encode_sleb128(elem_size))
                code.append(0x6c) # i32.mul
                code.append(0x6a) # i32.add
                
                # Value to write
                code.extend(self.compile_expr(stmt.value))
                
                # Store instruction: i32.store (alignment 2, offset 0)
                code.extend(b'\x36\x02\x00') 
                
        elif isinstance(stmt, IfStmt):
            code.extend(self.compile_expr(stmt.cond))
            code.append(0x04) # if
            code.append(0x40) # void block
            
            for s in stmt.then_branch:
                code.extend(self.compile_statement(s))
                
            if stmt.else_branch:
                code.append(0x05) # else
                for s in stmt.else_branch:
                    code.extend(self.compile_statement(s))
            code.append(0x0b) # end
            
        elif isinstance(stmt, ForStmt):
            # Compiled as:
            # i = start
            # loop:
            #   if i >= end break
            #   body...
            #   i = i + 1
            #   br loop
            idx = self.wasm_locals[stmt.var_name]
            
            # i = start
            code.extend(self.compile_expr(stmt.start))
            code.append(0x21) # local.set
            code.extend(encode_leb128(idx))
            
            # Wrap loop in block for exit/break target
            code.append(0x02) # block
            code.append(0x40) # void block
            
            code.append(0x03) # loop
            code.append(0x40) # void block
            
            # if i >= end break
            code.append(0x20) # local.get
            code.extend(encode_leb128(idx))
            code.extend(self.compile_expr(stmt.end))
            code.append(0x4e) # i32.ge_s
            
            code.append(0x04) # if
            code.append(0x40) # void
            code.append(0x0c) # br 2 (exit block)
            code.extend(encode_leb128(2))
            code.append(0x0b) # end if
            
            # Loop body
            for s in stmt.body:
                code.extend(self.compile_statement(s))
                
            # i = i + 1
            code.append(0x20) # local.get
            code.extend(encode_leb128(idx))
            code.append(0x41) # i32.const 1
            code.extend(encode_sleb128(1))
            code.append(0x6a) # i32.add
            code.append(0x21) # local.set
            code.extend(encode_leb128(idx))
            
            code.append(0x0c) # br 0 (repeat loop)
            code.extend(encode_leb128(0))
            code.append(0x0b) # end loop
            code.append(0x0b) # end block
            
        elif isinstance(stmt, MatchStmt):
            # Simple match compilation: sequential if-else checks matching variant tag
            p_type = self.resolver.infer_expr_type(stmt.expr)
            # Evaluate target expression
            code.extend(self.compile_expr(stmt.expr))
            # Save value to a temp local to check tags/values
            temp_idx = self.local_count
            self.local_count += 1
            code.append(0x21) # local.set
            code.extend(encode_leb128(temp_idx))
            
            for i, case in enumerate(stmt.cases):
                # For Union type, the first byte of struct memory is the variant index/tag.
                # Since we don't have full union heap, tag matches case index.
                # Tag check: load byte from temp address
                code.append(0x20) # local.get
                code.extend(encode_leb128(temp_idx))
                code.append(0x2d) # i32.load8_u
                code.extend(b'\x00\x00') # alignment 0, offset 0
                
                code.append(0x41) # i32.const (tag index)
                code.extend(encode_sleb128(i))
                code.append(0x46) # i32.eq
                
                code.append(0x04) # if
                code.append(0x40) # void
                
                # Unpack union type
                expr_t = self.resolver.infer_expr_type(stmt.expr)
                if isinstance(expr_t, IOResolvedType):
                    expr_t = expr_t.inner
                    
                variant_resolved_t = None
                if isinstance(expr_t, UnionResolvedType):
                    for var_t in expr_t.variants:
                        if var_t.name.split('_')[0] == case.pattern.name:
                            variant_resolved_t = var_t
                            break
                            
                # Bind match variables if any to memory offset or local
                for arg_idx, arg in enumerate(case.pattern.args):
                    field_offset = 1
                    if variant_resolved_t:
                        for prev_idx in range(arg_idx):
                            if prev_idx < len(variant_resolved_t.fields):
                                field_offset += variant_resolved_t.fields[prev_idx][1].get_size(self.resolver)
                            else:
                                field_offset += 4
                                
                    code.append(0x20) # local.get
                    code.extend(encode_leb128(temp_idx))
                    code.append(0x41) # offset of field
                    code.extend(encode_sleb128(field_offset))
                    code.append(0x6a) # add
                    
                    code.append(0x28) # i32.load
                    code.extend(b'\x02\x00')
                    
                    # Store in local arg binding
                    a_idx = self.wasm_locals[arg]
                    code.append(0x21) # local.set
                    code.extend(encode_leb128(a_idx))
                    
                for s in case.body:
                    code.extend(self.compile_statement(s))
                code.append(0x0b) # end if
                
        elif isinstance(stmt, ReturnStmt):
            if stmt.expr and self._is_tail_call(stmt.expr):
                resolution = self.resolver.resolved_calls.get(id(stmt.expr))
                callee = (
                    resolution["decl"]
                    if resolution
                    else self.current_compiling_func_decl
                )
                for i, arg in enumerate(stmt.expr.args):
                    code.extend(self.compile_expr(arg))
                    code.append(0x21) # local.set
                    code.extend(encode_leb128(self.wasm_locals[callee.params[i].name]))
                code.append(0x0c) # br 0 (tail-call loop)
                code.extend(encode_leb128(0))
            else:
                if stmt.expr:
                    code.extend(self.compile_expr(stmt.expr))
                code.append(0x0f) # WASM explicit return instruction
            
        elif isinstance(stmt, ExprStmt):
            code.extend(self.compile_expr(stmt.expr))
            # Pop stack if expression returns a value but statement ignores it
            expr_t = self.resolver.infer_expr_type(stmt.expr)
            if isinstance(expr_t, IOResolvedType):
                expr_t = expr_t.inner
            if expr_t and expr_t.name != 'void':
                code.append(0x1a) # drop
                
        return code

    def compile_expr(self, expr):
        code = bytearray()
        if isinstance(expr, StringLiteral):
            return self.compile_expr(lower_string_literal(expr, len(expr.value)))
        if isinstance(expr, Literal):
            if expr.val_type == 'Integer':
                code.append(0x41) # i32.const
                code.extend(encode_sleb128(expr.value))
            elif expr.val_type == 'Char':
                val = ord(expr.value) if isinstance(expr.value, str) else int(expr.value)
                code.append(0x41) # i32.const
                code.extend(encode_sleb128(val))
            elif expr.val_type == 'Bool':
                code.append(0x41) # i32.const
                code.extend(encode_sleb128(1 if expr.value else 0))
                
        elif isinstance(expr, Identifier):
            # Check if generic constant
            if expr.name in self.resolver.current_generic_map:
                val = self.resolver.current_generic_map[expr.name]
                if isinstance(val, int):
                    code.append(0x41) # i32.const
                    code.extend(encode_sleb128(val))
                    return code
            if expr.name in self.wasm_locals:
                l_idx = self.wasm_locals[expr.name]
                code.append(0x20) # local.get
                code.extend(encode_leb128(l_idx))
            else:
                # Load from global memory offset
                offset = self.resolver.globals[expr.name][1]
                code.append(0x41) # const address
                code.extend(encode_sleb128(offset))
                code.append(0x28) # i32.load
                code.extend(b'\x02\x00')
                
        elif isinstance(expr, UnaryExpr):
            unop = self.resolver.resolved_unops.get(id(expr))
            if unop:
                code.extend(self.compile_expr(expr.operand))
                f_idx = self.func_indices[ensure_mangled_name(unop["decl"])]
                code.append(0x10)
                code.extend(encode_leb128(f_idx))
            elif expr.op == '!':
                code.extend(self.compile_expr(expr.operand))
                code.append(0x45)  # i32.eqz

        elif isinstance(expr, BinaryExpr):
            binop = self.resolver.resolved_binops.get(id(expr))
            if binop:
                for arg in (expr.left, expr.right):
                    code.extend(self.compile_expr(arg))
                f_idx = self.func_indices[ensure_mangled_name(binop["decl"])]
                code.append(0x10)
                code.extend(encode_leb128(f_idx))
            else:
                left_t = right_t = None
                try:
                    left_t = self.resolver.infer_expr_type(expr.left)
                    right_t = self.resolver.infer_expr_type(expr.right)
                except LatticeTypeError:
                    pass
                left_max = self.resolver._struct_string_max_len(left_t) if left_t else None
                right_max = self.resolver._struct_string_max_len(right_t) if right_t else None
                if left_max is not None and right_max is not None and expr.op == '+':
                    out_addr = self.data_offset
                    self.data_offset += left_t.get_size(self.resolver)
                    code.extend(self.compile_expr(expr.left))
                    code.append(0x21)
                    code.extend(encode_leb128(self._scratch_local))
                    code.extend(self.compile_expr(expr.right))
                    code.append(0x21)
                    code.extend(encode_leb128(self._scratch_local2))
                    code.append(0x41)
                    code.extend(encode_sleb128(out_addr))
                    code.append(0x20)
                    code.extend(encode_leb128(self._scratch_local))
                    code.append(0x20)
                    code.extend(encode_leb128(self._scratch_local2))
                    code.append(0x41)
                    code.extend(encode_sleb128(left_max + right_max))
                    f_idx = self.func_indices['concat_strings']
                    code.append(0x10)
                    code.extend(encode_leb128(f_idx))
                    code.append(0x41)
                    code.extend(encode_sleb128(out_addr))
                elif left_max is not None and right_max is not None and expr.op in ['==', '!=']:
                    code.extend(self.compile_expr(expr.left))
                    code.append(0x21)
                    code.extend(encode_leb128(self._scratch_local))
                    code.extend(self.compile_expr(expr.right))
                    code.append(0x21)
                    code.extend(encode_leb128(self._scratch_local2))
                    code.append(0x20)
                    code.extend(encode_leb128(self._scratch_local))
                    code.append(0x20)
                    code.extend(encode_leb128(self._scratch_local2))
                    f_idx = self.func_indices['strings_equal']
                    code.append(0x10)
                    code.extend(encode_leb128(f_idx))
                    if expr.op == '!=':
                        code.append(0x45)
                else:
                    code.extend(self.compile_expr(expr.left))
                    code.extend(self.compile_expr(expr.right))
                    
                    if expr.op == '+': code.append(0x6a)
                    elif expr.op == '-': code.append(0x6b)
                    elif expr.op == '*': code.append(0x6c)
                    elif expr.op == '/': code.append(0x6d)
                    elif expr.op == '%': code.append(0x6f)
                    elif expr.op == '==': code.append(0x46)
                    elif expr.op == '!=': code.append(0x47)
                    elif expr.op == '<': code.append(0x48)
                    elif expr.op == '>': code.append(0x4a)
                    elif expr.op == '<=': code.append(0x4c)
                    elif expr.op == '>=': code.append(0x4e)
                    elif expr.op == '&&': code.append(0x71)
                    elif expr.op == '||': code.append(0x72)
                    elif expr.op == '^': code.append(0x73)
            
        elif isinstance(expr, IndexExpr):
            # Calculate address = base_address + index * element_size
            arr_t = self.resolver.infer_expr_type(expr.expr)
            elem_size = arr_t.elem_type.get_size(self.resolver)
            
            code.extend(self.compile_expr(expr.expr))
            code.extend(self.compile_expr(expr.index))
            code.append(0x41) # const elem_size
            code.extend(encode_sleb128(elem_size))
            code.append(0x6c) # i32.mul
            code.append(0x6a) # i32.add
            
            # Load from address: i32.load
            code.append(0x28)
            code.extend(b'\x02\x00') # alignment 2, offset 0
            
        elif isinstance(expr, FieldExpr):
            # Calculate address = base_address + field_offset
            struct_t = self.resolver.infer_expr_type(expr.expr)
            if isinstance(struct_t, ListResolvedType) and expr.field == 'data':
                code.extend(self.compile_expr(expr.expr))
                # Load pointer from offset 0
                code.append(0x28)
                code.extend(b'\x02\x00')
            else:
                field_offset = 0
                target_ftype = None
                for name, ftype in struct_t.fields:
                    if name == expr.field:
                        target_ftype = ftype
                        break
                    field_offset += ftype.get_size(self.resolver)
                    
                code.extend(self.compile_expr(expr.expr))
                code.append(0x41) # const field_offset
                code.extend(encode_sleb128(field_offset))
                code.append(0x6a) # add
                
                # Load value from field address
                code.append(0x28)
                code.extend(b'\x02\x00')
            
        elif isinstance(expr, CallExpr):
            func_name = expr.func.name
            if func_name == 'input':
                site_id = self.resolver.input_call_exprs.get(id(expr))
                if site_id is None:
                    raise RuntimeError("input(...) call was not registered during type checking")
                out_site = self.resolver.input_call_sites[site_id]
                out_t = out_site['resolved_type']
                out_addr = self.data_offset
                self.data_offset += out_t.get_size(self.resolver)

                code.extend(self.compile_expr(expr.args[0]))
                code.append(0x21)
                code.extend(encode_leb128(self._scratch_local))

                code.append(0x41)
                code.extend(encode_sleb128(site_id))
                code.append(0x20)
                code.extend(encode_leb128(self._scratch_local))
                code.append(0x41)
                code.extend(encode_sleb128(out_addr))
                f_idx = self.func_indices['input_typed']
                code.append(0x10)
                code.extend(encode_leb128(f_idx))

                code.append(0x41)
                code.extend(encode_sleb128(out_addr))
            elif func_name in self.resolver.type_decls or func_name in self.resolver.types:
                # Constructor!
                struct_t = self.resolver.infer_expr_type(expr)
                
                # Check if this constructor is a union variant
                union_decl = None
                variant_tag = -1
                for decl_name, decl in self.resolver.type_decls.items():
                    if isinstance(decl, UnionTypeDecl):
                        for tag, vexpr in enumerate(decl.variants):
                            if vexpr.name == func_name:
                                union_decl = decl
                                variant_tag = tag
                                break
                                
                union_size = 0
                if variant_tag != -1:
                    union_size = 1 + struct_t.get_size(self.resolver)
                        
                addr = self.data_offset
                if union_size > 0:
                    self.data_offset += union_size
                else:
                    self.data_offset += struct_t.get_size(self.resolver)
                    
                field_offset = 0
                if variant_tag != -1:
                    # Write tag byte
                    code.append(0x41) # address
                    code.extend(encode_sleb128(addr))
                    code.append(0x41) # tag val
                    code.extend(encode_sleb128(variant_tag))
                    code.append(0x3a) # i32.store8
                    code.extend(b'\x00\x00')
                    field_offset = 1
                    
                for i, arg in enumerate(expr.args):
                    code.append(0x41) # address
                    code.extend(encode_sleb128(addr + field_offset))
                    code.extend(self.compile_expr(arg))
                    code.extend(b'\x36\x02\x00') # i32.store
                    
                    if hasattr(struct_t, 'fields') and i < len(struct_t.fields):
                        field_offset += struct_t.fields[i][1].get_size(self.resolver)
                    else:
                        field_offset += 4
                        
                code.append(0x41) # return address
                code.extend(encode_sleb128(addr))
            else:
                for arg in expr.args:
                    arg_code = self.compile_expr(arg)
                    code.extend(arg_code)
                resolution = self.resolver.resolved_calls.get(id(expr))
                if resolution:
                    call_symbol = ensure_mangled_name(resolution["decl"])
                else:
                    call_symbol = self._call_symbol(func_name, expr)
                f_idx = self.func_indices[call_symbol]
                code.append(0x10) # call
                code.extend(encode_leb128(f_idx))
            
        elif isinstance(expr, ListLiteral):
            # Allocate list statically in data section and return memory offset
            addr = self.data_offset
            data_bytes = bytearray()
            for el in expr.elements:
                # Assuming list contains numeric literals or compile-time constants
                if isinstance(el, Literal) and el.val_type == 'Integer':
                    data_bytes.extend(el.value.to_bytes(4, byteorder='little', signed=True))
                elif isinstance(el, Literal) and el.val_type == 'Char':
                    val = ord(el.value) if isinstance(el.value, str) else int(el.value)
                    data_bytes.extend(val.to_bytes(4, byteorder='little', signed=True))
                else:
                    data_bytes.extend(int(0).to_bytes(4, byteorder='little'))
                    
            # Register data segment
            self.data_section.append(
                b'\x00\x41' + encode_sleb128(addr) + b'\x0b' + encode_leb128(len(data_bytes)) + bytes(data_bytes)
            )
            self.data_offset += len(data_bytes)
            
            # Return address on WASM stack
            code.append(0x41)
            code.extend(encode_sleb128(addr))
            
        return code

    def emit_binary(self):
        binary = bytearray(b'\x00\x61\x73\x6d\x01\x00\x00\x00') # WASM Magic + Version
        
        # 1. Type Section
        binary.append(1)
        binary.extend(encode_leb128(len(encode_vector(self.type_section))))
        binary.extend(encode_vector(self.type_section))
        
        # 2. Import Section
        if self.import_section:
            binary.append(2)
            binary.extend(encode_leb128(len(encode_vector(self.import_section))))
            binary.extend(encode_vector(self.import_section))
            
        # 3. Function Section
        binary.append(3)
        binary.extend(encode_leb128(len(encode_vector(self.func_section))))
        binary.extend(encode_vector(self.func_section))
        
        # 4. Memory Section (1 page = 64KB)
        binary.append(5)
        # Limit flag: 0 (min page limit only), min: 1 page
        mem_data = encode_vector([b'\x00' + encode_leb128(1)])
        binary.extend(encode_leb128(len(mem_data)))
        binary.extend(mem_data)
        
        # 5. Export Section
        binary.append(7)
        binary.extend(encode_leb128(len(encode_vector(self.export_section))))
        binary.extend(encode_vector(self.export_section))
        
        # 6. Code Section
        binary.append(10)
        binary.extend(encode_leb128(len(encode_vector(self.code_section))))
        binary.extend(encode_vector(self.code_section))
        
        # 7. Data Section
        if self.data_section:
            binary.append(11)
            binary.extend(encode_leb128(len(encode_vector(self.data_section))))
            binary.extend(encode_vector(self.data_section))
            
        return bytes(binary)
