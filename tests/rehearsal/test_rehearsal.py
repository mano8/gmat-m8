"""The local rehearsal of plan M8.4's "Done when", in the order it lists; each test builds on the
one before. The result is recorded in docs/deploy-rehearsal.md and the phase PR.
"""

import hashlib
import json
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any

import pytest

from tests.e2e.mailbox import Mailbox
from tests.e2e.totp import Authenticator
from tests.rehearsal.conftest import (
    ADMIN_EMAIL,
    ADMIN_PORT,
    OFFSITE_PASSWORD,
    PASSWORD,
    PREFIX,
    PROJECT,
    ROOT,
    Browser,
    World,
    docker,
    identity_of,
    make_world,
)

pytestmark = pytest.mark.rehearsal

V1 = "1.0.0"
V2 = "1.1.0"
BAD_SMOKE = "1.2.0"
BAD_MIGRATION = "1.2.1"
FIX_A = "1.3.0"
FIX_B = "1.3.1"
UNSIGNED = "1.4.0"
BAD_BACKUP = "1.5.0"
IRREVERSIBLE = "1.6.0"
COMPATIBLE = "1.7.0"
INVALID_LINK = "This link is not valid"
WANT = "rehearsal evidence:"


def note(text: str) -> None:
    print(f"{WANT} {text}")


def counts(world: World) -> dict[str, str]:
    return {
        "audit": world.sql("SELECT count(*) FROM audit_auditevent"),
        "attempts": world.sql("SELECT count(*) FROM notifications_emailattempt"),
        "users": world.sql("SELECT count(*) FROM accounts_user"),
        "materials": world.sql("SELECT count(*) FROM catalog_material"),
        "last_audit": world.sql("SELECT id FROM audit_auditevent ORDER BY id DESC LIMIT 1"),
    }


def public(world: World, path: str = "/healthz/") -> int:
    return world.stack.get(path).status


def window(stderr: str) -> str:
    """The seconds between the gates going on and the commit point, from the script's log."""
    found = {
        name: stamp
        for stamp, name in re.findall(r"(\d\d:\d\d:\d\d)Z (step 4|commit point passed)", stderr)
    }
    if len(found) < 2:
        return "?"
    seconds = [
        int(found[name][:2]) * 3600 + int(found[name][3:5]) * 60 + int(found[name][6:8])
        for name in ("step 4", "commit point passed")
    ]
    return f"{seconds[1] - seconds[0]} s"


def bootstrap(world: World) -> None:
    out = world.manage(
        "bootstrap_admin",
        "--email",
        ADMIN_EMAIL,
        "--company-code",
        "A",
        "--company-name",
        "Instalaciones Alfa SA",
        "--superuser",
    )
    found = re.search(r"https?://[^/\s]+(/invite/[\w-]+/)", out)
    assert found, out
    browser = Browser(world.stack)
    _, page = browser.follow(found[1])
    assert page.status == 200, page.text[:500]
    form = next(chunk for chunk in page.text.split("<form") if 'name="password1"' in chunk)
    action = re.search(r'\baction="([^"]+)"', form)
    assert action, form[:200]
    response = browser.post(action[1], page, password1=PASSWORD, password2=PASSWORD)
    assert response.status in {200, 302, 303}, response.text[:500]
    assert int(world.sql("SELECT count(*) FROM accounts_user")) == 1, "the invitation was accepted"
    world.manage("seed_demo", "--operator", ADMIN_EMAIL, "--password", PASSWORD, dev=True)


# --- vN → vN+1 --------------------------------------------------------------------------------


def test_the_first_deploy_of_v_n(world: World) -> None:
    world.release(V1, "base")
    result = world.deploy(V1)
    assert world.gates() == (False, False)
    assert public(world) == 200
    assert world.deployed()[-1][1:3] == [V1, world.images["base"]]
    assert world.worker_state() == "running"
    bootstrap(world)
    assert int(world.sql("SELECT count(*) FROM catalog_material")) > 100
    note(f"first deploy {V1} from an empty database, gate window {window(result.stderr)}")


def test_v_n_to_v_n_plus_one(world: World) -> None:
    world.release(V2, "next")
    before = counts(world)
    result = world.deploy(V2)
    assert world.gates() == (False, False)
    assert public(world) == 200
    assert public(world, "/accounts/login/") == 200
    assert world.sql("SELECT to_regclass('public.rehearsal_a') IS NOT NULL") == "t"
    assert world.running_image("web").endswith(world.images["next"])
    assert world.running_image("worker").endswith(world.images["next"])
    row = world.deployed()[-1]
    assert row[1:4] == [V2, world.images["next"], world.images["backup"]]
    assert (world.compose_dir / "backups" / f"{row[4]}.dump").exists()
    assert row[4].endswith(f"pre-deploy-{V2}")
    assert world.worker_state() == "running"
    after = counts(world)
    assert after["materials"] == before["materials"]
    note(f"{V1} -> {V2}: dump {row[4]}, gate window {window(result.stderr)}")


# --- a forced smoke failure after migrate -----------------------------------------------------


def _queue_a_notification_due_in(world: World, seconds: int) -> Any:
    return world.python(
        f"""
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from apps.accounts.models import User
from apps.notifications.models import EmailOutbox
from apps.notifications.outbox import queue_notification
with transaction.atomic():
    [message] = queue_notification(
        recipients=[User.objects.get(email="tech-mad@demo.example").pk],
        capability="catalog.view",
        obj=None,
        subject="Rehearsal: must wait",
        body="Sent only after the commit point.",
    )
    EmailOutbox.objects.filter(pk=message.pk).update(
        not_before=timezone.now() + timedelta(seconds={seconds})
    )
print(json.dumps(str(message.pk)))
"""
    )


