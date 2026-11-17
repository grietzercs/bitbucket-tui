"""Textual application: owns settings and the authenticated client session."""

from __future__ import annotations

import os
from typing import Callable, Dict, Mapping, Optional
from urllib.parse import urlsplit

import httpx
from textual.app import App
from textual.binding import Binding

from ..api import AuthStrategy, BitbucketClient, TLSOptions
from ..api.models import ServerInfo
from ..config import EnvCredentials, Settings
from .common import HelpScreen
from .login import LoginScreen

TransportFactory = Callable[[], httpx.AsyncBaseTransport]


class BitbucketApp(App):
    TITLE = "Bitbucket TUI"
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("f1", "help", "Help"),
        Binding("question_mark", "help", "Help", show=False),
    ]

    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        env_creds: Optional[EnvCredentials] = None,
        env: Optional[Mapping[str, str]] = None,
        transport_factory: Optional[TransportFactory] = None,
        auto_connect: bool = False,
        ui_timeout: float = 60.0,
        demo: bool = False,
    ) -> None:
        super().__init__()
        self.settings = settings or Settings()
        self.env_creds = env_creds
        self._env: Dict[str, str] = dict(env if env is not None else os.environ)
        self.transport_factory = transport_factory
        self.auto_connect = auto_connect
        self.ui_timeout = ui_timeout
        self.demo = demo
        self.client: Optional[BitbucketClient] = None
        self.server: Optional[ServerInfo] = None

    # ------------------------------------------------------------ session
    def env_source(self) -> Mapping[str, str]:
        return self._env

    def make_client(self, url: str, auth: AuthStrategy, tls: TLSOptions) -> BitbucketClient:
        transport = self.transport_factory() if self.transport_factory else None
        return BitbucketClient(
            url,
            auth,
            tls=tls,
            timeout=self.settings.request_timeout,
            connect_timeout=min(10.0, self.settings.request_timeout),
            transport=transport,
        )

    def require_client(self) -> BitbucketClient:
        if self.client is None:
            raise RuntimeError("Not connected")
        return self.client

    def session_label(self) -> str:
        if not self.server:
            return ""
        host = urlsplit(self.server.base_url).netloc
        return f"{self.server.username or '?'} @ {host}" + ("  [DEMO]" if self.demo else "")

    def start_session(self, client: BitbucketClient, info: ServerInfo) -> None:
        self.end_session()
        self.client = client
        self.server = info
        from .browse import ProjectsScreen

        self.push_screen(ProjectsScreen())

    def end_session(self) -> None:
        if self.client is not None:
            client = self.client
            self.client = None
            self.server = None
            self.run_worker(client.aclose(), exit_on_error=False)

    # ------------------------------------------------------------ app
    def on_mount(self) -> None:
        if self.settings.insecure:
            self.notify("TLS verification is DISABLED (--insecure).", severity="error", timeout=10)
        self.push_screen(LoginScreen())

    def action_help(self) -> None:
        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen())

    async def on_unmount(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None
