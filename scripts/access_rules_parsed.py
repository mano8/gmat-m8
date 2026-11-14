"""The parsed access rules (plan §3.6), described in ``check_access_rules.py``."""

import ast
from dataclasses import dataclass, field
from pathlib import Path

from access_rules_ast import (
    Imports,
    Module,
    Violations,
    called_by_bare_name,
    called_name,
    calls,
    closure,
    decorator_names,
    imports_of,
    is_services_module,
    name_of,
    python_modules,
    resolve_call,
)

MANAGER_ATTRS = frozenset({"objects", "_default_manager", "_base_manager"})
SHORTCUTS = frozenset({"get_object_or_404", "get_list_or_404"})
SCOPED_QUERYSET_BASE = "ScopedQuerySet"

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

type Function = ast.FunctionDef | ast.AsyncFunctionDef
type Key = tuple[str, str]  # (module, function)


# scoped-model-access ------------------------------------------------------------------


def _subclasses_of(classes: list[ast.ClassDef], root: str) -> set[str]:
    """The classes built on ``root``, at any depth."""
    bases = {node.name: {name_of(base) or "" for base in node.bases} for node in classes}
    return closure({root}, bases) - {root}


def _assignment(statement: ast.stmt) -> tuple[list[ast.expr], ast.expr] | None:
    if isinstance(statement, ast.Assign):
        return statement.targets, statement.value
    if isinstance(statement, ast.AnnAssign) and statement.value is not None:
        return [statement.target], statement.value
    return None


def _declares_manager_of(statement: ast.stmt, querysets: set[str]) -> bool:
    """``objects = SomeScopedQuerySet.as_manager()``, in any of its forms."""
    assignment = _assignment(statement)
    if assignment is None:
        return False
    targets, value = assignment
    if not any(name_of(target) in MANAGER_ATTRS for target in targets):
        return False
    used = {child.id for child in ast.walk(value) if isinstance(child, ast.Name)}
    return bool(used & querysets)


def _scoped_models(modules: list[Module]) -> set[str]:
    """Model classes whose manager is built on a ``ScopedQuerySet`` subclass."""
    classes = [node for module in modules for node in module.classes().values()]
    querysets = _subclasses_of(classes, SCOPED_QUERYSET_BASE)
    return {
        node.name
        for node in classes
        if any(_declares_manager_of(statement, querysets) for statement in node.body)
    }


def _scoped_read(node: ast.AST, scoped: set[str]) -> str | None:
    if (
        isinstance(node, ast.Attribute)
        and node.attr in MANAGER_ATTRS
        and name_of(node.value) in scoped
    ):
        return (
            f"{name_of(node.value)}.{node.attr}: read scoped rows through a selector (visible_to)"
        )
    if (
        isinstance(node, ast.Call)
        and called_name(node) in SHORTCUTS
        and node.args
        and name_of(node.args[0]) in scoped
    ):
        return f"{called_name(node)}({name_of(node.args[0])}, ...): pass a selector's queryset"
    return None


def _check_scoped_access(modules: list[Module], scoped: set[str], out: Violations) -> None:
    for module in modules:
        if not module.reads_through_selectors:
            continue
        for node in ast.walk(module.tree):
            detail = _scoped_read(node, scoped)
            if detail:
                out.add(module, node, "scoped-model-access", detail)


# is_superuser -------------------------------------------------------------------------


def _superuser_lookup(node: ast.AST) -> bool:
    """``filter(is_superuser=True)``, ``filter(is_superuser__in=...)``."""
    return (
        isinstance(node, ast.keyword)
        and node.arg is not None
        and (node.arg == SUPERUSER or node.arg.startswith(f"{SUPERUSER}__"))
    )


def _superuser_by_name(node: ast.AST) -> bool:
    """``getattr(user, "is_superuser")``."""
    return (
        isinstance(node, ast.Call)
        and called_name(node) in {"getattr", "hasattr"}
        and len(node.args) > 1
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == SUPERUSER
    )