def test_a_forced_smoke_failure_never_reopens_and_never_processes(world: World) -> None:
    world.release(BAD_SMOKE, "bad-smoke")
    message = _queue_a_notification_due_in(world, 90)
    mailbox = Mailbox(world.stack)
    sent_before = len(mailbox.messages())
    result = world.deploy(BAD_SMOKE, check=False)
    assert result.returncode != 0
    assert "verify-internal failed: readyz: HTTP 503" in result.stdout + result.stderr
    assert "recover.sh migrate-back" in result.stderr
    assert world.gates() == (True, True)
    assert world.sql("SELECT to_regclass('public.rehearsal_b') IS NOT NULL") == "t", "it migrated"

    frozen = counts(world)
    time.sleep(100)  # the notification is due now; a worker that processed would have sent it
    status = "SELECT status FROM notifications_emailoutbox WHERE id = '%s'"
    assert world.sql(status % message) == "QUEUED"
    assert world.worker_state() == "paused"
    assert len(mailbox.messages()) == sent_before
    assert public(world) == 503
    assert public(world, "/accounts/login/") == 503
    posted = world.stack.request("POST", "/accounts/login/", body=b"login=x&password=y")
    assert posted.status == 503
    assert "Mantenimiento" in posted.text
    assert counts(world) == frozen, "no write since the gates closed"
    note("forced smoke failure: 503 on GET and POST, worker paused past a due message, no mail")

    done = world.recover("migrate-back")
    assert "recovered" in done.stderr
    assert world.gates() == (False, False)
    assert world.sql("SELECT to_regclass('public.rehearsal_b') IS NOT NULL") == "f", "migrated back"
    assert world.sql("SELECT to_regclass('public.rehearsal_a') IS NOT NULL") == "t"
    assert world.running_image("web").endswith(world.images["next"])
    assert public(world) == 200
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and len(mailbox.messages()) == sent_before:
        time.sleep(2)
    assert len(mailbox.messages()) == sent_before + 1, "processing resumed after the commit point"
    note("recover.sh migrate-back: previous digests, schema back, the held message then delivered")


# --- a forced migration failure ---------------------------------------------------------------


def test_a_forced_migration_failure_recovers_without_data_loss(world: World) -> None:
    world.release(BAD_MIGRATION, "bad-migration")
    before = counts(world)
    result = world.deploy(BAD_MIGRATION, check=False)
    assert result.returncode != 0
    assert world.gates() == (True, True)
    assert "step 7" in result.stderr
    world.recover("migrate-back")
    assert world.gates() == (False, False)
    assert public(world) == 200
    assert world.sql("SELECT to_regclass('public.rehearsal_b') IS NOT NULL") == "f"
    after = counts(world)
    assert after == before, "no row was lost"
    note(f"forced migration failure: step 7 stopped, migrate-back, every count identical {after}")


# --- a refused deploy of an unsigned image or manifest ----------------------------------------


def _refused(world: World, version: str, env: dict[str, str]) -> str:
    before = len(world.deployed())
    result = world.deploy(version, check=False, env=env)
    assert result.returncode != 0
    assert "does not verify: refused" in result.stderr
    assert world.gates() == (False, False)
    assert public(world) == 200
    assert len(world.deployed()) == before
    return result.stderr


def test_an_unsigned_manifest_is_refused_by_the_real_cosign(world: World) -> None:
    world.release(UNSIGNED, "fix-a", signed=False)
    stderr = _refused(world, UNSIGNED, {"REHEARSAL_REAL_COSIGN": "verify-blob"})
    note(f"unsigned manifest refused by cosign itself: {stderr.strip().splitlines()[-1]}")


def test_an_unsigned_image_is_refused_by_the_real_cosign(world: World) -> None:
    world.release(UNSIGNED, "fix-a", signed=False)
    # Only the manifest's signature is vouched for here; the image goes to the real cosign.
    manifest = world.gh.assets[f"/assets/{UNSIGNED}/release-manifest.json"]
    world.sign(identity_of(UNSIGNED), hashlib.sha256(manifest).hexdigest())
    stderr = _refused(world, UNSIGNED, {"REHEARSAL_REAL_COSIGN": "verify"})
    assert "gmat-m8@" in stderr
    note(f"unsigned image refused by cosign itself: {stderr.strip().splitlines()[-1]}")


# --- two simultaneous deploys, and a recovery waiting for a deploy's lock ---------------------


def test_two_simultaneous_deploys_are_serialized(world: World) -> None:
    world.release(FIX_A, "fix-a")
    world.release(FIX_B, "fix-b")
    first = world.start("deploy.sh", FIX_A)
    time.sleep(3)
    second = world.start("deploy.sh", FIX_B)
    _, first_err = first.communicate(timeout=900)
    _, second_err = second.communicate(timeout=900)
    assert first.returncode == 0, first_err
    assert second.returncode == 0, second_err
    assert "holds the lock: waiting" in second_err
    versions = [row[1] for row in world.deployed()[-2:]]
    assert versions == [FIX_A, FIX_B]
    assert world.running_image("web").endswith(world.images["fix-b"])
    assert world.gates() == (False, False)
    note(f"two deploys at once: the second waited for the lock, {versions[0]} then {versions[1]}")


