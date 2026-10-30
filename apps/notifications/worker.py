"""The outbox worker: the only process that sends email (plan §5; ``entrypoint.sh worker``).

It runs as the ``SystemPrincipal`` ``system:worker`` (this module is its one allowlisted
constructor) and talks to SMTP through ``MAILERS["outbound"]``; nothing else in the code
base may (``check_access_rules.py``, rule ``mail-outside-outbox``).

Each loop iteration (``Worker.tick``):

1. **Switches.** While ``<WORKER_FLAGS_DIR>/worker.paused`` exists (deploys, recoveries) the
   worker only writes its heartbeat: no query, no claim, no send, no housekeeping. With
   ``OUTBOUND_EMAIL=off`` (restore drills, test stacks) it never claims a message and never
   opens an SMTP connection; the database-only reconciliation and housekeeping still run.
2. **Abandoned attempts**, at start-up, every ``WORKER_RECONCILE_INTERVAL`` and as soon as
   the oldest attempt in flight passes the deadline + grace, so a message claimed by a
   worker that died turns ``AMBIGUOUS`` about 90 s after its claim.
3. **Housekeeping**, at start-up and every ``WORKER_HOUSEKEEPING_INTERVAL``.
4. **One message**: claim (committed before any I/O), send, record the outcome.

**Sending.** Every socket operation has ``OUTBOX_SMTP_TIMEOUT`` (10 s), never more than what is
left of the message's total deadline (``OUTBOX_SEND_DEADLINE``, 60 s), and in the main
thread ``SIGALRM`` enforces that deadline even against a server that answers slowly at every
stage. The SMTP dialogue is driven step by step so the outcome is exact:

- ``FAILED``: the connection was refused or failed, or the server rejected the message, all
  before the ``DATA`` command (a rejected ``DATA`` command or a final rejection reply is a
  definite "not accepted" as well);
- ``AMBIGUOUS``: a timeout, disconnect or deadline after ``DATA`` was sent;
- ``SENT``: the final ``250``.

Errors are recorded as a stage and an SMTP code, never the server's text (it may echo an
address). Logs carry message ids, never addresses, subjects or bodies.
"""

import email.policy
import json
import logging
import os
import signal
import smtplib
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Any

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, mailers
from django.core.mail.backends.smtp import EmailBackend as SMTPBackend
from django.core.mail.utils import DNS_NAME
from django.db import DatabaseError, connections

from apps.access.principals import SystemPrincipal
from apps.notifications.models import Outcome
from apps.notifications.services import (
    Claim,
    Settled,
    claim_next,
    reconcile_abandoned,
    record_outcome,
    run_housekeeping,
)

logger = logging.getLogger(__name__)

PAUSE_FLAG = "worker.paused"
OUTBOUND_MAILER = "outbound"

RUNNING = "running"
PAUSED = "paused"
OUTBOUND_OFF = "outbound-off"


class DeadlineExceeded(Exception):
    """The message's total send deadline passed."""


@dataclass
class Deadline:
    """The end of one message's sending time (``time.monotonic``)."""

    at: float
    done: bool = False

    def op_timeout(self, per_operation: float) -> float:
        """The timeout of the next socket operation: what is left, at most ``per_operation``."""
        left = self.at - time.monotonic()
        if left <= 0:
            raise DeadlineExceeded
        return min(per_operation, left)


@contextmanager
def send_deadline(seconds: float) -> Iterator[Deadline]:
    """Bound one send to ``seconds`` of wall time.

    In the main thread ``SIGALRM`` interrupts whatever is blocking; in another thread (the
    concurrency tests) only the per-operation timeouts derived from the deadline apply.
    ``Deadline.done`` disarms the alarm's effect once the outcome is known.
    """
    deadline = Deadline(time.monotonic() + seconds)
    if threading.current_thread() is not threading.main_thread():
        yield deadline
        return

    def expire(signum: int, frame: FrameType | None) -> None:
        if not deadline.done:
            raise DeadlineExceeded

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield deadline
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def build_message(claim: Claim) -> EmailMultiAlternatives:
    """The frozen content as a Django message; several recipients go in BCC only."""
    headers = {"Message-ID": claim.header_message_id}
    if not claim.to:
        headers["To"] = "undisclosed-recipients:;"
    message = EmailMultiAlternatives(
        subject=claim.subject,
        body=claim.body,
        from_email=claim.from_email,
        to=list(claim.to),
        bcc=list(claim.bcc),
        headers=headers,
    )
    if claim.html_body:
        message.attach_alternative(claim.html_body, "text/html")
    return message


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    error: str = ""


