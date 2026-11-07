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

The parsed rules are in ``access_rules_parsed.py``, on what ``access_rules_ast.py`` reads.
"""

import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:  # loaded by path (the tests), not run as a script
    sys.path.append(str(HERE))

from access_rules_ast import code_files  # noqa: E402
from access_rules_parsed import check_python, constructor_modules  # noqa: E402, F401

ROOT = HERE.parent

TEMPLATE_SUFFIXES = {".html", ".txt", ".xml"}
YAML_SUFFIXES = {".yaml", ".yml"}

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
        # The single upload entry point: admission must run before CSRF parses the body
        # (plan §5 "Attachments", F73); its inner view is csrf_protect.
        ("apps/attachments/views.py", "csrf_exempt"),
    }
)


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


def check(root: Path = ROOT, allowlist: frozenset[tuple[str, str]] = ALLOWLIST) -> list[str]:
    """Return every violation under ``root``."""
    templates = [path for path in code_files(root) if path.suffix in TEMPLATE_SUFFIXES]
    return [
        *_scan(code_files(root), CODE_RULES, root, allowlist),
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
