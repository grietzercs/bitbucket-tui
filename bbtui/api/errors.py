"""Error types for the Bitbucket client and mapping from HTTP/transport failures.

Every failure the UI can encounter is turned into a :class:`BitbucketError`
subclass with a human-readable ``message`` and an optional ``hint`` so that
screens can always show *something* actionable instead of spinning.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx


@dataclass
class Veto:
    summary: str
    detail: str = ""

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "Veto":
        return cls(
            summary=str(data.get("summaryMessage") or data.get("message") or ""),
            detail=str(data.get("detailedMessage") or ""),
        )


class BitbucketError(Exception):
    """Base error. ``message`` is user-facing; ``hint`` suggests a fix."""

    default_hint: str = ""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        hint: Optional[str] = None,
        errors: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.hint = hint if hint is not None else self.default_hint
        self.errors = errors or []

    @property
    def exception_names(self) -> List[str]:
        return [str(e.get("exceptionName") or "") for e in self.errors]

    def describe(self) -> str:
        prefix = f"HTTP {self.status}: " if self.status else ""
        text = f"{prefix}{self.message}"
        if self.hint:
            text += f"\nHint: {self.hint}"
        return text

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.describe()


class ConfigError(BitbucketError):
    """Invalid user input (e.g. malformed URL) detected before any request."""


class AuthenticationError(BitbucketError):
    default_hint = (
        "The server rejected the credentials. Check the token/password, that the "
        "token has not expired, and that the auth method matches (Bearer vs Basic)."
    )


class PermissionDeniedError(BitbucketError):
    default_hint = "Authenticated, but this account/token lacks permission for this resource."


class NotFoundError(BitbucketError):
    default_hint = "The resource does not exist or is not visible to this account."


class ConflictError(BitbucketError):
    """HTTP 409. Used for merge vetoes, merge conflicts and stale versions."""

    @property
    def is_out_of_date(self) -> bool:
        return any("OutOfDate" in n for n in self.exception_names)

    @property
    def current_version(self) -> Optional[int]:
        for e in self.errors:
            if "currentVersion" in e:
                try:
                    return int(e["currentVersion"])
                except (TypeError, ValueError):
                    return None
        return None

    @property
    def vetoes(self) -> List[Veto]:
        out: List[Veto] = []
        for e in self.errors:
            for v in e.get("vetoes") or []:
                out.append(Veto.from_json(v))
        return out

    @property
    def conflicted(self) -> bool:
        return any(bool(e.get("conflicted")) for e in self.errors)


class RateLimitedError(BitbucketError):
    default_hint = "The server is rate limiting requests. Wait a moment and retry."


class ServerError(BitbucketError):
    default_hint = "The server returned an internal error. Retry, or check server health."


class UnexpectedResponseError(BitbucketError):
    default_hint = (
        "The response was not Bitbucket REST JSON. Is the URL the Bitbucket base URL "
        "(not a login/SSO page or a proxy)?"
    )


class NetworkError(BitbucketError):
    default_hint = "Check the URL, VPN connection and DNS. No proxy is used unless HTTPS_PROXY is set."


class TimeoutError_(NetworkError):  # noqa: N801 - avoid shadowing builtin
    default_hint = (
        "The server did not respond in time. Check VPN/network, or increase the "
        "timeout with --timeout."
    )


class TLSError(NetworkError):
    default_hint = (
        "TLS certificate verification failed. If your organisation uses an internal CA, "
        "enable 'Trust macOS Keychain CAs' or provide a CA bundle (PEM) path. Disabling "
        "verification is insecure and should only be used for diagnosis."
    )


class CancelledByUser(BitbucketError):
    pass


def _parse_error_body(response: httpx.Response) -> List[Dict[str, Any]]:
    try:
        data = response.json()
    except Exception:
        return []
    if isinstance(data, dict):
        errs = data.get("errors")
        if isinstance(errs, list):
            return [e for e in errs if isinstance(e, dict)]
        if "message" in data:
            return [data]
    return []


def error_from_response(response: httpx.Response) -> BitbucketError:
    """Map a non-2xx response to a typed error."""
    status = response.status_code
    errors = _parse_error_body(response)
    server_msg = "; ".join(str(e.get("message")) for e in errors if e.get("message"))

    if 300 <= status < 400:
        location = response.headers.get("location", "?")
        return UnexpectedResponseError(
            f"Server redirected to {location}",
            status=status,
            hint="A redirect usually means an SSO/login page or wrong base URL. "
            "Use the Bitbucket base URL and a token rather than browser login.",
        )
    if status == 401:
        names = [str(e.get("exceptionName") or "") for e in errors]
        if any(n.endswith("AuthorisationException") for n in names):
            # Bitbucket uses 401 + AuthorisationException for "not permitted" too.
            return PermissionDeniedError(
                server_msg or "Not permitted",
                status=status,
                errors=errors,
                hint="This account/token is not permitted to access this resource. If this "
                "happens everywhere, the credentials are not being accepted.",
            )
        return AuthenticationError(server_msg or "Authentication failed", status=status, errors=errors)
    if status == 403:
        return PermissionDeniedError(server_msg or "Forbidden", status=status, errors=errors)
    if status == 404:
        return NotFoundError(server_msg or "Not found", status=status, errors=errors)
    if status == 409:
        err = ConflictError(server_msg or "Conflict", status=status, errors=errors)
        if err.is_out_of_date:
            err.hint = "The pull request changed since it was loaded. Refresh and try again."
        elif err.vetoes:
            err.hint = "Merge was vetoed: " + "; ".join(v.summary for v in err.vetoes)
        return err
    if status == 429:
        return RateLimitedError(server_msg or "Too many requests", status=status, errors=errors)
    if status >= 500:
        return ServerError(server_msg or f"Server error {status}", status=status, errors=errors)
    return BitbucketError(server_msg or f"Unexpected HTTP {status}", status=status, errors=errors)


def _is_tls_failure(exc: BaseException) -> bool:
    seen = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLError):
            return True
        text = str(cur)
        if "CERTIFICATE_VERIFY_FAILED" in text or "SSL" in text and "certificate" in text.lower():
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def error_from_transport(exc: BaseException, url: str = "") -> BitbucketError:
    """Map an httpx transport exception to a typed error."""
    where = f" ({url})" if url else ""
    if isinstance(exc, httpx.TimeoutException):
        kind = type(exc).__name__
        return TimeoutError_(f"Request timed out{where} [{kind}]")
    if _is_tls_failure(exc):
        return TLSError(f"TLS/SSL error{where}: {exc}")
    if isinstance(exc, httpx.ConnectError):
        return NetworkError(f"Could not connect{where}: {exc}")
    if isinstance(exc, httpx.TransportError):
        return NetworkError(f"Network error{where}: {exc}")
    return BitbucketError(f"Unexpected error{where}: {exc}")
