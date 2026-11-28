"""``ops/checks.py edge`` (ADR-0034, M13.3): what deploy.sh and init.sh refuse about the edge.

Each refusal has its case, so a check that stops refusing fails here: a mode outside the
allowlist, a trust wider than the connector's one address, a tunnel without its profile or the
reverse, a certificate that does not name the site, a host Django would refuse.
"""

import time
from pathlib import Path

import pytest

from tests.edge_certificates import Validity, write_edge_files
from tests.unit.docker.test_ops_checks import PATH, checks, run

# The pilot host's shape: reached on the LAN by its address and through the tunnel by its name.
LAN = "192.168.1.20"
TUNNEL = "tunnel.example.org"
PILOT = {
    "APP_DOMAIN": LAN,
    "EDGE_TLS": "files",
    "EDGE_PROXY": "cloudflare",
    "EDGE_EXTRA_HOST": TUNNEL,
    "COMPOSE_PROFILES": "tunnel",
}
PILOT_APP = {
    "DJANGO_ALLOWED_HOSTS": f"{TUNNEL},{LAN}",
    "DJANGO_CSRF_TRUSTED_ORIGINS": f"https://{TUNNEL},https://{LAN}",
}
LOCAL_APP = {
    "DJANGO_ALLOWED_HOSTS": "localhost",
    "DJANGO_CSRF_TRUSTED_ORIGINS": "https://localhost:8443",
}


def edge(directory: Path, env: dict[str, str], app_env: dict[str, str] | None = None) -> list[str]:
    lines: list[str] = checks.check_edge(env, app_env or PILOT_APP, directory, time.time())
    return lines


@pytest.fixture
def pilot(tmp_path: Path) -> Path:
    write_edge_files(tmp_path, [LAN, TUNNEL])
    (tmp_path / "tunnel" / "token").write_text("token", encoding="utf-8")
    return tmp_path


class TestTheDefaults:
    def test_nothing_set_is_the_site_as_it_was(self, tmp_path: Path) -> None:
        lines = edge(tmp_path, {}, LOCAL_APP)
        assert lines[0] == "edge: EDGE_TLS=acme, EDGE_PROXY=none, the site answers localhost"
        assert lines[-1].startswith("the links in email use https://localhost:8443")
        assert not any(line.startswith("WARNING") for line in lines)

    def test_empty_values_take_the_defaults_as_compose_gives_them(self, tmp_path: Path) -> None:
        empty = dict.fromkeys(("EDGE_TLS", "EDGE_PROXY", "EDGE_TUNNEL_ADDRESS"), "")
        assert edge(tmp_path, empty, LOCAL_APP)[0].startswith(
            "edge: EDGE_TLS=acme, EDGE_PROXY=none"
        )

    def test_the_defaults_are_those_of_compose_and_the_template(self) -> None:
        stack = checks_dir().parent
        template = checks.read_env(stack / ".env.example")
        compose = (stack / "docker-compose.yml").read_text(encoding="utf-8")
        for key, value in checks.EDGE_DEFAULTS.items():
            if key != "APP_DOMAIN":
                assert template[key] == value, key
            assert f"${{{key}:-{value}}}" in compose, key


def checks_dir() -> Path:
    return PATH.parent


@pytest.mark.parametrize("key", ["EDGE_TLS", "EDGE_PROXY"])
@pytest.mark.parametrize(
    "value", ["ACME", "None", "*", "f*", "acme files", " acme", "../edge/tls-acme", "off"]
)
def test_a_mode_is_spelled_as_its_file_is_named(tmp_path: Path, key: str, value: str) -> None:
    """A glob that names one file would start Caddy (measured): only the exact words pass."""
    with pytest.raises(checks.Refused, match=rf"^{key}: "):
        edge(tmp_path, {key: value}, LOCAL_APP)


def test_each_mode_has_its_file_and_no_file_is_without_a_mode() -> None:
    files = {path.name for path in (checks_dir().parent / "caddy" / "edge").iterdir()}
    expected = {
        f"{kind}-{value}.caddy"
        for kind, key in (("tls", "EDGE_TLS"), ("proxy", "EDGE_PROXY"))
        for value in checks.EDGE_MODES[key]
    }
    assert files == expected


