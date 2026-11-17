"""Authentication strategies.

Both strategies produce a single ``Authorization`` header value which is used
for REST calls *and* (transiently, via environment-provided git config) for
HTTPS ``git clone``. Secrets never appear in ``repr``/``str``.
"""

from __future__ import annotations

import base64
from typing import Dict

from .. import redact


class AuthStrategy:
    method: str = "none"
    username: str = ""

    def authorization_header(self) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def headers(self) -> Dict[str, str]:
        return {"Authorization": self.authorization_header()}

    def describe(self) -> str:
        return self.method

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.describe()}>"

    __str__ = __repr__


class BearerTokenAuth(AuthStrategy):
    """Bitbucket HTTP access token / personal access token sent as Bearer."""

    method = "bearer"

    def __init__(self, token: str, username: str = "") -> None:
        token = (token or "").strip()
        if not token:
            raise ValueError("A token is required for Bearer authentication")
        self._token = token
        self.username = (username or "").strip()
        redact.register_secret(token)

    def authorization_header(self) -> str:
        return f"Bearer {self._token}"

    def describe(self) -> str:
        return "Bearer token (PAT)"


class BasicAuth(AuthStrategy):
    """HTTP Basic with username + password or username + PAT."""

    method = "basic"

    def __init__(self, username: str, password: str) -> None:
        username = (username or "").strip()
        if not username:
            raise ValueError("A username is required for Basic authentication")
        if not password:
            raise ValueError("A password or token is required for Basic authentication")
        self.username = username
        self._password = password
        redact.register_basic_credentials(username, password)

    def authorization_header(self) -> str:
        raw = f"{self.username}:{self._password}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def describe(self) -> str:
        return f"Basic ({self.username})"


def make_auth(method: str, *, username: str = "", secret: str = "") -> AuthStrategy:
    method = (method or "").strip().lower()
    if method in ("bearer", "token", "pat"):
        return BearerTokenAuth(secret, username=username)
    if method in ("basic", "password"):
        return BasicAuth(username, secret)
    raise ValueError(f"Unknown auth method: {method!r} (expected 'bearer' or 'basic')")
