"""The volume dataset of plan M7d.4: a year or two of an installation's data, built in bulk.

``build_volume`` adds, on top of the A/B/C organization of ``tests/org.py`` and its people:
100k audit events, 20k materials, 10k requests with 50k lines, 5k orders (15k lines and their
allocations), 5k receipts (with their attributions), 2k handovers, and what the screens that
list people and projects need (300 projects, 1,000 users with their memberships, 400
invitations with a queued, sent or failed email, 500 suppliers), and for the structure screens
(M10.2) 150 companies with two branches each, one company in ten active, so the other screens'
branch choices stay the size of a real group. ``Scale`` sizes it.

Written with ``bulk_create`` (the triggers and constraints still run; the composite foreign
keys are checked as rows go in, the others at the end by ``SET CONSTRAINTS ALL IMMEDIATE``),
so the data satisfies the schema but not every business rule: it is for measuring lists, not
for testing a ledger. A seeded generator makes every run the same. Three things are made
large on purpose so that the sections that page on their own have something to page: the
hospital (8 % of the requests), a project with 120 assignments, a person with 30 roles, 40
shared projects and 30 invitations, and a request with 300 audit events.
"""

import hashlib
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.db import connection
from django.utils import timezone

from apps.access.roles import Role
from apps.accounts.models import Invitation, User
from apps.audit.models import AuditEvent
from apps.catalog.models import Material, MaterialCategory, UnitOfMeasure
from apps.core.ids import new_id
from apps.handovers.models import Handover, HandoverLine
from apps.locations.models import Location
from apps.material_requests.models import MaterialRequest, MaterialRequestLine
from apps.notifications.models import EmailOutbox, Kind, Status
from apps.organizations.models import Branch, Company, Membership
from apps.projects.models import Project, ProjectAssignment
from apps.purchasing.models import PurchaseAllocation, PurchaseOrder, PurchaseOrderLine
from apps.receiving.models import GoodsReceipt, GoodsReceiptLine, ReceiptAttribution
from apps.suppliers.models import Supplier
from tests.org import Org, People

BATCH = 5_000
DAYS = 540  # the history the dataset covers
HOT_ASSIGNMENTS = 120
HOT_AUDIT_EVENTS = 300
OPEN_ORDER_SHARE = 0.2  # of the orders, the share still in flight


@dataclass(frozen=True)
class Scale:
    audit_events: int = 100_000
    materials: int = 20_000
    requests: int = 10_000
    lines_per_request: int = 5
    orders: int = 5_000
    lines_per_order: int = 3
    receipts: int = 5_000
    handovers: int = 2_000
    projects: int = 300
    users: int = 1_000
    invitations: int = 400
    suppliers: int = 500
    companies: int = 150


@dataclass
class Volume:
    """What the dataset added and the objects the tests aim at."""

    scale: Scale
    hot_project: Project  # 120 assignments
    hot_request: MaterialRequest  # 300 audit events
    busy_person: User  # 30 roles, 40 shared projects, 30 invitations
    requester: User  # owns a tenth of the requests
    counts: dict[str, int]


STATUS_MIX = (
    ("DRAFT", 3),
    ("SUBMITTED", 5),
    ("APPROVED", 12),
    ("REJECTED", 4),
    ("CANCELLED", 3),
    ("CLOSED", 73),
)
"""A year and a half of an installation at steady state: most requests are delivered and
closed, about one in eight is still being worked on."""
PRIORITY_MIX = (("LOW", 2), ("NORMAL", 6), ("HIGH", 3), ("URGENT", 1))
IN_FLIGHT = (("ISSUED", 70), ("DRAFT", 20), ("CANCELLED", 10))
UNNUMBERED = ("DRAFT", "CANCELLED")  # a discarded draft never gets a number
DECIDED = ("APPROVED", "REJECTED", "CLOSED")


def _request_times(created: datetime, status: str) -> dict[str, Any]:
    """submitted, decided and closed times and the close mode that go with ``status``."""
    closed = status == "CLOSED"
    return {
        "submitted_at": None if status in UNNUMBERED else created + timedelta(hours=1),
        "decided_at": created + timedelta(days=1) if status in DECIDED else None,
        "closed_at": created + timedelta(days=30) if closed else None,
        "close_mode": "AUTO" if closed else "",
    }


