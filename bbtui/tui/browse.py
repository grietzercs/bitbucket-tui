"""Browsing screens: projects, repositories, repository (files/branches/PRs), file view."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, List, Optional

from rich.syntax import Syntax
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer, Vertical
from textual.widgets import Footer, Header, Select, Static, TabbedContent, TabPane

from ..api import PR_STATES
from ..api.models import Branch, BrowseEntry, Page, Project, PullRequest, Repository, format_ts
from .common import BaseScreen, PagedTable, error_text, guarded, human_size, ui_timeout

if TYPE_CHECKING:  # pragma: no cover
    from .app import BitbucketApp


class _Session(BaseScreen):
    @property
    def bapp(self) -> "BitbucketApp":
        return self.app  # type: ignore[return-value]

    @property
    def client(self):
        return self.bapp.require_client()


# ------------------------------------------------------------------ projects
class ProjectsScreen(_Session):
    BINDINGS = [
        Binding("slash", "search_repos", "Search repos"),
        Binding("tilde", "personal", "My repos"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="info")
        yield PagedTable(
            ["Key", "Name", "Description"],
            self._fetch,
            lambda p: (p.key, p.name, (p.description or "")[:80]),
            limit=50,
            filter_placeholder="Filter projects by name (server-side)…",
            empty_text="No projects visible to this account.",
        )
        yield Footer()

    def on_mount(self) -> None:
        info = self.bapp.server
        self.title = "Projects"
        self.sub_title = self.bapp.session_label()
        lines = [f"Connected to {info.base_url}  ·  Bitbucket {info.version or '?'}  ·  user: {info.username or '?'}"]
        lines.append("Enter: open project   /: search all repositories   ~: my personal repositories   F1: help")
        for w in info.warnings:
            lines.append(f"⚠ {w}")
        self.query_one("#info", Static).update(Text("\n".join(lines)))
        self.query_one(PagedTable).focus_table()

    async def _fetch(self, start: int, limit: int, flt: str) -> Page[Project]:
        return await self.client.list_projects(start=start, limit=limit, name=flt)

    @on(PagedTable.Selected)
    def _open(self, ev: PagedTable.Selected) -> None:
        self.app.push_screen(ReposScreen(ev.item))

    def action_search_repos(self) -> None:
        self.app.push_screen(ReposScreen(None))

    def action_personal(self) -> None:
        user = self.bapp.server.user
        if not user or not user.slug:
            self.notify("Current username unknown; cannot open personal repositories.", severity="warning")
            return
        self.app.push_screen(
            ReposScreen(Project(key=f"~{user.slug}", name=f"{user.display_name or user.name} (personal)", type="PERSONAL"))
        )


# --------------------------------------------------------------------- repos
class ReposScreen(_Session):
    """Repos of one project, or global repository search when ``project`` is None."""

    def __init__(self, project: Optional[Project]) -> None:
        super().__init__()
        self.project = project

    def compose(self) -> ComposeResult:
        yield Header()
        if self.project is None:
            placeholder = "Search all repositories by name (server-side, /rest/api/1.0/repos?name=)…"
            cols = ["Project", "Slug", "Name", "State"]
        else:
            placeholder = f"Filter repositories in {self.project.key} by name…"
            cols = ["Slug", "Name", "State", "Description"]
        yield PagedTable(cols, self._fetch, self._row, limit=50, filter_placeholder=placeholder,
                         empty_text="No repositories found.")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Repositories" if self.project is None else f"{self.project.key}: {self.project.name}"
        self.sub_title = self.bapp.session_label()
        pt = self.query_one(PagedTable)
        if self.project is None:
            pt.query_one(".filter").focus()
        else:
            pt.focus_table()

    def _row(self, r: Repository):
        state = "archived" if r.archived else r.state.lower()
        if self.project is None:
            return (r.project.key, r.slug, r.name, state)
        return (r.slug, r.name, state, (r.description or "")[:60])

    async def _fetch(self, start: int, limit: int, flt: str) -> Page[Repository]:
        c = self.client
        if self.project is None:
            return await c.search_repos(start=start, limit=limit, name=flt)
        if not flt:
            return await c.list_project_repos(self.project.key, start=start, limit=limit)
        # Server-side name filter via /repos, narrowed to this project.
        page = await c.search_repos(
            start=start, limit=limit, name=flt,
            project_name="" if self.project.is_personal else self.project.name,
        )
        key = self.project.key.lower()
        page.values = [r for r in page.values if r.project.key.lower() == key]
        return page

    @on(PagedTable.Selected)
    def _open(self, ev: PagedTable.Selected) -> None:
        self.app.push_screen(RepoScreen(ev.item))


# ---------------------------------------------------------------- repository
class RepoScreen(_Session):
    BINDINGS = [
        Binding("f", "tab('files')", "Files"),
        Binding("b", "tab('branches')", "Branches"),
        Binding("p", "tab('prs')", "PRs"),
        Binding("c", "clone", "Clone"),
        Binding("s", "cycle_state", "PR state", show=False),
        Binding("backspace", "up_dir", "Up dir", show=False),
    ]

    DEFAULT_CSS = """
    RepoScreen #loc, RepoScreen #pr-bar { height: auto; padding: 0 1; color: $accent; }
    RepoScreen #pr-bar Select { width: 30; }
    """

    def __init__(self, repo: Repository) -> None:
        super().__init__()
        self.repo = repo
        self.ref = ""  # empty = default branch
        self.ref_label = "default branch"
        self.path = ""
        self.pr_state = "OPEN"

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="files"):
            with TabPane("Files", id="files"):
                yield Static(id="loc")
                yield PagedTable(["", "Name", "Size"], self._fetch_files, self._file_row, limit=200,
                                 empty_text="Empty directory.", id="files-table")
            with TabPane("Branches", id="branches"):
                yield PagedTable(["Branch", "Default", "Latest commit"], self._fetch_branches,
                                 lambda b: (b.display_id, "✓" if b.is_default else "", b.latest_commit[:12]),
                                 limit=50, filter_placeholder="Filter branches (server-side)…", autoload=False,
                                 id="branches-table")
            with TabPane("Pull requests", id="prs"):
                with Vertical(id="pr-bar"):
                    yield Select([(s, s) for s in PR_STATES], value="OPEN", allow_blank=False, id="pr-state")
                yield PagedTable(["#", "Title", "Author", "From → To", "State", "Updated"], self._fetch_prs,
                                 self._pr_row, limit=25, autoload=False, empty_text="No pull requests in this state.",
                                 id="prs-table")
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"{self.repo.project.key}/{self.repo.slug}"
        self.sub_title = self.bapp.session_label()
        self._update_loc()
        self.query_one("#files-table", PagedTable).focus_table()

    # ------------------------------------------------------------ files
    def _update_loc(self) -> None:
        self.query_one("#loc", Static).update(
            Text(f"Branch: {self.ref_label}   Path: /{self.path}   (Enter: open · Backspace: up · b: switch branch)")
        )

    async def _fetch_files(self, start: int, limit: int, flt: str) -> Page[BrowseEntry]:
        d = await self.client.browse_dir(self.repo.project.key, self.repo.slug, self.path, at=self.ref,
                                         start=start, limit=limit)
        page = d.children
        if start == 0 and self.path:
            parent = self.path.rsplit("/", 1)[0] if "/" in self.path else ""
            page.values.insert(0, BrowseEntry(name="..", path=parent, type="PARENT"))
        return page

    @staticmethod
    def _file_row(e: BrowseEntry):
        icon = {"DIRECTORY": "📁", "PARENT": "⬆", "SUBMODULE": "🔗"}.get(e.type, "📄")
        name = e.name + ("/" if e.type == "DIRECTORY" else "")
        return (icon, name, human_size(e.size) if e.type == "FILE" else "")

    def _open_dir(self, path: str) -> None:
        self.path = path.strip("/")
        self._update_loc()
        self.query_one("#files-table", PagedTable).reload()

    def action_up_dir(self) -> None:
        if self.query_one(TabbedContent).active == "files" and self.path:
            self._open_dir(self.path.rsplit("/", 1)[0] if "/" in self.path else "")

    @on(PagedTable.Selected, "#files-table")
    def _file_selected(self, ev: PagedTable.Selected) -> None:
        e: BrowseEntry = ev.item
        if e.type in ("DIRECTORY", "PARENT"):
            self._open_dir(e.path)
        elif e.type == "SUBMODULE":
            self.notify(f"{e.path} is a git submodule; browse it in its own repository.")
        else:
            self.app.push_screen(FileViewScreen(self.repo, e.path, self.ref, self.ref_label))

    # --------------------------------------------------------- branches
    async def _fetch_branches(self, start: int, limit: int, flt: str) -> Page[Branch]:
        return await self.client.list_branches(self.repo.project.key, self.repo.slug, start=start, limit=limit,
                                               filter_text=flt)

    @on(PagedTable.Selected, "#branches-table")
    def _branch_selected(self, ev: PagedTable.Selected) -> None:
        b: Branch = ev.item
        self.ref = b.id
        self.ref_label = b.display_id
        self.path = ""
        self.query_one(TabbedContent).active = "files"
        self._update_loc()
        ft = self.query_one("#files-table", PagedTable)
        ft.reload()
        ft.focus_table()
        self.notify(f"Browsing branch {b.display_id}")

    # -------------------------------------------------------------- PRs
    async def _fetch_prs(self, start: int, limit: int, flt: str) -> Page[PullRequest]:
        return await self.client.list_pull_requests(self.repo.project.key, self.repo.slug, self.pr_state,
                                                    start=start, limit=limit)

    @staticmethod
    def _pr_row(p: PullRequest):
        return (
            str(p.id),
            p.title[:70],
            p.author.user.display_name or p.author.user.name,
            f"{p.from_ref.display_id} → {p.to_ref.display_id}",
            p.state,
            format_ts(p.updated_date),
        )

    @on(Select.Changed, "#pr-state")
    def _state_changed(self, ev: Select.Changed) -> None:
        if ev.value and ev.value != self.pr_state:
            self.pr_state = str(ev.value)
            self.query_one("#prs-table", PagedTable).reload()

    def action_cycle_state(self) -> None:
        if self.query_one(TabbedContent).active != "prs":
            return
        idx = (PR_STATES.index(self.pr_state) + 1) % len(PR_STATES)
        self.query_one("#pr-state", Select).value = PR_STATES[idx]

    @on(PagedTable.Selected, "#prs-table")
    def _pr_selected(self, ev: PagedTable.Selected) -> None:
        from .pr import PullRequestScreen

        self.app.push_screen(PullRequestScreen(self.repo, ev.item))

    # ------------------------------------------------------------- tabs
    @on(TabbedContent.TabActivated)
    def _tab_activated(self, ev: TabbedContent.TabActivated) -> None:
        pane_id = ev.pane.id if ev.pane is not None else ""
        table_id = {"files": "#files-table", "branches": "#branches-table", "prs": "#prs-table"}.get(pane_id or "")
        if table_id:
            pt = self.query_one(table_id, PagedTable)
            pt.ensure_loaded()
            pt.focus_table()

    def action_tab(self, name: str) -> None:
        self.query_one(TabbedContent).active = name

    def action_clone(self) -> None:
        from .clone import CloneScreen

        branch = self.ref_label if self.ref else ""
        self.app.push_screen(CloneScreen(self.repo, branch=branch))


# ----------------------------------------------------------------- file view
class FileViewScreen(_Session):
    BINDINGS = [Binding("m", "more", "More lines")]
    PAGE = 1000
    MAX_LINES = 50000

    DEFAULT_CSS = """
    FileViewScreen #file-status { height: auto; padding: 0 1; color: $text-muted; }
    FileViewScreen #file-status.error { color: $error; }
    FileViewScreen ScrollableContainer { height: 1fr; }
    FileViewScreen #code { width: auto; }
    """

    def __init__(self, repo: Repository, path: str, ref: str, ref_label: str) -> None:
        super().__init__()
        self.repo = repo
        self.path = path
        self.ref = ref
        self.ref_label = ref_label
        self.lines: List[str] = []
        self.next_start: Optional[int] = 0
        self.loading = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="file-status")
        with ScrollableContainer():
            yield Static("", id="code")
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"{self.repo.slug}: {self.path}"
        self.sub_title = f"@ {self.ref_label}"
        self.query_one(ScrollableContainer).focus()
        self.action_more()

    def action_more(self) -> None:
        if self.loading or self.next_start is None:
            return
        if len(self.lines) >= self.MAX_LINES:
            self._status(f"Display limit of {self.MAX_LINES} lines reached.")
            return
        self.run_api(self._load(self.next_start))

    def _status(self, text: str, error: bool = False) -> None:
        st = self.query_one("#file-status", Static)
        st.update(Text(text))
        st.set_class(error, "error")

    async def _load(self, start: int) -> None:
        self.loading = True
        self._status(f"Loading lines from {start + 1}…  (Esc to cancel)")
        try:
            page = await guarded(
                self.client.browse_file(self.repo.project.key, self.repo.slug, self.path, at=self.ref,
                                        start=start, limit=self.PAGE),
                ui_timeout(self),
            )
        except asyncio.CancelledError:
            self._status("Cancelled. Press m to retry.", True)
            raise
        except Exception as exc:  # noqa: BLE001
            self._status(error_text(exc) + "\nPress m to retry.", True)
            return
        finally:
            self.loading = False
        if page.binary:
            self.next_start = None
            self.query_one("#code", Static).update(Text("Binary file: not displayed.", style="italic yellow"))
            self._status("Binary file.")
            return
        self.lines.extend(page.lines)
        self.next_start = page.next_page_start if not page.is_last_page else None
        code = "\n".join(self.lines)
        try:
            lexer = Syntax.guess_lexer(self.path, code=code[:5000])
        except Exception:  # noqa: BLE001
            lexer = "text"
        self.query_one("#code", Static).update(
            Syntax(code, lexer, line_numbers=True, word_wrap=False, theme="monokai", background_color="default",
                   indent_guides=False)
        )
        more = "  ·  more available: press m" if self.next_start is not None else "  ·  end of file"
        self._status(f"{len(self.lines)} line(s) loaded{more}")
