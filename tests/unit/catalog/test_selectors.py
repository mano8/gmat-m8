"""Catalog reads (plan §4.2, M3.1): scope, search and its accent-insensitivity."""

from typing import Any

import pytest

from apps.access.context import AccessContext
from apps.access.exceptions import NotInScope
from apps.catalog import selectors, services
from apps.catalog.models import Material, UnitOfMeasure
from tests.org import Org

pytestmark = pytest.mark.django_db


@pytest.fixture
def catalog(admin_principal: Any) -> dict[str, Material]:
    unit = UnitOfMeasure.objects.get(code="UD")
    meter = UnitOfMeasure.objects.get(code="M")
    cables = services.create_category(admin_principal, name="Cables y conductores")
    protection = services.create_category(admin_principal, name="Protección")
    rigid = services.create_category(admin_principal, name="Rígidos", parent_id=cables.pk)

    def add(reference: str, name: str, **extra: Any) -> Material:
        extra.setdefault("unit_id", unit.pk)
        return services.create_material(admin_principal, reference=reference, name=name, **extra)

    return {
        "cable25": add(
            "CAB-25", "Cable H07V-K 1x2,5 mm² azul", unit_id=meter.pk, category_id=cables.pk
        ),
        "cable4": add("CAB-40", "Cable H07V-K 1x4 mm²", unit_id=meter.pk, category_id=cables.pk),
        "diff": add("DIF-40-30", "Interruptor diferencial 2P 40A 30mA", category_id=protection.pk),
        "mag": add("MAG-16", "Magnetotérmico 1P+N 16A", category_id=protection.pk),
        "rigid": add("TUB-20", "Tubo rígido 20 mm", category_id=rigid.pk),
        "old": add("OLD-1", "Cable descatalogado 2,5"),
    }


def ctx_for(user: Any) -> AccessContext:
    return AccessContext.for_user(user)


def refs(rows: Any) -> list[str]:
    return [row.reference for row in rows]


class TestSearch:
    def test_cable_2_5_finds_the_2_5_cables(self, group_admin: Any, catalog: Any) -> None:
        found = selectors.search(ctx_for(group_admin), "cable 2,5")
        assert refs(found) == ["CAB-25", "OLD-1"]

    def test_a_decimal_matches_with_either_separator(self, group_admin: Any, catalog: Any) -> None:
        assert refs(selectors.search(ctx_for(group_admin), "cable 2.5")) == ["CAB-25", "OLD-1"]

    @pytest.mark.parametrize("query", ["diferencial", "DIFERENCIAL", "Diferéncial", "  difer "])
    def test_case_and_accents_do_not_matter(
        self, group_admin: Any, catalog: Any, query: str
    ) -> None:
        assert refs(selectors.search(ctx_for(group_admin), query)) == ["DIF-40-30"]

    def test_accented_names_are_found_without_accents(self, group_admin: Any, catalog: Any) -> None:
        assert refs(selectors.search(ctx_for(group_admin), "magnetotermico")) == ["MAG-16"]
        assert refs(selectors.search(ctx_for(group_admin), "rigido")) == ["TUB-20"]

    def test_the_reference_matches_and_an_exact_one_comes_first(
        self, group_admin: Any, catalog: Any
    ) -> None:
        found = selectors.search(ctx_for(group_admin), "dif-40")
        assert refs(found) == ["DIF-40-30"]
        assert refs(selectors.search(ctx_for(group_admin), "mag-16")) == ["MAG-16"]

    def test_the_category_matches_with_its_subcategories(
        self, group_admin: Any, catalog: Any
    ) -> None:
        by_category = selectors.search(ctx_for(group_admin), "proteccion")
        assert sorted(refs(by_category)) == ["DIF-40-30", "MAG-16"]
        parent = selectors.search(ctx_for(group_admin), "conductores")
        assert sorted(refs(parent)) == ["CAB-25", "CAB-40", "TUB-20"]

    def test_every_term_must_match(self, group_admin: Any, catalog: Any) -> None:
        assert refs(selectors.search(ctx_for(group_admin), "cable diferencial")) == []

    def test_inactive_materials_are_not_offered(
        self, admin_principal: Any, group_admin: Any, catalog: Any
    ) -> None:
        services.deactivate_material(admin_principal, catalog["old"].pk)
        assert refs(selectors.search(ctx_for(group_admin), "cable 2,5")) == ["CAB-25"]

    def test_wildcards_in_the_query_are_literal(self, group_admin: Any, catalog: Any) -> None:
        for query in ("%", "_", "c%e", "\\"):
            assert selectors.search(ctx_for(group_admin), query) == []

    def test_blank_queries_and_the_limit(self, admin_principal: Any, group_admin: Any) -> None:
        unit = UnitOfMeasure.objects.get(code="UD")
        for number in range(25):
            services.create_material(
                admin_principal, reference=f"BULK-{number:02}", name="Bulk", unit_id=unit.pk
            )
        ctx = ctx_for(group_admin)
        assert selectors.search(ctx, "   ") == []
        assert len(selectors.search(ctx, "bulk")) == selectors.SEARCH_LIMIT
        assert len(selectors.search(ctx, "bulk", limit=3)) == 3

    def test_a_result_carries_no_price(self, group_admin: Any, catalog: Any) -> None:
        (found,) = selectors.search(ctx_for(group_admin), "mag-16")
        assert not [f for f in found._meta.get_fields() if "price" in f.name]


