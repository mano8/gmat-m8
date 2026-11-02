"""The lease outlasts every enforced limit of an upload (``gmat.E020``, plan §5 F54), and the
parent's lock refuses a document that changed since its authorization."""

from typing import Any

import pytest
from django.db import transaction

from apps.access.principals import SessionPrincipal
from apps.attachments.checks import lease_outlasts_the_limits
from apps.attachments.parents import PARENTS, Kind, parent_of, type_of
from apps.attachments.quota import lock_parent
from apps.core.locking import LockOrder, LockSetChanged
from apps.material_requests.models import MaterialRequest
from tests.unit.attachments.conftest import admit


def test_the_default_lease_outlasts_the_limits() -> None:
    assert lease_outlasts_the_limits() == []


def test_a_lease_shorter_than_the_limits_is_refused(settings: Any) -> None:
    settings.ATTACHMENT_LEASE = 200
    (error,) = lease_outlasts_the_limits()
    assert error.id == "gmat.E020"


@pytest.mark.django_db
def test_a_document_changed_since_its_authorization_is_locked_again(
    draft: MaterialRequest,
) -> None:
    seen = MaterialRequest.objects.get(pk=draft.pk)
    MaterialRequest.objects.filter(pk=draft.pk).update(notes="changed")  # not authorization data
    locks = LockOrder()
    with transaction.atomic():
        assert lock_parent(locks, PARENTS[Kind.REQUEST], seen).pk == draft.pk
    seen.status = MaterialRequest.Status.SUBMITTED  # what the authorization read is stale
    with pytest.raises(LockSetChanged), transaction.atomic():
        lock_parent(LockOrder(), PARENTS[Kind.REQUEST], seen)


@pytest.mark.django_db
def test_rows_name_their_parent(draft: MaterialRequest, tech: SessionPrincipal) -> None:
    reservation = admit(tech, draft, 10)
    assert parent_of(reservation) == draft
    with pytest.raises(TypeError, match="takes no attachments"):
        type_of(tech)  # type: ignore[arg-type]
    reservation.request_id = None
    with pytest.raises(ValueError, match="no parent"):
        parent_of(reservation)
