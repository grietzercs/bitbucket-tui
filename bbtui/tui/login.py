"""Connection / login screen."""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING, Optional

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Checkbox, Collapsible, Footer, Header, Input, Label, RadioButton, RadioSet, Static

from .. import config, keychain
from ..api import BitbucketClient, ConfigError, TLSOptions, make_auth, normalize_base_url
from .common import BaseScreen, error_text, guarded

if TYPE_CHECKING:  # pragma: no cover
    from .app import BitbucketApp


class LoginScreen(BaseScreen):
    BINDINGS = [
        Binding("ctrl+s", "connect", "Connect"),
        Binding("ctrl+e", "load_env", "Load from env"),
        Binding("escape", "cancel_connect", "Cancel"),
    ]

    DEFAULT_CSS = """
    LoginScreen VerticalScroll { padding: 1 2; }
    LoginScreen Label.field { margin-top: 1; text-style: bold; }
    LoginScreen #status { margin-top: 1; height: auto; }
    LoginScreen #status.error { color: $error; }
    LoginScreen #status.ok { color: $success; }
    LoginScreen #insecure-warning { color: $error; text-style: bold; display: none; }
    LoginScreen #insecure-warning.show { display: block; }
    LoginScreen Horizontal { height: auto; margin-top: 1; }
    LoginScreen Button { margin-right: 2; }
    LoginScreen RadioSet { width: 100%; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._client: Optional[BitbucketClient] = None
        self._connect_worker = None

    @property
    def bapp(self) -> "BitbucketApp":
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        s = self.bapp.settings
        yield Header()
        with VerticalScroll():
            yield Static(
                Text.from_markup(
                    "[b]Connect to Bitbucket Server / Data Center[/b]\n"
                    "Enter the base URL you open in the browser and an HTTP access token (PAT).\n"
                    "Tokens are never written to disk unless you opt in to the macOS Keychain below."
                )
            )
            yield Label("Server URL", classes="field")
            yield Input(value=s.base_url, placeholder="https://bitbucket.example.com", id="url")
            yield Label("Authentication method", classes="field")
            with RadioSet(id="method"):
                yield RadioButton("Bearer token (HTTP access token / PAT)", value=s.auth_method != "basic", id="m-bearer")
                yield RadioButton("Basic (username + password or PAT)", value=s.auth_method == "basic", id="m-basic")
            yield Label("Username (required for Basic; optional for Bearer)", classes="field")
            yield Input(value=s.username, placeholder="username", id="username")
            yield Label("Token / password", classes="field")
            yield Input(password=True, placeholder="paste token (hidden)", id="secret")
            with Collapsible(title="TLS options", collapsed=not (s.ca_bundle or s.insecure)):
                yield Label("Custom CA bundle (PEM) path, optional", classes="field")
                yield Input(value=s.ca_bundle, placeholder="~/certs/corp-ca.pem", id="ca_bundle")
                yield Checkbox(
                    "Also trust CA certificates from the macOS Keychain (recommended for internal CAs)",
                    value=s.use_macos_keychain_cas,
                    id="keychain_cas",
                    disabled=sys.platform != "darwin",
                )
                yield Checkbox("Disable TLS certificate verification (INSECURE)", value=s.insecure, id="insecure")
                yield Static(
                    "WARNING: TLS verification is OFF. Your token can be intercepted by anyone on the network path. "
                    "Use only for short-lived diagnosis.",
                    id="insecure-warning",
                    classes="show" if s.insecure else "",
                )
            yield Checkbox("Remember server URL and username on this machine (never the token)", value=s.remember, id="remember")
            yield Checkbox("Save token in macOS Keychain (opt-in)", value=s.token_in_keychain, id="save_token")
            with Horizontal():
                yield Button("Connect (Ctrl+S)", variant="primary", id="connect")
                yield Button("Load from env (Ctrl+E)", id="env")
                yield Button("Quit", id="quit")
            yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Bitbucket TUI"
        self.sub_title = "connect"
        app = self.bapp
        if app.env_creds and (app.env_creds.base_url or app.env_creds.secret):
            self._apply_env(app.env_creds, quiet=True)
        else:
            self._load_keychain_token()
        self.query_one("#url" if not self.query_one("#url", Input).value else "#secret", Input).focus()
        if app.auto_connect and self.query_one("#url", Input).value and self.query_one("#secret", Input).value:
            self.action_connect()

    def on_screen_resume(self) -> None:
        # Back on the login screen: the previous session (if any) is closed.
        self.bapp.end_session()
        self.sub_title = "connect"

    # ------------------------------------------------------------- helpers
    def _method(self) -> str:
        return "basic" if self.query_one("#m-basic", RadioButton).value else "bearer"

    def _status(self, text: str, cls: str = "") -> None:
        st = self.query_one("#status", Static)
        st.update(Text(text))
        st.set_class(cls == "error", "error")
        st.set_class(cls == "ok", "ok")

    def _apply_env(self, creds: config.EnvCredentials, quiet: bool = False) -> None:
        if creds.base_url:
            self.query_one("#url", Input).value = creds.base_url
        if creds.username:
            self.query_one("#username", Input).value = creds.username
        if creds.auth_method in ("basic", "bearer"):
            self.query_one("#m-basic" if creds.auth_method == "basic" else "#m-bearer", RadioButton).value = True
        if creds.secret:
            self.query_one("#secret", Input).value = creds.secret
        if creds.ca_bundle:
            self.query_one("#ca_bundle", Input).value = creds.ca_bundle
        parts = [n for n, v in (("URL", creds.base_url), ("user", creds.username), ("token", creds.secret)) if v]
        if parts:
            self._status(f"Loaded {', '.join(parts)} from environment.", "ok")
        elif not quiet:
            self._status(
                "No BITBUCKET_URL / BITBUCKET_PAT / BITBUCKET_USER / BITBUCKET_PASSWORD found in the environment.",
                "error",
            )

    def _load_keychain_token(self) -> None:
        s = self.bapp.settings
        if not (s.token_in_keychain and s.base_url):
            return
        try:
            url = normalize_base_url(s.base_url)
        except ConfigError:
            return
        tok = keychain.get_token(url, s.auth_method, s.username)
        if tok:
            self.query_one("#secret", Input).value = tok
            self._status("Token loaded from macOS Keychain.", "ok")

    # ------------------------------------------------------------- events
    @on(Checkbox.Changed, "#insecure")
    def _insecure_changed(self, ev: Checkbox.Changed) -> None:
        self.query_one("#insecure-warning").set_class(ev.value, "show")
        if ev.value:
            self.notify("TLS verification disabled: INSECURE", severity="error", timeout=6)

    @on(Input.Submitted)
    def _submitted(self, ev: Input.Submitted) -> None:
        self.action_connect()

    @on(Button.Pressed, "#connect")
    def _connect_pressed(self) -> None:
        self.action_connect()

    @on(Button.Pressed, "#env")
    def action_load_env(self) -> None:
        self._apply_env(config.credentials_from_env(self.bapp.env_source()))

    @on(Button.Pressed, "#quit")
    def _quit(self) -> None:
        self.app.exit()

    def action_cancel_connect(self) -> None:
        if self._connect_worker is not None and self._connect_worker.is_running:
            self._connect_worker.cancel()
            self._status("Connection attempt cancelled.", "error")

    def action_connect(self) -> None:
        if self._connect_worker is not None and self._connect_worker.is_running:
            return
        url_raw = self.query_one("#url", Input).value
        method = self._method()
        username = self.query_one("#username", Input).value.strip()
        secret = self.query_one("#secret", Input).value
        try:
            url = normalize_base_url(url_raw)
            auth = make_auth(method, username=username, secret=secret)
        except ConfigError as exc:
            self._status(error_text(exc), "error")
            return
        except ValueError as exc:
            self._status(str(exc), "error")
            return
        self.query_one("#url", Input).value = url
        tls = TLSOptions(
            verify=not self.query_one("#insecure", Checkbox).value,
            ca_bundle=self.query_one("#ca_bundle", Input).value.strip() or None,
            use_macos_keychain=self.query_one("#keychain_cas", Checkbox).value,
        )
        try:
            client = self.bapp.make_client(url, auth, tls)
        except (OSError, ValueError) as exc:  # e.g. CA bundle missing/invalid
            self._status(f"TLS setup failed: {exc}", "error")
            return
        self._status(f"Connecting to {url} …  (Esc to cancel)")
        self._connect_worker = self.run_worker(
            self._do_connect(client, method, username, secret, tls), group="connect", exit_on_error=False
        )

    async def _do_connect(self, client: BitbucketClient, method: str, username: str, secret: str, tls: TLSOptions) -> None:
        try:
            info = await guarded(client.test_connection(), self.bapp.ui_timeout)
        except asyncio.CancelledError:
            await client.aclose()
            raise
        except Exception as exc:  # noqa: BLE001
            await client.aclose()
            self._status("Connection failed.\n" + error_text(exc), "error")
            return
        s = self.bapp.settings
        s.base_url = client.base_url
        s.auth_method = method
        s.username = username
        s.ca_bundle = tls.ca_bundle or ""
        s.use_macos_keychain_cas = tls.use_macos_keychain
        s.insecure = not tls.verify
        s.remember = self.query_one("#remember", Checkbox).value
        want_keychain = self.query_one("#save_token", Checkbox).value
        notes = []
        if want_keychain and not self.bapp.demo:
            if keychain.set_token(client.base_url, method, username, secret):
                s.token_in_keychain = True
                notes.append("token saved in Keychain")
            else:
                notes.append("could not save token in Keychain")
        elif s.token_in_keychain and not want_keychain:
            keychain.delete_token(client.base_url, method, username)
            s.token_in_keychain = False
        if (s.remember or s.token_in_keychain) and not self.bapp.demo:
            config.save(s)
        elif not s.remember and not self.bapp.demo:
            config.forget()
        msg = f"Connected as {info.username or 'unknown user'} (Bitbucket {info.version or '?'})"
        if notes:
            msg += "; " + ", ".join(notes)
        self._status(msg, "ok")
        self.bapp.start_session(client, info)
