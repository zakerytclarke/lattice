# Lattice Compiler Main Entry CLI Driver

import sys
import os

# Add current project root to python path to resolve local imports cleanly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compiler.parser import parse_code, Literal, Identifier, BinaryExpr, FieldExpr, ImportDecl, FuncDecl, TypeDecl, UnionTypeDecl, Param, TypeExpr
from compiler.errors import LatticeTypeError, LatticeSyntaxError, SafetyError, format_compilation_error
from compiler.resolver import Resolver
from compiler.verifier import SMTVerifier
from compiler.emitter import WASMEmitter
from compiler.stdlib import STDLIB_CODE
from compiler.entry_metadata import write_main_metadata
from compiler.input_metadata import write_program_metadata
from compiler.diagnostics import CompilationDiagnostics
from compiler.type_debug import print_type_report
from compiler.overloads import (
    register_function,
    merge_function_maps,
    get_overloads,
    iter_all_functions,
    function_signature_key,
    ensure_mangled_name,
)


def register_input_function(resolver):
    if get_overloads(resolver.functions, "input"):
        return
    register_function(
        resolver.functions,
        FuncDecl(
        'external',
        'input',
        [
            ('T', TypeExpr('Type', [], 0)),
            ('PromptLen', TypeExpr('Integer', [], 0)),
        ],
        [
            Param(
                'prompt',
                TypeExpr('String', [], 0, size=TypeExpr('PromptLen', [], 0)),
                0,
            )
        ],
        TypeExpr('IO', [TypeExpr('T', [], 0)], 0),
        [],
        [],
        0,
        ),
    )


# Parse and resolve standard library once globally
try:
    stdlib_ast = parse_code(STDLIB_CODE)
except LatticeSyntaxError as e:
    print(format_compilation_error(e, "stdlib"))
    sys.exit(1)
except SyntaxError as e:
    print(format_compilation_error(e, "stdlib"))
    sys.exit(1)

stdlib_resolver = Resolver()
try:
    stdlib_resolver.resolve_program(stdlib_ast)
except LatticeTypeError as e:
    print(format_compilation_error(e, "stdlib"))
    sys.exit(1)

register_input_function(stdlib_resolver)

# Resolve standard library function bodies
for _, decl in iter_all_functions(stdlib_resolver.functions):
    if decl.kind in ['normal', 'server', 'external'] and decl.name != 'input':
        stdlib_resolver.resolve_func_body(decl)

# Verify standard library safety at compiler startup
try:
    stdlib_verifier = SMTVerifier(stdlib_resolver)
    stdlib_verifier.verify_program_safety(stdlib_ast)
except SafetyError as e:
    print(format_compilation_error(e, "stdlib"))
    sys.exit(1)


