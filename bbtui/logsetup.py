"""Opt-in debug logging to a file, with redaction applied to every record."""

from __future__ import annotations

import logging
import os
from typing import Optional

from .redact import RedactingFilter


def setup_logging(debug_log: Optional[str]) -> Optional[str]:
    root = logging.getLogger("bbtui")
    root.handlers.clear()
    root.propagate = False
    # Third-party HTTP loggers stay quiet; our client logs redacted summaries.
    for name in ("httpx", "httpcore", "hpack"):
        logging.getLogger(name).setLevel(logging.WARNING)
    if not debug_log:
        root.addHandler(logging.NullHandler())
        root.setLevel(logging.WARNING)
        return None
    path = os.path.abspath(os.path.expanduser(debug_log))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(fd)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.addFilter(RedactingFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    root.info("debug logging enabled (secrets are redacted)")
    return path
