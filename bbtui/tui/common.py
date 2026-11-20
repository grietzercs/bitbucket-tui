"""Shared TUI building blocks.

The key piece is :class:`PagedTable`: a DataTable backed by a lazy
:class:`~bbtui.api.paging.Pager`. Every load runs in a worker with a hard
timeout, shows elapsed time, can be cancelled (Esc) and shows an error with a
retry key — nothing is allowed to spin forever.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Awaitable, Callable, List, Optional, Sequence, TypeVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.suggester import Suggester
from textual.timer import Timer
from textual.widgets import Button, DataTable, Input, Label, Static, TextArea
from textual.worker import Worker

from ..api.errors import BitbucketError, TimeoutError_
from ..api.models import Page
from ..api.paging import Pager
from ..redact import redact

T = TypeVar("T")

Fetch = Callable[[int, int, str], Awaitable[Page[Any]]]
RowFn = Callable[[Any], Sequence[Any]]


def error_text(exc: BaseException) -> str:
    """User-facing, redacted description of any exception."""
    if isinstance(exc, BitbucketError):
        return redact(exc.describe())
    describe = getattr(exc, "describe", None)
    if callable(describe):
        return redact(str(describe()))
    if isinstance(exc, asyncio.TimeoutError):
        return "Timed out waiting for the server."
    return redact(f"{type(exc).__name__}: {exc}")


async def guarded(coro: Awaitable[T], timeout: float) -> T:
    """Await ``coro`` with an overall deadline mapped to a friendly error."""
    try:
        return await asyncio.wait_for(coro, timeout)
    except asyncio.TimeoutError:
        raise TimeoutError_(f"No complete response after {timeout:.0f}s; gave up.") from None


def ui_timeout(widget: Any) -> float:
    return float(getattr(widget.app, "ui_timeout", 60.0))


class LazyDataTable(DataTable):
    """DataTable that asks its PagedTable for more rows when scrolled near the end
    (mouse wheel, PgDn, Ctrl+End), not only when the cursor moves."""

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if self.max_scroll_y and new_value >= self.max_scroll_y - 3:
            parent = self.parent
            if isinstance(parent, PagedTable):
                parent.load_more()


class PagedTable(Vertical):
    """Lazily-paged, optionally server-side-filtered table."""

    DEFAULT_CSS = """
    PagedTable { height: 1fr; }
    PagedTable > Input { margin: 0 0 0 0; }
    PagedTable > DataTable { height: 1fr; }
    PagedTable > .status { height: auto; max-height: 6; color: $text-muted; padding: 0 1; }
    PagedTable > .status.error { color: $error; }
    PagedTable > .status.loading { color: $warning; }
    """

    BINDINGS = [
        Binding("m", "load_more", "More"),
        Binding("r", "retry", "Reload/Retry"),
    ]

    class Selected(Message):
        def __init__(self, table: "PagedTable", item: Any) -> None:
            super().__init__()
            self.table = table
            self.item = item

        @property
        def control(self) -> "PagedTable":
            return self.table

    class Highlighted(Message):
        # Deliberately NOT a subclass of Selected: Textual dispatches handlers
        # along the message MRO, so subclassing would "open" on highlight.
        def __init__(self, table: "PagedTable", item: Any) -> None:
            super().__init__()
            self.table = table
            self.item = item

        @property
        def control(self) -> "PagedTable":
            return self.table

    def __init__(
        self,
        columns: Sequence[str],
        fetch: Fetch,
        row: RowFn,
        *,
        limit: int = 50,
        filter_placeholder: Optional[str] = None,
        autoload: bool = True,
        empty_text: str = "Nothing here.",
        id: Optional[str] = None,
    ) -> None:
        super().__init__(id=id)
        self.columns = list(columns)
        self._fetch = fetch
        self._row = row
        self.limit = limit
        self.filter_placeholder = filter_placeholder
        self.autoload = autoload
        self.empty_text = empty_text
        self.filter_text = ""
        self.pager: Pager[Any] = self._new_pager()
        self.loading = False
        self.failed = False
        self.loaded_once = False
        self._gen = 0
        self._worker: Optional[Worker] = None
        self._tick_timer: Optional[Timer] = None
        self._filter_timer: Optional[Timer] = None
        self._t0 = 0.0

    def _new_pager(self) -> Pager[Any]:
        flt = self.filter_text
        return Pager(lambda start, limit: self._fetch(start, limit, flt), limit=self.limit)

    def compose(self) -> ComposeResult:
        if self.filter_placeholder:
            yield Input(placeholder=self.filter_placeholder, classes="filter")
        yield LazyDataTable(cursor_type="row", zebra_stripes=True)
        yield Static("", classes="status")

    @property
    def table(self) -> DataTable:
        return self.query_one(DataTable)

    @property
    def status(self) -> Static:
        return self.query_one(".status", Static)

    @property
    def items(self) -> List[Any]:
        return self.pager.items

    def on_mount(self) -> None:
        self.table.add_columns(*self.columns)
        if self.autoload:
            self.reload()

    def ensure_loaded(self) -> None:
        if not self.loaded_once and not self.loading:
            self.reload()

    def set_fetch(self, fetch: Fetch, reload: bool = True) -> None:
        self._fetch = fetch
        if reload:
            self.reload()

    # ----------------------------------------------------------- loading
    def reload(self) -> None:
        self.cancel(silent=True)
        self._gen += 1
        self.pager = self._new_pager()
        self.failed = False
        self.table.clear()
        self.loaded_once = True
        self.load_more()

    def load_more(self) -> None:
        if self.loading or self.pager.exhausted:
            return
        # Set synchronously: several triggers (scroll + cursor) can fire in the
        # same tick, before the worker starts running.
        self.loading = True
        gen = self._gen
        self._worker = self.run_worker(self._load(gen), group="paged", exit_on_error=False)

    async def _load(self, gen: int) -> None:
        pager = self.pager
        self.failed = False
        self._t0 = time.monotonic()
        self._set_status(f"Loading page {pager.pages_loaded + 1}…  (Esc to cancel)", "loading")
        self._tick_timer = self.set_interval(0.25, self._tick)
        try:
            new = await guarded(pager.load_next(), ui_timeout(self))
        except asyncio.CancelledError:
            if gen == self._gen:
                self._set_status("Cancelled. Press r to retry.", "error")
                self.failed = True
            raise
        except Exception as exc:  # noqa: BLE001 - everything must surface
            if gen == self._gen:
                self.failed = True
                self._set_status(f"{error_text(exc)}\nPress r to retry.", "error")
            return
        finally:
            if gen == self._gen:
                self.loading = False
                if self._tick_timer:
                    self._tick_timer.stop()
                    self._tick_timer = None
        if gen != self._gen:
            return
        base = len(pager.items) - len(new)
        table = self.table
        for i, item in enumerate(new):
            table.add_row(*self._row(item), key=str(base + i))
        self._update_done_status()
        if base == 0 and new:
            self.post_message(self.Highlighted(self, new[0]))

    def _tick(self) -> None:
        if self.loading:
            el = time.monotonic() - self._t0
            self._set_status(
                f"Loading page {self.pager.pages_loaded + 1}… {el:.1f}s  (Esc to cancel, gives up after {ui_timeout(self):.0f}s)",
                "loading",
            )

    def _update_done_status(self) -> None:
        n = len(self.pager.items)
        flt = f" matching '{self.filter_text}'" if self.filter_text else ""
        if n == 0:
            self._set_status(self.empty_text + (f" (filter: '{self.filter_text}')" if self.filter_text else ""))
        elif self.pager.exhausted:
            self._set_status(f"{n} item(s){flt}, all loaded.")
        else:
            self._set_status(f"{n} item(s){flt} loaded; more available: scroll down or press m.")

    def _set_status(self, text: str, cls: str = "") -> None:
        st = self.status
        st.update(Text(text))
        st.set_class(cls == "error", "error")
        st.set_class(cls == "loading", "loading")

    def cancel(self, silent: bool = False) -> bool:
        if self.loading and self._worker is not None:
            # Clean up here (the worker may be cancelled before it ever ran) and
            # bump the generation so the cancelled coroutine's handlers are no-ops.
            self._gen += 1
            self.loading = False
            if self._tick_timer:
                self._tick_timer.stop()
                self._tick_timer = None
            self._worker.cancel()
            if not silent:
                self.failed = True
                self._set_status("Cancelled. Press r to retry.", "error")
            return True
        return False

    # ------------------------------------------------------------ actions
    def action_load_more(self) -> None:
        self.load_more()

    def action_retry(self) -> None:
        if self.failed and self.pager.items:
            self.load_more()
        else:
            self.reload()

    def current_item(self) -> Optional[Any]:
        t = self.table
        if t.row_count == 0:
            return None
        idx = t.cursor_row
        if 0 <= idx < len(self.pager.items):
            return self.pager.items[idx]
        return None

    @on(DataTable.RowHighlighted)
    def _highlighted(self, ev: DataTable.RowHighlighted) -> None:
        ev.stop()
        if ev.cursor_row >= self.table.row_count - 3:
            self.load_more()
        item = self.current_item()
        if item is not None:
            self.post_message(self.Highlighted(self, item))

    @on(DataTable.RowSelected)
    def _selected(self, ev: DataTable.RowSelected) -> None:
        ev.stop()
        try:
            item = self.pager.items[int(str(ev.row_key.value))]
        except (ValueError, IndexError, TypeError):
            return
        self.post_message(self.Selected(self, item))

    @on(Input.Changed, ".filter")
    def _filter_changed(self, ev: Input.Changed) -> None:
        ev.stop()
        if self._filter_timer:
            self._filter_timer.stop()
        value = ev.value.strip()
        self._filter_timer = self.set_timer(0.4, lambda: self._apply_filter(value))

    @on(Input.Submitted, ".filter")
    def _filter_submitted(self, ev: Input.Submitted) -> None:
        ev.stop()
        if self._filter_timer:
            self._filter_timer.stop()
        self._apply_filter(ev.value.strip(), force=True)
        self.table.focus()

    def _apply_filter(self, value: str, force: bool = False) -> None:
        if value == self.filter_text and not force:
            return
        self.filter_text = value
        self.reload()

    def focus_table(self) -> None:
        self.table.focus()


class BaseScreen(Screen):
    """Screen with Esc = cancel loading, else go back."""

    BINDINGS = [
        Binding("escape", "back", "Back/Cancel"),
    ]

    def cancel_loading(self) -> bool:
        cancelled = False
        for pt in self.query(PagedTable):
            cancelled = pt.cancel() or cancelled
        for w in list(getattr(self, "_api_workers", [])):
            if w.is_running:
                w.cancel()
                cancelled = True
        return cancelled

    def action_back(self) -> None:
        if self.cancel_loading():
            self.notify("Cancelled.", timeout=2)
            return
        if len(self.app.screen_stack) > 2:
            self.app.pop_screen()

    def run_api(self, coro: Awaitable[Any], group: str = "api", exclusive: bool = True) -> Worker:
        if not hasattr(self, "_api_workers"):
            self._api_workers = []  # type: ignore[attr-defined]
        w = self.run_worker(coro, group=group, exclusive=exclusive, exit_on_error=False)
        self._api_workers = [x for x in self._api_workers if x.is_running] + [w]  # type: ignore[attr-defined]
        return w


class ConfirmScreen(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    ConfirmScreen > Vertical { width: 70; height: auto; border: thick $warning; background: $surface; padding: 1 2; }
    ConfirmScreen Horizontal { height: auto; margin-top: 1; }
    ConfirmScreen Button { margin-right: 2; }
    """
    BINDINGS = [Binding("y", "yes", "Yes"), Binding("n,escape", "no", "No")]

    def __init__(self, message: str, yes_label: str = "Yes", danger: bool = False) -> None:
        super().__init__()
        self.message = message
        self.yes_label = yes_label
        self.danger = danger

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.message)
            with Horizontal():
                yield Button(f"{self.yes_label} (y)", variant="error" if self.danger else "primary", id="yes")
                yield Button("No (n)", id="no")

    @on(Button.Pressed, "#yes")
    def action_yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def action_no(self) -> None:
        self.dismiss(False)