def test_a_recovery_waits_for_a_deploys_lock(world: World) -> None:
    world.release(BAD_SMOKE, "bad-smoke")
    deploying = world.start("deploy.sh", BAD_SMOKE, "--confirm", BAD_SMOKE)
    time.sleep(3)
    recovering = world.start("recover.sh", "migrate-back")
    _, deploy_err = deploying.communicate(timeout=900)
    _, recover_err = recovering.communicate(timeout=900)
    assert deploying.returncode != 0, deploy_err
    assert recovering.returncode == 0, recover_err
    assert "holds the lock: waiting" in recover_err
    assert "recovered" in recover_err
    assert world.gates() == (False, False)
    assert public(world) == 200
    note("a recovery started during a deploy waited for its lock and read how it ended")


# --- the other rows of the decision tree ---------------------------------------------------------


def test_failed_before_the_migration_starts_the_previous_digests(world: World) -> None:
    world.release(BAD_BACKUP, "fix-a", backup_image="bad-backup")
    before = world.deployed()[-1]
    frozen = world.sql("SELECT count(*) FROM django_migrations")
    result = world.deploy(BAD_BACKUP, check=False)
    assert result.returncode != 0
    assert "recover.sh rollback" in result.stderr
    assert world.gates() == (True, True)
    assert public(world) == 503
    world.recover("rollback")
    assert world.gates() == (False, False)
    assert public(world) == 200
    assert world.running_image("web").endswith(before[2])
    assert world.running_image("backup").endswith(before[3])
    assert world.sql("SELECT count(*) FROM django_migrations") == frozen
    note("failed at step 6: recover.sh rollback started the previous digests, no migration ran")


def test_an_irreversible_release_goes_back_through_its_dump(world: World) -> None:
    notes = "Irreversible migration: the rehearsal's table\n"
    world.release(IRREVERSIBLE, "bad-smoke", notes=notes)
    unconfirmed = world.deploy(IRREVERSIBLE, check=False)
    assert unconfirmed.returncode != 0
    assert world.gates() == (False, False), "nobody typed the version: nothing changed"
    before = counts(world)
    result = world.deploy(IRREVERSIBLE, "--confirm", IRREVERSIBLE, check=False)
    assert result.returncode != 0
    assert world.gates() == (True, True)
    refused = world.recover("migrate-back", check=False)
    assert refused.returncode != 0
    assert "restore-predeploy" in refused.stderr
    world.recover("restore-predeploy", "--operator", ADMIN_EMAIL, "--confirm", "RESTORE")
    assert world.gates() == (False, False)
    assert public(world) == 200
    assert world.sql("SELECT to_regclass('public.rehearsal_b') IS NOT NULL") == "f"
    after = counts(world)
    assert (after["users"], after["materials"]) == (before["users"], before["materials"])
    note("irreversible release: migrate-back refused, restore-predeploy brought back every row")


def test_old_code_on_the_new_schema_rolls_back_the_images(world: World) -> None:
    before = world.deployed()[-1]
    world.release(COMPATIBLE, "fix-a", notes="Old code works on the new schema: no change\n")
    world.deploy(COMPATIBLE)
    world.recover("rollback-images")
    assert world.gates() == (False, False)
    assert public(world) == 200
    assert world.running_image("web").endswith(before[2])
    assert world.deployed()[-1][-1] == "rollback-images"
    note("the notes say the old code works: rollback-images started the previous digests")


# --- a restore followed by reconciliation -----------------------------------------------------


SETUP = r"""
import hashlib, pathlib, time
from datetime import timedelta
from allauth.account.forms import default_token_generator
from allauth.account.utils import user_pk_to_url_str
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.utils import timezone
from apps.accounts.models import Invitation, User
from apps.accounts.tokens import hash_token, new_token
from apps.attachments.models import Attachment
from apps.core.idempotency import issue_key
from apps.locations.models import Location
from apps.material_requests.models import MaterialRequest
from apps.notifications.models import EmailOutbox, Kind, Status
from apps.notifications.outbox import queue_notification
from apps.organizations.models import Branch
from apps.projects.models import Project
from apps.purchasing.models import PurchaseOrder
from apps.suppliers.models import Supplier
from django.db import transaction

now = timezone.now()
later = now + timedelta(days=1)
admin = User.objects.get(email="admin@demo.example")
mm = User.objects.get(email="mm-mad@demo.example")
eng = User.objects.get(email="eng-mad@demo.example")
secret = generate_totp_secret()
TOTP.activate(mm, secret)
codes = RecoveryCodes.activate(mm).generate_codes()
TOTP.activate(eng, generate_totp_secret())
raw, digest = new_token()
Invitation.objects.create(
    email="invited@demo.example", kind=Invitation.Kind.NEW_ACCOUNT, token_hash=digest,
    expires_at=later, invited_by=admin, grants={},
)
wo = User.objects.get(email="wo-mad@demo.example")
reset = f"{user_pk_to_url_str(wo)}-{default_token_generator.make_token(wo)}"
branch = Branch.objects.get(code="MAD", company__code="A")
order = PurchaseOrder.objects.create(
    branch=branch, buyer_company_id=branch.company_id, supplier=Supplier.objects.first(),
    ship_to=Location.objects.filter(branch=branch).first(), status=PurchaseOrder.Status.ISSUED,
    number="PED-REHEARSAL", issued_at=now, issued_by=admin, supplier_snapshot={"name": "x"},
)
def dispatch(revision):
    return EmailOutbox.objects.create(
        kind=Kind.PO_ORDER, recipient_address="orders@supplier.example", source_po=order,
        source_ref=f"PO:{order.pk}:rev{revision}", from_email="g <no-reply@localhost>",
        subject=f"Order rev {revision}", body="Please deliver.", not_before=later,
    )
stays_queued, sent_after = dispatch(0), dispatch(1)
with transaction.atomic():
    [notification] = queue_notification(
        recipients=[wo.pk], capability="catalog.view", obj=None, subject="Held", body="Held",
    )
    EmailOutbox.objects.filter(pk=notification.pk).update(not_before=later)
project = Project.objects.get(code="HOSP-X")
request = MaterialRequest.objects.create(
    project=project, procurement_branch=project.procurement_branch, requested_by=mm,
)
content = b"%PDF-1.4 rehearsal attachment"
key = hashlib.sha256(b"rehearsal-key").hexdigest()
pathlib.Path("/srv/media/files").joinpath(key).write_bytes(content)
Attachment.objects.create(
    request=request, storage_key=key, file_name="note.pdf", content_type="application/pdf",
    size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest(),
    uploaded_by=mm, uploaded_at=now,
)
print(json.dumps({
    "totp": secret, "codes": codes, "invite": raw, "reset": reset,
    "form_key": issue_key(admin.pk, "rehearsal"), "admin": str(admin.pk),
    "stays_queued": str(stays_queued.pk), "sent_after": str(sent_after.pk),
    "notification": str(notification.pk), "attachment_key": key,
}))
"""