class TestTheHosts:
    @pytest.mark.parametrize(
        "value",
        [
            "a.example b.example",
            "a.example,b.example",
            "*.example.org",
            ":8443",
            "tunnel.example.org:8443",
            "https://tunnel.example.org",
            "tunnel.example.org/x",
            "-bad.example",
            "2001:db8::1",
            "[::1]",
        ],
    )  # fmt: skip
    def test_the_extra_host_is_one_name(self, tmp_path: Path, value: str) -> None:
        with pytest.raises(checks.Refused, match=r"^EDGE_EXTRA_HOST: "):
            edge(tmp_path, {"EDGE_EXTRA_HOST": value}, LOCAL_APP)

    def test_app_domain_is_one_name_too(self, tmp_path: Path) -> None:
        with pytest.raises(checks.Refused, match=r"^APP_DOMAIN: "):
            edge(tmp_path, {"APP_DOMAIN": "localhost example.org"}, LOCAL_APP)

    def test_the_extra_host_is_another_one(self, tmp_path: Path) -> None:
        with pytest.raises(checks.Refused, match=r"is APP_DOMAIN already"):
            edge(tmp_path, {"EDGE_EXTRA_HOST": "LOCALHOST"}, LOCAL_APP)

    def test_django_must_answer_each_host(self, tmp_path: Path) -> None:
        with pytest.raises(
            checks.Refused, match=rf"DJANGO_ALLOWED_HOSTS in app.env lacks {TUNNEL}"
        ):
            edge(tmp_path, {"EDGE_EXTRA_HOST": TUNNEL}, LOCAL_APP)

    def test_app_domain_too(self, tmp_path: Path) -> None:
        with pytest.raises(checks.Refused, match=r"lacks gmat.example.org"):
            edge(tmp_path, {"APP_DOMAIN": "gmat.example.org"}, LOCAL_APP)

    @pytest.mark.parametrize("pattern", [TUNNEL, ".example.org", ".tunnel.example.org"])
    def test_as_django_matches_a_host(self, tmp_path: Path, pattern: str) -> None:
        app = {**LOCAL_APP, "DJANGO_ALLOWED_HOSTS": f"localhost, {pattern.upper()}"}
        assert edge(tmp_path, {"EDGE_EXTRA_HOST": TUNNEL}, app)

    def test_a_suffix_without_its_dot_is_not_a_match(self, tmp_path: Path) -> None:
        app = {**LOCAL_APP, "DJANGO_ALLOWED_HOSTS": "localhost,example.org"}
        with pytest.raises(checks.Refused, match=r"lacks"):
            edge(tmp_path, {"EDGE_EXTRA_HOST": TUNNEL}, app)


class TestTheTunnelNetwork:
    @pytest.mark.parametrize(
        "value",
        [
            "",  # the default
            "172.30.90.129",
            "172.30.90.254",
        ],
    )
    def test_an_address_outside_the_range(self, tmp_path: Path, value: str) -> None:
        assert edge(tmp_path, {"EDGE_TUNNEL_ADDRESS": value}, LOCAL_APP)

    @pytest.mark.parametrize(
        ("value", "reason"),
        [
            ("0.0.0.0/0", "not one address"),
            ("172.30.90.250/32", "not one address"),
            ("172.30.90.0/24", "not one address"),
            ("private_ranges", "not one address"),
            ("0.0.0.0/0 172.30.90.250", "not one address"),
            ("172.30.90.250,172.30.90.251", "not one address"),
            ("172.30.90.250 ", "not one address"),
            ("172.30.90.0250", "not one address"),
            ("2001:db8::1", "IPv6"),
            ("::ffff:172.30.90.250", "IPv6"),
            ("172.30.91.250", "not a container's address"),
            ("172.30.90.0", "not a container's address"),
            ("172.30.90.1", "not a container's address"),
            ("172.30.90.255", "not a container's address"),
            ("172.30.90.10", "inside EDGE_TUNNEL_RANGE"),
        ],
    )  # fmt: skip
    def test_the_one_address_caddy_believes(self, tmp_path: Path, value: str, reason: str) -> None:
        """Checked in every mode: the connector takes it as its address whenever it runs."""
        with pytest.raises(checks.Refused, match=rf"^EDGE_TUNNEL_ADDRESS: .*{reason}"):
            edge(tmp_path, {"EDGE_TUNNEL_ADDRESS": value}, LOCAL_APP)

    @pytest.mark.parametrize(
        ("env", "match"),
        [
            ({"EDGE_TUNNEL_SUBNET": "172.30.90.1/24"}, "^EDGE_TUNNEL_SUBNET: .*host bits"),
            ({"EDGE_TUNNEL_SUBNET": "fd00::/64"}, "^EDGE_TUNNEL_SUBNET: .*not an IPv4"),
            ({"EDGE_TUNNEL_SUBNET": "8.8.8.0/24"}, "^EDGE_TUNNEL_SUBNET: .*not a private"),
            ({"EDGE_TUNNEL_SUBNET": "10.0.0.0/8"}, "^EDGE_TUNNEL_SUBNET: .*not a private"),
            ({"EDGE_TUNNEL_SUBNET": "172.30.90.0/29"}, "^EDGE_TUNNEL_SUBNET: .*not a private"),
            ({"EDGE_TUNNEL_RANGE": "172.30.91.0/25"}, "^EDGE_TUNNEL_RANGE: .* inside EDGE_"),
            ({"EDGE_TUNNEL_RANGE": "172.30.90.0"}, "^EDGE_TUNNEL_RANGE: .*at least 8 addr"),
            ({"EDGE_TUNNEL_RANGE": "172.30.90.0/30"}, "^EDGE_TUNNEL_RANGE: .*at least 8 addr"),
            ({"EDGE_SUBNET": "172.30.0.0/16"}, "overlaps EDGE_SUBNET"),
        ],
    )
    def test_its_subnet_and_range(self, tmp_path: Path, env: dict[str, str], match: str) -> None:
        with pytest.raises(checks.Refused, match=match):
            edge(tmp_path, env, LOCAL_APP)

    def test_another_layout_that_holds_together(self, tmp_path: Path) -> None:
        env = {
            "EDGE_TUNNEL_SUBNET": "10.89.0.0/24",
            "EDGE_TUNNEL_RANGE": "10.89.0.128/25",
            "EDGE_TUNNEL_ADDRESS": "10.89.0.7",
        }
        assert edge(tmp_path, env, LOCAL_APP)


