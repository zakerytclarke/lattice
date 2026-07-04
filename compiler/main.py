# Lattice Compiler Main Entry CLI Driver

import sys
import os

# Add current project root to python path to resolve local imports cleanly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compiler.parser import parse_code, Literal, Identifier, BinaryExpr, FieldExpr
from compiler.resolver import Resolver, LatticeTypeError
from compiler.verifier import SMTVerifier, SafetyError
from compiler.emitter import WASMEmitter

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 compiler/main.py <source_file.lattice> [output_file.wasm]")
        sys.exit(1)
        
    source_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else source_path.replace(".lattice", ".wasm")
    
    # Check if standard library exists and load it
    stdlib_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "stdlib.lattice")
    stdlib_code = ""
    if os.path.exists(stdlib_path):
        with open(stdlib_path, "r") as f:
            stdlib_code = f.read()
            
    # Read user source code
    if not os.path.exists(source_path):
        print(f"Error: Source file '{source_path}' not found")
        sys.exit(1)
        
    with open(source_path, "r") as f:
        source_code = f.read()
        
    # Merge standard library and user code
    full_code = stdlib_code + "\n\n" + source_code
    
    try:
        ast = parse_code(full_code)
    except SyntaxError as e:
        print(f"Syntax Error: {e}")
        sys.exit(1)
        
    resolver = Resolver()
    try:
        resolver.resolve_program(ast)
    except LatticeTypeError as e:
        print(f"Type Inference/Checking Error: {e}")
        sys.exit(1)
        
    # Pre-resolve function bodies for local offsets calculation
    for name, decl in resolver.functions.items():
        if decl.kind in ['normal', 'server']:
            resolver.resolve_func_body(decl)
            
    # Run SMT Solver (Z3 bounds checker)
    try:
        import z3
    except ImportError:
        print("Error: 'z3-solver' package is required but not installed. Run 'pip install z3-solver' to run SMT safety checks.")
        sys.exit(1)
        
    try:
        verifier = SMTVerifier(resolver)
        verifier.verify_program_safety(ast)
    except SafetyError as e:
        print(f"SMT Safety Verification Failed: {e}")
        sys.exit(1)
        
    emitter = WASMEmitter(resolver)
    try:
        wasm_bytes = emitter.compile(ast)
    except Exception as e:
        print(f"Compilation/WASM Emission Error: {e}")
        sys.exit(1)
        
    with open(output_path, "wb") as f:
        f.write(wasm_bytes)

    # Serialize metadata for runtime parameter constraints
    from compiler.resolver import RefinedResolvedType, StructResolvedType
    
    def expr_to_str(expr):
        if isinstance(expr, Literal):
            return str(expr.value)
        if isinstance(expr, Identifier):
            return expr.name
        if isinstance(expr, BinaryExpr):
            left = expr_to_str(expr.left)
            right = expr_to_str(expr.right)
            return f"({left} {expr.op} {right})"
        if isinstance(expr, FieldExpr):
            return expr.field
        return ""

    metadata = []
    if 'main' in resolver.functions:
        main_decl = resolver.functions['main']
        for param in main_decl.params:
            param_t = resolver.resolve_type_expr(param.type_expr)
            if isinstance(param_t, RefinedResolvedType):
                metadata.append({
                    "name": param.name,
                    "kind": "inline",
                    "base_type": param_t.base_type.name,
                    "constraint_var": param_t.constraint_var,
                    "constraint_str": expr_to_str(param_t.constraint)
                })
            elif isinstance(param_t, StructResolvedType) and param_t.invariants:
                metadata.append({
                    "name": param.name,
                    "kind": "typedef",
                    "type_name": param_t.name,
                    "constraint_var": param_t.fields[0][0] if param_t.fields else "value",
                    "constraint_str": " && ".join(expr_to_str(inv) for inv in param_t.invariants)
                })
            else:
                metadata.append({
                    "name": param.name,
                    "kind": "plain",
                    "base_type": param_t.name if param_t else "Integer"
                })
                
    import json
    with open(output_path + ".metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)

if __name__ == "__main__":
    main()
