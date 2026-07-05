# Lattice Compiler Main Entry CLI Driver

import sys
import os

# Add current project root to python path to resolve local imports cleanly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compiler.parser import parse_code, Literal, Identifier, BinaryExpr, FieldExpr, ImportDecl, FuncDecl, TypeDecl, UnionTypeDecl
from compiler.errors import LatticeTypeError, SafetyError, format_compilation_error
from compiler.resolver import Resolver
from compiler.verifier import SMTVerifier
from compiler.emitter import WASMEmitter
from compiler.stdlib import STDLIB_CODE
from compiler.entry_metadata import write_main_metadata

# Parse and resolve standard library once globally
try:
    stdlib_ast = parse_code(STDLIB_CODE)
except SyntaxError as e:
    print(f"Standard Library Syntax Error: {e}")
    sys.exit(1)

stdlib_resolver = Resolver()
try:
    stdlib_resolver.resolve_program(stdlib_ast)
except LatticeTypeError as e:
    print(format_compilation_error(e, "stdlib"))
    sys.exit(1)

# Resolve standard library function bodies
for name, decl in stdlib_resolver.functions.items():
    if decl.kind in ['normal', 'server', 'external']:
        stdlib_resolver.resolve_func_body(decl)

# Verify standard library safety at compiler startup
try:
    stdlib_verifier = SMTVerifier(stdlib_resolver)
    stdlib_verifier.verify_program_safety(stdlib_ast)
except SafetyError as e:
    print(format_compilation_error(e, "stdlib"))
    sys.exit(1)