class TestTheTunnelMode:
    def test_the_pilot_host(self, pilot: Path) -> None:
        lines = edge(pilot, PILOT)
        assert lines[0] == (
            f"edge: EDGE_TLS=files, EDGE_PROXY=cloudflare, the site answers {LAN}, {TUNNEL}"
        )
        assert lines[1] == "edge: Caddy believes CF-Connecting-IP from 172.30.90.250 alone"
        assert lines[2].startswith(f"tls/cert.pem names {LAN}, {TUNNEL}, valid until ")
        assert lines[-1].startswith(f"the links in email use https://{TUNNEL} ")

    @pytest.mark.parametrize("profiles", ["", "uploads", "tunnels"])
    def test_needs_the_connectors_profile(self, pilot: Path, profiles: str) -> None:
        with pytest.raises(
            checks.Refused, match=r"^COMPOSE_PROFILES holds 'tunnel' if and only if"
        ):
            edge(pilot, {**PILOT, "COMPOSE_PROFILES": profiles})

    def test_the_profile_with_others(self, pilot: Path) -> None:
        assert edge(pilot, {**PILOT, "COMPOSE_PROFILES": "uploads, tunnel"})

    def test_the_profile_alone_would_make_every_visitor_one(self, tmp_path: Path) -> None:
        with pytest.raises(checks.Refused, match=r"^COMPOSE_PROFILES holds 'tunnel'"):
            edge(tmp_path, {"COMPOSE_PROFILES": "tunnel"}, LOCAL_APP)

    def test_needs_a_certificate_from_files(self, pilot: Path) -> None:
        with pytest.raises(checks.Refused, match=r"needs EDGE_TLS=files"):
            edge(pilot, {**PILOT, "EDGE_TLS": "acme"})

    def test_needs_the_token(self, pilot: Path) -> None:
        (pilot / "tunnel" / "token").unlink()
        with pytest.raises(checks.Refused, match=r"^tunnel/token is missing"):
            edge(pilot, PILOT)

    def test_a_token_that_is_a_link_is_refused(self, pilot: Path) -> None:
        token = pilot / "tunnel" / "token"
        token.unlink()
        token.symlink_to(pilot / "elsewhere")
        (pilot / "elsewhere").write_text("token", encoding="utf-8")
        with pytest.raises(checks.Refused, match=r"^tunnel/token is missing or not a plain file"):
            edge(pilot, PILOT)

    def test_needs_the_ca_pool(self, pilot: Path) -> None:
        (pilot / "tunnel" / "ca.pem").unlink()
        with pytest.raises(checks.Refused, match=r"^tunnel/ca.pem is missing"):
            edge(pilot, PILOT)

    def test_warns_when_the_pool_does_not_start_with_the_issuer(self, pilot: Path) -> None:
        issuer = (pilot / "tunnel" / "ca.pem").read_bytes()
        write_edge_files(pilot, [LAN, TUNNEL], ca_name="another CA")
        (pilot / "tunnel" / "ca.pem").write_bytes(issuer)
        assert any("is not the issuer of tls/cert.pem" in line for line in edge(pilot, PILOT))

    @pytest.mark.parametrize("enabled", ["true", "on", "1"])
    def test_an_admin_allowlist_that_holds_the_connector(self, pilot: Path, enabled: str) -> None:
        """Every visitor of the tunnel reaches Caddy from the connector's address."""
        env = {
            **PILOT,
            "DJANGO_ADMIN_ENABLED": enabled,
            "EDGE_TUNNEL_SUBNET": "10.89.0.0/24",
            "EDGE_TUNNEL_RANGE": "10.89.0.0/25",
            "EDGE_TUNNEL_ADDRESS": "10.89.0.200",
            "ADMIN_ALLOWED_CIDRS": "203.0.113.0/24 10.0.0.0/8",
        }
        with pytest.raises(checks.Refused, match=r"10.0.0.0/8 holds the tunnel's connector"):
            edge(pilot, env)
        assert edge(pilot, {**env, "DJANGO_ADMIN_ENABLED": "false"})
        assert edge(pilot, {**env, "ADMIN_ALLOWED_CIDRS": "203.0.113.0/24"})


