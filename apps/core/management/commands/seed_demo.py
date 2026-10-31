"""``manage seed_demo --operator <email> [--password …]`` (plan M3.4): demo data for development.

Creates, through the same services as the app and in one transaction, the A/B/C organization
of plan §3.2 (companies, the joint venture, branches, warehouses), one user per role plus a
user with mixed memberships, projects (one on hold), about 200 materials and five suppliers.
It is idempotent: whatever exists is left alone, so running it again adds only what is
missing and a second run changes nothing.

Only for development and tests: it refuses to run unless the settings are
``config.settings.dev`` or ``config.settings.test``. The operator is an existing group
administrator (create the first one with ``manage bootstrap_admin``). New demo users get
the ``--password`` (or a random one, printed once); privileged roles enroll MFA at their
first sign-in, like any user.
"""

import os
import secrets
from typing import Any, ClassVar
from uuid import UUID

from allauth.account.models import EmailAddress
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import CommandError, CommandParser

from apps.access.context import AccessContext
from apps.access.principals import OperatorPrincipal
from apps.access.roles import Capability
from apps.accounts.models import User
from apps.catalog import selectors as catalog_selectors
from apps.catalog import services as catalog_services
from apps.core import demo
from apps.core.commands import OperatorCommand
from apps.core.locking import RetryExhausted, run_in_transaction
from apps.locations.models import Location
from apps.locations.services import create_warehouse
from apps.organizations import services as org_services
from apps.organizations.models import Branch, Company, JointVentureMember, Membership
from apps.projects import selectors as project_selectors
from apps.projects import services as project_services
from apps.projects.models import ProjectAssignment
from apps.suppliers import selectors as supplier_selectors
from apps.suppliers import services as supplier_services

ALLOWED_SETTINGS = frozenset({"config.settings.dev", "config.settings.test"})
MIN_PASSWORD_LENGTH = 12


class Command(OperatorCommand):
    help = "Create demo data for development (organization, people, projects, catalog, suppliers)."
    required_capability: ClassVar[str] = Capability.ORG_MANAGE
    also_capabilities: ClassVar[tuple[str, ...]] = (
        Capability.PROJECT_MANAGE,
        Capability.CATALOG_MANAGE,
        Capability.SUPPLIER_MANAGE,
    )

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument(
            "--password",
            help="Password for the demo users created now (default: a random one, printed once)",
        )

    def handle(self, *args: Any, **options: Any) -> str | None:
        module = os.environ.get("DJANGO_SETTINGS_MODULE", "")
        if module not in ALLOWED_SETTINGS:
            raise CommandError(
                "seed_demo refuses to run with production settings "
                f"(DJANGO_SETTINGS_MODULE={module or 'unset'}); it is for development only."
            )
        return super().handle(*args, **options)

    def handle_operator(
        self, principal: OperatorPrincipal, *args: Any, **options: Any
    ) -> str | None:
        password = options["password"] or secrets.token_urlsafe(12)
        try:
            validate_password(password)
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc
        if len(password) < MIN_PASSWORD_LENGTH:
            raise CommandError(f"The password needs at least {MIN_PASSWORD_LENGTH} characters.")
        ctx = AccessContext.for_user(User.objects.get(pk=principal.user_id))
        seeding = _Seeding(self, principal, ctx, password)
        try:
            run_in_transaction(seeding.run)
        except RetryExhausted as exc:
            raise CommandError("seed_demo: the database stayed busy; retry") from exc
        for line in seeding.report():
            self.stdout.write(line)
        return None