class ModuleLoader:
    def __init__(self, diagnostics=None):
        # abs_path -> (ast, resolver)
        self.modules = {}
        self.sources = {}
        self.diagnostics = diagnostics or CompilationDiagnostics()
        
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
        self.sources[abs_path] = code.splitlines()
            
        try:
            ast = parse_code(code)
        except LatticeSyntaxError as e:
            e.file_name = os.path.basename(abs_path)
            e.source_lines = self.sources[abs_path]
            raise
        except SyntaxError as e:
            err = LatticeSyntaxError(
                str(e),
                getattr(e, 'lineno', 1) or 1,
                getattr(e, 'offset', 1) or 1,
            )
            err.file_name = os.path.basename(abs_path)
            err.source_lines = self.sources[abs_path]
            raise err
            
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
        resolver.diagnostics = self.diagnostics
        resolver._current_file_name = os.path.basename(abs_path)
        resolver._current_source_lines = self.sources[abs_path]
        resolver.types.update(stdlib_resolver.types)
        resolver.type_decls.update(stdlib_resolver.type_decls)
        merge_function_maps(resolver.functions, stdlib_resolver.functions)
        resolver.func_locals.update(stdlib_resolver.func_locals)
        register_input_function(resolver)
        
        # Now process imports into this resolver
        for imp, (dep_ast, dep_resolver) in dependencies.items():
            dep_defined_functions = {d.name for d in dep_ast.decls if isinstance(d, FuncDecl)}
            dep_defined_types = {d.name for d in dep_ast.decls if isinstance(d, (TypeDecl, UnionTypeDecl))}
            
            if imp.symbols == '*':
                for _, decl in iter_all_functions(dep_resolver.functions):
                    if decl.name in dep_defined_functions and decl.kind == 'external' and decl.name != 'main':
                        try:
                            register_function(resolver.functions, decl)
                        except LatticeTypeError as e:
                            raise LatticeTypeError(f"Import conflict: {e.message}", imp.line) from e
                        mn = ensure_mangled_name(decl)
                        if mn in dep_resolver.func_locals:
                            resolver.func_locals[mn] = dep_resolver.func_locals[mn]
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
                    for decl in get_overloads(dep_resolver.functions, sym):
                        if sym in dep_defined_functions and decl.kind == 'external':
                            try:
                                register_function(resolver.functions, decl)
                            except LatticeTypeError as e:
                                raise LatticeTypeError(f"Import conflict: {e.message}", imp.line) from e
                            mn = ensure_mangled_name(decl)
                            if mn in dep_resolver.func_locals:
                                resolver.func_locals[mn] = dep_resolver.func_locals[mn]
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
                        
        resolver.resolve_program(ast)

        if not self.diagnostics.has_errors():
            verifier = SMTVerifier(resolver)
            verifier.verify_program_safety(ast)
            
        self.modules[abs_path] = (ast, resolver)
        return ast, resolver


def parse_compiler_args(argv):
    args = list(argv)
    optimal = False
    while args and args[0].startswith("--"):
        if args[0] == "--optimal":
            optimal = True
            args.pop(0)
        else:
            print(f"Error: Unknown compiler flag '{args[0]}'")
            sys.exit(1)

    if not args:
        print("Usage: python3 compiler/main.py [--optimal] <source_file.lattice> [output_file.wasm]")
        sys.exit(1)

    source_path = args[0]
    if len(args) > 1:
        output_path = args[1]
    else:
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        build_dir = os.path.join(project_dir, "build")
        os.makedirs(build_dir, exist_ok=True)
        basename = os.path.basename(source_path).replace(".lattice", ".wasm")
        output_path = os.path.join(build_dir, basename)

    return optimal, source_path, output_path


