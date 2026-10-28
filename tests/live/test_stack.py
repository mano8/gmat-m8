"""The compose stack as a user and an attacker see it (plan §7, §8.1 layer C, M1.5 smoke).

Every test talks to Caddy over HTTPS or looks inside a running container with
``docker compose``. The order matters where a test uses up a rate-limit bucket; each such
test clears the buckets first and last.
"""

import json
import re
import socket
import ssl
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

import pytest

from tests.live.conftest import Response, Stack

pytestmark = pytest.mark.live

CSRF_FIELD = re.compile(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"')
CSRF_COOKIE = "__Host-csrftoken"


def _login_page(stack: Stack) -> tuple[str, str]:
    """The CSRF token and cookie of a fresh visit to the login page."""
    response = stack.get("/accounts/login/")
    assert response.status == 200
    match = CSRF_FIELD.search(response.text)
    assert match, "the login form carries no CSRF token"
    cookie = next(
        value.split(";", 1)[0]
        for value in response.headers_named("set-cookie")
        if value.startswith(f"{CSRF_COOKIE}=")
    )
    return match.group(1), cookie


def _failed_login(stack: Stack, token: str, cookie: str, email: str, **headers: str) -> Response:
    body = urlencode(
        {"csrfmiddlewaretoken": token, "login": email, "password": "wrong-password-123"}
    ).encode()
    return stack.request(
        "POST",
        "/accounts/login/",
        body=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": cookie,
            "Origin": stack.base_url,
            "Referer": f"{stack.base_url}/accounts/login/",
            **headers,
        },
    )


@pytest.fixture
def clean_buckets(stack: Stack) -> Iterator[None]:
    stack.reset_rate_limits()
    yield
    stack.reset_rate_limits()


# --- availability and headers ------------------------------------------------------------


def test_health_and_login_answer(stack: Stack) -> None:
    health = stack.get("/healthz/")
    assert health.status == 200
    assert json.loads(health.body) == {"status": "ok"}
    assert stack.get("/readyz/").status == 200
    assert stack.get("/accounts/login/").status == 200


def test_security_headers_survive_the_proxy(stack: Stack) -> None:
    login = stack.get("/accounts/login/")
    csp = login.header("content-security-policy") or ""
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp
    assert "max-age=" in (login.header("strict-transport-security") or "")
    assert login.header("x-frame-options") == "DENY"
    assert login.header("x-content-type-options") == "nosniff"
    assert login.header("cross-origin-opener-policy") == "same-origin"
    assert "camera=()" in (login.header("permissions-policy") or "")
    # Pre-authentication pages: nothing leaves through Referer and nothing is cached.
    assert login.header("referrer-policy") == "no-referrer"
    assert "no-store" in (login.header("cache-control") or "")
    assert login.header("x-request-id")
    # Neither Caddy nor Gunicorn announces itself.
    assert login.header("server") is None
    cookies = login.headers_named("set-cookie")
    assert cookies
    assert all(cookie.startswith("__Host-") and "Secure" in cookie for cookie in cookies)


def test_plain_http_redirects_to_the_public_https_port(stack: Stack) -> None:
    http_port = stack.env_value("HTTP_PORT", "8080")
    plain = Stack(f"http://{stack.host}:{http_port}", stack.compose_dir, stack.compose_files)
    redirect = plain.get("/accounts/login/?next=/")
    assert redirect.status in {301, 308}
    location = redirect.header("location") or ""
    assert location.startswith("https://")
    assert str(stack.port) in location or stack.port == 443


def test_static_files_are_served(stack: Stack) -> None:
    css = stack.get("/static/dist/app.css")
    assert css.status == 200
    assert "text/css" in (css.header("content-type") or "")
    assert "max-age=" in (css.header("cache-control") or "")


def test_check_deploy_passes_with_the_stack_configuration(stack: Stack) -> None:
    result = stack.compose("run", "--rm", "--no-deps", "-T", "web", "check")
    assert "no issues" in result.stdout + result.stderr