def _set_timeout(connection: smtplib.SMTP, deadline: Deadline, per_operation: float) -> None:
    if connection.sock is not None:
        connection.sock.settimeout(deadline.op_timeout(per_operation))


def _close(connection: smtplib.SMTP | None, *, polite: bool) -> None:
    if connection is None:
        return
    if polite:
        with suppress(OSError, smtplib.SMTPException, DeadlineExceeded):
            connection.quit()
            return
    with suppress(OSError):
        connection.close()


@dataclass
class _Session:
    """How far one SMTP dialogue got."""

    connection: smtplib.SMTP | None = None
    stage: str = "prepare"
    data_started: bool = False


def _dialogue(
    session: _Session,
    mailer: SMTPBackend,
    message: EmailMultiAlternatives,
    deadline: Deadline,
    per_op: float,
) -> Result:
    sender = mailer.prep_address(message.from_email)
    recipients = [mailer.prep_address(address) for address in message.recipients()]
    content = message.message(policy=email.policy.SMTP).as_bytes()
    options: dict[str, Any] = {"context": mailer.ssl_context} if mailer.use_ssl else {}
    session.stage = "connect"
    session.connection = connection = mailer.connection_class(
        mailer.host,
        mailer.port,
        local_hostname=DNS_NAME.get_fqdn(),
        timeout=deadline.op_timeout(per_op),
        **options,
    )
    if mailer.use_tls and not mailer.use_ssl:
        session.stage = "STARTTLS"
        _set_timeout(connection, deadline, per_op)
        connection.starttls(context=mailer.ssl_context)
    if mailer.username and mailer.password:
        session.stage = "AUTH"
        _set_timeout(connection, deadline, per_op)
        connection.login(mailer.username, mailer.password)
    session.stage = "MAIL"
    _set_timeout(connection, deadline, per_op)
    connection.ehlo_or_helo_if_needed()
    code, _ = connection.mail(sender)
    if code != 250:
        return Result(Outcome.FAILED, f"SMTP {code} at MAIL")
    session.stage = "RCPT"
    accepted = 0
    for recipient in recipients:
        _set_timeout(connection, deadline, per_op)
        code, _ = connection.rcpt(recipient)
        if code in {250, 251}:
            accepted += 1
    if not accepted:
        return Result(Outcome.FAILED, f"SMTP {code} at RCPT")
    session.stage = "DATA"
    _set_timeout(connection, deadline, per_op)
    session.data_started = True
    code, _ = connection.data(content)
    if code != 250:
        return Result(Outcome.FAILED, f"SMTP {code} after DATA")
    refused = len(recipients) - accepted
    return Result(Outcome.SENT, f"{refused} recipient(s) refused" if refused else "")


def _smtp_send(
    mailer: SMTPBackend, message: EmailMultiAlternatives, deadline: Deadline, per_op: float
) -> Result:
    """Drive the SMTP dialogue step by step and classify how it ended."""
    session = _Session()
    try:
        try:
            result = _dialogue(session, mailer, message, deadline, per_op)
        except smtplib.SMTPResponseException as exc:
            # A definite reply: refused greeting, STARTTLS, AUTH, or the DATA command itself.
            result = Result(Outcome.FAILED, f"SMTP {exc.smtp_code} at {session.stage}")
        except (DeadlineExceeded, OSError, smtplib.SMTPException, ValueError) as exc:
            name = type(exc).__name__
            if session.data_started:
                result = Result(Outcome.AMBIGUOUS, f"{name} after DATA started")
            else:
                result = Result(Outcome.FAILED, f"{name} at {session.stage}")
        _close(session.connection, polite=result.outcome == Outcome.SENT)
        return result
    finally:
        deadline.done = True


def send(claim: Claim, *, mailer: Any = None) -> Result:
    """Send one claimed message within the deadline; never raises for a delivery problem."""
    mailer = mailer if mailer is not None else mailers[OUTBOUND_MAILER]
    message = build_message(claim)
    per_op = float(getattr(mailer, "timeout", None) or settings.OUTBOX_SMTP_TIMEOUT)
    try:
        with send_deadline(float(settings.OUTBOX_SEND_DEADLINE)) as deadline:
            if isinstance(mailer, SMTPBackend):
                return _smtp_send(mailer, message, deadline, per_op)
            # Any other backend (console, locmem in tests): no stages to tell apart, so an
            # error may have happened after delivery.
            try:
                sent = mailer.send_messages([message])
            except Exception as exc:
                return Result(Outcome.AMBIGUOUS, f"{type(exc).__name__} in {type(mailer).__name__}")
            finally:
                deadline.done = True
            return Result(Outcome.SENT) if sent else Result(Outcome.FAILED, "not accepted")
    except DeadlineExceeded:
        # Only reachable outside the SMTP path (which classifies it itself).
        return Result(Outcome.AMBIGUOUS, "DeadlineExceeded")