class TestTheCertificateFromFiles:
    FILES = {"EDGE_TLS": "files", "APP_DOMAIN": LAN}
    APP = {"DJANGO_ALLOWED_HOSTS": LAN, "DJANGO_CSRF_TRUSTED_ORIGINS": f"https://{LAN}"}

    def test_names_the_site(self, tmp_path: Path) -> None:
        write_edge_files(tmp_path, [LAN])
        assert edge(tmp_path, self.FILES, self.APP)[1].startswith(f"tls/cert.pem names {LAN}")

    def test_a_certificate_that_does_not_name_the_address(self, tmp_path: Path) -> None:
        write_edge_files(tmp_path, ["192.168.1.21", TUNNEL])
        with pytest.raises(checks.Refused, match=rf"does not name {LAN}"):
            edge(tmp_path, self.FILES, self.APP)

    def test_nor_the_extra_host(self, pilot: Path) -> None:
        write_edge_files(pilot, [LAN, "other.example.org"])
        with pytest.raises(checks.Refused, match=rf"does not name {TUNNEL}"):
            edge(pilot, PILOT)

    def test_a_wildcard_names_one_level(self, pilot: Path) -> None:
        write_edge_files(pilot, [LAN, "*.example.org"])
        assert edge(pilot, PILOT)
        write_edge_files(pilot, [LAN, "*.org"])
        with pytest.raises(checks.Refused, match=rf"does not name {TUNNEL}"):
            edge(pilot, PILOT)

    def test_a_wildcard_never_names_an_address(self, tmp_path: Path) -> None:
        write_edge_files(tmp_path, ["*.1.20"])
        with pytest.raises(checks.Refused, match=rf"does not name {LAN}"):
            edge(tmp_path, self.FILES, self.APP)

    def test_an_expired_one(self, tmp_path: Path) -> None:
        write_edge_files(tmp_path, [LAN], Validity(starts=-30, ends=-1))
        with pytest.raises(checks.Refused, match=r"^tls/cert.pem expired on "):
            edge(tmp_path, self.FILES, self.APP)

    def test_one_not_valid_yet(self, tmp_path: Path) -> None:
        write_edge_files(tmp_path, [LAN], Validity(starts=2, ends=90))
        with pytest.raises(checks.Refused, match=r"^tls/cert.pem is valid only from "):
            edge(tmp_path, self.FILES, self.APP)

    @pytest.mark.parametrize(("days", "warned"), [(29, True), (31, False)])
    def test_warns_thirty_days_before_it_expires(
        self, tmp_path: Path, days: int, warned: bool
    ) -> None:
        write_edge_files(tmp_path, [LAN], Validity(ends=days))
        warning = edge(tmp_path, self.FILES, self.APP)[1]
        assert warning.startswith("WARNING: tls/cert.pem expires on ") == warned

    def test_needs_its_key(self, tmp_path: Path) -> None:
        write_edge_files(tmp_path, [LAN])
        (tmp_path / "tls" / "key.pem").unlink()
        with pytest.raises(checks.Refused, match=r"^tls/key.pem is missing"):
            edge(tmp_path, self.FILES, self.APP)

    def test_a_key_this_account_cannot_read_is_fine(self, tmp_path: Path) -> None:
        """It is the containers' uid's: only its presence is checked here."""
        write_edge_files(tmp_path, [LAN])
        (tmp_path / "tls" / "key.pem").chmod(0)
        assert edge(tmp_path, self.FILES, self.APP)

    @pytest.mark.parametrize("text", ["", "not a certificate", "-----BEGIN CERTIFICATE-----\n"])
    def test_a_file_that_is_no_certificate(self, tmp_path: Path, text: str) -> None:
        write_edge_files(tmp_path, [LAN])
        (tmp_path / "tls" / "cert.pem").write_text(text, encoding="utf-8")
        with pytest.raises(checks.Refused, match=r"^tls/cert.pem is not a PEM certificate"):
            edge(tmp_path, self.FILES, self.APP)

    def test_a_missing_one(self, tmp_path: Path) -> None:
        with pytest.raises(checks.Refused, match=r"^tls/cert.pem is missing"):
            edge(tmp_path, self.FILES, self.APP)

    def test_acme_reads_no_file(self, tmp_path: Path) -> None:
        assert edge(tmp_path, {"APP_DOMAIN": LAN}, self.APP)


