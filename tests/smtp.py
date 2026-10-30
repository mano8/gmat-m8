"""A fake SMTP server for the worker tests: slow, rejecting or hanging at any stage.

Stages are ``greeting``, ``EHLO``, ``MAIL``, ``RCPT``, ``DATA`` (the reply to the DATA
command) and ``final`` (the reply after the message content). For each one the behaviour can
answer with a code, wait ``delay`` seconds first, trickle the reply one byte at a time (each
read returns in time, so only a total deadline stops it), drop the connection, or hang.
"""

import socket
import socketserver
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

STAGES = ("greeting", "EHLO", "MAIL", "RCPT", "DATA", "final")


@dataclass
class Behaviour:
    codes: dict[str, int] = field(default_factory=dict)
    delay: float = 0.0
    trickle: bool = False
    slow_at: frozenset[str] | None = None  # the stages ``delay``/``trickle`` apply to; all if None
    drop_at: str | None = None
    hang_at: str | None = None
    refuse: frozenset[str] = frozenset()  # RCPT addresses answered with 550

    def code(self, stage: str) -> int:
        default = {"greeting": 220, "DATA": 354}.get(stage, 250)
        return self.codes.get(stage, default)


@dataclass
class Received:
    sender: str
    recipients: list[str]
    content: bytes


class _Handler(socketserver.StreamRequestHandler):
    server: _Server

    def setup(self) -> None:
        super().setup()
        self.server.connections += 1

    def reply(self, stage: str, code: int, text: str = "fake") -> bool:
        """Answer one stage; ``False`` when the connection was dropped instead."""
        behaviour = self.server.behaviour
        self.server.stages.append(stage)
        if behaviour.drop_at == stage:
            self.request.shutdown(socket.SHUT_RDWR)
            return False
        if behaviour.hang_at == stage:
            self.server.hanging.set()
            self.server.stopping.wait(60)
            return False
        line = f"{code} {text}\r\n".encode()
        if behaviour.slow_at is not None and stage not in behaviour.slow_at:
            self.wfile.write(line)
        elif behaviour.trickle:
            for byte in line:
                time.sleep(behaviour.delay)
                self.wfile.write(bytes([byte]))
                self.wfile.flush()
        else:
            time.sleep(behaviour.delay)
            self.wfile.write(line)
        return True

    def handle(self) -> None:
        behaviour = self.server.behaviour
        if not self.reply("greeting", behaviour.code("greeting")):
            return
        sender, recipients = "", []
        while not self.server.stopping.is_set():
            raw = self.rfile.readline()
            if not raw:
                return
            command = raw.decode("utf-8", "replace").strip()
            verb = command[:4].upper()
            if verb in {"EHLO", "HELO"}:
                ok = self.reply("EHLO", behaviour.code("EHLO"))
            elif verb == "MAIL":
                sender = command.partition(":")[2].strip(" <>")
                ok = self.reply("MAIL", behaviour.code("MAIL"))
            elif verb == "RCPT":
                address = command.partition(":")[2].strip(" <>")
                code = 550 if address in behaviour.refuse else behaviour.code("RCPT")
                if code < 400:
                    recipients.append(address)
                ok = self.reply("RCPT", code)
            elif verb == "DATA":
                code = behaviour.code("DATA")
                ok = self.reply("DATA", code)
                if ok and code == 354:
                    content = self._read_content()
                    self.server.contents.append(content)
                    final = behaviour.code("final")
                    ok = self.reply("final", final)
                    if ok and final == 250:
                        self.server.received.append(Received(sender, recipients, content))
            elif verb == "QUIT":
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                ok = self.reply(verb, 250)
            if not ok:
                return

    def _read_content(self) -> bytes:
        lines: list[bytes] = []
        while True:
            line = self.rfile.readline()
            if not line or line == b".\r\n":
                return b"".join(lines)
            lines.append(line)


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, behaviour: Behaviour) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.behaviour = behaviour
        self.connections = 0
        self.stages: list[str] = []
        self.contents: list[bytes] = []
        self.received: list[Received] = []
        self.hanging = threading.Event()
        self.stopping = threading.Event()

    @property
    def port(self) -> int:
        return int(self.server_address[1])


@contextmanager
def fake_smtp(behaviour: Behaviour | None = None) -> Iterator[_Server]:
    server = _Server(behaviour or Behaviour())
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.stopping.set()
        server.shutdown()
        server.server_close()


def smtp_mailers(port: int, *, timeout: float = 10) -> dict[str, dict[str, object]]:
    """``MAILERS`` with the outbox as default and the fake server as the worker's mailer."""
    return {
        "default": {"BACKEND": "apps.notifications.backends.OutboxEmailBackend"},
        "outbound": {
            "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "OPTIONS": {"host": "127.0.0.1", "port": port, "timeout": timeout},
        },
    }