def _decision(
    request: MaterialRequest, line_no: int, quantity: Decimal, has_material: bool, reviewer: User
) -> dict[str, Any]:
    """Decision, approved quantity and sourcing of a line, from its request's status."""
    status = request.status
    if status not in DECIDED:
        return {}  # pending, as created
    decided = {"decided_by": reviewer, "decided_at": request.decided_at}
    if status == "REJECTED":
        return {**decided, "decision": "REJECTED"}
    return {
        **decided,
        "decision": "APPROVED",
        "approved_qty": quantity if line_no % 4 else quantity // 2,
        "sourcing": "PURCHASE" if has_material and line_no % 5 else "UNDECIDED",
    }


def _voiding(voided: bool, at: datetime, by: User, reason: str) -> dict[str, Any]:
    """The status and void fields of a receipt or handover, voided a day after ``at``."""
    if not voided:
        return {}
    return {
        "status": "VOIDED",
        "voided_at": at + timedelta(days=1),
        "voided_by": by,
        "void_reason": reason,
    }


def _receipt_share(order: PurchaseOrder, nth: int) -> Decimal:
    if order.status == "CLOSED":
        return Decimal(1)
    return Decimal("0.5") if nth == 1 else Decimal("0.25")


def _pick[T](rng: random.Random, weighted: tuple[tuple[T, int], ...]) -> T:
    return rng.choices([item for item, _ in weighted], [weight for _, weight in weighted])[0]


