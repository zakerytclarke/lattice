# Lattice WebAssembly Binary Generator

import sys
from compiler.parser import *
from compiler.resolver import *

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
        # 1. Setup default print functions
        # For simplicity, we import printing from env
        self.add_import("env", "print_int", b'\x00', [TYPE_I32], [])
        
        # 2. Assign indices to declared functions
        for name, decl in self.resolver.functions.items():
            if name == 'print_int':
                continue
            params = [TYPE_I32] * len(decl.params)
            ret = [TYPE_I32] if decl.ret_type and decl.ret_type.name != 'void' else []
            t_idx = self.add_type_signature(params, ret)
            self.func_indices[name] = len(self.import_section) + len(self.func_section)
            self.func_section.append(encode_leb128(t_idx))

        # 3. Compile function bodies
        for name, decl in self.resolver.functions.items():
            if decl.kind in ['normal', 'server', 'external'] and name != 'print_int':
                body_bytes = self.compile_function(decl)
                self.code_section.append(body_bytes)
                
            # Export main/external entry points
            if decl.name in ['main', 'app_entry'] or decl.kind == 'external':
                self.export_section.append(
                    encode_string(decl.name) + b'\x00' + encode_leb128(self.func_indices[decl.name])
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

    def compile_function(self, func):
        self.resolver.locals = self.resolver.func_locals[func.name]
        self.wasm_locals = {}
        # Parameters occupy local indices starting from 0
        for i, param in enumerate(func.params):
            self.wasm_locals[param.name] = i
        self.local_count = len(func.params)
        
        # Local variables map to subsequent local indices
        locals_to_declare = []
        for name, (ltype, _) in self.resolver.locals.items():
            if name not in self.wasm_locals:
                self.wasm_locals[name] = self.local_count
                locals_to_declare.append(TYPE_I32)
                self.local_count += 1
                
        # Instructions bytecode
        code = bytearray()
        for stmt in func.body:
            code.extend(self.compile_statement(stmt))
            
        if func.ret_type and func.ret_type.name != 'void':
            code.append(0x41) # i32.const
            code.extend(encode_sleb128(0))
            
        code.append(0x0b) # End function body
        
        # Local declarations vector
        local_decls = []
        if locals_to_declare:
            # Group consecutive locals of same type
            local_decls.append(encode_leb128(len(locals_to_declare)) + TYPE_I32)
            
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
                
                # Bind match variables if any to memory offset or local
                for arg_idx, arg in enumerate(case.pattern.args):
                    # Load value from variant fields (starting at offset 1 after the tag byte)
                    code.append(0x20) # local.get
                    code.extend(encode_leb128(temp_idx))
                    code.append(0x41) # offset of field
                    code.extend(encode_sleb128(1 + arg_idx * 4))
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
            if stmt.expr:
                code.extend(self.compile_expr(stmt.expr))
            code.append(0x0f) # WASM explicit return instruction
            
        elif isinstance(stmt, ExprStmt):
            code.extend(self.compile_expr(stmt.expr))
            # Pop stack if expression returns a value but statement ignores it
            expr_t = self.resolver.infer_expr_type(stmt.expr)
            if expr_t and expr_t.name != 'void':
                code.append(0x1a) # drop
                
        return code

    def compile_expr(self, expr):
        code = bytearray()
        if isinstance(expr, Literal):
            if expr.val_type in ['Integer', 'Char']:
                code.append(0x41) # i32.const
                code.extend(encode_sleb128(expr.value))
            elif expr.val_type == 'Bool':
                code.append(0x41) # i32.const
                code.extend(encode_sleb128(1 if expr.value else 0))
                
        elif isinstance(expr, Identifier):
            # Check local index
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
                
        elif isinstance(expr, BinaryExpr):
            code.extend(self.compile_expr(expr.left))
            code.extend(self.compile_expr(expr.right))
            
            if expr.op == '+': code.append(0x6a) # i32.add
            elif expr.op == '-': code.append(0x6b) # i32.sub
            elif expr.op == '*': code.append(0x6c) # i32.mul
            elif expr.op == '/': code.append(0x6d) # i32.div_s
            elif expr.op == '%': code.append(0x6f) # i32.rem_s
            elif expr.op == '==': code.append(0x46) # i32.eq
            elif expr.op == '!=': code.append(0x47) # i32.ne
            elif expr.op == '<': code.append(0x48) # i32.lt_s
            elif expr.op == '>': code.append(0x4a) # i32.gt_s
            elif expr.op == '<=': code.append(0x4c) # i32.le_s
            elif expr.op == '>=': code.append(0x4e) # i32.ge_s
            elif expr.op == '&&': code.append(0x71) # i32.and
            elif expr.op == '||': code.append(0x72) # i32.or
            
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
            else:
                field_offset = 0
                for name, ftype in struct_t.fields:
                    if name == expr.field:
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
            if func_name in self.resolver.type_decls or func_name in self.resolver.types:
                # Constructor!
                struct_t = self.resolver.infer_expr_type(expr)
                addr = self.data_offset
                self.data_offset += struct_t.get_size(self.resolver)
                
                field_offset = 0
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
                f_idx = self.func_indices[func_name]
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
