import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_access_rules.py"


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_access_rules", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_repository_is_clean(script: ModuleType) -> None:
    assert script.check() == []


@pytest.mark.parametrize(
    ("rel", "text", "rule"),
    [
        ("apps/x/views.py", "from django.utils.safestring import mark_safe\n", "mark_safe"),
        ("apps/x/views.py", "@csrf_exempt\ndef v(r): ...\n", "csrf_exempt"),
        ("apps/x/selectors.py", "Model.objects.raw('select 1')\n", "raw-sql"),
        ("apps/x/selectors.py", "qs.extra(select={})\n", "extra-sql"),
        ("templates/a.html", "{{ value|safe }}\n", "safe-filter"),
        ("templates/a.html", "{{ value | safe }}\n", "safe-filter"),
        (".github/workflows/ci.yaml", "on:\n  pull_request_target:\n", "pull_request_target"),
        (".github/workflows/ci.yml", "on:\n  workflow_run:\n    workflows: [x]\n", "workflow_run"),
        (".github/workflows/ci.yml", "on: pull_request_target\n", "pull_request_target"),
        (".github/workflows/ci.yml", "on: [push, pull_request_target]\n", "pull_request_target"),
        (".github/workflows/ci.yml", "on: {workflow_run: {}}\n", "workflow_run"),
        (".github/workflows/ci.yml", 'on:\n  "pull_request_target":\n', "pull_request_target"),
    ],
)
def test_banned_construct_is_reported(
    script: ModuleType, tmp_path: Path, rel: str, text: str, rule: str
) -> None:
    _write(tmp_path, rel, text)
    violations = script.check(tmp_path, frozenset())
    assert len(violations) == 1
    assert rel in violations[0]
    assert rule in violations[0]


def test_allowlist_exempts_one_file_and_rule(script: ModuleType, tmp_path: Path) -> None:
    _write(tmp_path, "apps/x/upload.py", "@csrf_exempt\ndef up(r): ...\n")
    allowed = frozenset({("apps/x/upload.py", "csrf_exempt")})
    assert script.check(tmp_path, allowed) == []
    _write(tmp_path, "apps/x/other.py", "@csrf_exempt\ndef up(r): ...\n")
    assert len(script.check(tmp_path, allowed)) == 1


def test_migrations_and_other_directories_are_not_scanned(
    script: ModuleType, tmp_path: Path
) -> None:
    _write(tmp_path, "apps/x/migrations/0001.py", "mark_safe\n")
    _write(tmp_path, "tests/x.py", "mark_safe\n")
    _write(tmp_path, "docs/a.md", "|safe\n")
    assert script.check(tmp_path, frozenset()) == []


def test_workflow_comments_and_similar_names_are_not_violations(
    script: ModuleType, tmp_path: Path
) -> None:
    _write(
        tmp_path,
        ".github/workflows/ci.yaml",
        "# never use pull_request_target here\non:\n  pull_request: {}\n"
        "env:\n  NOTE: workflow_run_foo\n",
    )
    assert script.check(tmp_path, frozenset()) == []


def test_workflow_violation_reports_the_right_line(script: ModuleType, tmp_path: Path) -> None:
    _write(tmp_path, ".github/workflows/ci.yaml", "# c\non:\n  pull_request_target:\n")
    assert script.check(tmp_path, frozenset()) == [
        ".github/workflows/ci.yaml:3: banned construct 'pull_request_target'"
    ]


def test_missing_directories_are_fine(script: ModuleType, tmp_path: Path) -> None:
    assert script.check(tmp_path, frozenset()) == []


