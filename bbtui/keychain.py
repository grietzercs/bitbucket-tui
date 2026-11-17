"""Opt-in token storage in the OS keychain (macOS Keychain via `keyring`).

`keyring` talks to the Security framework directly, so the secret is never
placed on a command line (unlike `security add-generic-password -w <secret>`).
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("bbtui.keychain")

SERVICE = "bitbucket-tui"


def account_key(base_url: str, auth_method: str, username: str) -> str:
    return f"{base_url}|{auth_method}|{username}"


def _keyring():
    try:
        import keyring  # type: ignore

        return keyring
    except Exception as exc:  # pragma: no cover - import failure
        log.warning("keyring unavailable: %s", exc)
        return None


def get_token(base_url: str, auth_method: str, username: str) -> Optional[str]:
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(SERVICE, account_key(base_url, auth_method, username))
    except Exception as exc:
        log.warning("Keychain read failed: %s", type(exc).__name__)
        return None


def set_token(base_url: str, auth_method: str, username: str, secret: str) -> bool:
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.set_password(SERVICE, account_key(base_url, auth_method, username), secret)
        return True
    except Exception as exc:
        log.warning("Keychain write failed: %s", type(exc).__name__)
        return False


def delete_token(base_url: str, auth_method: str, username: str) -> None:
    kr = _keyring()
    if kr is None:
        return
    try:
        kr.delete_password(SERVICE, account_key(base_url, auth_method, username))
    except Exception:
        pass
