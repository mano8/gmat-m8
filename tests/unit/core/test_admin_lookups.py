"""Admin lists filter only on what they offer (``ServiceBackedModelAdmin.lookup_allowed``).

Django accepts a query-string lookup on any local field, so a list's result count could read
back, one guess at a time, a column its pages never show: a password hash, a token hash, an
MFA secret, a price or an audit payload (M8.3).
"""

from typing import Any

import pytest
from django.contrib import admin
from django.db.models import ForeignKey
from django.test import RequestFactory
from django.urls import reverse

from apps.audit.models import AuditEvent
from tests.org import People, Sentinel, client_for

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("url_name", "query"),
    [
        ("admin:accounts_user_changelist", "password__startswith=argon2"),
        ("admin:accounts_user_changelist", "password__regex=%5Eargon2"),
        ("admin:accounts_invitation_changelist", "token_hash__startswith=a"),
        ("admin:accounts_emailchangerequest_changelist", "cancel_token_hash__startswith=a"),
        ("admin:mfa_authenticator_changelist", "data__contains=a"),
        ("admin:purchasing_purchaseorderline_changelist", "unit_price__gte=1"),
        ("admin:audit_auditevent_changelist", "data__changes__isnull=false"),
        ("admin:audit_auditevent_changelist", "ip__startswith=10"),
        ("admin:attachments_attachment_changelist", "storage_key__startswith=a"),
    ],
)
def test_a_column_the_page_never_shows_cannot_filter_it(
    people: People, url_name: str, query: str
) -> None:
    response = client_for(people.group_admin).get(f"{reverse(url_name)}?{query}")
    assert response.status_code == 400


@pytest.mark.parametrize(
    ("url_name", "query"),
    [
        ("admin:accounts_user_changelist", "is_active__exact=1"),
        ("admin:audit_auditevent_changelist", "action__exact=receipt.posted"),
        ("admin:audit_auditevent_changelist", "occurred_at__year=2026"),
        ("admin:organizations_branch_changelist", "is_active__exact=1"),
    ],
)
def test_the_filters_a_list_offers_still_apply(people: People, url_name: str, query: str) -> None:
    assert client_for(people.group_admin).get(f"{reverse(url_name)}?{query}").status_code == 200


def _parameter(model_admin: Any, name: str) -> str:
    """The query parameter Django's list filter for ``name`` sends."""
    field = model_admin.model._meta.get_field(name)
    return f"{name}__id__exact" if isinstance(field, ForeignKey) else f"{name}__exact"


def test_every_list_filter_of_every_admin_is_still_allowed() -> None:
    request = RequestFactory().get("/")
    for model_admin in admin.site._registry.values():
        for item in model_admin.get_list_filter(request):
            name = str(item[0] if isinstance(item, list | tuple) else item)
            assert model_admin.lookup_allowed(_parameter(model_admin, name), "1", request), (
                f"{type(model_admin).__name__}: {name}"
            )
        if model_admin.date_hierarchy:
            lookup = f"{model_admin.date_hierarchy}__year"
            assert model_admin.lookup_allowed(lookup, "2026", request)


def test_an_audit_event_shows_neither_its_payload_nor_the_client_address(people: People) -> None:
    event = AuditEvent.objects.create(
        actor_label="mm@example.org",
        action="purchase_order_line.updated",
        data={"changes": {"unit_price": {"from": None, "to": Sentinel.PRICE.value}}},
        ip="203.0.113.77",
    )
    for actor in (people.group_admin, people.operator):
        html = client_for(actor).get(reverse("admin:audit_auditevent_change", args=[event.pk]))
        page = html.content.decode()
        assert html.status_code == 200 and "purchase_order_line.updated" in page
        assert not Sentinel.PRICE.found_in(page) and "203.0.113.77" not in page