def test_admin_lives_at_the_configured_path(stack: Stack) -> None:
    path = stack.env_value("ADMIN_URL_PATH", "admin")
    response = stack.get(f"/{path}/")
    assert response.status == 302
    login = response.header("location") or ""
    assert login.startswith(f"/{path}/login/")
    # The admin login hands over to allauth: Django's own admin login form is never shown.
    handover = stack.get(login)
    assert handover.status == 302
    assert "/accounts/login/" in (handover.header("location") or "")
    if path != "admin":
        assert stack.get("/admin/").status == 404


# --- rate limits and client addresses ------------------------------------------------------


def _shape(response: Response) -> str:
    """The page minus per-response values (CSRF token, echoed input): comparable across replies."""
    return re.sub(r'value="[^"]*"', 'value=""', response.text)


def _checked(response: Response, reference: str) -> bool:
    """True when the password was really checked (the ordinary "wrong password" page).

    allauth answers a rate-limited failed login with the same 200 and a different message,
    so the limit is told apart by the page itself, not by the status.
    """
    return response.status == 200 and _shape(response) == reference


def test_failed_logins_spread_over_all_workers_hit_the_limit(
    stack: Stack, clean_buckets: None
) -> None:
    token, cookie = _login_page(stack)
    # The first attempt of a clean bucket shows what an ordinary wrong password looks like.
    reference = _shape(_failed_login(stack, token, cookie, "reference@example.org"))

    def attempt(index: int) -> Response:
        # A distinct account per attempt: only the per-address limit (10 a minute) applies.
        return _failed_login(stack, token, cookie, f"nobody{index}@example.org")

    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = list(pool.map(attempt, range(24)))

    assert all(response.status in {200, 429} for response in responses)
    checked = 1 + sum(_checked(response, reference) for response in responses)
    assert checked <= 10, f"{checked} passwords were checked from one address"
    assert checked < 25, "no attempt was limited"


def test_one_account_is_limited_across_workers(stack: Stack, clean_buckets: None) -> None:
    token, cookie = _login_page(stack)
    reference = _shape(_failed_login(stack, token, cookie, "victim@example.org"))

    def attempt(_: int) -> Response:
        return _failed_login(stack, token, cookie, "victim@example.org")

    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = list(pool.map(attempt, range(16)))

    assert all(response.status in {200, 429} for response in responses)
    checked = 1 + sum(_checked(response, reference) for response in responses)
    assert checked <= 5, f"{checked} passwords were checked for one account"  # 5 per 5 minutes


def test_a_forged_forwarded_for_header_changes_nothing(stack: Stack, clean_buckets: None) -> None:
    token, cookie = _login_page(stack)
    reference = _shape(_failed_login(stack, token, cookie, "reference@example.org"))
    # Use up this client's address bucket (10 failures a minute).
    for index in range(12):
        _failed_login(stack, token, cookie, f"burn{index}@example.org")
    assert not _checked(_failed_login(stack, token, cookie, "burned@example.org"), reference)
    # Claiming to be someone else must not reach a fresh bucket.
    for index, forged in enumerate(("203.0.113.77", "198.51.100.8, 203.0.113.78", "::1")):
        response = _failed_login(
            stack, token, cookie, f"forged{index}@example.org", **{"X-Forwarded-For": forged}
        )
        assert not _checked(response, reference), f"X-Forwarded-For: {forged} got a fresh bucket"


# --- logs ---------------------------------------------------------------------------------


