import pytest
from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured

from config.env import (
    derived_fernet_key,
    env_bool,
    env_fernet_keys,
    env_int,
    env_list,
    env_secret,
    env_str,
    fernet_key_problems,
    is_placeholder,
)


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


class TestSecrets:
    @pytest.mark.parametrize(
        "value",
        [
            "dev-key",
            "TEST-key",
            "change-me-now",
            "please-replace-me",
            "your-secret",
            "Placeholder-value",
        ],
    )
    def test_placeholders(self, value: str) -> None:
        assert is_placeholder(value)

    def test_random_values_are_not_placeholders(self) -> None:
        assert not is_placeholder(Fernet.generate_key().decode())

    def test_env_secret(self) -> None:
        good = "Zq8vN2xLp4Rt7WmB9cYd3GhJ6KsFa1Ue"
        assert env_secret("X", min_length=10, environ={"X": good}) == good
        with pytest.raises(ImproperlyConfigured, match="placeholder"):
            env_secret("X", min_length=10, environ={"X": "change-me-please-123"})
        with pytest.raises(ImproperlyConfigured, match="at least"):
            env_secret("X", min_length=50, environ={"X": good})

    def test_derived_keys_are_valid_stable_and_distinct(self) -> None:
        assert derived_fernet_key("a") == derived_fernet_key("a")
        assert derived_fernet_key("a") != derived_fernet_key("b")
        Fernet(derived_fernet_key("a").encode())

    def test_fernet_key_problems(self) -> None:
        good = Fernet.generate_key().decode()
        assert fernet_key_problems([good]) == []
        assert fernet_key_problems([]) == ["no key is configured"]
        assert "a key is listed twice" in fernet_key_problems([good, good])
        assert fernet_key_problems(["short"]) == ["key 1 is not a valid Fernet key"]
        assert fernet_key_problems(["clé-non-ascii"]) == ["key 1 is not a valid Fernet key"]
        refused = derived_fernet_key("development")
        assert fernet_key_problems([good, refused], refused=frozenset({refused})) == [
            "key 2 is a development or placeholder key"
        ]

    def test_env_fernet_keys(self) -> None:
        first, second = Fernet.generate_key().decode(), Fernet.generate_key().decode()
        assert env_fernet_keys("K", environ={"K": f"{first}, {second}"}) == [first, second]
        with pytest.raises(ImproperlyConfigured, match="not a valid Fernet key"):
            env_fernet_keys("K", environ={"K": "nope"})
