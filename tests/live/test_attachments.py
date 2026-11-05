"""Attachments through Caddy and Gunicorn (plan §5, F54, F73; M6.4 done-when, live layer).

- three simultaneous slow uploads: two are admitted while their bodies trickle in, and the
  third is refused (429) by the application before its body is read (Caddy relays the answer
  after discarding at most 256 KB of the unread body, as Go's HTTP/1 server does);
- a stalled body is cut by Caddy's ``read_body`` timeout and its reservation released long
  before the lease would expire;
- a cross-site upload is refused before admission, and a CSRF failure releases its slot;
- an upload is stored on the media volume, downloaded as a sandboxed attachment, listed in
  the backup manifest and copied with the dump.

Each test signs in a fresh technician of a fresh branch with draft requests, created in the
running stack with ``manage.py shell`` (the test suite is not part of the image).
"""

import io
import json
import re
import socket
import ssl
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from PIL import Image

from tests.live.conftest import Response, Stack

pytestmark = pytest.mark.live

CSRF_FIELD = re.compile(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"')
KEY_FIELD = re.compile(r'name="idempotency_key"\s+value="([^"]+)"')
CSRF_COOKIE = "__Host-csrftoken"
SESSION_COOKIE = "__Host-sessionid"

SETUP = """
import json, time, uuid
from datetime import timedelta
from allauth.account.internal.flows.login import AUTHENTICATION_METHODS_SESSION_KEY
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from django.utils import timezone
from apps.accounts.models import User
from apps.accounts.sessions import SESSION_EPOCH_KEY, SESSION_LOGIN_AT_KEY, session_lifetime
from apps.material_requests.models import MaterialRequest
from apps.organizations.models import Branch, Company, Membership
from apps.projects.models import Project
tag = uuid.uuid4().hex[:8].upper()
company = Company.objects.create(code="L" + tag, legal_name="Live " + tag)
branch = Branch.objects.create(company=company, code="B" + tag, name="Live " + tag)
project = Project.objects.create(
    code="P" + tag, name="Live " + tag, branch=branch, procurement_branch=branch, status="ACTIVE"
)
user = User.objects.create_user(f"live-{tag.lower()}@example.org", uuid.uuid4().hex)
Membership.objects.create(
    user=user, role="TECHNICIAN", branch=branch, valid_from=timezone.now() - timedelta(hours=1)
)
drafts = [
    MaterialRequest.objects.create(project=project, procurement_branch=branch, requested_by=user)
    for _ in range(3)
]
user.refresh_from_db()
session = SessionStore()
session[SESSION_KEY] = str(user.pk)
session[BACKEND_SESSION_KEY] = "allauth.account.auth_backends.AuthenticationBackend"
session[HASH_SESSION_KEY] = user.get_session_auth_hash()
session[SESSION_EPOCH_KEY] = user.session_epoch
session[SESSION_LOGIN_AT_KEY] = int(time.time())
session[AUTHENTICATION_METHODS_SESSION_KEY] = [{"method": "password", "at": time.time()}]
session.set_expiry(session_lifetime(user))
session.create()
print(json.dumps({"session": session.session_key, "drafts": [str(d.pk) for d in drafts]}))
"""


@dataclass
class Technician:
    stack: Stack
    session: str
    drafts: list[str]

    def page(self, draft: str) -> tuple[str, str, str]:
        """The draft's page: (CSRF cookie, form token, upload key)."""
        response = self.stack.get(
            f"/requests/{draft}/", headers={"Cookie": f"{SESSION_COOKIE}={self.session}"}
        )
        assert response.status == 200, response.status
        cookie = next(
            value.split(";", 1)[0].split("=", 1)[1]
            for value in response.headers_named("set-cookie")
            if value.startswith(f"{CSRF_COOKIE}=")
        )
        form = response.text.split('data-testid="attachment-upload"', 1)
        assert len(form) == 2, "the draft offers no upload form"
        # The page has other forms with their own keys: take the upload form's.
        token, key = CSRF_FIELD.search(response.text), KEY_FIELD.search(form[1])
        assert token and key
        return cookie, token.group(1), key.group(1)

    def cookies(self, csrf: str) -> str:
        return f"{SESSION_COOKIE}={self.session}; {CSRF_COOKIE}={csrf}"


@pytest.fixture
def technician(stack: Stack) -> Iterator[Technician]:
    _wait_until_idle(stack)
    result = stack.compose(
        "exec", "-T", "web", "python", "manage.py", "shell", "-c", SETUP
    )  # fmt: skip
    data = json.loads(result.stdout.strip().splitlines()[-1])
    yield Technician(stack, data["session"], data["drafts"])
    stack.sql(f"DELETE FROM django_session WHERE session_key = '{data['session']}'")  # noqa: S608


def _wait_until_idle(stack: Stack, timeout: float = 60) -> None:
    """No upload of an earlier test still in progress (the bound is installation-wide)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _count(stack, "state = 'PROCESSING'") == 0:
            return
        time.sleep(1)
    pytest.fail("an upload stayed in progress")


def _count(stack: Stack, condition: str) -> int:
    return int(
        stack.sql(f"SELECT count(*) FROM attachments_attachmentreservation WHERE {condition}")  # noqa: S608
    )


def _states(stack: Stack, draft: str) -> list[str]:
    rows = stack.sql(
        "SELECT state FROM attachments_attachmentreservation "  # noqa: S608
        f"WHERE request_id = '{draft}' ORDER BY created_at"
    )
    return rows.split() if rows else []


def png(size: int = 32) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), "red").save(buffer, "PNG")
    return buffer.getvalue()


def multipart(fields: dict[str, str], content: bytes, name: str = "photo.png") -> tuple[bytes, str]:
    boundary = f"gmat{uuid.uuid4().hex}"
    head = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        for key, value in fields.items()
    )
    part = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    body = head + part + content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def upload(
    who: Technician, draft: str, content: bytes, *, origin: str | None = None, token: str = ""
) -> Response:
    csrf, form_token, key = who.page(draft)
    body, content_type = multipart(
        {"csrfmiddlewaretoken": token or form_token, "idempotency_key": key}, content
    )
    headers = {
        "Cookie": who.cookies(csrf),
        "Content-Type": content_type,
        "Origin": origin or who.stack.base_url,
        "Referer": f"{who.stack.base_url}/requests/{draft}/",
    }
    return who.stack.request("POST", f"/attachments/request/{draft}/", headers=headers, body=body)


class SlowUpload:
    """An upload over a raw TLS socket that sends its headers and then only what it is told."""

    def __init__(self, who: Technician, draft: str, declared: int = 2_000_000) -> None:
        csrf, token, key = who.page(draft)
        fields = {"csrfmiddlewaretoken": token, "idempotency_key": key}
        self.body, content_type = multipart(fields, b"\0" * declared)
        self.sent = self.body.index(b"\0") + 100  # the fields, the file's header, 100 bytes
        stack = who.stack
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((stack.host, stack.port), timeout=30)
        self.tls = context.wrap_socket(raw, server_hostname=stack.host)
        head = (
            f"POST /attachments/request/{draft}/ HTTP/1.1\r\n"
            f"Host: {stack.host}:{stack.port}\r\n"
            f"Cookie: {who.cookies(csrf)}\r\n"
            f"Origin: {stack.base_url}\r\n"
            f"Referer: {stack.base_url}/requests/{draft}/\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(self.body)}\r\n\r\n"
        )
        self.tls.sendall(head.encode() + self.body[: self.sent])

    def send(self, size: int) -> None:
        self.tls.sendall(self.body[self.sent : self.sent + size])
        self.sent += size

    def status(self, timeout: float) -> int | None:
        """The status line, if the server answered within ``timeout`` seconds."""
        self.tls.settimeout(timeout)
        try:
            line = self.tls.recv(64)
        except TimeoutError, ssl.SSLError, OSError:
            return None
        match = re.match(rb"HTTP/1\.1 (\d{3})", line)
        return int(match.group(1)) if match else None

    def close(self) -> None:
        self.tls.close()


def _wait_for(check: object, timeout: float) -> float:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if check():  # type: ignore[operator]
            return time.monotonic() - started
        time.sleep(0.25)
    raise AssertionError("condition not reached")


def test_an_upload_is_stored_downloaded_and_backed_up(stack: Stack, technician: Technician) -> None:
    draft = technician.drafts[0]
    response = upload(technician, draft, png())
    assert response.status == 302, response.text[:300]
    assert response.header("location") == f"/requests/{draft}/"
    row = stack.sql(
        "SELECT id || ' ' || storage_key || ' ' || sha256 FROM attachments_attachment "  # noqa: S608
        f"WHERE request_id = '{draft}'"
    )
    attachment_id, storage_key, sha256 = row.split()
    listing = stack.compose("exec", "-T", "web", "ls", "-l", f"/srv/media/files/{storage_key}")
    assert listing.stdout.startswith("-rw")
    download = stack.get(
        f"/attachments/{attachment_id}/",
        headers={"Cookie": f"{SESSION_COOKIE}={technician.session}"},
    )
    assert download.status == 200
    assert download.body[:4] == b"RIFF"  # re-encoded to WebP
    assert download.header("content-disposition") == 'attachment; filename="photo.webp"'
    assert download.header("x-content-type-options") == "nosniff"
    assert download.header("content-security-policy") == "sandbox; default-src 'none'"
    assert "no-store" in (download.header("cache-control") or "")
    # Nothing serves the volume directly.
    assert stack.get(f"/media/files/{storage_key}").status == 404
    backup = stack.compose("exec", "-T", "backup", "backup-now", "--label", "live-media")
    base = backup.stdout.strip().splitlines()[-1]
    manifest = json.loads(
        stack.compose("exec", "-T", "backup", "cat", f"/backups/{base}.manifest.json").stdout
    )
    listed = {"storage_key": storage_key, "size_bytes": len(download.body), "sha256": sha256}
    assert listed in manifest["attachments"]
    assert manifest["media"]["problems"] == []
    copied = stack.compose(
        "exec", "-T", "backup", "sha256sum", f"/backups/{base}.media/{storage_key}"
    )
    assert copied.stdout.split()[0] == sha256
    stack.compose("exec", "-T", "backup", "restore", "--verify", f"{base}.dump", timeout=600)


def test_the_third_of_three_slow_uploads_is_refused_before_its_body(
    stack: Stack, technician: Technician
) -> None:
    first, second = (SlowUpload(technician, draft) for draft in technician.drafts[:2])
    try:
        _wait_for(lambda: _count(stack, "state = 'PROCESSING'") == 2, timeout=4)
        third_draft = technician.drafts[2]
        refusal = f"Too Many Requests: /attachments/request/{third_draft}/"
        third = SlowUpload(technician, third_draft)
        try:
            # Django refuses it while the client has sent its headers and a few hundred bytes.
            _wait_for(lambda: refusal in stack.logs("web"), timeout=3)
            assert _count(stack, f"request_id = '{third_draft}'") == 0
            # Caddy (Go's HTTP/1 server) relays the answer once it has discarded up to 256 KB
            # of the unread body; the application never reads it.
            third.send(300_000)
            assert third.status(timeout=5) == 429
        finally:
            third.close()
    finally:
        first.close()
        second.close()
    # The two abandoned uploads give their slots back as soon as their bodies break.
    _wait_for(lambda: _count(stack, "state = 'PROCESSING'") == 0, timeout=20)


def test_a_stalled_body_is_cut_and_released_long_before_its_lease(
    stack: Stack, technician: Technician
) -> None:
    limit = float(stack.env_value("CADDY_READ_BODY_TIMEOUT", "120s").rstrip("s"))
    if limit > 30:
        pytest.skip("set CADDY_READ_BODY_TIMEOUT=5s in .env (compose-smoke does) to run this test")
    draft = technician.drafts[0]
    stalled = SlowUpload(technician, draft)  # sends its headers and 200 bytes, then nothing
    try:
        _wait_for(lambda: _states(stack, draft) == ["PROCESSING"], timeout=4)
        released_after = _wait_for(
            lambda: _states(stack, draft) == ["RELEASED"], timeout=limit + 15
        )
    finally:
        stalled.close()
    assert released_after < limit + 12  # the lease is 15 minutes
    assert stack.sql("SELECT reserved_bytes FROM attachments_storagequota") == "0"


def test_a_cross_site_upload_is_refused_before_admission(
    stack: Stack, technician: Technician
) -> None:
    draft = technician.drafts[0]
    assert upload(technician, draft, png(), origin="https://evil.example").status == 403
    assert _states(stack, draft) == []


def test_a_csrf_failure_releases_its_reservation(stack: Stack, technician: Technician) -> None:
    draft = technician.drafts[0]
    assert upload(technician, draft, png(), token="x" * 64).status == 403
    assert _states(stack, draft) == ["RELEASED"]


def test_a_disguised_file_is_refused(stack: Stack, technician: Technician) -> None:
    draft = technician.drafts[0]
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
    assert upload(technician, draft, svg).status == 415
    assert _states(stack, draft) == ["RELEASED"]


# --- the scanner and the decoder (M8.3, X7 and X11; ADR-0015) ------------------------------------

EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
# The industry's harmless test file, as a stream inside a PDF: the type check lets it through and
# only clamd, which unpacks PDF objects, can tell.
INFECTED_PDF = (
    b"%PDF-1.4\n1 0 obj\n<< /Length "
    + str(len(EICAR)).encode()
    + b" >>\nstream\n"
    + EICAR
    + b"\nendstream\nendobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"
)


def attachments_of(stack: Stack, draft: str) -> str:
    return stack.sql(f"SELECT count(*) FROM attachments_attachment WHERE request_id = '{draft}'")  # noqa: S608


def exec_in(stack: Stack, service: str, *command: str) -> Any:
    return stack.compose("exec", "-T", service, *command, check=False, timeout=60)


def test_a_file_with_malware_is_refused_by_the_real_scanner(
    stack: Stack, technician: Technician
) -> None:
    draft = technician.drafts[1]
    stored = attachments_of(stack, draft)
    response = upload(technician, draft, INFECTED_PDF, name="nota.pdf")
    assert response.status == 422, response.text[:300]
    assert attachments_of(stack, draft) == stored
    audited = stack.sql(
        "SELECT data->>'signature' FROM audit_auditevent "
        f"WHERE action = 'attachment.malware_refused' AND object_id = '{draft}'"
    )
    assert "Eicar" in audited or "EICAR" in audited


def test_a_clean_pdf_goes_through_the_real_scanner(stack: Stack, technician: Technician) -> None:
    draft = technician.drafts[1]
    pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
    assert upload(technician, draft, pdf, name="n.pdf").status == 302


def test_with_the_scanner_down_nothing_is_stored(stack: Stack, technician: Technician) -> None:
    """Fail closed; and the rest of the site does not depend on it."""
    draft = technician.drafts[2]
    stored = attachments_of(stack, draft)
    stack.compose("stop", "clamd")
    try:
        response = upload(technician, draft, png())
        assert response.status == 503
        assert response.header("retry-after")
        assert stack.get("/healthz/").status == 200
        assert attachments_of(stack, draft) == stored
    finally:
        stack.compose(
            "up", "-d", "--no-deps", "--no-build", "--wait", "--wait-timeout", "300", "clamd"
        )


def test_with_the_decoder_down_an_image_is_refused(stack: Stack, technician: Technician) -> None:
    draft = technician.drafts[2]
    stack.compose("stop", "decoder")
    try:
        assert upload(technician, draft, png()).status == 503
    finally:
        stack.compose("up", "-d", "--no-deps", "--no-build", "--wait", "decoder")
    assert upload(technician, draft, png()).status == 302


PROBE_OUT = (
    "import socket, sys\n"
    "try:\n"
    "    socket.create_connection(('1.1.1.1', 443), timeout=3)\n"
    "except OSError:\n"
    "    sys.exit(0)\n"
    "sys.exit(1)\n"
)


def test_the_decoder_has_its_own_uid_no_network_and_no_secrets(stack: Stack) -> None:
    assert exec_in(stack, "decoder", "id", "-u").stdout.strip() == "1001"
    assert exec_in(stack, "web", "id", "-u").stdout.strip() == "1000"
    environment = exec_in(stack, "decoder", "env").stdout
    for name in ("SECRET_KEY", "DB_PASSWORD", "ENCRYPTION_KEYS", "SMTP", "EMAIL"):
        assert name not in environment
    assert exec_in(stack, "decoder", "ls", "/srv/media/files").returncode != 0
    assert exec_in(stack, "decoder", "python", "-I", "-c", PROBE_OUT).returncode == 0
    interfaces = exec_in(
        stack,
        "decoder",
        "python",
        "-I",
        "-c",
        "import socket; print([n for _, n in socket.if_nameindex()])",
    )
    assert interfaces.stdout.strip() == "['lo']"


def test_the_scanner_has_no_network_and_cannot_see_the_files(stack: Stack) -> None:
    assert exec_in(stack, "clamd", "id", "-u").stdout.strip() == "100"
    assert exec_in(stack, "clamd", "ls", "/srv/media/files").returncode != 0
    assert exec_in(stack, "clamd", "ls", "/sys/class/net").stdout.split() == ["lo"]


def test_web_cannot_replace_the_sockets_it_uses(stack: Stack) -> None:
    for directory in ("/run/gmat/clamd", "/run/gmat/decoder"):
        assert exec_in(stack, "web", "touch", f"{directory}/x").returncode != 0
    assert (
        exec_in(
            stack,
            "web",
            "python",
            "-c",
            "from pathlib import Path; "
            "print(all(Path(p).is_socket() for p in "
            "('/run/gmat/clamd/clamd.sock', '/run/gmat/decoder/decoder.sock')))",
        ).stdout.strip()
        == "True"
    )