def test_main_reports_exit_status(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(script, "check", lambda: [])
    assert script.main() == 0
    assert "ok" in capsys.readouterr().out
    monkeypatch.setattr(script, "check", lambda: ["apps/x.py:1: banned construct 'mark_safe'"])
    assert script.main() == 1
    assert "1 violation" in capsys.readouterr().err


# --- parsed rules (plan §3.6 guardrails) -------------------------------------------

SCOPED_MODELS = """
from apps.access.scopes import ScopedQuerySet

class ThingQuerySet(ScopedQuerySet["Thing"]):
    pass

class SubQuerySet(ThingQuerySet):
    pass

class Thing(Model):
    objects = ThingQuerySet.as_manager()

class Other(Model):
    objects: Manager = Manager.from_queryset(SubQuerySet)()

class Plain(Model):
    objects = Manager()
"""

PRINCIPALS = """
CONSTRUCTOR_MODULES: dict[str, frozenset[str]] = {
    "TokenPrincipal": frozenset({"apps.accounts.tokens"}),
    "SystemPrincipal": frozenset({"apps.notifications.worker"}),
    "OperatorPrincipal": frozenset({"apps.core.commands"}),
}
"""


def _violations(script: ModuleType, root: Path, files: dict[str, str]) -> list[str]:
    for rel, text in files.items():
        _write(root, rel, text)
    found: list[str] = script.check(root, frozenset())
    return found


def _rules(violations: list[str]) -> list[str]:
    return [violation.split(": ")[1] for violation in violations]


class TestScopedModelAccess:
    @pytest.mark.parametrize(
        "rel",
        [
            "apps/x/views.py",
            "apps/x/views/detail.py",
            "apps/x/forms.py",
            "apps/x/forms/choices.py",
            "apps/x/templatetags/x_tags.py",
        ],
    )
    def test_manager_access_in_views_forms_and_tags(
        self, script: ModuleType, tmp_path: Path, rel: str
    ) -> None:
        source = "def v(r):\n    return Thing.objects.all(), models.Other._default_manager\n"
        found = _violations(script, tmp_path, {"apps/x/models.py": SCOPED_MODELS, rel: source})
        assert _rules(found) == ["scoped-model-access", "scoped-model-access"]
        assert any("Thing.objects" in violation for violation in found)

    def test_shortcuts_with_a_scoped_model(self, script: ModuleType, tmp_path: Path) -> None:
        source = "def v(r, pk):\n    return get_object_or_404(Thing, pk=pk)\n"
        found = _violations(
            script, tmp_path, {"apps/x/models.py": SCOPED_MODELS, "apps/x/views.py": source}
        )
        assert _rules(found) == ["scoped-model-access"]

    def test_selectors_plain_models_and_querysets_are_fine(
        self, script: ModuleType, tmp_path: Path
    ) -> None:
        files = {
            "apps/x/models.py": SCOPED_MODELS,
            "apps/x/selectors.py": "def s(ctx):\n    return Thing.objects.visible_to(ctx, 'x')\n",
            "apps/x/views.py": (
                "def v(r, pk):\n"
                "    Plain.objects.all()\n"
                "    get_object_or_404(selectors.things(r.access), pk=pk)\n"
            ),
            "apps/x/admin.py": "Thing.objects.all()\n",
        }
        assert _violations(script, tmp_path, files) == []


class TestSuperuser:
    @pytest.mark.parametrize(
        "source",
        [
            "if user.is_superuser:\n    pass\n",
            "User.objects.filter(is_superuser=True)\n",
            "User.objects.filter(is_superuser__in=[True])\n",
            "getattr(user, 'is_superuser')\n",
        ],
    )
    def test_checks_outside_access_are_reported(
        self, script: ModuleType, tmp_path: Path, source: str
    ) -> None:
        assert _rules(_violations(script, tmp_path, {"apps/x/views.py": source})) == [
            "is_superuser"
        ]

    def test_templates_too(self, script: ModuleType, tmp_path: Path) -> None:
        found = _violations(script, tmp_path, {"templates/a.html": "{% if user.is_superuser %}"})
        assert found == ["templates/a.html:1: banned construct 'is_superuser'"]

    def test_access_strings_and_docstrings_are_fine(
        self, script: ModuleType, tmp_path: Path
    ) -> None:
        files = {
            "apps/access/policy.py": "def f(user):\n    return user.is_superuser\n",
            "apps/accounts/models.py": (
                '"""is_superuser is for operators."""\nextra.setdefault("is_superuser", False)\n'
            ),
        }
        assert _violations(script, tmp_path, files) == []


class TestServicesAuthorize:
    def test_a_write_without_authorization(self, script: ModuleType, tmp_path: Path) -> None:
        source = (
            "@transactional\n"
            "def close(principal, pk):\n"
            "    Thing.objects.filter(pk=pk).update(closed=True)\n"
            "def rename(principal, thing):\n"
            "    thing.save()\n"
            "def _helper(thing):\n"
            "    thing.delete()\n"
            "def public_via_helper(thing):\n"
            "    _helper(thing)\n"
            "def bulk(things):\n"
            "    apply_changes(things, {})\n"
            "def read(pk):\n"
            "    return Thing.objects.get(pk=pk)\n"
        )
        found = _violations(script, tmp_path, {"apps/x/services.py": source})
        assert _rules(found) == ["service-without-authorize"] * 4
        reported = [violation.split(": ")[2].split("(")[0] for violation in found]
        assert reported == ["bulk", "close", "public_via_helper", "rename"]

    def test_authorization_directly_through_helpers_or_other_services(
        self, script: ModuleType, tmp_path: Path
    ) -> None:
        files = {
            "apps/x/services.py": (
                "from apps.access.authorization import authorize_in_txn\n"
                "def _authorize(p):\n"
                "    authorize_in_txn(p, 'x', locks=None)\n"
                "def lock_for_change(p):\n"
                "    _authorize(p)\n"
                "@transactional\n"
                "def direct(p):\n"
                "    authorize_in_txn(p, 'x', locks=None)\n"
                "    Thing.objects.create()\n"
                "@transactional\n"
                "def through_helper(p, thing):\n"
                "    _authorize(p)\n"
                "    thing.save()\n"
            ),
            "apps/y/services.py": (
                "from apps.x.services import lock_for_change\n"
                "from apps.x import services as x_services\n"
                "@transactional\n"
                "def imported(p, thing):\n"
                "    lock_for_change(p)\n"
                "    thing.save()\n"
                "@transactional\n"
                "def aliased(p, thing):\n"
                "    x_services.lock_for_change(p)\n"
                "    thing.save()\n"
            ),
            "apps/z/services/orders.py": (
                "import apps.x.services as xs\n"
                "@transactional\n"
                "def packaged(p):\n"
                "    xs.lock_for_change(p)\n"
            ),
        }
        assert _violations(script, tmp_path, files) == []


class TestGetViewsCallingServices:
    @pytest.mark.parametrize(
        "source",
        [
            "from apps.x.services import close\ndef v(r, pk):\n    close(r, pk)\n",
            "from apps.x import services\n@require_safe\ndef v(r, pk):\n    services.close(r)\n",
            (
                "from apps.x.services import close\n"
                "def _do(r):\n    close(r)\n"
                "@require_capability('x')\ndef v(r):\n    _do(r)\n"
            ),
            (
                "from apps.x.services import close\n"
                "@require_http_methods(['GET', 'POST'])\ndef v(r):\n    close(r)\n"
            ),
            (
                "from apps.x.services import close\n"
                "@require_http_methods(METHODS)\ndef v(r):\n    close(r)\n"
            ),
            (
                "from apps.x.services import close\n"
                "class V(CapabilityRequiredMixin):\n"
                "    def post(self, r):\n        close(r)\n"
            ),
            (
                "from apps.x.services import close\n"
                "def _do(r):\n    close(r)\n"
                "class V(View):\n    http_method_names = ['get', 'post']\n"
                "    def post(self, r):\n        _do(r)\n"
            ),
        ],
    )
    def test_reported(self, script: ModuleType, tmp_path: Path, source: str) -> None:
        found = _violations(script, tmp_path, {"apps/v/views.py": source})
        assert _rules(found) == ["get-view-calls-service"]

    @pytest.mark.parametrize(
        "source",
        [
            "from apps.x.services import close\n@mutating('x')\ndef v(r):\n    close(r)\n",
            "from apps.x.services import close\n@require_POST\ndef v(r):\n    close(r)\n",
            (
                "from apps.x.services import close\n"
                "@require_capability('x', post_only=True)\ndef v(r):\n    close(r)\n"
            ),
            (
                "from apps.x.services import close\n"
                "@require_http_methods(['POST'])\ndef v(r):\n    close(r)\n"
            ),
            (
                "from apps.x.services import close\n"
                "class V(MutatingViewMixin):\n    def post(self, r):\n        close(r)\n"
                "class W(V):\n    def post(self, r):\n        close(r)\n"
            ),
            (
                "from apps.x import services\n"
                "class V(View):\n    http_method_names = ['post']\n"
                "    def post(self, r):\n        services.close(r)\n"
            ),
            (
                "from apps.x.selectors import things\nfrom apps.x.services import close\n"
                "def _unused(r):\n    close(r)\n"
                "@require_safe\ndef v(r):\n    return things(r)\n"
            ),
        ],
    )
    def test_post_only_views_and_reads_are_fine(
        self, script: ModuleType, tmp_path: Path, source: str
    ) -> None:
        assert _violations(script, tmp_path, {"apps/v/views.py": source}) == []


class TestModelAdmin:
    @pytest.mark.parametrize(
        "source",
        [
            "from django.contrib import admin\nclass A(admin.ModelAdmin):\n    pass\n",
            "from django.contrib.admin import ModelAdmin\nclass A(ModelAdmin):\n    pass\n",
            "from django.contrib import admin\nadmin.site.register(Thing)\n",
        ],
    )
    def test_plain_admin_is_reported(self, script: ModuleType, tmp_path: Path, source: str) -> None:
        assert _rules(_violations(script, tmp_path, {"apps/x/admin.py": source})) == [
            "plain-model-admin"
        ]

    def test_service_backed_admins_are_fine(self, script: ModuleType, tmp_path: Path) -> None:
        source = (
            "@admin.register(Thing)\nclass A(ReadOnlyModelAdmin):\n    pass\n"
            "admin.site.register(Other, OtherAdmin)\n"
            "admin.site.register(Third, admin_class=ThirdAdmin)\n"
            "admin.site.unregister(Group)\n"
        )
        assert _violations(script, tmp_path, {"apps/x/admin.py": source}) == []

    def test_the_base_class_is_allowlisted(self, script: ModuleType) -> None:
        assert ("apps/core/admin.py", "plain-model-admin") in script.ALLOWLIST


class TestPrincipalConstructors:
    @pytest.mark.parametrize(
        ("rel", "source"),
        [
            ("apps/x/views.py", "SystemPrincipal()\n"),
            ("apps/x/services.py", "principals.OperatorPrincipal(user_id=1)\n"),
            ("apps/accounts/forms.py", "TokenPrincipal(kind=k)\n"),
            ("apps/notifications/workers.py", "SystemPrincipal()\n"),
        ],
    )
    def test_outside_the_allowlist(
        self, script: ModuleType, tmp_path: Path, rel: str, source: str
    ) -> None:
        found = _violations(
            script, tmp_path, {"apps/access/principals.py": PRINCIPALS, rel: source}
        )
        assert _rules(found) == ["principal-constructor"]

    @pytest.mark.parametrize(
        ("rel", "source"),
        [
            ("apps/accounts/tokens.py", "TokenPrincipal(kind=k)\n"),
            ("apps/accounts/tokens/redeem.py", "TokenPrincipal(kind=k)\n"),
            ("apps/notifications/worker.py", "SystemPrincipal()\n"),
            ("apps/core/commands.py", "OperatorPrincipal(user_id=1)\n"),
            ("apps/x/views.py", "SessionPrincipal.from_request(r)\n"),
        ],
    )
    def test_inside_the_allowlist(
        self, script: ModuleType, tmp_path: Path, rel: str, source: str
    ) -> None:
        files = {"apps/access/principals.py": PRINCIPALS, rel: source}
        assert _violations(script, tmp_path, files) == []

    def test_the_registry_is_read_from_the_repository(self, script: ModuleType) -> None:
        from apps.access.principals import CONSTRUCTOR_MODULES

        # ``tests/conftest.py`` adds ``tests`` to every entry for the session.
        expected = {name: modules - {"tests"} for name, modules in CONSTRUCTOR_MODULES.items()}
        problems: list[str] = []
        assert script._constructor_modules(script.ROOT, problems) == expected
        assert problems == []

    def test_without_the_registry_nothing_may_construct_them(
        self, script: ModuleType, tmp_path: Path
    ) -> None:
        found = _violations(script, tmp_path, {"apps/core/commands.py": "OperatorPrincipal()\n"})
        assert _rules(found) == ["principal-constructor"]

    @pytest.mark.parametrize(
        "registry",
        [
            "CONSTRUCTOR_MODULES = build()\n",
            "CONSTRUCTOR_MODULES = {NAME: frozenset({'a'})}\n",
            "CONSTRUCTOR_MODULES = {'TokenPrincipal': {'a'}}\n",
            "CONSTRUCTOR_MODULES = {'TokenPrincipal': frozenset({A})}\n",
            "OTHER = 1\n",
        ],
    )
    def test_an_unreadable_registry_fails_closed(
        self, script: ModuleType, tmp_path: Path, registry: str
    ) -> None:
        found = _violations(script, tmp_path, {"apps/access/principals.py": registry})
        assert _rules(found) == ["principal-constructor"]
        assert "literal" in found[0]


class TestMail:
    @pytest.mark.parametrize(
        "source",
        [
            "import smtplib\n",
            "import smtplib as s\n",
            "from smtplib import SMTP\n",
            "from django.core.mail import send_mail\n",
            "from django.core import mail\nmail.send_mail('s', 'b', 'f', ['t'])\n",
            "EmailMessage('s').send()\n",
            "mail_admins('s', 'b')\n",
        ],
    )
    def test_outside_the_worker(self, script: ModuleType, tmp_path: Path, source: str) -> None:
        found = _violations(script, tmp_path, {"apps/x/services.py": source})
        assert set(_rules(found)) == {"mail-outside-outbox"}

    def test_the_worker_and_backends_are_fine(self, script: ModuleType, tmp_path: Path) -> None:
        files = {
            "apps/notifications/worker.py": "from django.core.mail import get_connection\n",
            "apps/core/mail.py": (
                "from django.core.mail.backends.base import BaseEmailBackend\n"
                "class B(BaseEmailBackend):\n    def send_messages(self, m):\n        return 0\n"
            ),
        }
        assert _violations(script, tmp_path, files) == []


def test_unparsable_python_is_reported(script: ModuleType, tmp_path: Path) -> None:
    assert _rules(_violations(script, tmp_path, {"apps/x/views.py": "def (:\n"})) == [
        "syntax-error"
    ]


def test_the_allowlist_applies_to_parsed_rules(script: ModuleType, tmp_path: Path) -> None:
    _write(tmp_path, "apps/x/views.py", "SystemPrincipal()\n")
    allowed = frozenset({("apps/x/views.py", "principal-constructor")})
    assert script.check(tmp_path, allowed) == []


class TestDoneWhenViolations:
    """M2.4 "done when": the static half of the six deliberate violations.

    The unregistered URL and the leaked sentinel are caught by the acceptance matrix
    (``tests/access``); the plain ``ModelAdmin`` also by the system check ``gmat.E005``.
    """

    @pytest.mark.parametrize(
        ("files", "rule"),
        [
            (
                {
                    "apps/x/models.py": SCOPED_MODELS,
                    "apps/x/views.py": "def v(r):\n    return Thing.objects.all()\n",
                },
                "scoped-model-access",
            ),
            (
                {
                    "apps/access/principals.py": PRINCIPALS,
                    "apps/x/views.py": "@mutating('x')\ndef v(r):\n    SystemPrincipal()\n",
                },
                "principal-constructor",
            ),
            (
                {
                    "apps/x/views.py": (
                        "from apps.x.services import close\n"
                        "@require_capability('x')\ndef v(r):\n    close(r)\n"
                    )
                },
                "get-view-calls-service",
            ),
            (
                {"apps/x/admin.py": "class A(admin.ModelAdmin):\n    pass\n"},
                "plain-model-admin",
            ),
        ],
        ids=["unscoped-view", "system-principal-in-view", "get-view-service", "plain-admin"],
    )
    def test_each_is_caught(
        self,
        script: ModuleType,
        tmp_path: Path,
        files: dict[str, str],
        rule: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert _rules(_violations(script, tmp_path, files)) == [rule]
        monkeypatch.setattr(script, "ROOT", tmp_path)
        monkeypatch.setattr(script.check, "__defaults__", (tmp_path, frozenset()))
        assert script.main() == 1
        assert rule in capsys.readouterr().err
