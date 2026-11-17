"""Command-line entry point: ``bbtui`` / ``python -m bbtui``."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List, Mapping, Optional, Tuple

from . import __version__, config
from .logsetup import setup_logging


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bbtui",
        description="Terminal UI for Bitbucket Server / Data Center. Tokens are never accepted as "
        "command-line arguments; use the login form, BITBUCKET_PAT/BITBUCKET_PASSWORD env vars, "
        "an --env-file, or the opt-in macOS Keychain.",
    )
    p.add_argument("--url", help="Bitbucket base URL, e.g. https://bitbucket.example.com (env: BITBUCKET_URL)")
    p.add_argument("--auth", choices=["bearer", "basic"], help="auth method (env: BITBUCKET_AUTH)")
    p.add_argument("--user", help="username (env: BITBUCKET_USER)")
    p.add_argument("--env-file", metavar="PATH", help="read BITBUCKET_* values from a .env file")
    p.add_argument("--ca-bundle", metavar="PEM", help="extra CA bundle to trust (env: BITBUCKET_CA_BUNDLE)")
    p.add_argument("--no-keychain-cas", action="store_true", help="do not load CA certs from the macOS Keychain")
    p.add_argument("--insecure", action="store_true", help="DISABLE TLS verification (dangerous; diagnosis only)")
    p.add_argument("--timeout", type=float, default=None, help="per-request network timeout in seconds (default 30)")
    p.add_argument("--connect", action="store_true", help="connect immediately if URL and token are available")
    p.add_argument("--debug-log", metavar="PATH", default=os.environ.get("BBTUI_DEBUG_LOG"),
                   help="write a redacted debug log (env: BBTUI_DEBUG_LOG)")
    p.add_argument("--forget", action="store_true", help="delete remembered settings and exit")
    p.add_argument("--demo", action="store_true", help="run against a built-in fake server (no network)")
    p.add_argument("--version", action="version", version=f"bbtui {__version__}")
    return p


def resolve_settings(args: argparse.Namespace, environ: Mapping[str, str]) -> Tuple[config.Settings, config.EnvCredentials, Dict[str, str]]:
    """Precedence: CLI flags > environment / --env-file > remembered config > defaults."""
    env: Dict[str, str] = dict(environ)
    if args.env_file:
        env.update(config.load_dotenv_file(args.env_file))
    settings = config.load_saved()
    creds = config.credentials_from_env(env)
    if creds.auth_method:
        settings.auth_method = creds.auth_method
    if creds.username:
        settings.username = creds.username
    if creds.base_url:
        settings.base_url = creds.base_url
    if creds.ca_bundle:
        settings.ca_bundle = creds.ca_bundle
    if args.url:
        settings.base_url = creds.base_url = args.url
    if args.auth:
        settings.auth_method = creds.auth_method = args.auth
    if args.user:
        settings.username = creds.username = args.user
    if args.ca_bundle:
        settings.ca_bundle = creds.ca_bundle = args.ca_bundle
    if args.no_keychain_cas:
        settings.use_macos_keychain_cas = False
    if args.insecure:
        settings.insecure = True
    if args.timeout:
        settings.request_timeout = max(1.0, args.timeout)
    return settings, creds, env


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.forget:
        config.forget()
        print(f"Removed {config.config_path()} (Keychain items, if any, remain under service 'bitbucket-tui').")
        return 0

    log_path = setup_logging(args.debug_log)
    try:
        settings, creds, env = resolve_settings(args, os.environ)
    except OSError as exc:
        print(f"Cannot read env file {args.env_file}: {exc}", file=sys.stderr)
        return 2

    transport_factory = None
    if args.demo:
        from .testing.fake_server import DEMO_TOKEN, DEMO_URL, DEMO_USER, FakeBitbucket

        fake = FakeBitbucket()
        transport_factory = fake.transport
        settings = config.Settings(base_url=DEMO_URL, auth_method="bearer", username=DEMO_USER,
                                   use_macos_keychain_cas=False, clone_dir=settings.clone_dir)
        creds = config.EnvCredentials(base_url=DEMO_URL, username=DEMO_USER, auth_method="bearer", secret=DEMO_TOKEN)

    from .tui.app import BitbucketApp

    app = BitbucketApp(
        settings,
        env_creds=creds,
        env=env,
        transport_factory=transport_factory,
        auto_connect=args.connect or args.demo,
        ui_timeout=max(30.0, settings.request_timeout * 2 + 5),
        demo=args.demo,
    )
    app.run()
    if log_path:
        print(f"Debug log (redacted): {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
