"""The templates in ``docs/import-templates/`` stay valid: each one imports cleanly."""

from pathlib import Path
from typing import Any

import pytest

from apps.catalog.models import Material
from apps.projects.models import Project
from apps.suppliers.models import Supplier
from tests.imports import run_import
from tests.org import Org

pytestmark = pytest.mark.django_db

TEMPLATES = Path(__file__).resolve().parents[2] / "docs" / "import-templates"


@pytest.mark.parametrize(
    ("command", "template", "model", "created"),
    [
        ("import_catalog", "catalog.csv", Material, 2),
        ("import_suppliers", "suppliers.csv", Supplier, 2),
        ("import_projects", "projects.csv", Project, 2),
    ],
)
def test_each_template_imports(
    org: Org, people: Any, command: str, template: str, model: Any, created: int
) -> None:
    before = model.objects.count()
    out = run_import(command, TEMPLATES / template, people.group_admin.email)
    assert f"{created} created" in out and "Ignored columns" not in out
    assert model.objects.count() == before + created
    again = run_import(command, TEMPLATES / template, people.group_admin.email)
    assert f"{created} unchanged" in again
