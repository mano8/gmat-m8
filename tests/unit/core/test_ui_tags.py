from decimal import Decimal

import pytest
from django.template import Context, Template
from django.utils import translation

from apps.core.templatetags.ui_tags import LEGACY_ICONS, at, icon_names, quantity


def render(source: str, **context: object) -> str:
    """A template made of ``source`` with the UI tags loaded."""
    return Template("{% load ui_tags %}" + source).render(Context(context)).strip()


class TestAt:
    def test_returns_the_item(self) -> None:
        assert at(["a", "b"], 1) == "b"

    @pytest.mark.parametrize(("items", "index"), [(["a"], 3), (None, 0)])
    def test_an_empty_string_when_there_is_none(self, items: object, index: int) -> None:
        assert at(items, index) == ""  # type: ignore[arg-type]


class TestQuantity:
    @pytest.mark.parametrize(
        ("value", "shown"),
        [(Decimal("10.000"), "10"), (Decimal("2.500"), "2.5"), (Decimal("0.125"), "0.125")],
    )
    def test_drops_trailing_zeros(self, value: Decimal, shown: str) -> None:
        with translation.override("en"):
            assert quantity(value) == shown

    def test_uses_the_decimal_comma_in_spanish(self) -> None:
        with translation.override("es"):
            assert quantity(Decimal("2.500")) == "2,5"

    def test_nothing_for_none(self) -> None:
        assert quantity(None) == ""


class TestIcon:
    def test_draws_the_vendored_shapes_and_is_hidden_from_assistive_technology(self) -> None:
        html = render('{% icon "package" %}')
        assert html.startswith("<svg ")
        assert 'aria-hidden="true"' in html
        assert "<path" in html
        assert 'class="size-5 shrink-0"' in html

    def test_a_label_makes_it_an_image(self) -> None:
        html = render('{% icon "info" label="Details" %}')
        assert 'role="img" aria-label="Details"' in html
        assert "aria-hidden" not in html

    def test_the_label_is_escaped(self) -> None:
        assert "&lt;b&gt;" in render('{% icon "info" label=text %}', text="<b>")

    def test_classes_replace_the_default_size(self) -> None:
        assert 'class="size-4 text-ok shrink-0"' in render(
            '{% icon "check" classes="size-4 text-ok" %}'
        )

    def test_the_first_icon_set_keeps_its_names(self) -> None:
        for legacy in LEGACY_ICONS:
            assert "<svg" in render("{% icon name %}", name=legacy)

    def test_an_unknown_name_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="unknown icon 'nope'"):
            render('{% icon "nope" %}')

    def test_every_vendored_icon_renders(self) -> None:
        assert len(icon_names()) > 60
        for name in icon_names():
            assert "<svg" in render("{% icon name %}", name=name)


class TestChip:
    @pytest.mark.parametrize(
        ("tone", "classes"),
        [
            ("ok", "chip chip-ok"),
            ("warn", "chip chip-warn"),
            ("danger", "chip chip-danger"),
            ("brand", "chip chip-brand"),
            ("info", "chip chip-info"),
            ("", "chip"),
        ],
    )
    def test_a_tone_is_a_class_an_icon_and_a_word(self, tone: str, classes: str) -> None:
        html = render("{% chip label tone %}", label="Approved", tone=tone)
        assert f'<span class="{classes}">' in html
        assert "<svg" in html
        assert html.endswith("Approved</span>")

    def test_an_unknown_tone_is_neutral(self) -> None:
        assert '<span class="chip">' in render("{% chip 'X' 'purple' %}")

    def test_the_icon_can_be_chosen(self) -> None:
        plain = render("{% chip 'X' 'ok' %}")
        other = render("{% chip 'X' 'ok' 'clock' %}")
        assert plain != other

    def test_the_label_is_escaped(self) -> None:
        assert "&lt;script&gt;" in render("{% chip label 'ok' %}", label="<script>")


class TestSparkline:
    def test_draws_a_line_an_area_and_the_last_point(self) -> None:
        html = render("{% sparkline values %}", values=[1, 3, 2, 5])
        assert 'class="spark"' in html
        assert "<polyline" in html
        assert "spark-area" in html
        assert "<circle" in html
        assert 'aria-hidden="true"' in html

    def test_the_highest_value_is_at_the_top(self) -> None:
        html = render("{% sparkline values %}", values=[0, 10])
        assert 'points="0.0,26.0 84.0,2.0"' in html

    def test_a_flat_series_does_not_divide_by_zero(self) -> None:
        assert "<polyline" in render("{% sparkline values %}", values=[4, 4, 4])

    def test_decimals_are_read(self) -> None:
        assert "<polyline" in render(
            "{% sparkline values %}", values=[Decimal("1.5"), Decimal("2.5")]
        )

    @pytest.mark.parametrize("values", [[], [3], None])
    def test_too_few_values_draw_nothing(self, values: object) -> None:
        assert render("{% sparkline values %}", values=values).strip() == ""


class TestInitials:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Elena Navarro", "EN"),
            ("madonna", "M"),
            ("ana.lopez@example.com", "AL"),
            ("", ""),
            (None, ""),
            ("a b c", "AB"),
        ],
    )
    def test_up_to_two_capitals(self, name: object, expected: str) -> None:
        assert render("{{ name|initials }}", name=name) == expected