def _checks_superuser(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == SUPERUSER:
        return True
    return _superuser_lookup(node) or _superuser_by_name(node)


def _check_superuser(modules: list[Module], out: Violations) -> None:
    for module in modules:
        if module.rel.startswith(ACCESS_PACKAGE):
            continue
        for node in ast.walk(module.tree):
            if _checks_superuser(node):
                out.add(
                    module,
                    node,
                    "is_superuser",
                    "superuser checks belong in apps/access (policy), never in business code",
                )


# service-without-authorize ------------------------------------------------------------


def _writes(node: Function) -> bool:
    if decorator_names(node) & TRANSACTION_DECORATORS:
        return True
    for call in calls(node):
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr in WRITE_METHODS:
            return True
        if isinstance(func, ast.Name) and func.id in WRITE_FUNCTIONS | TRANSACTION_DECORATORS:
            return True
    return False


def _reexports(services: list[Module]) -> dict[Key, Key]:
    """What a services package's ``__init__`` takes from its modules: a caller of
    ``apps.x.services.foo`` reaches the function in the module that defines it."""
    found: dict[Key, Key] = {}
    for module in services:
        if module.rel.endswith("/__init__.py"):
            names = imports_of(module).names
            found |= {(module.name, local): origin for local, origin in names.items()}
    return found


def _callees(
    node: Function, module: Module, imports: Imports, reexports: dict[Key, Key]
) -> set[Key]:
    """The functions ``node`` calls: this module's and any imported one's."""
    defined = module.defs()
    found = {(module.name, name) for name in called_by_bare_name(node) if name in defined}
    for call in calls(node):
        resolved = resolve_call(call, imports)
        if resolved is not None:
            found.add(reexports.get(resolved, resolved))
    return found


@dataclass
class _ServiceFacts:
    """What each function of the services does by itself, and whom it calls."""

    callees: dict[Key, set[Key]] = field(default_factory=dict)
    authorizes: set[Key] = field(default_factory=set)
    writes: set[Key] = field(default_factory=set)


def _service_facts(services: list[Module]) -> _ServiceFacts:
    facts = _ServiceFacts()
    reexports = _reexports(services)
    for module in services:
        imports = imports_of(module)
        for name, node in module.defs().items():
            key = (module.name, name)
            facts.callees[key] = _callees(node, module, imports, reexports)
            if any(called_name(call) == AUTHORIZE for call in calls(node)):
                facts.authorizes.add(key)
            if _writes(node):
                facts.writes.add(key)
    return facts


def _check_services(modules: list[Module], out: Violations) -> None:
    services = [module for module in modules if module.is_services]
    facts = _service_facts(services)
    # Through helpers, across the modules of a package and across services.
    authorizing = closure(facts.authorizes, facts.callees)
    writing = closure(facts.writes, facts.callees)
    for module in services:
        for name, node in sorted(module.defs().items()):
            key = (module.name, name)
            if key in writing and key not in authorizing and not name.startswith("_"):
                out.add(
                    module,
                    node,
                    "service-without-authorize",
                    f"{name}() writes without calling {AUTHORIZE} (make it private if it is a "
                    "helper of an authorized service)",
                )


# get-view-calls-service ---------------------------------------------------------------


def _calls_service(node: ast.AST, imports: Imports) -> bool:
    for call in calls(node):
        resolved = resolve_call(call, imports)
        if resolved is not None and is_services_module(resolved[0]):
            return True
    return False


def _only_post(node: ast.AST) -> bool:
    if not isinstance(node, ast.List | ast.Tuple | ast.Set):
        return False
    values = {elt.value for elt in node.elts if isinstance(elt, ast.Constant)}
    return len(node.elts) == len(values) and {str(v).upper() for v in values} == {"POST"}


def _asks_post_only(call: ast.Call) -> bool:
    return any(
        kw.arg == "post_only" and isinstance(kw.value, ast.Constant) and bool(kw.value.value)
        for kw in call.keywords
    )


def _decorator_is_post_only(decorator: ast.expr) -> bool:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    name = name_of(target)
    if name in POST_ONLY_DECORATORS:
        return True
    if not isinstance(decorator, ast.Call):
        return False
    if name == "require_capability":
        return _asks_post_only(decorator)
    return name == "require_http_methods" and bool(decorator.args) and _only_post(decorator.args[0])


def _post_only_function(node: Function) -> bool:
    return any(_decorator_is_post_only(decorator) for decorator in node.decorator_list)


def _declares_post_only(node: ast.ClassDef) -> bool:
    """A POST-only mixin among its bases, or ``http_method_names`` of POST alone."""
    if any(name_of(base) in POST_ONLY_MIXINS for base in node.bases):
        return True
    return any(
        isinstance(statement, ast.Assign)
        and any(name_of(target) == "http_method_names" for target in statement.targets)
        and _only_post(statement.value)
        for statement in node.body
    )


def _post_only_classes(classes: dict[str, ast.ClassDef]) -> set[str]:
    """Those that declare it, and their local subclasses."""
    bases = {name: {name_of(base) or "" for base in cls.bases} for name, cls in classes.items()}
    return closure({name for name, cls in classes.items() if _declares_post_only(cls)}, bases)


def _check_view_functions(module: Module, reaches: set[str], out: Violations) -> None:
    defs = module.defs()
    for name in sorted(reaches):
        if not name.startswith("_") and not _post_only_function(defs[name]):
            out.add(
                module,
                defs[name],
                "get-view-calls-service",
                f"{name}() calls a service but accepts GET/HEAD: make it POST-only (@mutating)",
            )


def _check_view_classes(
    module: Module, imports: Imports, reaches: set[str], out: Violations
) -> None:
    classes = module.classes()
    post_only = _post_only_classes(classes)
    for name, cls in classes.items():
        calls_service = _calls_service(cls, imports) or bool(called_by_bare_name(cls) & reaches)
        if calls_service and name not in post_only:
            out.add(
                module,
                cls,
                "get-view-calls-service",
                f"{name} calls a service but accepts GET/HEAD: base it on MutatingViewMixin",
            )


def _check_views(modules: list[Module], out: Violations) -> None:
    for module in modules:
        if not module.is_views:
            continue
        imports = imports_of(module)
        defs = module.defs()
        direct = {name for name, node in defs.items() if _calls_service(node, imports)}
        # Through local helpers.
        reaches = closure(direct, {name: called_by_bare_name(node) for name, node in defs.items()})
        _check_view_functions(module, reaches, out)
        _check_view_classes(module, imports, reaches, out)


# plain-model-admin --------------------------------------------------------------------


def _registers_without_admin(call: ast.Call) -> bool:
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "register"
        and name_of(func.value) == "site"
        and len(call.args) == 1
        and not any(kw.arg == "admin_class" for kw in call.keywords)
    )


