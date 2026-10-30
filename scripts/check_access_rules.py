#!/usr/bin/env python
"""Static guard for the access rules of plan §3.6 and the banned constructs of CLAUDE.md.

Text rules (any code file or template):

- ``|safe``, ``mark_safe``, ``csrf_exempt``, ``.raw(`` and ``.extra(``;
- ``is_superuser`` in a template;
- in ``.github/workflows``, the ``pull_request_target`` and ``workflow_run`` triggers.

Python rules (parsed, so comments and docstrings never match):

- ``scoped-model-access``: ``<ScopedModel>.objects`` (or ``get_object_or_404(<ScopedModel>``)
  in views, forms and template tags; a scoped model is one whose manager is built on
  ``ScopedQuerySet``. They read through ``visible_to`` in selectors instead;
- ``is_superuser``: a superuser check outside ``apps/access`` (attribute, lookup, keyword);
- ``service-without-authorize``: a public service function that writes (``@transactional``
  or an ORM write) without reaching ``authorize_in_txn``, directly or through its helpers;
- ``get-view-calls-service``: a view that reaches a service without being POST-only
  (``@mutating``, ``@require_POST``, ``post_only=True``, ``MutatingViewMixin``) (F66);
- ``plain-model-admin``: a ``ModelAdmin`` not built on the service-backed base (F60), or
  ``site.register(Model)`` without an admin class;
- ``principal-constructor``: a token, system or operator principal constructed outside
  the modules of ``apps.access.principals.CONSTRUCTOR_MODULES`` (read from that file);
- ``mail-outside-outbox``: SMTP or Django's mail-sending API outside the outbox worker.

Exit status 1 lists every violation. An allowlist entry names one file and one rule and
needs a reviewer's approval in the PR that adds it. The runtime keeps its own checks
(``gmat.E005`` for admin classes, the principals' constructor check), so a construct this
script cannot see statically is still refused.
"""

import ast
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CODE_DIRS = ("apps", "config", "templates")
CODE_SUFFIXES = {".py", ".html", ".txt", ".xml"}
TEMPLATE_SUFFIXES = {".html", ".txt", ".xml"}
YAML_SUFFIXES = {".yaml", ".yml"}
SKIP_PARTS = {"migrations", "__pycache__"}

# rule name -> pattern
CODE_RULES: dict[str, re.Pattern[str]] = {
    "safe-filter": re.compile(r"\|\s*safe\b"),
    "mark_safe": re.compile(r"\bmark_safe\b"),
    "csrf_exempt": re.compile(r"\bcsrf_exempt\b"),
    "raw-sql": re.compile(r"\.raw\s*\("),
    "extra-sql": re.compile(r"\.extra\s*\("),
}
TEMPLATE_RULES: dict[str, re.Pattern[str]] = {
    "is_superuser": re.compile(r"\bis_superuser\b"),
}

# A trigger may appear as a key, in a list or in a flow mapping, so match the bare word on
# any non-comment line.
WORKFLOW_RULES: dict[str, re.Pattern[str]] = {
    "pull_request_target": re.compile(r"(?<![\w-])pull_request_target(?![\w-])"),
    "workflow_run": re.compile(r"(?<![\w-])workflow_run(?![\w-])"),
}
YAML_COMMENT = re.compile(r"#.*$", re.MULTILINE)

# (posix path relative to the repository root, rule) pairs that are allowed.
ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        # The service-backed base itself (plan §5, "Django Admin").
        ("apps/core/admin.py", "plain-model-admin"),
    }
)

# --- Python rule parameters -----------------------------------------------------------

MANAGER_ATTRS = frozenset({"objects", "_default_manager", "_base_manager"})
SHORTCUTS = frozenset({"get_object_or_404", "get_list_or_404"})
SCOPED_QUERYSET_BASE = "ScopedQuerySet"
# Directories (under ``apps/<app>/``) and modules where scoped models are read only
# through selectors.
SCOPED_READ_PACKAGES = frozenset({"views", "forms", "templatetags"})

SUPERUSER = "is_superuser"
ACCESS_PACKAGE = "apps/access/"

