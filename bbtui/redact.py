"""Secret redaction for logs, error messages and subprocess output.

Two layers:
* pattern-based: Authorization header values, ``Bearer``/``Basic`` credentials,
  ``user:pass@`` URL userinfo and ``GIT_CONFIG_VALUE_n`` assignments;
* value-based: any secret explicitly registered with :func:`register_secret`
  (the token/password the user typed) is replaced wherever it appears, together
  with its Basic-auth base64 forms.
"""

from __future__ import annotations

import base64
import logging
import re
import threading
from typing import Dict, Iterable, Mapping, Set

REDACTED = "***REDACTED***"

_lock = threading.Lock()
_secrets: Set[str] = set()

_SENSITIVE_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-auth-token"}

_PATTERNS = [
    # Authorization: Bearer xxx / Basic xxx (header lines, http.extraHeader values)
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*)(bearer|basic|token)\s+[^\s'\"]+"), r"\1\2 " + REDACTED),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + REDACTED),
    # scheme://user:secret@host -> scheme://user:***@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+):([^@\s/]+)@"), r"\1:" + REDACTED + "@"),
    # GIT_CONFIG_VALUE_0=...
    (re.compile(r"\b(GIT_CONFIG_VALUE_\d+=)\S+"), r"\1" + REDACTED),
]


def register_secret(value: str) -> None:
    """Remember a secret so it is scrubbed from any text passed to :func:`redact`."""
    if not value or len(value) < 4:
        return
    with _lock:
        _secrets.add(value)


def register_basic_credentials(username: str, password: str) -> None:
    register_secret(password)
    if username and password:
        register_secret(base64.b64encode(f"{username}:{password}".encode()).decode())


def clear_secrets() -> None:
    with _lock:
        _secrets.clear()


def redact(text: str, extra_secrets: Iterable[str] = ()) -> str:
    if not text:
        return text
    with _lock:
        secrets = set(_secrets)
    secrets.update(s for s in extra_secrets if s and len(s) >= 4)
    # longest first so a secret that contains another is fully removed
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in _SENSITIVE_HEADERS:
            out[k] = REDACTED
        else:
            out[k] = redact(str(v))
    return out


class RedactingFilter(logging.Filter):
    """Logging filter that redacts the fully formatted message and args."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        record.msg = redact(msg)
        record.args = ()
        return True