def _plain_admin(node: ast.AST) -> str | None:
    if isinstance(node, ast.ClassDef) and any(name_of(base) == "ModelAdmin" for base in node.bases):
        return f"{node.name} must be built on ReadOnlyModelAdmin (apps.core.admin)"
    if isinstance(node, ast.Call) and _registers_without_admin(node):
        return "site.register(Model) without an admin class uses a plain ModelAdmin"
    return None


def _check_admin_classes(modules: list[Module], out: Violations) -> None:
    for module in modules:
        for node in ast.walk(module.tree):
            detail = _plain_admin(node)
            if detail:
                out.add(module, node, "plain-model-admin", detail)


# editable-admin ------------------------------------------------------------------------

READ_ONLY_ADMIN = "ReadOnlyModelAdmin"
# The bases a class of an ``admin.py`` may have: the read-only admin itself (never a subclass
# of it, so nothing in between can turn writing back on) and a list filter.
ADMIN_MODULE_BASES = frozenset({READ_ONLY_ADMIN, "SimpleListFilter"})
# The same tuple as ``apps.core.admin.READ_ONLY_METHODS`` (a test compares them), plus
# ``inlines``: an inline is a second form that saves.
READ_ONLY_METHODS = (
    "has_add_permission",
    "has_change_permission",
    "has_delete_permission",
    "save_model",
    "save_related",
    "save_formset",
    "delete_model",
    "delete_queryset",
    "get_actions",
    "get_urls",
    "changeform_view",
)
ADMIN_WRITE_NAMES = frozenset({*READ_ONLY_METHODS, "inlines"})


def _defined_names(node: ast.ClassDef) -> set[str]:
    names: set[str] = set()
    for item in node.body:
        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(item.name)
        elif isinstance(item, ast.Assign):
            names.update(name for target in item.targets if (name := name_of(target)))
        elif isinstance(item, ast.AnnAssign) and (name := name_of(item.target)):
            names.add(name)
    return names


def _editable_admin(module: Module, node: ast.ClassDef) -> str | None:
    bases = {name_of(base) for base in node.bases}
    if "ModelAdmin" in bases:
        return None  # plain-model-admin reports it
    if module.parts[-1] == "admin.py" and not bases <= ADMIN_MODULE_BASES:
        return f"{node.name} must be built on {READ_ONLY_ADMIN} itself (ADR-0030)"
    redefined = sorted(_defined_names(node) & ADMIN_WRITE_NAMES)
    if READ_ONLY_ADMIN in bases and redefined:
        return f"{node.name} redefines {', '.join(redefined)}: the admin is read-only (ADR-0030)"
    return None


