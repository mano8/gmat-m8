"""RFC 6238 codes (SHA-1, 6 digits, 30 s) for the authenticators the browser tests enroll."""

import base64
import hashlib
import hmac
import struct
import time

STEP = 30
DIGITS = 6


def code_at(secret: str, at: float) -> str:
    """The code ``secret`` (base32, as the activation page shows it) yields at time ``at``."""
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", int(at // STEP)), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


class Authenticator:
    """One user's authenticator. The server accepts a code once per step
    (``MFA_TOTP_TOLERANCE = 0``), so a second code asked for in the same step waits for the next."""

    def __init__(self, secret: str) -> None:
        self.secret = secret
        self._last_step = -1

    def next_code(self) -> str:
        step = int(time.time() // STEP)
        if step <= self._last_step:
            time.sleep((self._last_step + 1) * STEP - time.time() + 0.5)
            step = int(time.time() // STEP)
        self._last_step = step
        return code_at(self.secret, step * STEP)
