"""The read-only admin base (ADR-0030): no write path, and the checks that keep it so."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from django.contrib import admin
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest
from django.test import RequestFactory

from apps.core import checks
from apps.core.admin import READ_ONLY_METHODS, ReadOnlyModelAdmin

ROOT = Path(__file__).resolve().parents[3]


def _request() -> HttpRequest:
    return RequestFactory().post("/admin/")


def _make() -> ReadOnlyModelAdmin:
    return ReadOnlyModelAdmin(Group, admin.site)


def test_nobody_adds_changes_or_deletes() -> None:
    model_admin, request = _make(), _request()
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False
    assert model_admin.has_delete_permission(request) is False
    assert model_admin.get_actions(request) == {}


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("save_model", (Group(name="x"), None, True)),
        ("save_related", (None, [], True)),
        ("save_formset", (None, None, True)),
        ("delete_model", (Group(name="x"),)),
        ("delete_queryset", (Group.objects.none(),)),
    ],
)
def test_every_write_path_refuses(method: str, args: tuple[Any, ...]) -> None:
    """Django checks the permissions first; these refuse whatever reaches them anyway."""
    with pytest.raises(PermissionDenied, match="read-only"):
        getattr(_make(), method)(_request(), *args)


class TestTheReadOnlyCheck:
    """``gmat.E024``: a subclass that turns a write path back on is refused (M10.5)."""

    def test_the_project_passes(self) -> None:
        assert checks.check_admin_read_only(None) == []

    @pytest.mark.parametrize("method", READ_ONLY_METHODS)
    def test_a_redefined_write_path_is_refused(self, method: str) -> None:
        def anything(self: Any, *args: Any, **kwargs: Any) -> bool:
            return True

        editable = type("EditableGroupAdmin", (ReadOnlyModelAdmin,), {method: anything})
        assert checks._editable_admin_problems(editable) == [f"redefines {method}"]
        self._registered(editable, f"redefines {method}")

    def test_an_inline_is_refused(self) -> None:
        inline = type("GroupInline", (admin.TabularInline,), {"model": Group})
        with_inline = type("GroupAdmin", (ReadOnlyModelAdmin,), {"inlines": [inline]})
        self._registered(with_inline, "declares inlines")

    def test_an_intermediate_class_cannot_hide_it(self) -> None:
        class Editable(ReadOnlyModelAdmin):
            def has_change_permission(self, request: Any, obj: Any = None) -> bool:
                return True

        class Innocent(Editable):
            pass

        assert checks._editable_admin_problems(Innocent) == ["redefines has_change_permission"]

    @staticmethod
    def _registered(admin_class: type[ReadOnlyModelAdmin], problem: str) -> None:
        admin.site.register(Group, admin_class)
        try:
            [error] = checks.check_admin_read_only(None)
        finally:
            admin.site.unregister(Group)
        assert error.id == "gmat.E024"
        assert problem in error.msg and "auth.Group" in error.msg


def _access_rules() -> ModuleType:
    path = ROOT / "scripts" / "access_rules_parsed.py"
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location("access_rules_parsed_for_test", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
    return module


def test_the_static_rule_guards_the_same_methods() -> None:
    """``scripts/check_access_rules.py`` cannot import Django: it keeps a copy of the list."""
    assert _access_rules().READ_ONLY_METHODS == READ_ONLY_METHODS
