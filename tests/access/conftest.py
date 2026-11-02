"""The acceptance matrix's world: the shared fixture of plan §8.1, built once per module.

Building it per test would dominate the run time (one test per URL), so it is created in
an outer transaction that lives for the module; each test runs in a savepoint inside it
(pytest-django's ``db``), and the outer transaction is rolled back at the end.
"""

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal

import pytest
from allauth.account.models import EmailAddress
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from pytest_django import DjangoDbBlocker

from apps.accounts.models import EmailChangeRequest, Invitation
from apps.accounts.services import (
    EMAIL_CHANGE_CANCELLABLE,
    EMAIL_CHANGE_CONFIRMABLE,
    INVITATION_PENDING,
)
from apps.accounts.tokens import new_token
from apps.attachments import files
from apps.attachments.models import (
    QUOTA_ID,
    Attachment,
    AttachmentReservation,
    MediaDeletion,
    StorageQuota,
)
from apps.audit.models import AuditEvent
from apps.audit.recorder import AuditIdentity, record
from apps.catalog.models import Material, MaterialCategory, UnitOfMeasure
from apps.handovers.models import Handover, HandoverLine
from apps.material_requests import revisions
from apps.material_requests.models import (
    MaterialRequest,
    MaterialRequestBaseline,
    MaterialRequestBaselineLine,
    MaterialRequestLine,
    MaterialRequestLineRevision,
)
from apps.notifications.models import EmailOutbox
from apps.notifications.models import Status as MailStatus
from apps.notifications.outbox import queue_security_email
from apps.organizations.models import JointVentureMember, Membership
from apps.projects.models import ProjectAssignment
from apps.receiving.models import GoodsReceipt, GoodsReceiptLine, ReceiptAttribution
from tests.access.matrix import World, build_world
from tests.orders import allocate, line, order
from tests.org import build_sentinel_order
from tests.outbox import make_dispatch


def _edited(world: World, key: str) -> None:
    """The request's first line as its requester submitted it (2) and a reviewer's edit of
    it (to 3): the pages that show the edits (M7b.4) render with data in the matrix."""
    request, line = world.objects[key], world.objects[f"{key}_line"]
    baseline = MaterialRequestBaseline.objects.create(
        request=request,
        sequence=1,
        submitted_at=request.submitted_at,
        submitted_by=request.requested_by,
    )
    MaterialRequestBaselineLine.objects.create(
        baseline=baseline,
        line_id=line.pk,
        line_no=line.line_no,
        material=line.material,
        material_snapshot={"reference": line.material.reference, "name": line.material.name},
        unit=line.unit,
        requested_qty=Decimal("2.000"),
    )
    after = revisions.values(line)
    MaterialRequestLineRevision.objects.create(
        request=request,
        baseline=baseline,
        line_id=line.pk,
        line_no=line.line_no,
        kind=MaterialRequestLineRevision.Kind.CHANGE,
        editor=world.people.engineer,
        reason="Measured on site",
        before={**after, "requested_qty": "2"},
        after=after,
    )