AUTHORIZE = "authorize_in_txn"
TRANSACTION_DECORATORS = frozenset({"transactional", "run_in_transaction"})
WRITE_METHODS = frozenset(
    {
        "save",
        "create",
        "bulk_create",
        "bulk_update",
        "update",
        "update_or_create",
        "get_or_create",
        "delete",
        "create_user",
        "create_superuser",
        "set_password",
    }
)
WRITE_FUNCTIONS = frozenset({"apply_changes"})

POST_ONLY_DECORATORS = frozenset({"mutating", "require_POST"})
POST_ONLY_MIXINS = frozenset({"MutatingViewMixin"})

PRINCIPALS_FILE = "apps/access/principals.py"
CONSTRUCTOR_REGISTRY = "CONSTRUCTOR_MODULES"
# Used when the registry file does not exist: nothing may construct them (fail closed).
RESTRICTED_PRINCIPALS = ("TokenPrincipal", "SystemPrincipal", "OperatorPrincipal")

MAIL_MODULES = frozenset({"apps.notifications.worker"})
MAIL_SENDERS = frozenset(
    {
        "send_mail",
        "send_mass_mail",
        "mail_admins",
        "mail_managers",
        "EmailMessage",
        "EmailMultiAlternatives",
        "get_connection",
    }
)


# --- text rules -----------------------------------------------------------------------


def _code_files(root: Path) -> Iterator[Path]:
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


def _workflow_files(root: Path) -> Iterator[Path]:
    base = root / ".github" / "workflows"
    if base.is_dir():
        yield from sorted([*base.glob("*.yaml"), *base.glob("*.yml")])


def _scan(
    paths: Iterable[Path],
    rules: dict[str, re.Pattern[str]],
    root: Path,
    allowlist: frozenset[tuple[str, str]],
) -> list[str]:
    violations: list[str] = []
    for path in paths:
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix in YAML_SUFFIXES:
            # Blank the comment but keep the line count so reported line numbers stay right.
            text = YAML_COMMENT.sub("", text)
        for rule, pattern in rules.items():
            if (rel, rule) in allowlist:
                continue
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                violations.append(f"{rel}:{line}: banned construct '{rule}'")
    return violations


# --- Python rules ---------------------------------------------------------------------


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


def _python_modules(root: Path, violations: list[str]) -> list[Module]:
    modules: list[Module] = []
    for path in _code_files(root):
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


def _name(node: ast.AST) -> str | None:
    """The last name of ``a``, ``a.b`` or ``a[b]`` (``ScopedQuerySet["Branch"]``)."""
    if isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _called_name(call: ast.Call) -> str | None:
    return _name(call.func)


def _calls(node: ast.AST) -> Iterator[ast.Call]:
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            yield child


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = _name(target)
        if name:
            names.add(name)
    return names


# scoped-model-access ------------------------------------------------------------------


def _scoped_models(modules: list[Module]) -> set[str]:
    """Model classes whose manager is built on a ``ScopedQuerySet`` subclass."""
    classes = [node for module in modules for node in module.classes().values()]
    querysets = {SCOPED_QUERYSET_BASE}
    changed = True
    while changed:  # subclasses of subclasses
        changed = False
        for node in classes:
            if node.name not in querysets and any(_name(b) in querysets for b in node.bases):
                querysets.add(node.name)
                changed = True
    querysets.discard(SCOPED_QUERYSET_BASE)
    models: set[str] = set()
    for node in classes:
        for statement in node.body:
            targets: list[ast.expr] = []
            if isinstance(statement, ast.Assign):
                targets, value = statement.targets, statement.value
            elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
                targets, value = [statement.target], statement.value
            else:
                continue
            names = {_name(target) for target in targets}
            if not MANAGER_ATTRS.intersection(n for n in names if n):
                continue
            used = {child.id for child in ast.walk(value) if isinstance(child, ast.Name)}
            if used & querysets:
                models.add(node.name)
    return models