class ModuleLoader:
    def __init__(self):
        # abs_path -> (ast, resolver)
        self.modules = {}
        
    def load_module(self, path):
        abs_path = os.path.abspath(path)
        if abs_path in self.modules:
            return self.modules[abs_path]
            
        if not os.path.exists(abs_path):
            # Try appending .lattice
            if not abs_path.endswith('.lattice') and os.path.exists(abs_path + '.lattice'):
                abs_path = abs_path + '.lattice'
            else:
                raise FileNotFoundError(f"File not found: {path}")
                
        if abs_path in self.modules:
            return self.modules[abs_path]
            
        with open(abs_path, 'r') as f:
            code = f.read()
            
        try:
            ast = parse_code(code)
        except SyntaxError as e:
            raise SyntaxError(f"In {os.path.basename(abs_path)}: {e}")
            
        # Parse imports first
        imports = [d for d in ast.decls if isinstance(d, ImportDecl)]
        
        dependencies = {}
        for imp in imports:
            dir_name = os.path.dirname(abs_path)
            mod_name = imp.module_name
            if not mod_name.endswith('.lattice'):
                mod_name += '.lattice'
                
            dep_path = os.path.join(dir_name, mod_name)
            try:
                dep_ast, dep_resolver = self.load_module(dep_path)
            except Exception as e:
                raise LatticeTypeError(f"Failed to load import '{imp.module_name}': {e}", imp.line)
            dependencies[imp] = (dep_ast, dep_resolver)
            
        resolver = Resolver()
        # Copy standard library definitions
        resolver.types.update(stdlib_resolver.types)
        resolver.type_decls.update(stdlib_resolver.type_decls)
        resolver.functions.update(stdlib_resolver.functions)
        resolver.func_locals.update(stdlib_resolver.func_locals)
        
        # Now process imports into this resolver
        for imp, (dep_ast, dep_resolver) in dependencies.items():
            dep_defined_functions = {d.name for d in dep_ast.decls if isinstance(d, FuncDecl)}
            dep_defined_types = {d.name for d in dep_ast.decls if isinstance(d, (TypeDecl, UnionTypeDecl))}
            
            if imp.symbols == '*':
                # Import all external functions defined in dep
                for name, decl in dep_resolver.functions.items():
                    if name in dep_defined_functions and decl.kind == 'external' and name != 'main':
                        if name in resolver.functions and resolver.functions[name] is not stdlib_resolver.functions.get(name):
                            raise LatticeTypeError(f"Import conflict: symbol '{name}' already defined", imp.line)
                        resolver.functions[name] = decl
                        if name in dep_resolver.func_locals:
                            resolver.func_locals[name] = dep_resolver.func_locals[name]
                # Import all external types defined in dep
                for name, decl in dep_resolver.type_decls.items():
                    if name in dep_defined_types and getattr(decl, 'is_external', False):
                        if name in resolver.type_decls:
                            raise LatticeTypeError(f"Import conflict: type '{name}' already defined", imp.line)
                        resolver.type_decls[name] = decl
                        mono_prefix = name + "_"
                        for tname, tval in dep_resolver.types.items():
                            if tname == name or tname.startswith(mono_prefix):
                                resolver.types[tname] = tval
            else:
                for sym in imp.symbols:
                    found = False
                    if sym in dep_defined_functions and sym in dep_resolver.functions:
                        decl = dep_resolver.functions[sym]
                        if decl.kind == 'external':
                            if sym in resolver.functions and resolver.functions[sym] is not stdlib_resolver.functions.get(sym):
                                raise LatticeTypeError(f"Import conflict: symbol '{sym}' already defined", imp.line)
                            resolver.functions[sym] = decl
                            if sym in dep_resolver.func_locals:
                                resolver.func_locals[sym] = dep_resolver.func_locals[sym]
                            found = True
                    if sym in dep_defined_types and sym in dep_resolver.type_decls:
                        decl = dep_resolver.type_decls[sym]
                        if getattr(decl, 'is_external', False):
                            if sym in resolver.type_decls:
                                raise LatticeTypeError(f"Import conflict: type '{sym}' already defined", imp.line)
                            resolver.type_decls[sym] = decl
                            mono_prefix = sym + "_"
                            for tname, tval in dep_resolver.types.items():
                                if tname == sym or tname.startswith(mono_prefix):
                                    resolver.types[tname] = tval
                            found = True
                            
                    if not found:
                        raise LatticeTypeError(f"Symbol '{sym}' not found or not declared external in module '{imp.module_name}'", imp.line)
                        
        try:
            resolver.resolve_program(ast)
        except LatticeTypeError as e:
            e.file_name = os.path.basename(abs_path)
            raise

        # Verify safety using Z3
        verifier = SMTVerifier(resolver)
        try:
            verifier.verify_program_safety(ast)
        except SafetyError as e:
            e.file_name = os.path.basename(abs_path)
            raise
            
        self.modules[abs_path] = (ast, resolver)
        return ast, resolver


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 compiler/main.py <source_file.lattice> [output_file.wasm]")
        sys.exit(1)
        
    source_path = sys.argv[1]
    if len(sys.argv) > 2:
        output_path = sys.argv[2]
    else:
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        build_dir = os.path.join(project_dir, "build")
        os.makedirs(build_dir, exist_ok=True)
        basename = os.path.basename(source_path).replace(".lattice", ".wasm")
        output_path = os.path.join(build_dir, basename)
        
    # Make sure output directory exists
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    
    # Read user source code
    if not os.path.exists(source_path):
        print(f"Error: Source file '{source_path}' not found")
        sys.exit(1)
        
    # Load and resolve modules
    loader = ModuleLoader()
    try:
        main_ast, main_resolver = loader.load_module(source_path)
    except (SyntaxError, LatticeTypeError, SafetyError) as e:
        file_name = getattr(e, 'file_name', None) or os.path.basename(source_path)
        print(format_compilation_error(e, file_name))
        sys.exit(1)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)
        
    # Verify main function existence and that it is declared external
    if 'main' not in main_resolver.functions:
        print("Error: The function main must be declared in the main source file.")
        sys.exit(1)
        
    main_decl = main_resolver.functions['main']
    main_defined_in_main = any(d.name == 'main' for d in main_ast.decls if isinstance(d, FuncDecl))
    if not main_defined_in_main:
        print("Error: The function main must be declared in the main source file.")
        sys.exit(1)
        
    if main_decl.kind != 'external':
        print("Error: The function main must be declared external.")
        sys.exit(1)

    for param in main_decl.params:
        if param.type_expr is None:
            print(
                format_compilation_error(
                    LatticeTypeError(
                        f"Parameter '{param.name}' of main must have a type",
                        main_decl.line,
                        hint="Add an explicit type annotation or use the parameter so its type can be inferred.",
                    ),
                    os.path.basename(source_path),
                )
            )
            sys.exit(1)
        
    # Collect all definitions from standard library and all loaded modules
    all_functions = {}
    all_func_locals = {}
    all_types = {}
    all_type_decls = {}
    
    # 1. Start with standard library definitions
    all_functions.update(stdlib_resolver.functions)
    all_func_locals.update(stdlib_resolver.func_locals)
    all_types.update(stdlib_resolver.types)
    all_type_decls.update(stdlib_resolver.type_decls)
    
    # 2. Add definitions from all loaded modules
    for abs_path, (mod_ast, mod_resolver) in loader.modules.items():
        for decl in mod_ast.decls:
            if isinstance(decl, FuncDecl):
                if decl.name == 'main' and abs_path != os.path.abspath(source_path):
                    continue
                all_functions[decl.name] = decl
                if decl.name in mod_resolver.func_locals:
                    all_func_locals[decl.name] = mod_resolver.func_locals[decl.name]
            elif isinstance(decl, (TypeDecl, UnionTypeDecl)):
                all_type_decls[decl.name] = decl
                mono_prefix = decl.name + "_"
                for tname, tval in mod_resolver.types.items():
                    if tname == decl.name or tname.startswith(mono_prefix):
                        all_types[tname] = tval

    # 3. Populate main_resolver with all collected definitions
    main_resolver.functions.update(all_functions)
    main_resolver.func_locals.update(all_func_locals)
    main_resolver.types.update(all_types)
    main_resolver.type_decls.update(all_type_decls)
    
    emitter = WASMEmitter(main_resolver)
    try:
        wasm_bytes = emitter.compile(main_ast)
    except Exception as e:
        print(f"Compilation/WASM Emission Error: {e}")
        sys.exit(1)
        
    with open(output_path, "wb") as f:
        f.write(wasm_bytes)

    write_main_metadata(main_decl, output_path)


if __name__ == "__main__":
    main()