@dataclass
class Worker:
    principal: SystemPrincipal
    flags_dir: Path
    heartbeat_file: Path
    outbound: bool
    poll_interval: float
    reconcile_interval: float
    housekeeping_interval: float
    name: str = ""
    send: Callable[[Claim], Result] = send
    clock: Callable[[], float] = time.monotonic
    stop: threading.Event = field(default_factory=threading.Event)
    next_reconcile: float = 0.0
    next_housekeeping: float = 0.0
    was_paused: bool = False

    @classmethod
    def from_settings(cls, **overrides: Any) -> Worker:
        values: dict[str, Any] = {
            "principal": SystemPrincipal(),
            "flags_dir": Path(settings.WORKER_FLAGS_DIR),
            "heartbeat_file": Path(settings.WORKER_HEARTBEAT_FILE),
            "outbound": bool(settings.OUTBOUND_EMAIL),
            "poll_interval": float(settings.WORKER_POLL_INTERVAL),
            "reconcile_interval": float(settings.WORKER_RECONCILE_INTERVAL),
            "housekeeping_interval": float(settings.WORKER_HOUSEKEEPING_INTERVAL),
            "name": f"{socket.gethostname()}:{os.getpid()}",
        }
        return cls(**(values | overrides))

    def state(self) -> str:
        if (self.flags_dir / PAUSE_FLAG).exists():
            return PAUSED
        return RUNNING if self.outbound else OUTBOUND_OFF

    def beat(self, state: str) -> None:
        """Write the heartbeat atomically (``entrypoint.sh worker-healthcheck`` reads it)."""
        record = {"state": state, "at": time.time(), "pid": os.getpid()}
        temporary = self.heartbeat_file.with_name(f".{self.heartbeat_file.name}.tmp")
        temporary.write_text(json.dumps(record), encoding="utf-8")
        temporary.replace(self.heartbeat_file)

    def tick(self) -> bool:
        """One iteration; ``True`` when there may be more work right away."""
        state = self.state()
        self.beat(state)
        if state == PAUSED:
            if not self.was_paused:
                logger.info("Worker paused: nothing is claimed, sent or cleaned up")
                connections.close_all()
            self.was_paused = True
            return False
        if self.was_paused:
            logger.info("Worker resumed")
            self.was_paused = False
            self.next_reconcile = 0.0
        now = self.clock()
        if now >= self.next_reconcile:
            reconciled = reconcile_abandoned(self.principal)
            if reconciled.abandoned:
                logger.warning("%d abandoned attempt(s) set AMBIGUOUS", reconciled.abandoned)
            delay = self.reconcile_interval
            if reconciled.next_due_in is not None:
                delay = min(delay, reconciled.next_due_in + 0.5)
            self.next_reconcile = now + delay
        if now >= self.next_housekeeping:
            done = run_housekeeping(self.principal)
            logger.info(
                "Housekeeping done: %s", ", ".join(f"{name}={rows}" for name, rows in done.items())
            )
            self.next_housekeeping = now + self.housekeeping_interval
        if state == OUTBOUND_OFF:
            return False
        claimed = claim_next(self.principal, worker=self.name)
        if claimed is None:
            return False
        if isinstance(claimed, Settled):
            logger.info("Outbox message %s settled as %s", claimed.message_id, claimed.status)
            return True
        result = self.send(claimed)
        applied = record_outcome(
            self.principal, claimed.attempt_id, result.outcome, error=result.error
        )
        logger.info(
            "Outbox message %s attempt %d: %s%s",
            claimed.message_id,
            claimed.attempt_number,
            result.outcome,
            "" if applied else " (superseded, not applied)",
        )
        return True

    def run(self) -> None:
        """Loop until SIGTERM / SIGINT; database outages are waited out."""
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: self.stop.set())
        logger.info("Worker started (%s)", self.state())
        failures = 0
        while not self.stop.is_set():
            try:
                busy = self.tick()
                failures = 0
            except DatabaseError:
                failures += 1
                logger.warning("Database unavailable; retrying", exc_info=failures == 1)
                connections.close_all()
                busy = False
            if not busy:
                self.stop.wait(self.poll_interval * min(2**failures, 30))
        logger.info("Worker stopped")


def main() -> None:
    """``manage.py worker``: run the outbox worker until stopped."""
    Worker.from_settings().run()
