"""Service-backed admin base: related objects are never saved behind the service's back."""

from typing import Any

import pytest
from django.contrib import admin
from django.contrib.auth.models import Group
from django.test import RequestFactory

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