def _admin_rows(world: World) -> None:
    """One row of each model the admin lists, for its object URLs."""
    people, org = world.people, world.org
    world.objects["emailaddress"] = EmailAddress.objects.create(
        user=people.tech_mad, email=people.tech_mad.email, primary=True, verified=True
    )
    world.objects["auditevent"] = record(
        AuditIdentity(label=people.group_admin.email, user_id=people.group_admin.pk),
        "fixture.created",
    )
    for key, branch in (("audit_mad", org.a_mad), ("audit_sev", org.a_sev)):
        world.objects[key] = record(
            AuditIdentity(label=people.group_admin.email, user_id=people.group_admin.pk),
            "fixture.created",
            obj=branch,
            branch_id=branch.pk,
        )
    world.objects["jointventuremember"] = JointVentureMember.objects.create(
        venture=org.b, member=org.a
    )
    unit = UnitOfMeasure.objects.get(code="UD")
    world.objects["unitofmeasure"] = unit
    world.objects["materialcategory"] = MaterialCategory.objects.create(name="Cables")
    world.objects["material"] = Material.objects.create(
        reference="CAB-25",
        name="Cable H07V-K 1x2,5 mm²",
        unit=unit,
        category=world.objects["materialcategory"],
    )
    world.objects["materialrequest"] = request = MaterialRequest.objects.create(
        project=org.hospital, procurement_branch=org.a_mad, requested_by=people.tech_mad
    )
    world.objects["materialrequestline"] = MaterialRequestLine.objects.create(
        request=request,
        project=org.hospital,
        line_no=1,
        material=world.objects["material"],
        unit=unit,
        requested_qty=Decimal("10.000"),
    )
    world.objects["partner_request"] = MaterialRequest.objects.create(
        project=org.hospital, procurement_branch=org.a_mad, requested_by=people.partner
    )
    world.objects["submitted_request"] = submitted = MaterialRequest.objects.create(
        project=org.hospital,
        procurement_branch=org.a_mad,
        requested_by=people.tech_mad,
        status=MaterialRequest.Status.SUBMITTED,
        number="SOL-A-MAD-2026-00001",
        submitted_at=timezone.now(),
    )
    MaterialRequestLine.objects.create(
        request=submitted,
        project=org.hospital,
        line_no=1,
        material=world.objects["material"],
        unit=unit,
        requested_qty=Decimal("3.000"),
    )
    # The reviewer's screens (M4.4): one request per row that may change state, so the
    # order of the rows and actors never matters. All are the technician's, on the hospital.
    material, decided = world.objects["material"], timezone.now()

    def reviewable(key: str, status: str, number: int, *, approved: bool = False) -> None:
        item = MaterialRequest.objects.create(
            project=org.hospital,
            procurement_branch=org.a_mad,
            requested_by=people.tech_mad,
            status=status,
            number=f"SOL-A-MAD-2026-{number:05d}",
            submitted_at=decided,
        )
        decision = (
            {
                "decision": MaterialRequestLine.Decision.APPROVED,
                "approved_qty": Decimal("3.000"),
                "decided_by": people.mm_mad,
                "decided_at": decided,
            }
            if approved
            else {}
        )
        world.objects[key] = item
        world.objects[f"{key}_line"] = MaterialRequestLine.objects.create(
            request=item,
            project=org.hospital,
            line_no=1,
            material=material,
            unit=unit,
            requested_qty=Decimal("3.000"),
            **decision,
        )

    Status = MaterialRequest.Status
    reviewable("review_request", Status.SUBMITTED, 2)
    reviewable("approve_request", Status.SUBMITTED, 3)
    reviewable("cancel_request", Status.SUBMITTED, 4)
    reviewable("approved_request", Status.APPROVED, 5, approved=True)
    world.objects["submitted_request_line"] = submitted.lines.get()
    _edited(world, "submitted_request")
    _edited(world, "review_request")
    _edited(world, "approved_request")
    world.objects["free_line"] = MaterialRequestLine.objects.create(
        request=world.objects["approved_request"],
        project=org.hospital,
        line_no=2,
        free_text="Cable de obra",
        unit=unit,
        requested_qty=Decimal("2.000"),
        decision=MaterialRequestLine.Decision.APPROVED,
        approved_qty=Decimal("2.000"),
        decided_by=people.mm_mad,
        decided_at=decided,
    )
    # The issued order carrying the price and other-project quantity sentinels (M5.1).
    sentinel = build_sentinel_order(org, people)
    world.objects["purchaseorder"] = sentinel.order
    world.objects["purchaseorderline"] = sentinel.line
    world.objects["purchaseallocation"] = sentinel.hospital
    # Its dispatch to the sentinel supplier, left AMBIGUOUS by the worker (M5.3).
    world.objects["dispatch"] = make_dispatch(
        sentinel.order, MailStatus.AMBIGUOUS, address=org.supplier.email
    )
    # A draft of A-MAD with one line allocated to the approved request: the builder's URLs.
    world.objects["draftorder"] = draft = order(org)
    world.objects["draftline"] = draft_line = line(draft, material, "3")
    world.objects["draftallocation"] = allocate(
        draft_line, world.objects["approved_request_line"], "3"
    )
    # A receipt of 2 at the hospital's worksite on an order of its own, with the direct
    # handover it made (M6.1): the receiving and handover admin pages.
    reviewable("received_request", Status.APPROVED, 6, approved=True)
    received_line = world.objects["received_request_line"]
    received = order(
        org, status="ISSUED", issued_by=people.mm_mad, ship_to=org.worksite(org.hospital)
    )
    received_po_line = line(received, material, "3")
    two = Decimal("2.000")
    world.objects["goodsreceipt"] = receipt = GoodsReceipt.objects.create(
        number="REC-A-MAD-2026-00001",
        po=received,
        branch=org.a_mad,
        location=org.worksite(org.hospital),
        received_at=decided,
        received_by=people.wo_mad,
    )
    world.objects["goodsreceiptline"] = receipt_line = GoodsReceiptLine.objects.create(
        receipt=receipt, po=received, po_line=received_po_line, qty_received=two
    )
    world.objects["receiptattribution"] = attribution = ReceiptAttribution.objects.create(
        receipt_line=receipt_line,
        allocation=allocate(received_po_line, received_line, "3"),
        po_line=received_po_line,
        qty=two,
    )
    world.objects["handover"] = handover = Handover.objects.create(
        number="ENT-A-MAD-2026-00001",
        kind=Handover.Kind.DIRECT_RECEIPT,
        branch=org.a_mad,
        source_location=receipt.location,
        project=org.hospital,
        handed_by=people.wo_mad,
        received_by=people.wo_mad,
        handed_at=decided,
        goods_receipt=receipt,
    )
    world.objects["handoverline"] = HandoverLine.objects.create(
        handover=handover,
        project=org.hospital,
        request_line=received_line,
        qty=two,
        receipt_attribution=attribution,
    )
    # A receipt at the warehouse (all surplus) and a handover from it (M6.3): the receiving and
    # handover screens' warehouse positions, which a site manager does not reach.
    world.objects["warehousereceipt"] = warehouse_receipt = GoodsReceipt.objects.create(
        number="REC-A-MAD-2026-00002",
        po=received,
        branch=org.a_mad,
        location=org.warehouses["A Madrid"],
        received_at=decided,
        received_by=people.wo_mad,
    )
    GoodsReceiptLine.objects.create(
        receipt=warehouse_receipt,
        po=received,
        po_line=received_po_line,
        qty_received=Decimal("1.000"),
        surplus_qty=Decimal("1.000"),
    )
    world.objects["warehousehandover"] = warehouse_handover = Handover.objects.create(
        number="ENT-A-MAD-2026-00002",
        kind=Handover.Kind.FROM_WAREHOUSE,
        branch=org.a_mad,
        source_location=org.warehouses["A Madrid"],
        project=org.hospital,
        handed_by=people.wo_mad,
        received_by=people.partner,
        handed_at=decided,
    )
    HandoverLine.objects.create(
        handover=warehouse_handover,
        project=org.hospital,
        request_line=world.objects["approved_request_line"],
        qty=Decimal("1.000"),
    )
    _attachments(world)
    world.objects["membership"] = Membership.objects.get(user=people.tech_mad)
    world.objects["projectassignment"] = ProjectAssignment.objects.get(user=people.site_manager)
    later = timezone.now() + timedelta(hours=24)
    world.objects["invitation"] = Invitation.objects.create(
        email="invitee@example.org",
        kind=Invitation.Kind.NEW_ACCOUNT,
        invited_by=people.group_admin,
        token_hash=new_token()[1],
        expires_at=later,
        grants={"memberships": [{"role": "TECHNICIAN", "branch_id": str(org.a_mad.pk)}]},
    )
    world.objects["emailchangerequest"] = EmailChangeRequest.objects.create(
        user=people.tech_mad,
        new_email="tech-new@example.org",
        requested_by=people.group_admin,
        token_hash=new_token()[1],
        cancel_token_hash=new_token()[1],
        expires_at=later,
    )
    _security_mail(world)
    assert AuditEvent.objects.exists()