class TextPromptScreen(ModalScreen[Optional[str]]):
    DEFAULT_CSS = """
    TextPromptScreen { align: center middle; }
    TextPromptScreen > Vertical { width: 90; height: 22; border: thick $primary; background: $surface; padding: 1 2; }
    TextPromptScreen TextArea { height: 1fr; }
    TextPromptScreen Horizontal { height: auto; margin-top: 1; }
    TextPromptScreen Button { margin-right: 2; }
    """
    BINDINGS = [Binding("ctrl+s", "submit", "Submit"), Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str) -> None:
        super().__init__()
        self.title_text = title

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.title_text + "  (Ctrl+S to submit, Esc to cancel)")
            yield TextArea(id="text")
            with Horizontal():
                yield Button("Submit", variant="primary", id="submit")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one(TextArea).focus()

    @on(Button.Pressed, "#submit")
    def action_submit(self) -> None:
        text = self.query_one(TextArea).text
        self.dismiss(text if text.strip() else None)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


HELP_TEXT = """\
[b]Global[/b]
  Ctrl+Q        quit                       F1 / ?   this help
  Esc           cancel a running request; otherwise go back
  Tab/Shift+Tab move focus                 Enter    open / select

[b]Lists[/b] (projects, repos, branches, PRs, files, changed files)
  ↑/↓ PgUp/PgDn move (more pages load automatically near the end)
  m             load next page             r        reload / retry after an error
  type in the filter box: server-side filter (Enter to apply immediately)

[b]Projects[/b]   /  global repository search   ~  my personal repositories
[b]Repositories[/b] *  add / remove favorite   Ctrl+T  switch All / Favorites (global search)
[b]Repository[/b] f files   b branches   p pull requests   c clone   Backspace  up a directory
              s  cycle PR state filter (OPEN → MERGED → DECLINED → ALL)
[b]File view[/b]  m  load more lines
[b]Pull request[/b] d diff   M merge   a approve   w needs work   X decline
              n  add comment   c clone source branch   R refresh   m more activity
[b]Diff[/b]       n / p next / previous file   x toggle context lines (3/10)
[b]Clone[/b]      → (right arrow) accepts a path completion suggestion
"""


class HelpScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen > Vertical { width: 92; height: auto; max-height: 90%; border: thick $primary; background: $surface; padding: 1 2; }
    """
    BINDINGS = [Binding("escape,q,f1,question_mark", "close", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(HELP_TEXT)

    def action_close(self) -> None:
        self.dismiss(None)


class PathSuggester(Suggester):
    """Suggests completions for local directory paths (accept with →)."""

    def __init__(self) -> None:
        super().__init__(use_cache=False, case_sensitive=True)

    async def get_suggestion(self, value: str) -> Optional[str]:
        if not value:
            return None
        expanded = os.path.expanduser(value)
        directory, prefix = os.path.split(expanded)
        directory = directory or "."
        if not prefix:
            return None
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return None
        for n in names:
            if n.startswith(prefix) and n != prefix and os.path.isdir(os.path.join(directory, n)):
                if n.startswith(".") and not prefix.startswith("."):
                    continue
                return value + n[len(prefix):] + "/"
        return None


def kv(label: str, value: Any, style: str = "") -> Text:
    t = Text()
    t.append(f"{label}: ", style="bold")
    t.append(str(value), style=style)
    return t


def human_size(n: Optional[int]) -> str:
    if n is None:
        return ""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return str(n)  # pragma: no cover
