import pytest
from django.core.exceptions import ImproperlyConfigured

from config.env import env_bool, env_int, env_list, env_str


class TestEnvStr:
    def test_required_missing_fails(self) -> None:
        with pytest.raises(ImproperlyConfigured, match="X is required"):
            env_str("X", environ={})

    def test_blank_counts_as_missing(self) -> None:
        with pytest.raises(ImproperlyConfigured):
            env_str("X", environ={"X": "   "})

    def test_default_and_value(self) -> None:
        assert env_str("X", "d", environ={}) == "d"
        assert env_str("X", "d", environ={"X": " v "}) == "v"

    def test_min_length_applies_to_defaults_too(self) -> None:
        with pytest.raises(ImproperlyConfigured, match="at least 5"):
            env_str("X", min_length=5, environ={"X": "abc"})
        with pytest.raises(ImproperlyConfigured, match="at least 5"):
            env_str("X", "abc", min_length=5, environ={})

    def test_reads_process_environment_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GMAT_TEST_VALUE", "from-env")
        assert env_str("GMAT_TEST_VALUE") == "from-env"


class TestEnvBool:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_true_words(self, value: str) -> None:
        assert env_bool("X", False, environ={"X": value}) is True

    @pytest.mark.parametrize("value", ["0", "false", "No", "off"])
    def test_false_words(self, value: str) -> None:
        assert env_bool("X", True, environ={"X": value}) is False

    def test_default_when_missing(self) -> None:
        assert env_bool("X", True, environ={}) is True

    def test_unknown_word_fails_closed(self) -> None:
        with pytest.raises(ImproperlyConfigured):
            env_bool("X", False, environ={"X": "maybe"})


class TestEnvInt:
    def test_parse_and_default(self) -> None:
        assert env_int("X", environ={"X": "42"}) == 42
        assert env_int("X", 7, environ={}) == 7

    def test_required_missing_fails(self) -> None:
        with pytest.raises(ImproperlyConfigured, match="is required"):
            env_int("X", environ={})

    def test_not_a_number_fails(self) -> None:
        with pytest.raises(ImproperlyConfigured, match="integer"):
            env_int("X", environ={"X": "4x"})

    @pytest.mark.parametrize("value", ["0", "70000"])
    def test_bounds(self, value: str) -> None:
        with pytest.raises(ImproperlyConfigured, match="out of range"):
            env_int("X", minimum=1, maximum=65535, environ={"X": value})


class TestEnvList:
    def test_split_and_strip(self) -> None:
        assert env_list("X", environ={"X": "a, b ,,c"}) == ["a", "b", "c"]

    def test_default_is_copied(self) -> None:
        default = ["a"]
        result = env_list("X", default, environ={})
        assert result == ["a"]
        assert result is not default

    def test_required_missing_fails(self) -> None:
        with pytest.raises(ImproperlyConfigured, match="is required"):
            env_list("X", environ={})

    def test_only_separators_fails(self) -> None:
        with pytest.raises(ImproperlyConfigured, match="must not be empty"):
            env_list("X", environ={"X": " , ,"})