AFTER_THE_BACKUP = r"""
import time
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from django.utils import timezone
from apps.access.principals import SessionPrincipal
from apps.accounts.models import User
from apps.accounts.services import reset_user_mfa
from apps.notifications.models import EmailOutbox, Status
from apps.organizations.models import Membership
from apps.organizations.services import revoke_membership

admin = User.objects.get(email="admin@demo.example")
mm = User.objects.get(email="mm-mad@demo.example")
principal = SessionPrincipal(
    user_id=admin.pk, email=admin.email, session_epoch=admin.session_epoch, mfa_at=time.time(),
)
tech = Membership.objects.get(user__email="tech-mad@demo.example", revoked_at=None)
revoke_membership(principal, tech.pk)
reset_user_mfa(principal, User.objects.get(email="eng-mad@demo.example").pk, reason="lost phone")
Authenticator.objects.filter(user=mm, type="recovery_codes").delete()
regenerated = RecoveryCodes.activate(mm).generate_codes()
EmailOutbox.objects.filter(pk=SENT_AFTER).update(status=Status.SENT, sent_at=timezone.now())
print(json.dumps({"regenerated": regenerated, "membership": str(tech.pk)}))
"""


@dataclass
class Scene:
    """What existed before the restore, to be refused after it."""

    setup: dict[str, Any]
    gap: dict[str, Any]
    dump: str
    totp: Authenticator
    session: Browser
    report: dict[str, Any]


@pytest.fixture(scope="module")
def scene(world: World) -> Scene:
    setup = world.python(SETUP)
    totp = Authenticator(setup["totp"])
    session = Browser(world.stack)
    assert session.sign_in("mm-mad@demo.example").status == 302
    assert session.second_factor(totp.next_code()).status == 302
    assert session.get("/").status == 200, "the old session works before the backup"
    assert INVALID_LINK not in Browser(world.stack).follow(f"/invite/{setup['invite']}/")[1].text
    used = Browser(world.stack)
    assert used.sign_in("mm-mad@demo.example").status == 302
    assert used.second_factor(setup["codes"][2]).status == 302, "a recovery code works before"

    out = world.compose(
        "exec", "-T", "backup", "backup-now", "--label", "rehearsal", timeout=600
    ).stdout
    dump = out.strip().splitlines()[-1]

    code = AFTER_THE_BACKUP.replace("SENT_AFTER", repr(setup["sent_after"]))
    gap = world.python(code)
    changed = Browser(world.stack)
    assert changed.sign_in("wo-mad@demo.example").status == 302
    page = changed.get("/accounts/password/change/")
    answer = changed.post(
        "/accounts/password/change/",
        page,
        oldpassword=PASSWORD,
        password1="Another-rehearsal-pass-2026!",
        password2="Another-rehearsal-pass-2026!",
    )
    assert answer.status == 302

    world.recover(
        "restore",
        dump,
        "--operator",
        ADMIN_EMAIL,
        "--incident",
        "rehearsal",
        "--confirm",
        "RESTORE",
    )
    preserved = sorted((world.compose_dir / "preserved").iterdir())[-1]
    report = json.loads((preserved / "reconcile-report.json").read_text(encoding="utf-8"))
    assert world.gates() == (False, False)
    return Scene(setup, gap, dump, totp, session, report)


