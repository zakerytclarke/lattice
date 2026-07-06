# Compilation error collection and reporting

from compiler.errors import format_compilation_error


class CompilationDiagnostics:
    def __init__(self):
        self.errors = []

    def add(self, error, file_name=None, source_lines=None):
        if file_name and not getattr(error, "file_name", None):
            error.file_name = file_name
        if source_lines and not getattr(error, "source_lines", None):
            error.source_lines = source_lines
        self.errors.append(error)

    def has_errors(self):
        return len(self.errors) > 0

    def format_all(self):
        if not self.errors:
            return ""
        parts = []
        for i, error in enumerate(self.errors, 1):
            formatted = format_compilation_error(
                error,
                getattr(error, "file_name", None),
                getattr(error, "source_lines", None),
            )
            if len(self.errors) > 1:
                parts.append(f"--- error {i} of {len(self.errors)} ---\n{formatted}")
            else:
                parts.append(formatted)
        return "\n\n".join(parts)