class TestScope:
    @pytest.mark.parametrize(
        "who",
        ["group_admin", "mm_mad", "wo_mad", "tech_mad", "site_manager", "partner", "mixed"],
    )
    def test_everyone_with_a_membership_reads_the_whole_catalog(
        self, org: Org, people: Any, catalog: Any, who: str
    ) -> None:
        ctx = ctx_for(getattr(people, who))
        assert len(selectors.materials(ctx, state=selectors.STATE_ALL)) == len(catalog)
        assert refs(selectors.search(ctx, "diferencial")) == ["DIF-40-30"]
        assert selectors.material(ctx, catalog["diff"].pk) == catalog["diff"]

    @pytest.mark.parametrize(
        "who",
        ["expired", "revoked", "inactive_partner", "inactive_branch", "deactivated", "nobody"],
    )
    def test_nobody_without_a_valid_membership_reads_anything(
        self, org: Org, people: Any, catalog: Any, who: str
    ) -> None:
        user = getattr(people, who)
        user.refresh_from_db()  # the fixture deactivates by queryset
        ctx = ctx_for(user)
        assert len(selectors.materials(ctx, state=selectors.STATE_ALL)) == 0
        assert selectors.search(ctx, "diferencial") == []
        assert list(selectors.categories(ctx)) == []
        with pytest.raises(NotInScope):
            selectors.material(ctx, catalog["diff"].pk)

    def test_manageable(self, org: Org, people: Any, catalog: Any) -> None:
        manages = {
            who: selectors.manageable(ctx_for(getattr(people, who)))
            for who in ("group_admin", "mm_mad", "wo_mad", "tech_mad", "partner")
        }
        assert manages == {
            "group_admin": True,
            "mm_mad": True,
            "wo_mad": False,
            "tech_mad": False,
            "partner": False,
        }
        assert selectors.manageable(ctx_for(people.mm_mad), catalog["diff"])
        assert not selectors.manageable(ctx_for(people.tech_mad), catalog["diff"])


class TestList:
    def test_states(self, admin_principal: Any, group_admin: Any, catalog: Any) -> None:
        services.deactivate_material(admin_principal, catalog["old"].pk)
        ctx = ctx_for(group_admin)
        assert "OLD-1" not in refs(selectors.materials(ctx))
        assert refs(selectors.materials(ctx, state=selectors.STATE_INACTIVE)) == ["OLD-1"]
        assert "OLD-1" in refs(selectors.materials(ctx, state=selectors.STATE_ALL))

    def test_filter_by_category_includes_subcategories(
        self, group_admin: Any, catalog: Any
    ) -> None:
        ctx = ctx_for(group_admin)
        root = catalog["cable25"].category
        assert refs(selectors.materials(ctx, category_id=root.pk)) == ["CAB-25", "CAB-40", "TUB-20"]
        assert refs(selectors.materials(ctx, category_id=catalog["rigid"].category_id)) == [
            "TUB-20"
        ]

    def test_text_and_order(self, group_admin: Any, catalog: Any) -> None:
        ctx = ctx_for(group_admin)
        # TUB-20 matches through its category, a subcategory of "Cables y conductores".
        assert refs(selectors.materials(ctx, q="cable")) == ["CAB-25", "CAB-40", "OLD-1", "TUB-20"]
        assert refs(selectors.materials(ctx, q="cable 4")) == ["CAB-40"]

    def test_the_category_tree(self, group_admin: Any, catalog: Any) -> None:
        ctx = ctx_for(group_admin)
        names = [str(category) for category in selectors.categories(ctx)]
        assert names == [
            "Cables y conductores",
            "Cables y conductores / Rígidos",
            "Protección",
        ]
        assert [c.name for c in selectors.root_categories(ctx)] == [
            "Cables y conductores",
            "Protección",
        ]
        assert [u.code for u in selectors.units(ctx)][:2] == ["BOLSA", "BOTE"]


def test_an_unknown_or_invisible_category_is_not_in_scope(
    org: Org, people: Any, catalog: Any
) -> None:
    known = catalog["cable25"].category
    assert selectors.category(ctx_for(people.tech_mad), known.pk) == known
    with pytest.raises(NotInScope):
        selectors.category(ctx_for(people.nobody), known.pk)
