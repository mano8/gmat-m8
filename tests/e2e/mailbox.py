"""The stack's local mail sink (Mailpit) and the worker's pause flag, as seen from the tests.

Mailpit is not published: only the worker shares a network with it, so its HTTP API is asked
from inside the ``worker`` container (the way ``docker-compose.local-mail.yml`` documents).
"""

import json
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from tests.live.conftest import Stack

API = "http://mailpit:8025/api/v1"
FETCH = (
    "import sys, urllib.request;"
    "print(urllib.request.urlopen(sys.argv[1], timeout=10).read().decode())"
)
LINK = re.compile(r"https?://[^\s<>\"')]+")


class Mailbox:
    def __init__(self, stack: Stack) -> None:
        self.stack = stack

    def _get(self, path: str) -> Any:
        result = self.stack.compose("exec", "-T", "worker", "python", "-c", FETCH, f"{API}{path}")
        return json.loads(result.stdout)

    def messages(self) -> list[dict[str, Any]]:
        return list(self._get("/messages?limit=500")["messages"])

    def wait_for(
        self,
        recipient: str,
        *,
        ignore: set[str] | None = None,
        containing: str = "",
        timeout: float = 90.0,
    ) -> dict[str, Any]:
        """A message to ``recipient`` that is not in ``ignore`` (ids), once the worker has
        delivered it; its id joins ``ignore``, so the next call waits for the next message.

        ``containing`` waits for the one with this text. An account also gets notices of its
        password and second-factor changes (M8.3), so "the next message to this address" may
        not be the link a test is after; a notice skipped here stays available to a later call.
        """
        known = ignore if ignore is not None else set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in self.messages():
                if message["ID"] in known:
                    continue
                if not any(to["Address"] == recipient for to in message.get("To") or []):
                    continue
                full = dict(self._get(f"/message/{message['ID']}"))
                if containing in full["Text"]:
                    known.add(message["ID"])
                    return full
            time.sleep(2)
        raise AssertionError(f"no message to {recipient} reached the mail sink in {timeout:.0f}s")

    @staticmethod
    def links(message: dict[str, Any]) -> list[str]:
        return LINK.findall(message["Text"])

    def link(self, message: dict[str, Any], contains: str) -> str:
        found = [link for link in self.links(message) if contains in link]
        assert found, f"no link with {contains!r} in the message: {message['Text']!r}"
        return found[0]


@contextmanager
def worker_paused(stack: Stack) -> Iterator[None]:
    """The worker claims and sends nothing while the flag exists (deploys and recoveries)."""
    flag = stack.compose_dir / "flags" / "worker.paused"
    flag.touch()
    try:
        yield
    finally:
        flag.unlink(missing_ok=True)