def _check_scoped_access(modules: list[Module], scoped: set[str], out: Violations) -> None:
    for module in modules:
        if not module.reads_through_selectors:
            continue
        for node in ast.walk(module.tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in MANAGER_ATTRS
                and _name(node.value) in scoped
            ):
                out.add(
                    module,
                    node,
                    "scoped-model-access",
                    f"{_name(node.value)}.{node.attr}: read scoped rows through a selector "
                    "(visible_to)",
                )
            elif (
                isinstance(node, ast.Call)
                and _called_name(node) in SHORTCUTS
                and node.args
                and _name(node.args[0]) in scoped
            ):
                out.add(
                    module,
                    node,
                    "scoped-model-access",
                    f"{_called_name(node)}({_name(node.args[0])}, ...): pass a selector's queryset",
                )


# is_superuser -------------------------------------------------------------------------


def _check_superuser(modules: list[Module], out: Violations) -> None:
    for module in modules:
        if module.rel.startswith(ACCESS_PACKAGE):
            continue
        for node in ast.walk(module.tree):
            hit = (
                (isinstance(node, ast.Attribute) and node.attr == SUPERUSER)
                or (
                    isinstance(node, ast.keyword)
                    and node.arg is not None
                    and (node.arg == SUPERUSER or node.arg.startswith(f"{SUPERUSER}__"))
                )
                or (
                    isinstance(node, ast.Call)
                    and _called_name(node) in {"getattr", "hasattr"}
                    and len(node.args) > 1
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value == SUPERUSER
                )
            )
            if hit:
                out.add(
                    module,
                    node,
                    "is_superuser",
                    "superuser checks belong in apps/access (policy), never in business code",
                )


# imports ------------------------------------------------------------------------------


@dataclass
class Imports:
    """What a module imported: names from modules, and module aliases."""

    names: dict[str, tuple[str, str]] = field(default_factory=dict)  # local -> (module, name)
    modules: dict[str, str] = field(default_factory=dict)  # local alias -> module


def _imports(module: Module) -> Imports:
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


def _is_services_module(dotted: str) -> bool:
    return dotted.startswith("apps.") and (dotted.endswith(".services") or ".services." in dotted)


def _resolve_call(call: ast.Call, imports: Imports) -> tuple[str, str] | None:
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


# service-without-authorize ------------------------------------------------------------


def _check_services(modules: list[Module], out: Violations) -> None:
    services = [module for module in modules if module.is_services]
    authorizing: set[tuple[str, str]] = set()
    facts: dict[tuple[str, str], tuple[bool, set[str], set[tuple[str, str]]]] = {}
    for module in services:
        imports = _imports(module)
        for name, node in module.defs().items():
            direct = False
            local: set[str] = set()
            external: set[tuple[str, str]] = set()
            for call in _calls(node):
                called = _called_name(call)
                if called == AUTHORIZE:
                    direct = True
                elif isinstance(call.func, ast.Name) and called in module.defs():
                    local.add(called)
                resolved = _resolve_call(call, imports)
                if resolved is not None:
                    external.add(resolved)
            facts[module.name, name] = (direct, local, external)
            if direct:
                authorizing.add((module.name, name))
    changed = True
    while changed:  # through helpers, also across service modules
        changed = False
        for (module_name, name), (_, local, external) in facts.items():
            key = (module_name, name)
            if key in authorizing:
                continue
            callees = {(module_name, callee) for callee in local} | external
            if callees & authorizing:
                authorizing.add(key)
                changed = True
    for module in services:
        defs = module.defs()
        writes = {name for name, node in defs.items() if _writes(node)}
        changed = True
        while changed:
            changed = False
            for name, node in defs.items():
                if name in writes:
                    continue
                if any(
                    isinstance(call.func, ast.Name) and call.func.id in writes
                    for call in _calls(node)
                ):
                    writes.add(name)
                    changed = True
        for name in sorted(writes):
            if name.startswith("_") or (module.name, name) in authorizing:
                continue
            out.add(
                module,
                defs[name],
                "service-without-authorize",
                f"{name}() writes without calling {AUTHORIZE} (make it private if it is a "
                "helper of an authorized service)",
            )


