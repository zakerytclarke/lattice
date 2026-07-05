# Lattice AST & Parser

import re
import sys

# ============================================================================
# 1. AST Node Definitions
# ============================================================================

class ASTNode:
    def __init__(self, line):
        self.line = line

class Program(ASTNode):
    def __init__(self, decls, line):
        super().__init__(line)
        self.decls = decls

class TypeDecl(ASTNode):
    def __init__(self, name, generics, fields, invariants, line):
        super().__init__(line)
        self.name = name
        self.generics = generics  # list of (name, type)
        self.fields = fields      # list of (name, type)
        self.invariants = invariants  # list of expression ASTs
        self.is_union = False
        self.is_external = False

class UnionTypeDecl(ASTNode):
    def __init__(self, name, generics, variants, line):
        super().__init__(line)
        self.name = name
        self.generics = generics
        self.variants = variants  # list of TypeExprs
        self.is_union = True
        self.is_external = False

class FuncDecl(ASTNode):
    def __init__(self, kind, name, generics, params, ret_type, constraints, body, line):
        super().__init__(line)
        self.kind = kind          # 'normal', 'external', 'server'
        self.name = name
        self.generics = generics  # list of (name, type)
        self.params = params      # list of Param
        self.ret_type = ret_type  # TypeExpr or None
        self.constraints = constraints  # list of expressions (for server functions/termination)
        self.body = body          # list of statements

class ImportDecl(ASTNode):
    def __init__(self, symbols, module_name, line):
        super().__init__(line)
        self.symbols = symbols      # list of str, or '*'
        self.module_name = module_name  # str

class Param(ASTNode):
    def __init__(self, name, type_expr, line):
        super().__init__(line)
        self.name = name
        self.type_expr = type_expr # TypeExpr or None

class VarDecl(ASTNode):
    def __init__(self, is_const, name, type_expr, value, line):
        super().__init__(line)
        self.is_const = is_const
        self.name = name
        self.type_expr = type_expr # TypeExpr or None
        self.value = value        # Expr

class Assign(ASTNode):
    def __init__(self, target, value, line):
        super().__init__(line)
        self.target = target      # Identifier, IndexExpr, or FieldExpr
        self.value = value        # Expr

class IfStmt(ASTNode):
    def __init__(self, cond, then_branch, else_branch, line):
        super().__init__(line)
        self.cond = cond
        self.then_branch = then_branch
        self.else_branch = else_branch

class ForStmt(ASTNode):
    def __init__(self, var_name, start, end, body, line):
        super().__init__(line)
        self.var_name = var_name
        self.start = start
        self.end = end
        self.body = body

class MatchStmt(ASTNode):
    def __init__(self, expr, cases, line):
        super().__init__(line)
        self.expr = expr
        self.cases = cases        # list of MatchCase

class MatchCase(ASTNode):
    def __init__(self, pattern, body, line):
        super().__init__(line)
        self.pattern = pattern    # Pattern
        self.body = body          # list of statements

class Pattern(ASTNode):
    def __init__(self, name, type_bind, args, line):
        super().__init__(line)
        self.name = name          # e.g., 'Some', 'None', 'Empty'
        self.type_bind = type_bind # e.g. W, Z in x: Matrix[W, Z]
        self.args = args          # list of str (sub-bindings)

class ReturnStmt(ASTNode):
    def __init__(self, expr, line):
        super().__init__(line)
        self.expr = expr

class ExprStmt(ASTNode):
    def __init__(self, expr, line):
        super().__init__(line)
        self.expr = expr

# Expressions
class BinaryExpr(ASTNode):
    def __init__(self, op, left, right, line):
        super().__init__(line)
        self.op = op
        self.left = left
        self.right = right

class IndexExpr(ASTNode):
    def __init__(self, expr, index, line):
        super().__init__(line)
        self.expr = expr
        self.index = index

class FieldExpr(ASTNode):
    def __init__(self, expr, field, line):
        super().__init__(line)
        self.expr = expr
        self.field = field

class CallExpr(ASTNode):
    def __init__(self, func, args, generic_args, line):
        super().__init__(line)
        self.func = func
        self.args = args
        self.generic_args = generic_args  # explicit generics, e.g. insert[4](...)