def _security_mail(world: World) -> None:
    """The queued mails the delivery fragments (M7c.1) report on: an invitation's and the
    two of an email change, one of them failed with an error text no page may show."""
    invitation = world.objects["invitation"]
    change = world.objects["emailchangerequest"]
    queue_security_email(
        address=invitation.email,
        target=invitation,
        rule=INVITATION_PENDING,
        subject="Invitation",
        body="the link",
        source_ref="invitation",
    )
    queue_security_email(
        address=change.new_email,
        target=change,
        rule=EMAIL_CHANGE_CONFIRMABLE,
        subject="Confirm",
        body="the link",
        source_ref="email_change.confirm",
    )
    failed = queue_security_email(
        address=change.user.email,
        target=change,
        rule=EMAIL_CHANGE_CANCELLABLE,
        subject="Notice",
        body="the link",
        source_ref="email_change.cancel",
    )
    EmailOutbox.objects.filter(pk=failed.pk).update(
        status=MailStatus.FAILED, last_error="SMTP 550 at RCPT"
    )


def _attachments(world: World) -> None:
    """One stored file on the technician's draft, the worksite and warehouse receipts and the
    warehouse handover (M6.4), and a row of each attachment model for the admin."""
    people = world.people
    parents = {
        "requestattachment": ("request", world.objects["materialrequest"], people.tech_mad),
        "attachment": ("receipt", world.objects["goodsreceipt"], people.wo_mad),
        "warehouseattachment": ("receipt", world.objects["warehousereceipt"], people.wo_mad),
        "handoverattachment": ("handover", world.objects["warehousehandover"], people.wo_mad),
    }
    files.ensure_dirs()
    for key, (field, parent, uploader) in parents.items():
        storage_key = files.new_storage_key()
        files.stored_path(storage_key).write_bytes(b"%PDF-1.4\n%%EOF\n")
        world.objects[key] = Attachment.objects.create(
            **{field: parent},
            storage_key=storage_key,
            file_name=f"{key}.pdf",
            content_type=Attachment.ContentType.PDF,
            size_bytes=15,
            sha256="0" * 64,
            uploaded_by=uploader,
            uploaded_at=timezone.now(),
        )
    world.objects["attachmentreservation"] = AttachmentReservation.objects.create(
        receipt=world.objects["goodsreceipt"],
        reserved_by=people.wo_mad,
        reserved_bytes=100,
        state=AttachmentReservation.State.RELEASED,
        lease_until=timezone.now(),
        finished_at=timezone.now(),
    )
    world.objects["storagequota"], _ = StorageQuota.objects.get_or_create(
        pk=QUOTA_ID, defaults={"limit_bytes": settings.ATTACHMENT_STORAGE_LIMIT}
    )
    world.objects["mediadeletion"] = MediaDeletion.objects.create(
        storage_key=files.new_storage_key(), size_bytes=1, delete_after=timezone.now()
    )


@pytest.fixture(scope="module")
def world(django_db_setup: None, django_db_blocker: DjangoDbBlocker) -> Iterator[World]:
    with django_db_blocker.unblock(), transaction.atomic():
        yield build_world(_admin_rows)
        transaction.set_rollback(True)