def test_the_sentinel_never_reaches_a_log(stack: Stack) -> None:
    sentinel = f"SENTINEL{uuid.uuid4().hex}"
    token_path = f"/accounts/password/reset/key/Mg-{sentinel}/"
    hostile = {
        "Referer": f"{stack.base_url}{token_path}?token={sentinel}",
        "HX-Current-URL": f"{stack.base_url}{token_path}",
    }
    stack.get(f"{token_path}?token={sentinel}", headers=hostile)
    stack.get(f"/accounts/login/?next=/x/{sentinel}/", headers=hostile)
    stack.get("/no-such-page/", headers=hostile)
    stack.get(f"/healthz/?probe={sentinel}", headers=hostile)
    token, cookie = _login_page(stack)
    stack.reset_rate_limits()
    body = urlencode(
        {"csrfmiddlewaretoken": token, "login": f"{sentinel}@example.org", "password": sentinel}
    ).encode()
    stack.request(
        "POST",
        "/accounts/login/",
        body=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": cookie,
            "Origin": stack.base_url,
            **hostile,
        },
    )
    time.sleep(1)
    caddy = stack.logs("caddy")
    # The access log exists and shows the redaction at work...
    assert "/accounts/password/reset/key/REDACTED" in caddy.replace("\\/", "/")
    # ...and no stream carries the secret: not the proxy, not Gunicorn, not Django.
    for service in ("caddy", "web"):
        assert sentinel not in stack.logs(service), f"{service} logged the sentinel"
    stack.reset_rate_limits()


# --- maintenance gate ----------------------------------------------------------------------


def test_maintenance_flag_gates_the_public_site_but_not_web(stack: Stack) -> None:
    flag = stack.compose_dir / "flags" / "maintenance.on"
    flag.parent.mkdir(exist_ok=True)
    try:
        flag.write_text("", encoding="utf-8")
        gated = stack.get("/accounts/login/")
        assert gated.status == 503
        assert "Mantenimiento" in gated.text
        assert gated.header("retry-after")
        assert "no-store" in (gated.header("cache-control") or "")
        assert stack.get("/healthz/").status == 503
        # web itself keeps answering inside the stack, so a deploy can verify it before opening.
        internal = stack.compose(
            "exec",
            "-T",
            "web",
            "/app/docker/app/entrypoint.sh",
            "healthcheck",
            check=False,
        )
        assert internal.returncode == 0
    finally:
        flag.unlink(missing_ok=True)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and stack.get("/accounts/login/").status != 200:
        time.sleep(0.5)
    assert stack.get("/accounts/login/").status == 200


def test_the_fallback_page_replaces_a_missing_web(stack: Stack) -> None:
    try:
        stack.compose("stop", "web")
        gone = stack.get("/accounts/login/")
        assert gone.status == 503
        assert "Mantenimiento" in gone.text
    finally:
        stack.wait_healthy("web")
    assert stack.get("/accounts/login/").status == 200


# --- slow clients --------------------------------------------------------------------------


def test_a_stalled_request_body_is_cut_off_from_the_application(stack: Stack) -> None:
    """Caddy's ``read_body`` timeout frees the Gunicorn thread of a client that stops sending.

    Without it the thread would wait for the missing bytes until the client gives up. After the
    timeout Caddy closes its connection to Gunicorn, Django sees the truncated body and answers
    (here with a CSRF refusal, which the web log shows). Caddy keeps the client socket until
    the client closes it, which costs the application nothing.
    """
    limit = float(stack.env_value("CADDY_READ_BODY_TIMEOUT", "120s").rstrip("s"))
    if limit > 30:
        pytest.skip("set CADDY_READ_BODY_TIMEOUT=5s in .env (compose-smoke does) to run this test")
    refusal = "Referer checking failed"
    before = stack.logs("web").count(refusal)
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    request = (
        "POST /accounts/login/ HTTP/1.1\r\n"
        f"Host: {stack.host}:{stack.port}\r\n"
        "Content-Type: application/x-www-form-urlencoded\r\n"
        "Content-Length: 1000\r\n\r\nlogin=slow"
    ).encode()
    started = time.monotonic()
    handled_after = None
    with (
        socket.create_connection((stack.host, stack.port), timeout=10) as raw,
        context.wrap_socket(raw, server_hostname=stack.host) as tls,
    ):
        tls.sendall(request)  # the client now stalls with the connection still open
        while time.monotonic() - started < limit + 15:
            if stack.logs("web").count(refusal) > before:
                handled_after = time.monotonic() - started
                break
            time.sleep(0.5)
    assert handled_after is not None, "Django never saw the stalled request"
    assert handled_after < limit + 12, f"Django saw it only after {handled_after:.0f} s"


# --- isolation -------------------------------------------------------------------------------