class _Seeding:
    """One attempt at seeding; ``run`` is idempotent, so a retry starts clean."""

    def __init__(
        self, command: Command, org: OperatorPrincipal, ctx: AccessContext, password: str
    ) -> None:
        self.org = org
        self.project = command.principal_for(Capability.PROJECT_MANAGE)
        self.catalog = command.principal_for(Capability.CATALOG_MANAGE)
        self.suppliers = command.principal_for(Capability.SUPPLIER_MANAGE)
        self.ctx = ctx
        self.password = password
        self.created: dict[str, int] = {}
        self.existing: dict[str, int] = {}
        self.credentials: list[tuple[str, str]] = []

    def count(self, what: str, *, created: bool) -> None:
        bucket = self.created if created else self.existing
        bucket[what] = bucket.get(what, 0) + 1

    def reset(self) -> None:
        self.created.clear()
        self.existing.clear()
        self.credentials.clear()

    def run(self) -> None:
        self.reset()
        companies = self.organization()
        branches = self.branches(companies)
        users = self.users(companies, branches)
        self.projects(branches, users)
        self.catalog_and_suppliers()

    def report(self) -> list[str]:
        lines = ["seed_demo:"]
        for what in sorted(self.created | self.existing):
            made, kept = self.created.get(what, 0), self.existing.get(what, 0)
            lines.append(f"  {what}: {made} created, {kept} existing")
        if self.credentials:
            lines.append(f"Demo users created now (password shown once: {self.password}):")
            lines.extend(f"  {email}  {roles}" for email, roles in self.credentials)
        return lines

    # --- organization ------------------------------------------------------------

    def organization(self) -> dict[str, Company]:
        companies = {company.code: company for company in Company.objects.all()}
        for code, legal_name, kind in demo.COMPANIES:
            found = code in companies
            if not found:
                companies[code] = org_services.create_company(
                    self.org, code=code, legal_name=legal_name, kind=kind
                )
            self.count("companies", created=not found)
        for venture, member in demo.JOINT_VENTURE_MEMBERS:
            found = JointVentureMember.objects.filter(
                venture=companies[venture], member=companies[member]
            ).exists()
            if not found:
                org_services.add_joint_venture_member(
                    self.org, venture_id=companies[venture].pk, member_id=companies[member].pk
                )
            self.count("joint-venture members", created=not found)
        return companies

    def branches(self, companies: dict[str, Company]) -> dict[str, Branch]:
        def load() -> dict[str, Branch]:
            return {
                f"{branch.company.code}-{branch.code}": branch
                for branch in Branch.objects.visible_to(
                    self.ctx, Capability.ORG_MANAGE
                ).select_related("company")
            }

        branches = load()
        for company, code, name, city, managed_by in demo.BRANCHES:
            key = f"{company}-{code}"
            found = key in branches
            if not found:
                branches[key] = org_services.create_branch(
                    self.org,
                    company_id=companies[company].pk,
                    code=code,
                    name=name,
                    city=city,
                    managed_by_id=branches[managed_by].pk if managed_by else None,
                )
            self.count("branches", created=not found)
        for key, name in demo.WAREHOUSES:
            branch = branches[key]
            warehouse = (
                Location.objects.visible_to(self.ctx, Capability.ORG_MANAGE)
                .filter(kind=Location.Kind.WAREHOUSE, branch=branch, name=name)
                .first()
            )
            created = warehouse is None
            if warehouse is None:
                warehouse = create_warehouse(self.org, branch_id=branch.pk, name=name)
            if branch.default_warehouse_id is None:
                branches[key] = org_services.update_branch(
                    self.org, branch.pk, default_warehouse_id=warehouse.pk
                )
            self.count("warehouses", created=created)
        return branches

    # --- people ------------------------------------------------------------------

    def users(self, companies: dict[str, Company], branches: dict[str, Branch]) -> dict[str, User]:
        users: dict[str, User] = {}
        for spec in demo.USERS:
            user = User.objects.filter(email=spec.email).first()
            created = user is None
            if user is None:
                user = User.objects.create_user(
                    spec.email,
                    self.password,
                    full_name=spec.name,
                    employer_company=companies[spec.employer],
                )
                EmailAddress.objects.create(
                    user=user, email=user.email, primary=True, verified=True
                )
                self.credentials.append(
                    (spec.email, ", ".join(str(r) for r, _ in spec.memberships))
                )
            self.count("users", created=created)
            users[spec.key] = user
            for role, target in spec.memberships:
                self.membership(user, role, target, companies, branches)
        return users

    def membership(
        self,
        user: User,
        role: str,
        target: str,
        companies: dict[str, Company],
        branches: dict[str, Branch],
    ) -> None:
        company = companies.get(target)
        branch = branches.get(target)
        held = Membership.objects.filter(
            user=user, role=role, branch=branch, company=company, revoked_at__isnull=True
        ).exists()
        if not held:
            org_services.grant_membership(
                self.org,
                user_id=user.pk,
                role=role,
                branch_id=branch.pk if branch is not None else None,
                company_id=company.pk if company is not None else None,
            )
        self.count("memberships", created=not held)

    # --- projects ----------------------------------------------------------------

    def projects(self, branches: dict[str, Branch], users: dict[str, User]) -> None:
        for code, name, branch, procurement, status, client in demo.PROJECTS:
            project = project_selectors.project_by_code(self.ctx, code)
            created = project is None
            if project is None:
                project = project_services.create_project(
                    self.project,
                    code=code,
                    name=name,
                    branch_id=branches[branch].pk,
                    procurement_branch_id=branches[procurement].pk,
                    client_name=client,
                )
                if status != project.status:
                    project = project_services.update_project(
                        self.project, project.pk, status=status
                    )
            self.count("projects", created=created)
        for key, code, kind in demo.ASSIGNMENTS:
            project = project_selectors.project_by_code(self.ctx, code)
            if project is None:
                raise CommandError(f"The demo project {code} is missing")
            held = ProjectAssignment.objects.filter(
                project=project, user=users[key], kind=kind, revoked_at__isnull=True
            ).exists()
            if not held:
                project_services.assign_to_project(
                    self.project, project_id=project.pk, user_id=users[key].pk, kind=kind
                )
            self.count("assignments", created=not held)

    # --- catalog and suppliers ---------------------------------------------------

    def catalog_and_suppliers(self) -> None:
        units = catalog_selectors.units_by_code(self.ctx)
        categories: dict[str, UUID | None] = {}

        def category(path: str) -> UUID | None:
            parent_id: UUID | None = None
            for name in (part.strip() for part in path.split(">")):
                found = catalog_selectors.category_named(self.ctx, name, parent_id)
                if found is None:
                    found = catalog_services.create_category(
                        self.catalog, name=name, parent_id=parent_id
                    )
                    self.count("categories", created=True)
                parent_id = found.pk
            return parent_id

        for material in demo.materials():
            if catalog_selectors.material_by_reference(self.ctx, material.reference) is not None:
                self.count("materials", created=False)
                continue
            if material.category not in categories:
                categories[material.category] = category(material.category)
            catalog_services.create_material(
                self.catalog,
                reference=material.reference,
                name=material.name,
                unit_id=units[material.unit].pk,
                category_id=categories[material.category],
                manufacturer=material.manufacturer,
                is_stock_item=material.is_stock_item,
            )
            self.count("materials", created=True)
        for name, tax_id, contact, email, phone, address in demo.SUPPLIERS:
            found = supplier_selectors.supplier_by_tax_id(self.ctx, tax_id) is not None
            if not found:
                supplier_services.create_supplier(
                    self.suppliers,
                    name=name,
                    tax_id=tax_id,
                    contact_name=contact,
                    email=email,
                    phone=phone,
                    address=address,
                )
            self.count("suppliers", created=not found)