def _spread(index: int, total: int, start: Any) -> Any:
    """``index`` of ``total`` rows evenly over the history, oldest first."""
    return start + timedelta(seconds=DAYS * 86_400 * index // max(total, 1))


def _insert(model: Any, rows: list[Any]) -> None:
    model.objects.bulk_create(rows, batch_size=BATCH)


class _Builder:
    def __init__(self, org: Org, people: People, scale: Scale) -> None:
        self.org = org
        self.people = people
        self.scale = scale
        self.rng = random.Random(7)  # noqa: S311 - reproducible test data
        self.now = timezone.now()
        self.start = self.now - timedelta(days=DAYS)
        self.counts: dict[str, int] = {}
        self.warehouse = {branch.pk: org.warehouses[branch.name] for branch in self.warehoused()}
        self.default_unit = UnitOfMeasure.objects.get(code="UD")
        self.worksite: dict[Any, Any] = {}
        self.site_receipts: list[tuple[GoodsReceipt, Any]] = []
        self.attribution_of: dict[Any, ReceiptAttribution] = {}
        self.branches = {branch.pk: branch for branch in self.warehoused()}
        self.branch_of_line: dict[Any, Any] = {}

    def warehoused(self) -> list[Branch]:
        return [self.org.a_mad, self.org.a_sev, self.org.b_mad]

    def done(self, name: str, rows: list[Any]) -> list[Any]:
        self.counts[name] = len(rows)
        return rows

    # --- master data and people --------------------------------------------------------

    def catalog(self) -> list[Material]:
        units = list(UnitOfMeasure.objects.all()[:3])
        parents = MaterialCategory.objects.bulk_create(
            [MaterialCategory(name=f"Volume family {n}") for n in range(8)]
        )
        children = MaterialCategory.objects.bulk_create(
            [
                MaterialCategory(name=f"Volume family {n}.{m}", parent=parent)
                for n, parent in enumerate(parents)
                for m in range(4)
            ]
        )
        kinds = ("Cable", "Magnetotérmico", "Tubo corrugado", "Caja de registro", "Luminaria LED")
        materials = [
            Material(
                id=new_id(),
                reference=f"VOL-{number:06}",
                name=f"{self.rng.choice(kinds)} serie {number}",
                unit=units[number % len(units)],
                category=children[number % len(children)],
                is_active=number % 25 != 0,
            )
            for number in range(self.scale.materials)
        ]
        _insert(Material, materials)
        return self.done("materials", materials)

    def suppliers(self) -> list[Supplier]:
        rows = [
            Supplier(
                id=new_id(),
                name=f"Proveedor volumen {number:04}",
                tax_id=f"V{number:08}",
                email=f"proveedor{number}@volume.example",
                is_active=number % 20 != 0,
            )
            for number in range(self.scale.suppliers)
        ]
        _insert(Supplier, rows)
        return self.done("suppliers", rows)

    def organization(self) -> list[Company]:
        """Companies of the group and their branches (M10.2's lists); one in ten active."""
        companies = [
            Company(
                id=new_id(),
                code=f"V{number:04}",
                legal_name=f"Empresa volumen {number:04}",
                kind=Company.Kind.JOINT_VENTURE if number % 7 == 0 else Company.Kind.STANDARD,
                is_active=number % 10 == 0,
            )
            for number in range(self.scale.companies)
        ]
        _insert(Company, companies)
        branches = [
            Branch(
                id=new_id(),
                company=company,
                code=code,
                name=f"{company.legal_name} {code}",
                city="Volumen",
                is_active=code == "HQ" or number % 3 != 0,
            )
            for number, company in enumerate(companies)
            for code in ("HQ", "NORTE")
        ]
        _insert(Branch, branches)
        self.done("branches", branches)
        return self.done("companies", companies)

    def projects(self) -> list[Project]:
        org = self.org
        placements = (
            (org.a_mad, org.a_mad, 4),
            (org.a_sev, org.a_sev, 3),
            (org.b_mad, org.a_mad, 2),
            (org.a_sev, org.a_mad, 1),
        )
        weighted = tuple(((branch, buyer), weight) for branch, buyer, weight in placements)
        rows = []
        for number in range(self.scale.projects):
            branch, buyer = _pick(self.rng, weighted)
            rows.append(
                Project(
                    id=new_id(),
                    code=f"VOL-{number:04}",
                    name=f"Obra de volumen {number}",
                    client_name=f"Cliente {number % 40}",
                    branch=branch,
                    procurement_branch=buyer,
                    status=_pick(
                        self.rng,
                        (
                            (Project.Status.ACTIVE, 8),
                            (Project.Status.ON_HOLD, 1),
                            (Project.Status.CLOSED, 1),
                        ),
                    ),
                )
            )
        _insert(Project, rows)
        sites = [
            Location(
                id=new_id(),
                kind=Location.Kind.WORKSITE,
                branch=project.branch,
                project=project,
                name=f"Obra {project.code}",
            )
            for project in rows
        ]
        _insert(Location, sites)
        for site in [*sites, *Location.objects.filter(kind=Location.Kind.WORKSITE)]:
            self.worksite[site.project_id] = site.pk
        return self.done("projects", rows)

    def users(self) -> list[User]:
        org = self.org
        mix = (
            (Role.TECHNICIAN, 60),
            (Role.WAREHOUSE_OPERATOR, 10),
            (Role.MATERIAL_MANAGER, 6),
            (Role.SITE_MANAGER, 14),
            (Role.PARTNER_REQUESTER, 10),
        )
        users, memberships = [], []
        branches = (org.a_mad, org.a_mad, org.a_sev, org.b_mad)
        for number in range(self.scale.users):
            user = User(
                id=new_id(),
                email=f"vol-{number}@volume.example",
                full_name=f"Persona {number}",
                password="!",
                employer_company=org.a,
                is_active=number % 30 != 0,
            )
            role = _pick(self.rng, mix)
            partner = role is Role.PARTNER_REQUESTER
            users.append(user)
            memberships.append(
                Membership(
                    id=new_id(),
                    user=user,
                    role=role,
                    branch=None if partner else branches[number % len(branches)],
                    company=org.c if partner else None,
                    valid_from=self.start,
                    revoked_at=self.now - timedelta(days=3) if number % 20 == 0 else None,
                )
            )
        _insert(User, users)
        _insert(Membership, memberships)
        self.done("users", users)
        self.done("memberships", memberships)
        return users

    def shared_projects(self, users: list[User], projects: list[Project]) -> tuple[Project, User]:
        """Assignments: the site manager's 60 projects, the hot project's 120, the busy
        person's 40 and 30 roles."""
        manager, partner = ProjectAssignment.Kind.MANAGER, ProjectAssignment.Kind.PARTNER
        hot, busy = projects[0], users[1]
        rows = [
            ProjectAssignment(
                id=new_id(),
                project=project,
                user=self.people.site_manager,
                kind=manager,
                valid_from=self.start,
            )
            for project in projects[1:60]
        ]
        for number, user in enumerate(users[2 : 2 + HOT_ASSIGNMENTS]):
            revoked = self.now - timedelta(days=2) if number % 4 == 0 else None
            rows.append(
                ProjectAssignment(
                    id=new_id(),
                    project=hot,
                    user=user,
                    kind=manager if number % 2 else partner,
                    valid_from=self.start,
                    revoked_at=revoked,
                )
            )
        for number, project in enumerate(projects[1:41]):
            revoked = self.now - timedelta(days=2) if number % 5 == 0 else None
            rows.append(
                ProjectAssignment(
                    id=new_id(),
                    project=project,
                    user=busy,
                    kind=partner,
                    valid_from=self.start,
                    revoked_at=revoked,
                )
            )
        _insert(ProjectAssignment, rows)
        roles = [
            Membership(
                id=new_id(),
                user=busy,
                role=Role.TECHNICIAN,
                branch=self.org.a_mad if number % 2 else self.org.a_sev,
                valid_from=self.start + timedelta(days=number),
                revoked_at=self.now - timedelta(days=1) if number < 29 else None,
            )
            for number in range(30)
        ]
        _insert(Membership, roles)
        self.counts["assignments"] = len(rows)
        return hot, busy

    def invitations(self, users: list[User], busy: User) -> None:
        group_admin = self.people.group_admin
        invitations, mails = [], []
        for number in range(self.scale.invitations):
            existing = number < 30  # the busy person's
            email = busy.email if existing else f"invitada-{number}@volume.example"
            accepted = number % 7 == 0
            invitation = Invitation(
                id=new_id(),
                email=email,
                kind=Invitation.Kind.EXISTING_ACCOUNT if existing else Invitation.Kind.NEW_ACCOUNT,
                target_user=busy if existing else None,
                invited_by=group_admin,
                full_name=f"Invitada {number}",
                employer_company=self.org.a,
                token_hash=hashlib.sha256(f"volume-{number}".encode()).hexdigest(),
                expires_at=self.now + timedelta(days=14 - number % 30),
                accepted_at=self.now - timedelta(days=1) if accepted else None,
                created_at=_spread(number, self.scale.invitations, self.start),
            )
            invitations.append(invitation)
            if number % 5 != 4:  # a fifth was never mailed
                mails.append(
                    EmailOutbox(
                        id=new_id(),
                        kind=Kind.SECURITY,
                        status=_pick(
                            self.rng,
                            ((Status.SENT, 6), (Status.QUEUED, 2), (Status.FAILED, 1)),
                        ),
                        sensitive=True,
                        recipient_address=email,
                        eligibility_rule="account.active",
                        eligibility_object_type="accounts.invitation",
                        eligibility_object_id=invitation.pk,
                        source_ref=f"invitation:{invitation.pk}",
                        from_email="noreply@volume.example",
                        subject="Invitation",
                        expires_at=self.now + timedelta(days=1),
                        created_at=invitation.created_at,
                    )
                )
        _insert(Invitation, invitations)
        _insert(EmailOutbox, mails)
        self.done("invitations", invitations)

    # --- requests and orders -----------------------------------------------------------

    def requests(
        self, projects: list[Project], users: list[User], materials: list[Material]
    ) -> tuple[list[MaterialRequest], list[MaterialRequestLine], User]:
        requesters = [self.people.tech_mad, *users[:200]]
        requests: list[MaterialRequest] = []
        lines: list[MaterialRequestLine] = []
        for number in range(self.scale.requests):
            request = self.new_request(number, projects, requesters)
            requests.append(request)
            lines.extend(self.request_lines(request, materials))
        _insert(MaterialRequest, requests)
        _insert(MaterialRequestLine, lines)
        self.done("requests", requests)
        self.done("request_lines", lines)
        return requests, lines, self.people.tech_mad

    def new_request(
        self, number: int, projects: list[Project], requesters: list[User]
    ) -> MaterialRequest:
        rng = self.rng
        project = self.org.hospital if rng.random() < 0.08 else rng.choice(projects)
        status = _pick(rng, STATUS_MIX)
        created = _spread(number, self.scale.requests, self.start)
        return MaterialRequest(
            id=new_id(),
            number=f"SOL-{project.code}-{number:06}" if status not in UNNUMBERED else None,
            project=project,
            procurement_branch_id=project.procurement_branch_id,
            requested_by=self.people.tech_mad if number % 10 == 0 else rng.choice(requesters),
            priority=_pick(rng, PRIORITY_MIX),
            needed_by=(created + timedelta(days=rng.randrange(5, 90))).date(),
            status=status,
            created_at=created,
            **_request_times(created, status),
            **self.assignment(number, status, created),
        )

    def assignment(self, number: int, status: str, created: datetime) -> dict[str, Any]:
        """A third of the requests in review are sent to the engineer."""
        if status not in ("SUBMITTED", "APPROVED") or number % 3:
            return {}
        return {
            "assigned_reviewer": self.people.engineer,
            "assigned_at": created + timedelta(hours=2),
            "assigned_by": self.people.mm_mad,
        }

    def request_lines(
        self, request: MaterialRequest, materials: list[Material]
    ) -> list[MaterialRequestLine]:
        return [
            self.request_line(request, line_no, materials)
            for line_no in range(1, self.scale.lines_per_request + 1)
        ]

    def request_line(
        self, request: MaterialRequest, line_no: int, materials: list[Material]
    ) -> MaterialRequestLine:
        rng = self.rng
        free_text = line_no == 5 and rng.random() < 0.2
        material = None if free_text else rng.choice(materials)
        quantity = Decimal(rng.randrange(1, 200))
        return MaterialRequestLine(
            id=new_id(),
            request=request,
            project_id=request.project_id,
            line_no=line_no,
            material=material,
            free_text="Pieza especial a describir" if free_text else "",
            unit_id=material.unit_id if material else self.default_unit.pk,
            requested_qty=quantity,
            created_at=request.created_at,
            **_decision(request, line_no, quantity, material is not None, self.people.mm_mad),
        )

    def orders(
        self, lines: list[MaterialRequestLine], requests: list[MaterialRequest]
    ) -> tuple[list[PurchaseOrder], list[PurchaseOrderLine], list[PurchaseAllocation]]:
        pool = self.line_pools(lines, requests)
        suppliers = list(Supplier.objects.all())
        orders: list[PurchaseOrder] = []
        order_lines: list[PurchaseOrderLine] = []
        allocations: list[PurchaseAllocation] = []
        for number in range(self.scale.orders):
            live = self.rng.random() < OPEN_ORDER_SHARE
            picked = self.pick_lines(pool, live)
            if not picked:
                continue
            order = self.new_order(number, picked[0], live, self.rng.choice(suppliers))
            orders.append(order)
            for po_line, allocation in self.order_lines(order, picked, live, number):
                order_lines.append(po_line)
                allocations.append(allocation)
        _insert(PurchaseOrder, orders)
        _insert(PurchaseOrderLine, order_lines)
        _insert(PurchaseAllocation, allocations)
        self.done("orders", orders)
        self.done("order_lines", order_lines)
        self.done("allocations", allocations)
        return orders, order_lines, allocations

    def line_pools(
        self, lines: list[MaterialRequestLine], requests: list[MaterialRequest]
    ) -> dict[tuple[Any, bool], list[MaterialRequestLine]]:
        """The lines to buy, by branch and by whether their request is still being worked on:
        the open ones go on orders in flight, the rest on orders already closed."""
        self.branch_of_line = {request.pk: request.procurement_branch_id for request in requests}
        open_ids = {request.pk for request in requests if request.status == "APPROVED"}
        pool: dict[tuple[Any, bool], list[MaterialRequestLine]] = {}
        for line in lines:
            if line.sourcing == "PURCHASE" and line.approved_qty:
                key = (self.branch_of_line[line.request_id], line.request_id in open_ids)
                pool.setdefault(key, []).append(line)
        for rows in pool.values():
            self.rng.shuffle(rows)
        return pool

    def pick_lines(
        self, pool: dict[tuple[Any, bool], list[MaterialRequestLine]], live: bool
    ) -> list[MaterialRequestLine]:
        """The lines of one order: a branch (the more lines it has left the likelier), and a
        few of its lines for different materials."""
        sizes = tuple((pk, len(pool.get((pk, live), []))) for pk in self.warehouse)
        if not any(size for _, size in sizes):
            return []
        key = (_pick(self.rng, sizes), live)
        picked: dict[Any, MaterialRequestLine] = {}
        while len(picked) < self.scale.lines_per_order and pool.get(key):
            line = pool[key].pop()
            picked.setdefault(line.material_id, line)
        return list(picked.values())

    def new_order(
        self, number: int, first: MaterialRequestLine, live: bool, supplier: Supplier
    ) -> PurchaseOrder:
        branch = self.branches[self.branch_of_line[first.request_id]]
        status = _pick(self.rng, IN_FLIGHT) if live else "CLOSED"
        created = _spread(number, self.scale.orders, self.start + timedelta(days=2))
        issued = status != "DRAFT"
        return PurchaseOrder(
            id=new_id(),
            number=f"PED-{branch.company.code}-{number:06}" if issued else None,
            branch=branch,
            buyer_company_id=branch.company_id,
            supplier=supplier,
            ship_to=self.warehouse[branch.pk],
            status=status,
            expected_date=(created + timedelta(days=self.rng.randrange(3, 40))).date(),
            issued_at=created if issued else None,
            issued_by=self.people.mm_mad if issued else None,
            supplier_snapshot={"name": supplier.name} if issued else {},
            created_at=created,
        )

    def order_lines(
        self, order: PurchaseOrder, picked: list[MaterialRequestLine], live: bool, number: int
    ) -> list[tuple[PurchaseOrderLine, PurchaseAllocation]]:
        # One in-flight order in four was placed for more than the lines it serves (a cut freed
        # the rest): quantity on order for nobody.
        extra = Decimal(5) if live and number % 4 == 0 else Decimal(0)
        rows = []
        for line_no, line in enumerate(picked, start=1):
            assert line.material_id is not None and line.approved_qty is not None
            po_line = PurchaseOrderLine(
                id=new_id(),
                po=order,
                line_no=line_no,
                material_id=line.material_id,
                unit_id=line.unit_id,
                ordered_qty=line.approved_qty + extra,
                unit_price=Decimal(self.rng.randrange(1, 900)) / 10,
                material_snapshot={"reference": "VOL"},
                created_at=order.created_at,
            )
            allocation = PurchaseAllocation(
                id=new_id(),
                po_line=po_line,
                request_line=line,
                qty=line.approved_qty,
                created_at=order.created_at,
            )
            rows.append((po_line, allocation))
        return rows

    # --- receipts and handovers --------------------------------------------------------

    def receipts(
        self,
        orders: list[PurchaseOrder],
        order_lines: list[PurchaseOrderLine],
        allocations: list[PurchaseAllocation],
    ) -> list[GoodsReceipt]:
        lines_of: dict[Any, list[PurchaseOrderLine]] = {}
        for ordered in order_lines:
            lines_of.setdefault(ordered.po_id, []).append(ordered)
        allocation_of = {allocation.po_line_id: allocation for allocation in allocations}
        receipts: list[GoodsReceipt] = []
        receipt_lines: list[GoodsReceiptLine] = []
        attributions: list[ReceiptAttribution] = []
        for number, (order, nth) in enumerate(self.receipt_plan(orders)):
            lines = lines_of[order.pk]
            project_id = allocation_of[lines[0].pk].request_line.project_id
            receipt = self.new_receipt(number, order, nth, project_id)
            receipts.append(receipt)
            share = _receipt_share(order, nth)
            for po_line in lines:
                allocation = allocation_of[po_line.pk]
                quantity = (allocation.qty * share).quantize(Decimal("0.001"))
                line = GoodsReceiptLine(
                    id=new_id(),
                    receipt=receipt,
                    po=order,
                    po_line=po_line,
                    qty_received=quantity,
                    created_at=receipt.created_at,
                )
                receipt_lines.append(line)
                attribution = ReceiptAttribution(
                    id=new_id(),
                    receipt_line=line,
                    allocation=allocation,
                    po_line=po_line,
                    qty=quantity,
                    created_at=receipt.created_at,
                )
                attributions.append(attribution)
                self.attribution_of.setdefault(receipt.pk, attribution)
        _insert(GoodsReceipt, receipts)
        _insert(GoodsReceiptLine, receipt_lines)
        _insert(ReceiptAttribution, attributions)
        self.done("receipts", receipts)
        self.done("receipt_lines", receipt_lines)
        self.done("attributions", attributions)
        return receipts

    def receipt_plan(self, orders: list[PurchaseOrder]) -> list[tuple[PurchaseOrder, int]]:
        """Every received order gets a receipt (a closed one in full, an issued one for half),
        and some issued orders a second one for a quarter."""
        received = [order for order in orders if order.status in ("ISSUED", "CLOSED")]
        issued = [order for order in received if order.status == "ISSUED"]
        plan = [(order, 1) for order in received]
        plan += [(order, 2) for order in issued[: max(self.scale.receipts - len(received), 0)]]
        return plan[: self.scale.receipts]

    def new_receipt(
        self, number: int, order: PurchaseOrder, nth: int, project_id: Any
    ) -> GoodsReceipt:
        assert order.issued_at is not None
        at = order.issued_at + timedelta(days=nth * 3)
        at_site = number % 5 == 0
        receipt = GoodsReceipt(
            id=new_id(),
            number=f"REC-{order.branch_id.hex[:6]}-{number:06}",
            po=order,
            branch_id=order.branch_id,
            location_id=self.worksite[project_id] if at_site else order.ship_to_id,
            received_at=at,
            received_by=self.people.wo_mad,
            created_at=at,
            **_voiding(number % 33 == 0, at, self.people.wo_mad, "Duplicado"),
        )
        if at_site and nth == 1 and receipt.status == "POSTED":
            self.site_receipts.append((receipt, project_id))
        return receipt

    def direct_handovers(self) -> tuple[list[Handover], list[HandoverLine]]:
        """What was received at a worksite is handed over with the receipt (one line: the first
        attribution, whose project is the handover's)."""
        handovers: list[Handover] = []
        handover_lines: list[HandoverLine] = []
        for number, (receipt, project_id) in enumerate(self.site_receipts):
            attribution = self.attribution_of[receipt.pk]
            handover = Handover(
                id=new_id(),
                number=f"ENT-D-{number:06}",
                kind="DIRECT_RECEIPT",
                branch_id=receipt.branch_id,
                source_location_id=receipt.location_id,
                project_id=project_id,
                handed_by=self.people.wo_mad,
                received_by_name="Capataz de obra",
                handed_at=receipt.received_at,
                goods_receipt=receipt,
                created_at=receipt.created_at,
            )
            handovers.append(handover)
            handover_lines.append(
                HandoverLine(
                    id=new_id(),
                    handover=handover,
                    project_id=project_id,
                    request_line=attribution.allocation.request_line,
                    qty=attribution.qty,
                    receipt_attribution=attribution,
                    created_at=receipt.created_at,
                )
            )
        return handovers, handover_lines

    def handovers(self, lines: list[MaterialRequestLine], requests: list[MaterialRequest]) -> None:
        by_id = {request.pk: request for request in requests}
        candidates = [
            line
            for line in lines
            if line.decision == "APPROVED" and line.approved_qty and line.approved_qty >= 2
        ]
        self.rng.shuffle(candidates)
        handovers, handover_lines = self.direct_handovers()
        for number in range(max(self.scale.handovers - len(handovers), 0)):
            line = candidates.pop()
            handover = self.new_handover(number, by_id[line.request_id])
            handovers.append(handover)
            handover_lines.append(
                HandoverLine(
                    id=new_id(),
                    handover=handover,
                    project_id=handover.project_id,
                    request_line=line,
                    qty=Decimal(1),
                    created_at=handover.created_at,
                )
            )
        _insert(Handover, handovers)
        _insert(HandoverLine, handover_lines)
        self.done("handovers", handovers)
        self.counts["direct_handovers"] = len(self.site_receipts)

    def new_handover(self, number: int, request: MaterialRequest) -> Handover:
        branch_id = request.procurement_branch_id
        at = _spread(number, self.scale.handovers, self.start + timedelta(days=10))
        return Handover(
            id=new_id(),
            number=f"ENT-{branch_id.hex[:6]}-{number:06}",
            kind="FROM_WAREHOUSE",
            branch_id=branch_id,
            source_location=self.warehouse[branch_id],
            project_id=request.project_id,
            handed_by=self.people.wo_mad,
            received_by_name="Capataz de obra",
            handed_at=at,
            created_at=at,
            **_voiding(number % 40 == 0, at, self.people.wo_mad, "Error"),
        )

    # --- audit -------------------------------------------------------------------------

    def audit(self, requests: list[MaterialRequest], users: list[User]) -> MaterialRequest:
        rng, scale = self.rng, self.scale
        branches = [
            self.org.a_mad.pk,
            self.org.a_mad.pk,
            self.org.a_sev.pk,
            self.org.b_mad.pk,
            self.org.a_hq.pk,
            self.org.c_hq.pk,
            None,
        ]
        actions = ("request.submitted", "request.approved", "po.issued", "receipt.posted", "login")
        types = (
            "material_requests.materialrequest",
            "purchasing.purchaseorder",
            "receiving.goodsreceipt",
            "handovers.handover",
            "catalog.material",
        )
        hot = next(r for r in requests if r.procurement_branch_id == self.org.a_mad.pk)
        actors = [self.people.mm_mad, self.people.tech_mad, *users[:60]]
        events = []
        for number in range(scale.audit_events):
            actor = rng.choice(actors)
            hot_event = number < HOT_AUDIT_EVENTS
            events.append(
                AuditEvent(
                    id=new_id(),
                    occurred_at=_spread(number, scale.audit_events, self.start),
                    actor=actor,
                    actor_label=actor.email,
                    via=rng.choice(("", "invitation", "cli:seed")),
                    action=rng.choice(actions),
                    object_type=types[0] if hot_event else rng.choice(types),
                    object_id=hot.pk if hot_event else new_id(),
                    branch_id=hot.procurement_branch_id if hot_event else rng.choice(branches),
                )
            )
        _insert(AuditEvent, events)
        self.counts["audit_events"] = len(events)
        return hot


def build_volume(org: Org, people: People, scale: Scale | None = None) -> Volume:
    """Build the dataset inside the current transaction and ``ANALYZE`` it."""
    scale = scale or Scale()
    builder = _Builder(org, people, scale)
    materials = builder.catalog()
    builder.suppliers()
    builder.organization()
    projects = builder.projects()
    users = builder.users()
    hot_project, busy = builder.shared_projects(users, projects)
    builder.invitations(users, busy)
    requests, lines, requester = builder.requests(projects, users, materials)
    orders, order_lines, allocations = builder.orders(lines, requests)
    builder.receipts(orders, order_lines, allocations)
    builder.handovers(lines, requests)
    hot_request = builder.audit(requests, users)
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")  # the deferred foreign keys, now
        cursor.execute("SET CONSTRAINTS ALL DEFERRED")
        cursor.execute("ANALYZE")  # what autovacuum has done within minutes in production
    return Volume(
        scale=scale,
        hot_project=hot_project,
        hot_request=hot_request,
        busy_person=busy,
        requester=requester,
        counts=builder.counts,
    )