def _writes(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    if _decorator_names(node) & TRANSACTION_DECORATORS:
        return True
    for call in _calls(node):
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr in WRITE_METHODS:
            return True
        if isinstance(func, ast.Name) and func.id in WRITE_FUNCTIONS | TRANSACTION_DECORATORS:
            return True
    return False


# get-view-calls-service ---------------------------------------------------------------


def _calls_service(node: ast.AST, imports: Imports) -> bool:
    for call in _calls(node):
        resolved = _resolve_call(call, imports)
        if resolved is not None and _is_services_module(resolved[0]):
            return True
    return False


def _post_only_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = _name(target)
        if name in POST_ONLY_DECORATORS:
            return True
        if not isinstance(decorator, ast.Call):
            continue
        if name == "require_capability" and any(
            kw.arg == "post_only" and isinstance(kw.value, ast.Constant) and kw.value.value
            for kw in decorator.keywords
        ):
            return True
        if name == "require_http_methods" and decorator.args and _only_post(decorator.args[0]):
            return True
    return False


def _only_post(node: ast.AST) -> bool:
    if not isinstance(node, ast.List | ast.Tuple | ast.Set):
        return False
    values = {elt.value for elt in node.elts if isinstance(elt, ast.Constant)}
    return len(node.elts) == len(values) and {str(v).upper() for v in values} == {"POST"}


def _post_only_class(node: ast.ClassDef, post_only: set[str]) -> bool:
    if any(_name(base) in POST_ONLY_MIXINS | post_only for base in node.bases):
        return True
    for statement in node.body:
        if (
            isinstance(statement, ast.Assign)
            and any(_name(t) == "http_method_names" for t in statement.targets)
            and _only_post(statement.value)
        ):
            return True
    return False


def _check_views(modules: list[Module], out: Violations) -> None:
    for module in modules:
        if not module.is_views:
            continue
        imports = _imports(module)
        defs = module.defs()
        reaches = {name for name, node in defs.items() if _calls_service(node, imports)}
        changed = True
        while changed:  # through local helpers
            changed = False
            for name, node in defs.items():
                if name not in reaches and any(
                    isinstance(call.func, ast.Name) and call.func.id in reaches
                    for call in _calls(node)
                ):
                    reaches.add(name)
                    changed = True
        for name in sorted(reaches):
            node = defs[name]
            if not name.startswith("_") and not _post_only_function(node):
                out.add(
                    module,
                    node,
                    "get-view-calls-service",
                    f"{name}() calls a service but accepts GET/HEAD: make it POST-only (@mutating)",
                )
        classes = module.classes()
        post_only: set[str] = set()
        changed = True
        while changed:  # local subclasses of POST-only views
            changed = False
            for name, cls in classes.items():
                if name not in post_only and _post_only_class(cls, post_only):
                    post_only.add(name)
                    changed = True
        for name, cls in classes.items():
            calls_service = _calls_service(cls, imports) or any(
                isinstance(call.func, ast.Name) and call.func.id in reaches for call in _calls(cls)
            )
            if calls_service and name not in post_only:
                out.add(
                    module,
                    cls,
                    "get-view-calls-service",
                    f"{name} calls a service but accepts GET/HEAD: base it on MutatingViewMixin",
                )


# plain-model-admin --------------------------------------------------------------------


def _check_admin_classes(modules: list[Module], out: Violations) -> None:
    for module in modules:
        for node in ast.walk(module.tree):
            if isinstance(node, ast.ClassDef) and any(
                _name(base) == "ModelAdmin" for base in node.bases
            ):
                out.add(
                    module,
                    node,
                    "plain-model-admin",
                    f"{node.name} must be built on ServiceBackedModelAdmin "
                    "(apps.core.admin) or one of its subclasses",
                )
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "register"
                and _name(node.func.value) == "site"
                and len(node.args) == 1
                and not any(kw.arg == "admin_class" for kw in node.keywords)
            ):
                out.add(
                    module,
                    node,
                    "plain-model-admin",
                    "site.register(Model) without an admin class uses a plain ModelAdmin",
                )


# principal-constructor ----------------------------------------------------------------