def test_only_caddy_publishes_ports(stack: Stack) -> None:
    listing = stack.compose("ps", "--all", "--format", "json").stdout
    published: dict[str, list[int]] = {}
    for line in listing.splitlines():
        row = json.loads(line)
        ports = [p["PublishedPort"] for p in row.get("Publishers") or [] if p.get("PublishedPort")]
        if ports:
            published[row["Service"]] = ports
    assert set(published) == {"caddy"}, published


def test_the_backup_container_cannot_reach_web(stack: Stack) -> None:
    reach = stack.compose("exec", "-T", "backup", "nc", "-z", "-w", "2", "web", "8000", check=False)
    assert reach.returncode != 0
    resolve = stack.compose("exec", "-T", "backup", "getent", "hosts", "web", check=False)
    assert resolve.returncode != 0


def test_web_has_no_route_to_the_internet(stack: Stack) -> None:
    probe = stack.compose(
        "exec",
        "-T",
        "web",
        "python",
        "-c",
        "import socket; socket.create_connection(('1.1.1.1', 443), timeout=3)",
        check=False,
    )
    assert probe.returncode != 0


def _as_role(stack: Stack, role: str, statement: str) -> tuple[int, str]:
    result = stack.compose(
        "exec",
        "-T",
        "db",
        "sh",
        "-c",
        f'PGPASSWORD="$GMAT_DB_{role}_PASSWORD" psql -h 127.0.0.1 -U "$GMAT_DB_{role}_USER" '
        '-d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -tA -c "$0"',
        statement,
        check=False,
    )
    return result.returncode, result.stdout + result.stderr


def test_database_roles_are_least_privilege(stack: Stack) -> None:
    code, _ = _as_role(stack, "RUNTIME", "SELECT count(*) FROM accounts_user")
    assert code == 0
    for statement in (
        "CREATE TABLE smoke_should_fail (i int)",
        "ALTER TABLE accounts_user ADD COLUMN smoke int",
        "DROP TABLE accounts_user",
        "TRUNCATE accounts_user",
    ):
        code, output = _as_role(stack, "RUNTIME", statement)
        assert code != 0, statement
        assert "permission denied" in output or "must be owner" in output, output
    code, output = _as_role(stack, "BACKUP", "SELECT count(*) FROM accounts_user")
    assert code == 0
    code, output = _as_role(stack, "BACKUP", "DELETE FROM django_session")
    assert code != 0
    assert "read-only" in output or "permission denied" in output


# --- backup and restore ------------------------------------------------------------------------


def test_backup_now_then_restore_verify_against_the_manifest(stack: Stack) -> None:
    marker = f"lt{uuid.uuid4().hex[:30]}"
    stack.sql(
        "INSERT INTO django_session (session_key, session_data, expire_date) "  # noqa: S608
        f"VALUES ('{marker}', 'x', now() + interval '1 day')"
    )
    try:
        created = stack.compose("exec", "-T", "backup", "backup-now", "--label", "smoke")
        base = created.stdout.strip().splitlines()[-1]
        assert re.fullmatch(r"gmat-\d{8}T\d{6}Z-smoke", base), created.stdout

        manifest = json.loads(
            stack.compose("exec", "-T", "backup", "cat", f"/backups/{base}.manifest.json").stdout
        )
        assert manifest["label"] == "smoke"
        assert manifest["excluded_data_tables"] == ["django_session", "django_cache"]
        assert manifest["row_counts"]["django_session"] >= 1  # live rows existed at dump time
        assert "accounts" in manifest["migration_head"]

        # The dump carries the table but none of its rows.
        data = stack.compose(
            "exec", "-T", "backup", "pg_restore", "--data-only", "--table=django_session",
            "-f", "-", f"/backups/{base}.dump",
        ).stdout  # fmt: skip
        assert marker not in data

        verified = stack.compose("exec", "-T", "backup", "restore", "--verify", f"{base}.dump")
        assert "restore verify OK" in verified.stdout + verified.stderr
    finally:
        stack.sql(f"DELETE FROM django_session WHERE session_key = '{marker}'")  # noqa: S608
