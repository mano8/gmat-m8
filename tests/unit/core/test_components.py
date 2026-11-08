"""The component library of templates/components/ (ADR-0027, M8b.1): arguments and markup."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.utils import translation

from tests.unit.core.test_ui_shell import assert_csp_clean


def component(name: str, /, **context: object) -> str:
    return render_to_string(f"components/{name}.html", context)


class TestButton:
    @pytest.mark.parametrize(
        ("variant", "css"),
        [
            ("primary", "btn-primary"),
            ("secondary", "btn-secondary"),
            ("ghost", "btn-ghost"),
            ("danger", "btn-danger"),
            ("link", "btn-link"),
        ],
    )
    def test_each_variant_has_its_class(self, variant: str, css: str) -> None:
        assert css in component("button", label="Go", variant=variant)

    def test_size_icon_and_a_hidden_label(self) -> None:
        html = component("button", label="More", size="sm", icon="ellipsis", icon_only=True)
        assert "btn-sm" in html
        assert "btn-icon" in html
        assert "<svg" in html
        assert '<span class="sr-only">More</span>' in html

    def test_a_link_with_an_icon(self) -> None:
        html = component("button", label="Download", href="/x/", icon="download")
        assert html.strip().startswith("<a ")
        assert "<svg" in html
        assert "Download" in html

    def test_a_plain_button_has_no_variant_class(self) -> None:
        html = component("button", label="Go")
        assert 'class="btn"' in html


class TestStatusChip:
    def test_icon_tone_and_text(self) -> None:
        html = component("status_chip", label="Posted", tone="ok")
        assert "chip chip-ok" in html
        assert "<svg" in html
        assert "Posted" in html

    def test_the_icon_can_be_replaced(self) -> None:
        assert component("status_chip", label="X", tone="ok") != component(
            "status_chip", label="X", tone="ok", icon="clock"
        )


class TestEmptyState:
    def test_icon_sentence_and_action(self) -> None:
        html = component("empty_state", title="Nothing", text="Add one", href="/n/", label="New")
        assert html.count("<svg") == 1
        assert 'href="/n/"' in html

    def test_without_an_action(self) -> None:
        assert "btn" not in component("empty_state", title="Nothing", icon="search")


class TestTable:
    def test_number_columns_are_marked(self) -> None:
        html = component("table", columns=["Item", "Qty"], rows=[["a", "1"]], numeric=[1])
        assert html.count('class="num"') == 2  # the header cell and the body cell
        assert "table-responsive" in html

    def test_without_number_columns(self) -> None:
        assert "num" not in component("table", columns=["Item"], rows=[["a"]])


class TestMessages:
    def test_each_level_has_its_own_icon_and_tone(self) -> None:
        from tests.unit.core.test_ui_shell import request_with_messages

        request = request_with_messages((25, "Saved"), (30, "Careful"), (40, "Failed"), (20, "FYI"))
        html = render_to_string("components/messages.html", request=request)
        assert html.count("<svg") == 8  # an icon and a close button each
        for tone in ("text-ok", "text-warn", "text-danger", "text-info"):
            assert tone in html
        assert_csp_clean(html)


class TestPageHeader:
    def test_title_breadcrumbs_chip_and_actions(self) -> None:
        html = component(
            "page_header",
            title="REC-1",
            title_mono=True,
            subtitle="Context",
            breadcrumbs=[("Receiving", "/r/"), ("REC-1", "")],
            chip_label="Posted",
            chip_tone="ok",
            primary_label="Receive",
            primary_href="/new/",
            primary_icon="package-check",
            menu_id="m1",
            menu_items=[{"label": "Print", "href": "/p/"}],
        )
        assert '<h1 class="doc-no">REC-1</h1>' in html
        assert 'href="/r/"' in html
        assert 'aria-current="page"' in html
        assert "chip-ok" in html
        assert "btn-primary" in html
        assert 'popovertarget="m1"' in html
        assert "Context" in html
        assert_csp_clean(html)

    def test_title_only(self) -> None:
        html = component("page_header", title="Requests")
        assert "<h1>Requests</h1>" in html
        assert "page-head-actions" not in html
        assert "crumbs" not in html

    def test_the_menu_alone_has_no_primary_button(self) -> None:
        html = component(
            "page_header", title="X", menu_id="m", menu_items=[{"label": "A", "href": "/"}]
        )
        assert "btn-primary" not in html
        assert "menu" in html


class TestMenu:
    def test_a_popover_of_links(self) -> None:
        html = component(
            "menu",
            id="more",
            label="More actions",
            items=[
                {"label": "Print", "href": "/p/", "icon": "printer"},
                {"label": "Void", "href": "/v/", "tone": "danger"},
            ],
        )
        assert 'popovertarget="more"' in html
        assert 'id="more" popover' in html
        assert 'aria-label="More actions"' in html
        assert 'data-tone="danger"' in html
        assert html.count("menu-item") == 2

    def test_the_ghost_variant_and_a_custom_icon(self) -> None:
        html = component("menu", id="m", label="L", items=[], variant="ghost", icon="settings")
        assert "btn-ghost" in html
        assert "<svg" in html


class TestTabs:
    def test_the_current_tab_is_marked_and_a_count_can_be_shown(self) -> None:
        html = component(
            "tabs",
            items=[
                {"label": "Lines", "href": "/a/", "current": True, "count": 4},
                {"label": "Files", "href": "/b/"},
            ],
            label="Sections of the order",
        )
        assert html.count('aria-current="page"') == 1
        assert 'aria-label="Sections of the order"' in html
        assert ">4<" in html

    def test_a_zero_count_is_shown(self) -> None:
        assert ">0<" in component("tabs", items=[{"label": "X", "href": "/", "count": 0}])

    def test_a_default_label(self) -> None:
        with translation.override("en"):
            assert 'aria-label="Sections"' in component("tabs", items=[])


class TestTimeline:
    def test_entries_with_their_time_and_tone(self) -> None:
        when = datetime(2026, 11, 8, 9, 30, tzinfo=UTC)
        html = component(
            "timeline",
            entries=[
                {
                    "title": "Posted",
                    "detail": "80 of 120",
                    "when": when,
                    "tone": "ok",
                    "icon": "check",
                },
                {"title": "Issued"},
            ],
        )
        assert html.count("<li>") == 2
        assert 'data-tone="ok"' in html
        assert 'datetime="2026-11-08T09:30:00+00:00"' in html
        assert "80 of 120" in html

    def test_text_is_escaped(self) -> None:
        assert "&lt;b&gt;" in component("timeline", entries=[{"title": "<b>x</b>"}])


class TestDefinitionList:
    def test_facts_are_pairs(self) -> None:
        html = component(
            "definition_list", facts=[("Supplier", "Cables <S.A.>"), ("Project", "Nave 3")]
        )
        assert html.count("<dt>") == 2
        assert "Cables &lt;S.A.&gt;" in html
        assert " card" not in html

    def test_in_a_card(self) -> None:
        assert "@container card" in component("definition_list", facts=[], card=True)


class TestAvatar:
    def test_initials_hidden_from_assistive_technology(self) -> None:
        html = component("avatar", name="Elena Navarro")
        assert ">EN<" in html
        assert 'aria-hidden="true"' in html


class TestProgress:
    def test_partial_progress(self) -> None:
        html = component(
            "progress", value=Decimal("80"), maximum=Decimal("120"), label="Received", unit="m"
        )
        assert 'data-state="partial"' in html
        assert 'max="120"' in html
        assert 'value="80"' in html
        assert 'aria-label="Received"' in html
        assert "80 / 120 m" in html

    def test_complete_progress(self) -> None:
        assert 'data-state="done"' in component("progress", value=5, maximum=5, label="Received")

    def test_decimals_stay_valid_attributes_in_a_decimal_comma_language(self) -> None:
        with translation.override("es"):
            html = component("progress", value=Decimal("2.5"), maximum=Decimal("10.5"), label="x")
        assert 'value="2.5"' in html
        assert 'max="10.5"' in html

    def test_nothing_to_measure_has_no_meter(self) -> None:
        html = component("progress", value=0, maximum=0, label="Received")
        assert "<meter" not in html


class TestSkeleton:
    def test_busy_and_announced(self) -> None:
        with translation.override("en"):
            html = component("skeleton")
        assert 'aria-busy="true"' in html
        assert "Loading…" in html
        assert html.count('class="skeleton ') == 3


class TestStatTile:
    def test_value_change_and_sparkline(self) -> None:
        html = component(
            "stat_tile",
            label="To approve",
            value="7",
            icon="clipboard-check",
            change="2 more than last week",
            direction="up",
            good=False,
            spark=[1, 2, 3],
        )
        assert "stat-value" in html
        assert ">7<" in html
        assert 'data-good="false"' in html
        assert "2 more than last week" in html
        assert 'class="spark"' in html
        assert html.strip().startswith("<div")

    def test_a_link_when_it_has_a_destination(self) -> None:
        html = component(
            "stat_tile",
            label="Overdue",
            value="3",
            href="/overdue/",
            direction="down",
            good=True,
            change="1 fewer",
        )
        assert html.strip().startswith('<a href="/overdue/"')
        assert 'data-good="true"' in html

    def test_no_change_no_sparkline(self) -> None:
        html = component("stat_tile", label="Open", value="24")
        assert "stat-change" not in html
        assert "spark" not in html.replace("stat-", "")

    def test_a_move_that_is_neither_good_nor_bad(self) -> None:
        assert "data-good" not in component(
            "stat_tile", label="L", value="1", change="same", direction="flat"
        )


class TestListComponents:
    def test_pagination_and_cursor_pagination_use_icons(self) -> None:
        from django.template.loader import render_to_string as render

        from apps.core.listing import CURSOR_START, CursorPage, paginate

        request = RequestFactory().get("/", {"page": "2"})
        page = paginate(request, list(range(100)), default_per_page=10)
        numbered = render("components/pagination.html", {"listing": page}, request=request)
        assert numbered.count("<svg") >= 4
        cursor = CursorPage(rows=[], per_page=10, start="b", next="c", back=("a",))
        assert "<svg" in render(
            "components/cursor_pagination.html", {"listing": cursor}, request=request
        )
        first = CursorPage(rows=[], per_page=10, start=CURSOR_START, next="d", back=())
        assert "<svg" in render(
            "components/cursor_pagination.html", {"listing": first}, request=request
        )

    def test_sort_header_shows_the_direction_as_an_icon(self) -> None:
        from django.template.loader import render_to_string as render

        from apps.core.listing import paginate

        request = RequestFactory().get("/", {"sort": "-name"})
        page = paginate(
            request, [1, 2], default_per_page=10, sorts={"name": ("name",)}, default_sort="name"
        )
        html = render(
            "components/sort_header.html",
            {"listing": page, "key": "name", "label": "Name"},
            request=request,
        )
        assert "<svg" in html
        assert "↓" not in html and "↑" not in html