def _constructor_modules(root: Path, out: list[str]) -> dict[str, frozenset[str]]:
    path = root / PRINCIPALS_FILE
    if not path.is_file():
        return {name: frozenset() for name in RESTRICTED_PRINCIPALS}
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=PRINCIPALS_FILE)
    for node in tree.body:
        target: ast.expr
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        else:
            continue
        if _name(target) == CONSTRUCTOR_REGISTRY:
            registry = _literal_registry(value)
            if registry is not None:
                return registry
            break
    out.append(
        f"{PRINCIPALS_FILE}:1: principal-constructor: {CONSTRUCTOR_REGISTRY} must be a literal "
        "dict of frozenset({...}) values"
    )
    return {name: frozenset() for name in RESTRICTED_PRINCIPALS}


def _literal_registry(value: ast.expr | None) -> dict[str, frozenset[str]] | None:
    if not isinstance(value, ast.Dict):
        return None
    registry: dict[str, frozenset[str]] = {}
    for key, item in zip(value.keys, value.values, strict=True):
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            return None
        if not (
            isinstance(item, ast.Call)
            and _name(item.func) == "frozenset"
            and len(item.args) == 1
            and isinstance(item.args[0], ast.Set)
            and all(
                isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                for elt in item.args[0].elts
            )
        ):
            return None
        registry[key.value] = frozenset(
            str(elt.value) for elt in item.args[0].elts if isinstance(elt, ast.Constant)
        )
    return registry


def _within(module: str, allowed: frozenset[str]) -> bool:
    return any(module == name or module.startswith(f"{name}.") for name in allowed)


def _check_principals(
    modules: list[Module], registry: dict[str, frozenset[str]], out: Violations
) -> None:
    for module in modules:
        if module.rel == PRINCIPALS_FILE:
            continue
        for call in _calls(module.tree):
            name = _called_name(call)
            if name in registry and not _within(module.name, registry[name]):
                out.add(
                    module,
                    call,
                    "principal-constructor",
                    f"{name} may only be constructed in {', '.join(sorted(registry[name]))}",
                )


# mail-outside-outbox ------------------------------------------------------------------


def _check_mail(modules: list[Module], out: Violations) -> None:
    detail = "mail goes through EmailOutbox and the worker (plan §5)"
    for module in modules:
        if _within(module.name, MAIL_MODULES):
            continue
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Import) and any(
                alias.name.split(".")[0] == "smtplib" for alias in node.names
            ):
                out.add(module, node, "mail-outside-outbox", f"smtplib: {detail}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] == "smtplib" or (
                    node.module.startswith("django.core.mail")
                    and any(alias.name in MAIL_SENDERS for alias in node.names)
                ):
                    out.add(module, node, "mail-outside-outbox", f"{node.module}: {detail}")
            elif isinstance(node, ast.Call) and _called_name(node) in MAIL_SENDERS:
                out.add(module, node, "mail-outside-outbox", f"{_called_name(node)}(): {detail}")


# --- entry points ---------------------------------------------------------------------


def check_python(root: Path, allowlist: frozenset[tuple[str, str]]) -> list[str]:
    """The parsed rules over ``apps/`` and ``config/``."""
    problems: list[str] = []
    modules = _python_modules(root, problems)
    registry = _constructor_modules(root, problems)
    out = Violations(allowlist)
    _check_scoped_access(modules, _scoped_models(modules), out)
    _check_superuser(modules, out)
    _check_services(modules, out)
    _check_views(modules, out)
    _check_admin_classes(modules, out)
    _check_principals(modules, registry, out)
    _check_mail(modules, out)
    return [*problems, *out.found]


def check(root: Path = ROOT, allowlist: frozenset[tuple[str, str]] = ALLOWLIST) -> list[str]:
    """Return every violation under ``root``."""
    templates = [path for path in _code_files(root) if path.suffix in TEMPLATE_SUFFIXES]
    return [
        *_scan(_code_files(root), CODE_RULES, root, allowlist),
        *_scan(templates, TEMPLATE_RULES, root, allowlist),
        *check_python(root, allowlist),
        *_scan(_workflow_files(root), WORKFLOW_RULES, root, allowlist),
    ]


def main() -> int:
    violations = check()
    for violation in violations:
        print(violation, file=sys.stderr)
    if violations:
        print(f"{len(violations)} violation(s)", file=sys.stderr)
        return 1
    print("check_access_rules: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
