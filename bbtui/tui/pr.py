"""Pull request detail, diff and merge screens."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, List, Optional

from rich.console import Group
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Select, Static, TabbedContent, TabPane
from textual.worker import Worker

from ..api.errors import ConflictError
from ..api.models import Activity, Change, MergeCheck, MergeConfig, Page, PullRequest, Repository, format_ts
from ..api.paging import Pager
from .common import BaseScreen, ConfirmScreen, PagedTable, TextPromptScreen, error_text, guarded, ui_timeout
from .diffrender import change_label, change_path, render_file_diff

if TYPE_CHECKING:  # pragma: no cover
    from .app import BitbucketApp

STATUS_STYLE = {"APPROVED": "bold green", "NEEDS_WORK": "bold yellow", "UNAPPROVED": "dim"}
STATE_STYLE = {"OPEN": "bold green", "MERGED": "bold magenta", "DECLINED": "bold red"}


def render_merge_check(check: Optional[MergeCheck], error: str = "") -> Text:
    t = Text()
    t.append("Merge check: ", style="bold")
    if error:
        t.append(error, style="red")
        return t
    if check is None:
        t.append("checking…", style="yellow")
        return t
    if check.can_merge:
        t.append(f"can merge ({check.outcome})", style="bold green")
    else:
        t.append(f"cannot merge ({check.outcome})", style="bold red")
    if check.conflicted:
        t.append("\n  ✗ Merge conflicts: resolve them locally and push to the source branch.", style="red")
    for v in check.vetoes:
        t.append(f"\n  ✗ {v.summary}", style="red")
        if v.detail and v.detail != v.summary:
            t.append(f"\n      {v.detail}", style="dim")
    return t


def render_pr_overview(pr: PullRequest, repo: Repository) -> Group:
    head = Text()
    head.append(f"#{pr.id} ", style="bold")
    head.append(pr.title + "\n", style="bold")
    head.append(pr.state, style=STATE_STYLE.get(pr.state, ""))
    head.append(f"   version {pr.version}")
    if pr.locked:
        head.append("   locked", style="yellow")
    head.append("\n")
    head.append("Author: ", style="bold")
    head.append(pr.author.user.label() + "\n")
    frm = pr.from_ref
    src_repo = ""
    if frm.repository and frm.repository.full_name.lower() != repo.full_name.lower():
        src_repo = f"{frm.repository.full_name}:"
    head.append("Branches: ", style="bold")
    head.append(f"{src_repo}{frm.display_id}", style="cyan")
    head.append(" → ")
    head.append(pr.to_ref.display_id + "\n", style="cyan")
    head.append("Created: ", style="bold")
    head.append(f"{format_ts(pr.created_date)}   ")
    head.append("Updated: ", style="bold")
    head.append(f"{format_ts(pr.updated_date)}   ")
    head.append(f"comments: {pr.comment_count}   open tasks: {pr.open_task_count}\n")
    if pr.web_url:
        head.append("URL: ", style="bold")
        head.append(pr.web_url + "\n", style="underline")

    reviewers = Table(title="Reviewers", title_justify="left", show_edge=False, expand=False)
    reviewers.add_column("Reviewer")
    reviewers.add_column("Status")
    for r in pr.reviewers:
        reviewers.add_row(r.user.label(), Text(r.status, style=STATUS_STYLE.get(r.status, "")))
    if not pr.reviewers:
        reviewers.add_row("(none)", "")
    parts = [head, reviewers, Text("")]
    parts.append(Text("Description", style="bold underline"))
    parts.append(Markdown(pr.description) if pr.description.strip() else Text("(no description)", style="dim"))
    return Group(*parts)


def render_activity(acts: List[Activity]) -> Text:
    t = Text()
    if not acts:
        t.append("No activity.", style="dim")
        return t
    for a in acts:
        t.append(f"{format_ts(a.created_date)}  ", style="dim")
        t.append(a.user.display_name or a.user.name, style="bold")
        t.append(f"  {a.action.lower()}", style=STATE_STYLE.get(a.action, "cyan"))
        if a.anchor_path:
            t.append(f"  on {a.anchor_path}" + (f":{a.anchor_line}" if a.anchor_line else ""), style="dim")
        t.append("\n")
        if a.comment is not None:
            _comment(t, a.comment, 1)
    return t


def _comment(t: Text, c, depth: int) -> None:
    pad = "    " * depth
    for line in (c.text or "").splitlines() or [""]:
        t.append(f"{pad}{line}\n")
    for r in c.replies:
        t.append(f"{pad}↳ ", style="dim")
        t.append(f"{r.author.display_name or r.author.name} ({format_ts(r.created_date)})\n", style="bold dim")
        _comment(t, r, depth + 1)


class PullRequestScreen(BaseScreen):
    BINDINGS = [
        Binding("d", "diff", "Diff"),
        Binding("M", "merge", "Merge"),
        Binding("a", "review('APPROVED')", "Approve"),
        Binding("w", "review('NEEDS_WORK')", "Needs work", show=False),
        Binding("X", "decline", "Decline"),
        Binding("n", "comment", "Comment"),
        Binding("c", "clone", "Clone branch"),
        Binding("R", "refresh", "Refresh"),
        Binding("m", "more_activity", "More activity", show=False),
    ]

    DEFAULT_CSS = """
    PullRequestScreen VerticalScroll { padding: 0 1; }
    PullRequestScreen #pr-status { height: auto; padding: 0 1; }
    PullRequestScreen #pr-status.error { color: $error; }
    PullRequestScreen #merge-check { margin-bottom: 1; }
    """

    def __init__(self, repo: Repository, pr: PullRequest) -> None:
        super().__init__()
        self.repo = repo
        self.pr = pr
        self.check: Optional[MergeCheck] = None
        self.check_error = ""
        self.activity_pager: Pager[Activity] = self._new_activity_pager()
        self.activity_loading = False

    @property
    def bapp(self) -> "BitbucketApp":
        return self.app  # type: ignore[return-value]

    @property
    def client(self):
        return self.bapp.require_client()

    def _new_activity_pager(self) -> Pager[Activity]:
        return Pager(
            lambda s, l: self.client.list_activities(self.repo.project.key, self.repo.slug, self.pr.id, start=s, limit=l),
            limit=50,
        )

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="pr-status")
        with TabbedContent(initial="overview"):
            with TabPane("Overview", id="overview"):
                with VerticalScroll():
                    yield Static(id="merge-check")
                    yield Static(id="overview-body")
            with TabPane("Activity", id="activity"):
                with VerticalScroll():
                    yield Static(id="activity-body")
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"{self.repo.full_name} PR #{self.pr.id}"
        self.sub_title = self.bapp.session_label()
        self._render_pr()
        self.action_refresh()

    def _status(self, text: str, error: bool = False) -> None:
        st = self.query_one("#pr-status", Static)
        st.update(Text(text))
        st.set_class(error, "error")

    def _render_pr(self) -> None:
        self.query_one("#overview-body", Static).update(render_pr_overview(self.pr, self.repo))
        self.query_one("#merge-check", Static).update(render_merge_check(self.check, self.check_error))
        hint = "d: diff   M: merge   a: approve   w: needs work   X: decline   n: comment   c: clone branch   R: refresh"
        self._status(hint)

    def action_refresh(self) -> None:
        self.run_api(self._refresh(), group="pr-refresh")

    async def _refresh(self) -> None:
        key, slug, pid = self.repo.project.key, self.repo.slug, self.pr.id
        self._status("Loading pull request…  (Esc to cancel)")
        try:
            self.pr = await guarded(self.client.get_pull_request(key, slug, pid), ui_timeout(self))
        except asyncio.CancelledError:
            self._status("Cancelled. Press R to refresh.", True)
            raise
        except Exception as exc:  # noqa: BLE001
            self._status(error_text(exc) + "\nPress R to retry.", True)
            return
        self.check, self.check_error = None, ""
        self._render_pr()
        if self.pr.state == "OPEN":
            try:
                self.check = await guarded(self.client.merge_check(key, slug, pid), ui_timeout(self))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.check_error = error_text(exc)
        else:
            self.check_error = f"pull request is {self.pr.state.lower()}"
        self._render_pr()
        self.activity_pager = self._new_activity_pager()
        await self._load_activity()

    async def _load_activity(self) -> None:
        if self.activity_loading or self.activity_pager.exhausted:
            return
        self.activity_loading = True
        body = self.query_one("#activity-body", Static)
        try:
            await guarded(self.activity_pager.load_next(), ui_timeout(self))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            body.update(Text(f"Could not load activity: {error_text(exc)}", style="red"))
            return
        finally:
            self.activity_loading = False
        t = render_activity(self.activity_pager.items)
        if not self.activity_pager.exhausted:
            t.append("\n… more activity: press m\n", style="yellow")
        body.update(t)

    def action_more_activity(self) -> None:
        self.run_api(self._load_activity(), group="pr-activity")

    def action_diff(self) -> None:
        self.app.push_screen(DiffScreen(self.repo, self.pr))

    def action_merge(self) -> None:
        if self.pr.state != "OPEN":
            self.notify(f"PR is {self.pr.state}; only OPEN pull requests can be merged.", severity="warning")
            return

        def done(result: Optional[PullRequest]) -> None:
            if result is not None:
                self.pr = result
                self.notify(f"PR #{result.id} merged.", severity="information")
            self.action_refresh()

        self.app.push_screen(MergeScreen(self.repo, self.pr), done)

    def action_review(self, status: str) -> None:
        user = self.bapp.server.user if self.bapp.server else None
        if not user or not user.slug:
            self.notify("Current user unknown; cannot set review status.", severity="warning")
            return
        self.run_api(self._review(user.slug, status), group="pr-action")

    async def _review(self, slug: str, status: str) -> None:
        try:
            await guarded(self.client.set_review_status(self.repo.project.key, self.repo.slug, self.pr.id, slug, status),
                          ui_timeout(self))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._status(f"Could not set review status: {error_text(exc)}", True)
            return
        self.notify(f"Review status set to {status}.")
        await self._refresh()

    def action_decline(self) -> None:
        if self.pr.state != "OPEN":
            self.notify("Only OPEN pull requests can be declined.", severity="warning")
            return

        def confirmed(ok: Optional[bool]) -> None:
            if ok:
                self.run_api(self._decline(), group="pr-action")

        self.app.push_screen(ConfirmScreen(f"Decline PR #{self.pr.id} '{self.pr.title}'?", "Decline", danger=True), confirmed)

    async def _decline(self) -> None:
        try:
            await guarded(self.client.decline(self.repo.project.key, self.repo.slug, self.pr.id, self.pr.version),
                          ui_timeout(self))
        except asyncio.CancelledError:
            raise
        except ConflictError as exc:
            self._status(error_text(exc), True)
            return
        except Exception as exc:  # noqa: BLE001
            self._status(f"Decline failed: {error_text(exc)}", True)
            return
        self.notify("Pull request declined.")
        await self._refresh()

    def action_comment(self) -> None:
        def submitted(text: Optional[str]) -> None:
            if text:
                self.run_api(self._comment(text), group="pr-action")

        self.app.push_screen(TextPromptScreen(f"Comment on PR #{self.pr.id}"), submitted)

    async def _comment(self, text: str) -> None:
        try:
            await guarded(self.client.add_comment(self.repo.project.key, self.repo.slug, self.pr.id, text), ui_timeout(self))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._status(f"Comment failed: {error_text(exc)}", True)
            return
        self.notify("Comment added.")
        await self._refresh()

    def action_clone(self) -> None:
        from .clone import CloneScreen

        src = self.pr.from_ref.repository or self.repo
        if not src.clone_links:
            src = self.repo
        self.app.push_screen(CloneScreen(src, branch=self.pr.from_ref.display_id))


# ---------------------------------------------------------------------- diff
class DiffScreen(BaseScreen):
    BINDINGS = [
        Binding("n", "next_file", "Next file"),
        Binding("p", "prev_file", "Prev file"),
        Binding("x", "toggle_context", "Context ±"),
    ]

    DEFAULT_CSS = """
    DiffScreen Horizontal { height: 1fr; }
    DiffScreen #changes { width: 40%; min-width: 30; border-right: solid $primary; }
    DiffScreen #diff-scroll { width: 1fr; }
    DiffScreen #diff { width: auto; }
    DiffScreen #diff-status { height: auto; padding: 0 1; color: $text-muted; }
    DiffScreen #diff-status.error { color: $error; }
    """

    def __init__(self, repo: Repository, pr: PullRequest) -> None:
        super().__init__()
        self.repo = repo
        self.pr = pr
        self.context_lines = 3
        self.current: Optional[Change] = None
        self._diff_worker: Optional[Worker] = None

    @property
    def client(self):
        return self.app.require_client()  # type: ignore[attr-defined]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="diff-status")
        with Horizontal():
            yield PagedTable(["", "Changed file"], self._fetch_changes,
                             lambda c: (change_label(c), change_path(c)), limit=100,
                             empty_text="No changed files.", id="changes")
            with VerticalScroll(id="diff-scroll"):
                yield Static("Select a file to view its diff.", id="diff")
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"PR #{self.pr.id} diff"
        self.sub_title = f"{self.pr.from_ref.display_id} → {self.pr.to_ref.display_id}"
        self.query_one(PagedTable).focus_table()

    async def _fetch_changes(self, start: int, limit: int, flt: str) -> Page[Change]:
        return await self.client.list_changes(self.repo.project.key, self.repo.slug, self.pr.id, start=start, limit=limit)

    def _status(self, text: str, error: bool = False) -> None:
        st = self.query_one("#diff-status", Static)
        st.update(Text(text))
        st.set_class(error, "error")

    @on(PagedTable.Highlighted, "#changes")
    def _highlighted(self, ev: PagedTable.Highlighted) -> None:
        self._show(ev.item)

    @on(PagedTable.Selected, "#changes")
    def _selected(self, ev: PagedTable.Selected) -> None:
        self._show(ev.item, force=True)
        self.query_one("#diff-scroll").focus()

    def _show(self, change: Change, force: bool = False) -> None:
        if not force and self.current is change:
            return
        self.current = change
        if self._diff_worker is not None and self._diff_worker.is_running:
            self._diff_worker.cancel()
        self._diff_worker = self.run_api(self._load_diff(change), group="diff")

    async def _load_diff(self, change: Change) -> None:
        await asyncio.sleep(0.15)  # debounce rapid cursor movement
        self._status(f"Loading diff for {change.path}…  (Esc to cancel)")
        try:
            result = await guarded(
                self.client.pr_diff(self.repo.project.key, self.repo.slug, self.pr.id, change.path,
                                    context_lines=self.context_lines, src_path=change.src_path),
                ui_timeout(self),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._status(f"{change.path}: {error_text(exc)}", True)
            return
        diff = self.query_one("#diff", Static)
        if not result.diffs:
            diff.update(Text(f"No diff content for {change.path} ({change.type}).", style="italic"))
        else:
            body = Text()
            for fd in result.diffs:
                body.append_text(render_file_diff(fd))
            if result.truncated:
                body.append("\n… diff truncated by server …\n", style="bold yellow")
            diff.update(body)
        self.query_one("#diff-scroll", VerticalScroll).scroll_home(animate=False)
        pt = self.query_one(PagedTable)
        idx = pt.table.cursor_row + 1
        self._status(f"File {idx}/{len(pt.items)}{'+' if not pt.pager.exhausted else ''}: {change.path}  "
                     f"({self.context_lines} context lines · n/p next/prev · x toggle context · Tab to scroll diff)")

    def _move(self, delta: int) -> None:
        pt = self.query_one(PagedTable)
        t = pt.table
        if t.row_count == 0:
            return
        new = max(0, min(t.row_count - 1, t.cursor_row + delta))
        t.move_cursor(row=new)

    def action_next_file(self) -> None:
        self._move(1)

    def action_prev_file(self) -> None:
        self._move(-1)

    def action_toggle_context(self) -> None:
        self.context_lines = 10 if self.context_lines == 3 else 3
        if self.current is not None:
            self._show(self.current, force=True)


# --------------------------------------------------------------------- merge
class MergeScreen(ModalScreen[Optional[PullRequest]]):
    BINDINGS = [Binding("escape", "close", "Close")]

    DEFAULT_CSS = """
    MergeScreen { align: center middle; }
    MergeScreen > Vertical { width: 100; height: auto; max-height: 95%; border: thick $error; background: $surface; padding: 1 2; }
    MergeScreen #m-summary, MergeScreen #m-check, MergeScreen #m-result { height: auto; margin-bottom: 1; }
    MergeScreen #m-result.error { color: $error; }
    MergeScreen #m-result.ok { color: $success; }
    MergeScreen Horizontal { height: auto; }
    MergeScreen Button { margin-right: 2; }
    MergeScreen Select { width: 60; margin-bottom: 1; }
    """

    def __init__(self, repo: Repository, pr: PullRequest) -> None:
        super().__init__()
        self.repo = repo
        self.pr = pr
        self.check: Optional[MergeCheck] = None
        self.config: Optional[MergeConfig] = None
        self.merged: Optional[PullRequest] = None
        self.busy = False
        self._worker: Optional[Worker] = None

    @property
    def client(self):
        return self.app.require_client()  # type: ignore[attr-defined]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(id="m-summary")
            yield Static(render_merge_check(None), id="m-check")
            yield Select([], prompt="Merge strategy (server default)", allow_blank=True, id="m-strategy")
            yield Input(placeholder="Merge commit message (optional; server default if empty)", id="m-message")
            with Horizontal():
                yield Button("Confirm merge", variant="error", id="m-merge", disabled=True)
                yield Button("Refresh", id="m-refresh")
                yield Button("Close", id="m-close")
            yield Static("", id="m-result")

    def on_mount(self) -> None:
        self._summary()
        self.query_one("#m-strategy", Select).display = False
        self._run(self._load())

    def _run(self, coro) -> None:
        self._worker = self.run_worker(coro, group="merge", exclusive=True, exit_on_error=False)

    def _summary(self) -> None:
        t = Text()
        t.append("Merge pull request\n", style="bold underline")
        t.append(f"#{self.pr.id} {self.pr.title}\n", style="bold")
        t.append(f"{self.pr.from_ref.display_id} → {self.pr.to_ref.display_id}", style="cyan")
        t.append(f"    (PR version {self.pr.version}, state {self.pr.state})")
        self.query_one("#m-summary", Static).update(t)

    def _result(self, text: str, cls: str = "") -> None:
        st = self.query_one("#m-result", Static)
        st.update(Text(text))
        st.set_class(cls == "error", "error")
        st.set_class(cls == "ok", "ok")

    async def _load(self, note: str = "") -> None:
        """(Re)load PR, merge check and strategies. ``note`` is kept visible above the outcome."""
        key, slug = self.repo.project.key, self.repo.slug
        self.query_one("#m-merge", Button).disabled = True
        prefix = note + "\n" if note else ""
        self._result(prefix + "Checking mergeability…  (Esc to close)", "error" if note else "")
        try:
            self.pr, self.check, self.config = await guarded(
                asyncio.gather(
                    self.client.get_pull_request(key, slug, self.pr.id),
                    self.client.merge_check(key, slug, self.pr.id),
                    self.client.merge_config(key, slug),
                ),
                ui_timeout(self),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.query_one("#m-check", Static).update(render_merge_check(None, error_text(exc)))
            self._result(prefix + "Could not check mergeability. Press Refresh to retry.", "error")
            return
        self._summary()
        self.query_one("#m-check", Static).update(render_merge_check(self.check))
        sel = self.query_one("#m-strategy", Select)
        previous = None if sel.is_blank() else sel.value
        if self.config and self.config.enabled_strategies:
            sel.set_options([(f"{s.name} ({s.id})" + (" [default]" if s.id == self.config.default_strategy_id else ""), s.id)
                             for s in self.config.enabled_strategies])
            ids = [s.id for s in self.config.enabled_strategies]
            if previous in ids:
                sel.value = previous  # keep the user's choice across refreshes
            elif self.config.default_strategy_id in ids:
                sel.value = self.config.default_strategy_id
            sel.display = True
        else:
            sel.display = False
        can = bool(self.check and self.check.can_merge and self.pr.state == "OPEN")
        self.query_one("#m-merge", Button).disabled = not can
        if can:
            self._result(prefix + "Ready. Review the details above, then press 'Confirm merge'.", "error" if note else "")
        elif self.pr.state != "OPEN":
            self._result(prefix + f"Pull request is {self.pr.state}; nothing to merge.", "error")
        else:
            self._result(prefix + "This pull request cannot be merged right now (see merge check).", "error")

    @on(Button.Pressed, "#m-refresh")
    def _refresh(self) -> None:
        if not self.busy:
            self._run(self._load())

    @on(Button.Pressed, "#m-close")
    def action_close(self) -> None:
        if self.busy:
            self.notify("Merge request in flight; waiting for the server's answer.", severity="warning")
            return
        if self._worker is not None and self._worker.is_running:
            self._worker.cancel()
        self.dismiss(self.merged)

    @on(Button.Pressed, "#m-merge")
    def _merge(self) -> None:
        if self.busy or self.merged is not None:
            return
        strategy = self.query_one("#m-strategy", Select)
        sid = strategy.value if strategy.display and not strategy.is_blank() else None
        msg = self.query_one("#m-message", Input).value.strip() or None
        self._run(self._do_merge(str(sid) if sid else None, msg))

    async def _do_merge(self, strategy_id: Optional[str], message: Optional[str]) -> None:
        self.busy = True
        self.query_one("#m-merge", Button).disabled = True
        self._result(f"Merging with version {self.pr.version}" + (f", strategy {strategy_id}" if strategy_id else "") + " …")
        try:
            merged = await guarded(
                self.client.merge(self.repo.project.key, self.repo.slug, self.pr.id, self.pr.version,
                                  strategy_id=strategy_id, message=message),
                ui_timeout(self),
            )
        except ConflictError as exc:
            self.busy = False
            if exc.is_out_of_date:
                cur = exc.current_version
                note = (
                    f"The pull request was updated by someone else (your version {self.pr.version}"
                    + (f", current {cur}" if cur is not None else "")
                    + "). Details were refreshed: review them and confirm again."
                )
                await self._load(note)
                return
            lines = ["Merge refused by the server (409)."]
            if exc.conflicted:
                lines.append("✗ The pull request has merge conflicts.")
            for v in exc.vetoes:
                lines.append(f"✗ {v.summary}" + (f": {v.detail}" if v.detail else ""))
            if not exc.vetoes and not exc.conflicted:
                lines.append(exc.message)
            self._result("\n".join(lines), "error")
            return
        except Exception as exc:  # noqa: BLE001
            self.busy = False
            self._result(
                "Merge failed or outcome unknown: " + error_text(exc)
                + "\nPress Refresh to see the pull request's current state before retrying.",
                "error",
            )
            return
        self.busy = False
        self.merged = merged
        self._result(f"✓ Merged. PR #{merged.id} is now {merged.state} (version {merged.version}). Press Close.", "ok")
