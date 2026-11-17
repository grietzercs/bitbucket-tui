"""TLS configuration: certifi + optional custom CA bundle + optional macOS Keychain CAs."""

from __future__ import annotations

import logging
import os
import re
import ssl
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional, Union

log = logging.getLogger("bbtui.tls")

_PEM_RE = re.compile(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.S)

MACOS_KEYCHAINS = [
    "/System/Library/Keychains/SystemRootCertificates.keychain",
    "/Library/Keychains/System.keychain",
    os.path.expanduser("~/Library/Keychains/login.keychain-db"),
]


@dataclass
class TLSOptions:
    verify: bool = True
    ca_bundle: Optional[str] = None
    use_macos_keychain: bool = False


def macos_keychain_pems(keychains: Optional[List[str]] = None, timeout: float = 10.0) -> List[str]:
    """Export CA certificates from macOS keychains as PEM strings (read-only)."""
    if sys.platform != "darwin":
        return []
    pems: List[str] = []
    for kc in keychains or MACOS_KEYCHAINS:
        if not os.path.exists(kc):
            continue
        try:
            out = subprocess.run(
                ["security", "find-certificate", "-a", "-p", kc],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            ).stdout
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("Could not read keychain %s: %s", kc, exc)
            continue
        pems.extend(_PEM_RE.findall(out))
    return pems


def build_verify(opts: TLSOptions) -> Union[ssl.SSLContext, bool]:
    """Return the value to pass to ``httpx.AsyncClient(verify=...)``."""
    if not opts.verify:
        log.warning("TLS verification DISABLED by user request")
        return False
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:  # pragma: no cover
        ctx = ssl.create_default_context()
    if opts.ca_bundle:
        path = os.path.expanduser(opts.ca_bundle)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"CA bundle not found: {path}")
        ctx.load_verify_locations(cafile=path)
    if opts.use_macos_keychain:
        loaded = 0
        for pem in macos_keychain_pems():
            try:
                ctx.load_verify_locations(cadata=pem)
                loaded += 1
            except (ssl.SSLError, ValueError):
                continue
        log.info("Loaded %d CA certificates from macOS keychains", loaded)
    return ctx
