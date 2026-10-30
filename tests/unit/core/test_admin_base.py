"""Service-backed admin base: related objects are never saved behind the service's back."""

from types import SimpleNamespace
from typing import Any

import pytest
from django.contrib import admin
from django.contrib.auth.models import Group
from django.test import RequestFactory

from apps.accounts.models import User
from apps.core.admin import ServiceBackedModelAdmin


class Form:
    def __init__(self) -> None:
        self.m2m_saved = False

    def save_m2m(self) -> None:
        self.m2m_saved = True


def test_many_to_many_is_left_to_the_service() -> None:
    form = Form()
    ServiceBackedModelAdmin(Group, admin.site).save_related(
        RequestFactory().post("/"), form, [], change=True
    )
    assert form.m2m_saved is False


def test_inlines_must_go_through_a_service() -> None:
    formsets: list[Any] = [object()]
    with pytest.raises(NotImplementedError, match="inlines"):
        ServiceBackedModelAdmin(Group, admin.site).save_related(
            RequestFactory().post("/"), Form(), formsets, change=True
        )


class FakeForm:
    def __init__(self, cleaned_data: dict[str, Any]) -> None:
        self.cleaned_data = cleaned_data
        self.changed_data = list(cleaned_data)


def test_form_values_key_relations_by_attribute_name() -> None:
    model_admin = ServiceBackedModelAdmin(User, admin.site)
    form = FakeForm({"full_name": "Ana", "employer_company": None})
    assert model_admin.form_values(form) == {"full_name": "Ana", "employer_company_id": None}
    form.cleaned_data["employer_company"] = SimpleNamespace(pk=7)
    assert model_admin.form_values(form, ["employer_company"]) == {"employer_company_id": 7}


def test_adopt_points_the_instance_at_the_saved_row() -> None:
    unsaved, saved = Group(name="x"), Group(pk=42, name="x")
    saved._state.adding = False
    ServiceBackedModelAdmin.adopt(unsaved, saved)
    assert (unsaved.pk, unsaved._state.adding) == (42, False)


def test_nobody_edits_unless_a_subclass_opts_in() -> None:
    assert ServiceBackedModelAdmin(Group, admin.site).may_edit(RequestFactory().get("/")) is False