def main():
    optimal, source_path, output_path = parse_compiler_args(sys.argv[1:])
        
    # Make sure output directory exists
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    
    # Read user source code
    if not os.path.exists(source_path):
        print(f"Error: Source file '{source_path}' not found")
        sys.exit(1)
        
    # Load and resolve modules
    diagnostics = CompilationDiagnostics()
    loader = ModuleLoader(diagnostics)
    main_ast = None
    main_resolver = None
    try:
        main_ast, main_resolver = loader.load_module(source_path)
    except (LatticeSyntaxError, SyntaxError, LatticeTypeError, SafetyError) as e:
        file_name = getattr(e, 'file_name', None) or os.path.basename(source_path)
        source_lines = getattr(e, 'source_lines', None) or loader.sources.get(os.path.abspath(source_path))
        diagnostics.add(e, file_name, source_lines)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    if diagnostics.has_errors():
        if optimal and main_resolver is not None and main_ast is not None:
            print_type_report(
                main_resolver,
                main_ast.decls,
                os.path.basename(source_path),
            )
            print()
        print(diagnostics.format_all())
        sys.exit(1)
        
    # Verify main function existence and that it is declared external
    if not get_overloads(main_resolver.functions, 'main'):
        diagnostics.add(
            LatticeTypeError("The function main must be declared in the main source file.", 1),
            os.path.basename(source_path),
            loader.sources.get(os.path.abspath(source_path)),
        )
        
    main_decl = get_overloads(main_resolver.functions, 'main')[0] if get_overloads(main_resolver.functions, 'main') else None
    main_defined_in_main = any(d.name == 'main' for d in main_ast.decls if isinstance(d, FuncDecl))
    if main_decl and not main_defined_in_main:
        diagnostics.add(
            LatticeTypeError("The function main must be declared in the main source file.", main_decl.line),
            os.path.basename(source_path),
            loader.sources.get(os.path.abspath(source_path)),
        )
        
    if main_decl and main_decl.kind != 'external':
        diagnostics.add(
            LatticeTypeError("The function main must be declared external.", main_decl.line),
            os.path.basename(source_path),
            loader.sources.get(os.path.abspath(source_path)),
        )

    if main_decl:
        for param in main_decl.params:
            if param.type_expr is None:
                diagnostics.add(
                    LatticeTypeError(
                        f"Parameter '{param.name}' of main must have a type",
                        main_decl.line,
                        column=getattr(main_decl, 'column', None),
                        hint="Add an explicit type annotation or use the parameter so its type can be inferred.",
                    ),
                    os.path.basename(source_path),
                    loader.sources.get(os.path.abspath(source_path)),
                )

    if diagnostics.has_errors():
        if optimal:
            print_type_report(
                main_resolver,
                main_ast.decls,
                os.path.basename(source_path),
            )
            print()
        print(diagnostics.format_all())
        sys.exit(1)
        
    # Collect all definitions from standard library and all loaded modules
    all_functions = {}
    all_func_locals = {}
    all_types = {}
    all_type_decls = {}
    
    merge_function_maps(all_functions, stdlib_resolver.functions)
    all_func_locals.update(stdlib_resolver.func_locals)
    all_types.update(stdlib_resolver.types)
    all_type_decls.update(stdlib_resolver.type_decls)
    
    source_abs = os.path.abspath(source_path)
    for abs_path, (mod_ast, mod_resolver) in loader.modules.items():
        for decl in mod_ast.decls:
            if isinstance(decl, FuncDecl):
                if decl.name == 'main' and abs_path != source_abs:
                    continue
                for overload in get_overloads(mod_resolver.functions, decl.name):
                    try:
                        register_function(all_functions, overload)
                    except LatticeTypeError:
                        pass
                    mn = ensure_mangled_name(overload)
                    if mn in mod_resolver.func_locals:
                        all_func_locals[mn] = mod_resolver.func_locals[mn]
            elif isinstance(decl, (TypeDecl, UnionTypeDecl)):
                all_type_decls[decl.name] = decl
                mono_prefix = decl.name + "_"
                for tname, tval in mod_resolver.types.items():
                    if tname == decl.name or tname.startswith(mono_prefix):
                        all_types[tname] = tval

    merge_function_maps(main_resolver.functions, all_functions)
    main_resolver.func_locals.update(all_func_locals)
    main_resolver.types.update(all_types)
    main_resolver.type_decls.update(all_type_decls)

    for _, decl in iter_all_functions(main_resolver.functions):
        if decl.kind in ['normal', 'server', 'external'] and decl.name != 'input':
            try:
                main_resolver.resolve_func_body(decl)
            except LatticeTypeError as e:
                diagnostics.add(
                    e,
                    getattr(e, 'file_name', None) or os.path.basename(source_path),
                    loader.sources.get(os.path.abspath(source_path)),
                )

    if diagnostics.has_errors():
        if optimal:
            print_type_report(
                main_resolver,
                main_ast.decls,
                os.path.basename(source_path),
            )
            print()
        print(diagnostics.format_all())
        sys.exit(1)

    if optimal:
        print_type_report(
            main_resolver,
            main_ast.decls,
            os.path.basename(source_path),
        )
        print()
    
    emitter = WASMEmitter(main_resolver)
    try:
        wasm_bytes = emitter.compile(main_ast)
    except Exception as e:
        print(f"Compilation/WASM Emission Error: {e}")
        sys.exit(1)
        
    with open(output_path, "wb") as f:
        f.write(wasm_bytes)

    write_program_metadata(main_decl, main_resolver.input_call_sites, output_path)


if __name__ == "__main__":
    main()
