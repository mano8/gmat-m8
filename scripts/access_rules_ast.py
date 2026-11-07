"""What the parsed access rules read: the code's modules, their imports and calls.

``access_rules_parsed`` holds the rules; ``check_access_rules.py`` is the entry point.
"""

import ast
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

CODE_DIRS = ("apps", "config", "templates")
CODE_SUFFIXES = {".py", ".html", ".txt", ".xml"}
SKIP_PARTS = {"migrations", "__pycache__"}
# Directories (under ``apps/<app>/``) and modules where scoped models are read only
# through selectors.
SCOPED_READ_PACKAGES = frozenset({"views", "forms", "templatetags"})


def code_files(root: Path) -> Iterator[Path]:
    for directory in CODE_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if (
                path.is_file()
                and path.suffix in CODE_SUFFIXES
                and not SKIP_PARTS.intersection(path.parts)
            ):
                yield path


@dataclass
class Module:
    """One parsed Python file."""

    rel: str
    name: str
    tree: ast.Module

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(self.rel.split("/"))

    @property
    def is_services(self) -> bool:
        return self.rel.startswith("apps/") and (
            self.rel.endswith("/services.py") or "/services/" in self.rel
        )

    @property
    def is_views(self) -> bool:
        return self.rel.startswith("apps/") and (
            self.rel.endswith("/views.py") or "/views/" in self.rel
        )

    @property
    def reads_through_selectors(self) -> bool:
        """Views, forms and template tags (plan §3.6)."""
        if not self.rel.startswith("apps/"):
            return False
        stem = self.parts[-1].removesuffix(".py")
        return stem in SCOPED_READ_PACKAGES or bool(
            SCOPED_READ_PACKAGES.intersection(self.parts[2:-1])
        )

    def defs(self) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
        return {
            node.name: node
            for node in self.tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }

    def classes(self) -> dict[str, ast.ClassDef]:
        return {node.name: node for node in self.tree.body if isinstance(node, ast.ClassDef)}


@dataclass
class Violations:
    allowlist: frozenset[tuple[str, str]]
    found: list[str] = field(default_factory=list)

    def add(self, module: Module, node: ast.AST, rule: str, detail: str) -> None:
        if (module.rel, rule) in self.allowlist:
            return
        line = getattr(node, "lineno", 1)
        self.found.append(f"{module.rel}:{line}: {rule}: {detail}")


def _module_name(rel: str) -> str:
    dotted = rel.removesuffix(".py").replace("/", ".")
    return dotted.removesuffix(".__init__")


def python_modules(root: Path, violations: list[str]) -> list[Module]:
    modules: list[Module] = []
    for path in code_files(root):
        if path.suffix != ".py":
            continue
        rel = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except SyntaxError as exc:
            violations.append(f"{rel}:{exc.lineno or 1}: syntax-error: cannot parse")
            continue
        modules.append(Module(rel, _module_name(rel), tree))
    return modules


def name_of(node: ast.AST) -> str | None:
    """The last name of ``a``, ``a.b`` or ``a[b]`` (``ScopedQuerySet["Branch"]``)."""
    if isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def called_name(call: ast.Call) -> str | None:
    return name_of(call.func)


def calls(node: ast.AST) -> Iterator[ast.Call]:
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            yield child


def called_by_bare_name(node: ast.AST) -> set[str]:
    """The names ``node`` calls as ``name()``: this module's helpers, or what it imported."""
    return {call.func.id for call in calls(node) if isinstance(call.func, ast.Name)}


def decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = name_of(target)
        if name:
            names.add(name)
    return names


def closure[T](found: set[T], reaches: dict[T, set[T]]) -> set[T]:
    """``found`` and everything that reaches it, through any number of steps of ``reaches``."""
    grown = set(found)
    while more := {key for key, targets in reaches.items() if key not in grown and targets & grown}:
        grown |= more
    return grown


@dataclass
class Imports:
    """What a module imported: names from modules, and module aliases."""

    names: dict[str, tuple[str, str]] = field(default_factory=dict)  # local -> (module, name)
    modules: dict[str, str] = field(default_factory=dict)  # local alias -> module


def imports_of(module: Module) -> Imports:
    found = Imports()
    for node in ast.walk(module.tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                local = alias.asname or alias.name
                found.names[local] = (node.module, alias.name)
                found.modules[local] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    found.modules[alias.asname] = alias.name
    return found


def is_services_module(dotted: str) -> bool:
    return dotted.startswith("apps.") and (dotted.endswith(".services") or ".services." in dotted)


def resolve_call(call: ast.Call, imports: Imports) -> tuple[str, str] | None:
    """``(module, function)`` of a call to an imported function, if it is one."""
    func = call.func
    if isinstance(func, ast.Name) and func.id in imports.names:
        return imports.names[func.id]
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id in imports.modules
    ):
        return imports.modules[func.value.id], func.attr
    return None