class TestARestoreRejectsWhatItBroughtBack:
    def test_the_old_session(self, scene: Scene) -> None:
        response = scene.session.get("/")
        assert response.status == 302
        assert "/accounts/login/" in (response.header("location") or "")
        note("old session: redirected to sign-in")

    def test_the_old_invitation(self, world: World, scene: Scene) -> None:
        _, page = Browser(world.stack).follow(f"/invite/{scene.setup['invite']}/")
        assert INVALID_LINK in page.text
        note("old invitation link: refused")

    def test_the_old_reset_link(self, world: World, scene: Scene) -> None:
        path, page = Browser(world.stack).follow(
            f"/accounts/password/reset/key/{scene.setup['reset']}/"
        )
        assert 'name="password1"' not in page.text, path
        note("old password-reset link: refused")

    def test_the_old_form_key(self, world: World, scene: Scene) -> None:
        key, actor = scene.setup["form_key"], scene.setup["admin"]
        answer = world.python(
            f"""
from uuid import UUID
from apps.core.idempotency import IdempotencyKeyInvalid, verify_key
try:
    verify_key({key!r}, actor_id=UUID({actor!r}), operation="rehearsal")
    print(json.dumps("accepted"))
except IdempotencyKeyInvalid:
    print(json.dumps("refused"))
"""
        )
        assert answer == "refused"
        note("old signed form key: refused")

    @pytest.mark.parametrize(
        ("which", "index"),
        [
            ("consumed before the backup", 2),
            ("unused at the backup", 3),
            ("regenerated after", None),
        ],
    )
    def test_every_old_recovery_code(
        self, world: World, scene: Scene, which: str, index: int | None
    ) -> None:
        code = scene.setup["codes"][index] if index is not None else scene.gap["regenerated"][0]
        browser = Browser(world.stack)
        assert browser.sign_in("mm-mad@demo.example").status == 302
        answer = browser.second_factor(code)
        assert answer.status != 302, f"a recovery code {which} was accepted"
        note(f"old recovery code ({which}): refused")

    def test_totp_survives_and_the_codes_are_owed(self, world: World, scene: Scene) -> None:
        browser = Browser(world.stack)
        assert browser.sign_in("mm-mad@demo.example").status == 302
        assert browser.second_factor(scene.totp.next_code()).status == 302
        response = browser.get("/")
        assert response.status == 302
        assert "/accounts/2fa/recovery-codes/generate/" in (response.header("location") or "")
        note("TOTP survives (MFA keys kept); a privileged account must generate new codes first")

    def test_the_revocation_after_the_snapshot_is_applied_again(
        self, world: World, scene: Scene
    ) -> None:
        pk = scene.gap["membership"]
        revoked = "SELECT revoked_at IS NOT NULL FROM organizations_membership WHERE id = '%s'"
        assert world.sql(revoked % pk) == "t"
        assert scene.report["replayed"]["membership.revoked"] == 1
        note("membership revoked after the snapshot: revoked again")

    def test_the_mfa_reset_after_the_snapshot_is_applied_again(
        self, world: World, scene: Scene
    ) -> None:
        count = world.sql(
            "SELECT count(*) FROM mfa_authenticator a JOIN accounts_user u ON u.id = a.user_id "
            "WHERE u.email = 'eng-mad@demo.example'"
        )
        assert count == "0"
        assert scene.report["replayed"]["user.mfa_reset"] == 1
        note("MFA reset after the snapshot: factors removed again")

    def test_the_password_changed_after_the_snapshot_cannot_be_used(
        self, world: World, scene: Scene
    ) -> None:
        for password in (PASSWORD, "Another-rehearsal-pass-2026!"):
            assert Browser(world.stack).sign_in("wo-mad@demo.example", password).status == 200
        assert scene.report["replayed"]["user.password_changed"] == 1
        note("password changed after the snapshot: neither the old nor the new one signs in")

    def test_the_queued_dispatch_is_quarantined_and_a_sent_one_stays_sent(
        self, world: World, scene: Scene
    ) -> None:
        status = "SELECT status FROM notifications_emailoutbox WHERE id = '%s'"
        assert world.sql(status % scene.setup["stays_queued"]) == "QUARANTINED"
        assert world.sql(status % scene.setup["notification"]) == "QUARANTINED"
        assert world.sql(status % scene.setup["sent_after"]) == "SENT"
        assert scene.report["outbox_outcome_taken"] == {"SENT": 1}
        note("restored queued dispatch and notification: QUARANTINED; one sent since: SENT")

    def test_a_human_releases_them(self, world: World, scene: Scene) -> None:
        listing = world.manage("outbox_quarantine", "--operator", ADMIN_EMAIL, "list")
        assert scene.setup["stays_queued"] in listing
        world.manage(
            "outbox_quarantine", "--operator", ADMIN_EMAIL, "discard-dispatch",
            scene.setup["stays_queued"], "--note", "checked with the supplier",
        )  # fmt: skip
        world.manage("outbox_quarantine", "--operator", ADMIN_EMAIL, "discard-notifications")
        assert "PO_ORDER" not in world.manage(
            "outbox_quarantine", "--operator", ADMIN_EMAIL, "list"
        )
        note("the operator discards the held dispatch and notifications")

    def test_the_failed_state_is_kept(self, world: World, scene: Scene) -> None:
        preserved = sorted((world.compose_dir / "preserved").iterdir())[-1]
        assert (preserved / "failed.dump").stat().st_size > 0
        assert (preserved / "failed-state.json").stat().st_size > 0
        assert (preserved / "written-since-deploy.json").stat().st_size > 0
        volumes = docker("volume", "ls", "-q", "--filter", f"name={PROJECT}_pg_data_failed_")
        assert volumes
        assert "rehearsal" in (world.compose_dir / "incident.log").read_text(encoding="utf-8")
        note(f"failed state kept: dump, audit export, re-entry list, volume {volumes.split()[-1]}")

    def test_the_attachment_is_still_there(self, world: World, scene: Scene) -> None:
        key = scene.setup["attachment_key"]
        answer = world.python(
            f"print(json.dumps(__import__('pathlib').Path('/srv/media/files/{key}').exists()))"
        )
        assert answer is True


# --- the offsite repository -------------------------------------------------------------------


