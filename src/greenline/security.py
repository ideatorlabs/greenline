"""Webhook signature checks and encryption of stored access tokens."""

from __future__ import annotations

import hashlib
import hmac

from cryptography.fernet import Fernet

from greenline.errors import NotConfigured

_ENC = "enc:"
_PLAIN = "plain:"


def sign(app_secret: str, raw_body: bytes) -> str:
    """The ``X-Hub-Signature-256`` header value Meta would send for ``raw_body``."""
    return "sha256=" + hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()


def verify_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    """Check an ``X-Hub-Signature-256`` header against the raw request body.

    Fails closed: with no app secret configured, nothing verifies.
    """
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = sign(app_secret, raw_body)
    return hmac.compare_digest(expected, header.strip())


def generate_key() -> str:
    """A new Fernet key, suitable for ``GREENLINE_TOKEN_KEY``."""
    return Fernet.generate_key().decode()


class TokenCipher:
    """Encrypts access tokens before they are stored.

    With a key, tokens are stored as ``enc:<fernet token>``. Without one they are stored as
    ``plain:<token>`` so it is obvious in the database that they are not protected; set
    ``required=True`` (``GREENLINE_REQUIRE_TOKEN_KEY``) to refuse that in production.
    """

    def __init__(self, key: str = "", *, required: bool = False) -> None:
        self._fernet = Fernet(key.encode()) if key else None
        self._required = required

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def encrypt(self, token: str) -> str:
        if not token:
            return ""
        if self._fernet is None:
            if self._required:
                raise NotConfigured("a token key is required before access tokens can be stored")
            return _PLAIN + token
        return _ENC + self._fernet.encrypt(token.encode()).decode()

    def decrypt(self, stored: str) -> str:
        if not stored:
            return ""
        if stored.startswith(_PLAIN):
            return stored[len(_PLAIN) :]
        if stored.startswith(_ENC):
            if self._fernet is None:
                raise NotConfigured("a token key is required to decrypt a stored access token")
            return self._fernet.decrypt(stored[len(_ENC) :].encode()).decode()
        return stored
