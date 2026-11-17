"""Clone screen: pick protocol + destination, run git clone with streamed output."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Optional

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Footer, Header, Input, Label, RadioButton, RadioSet, RichLog, Static
from textual.worker import Worker

from .. import config
from ..api.models import Repository
from ..services.clone import (
    MIN_GIT_FOR_ENV_CONFIG,
    PROTOCOL_LABELS,
    CloneError,
    CloneRunner,
    available_protocols,
    build_clone_plan,
    check_destination,
    clone_url_for,
    config_contains_secret,
    explain_failure,
    git_version,
    resolve_ssh_target,
    tcp_preflight,
)
from .common import BaseScreen, ConfirmScreen, PathSuggester, error_text

if TYPE_CHECKING:  # pragma: no cover
    from .app import BitbucketApp


class CloneScreen(BaseScreen):
    BINDINGS = [Binding("ctrl+s", "start", "Clone")]

    DEFAULT_CSS = """
    CloneScreen VerticalScroll#form { height: auto; max-height: 75%; padding: 0 2; }
    CloneScreen RadioSet { height: auto; }
    CloneScreen Input { margin: 0; }
    CloneScreen Label.field { margin-top: 1; text-style: bold; }
    CloneScreen #dest-check { height: auto; color: $text-muted; }
    CloneScreen #dest-check.error { color: $error; }
    CloneScreen Horizontal { height: auto; margin-top: 1; }
    CloneScreen Button { margin-right: 2; }
    CloneScreen #progress { height: auto; padding: 0 2; color: $warning; }
    CloneScreen #result { height: auto; padding: 0 2; }
    CloneScreen #result.error { color: $error; }
    CloneScreen #result.ok { color: $success; }
    CloneScreen RichLog { height: 1fr; min-height: 6; border: round $primary; margin: 0 1; }
    """

    def __init__(self, repo: Repository, branch: str = "") -> None:
        super().__init__()
        self.repo = repo
        self.branch = branch
        self.runner: Optional[CloneRunner] = None
        self._worker: Optional[Worker] = None

    @property
    def bapp(self) -> "BitbucketApp":
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        s = self.bapp.settings
        protos = available_protocols(self.repo)
        default_proto = s.clone_protocol if s.clone_protocol in protos else (protos[0] if protos else "http")
        dest = os.path.join(s.clone_dir or "~/dev", self.repo.slug)
        yield Header()
        with VerticalScroll(id="form"):
            yield Static(Text(f"Clone {self.repo.full_name}" + (f"  (branch {self.branch})" if self.branch else ""),
                              style="bold"))
            yield Label("Protocol", classes="field")
            with RadioSet(id="proto"):
                for p in ("http", "ssh"):
                    try:
                        href = clone_url_for(self.repo, p)  # userinfo stripped for HTTPS
                    except CloneError:
                        href = "not offered by server"
                    label = f"{PROTOCOL_LABELS[p]}  {href}" + ("  (uses your token, not stored)" if p == "http" else "")
                    yield RadioButton(label, value=p == default_proto, id=f"proto-{p}", disabled=p not in protos)
            yield Label("Branch (empty = default branch)", classes="field")
            yield Input(value=self.branch, placeholder="main", id="branch")
            yield Label("Destination directory (→ accepts completion)", classes="field")
            yield Input(value=dest, suggester=PathSuggester(), id="dest")
            yield Static("", id="dest-check")
            with Horizontal():
                yield Button("Clone (Ctrl+S)", variant="primary", id="start")
                yield Button("Cancel clone", id="cancel", disabled=True)
                yield Button("Back", id="back")
        yield Static("", id="progress")
        yield Static("", id="result")
        yield RichLog(id="log", wrap=True, markup=False, highlight=False, max_lines=5000)
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Clone"
        self.sub_title = self.repo.full_name
        self._check_dest()
        self.query_one("#dest", Input).focus()
        if not available_protocols(self.repo):
            self._result("The server returned no clone links for this repository.", "error")

    # ----------------------------------------------------------- helpers
    def _protocol(self) -> str:
        return "ssh" if self.query_one("#proto-ssh", RadioButton).value else "http"

    def _result(self, text: str, cls: str = "") -> None:
        st = self.query_one("#result", Static)
        st.update(Text(text))
        st.set_class(cls == "error", "error")
        st.set_class(cls == "ok", "ok")

    def _check_dest(self) -> None:
        chk = check_destination(self.query_one("#dest", Input).value)
        st = self.query_one("#dest-check", Static)
        st.update(Text(chk.message))
        st.set_class(not chk.ok, "error")

    @property
    def running(self) -> bool:
        return self._worker is not None and self._worker.is_running

    @on(Input.Changed, "#dest")
    def _dest_changed(self) -> None:
        self._check_dest()

    @on(Input.Submitted)
    def _submitted(self) -> None:
        self.action_start()

    @on(Button.Pressed, "#start")
    def _start_pressed(self) -> None:
        self.action_start()

    @on(Button.Pressed, "#back")
    def _back_pressed(self) -> None:
        self.action_back()

    @on(Button.Pressed, "#cancel")
    def _cancel_pressed(self) -> None:
        self.cancel_loading()

    def cancel_loading(self) -> bool:
        if self.running and self._worker is not None:
            self._worker.cancel()
            return True
        return False

    def action_back(self) -> None:
        if self.cancel_loading():
            self.notify("Clone cancelled.")
            return
        self.app.pop_screen()

    # ------------------------------------------------------------ start
    def action_start(self) -> None:
        if self.running:
            return
        chk = check_destination(self.query_one("#dest", Input).value)
        if not chk.ok:
            self._result(chk.message, "error")
            return
        protocol = self._protocol()
        try:
            url = clone_url_for(self.repo, protocol)
        except CloneError as exc:
            self._result(exc.describe(), "error")
            return
        if chk.needs_parent:
            parent = os.path.dirname(chk.path)

            def confirmed(ok: Optional[bool]) -> None:
                if not ok:
                    self._result("Cancelled: parent directory not created.", "error")
                    return
                try:
                    os.makedirs(parent, exist_ok=True)
                except OSError as exc:
                    self._result(f"Could not create {parent}: {exc}", "error")
                    return
                self._check_dest()
                self._launch(url, chk.path, protocol)

            self.app.push_screen(ConfirmScreen(f"Directory {parent} does not exist.\nCreate it?", "Create"), confirmed)
            return
        self._launch(url, chk.path, protocol)

    def _launch(self, url: str, dest: str, protocol: str) -> None:
        self._worker = self.run_worker(self._clone(url, dest, protocol), group="clone", exclusive=True,
                                       exit_on_error=False)

    async def _clone(self, url: str, dest: str, protocol: str) -> None:
        log = self.query_one("#log", RichLog)
        progress = self.query_one("#progress", Static)
        start_btn = self.query_one("#start", Button)
        cancel_btn = self.query_one("#cancel", Button)
        branch = self.query_one("#branch", Input).value.strip()
        start_btn.disabled = True
        cancel_btn.disabled = False
        self._result("")
        progress.update("")
        try:
            if protocol == "http":
                ver = await git_version()
                if ver is None:
                    self._result("git was not found on PATH.", "error")
                    return
                if ver < MIN_GIT_FOR_ENV_CONFIG:
                    self._result(
                        f"git {ver[0]}.{ver[1]} is too old for secret-free HTTPS auth (needs >= 2.31).", "error"
                    )
                    return
            auth = self.bapp.client.auth if self.bapp.client else None
            try:
                plan = build_clone_plan(url, dest, protocol, auth if protocol == "http" else None, branch=branch)
            except CloneError as exc:
                self._result(exc.describe(), "error")
                return
            target = await resolve_ssh_target(*plan.ssh_target) if plan.ssh_target else None
            if target:
                host, port = target
                progress.update(f"Checking SSH reachability of {host}:{port}…")
                err = await tcp_preflight(host, port, timeout=6)
                if err:
                    progress.update("")
                    self._result(
                        f"{err}\nHint: SSH port {port} is not reachable from this machine (firewall/VPN). "
                        "Select HTTPS and try again.",
                        "error",
                    )
                    return
            log.write(f"$ {plan.display_command()}")
            if protocol == "http":
                log.write("  (Authorization header supplied via transient GIT_CONFIG_* environment; not stored)")
            self.runner = CloneRunner(plan)

            def on_line(line: str, is_progress: bool) -> None:
                if is_progress:
                    progress.update(Text(line))
                else:
                    log.write(line)

            rc = await self.runner.run(on_line)
            progress.update("")
            if rc == 0:
                msg = f"✓ Cloned into {plan.dest}\nRemote URL: {plan.url}"
                if plan.protocol == "http":
                    if config_contains_secret(plan.dest, plan.secrets):
                        msg += "\n⚠ WARNING: credentials were found in .git/config; remove them!"
                    else:
                        msg += (
                            "\nNo credentials were stored in the clone. For later fetch/push over HTTPS, git will use "
                            "your normal credential helper (e.g. osxkeychain), or use SSH."
                        )
                self._result(msg, "ok")
                self._remember(dest, protocol)
            else:
                hint = explain_failure("\n".join(self.runner.output), protocol, plan.ssh_target)
                self._result(f"git clone failed (exit code {rc})." + (f"\nHint: {hint}" if hint else ""), "error")
        except asyncio.CancelledError:
            if self.runner is not None:
                await self.runner.terminate()
            progress.update("")
            self._result("Clone cancelled (git cleans up the partial directory).", "error")
            raise
        except Exception as exc:  # noqa: BLE001
            self._result(f"Clone failed: {error_text(exc)}", "error")
        finally:
            start_btn.disabled = False
            cancel_btn.disabled = True
            self._check_dest()

    def _remember(self, dest: str, protocol: str) -> None:
        s = self.bapp.settings
        s.clone_dir = os.path.dirname(dest)
        s.clone_protocol = protocol
        if s.remember and not self.bapp.demo:
            try:
                config.save(s)
            except OSError:
                pass