def restic_vps(world: World, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """restic in the backup image, with the VPS's own (append-only) repository."""
    return world.compose(
        "run", "--rm", "--no-deps", "-T", "--entrypoint", "restic", "backup", *args, check=check
    )


def restic_admin(world: World, *args: str) -> str:
    """restic with the maintainer's delete-capable access, from outside the stack."""
    image = f"{PREFIX}/gmat-m8-backup@{world.images['backup']}"
    return docker(
        "run", "--rm", "--network", "host", "--entrypoint", "restic",
        "-e", f"RESTIC_REPOSITORY=rest:http://127.0.0.1:{ADMIN_PORT}/gmat",
        "-e", f"RESTIC_PASSWORD={OFFSITE_PASSWORD}", "-e", "RESTIC_CACHE_DIR=/tmp/c",
        image, *args,
    )  # fmt: skip


def snapshots(world: World) -> list[dict[str, Any]]:
    listed: list[dict[str, Any]] = json.loads(restic_admin(world, "snapshots", "--json"))
    return listed


def test_the_vps_credentials_cannot_delete_a_snapshot(world: World) -> None:
    listed = snapshots(world)
    assert listed, "the deploys and the backup reached the offsite repository"
    # restic exits 0 when the server refuses: the 403 and the listing are the proof.
    forget = restic_vps(world, "forget", listed[0]["id"], check=False)
    assert "403" in forget.stdout + forget.stderr
    assert {item["id"] for item in snapshots(world)} == {item["id"] for item in listed}
    # By host: every snapshot's paths differ, so restic's default grouping would remove nothing.
    prune = restic_vps(
        world, "forget", "--keep-last", "1", "--group-by", "host", "--prune", check=False
    )
    assert "403" in prune.stdout + prune.stderr
    assert {item["id"] for item in snapshots(world)} == {item["id"] for item in listed}
    restic_admin(world, "check")
    note(f"VPS credentials: forget and prune answered 403, all {len(listed)} snapshots intact")


PRUNE = [str(ROOT / "scripts" / "offsite_prune.py")]


def run_prune(world: World, *args: str) -> subprocess.CompletedProcess[str]:
    image = f"{PREFIX}/gmat-m8-backup@{world.images['backup']}"
    wrapper = world.directory / "maintainer-bin"
    wrapper.mkdir(exist_ok=True)
    restic = wrapper / "restic"
    restic.write_text(
        "#!/bin/sh\n"
        "exec docker run --rm --network host --entrypoint restic "
        f'-e "RESTIC_REPOSITORY=rest:http://127.0.0.1:{ADMIN_PORT}/gmat" '
        f'-e "RESTIC_PASSWORD={OFFSITE_PASSWORD}" -e RESTIC_CACHE_DIR=/tmp/c {image} "$@"\n',
        encoding="utf-8",
    )
    restic.chmod(0o755)
    return subprocess.run(  # noqa: S603 (the repository's script)
        ["python3", *PRUNE, *args],  # noqa: S607
        env={**world.env, "PATH": f"{wrapper}:{world.env['PATH']}"},
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


def forge(
    world: World, when: str, *, host: str = "gmat-m8", tags: tuple[str, ...] = ("gmat-m8",)
) -> str:
    """Append a snapshot with the VPS's credentials, as an attacker on the VPS could."""
    tag_args = [item for tag in tags for item in ("--tag", tag)]
    restic_vps(
        world, "backup", "--quiet", "--time", when, "--host", host, *tag_args, "/etc/hostname"
    )
    stamp = when.replace(" ", "T")
    forged = [
        item
        for item in snapshots(world)
        if item.get("hostname") == host and item["time"].startswith(stamp)
    ]
    assert len(forged) == 1, forged
    return str(forged[0]["id"])


def forget_forged(world: World, *ids: str) -> None:
    """Once investigated, by the maintainer."""
    restic_admin(world, "forget", *ids)


FORGED_DAY = ("gmat-m8", "dump:gmat-20261020T000000Z")
POISON_TARGET = ("gmat-m8", "dump:gmat-20260301T000000Z")


class TestForgedSnapshots:
    def test_a_clean_repository_passes_the_check(self, world: World) -> None:
        result = run_prune(world, "--dry-run")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "0 to remove" in result.stdout

    @pytest.mark.parametrize(
        ("kind", "when", "fields", "says"),
        [
            ("a future timestamp", "2099-01-01 00:00:00", {}, "timestamp in the future"),
            ("an unknown host", "2026-11-01 00:00:00", {"host": "attacker"}, "unexpected host"),
            (
                "an unknown tag",
                "2026-11-01 00:00:00",
                {"tags": ("gmat-m8", "evil")},
                "unexpected tag",
            ),
        ],
    )
    def test_the_prune_refuses(
        self, world: World, kind: str, when: str, fields: dict[str, Any], says: str
    ) -> None:
        before = {item["id"] for item in snapshots(world)}
        forged = forge(world, when, **fields)
        result = run_prune(world)
        assert result.returncode == 2, result.stdout + result.stderr
        assert "REFUSED" in result.stdout and says in result.stdout
        assert before <= {item["id"] for item in snapshots(world)}, "nothing was removed"
        forget_forged(world, forged)
        note(f"forged snapshot with {kind}: the prune refused, nothing removed")

    def test_an_abnormal_count_per_day(self, world: World) -> None:
        forged = [
            forge(
                world,
                f"2026-10-20 00:{minute:02d}:00",
                tags=("gmat-m8", "dump:gmat-20261020T000000Z"),
            )
            for minute in range(13)
        ]
        result = run_prune(world)
        assert result.returncode == 2
        assert "snapshots in one day" in result.stdout
        forget_forged(world, *forged)
        note("13 forged snapshots in one day: the prune refused")

    def test_retention_poisoning_is_real_and_the_prune_refuses_it(self, world: World) -> None:
        """Forged snapshots take the monthly rule's places: an older real one would go at the next
        prune, while ``--keep-within`` still protects every snapshot of the last 90 days."""
        real_recent = {item["id"] for item in snapshots(world)}
        older_real = forge(world, "2026-03-01 00:00:00", tags=POISON_TARGET)
        forged = [
            forge(world, f"2099-{month:02d}-01 00:00:00", tags=("gmat-m8",))
            for month in range(1, 13)
        ]
        plan = json.loads(
            restic_admin(
                world,
                "forget",
                "--dry-run",
                "--json",
                "--group-by",
                "host",
                "--host",
                "gmat-m8",
                "--keep-within",
                "90d",
                "--keep-monthly",
                "12",
            )
        )
        removed = {item["id"] for group in plan for item in group.get("remove") or []}
        assert older_real in removed, "the forgeries pushed a real snapshot out of the policy"
        assert not removed & real_recent, "--keep-within keeps every snapshot of the window"
        result = run_prune(world)
        assert result.returncode == 2
        assert "timestamp in the future" in result.stdout
        assert older_real in {item["id"] for item in snapshots(world)}
        forget_forged(world, older_real, *forged)
        note(
            "12 forged future snapshots would push an older real one out of --keep-monthly; "
            f"--keep-within keeps all {len(real_recent)} of the window; the prune refused"
        )

    def test_keep_within_keeps_the_protected_window(self, world: World) -> None:
        """Monthly rules count months that have a snapshot: 13 old months leave the oldest out."""
        months = [f"{year}-{month:02d}" for year in (2025,) for month in range(1, 13)] + ["2026-01"]
        recent = forge(world, "2026-09-01 00:00:00", tags=("gmat-m8", "dump:gmat-20260901T000000Z"))
        old = {
            month: forge(
                world,
                f"{month}-01 00:00:00",
                tags=("gmat-m8", f"dump:gmat-{month.replace('-', '')}01T000000Z"),
            )
            for month in months
        }
        protected = {item["id"] for item in snapshots(world)} - set(old.values())
        assert recent in protected
        result = run_prune(world)
        assert result.returncode == 0, result.stdout + result.stderr
        left = {item["id"] for item in snapshots(world)}
        assert protected <= left, "every snapshot of the protected window survives"
        assert old["2025-01"] not in left, "beyond 90 days, the weekly and monthly rules decide"
        assert old["2026-01"] in left
        note(
            f"after the prune all {len(protected)} snapshots of the last 90 days are there; "
            f"{len(old) + len(protected) - len(left)} beyond the monthly rule are gone"
        )


# --- the offsite restore drill ----------------------------------------------------------------


def drill_world(base: dict[str, Any], name: str, subnet: str, source: World) -> World:
    project = f"{PROJECT}-{name}"
    drill = make_world(
        base["root"] / name, project, 18543 if name == "drill-a" else 18643, subnet, base
    )
    for file in ("app.env",):
        (drill.compose_dir / file).write_text(
            (source.compose_dir / file).read_text(encoding="utf-8"), encoding="utf-8"
        )
    # The same database roles as the backup's installation (offline copies of the env files).
    text = (source.compose_dir / ".env").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if not line.startswith("EDGE_SUBNET=")]
    (drill.compose_dir / ".env").write_text(
        "\n".join([*lines, f"EDGE_SUBNET={subnet}"]) + "\n", encoding="utf-8"
    )
    return drill


QUEUED_FOR_THE_DRILL = r"""
from datetime import timedelta
from django.utils import timezone
from apps.notifications.models import EmailOutbox, Kind
from apps.purchasing.models import PurchaseOrder
order = PurchaseOrder.objects.get(number="PED-REHEARSAL")
EmailOutbox.objects.create(
    kind=Kind.PO_ORDER, recipient_address="orders@supplier.example", source_po=order,
    source_ref=f"PO:{order.pk}:rev7", from_email="g <no-reply@localhost>",
    subject="Order rev 7", body="Please deliver.", not_before=timezone.now() + timedelta(days=1),
)
print(json.dumps("queued"))
"""


@pytest.fixture(scope="module")
def offsite_snapshot(world: World) -> dict[str, str]:
    """The newest snapshot of the deploys above; a supplier dispatch is queued in it."""
    world.python(QUEUED_FOR_THE_DRILL)
    world.compose("exec", "-T", "backup", "backup-now", "--label", "drill", timeout=600)
    newest = max(snapshots(world), key=lambda item: item["time"])
    return {"id": newest["id"]}


def run_drill(world: World, base: dict[str, Any], source: World, snapshot: str, *extra: str) -> str:
    env = {
        "DRILL_COMPOSE_FILES": "docker-compose.yml:docker-compose.restore-drill.yml",
        "DRILL_PROJECT_NAME": world.env["COMPOSE_PROJECT_NAME"],
        "DRILL_RESTIC_NETWORK": f"{PROJECT}_egress",
        "RESTIC_REPOSITORY": "rest:http://rest-append:8000/gmat",
        "RESTIC_PASSWORD": OFFSITE_PASSWORD,
    }
    result = world.script(
        "restore-drill.sh",
        "--snapshot",
        snapshot,
        "--operator",
        ADMIN_EMAIL,
        "--app-image",
        f"{PREFIX}/gmat-m8@{source.running_image('web').rsplit('@', 1)[1]}",
        "--backup-image",
        f"{PREFIX}/gmat-m8-backup@{base['images']['backup']}",
        *extra,
        check=False,
        env=env,
    )
    print(result.stdout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL " not in result.stdout
    assert "INFO  1 message(s) held in quarantine" in result.stdout, "the queued dispatch"
    assert "PASS  nothing was sent: no new attempt" in result.stdout
    return result.stdout


class TestTheDrill:
    def test_without_a_preserved_database_privileged_accounts_come_up_suspended(
        self, world: World, base: dict[str, Any], offsite_snapshot: dict[str, str]
    ) -> None:
        drill = drill_world(base, "drill-a", "172.30.83.0/24", world)
        out = run_drill(drill, base, world, offsite_snapshot["id"], "--wipe")
        assert "PASS  privileged accounts come up suspended" in out
        assert "PASS  web: outbound HTTP and SMTP fail" in out
        assert "PASS  worker: outbound HTTP and SMTP fail" in out
        assert "PASS  nothing was sent: no new attempt" in out
        assert "PASS  a sample of attachments opens and matches its checksum" in out
        assert "PASS  row counts match the manifest" in out
        passed = [line.removeprefix("PASS  ") for line in out.splitlines() if "PASS  " in line]
        note("offsite drill, no preserved database: " + ", ".join(passed))

    def test_with_a_preserved_database_a_later_revocation_is_applied_again(
        self, world: World, base: dict[str, Any], offsite_snapshot: dict[str, str]
    ) -> None:
        # A later dump holds a revocation the snapshot does not: it stands for the failed database.
        world.python(
            r"""
import time
from apps.access.principals import SessionPrincipal
from apps.accounts.models import User
from apps.organizations.models import Membership
from apps.organizations.services import revoke_membership
admin = User.objects.get(email="admin@demo.example")
principal = SessionPrincipal(user_id=admin.pk, email=admin.email,
    session_epoch=admin.session_epoch, mfa_at=time.time())
grant = Membership.objects.get(user__email="site-manager@demo.example", revoked_at=None)
revoke_membership(principal, grant.pk)
print(json.dumps("ok"))
"""
        )
        later = (
            world.compose("exec", "-T", "backup", "backup-now", "--label", "later", timeout=600)
            .stdout.strip()
            .splitlines()[-1]
        )
        drill = drill_world(base, "drill-b", "172.30.84.0/24", world)
        out = run_drill(
            drill, base, world, offsite_snapshot["id"],
            "--preserved-dump", str(world.compose_dir / "backups" / f"{later}.dump"),
        )  # fmt: skip
        assert "PASS  the failed state was replayed" in out
        report = json.loads((drill.compose_dir / "drill/reconcile-report.json").read_text())
        assert report["replayed"]["membership.revoked"] == 1
        drill.compose("down", "--volumes", "--remove-orphans")
        note("offsite drill, preserved dump: the later revocation replayed, nothing sent")


# --- the VPS is gone --------------------------------------------------------------------------


def test_a_new_vps_comes_up_from_the_offsite_copy_with_privileged_accounts_suspended(
    world: World, base: dict[str, Any], offsite_snapshot: dict[str, str]
) -> None:
    fresh = make_world(base["root"] / "new-vps", f"{PROJECT}-new", 18743, "172.30.85.0/24", base)
    for file in (".env", "app.env"):
        text = (world.compose_dir / file).read_text(encoding="utf-8")
        (fresh.compose_dir / file).write_text(text, encoding="utf-8")
    env = fresh.compose_dir / ".env"
    kept = [
        line
        for line in env.read_text(encoding="utf-8").splitlines()
        if not line.startswith(("EDGE_SUBNET=", "REHEARSAL_HTTP", "HTTP_PORT=", "HTTPS_PORT="))
    ]
    env.write_text(
        "\n".join(
            [
                *kept,
                "EDGE_SUBNET=172.30.85.0/24",
                "REHEARSAL_HTTP_PORT=18380",
                "REHEARSAL_HTTPS_PORT=18743",
                "HTTP_PORT=18380",
                "HTTPS_PORT=18743",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # The offsite storage is reachable from the new installation's own egress network.
    fresh.compose("up", "-d", "--no-deps", "--no-build", "mailpit")
    docker(
        "network",
        "connect",
        "--alias",
        "rest-append",
        f"{PROJECT}-new_egress",
        f"{PROJECT}-rest-append-1",
    )
    version = world.deployed()[-1][1]
    result = fresh.recover(
        "restore-offsite", "--snapshot", offsite_snapshot["id"], "--version", version,
        "--operator", ADMIN_EMAIL, "--incident", "the VPS is gone", "--confirm", "RESTORE",
        check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    assert fresh.gates() == (False, False)
    assert fresh.stack.get("/healthz/").status == 200
    preserved = sorted((fresh.compose_dir / "preserved").iterdir())[-1]
    report = json.loads((preserved / "reconcile-report.json").read_text(encoding="utf-8"))
    assert report["failed_state_used"] is False
    assert "mm-mad@demo.example" in report["suspended"]
    assert ADMIN_EMAIL not in report["suspended"]
    listed = fresh.manage("confirm_account", "--operator", ADMIN_EMAIL, "--list")
    assert "mm-mad@demo.example" in listed
    fresh.manage("confirm_account", "--operator", ADMIN_EMAIL, "--email", "mm-mad@demo.example")
    assert "mm-mad@demo.example" not in fresh.manage(
        "confirm_account", "--operator", ADMIN_EMAIL, "--list"
    )
    docker("network", "disconnect", f"{PROJECT}-new_egress", f"{PROJECT}-rest-append-1")
    note(
        "a new VPS from the offsite copy: privileged accounts suspended until confirmed one by one"
    )