class TestTheLinksInEmail:
    def test_names_the_first_trusted_origin(self, pilot: Path) -> None:
        app = {
            **PILOT_APP,
            "DJANGO_CSRF_TRUSTED_ORIGINS": f" https://{LAN}:9443 , https://{TUNNEL}",
        }
        lines = edge(pilot, PILOT, app)
        assert lines[-1].startswith(f"the links in email use https://{LAN}:9443 ")

    def test_warns_about_a_host_caddy_does_not_answer(self, tmp_path: Path) -> None:
        app = {**LOCAL_APP, "DJANGO_CSRF_TRUSTED_ORIGINS": "https://gmat.example.org"}
        lines = edge(tmp_path, {}, app)
        assert (
            lines[-1]
            == "WARNING: Caddy answers localhost, not the host of https://gmat.example.org"
        )

    def test_none_at_all_is_refused(self, tmp_path: Path) -> None:
        app = {**LOCAL_APP, "DJANGO_CSRF_TRUSTED_ORIGINS": " , "}
        with pytest.raises(
            checks.Refused, match=r"DJANGO_CSRF_TRUSTED_ORIGINS in app.env is empty"
        ):
            edge(tmp_path, {}, app)


class TestTheCommand:
    def _files(self, directory: Path, env: dict[str, str], app: dict[str, str]) -> None:
        for name, values in ((".env", env), ("app.env", app)):
            text = "".join(f"{key}={value}\n" for key, value in values.items())
            (directory / name).write_text(f"# a comment=1\n{text}", encoding="utf-8")

    def test_prints_what_it_found(self, pilot: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self._files(pilot, PILOT, PILOT_APP)
        code = run("edge", "--env", str(pilot / ".env"), "--app-env", str(pilot / "app.env"))
        assert code == 0
        assert capsys.readouterr().out.splitlines()[1] == (
            "edge: Caddy believes CF-Connecting-IP from 172.30.90.250 alone"
        )

    def test_refuses_with_its_reason(self, pilot: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self._files(pilot, {**PILOT, "EDGE_TUNNEL_ADDRESS": "0.0.0.0/0"}, PILOT_APP)
        code = run("edge", "--env", str(pilot / ".env"), "--app-env", str(pilot / "app.env"))
        assert code == 2
        assert capsys.readouterr().err.startswith("EDGE_TUNNEL_ADDRESS: '0.0.0.0/0'")

    def test_the_last_line_of_a_key_counts_and_quotes_are_not_removed(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text('EDGE_TLS=files\nEDGE_TLS="acme"\n#EDGE_PROXY=x\n', encoding="utf-8")
        env = checks.read_env(path)
        assert env == {"EDGE_TLS": '"acme"'}
        with pytest.raises(checks.Refused, match=r"^EDGE_TLS: '\"acme\"'"):
            edge(tmp_path, env, LOCAL_APP)