class TypeExpr(ASTNode):
    def __init__(self, name, args, line, constraint=None, constraint_var=None):
        super().__init__(line)
        self.name = name          # e.g., 'Integer', 'List'
        self.args = args          # generic TypeExprs or values
        self.constraint = constraint
        self.constraint_var = constraint_var

class Literal(ASTNode):
    def __init__(self, value, val_type, line):
        super().__init__(line)
        self.value = value        # int, bool, str, char
        self.val_type = val_type  # 'Int', 'Bool', 'String', 'Char'

class Identifier(ASTNode):
    def __init__(self, name, line):
        super().__init__(line)
        self.name = name

class ListLiteral(ASTNode):
    def __init__(self, elements, line):
        super().__init__(line)
        self.elements = elements

# ============================================================================
# 2. Tokenizer / Lexer
# ============================================================================

TOKEN_SPEC = [
    ('COMMENT',   r'//.*'),
    ('NUMBER',    r'\d+'),
    ('STRING',    r'"[^"]*"|\'[^\']{2,}\'|\'\''),
    ('CHAR',      r"'[^']'"),
    ('ARROW',     r'->'),
    ('DOUBLE_DOT',r'\.\.'),
    ('FAT_ARROW', r'=>'),
    ('OP',        r'==|!=|<=|>=|&&|\|\||[+\-*/%=<>&|!.]'),
    ('ID',        r'[a-zA-Z_][a-zA-Z0-9_]*'),
    ('LPAREN',    r'\('),
    ('RPAREN',    r'\)'),
    ('LBRACKET',  r'\['),
    ('RBRACKET',  r'\]'),
    ('LBRACE',    r'\{'),
    ('RBRACE',    r'\}'),
    ('COMMA',     r','),
    ('COLON',     r':'),
    ('SEMICOLON', r';'),
    ('SKIP',      r'[ \t\r]+'),
    ('NEWLINE',   r'\n'),
    ('MISMATCH',  r'.'),
]

class Token:
    def __init__(self, type, value, line):
        self.type = type
        self.value = value
        self.line = line
    def __repr__(self):
        return f"Token({self.type}, {repr(self.value)}, {self.line})"

def tokenize(code):
    tokens = []
    line_num = 1
    regex = '|'.join(f'(?P<{name}>{pattern})' for name, pattern in TOKEN_SPEC)
    for mo in re.finditer(regex, code):
        kind = mo.lastgroup
        value = mo.group(kind)
        if kind == 'NEWLINE':
            line_num += 1
        elif kind == 'SKIP' or kind == 'COMMENT':
            pass
        elif kind == 'MISMATCH':
            raise SyntaxError(f"Unexpected character {repr(value)} at line {line_num}")
        else:
            if kind == 'STRING':
                value = value[1:-1]
            elif kind == 'CHAR':
                value = value[1:-1]
            tokens.append(Token(kind, value, line_num))
    return tokens

# ============================================================================
# 3. Parser
# ============================================================================

class Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self, offset=0):
        if self.pos + offset >= len(self.tokens):
            return None
        return self.tokens[self.pos + offset]

    def match(self, *expected_types):
        t = self.peek()
        if t and t.type in expected_types:
            self.pos += 1
            return t
        return None

    def match_val(self, expected_type, expected_val):
        t = self.peek()
        if t and t.type == expected_type and t.value == expected_val:
            self.pos += 1
            return t
        return None

    def consume(self, expected_type, msg=""):
        t = self.match(expected_type)
        if not t:
            curr = self.peek()
            line = curr.line if curr else "EOF"
            val = curr.value if curr else ""
            raise SyntaxError(f"Parse error at line {line} near '{val}': {msg or f'expected {expected_type}'}")
        return t

    def parse_program(self):
        decls = []
        while self.peek():
            decls.append(self.parse_declaration())
        return Program(decls, 1)

    def parse_declaration(self):
        t = self.peek()
        if not t:
            raise SyntaxError("Unexpected EOF while parsing declaration")
        
        # 1. Check for import
        if t.type == 'ID' and t.value == 'import':
            return self.parse_import_declaration()
            
        # 2. Check for optional external or server modifier
        is_external = self.match_val('ID', 'external')
        is_server = self.match_val('ID', 'server')
        
        # 3. Check for type or function declaration
        t2 = self.peek()
        if not t2:
            raise SyntaxError("Unexpected EOF while parsing declaration")
            
        if t2.type == 'ID' and t2.value == 'type':
            type_decl = self.parse_type_declaration()
            type_decl.is_external = (is_external is not None)
            return type_decl
            
        if t2.type == 'ID' and t2.value == 'function':
            self.consume('ID') # consume 'function'
            kind = 'external' if is_external else ('server' if is_server else 'normal')
            return self.parse_func_declaration(kind)
            
        raise SyntaxError(f"Expected declaration (type or function) at line {t.line}")

    def parse_import_declaration(self):
        line = self.consume('ID').line # import
        
        # Parse symbols to import
        symbols = []
        if self.match_val('OP', '*'):
            symbols = '*'
        else:
            # Parse identifier or list of comma-separated identifiers
            while True:
                symbols.append(self.consume('ID').value)
                if not self.match('COMMA'):
                    break
                    
        self.consume('ID', "expected 'from'") # from
        
        # Parse module name (can be a string literal or ID)
        t = self.peek()
        if t and t.type == 'STRING':
            module_name = self.consume('STRING').value
        else:
            module_name = self.consume('ID').value
            
        self.match_val('SEMICOLON', ';') # optional semicolon
        
        return ImportDecl(symbols, module_name, line)

    def parse_type_declaration(self):
        line = self.consume('ID').line # type
        name = self.consume('ID').value
        
        generics = []
        base_type_ref = None
        if self.match('LBRACKET'):
            gname = self.peek().value if self.peek() else ""
            if gname in ['Integer', 'Int', 'Bool', 'Char', 'Rational']:
                base_type_ref = self.consume('ID').value
                self.consume('RBRACKET')
            else:
                while True:
                    gname = self.consume('ID').value
                    gtype = None
                    if self.match('COLON'):
                        gtype = self.parse_type_expr()
                    generics.append((gname, gtype))
                    if not self.match('COMMA'):
                        break
                self.consume('RBRACKET')
            
        # Check if Union
        if self.match_val('OP', '='):
            u_word = self.consume('ID')
            if u_word.value != 'Union':
                raise SyntaxError(f"Expected 'Union' at line {u_word.line}")
            self.consume('LBRACKET')
            variants = []
            while True:
                variants.append(self.parse_type_expr())
                if not self.match('COMMA'):
                    break
            self.consume('RBRACKET')
            return UnionTypeDecl(name, generics, variants, line)
            
        # Struct type definition
        fields = []
        if self.match('LPAREN'):
            if not self.peek() or self.peek().type != 'RPAREN':
                while True:
                    fname = self.consume('ID').value
                    ftype = None
                    if self.match('COLON'):
                        ftype = self.parse_type_expr()
                    else:
                        if base_type_ref:
                            ftype = TypeExpr(base_type_ref, [], line)
                        elif generics:
                            ftype = TypeExpr(generics[0][0], [], line)
                        else:
                            ftype = TypeExpr('Integer', [], line)
                    fields.append((fname, ftype))
                    if not self.match('COMMA'):
                        break
            self.consume('RPAREN')
        
        invariants = []
        if self.match('LBRACE'):
            while self.peek() and self.peek().type != 'RBRACE':
                invariants.append(self.parse_expr())
                self.match('SEMICOLON') # optional semicolon for constraints
            self.consume('RBRACE')
            
        return TypeDecl(name, generics, fields, invariants, line)

    def parse_func_declaration(self, kind):
        line = self.peek().line if self.peek() else 0
        name = self.consume('ID').value
        
        generics = []
        if self.match('LBRACKET'):
            while True:
                gname = self.consume('ID').value
                gtype = None
                if self.match('COLON'):
                    gtype = self.parse_type_expr()
                generics.append((gname, gtype))
                if not self.match('COMMA'):
                    break
            self.consume('RBRACKET')
            
        self.consume('LPAREN')
        params = []
        # Support optional 'let' or 'const' prefix in params (ignored internally or for compatibility)
        if not self.peek() or self.peek().type != 'RPAREN':
            while True:
                self.match_val('ID', 'let')
                self.match_val('ID', 'const')
                pname = self.consume('ID').value
                ptype = None
                if self.match('COLON'):
                    ptype = self.parse_type_expr()
                params.append(Param(pname, ptype, line))
                if not self.match('COMMA'):
                    break
        self.consume('RPAREN')
        
        ret_type = None
        if self.match('ARROW'):
            ret_type = self.parse_type_expr()
            
        constraints = []
        # Preconditions / server validation constraints before function body
        if self.peek() and self.peek().type == 'LBRACE':
            depth = 0
            has_second_lbrace = False
            for i in range(self.pos, len(self.tokens)):
                tok = self.tokens[i]
                if tok.type == 'LBRACE':
                    depth += 1
                elif tok.type == 'RBRACE':
                    depth -= 1
                    if depth == 0:
                        if i + 1 < len(self.tokens) and self.tokens[i + 1].type == 'LBRACE':
                            has_second_lbrace = True
                        break
            if has_second_lbrace:
                self.consume('LBRACE')
                while self.peek() and self.peek().type != 'RBRACE':
                    constraints.append(self.parse_expr())
                    self.match('SEMICOLON')
                self.consume('RBRACE')
            
        self.consume('LBRACE')
        body = []
        while self.peek() and self.peek().type != 'RBRACE':
            body.append(self.parse_statement())
        self.consume('RBRACE')
        
        return FuncDecl(kind, name, generics, params, ret_type, constraints, body, line)

    def parse_type_expr(self):
        line = self.peek().line if self.peek() else 0
        
        # Parse Type Name (could be placeholder '_' or identifier)
        tname = "_"
        if self.match_val('ID', '_'):
            tname = "_"
        else:
            tname = self.consume('ID').value
            
        args = []
        # Support either Name[A, B] or Name(A, B) for parameterized types
        if self.match('LBRACKET') or self.match('LPAREN'):
            delim = 'RBRACKET' if self.tokens[self.pos-1].type == 'LBRACKET' else 'RPAREN'
            while True:
                # Check if it is a nested TypeExpr or a numeric size constraint
                if self.peek() and self.peek().type == 'NUMBER':
                    num = self.consume('NUMBER').value
                    args.append(Literal(int(num), 'Integer', line))
                elif self.peek() and self.peek().type == 'ID' and self.peek().value in ['true', 'false']:
                    val = self.consume('ID').value == 'true'
                    args.append(Literal(val, 'Bool', line))
                else:
                    args.append(self.parse_type_expr())
                if not self.match('COMMA'):
                    break
            self.consume(delim)
            
        constraint = None
        constraint_var = None
        was_paren = (self.pos - 1 >= 0 and self.tokens[self.pos - 1].type == 'RPAREN')
        if self.peek() and self.peek().type == 'LBRACE' and was_paren and len(args) == 1 and isinstance(args[0], TypeExpr) and len(args[0].args) == 0:
            constraint_var = args[0].name
            args = []
            self.consume('LBRACE')
            constraint = self.parse_expr()
            self.consume('RBRACE')
            
        return TypeExpr(tname, args, line, constraint, constraint_var)

    def parse_statement(self):
        t = self.peek()
        if not t:
            raise SyntaxError("Unexpected EOF while parsing statement")
            
        # 1. Local Var Declaration
        if t.type == 'ID' and t.value in ['let', 'const']:
            is_const = t.value == 'const'
            self.consume('ID')
            name = self.consume('ID').value
            type_expr = None
            if self.match('COLON'):
                type_expr = self.parse_type_expr()
            self.consume('OP') # =
            val = self.parse_expr()
            self.consume('SEMICOLON')
            return VarDecl(is_const, name, type_expr, val, t.line)
            
        # 2. Return statement
        if t.type == 'ID' and t.value == 'return':
            self.consume('ID')
            expr = None
            if self.peek() and self.peek().type != 'SEMICOLON':
                expr = self.parse_expr()
            self.consume('SEMICOLON')
            return ReturnStmt(expr, t.line)
            
        # 3. If Statement
        if t.type == 'ID' and t.value == 'if':
            self.consume('ID')
            self.consume('LPAREN')
            cond = self.parse_expr()
            self.consume('RPAREN')
            
            self.consume('LBRACE')
            then_body = []
            while self.peek() and self.peek().type != 'RBRACE':
                then_body.append(self.parse_statement())
            self.consume('RBRACE')
            
            else_body = []
            if self.match_val('ID', 'else'):
                if self.peek() and self.peek().type == 'LBRACE':
                    self.consume('LBRACE')
                    while self.peek() and self.peek().type != 'RBRACE':
                        else_body.append(self.parse_statement())
                    self.consume('RBRACE')
                else:
                    else_body.append(self.parse_statement())
            return IfStmt(cond, then_body, else_body, t.line)
            
        # 4. For loop
        if t.type == 'ID' and t.value == 'for':
            self.consume('ID')
            var_name = self.consume('ID').value
            self.consume('ID') # in
            start = self.parse_expr()
            self.consume('DOUBLE_DOT')
            end = self.parse_expr()
            
            self.consume('LBRACE')
            body = []
            while self.peek() and self.peek().type != 'RBRACE':
                body.append(self.parse_statement())
            self.consume('RBRACE')
            return ForStmt(var_name, start, end, body, t.line)
            
        # 5. Match Statement
        if t.type == 'ID' and t.value == 'match':
            self.consume('ID')
            expr = self.parse_expr()
            self.consume('LBRACE')
            cases = []
            while self.peek() and self.peek().type != 'RBRACE':
                cases.append(self.parse_match_case())
            self.consume('RBRACE')
            return MatchStmt(expr, cases, t.line)
            
        # 6. Expression statement or Assignment
        expr = self.parse_expr()
        if self.match_val('OP', '='):
            val = self.parse_expr()
            self.consume('SEMICOLON')
            return Assign(expr, val, t.line)
        self.consume('SEMICOLON')
        return ExprStmt(expr, t.line)

    def parse_match_case(self):
        line = self.peek().line if self.peek() else 0
        pattern = self.parse_pattern()
        self.consume('FAT_ARROW')
        self.consume('LBRACE')
        body = []
        while self.peek() and self.peek().type != 'RBRACE':
            body.append(self.parse_statement())
        self.consume('RBRACE')
        return MatchCase(pattern, body, line)

    def parse_pattern(self):
        line = self.peek().line if self.peek() else 0
        name = self.consume('ID').value
        
        # Support patterns like Some(x), None, or Some(x: Matrix[W, Z])
        args = []
        type_bind = None
        
        if self.match('LPAREN'):
            # Check if type bind (e.g. Some(x: Matrix[W, Z]))
            pname = self.consume('ID').value
            if self.match('COLON'):
                type_bind = self.parse_type_expr()
                args.append(pname)
            else:
                args.append(pname)
                while self.match('COMMA'):
                    args.append(self.consume('ID').value)
            self.consume('RPAREN')
            
        return Pattern(name, type_bind, args, line)

    # Precedence climbing parser
    def parse_expr(self):
        return self.parse_logical_or()

    def parse_logical_or(self):
        expr = self.parse_logical_and()
        while self.match_val('OP', '||'):
            op = self.tokens[self.pos-1].value
            right = self.parse_logical_and()
            expr = BinaryExpr(op, expr, right, expr.line)
        return expr

    def parse_logical_and(self):
        expr = self.parse_equality()
        while self.match_val('OP', '&&'):
            op = self.tokens[self.pos-1].value
            right = self.parse_equality()
            expr = BinaryExpr(op, expr, right, expr.line)
        return expr

    def parse_equality(self):
        expr = self.parse_relational()
        while self.peek() and self.peek().type == 'OP' and self.peek().value in ['==', '!=']:
            op = self.consume('OP').value
            right = self.parse_relational()
            expr = BinaryExpr(op, expr, right, expr.line)
        return expr

    def parse_relational(self):
        expr = self.parse_additive()
        while self.peek() and self.peek().type == 'OP' and self.peek().value in ['<', '>', '<=', '>=']:
            op = self.consume('OP').value
            right = self.parse_additive()
            expr = BinaryExpr(op, expr, right, expr.line)
        return expr

    def parse_additive(self):
        expr = self.parse_multiplicative()
        while self.peek() and self.peek().type == 'OP' and self.peek().value in ['+', '-']:
            op = self.consume('OP').value
            right = self.parse_multiplicative()
            expr = BinaryExpr(op, expr, right, expr.line)
        return expr

    def parse_multiplicative(self):
        expr = self.parse_primary_postfix()
        while self.peek() and self.peek().type == 'OP' and self.peek().value in ['*', '/', '%']:
            op = self.consume('OP').value
            right = self.parse_primary_postfix()
            expr = BinaryExpr(op, expr, right, expr.line)
        return expr

    def is_generic_call(self):
        depth = 0
        pos = self.pos
        while pos < len(self.tokens):
            t = self.tokens[pos]
            if t.type == 'LBRACKET':
                depth += 1
            elif t.type == 'RBRACKET':
                depth -= 1
                if depth == 0:
                    # Check if next token is LPAREN
                    if pos + 1 < len(self.tokens) and self.tokens[pos + 1].type == 'LPAREN':
                        return True
                    return False
            pos += 1
        return False

    def parse_primary_postfix(self):
        expr = self.parse_primary()
        while True:
            # Field access: expr.field
            if self.match_val('OP', '.'):
                field = self.consume('ID').value
                expr = FieldExpr(expr, field, expr.line)
            # Function Call: explicit generic function Call: expr[4](...)
            elif self.peek() and self.peek().type == 'LBRACKET' and self.is_generic_call():
                # Generic call insert[4](...)
                self.consume('LBRACKET')
                g_args = []
                while True:
                    if self.peek().type == 'NUMBER':
                        g_args.append(int(self.consume('NUMBER').value))
                    else:
                        g_args.append(self.consume('ID').value)
                    if not self.match('COMMA'):
                        break
                self.consume('RBRACKET')
                self.consume('LPAREN')
                args = []
                if not self.peek() or self.peek().type != 'RPAREN':
                    while True:
                        args.append(self.parse_expr())
                        if not self.match('COMMA'):
                            break
                self.consume('RPAREN')
                expr = CallExpr(expr, args, g_args, expr.line)
            # Array index: expr[index]
            elif self.match('LBRACKET'):
                index = self.parse_expr()
                self.consume('RBRACKET')
                expr = IndexExpr(expr, index, expr.line)
            elif self.match('LPAREN'):
                args = []
                if not self.peek() or self.peek().type != 'RPAREN':
                    while True:
                        args.append(self.parse_expr())
                        if not self.match('COMMA'):
                            break
                self.consume('RPAREN')
                expr = CallExpr(expr, args, [], expr.line)
            else:
                break
        return expr

    def parse_primary(self):
        t = self.peek()
        if not t:
            raise SyntaxError("Unexpected EOF while parsing expression")
            
        if self.match('NUMBER'):
            return Literal(int(self.tokens[self.pos-1].value), 'Integer', t.line)
        if self.match('STRING'):
            return Literal(self.tokens[self.pos-1].value, 'String', t.line)
        if self.match('CHAR'):
            return Literal(self.tokens[self.pos-1].value, 'Char', t.line)
        if self.match_val('ID', 'true'):
            return Literal(True, 'Bool', t.line)
        if self.match_val('ID', 'false'):
            return Literal(False, 'Bool', t.line)
            
        if self.match('ID'):
            return Identifier(self.tokens[self.pos-1].value, t.line)
            
        # Nested expression ( expr )
        if self.match('LPAREN'):
            expr = self.parse_expr()
            self.consume('RPAREN')
            return expr
            
        # List literal [x, y, z]
        if self.match('LBRACKET'):
            elements = []
            if not self.peek() or self.peek().type != 'RBRACKET':
                while True:
                    elements.append(self.parse_expr())
                    if not self.match('COMMA'):
                        break
            self.consume('RBRACKET')
            return ListLiteral(elements, t.line)
            
        raise SyntaxError(f"Unexpected token '{t.value}' of type '{t.type}' at line {t.line}")

# Helper utility to parse source code directly
def parse_code(code):
    tokens = tokenize(code)
    parser = Parser(tokens)
    return parser.parse_program()