def _check_editable_admins(modules: list[Module], out: Violations) -> None:
    for module in modules:
        if not module.rel.startswith("apps/"):
            continue
        for node in ast.walk(module.tree):
            if isinstance(node, ast.ClassDef) and (detail := _editable_admin(module, node)):
                out.add(module, node, "editable-admin", detail)


# principal-constructor ----------------------------------------------------------------


def constructor_modules(root: Path, out: list[str]) -> dict[str, frozenset[str]]:
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
        if name_of(target) == CONSTRUCTOR_REGISTRY:
            registry = _literal_registry(value)
            if registry is not None:
                return registry
            break
    out.append(
        f"{PRINCIPALS_FILE}:1: principal-constructor: {CONSTRUCTOR_REGISTRY} must be a literal "
        "dict of frozenset({...}) values"
    )
    return {name: frozenset() for name in RESTRICTED_PRINCIPALS}


def _literal_names(item: ast.expr) -> frozenset[str] | None:
    """The strings of ``frozenset({"a", "b"})``; ``None`` for anything else."""
    if not (isinstance(item, ast.Call) and name_of(item.func) == "frozenset"):
        return None
    if len(item.args) != 1 or not isinstance(item.args[0], ast.Set):
        return None
    values = [elt.value if isinstance(elt, ast.Constant) else None for elt in item.args[0].elts]
    if not all(isinstance(value, str) for value in values):
        return None
    return frozenset(str(value) for value in values)


def _literal_registry(value: ast.expr | None) -> dict[str, frozenset[str]] | None:
    if not isinstance(value, ast.Dict):
        return None
    registry: dict[str, frozenset[str]] = {}
    for key, item in zip(value.keys, value.values, strict=True):
        names = _literal_names(item)
        if names is None or not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            return None
        registry[key.value] = names
    return registry


def _within(module: str, allowed: frozenset[str]) -> bool:
    return any(module == name or module.startswith(f"{name}.") for name in allowed)


def _check_principals(
    modules: list[Module], registry: dict[str, frozenset[str]], out: Violations
) -> None:
    for module in modules:
        if module.rel == PRINCIPALS_FILE:
            continue
        for call in calls(module.tree):
            name = called_name(call)
            if name in registry and not _within(module.name, registry[name]):
                out.add(
                    module,
                    call,
                    "principal-constructor",
                    f"{name} may only be constructed in {', '.join(sorted(registry[name]))}",
                )


# mail-outside-outbox ------------------------------------------------------------------


def _imports_mail_module(node: ast.ImportFrom) -> bool:
    module = node.module or ""
    if module.split(".")[0] == "smtplib":
        return True
    return module.startswith("django.core.mail") and any(
        alias.name in MAIL_SENDERS for alias in node.names
    )


def _mail_use(node: ast.AST) -> str | None:
    """What in ``node`` sends mail, as the report names it."""
    if isinstance(node, ast.Import):
        smtp = any(alias.name.split(".")[0] == "smtplib" for alias in node.names)
        return "smtplib" if smtp else None
    if isinstance(node, ast.ImportFrom):
        return node.module if _imports_mail_module(node) else None
    if isinstance(node, ast.Call) and called_name(node) in MAIL_SENDERS:
        return f"{called_name(node)}()"
    return None


def _check_mail(modules: list[Module], out: Violations) -> None:
    for module in modules:
        if _within(module.name, MAIL_MODULES):
            continue
        for node in ast.walk(module.tree):
            use = _mail_use(node)
            if use:
                out.add(
                    module,
                    node,
                    "mail-outside-outbox",
                    f"{use}: mail goes through EmailOutbox and the worker (plan §5)",
                )


def check_python(root: Path, allowlist: frozenset[tuple[str, str]]) -> list[str]:
    """The parsed rules over ``apps/`` and ``config/``."""
    problems: list[str] = []
    modules = python_modules(root, problems)
    registry = constructor_modules(root, problems)
    out = Violations(allowlist)
    _check_scoped_access(modules, _scoped_models(modules), out)
    _check_superuser(modules, out)
    _check_services(modules, out)
    _check_views(modules, out)
    _check_admin_classes(modules, out)
    _check_editable_admins(modules, out)
    _check_principals(modules, registry, out)
    _check_mail(modules, out)
    return [*problems, *out.found]
