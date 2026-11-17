"""Headless TUI smoke tests (Textual Pilot) against the fake Bitbucket server."""

import asyncio

from rich.console import Console
from textual.widgets import Button, Input, RadioButton, Static

from bbtui.config import EnvCredentials, Settings
from bbtui.testing.fake_server import DEMO_TOKEN, DEMO_URL, FakeBitbucket
from bbtui.tui.app import BitbucketApp
from bbtui.tui.browse import FileViewScreen, ProjectsScreen, RepoScreen, ReposScreen
from bbtui.tui.clone import CloneScreen
from bbtui.tui.common import PagedTable
from bbtui.tui.login import LoginScreen
from bbtui.tui.pr import DiffScreen, MergeScreen, PullRequestScreen

SIZE = (140, 45)


def text_of(widget) -> str:
    content = widget.content
    if isinstance(content, str):
        return content
    console = Console(width=200, color_system=None, force_terminal=False)
    with console.capture() as cap:
        console.print(content)
    return cap.get()


async def wait_for(pilot, cond, timeout=5.0, msg="condition"):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        try:
            if cond():
                return
        except Exception:
            pass
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {msg}")


def make_app(fake, secret="", **kw):
    settings = Settings(base_url=DEMO_URL, use_macos_keychain_cas=False, clone_dir="/tmp")
    creds = EnvCredentials(base_url=DEMO_URL, secret=secret, auth_method="bearer") if secret else EnvCredentials()
    return BitbucketApp(settings, env_creds=creds, env={}, transport_factory=fake.transport, ui_timeout=10, **kw)


async def login(app, pilot, token=DEMO_TOKEN):
    app.screen.query_one("#secret", Input).value = token
    await pilot.press("ctrl+s")
    await wait_for(pilot, lambda: isinstance(app.screen, ProjectsScreen), msg="projects screen")
    pt = app.screen.query_one(PagedTable)
    await wait_for(pilot, lambda: pt.table.row_count > 0, msg="projects loaded")
    return pt


async def test_login_validation_and_bad_token(fake):
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        assert isinstance(app.screen, LoginScreen)
        status = app.screen.query_one("#status", Static)
        # invalid URL
        app.screen.query_one("#url", Input).value = "bitbucket.example.test"
        app.screen.query_one("#secret", Input).value = "x" * 10
        await pilot.press("ctrl+s")
        await wait_for(pilot, lambda: "https://" in text_of(status), msg="url validation error")
        # wrong token -> 401 shown, stays on login
        app.screen.query_one("#url", Input).value = DEMO_URL + "/"
        app.screen.query_one("#secret", Input).value = "wrong-token-123"
        await pilot.press("ctrl+s")
        await wait_for(pilot, lambda: "401" in text_of(status), msg="401 error")
        assert "Connection failed" in text_of(status)
        assert isinstance(app.screen, LoginScreen)
        # the secret input is masked
        assert app.screen.query_one("#secret", Input).password


async def test_basic_requires_username(fake):
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        app.screen.query_one("#m-basic", RadioButton).value = True
        app.screen.query_one("#username", Input).value = ""
        app.screen.query_one("#secret", Input).value = DEMO_TOKEN
        await pilot.press("ctrl+s")
        status = app.screen.query_one("#status", Static)
        await wait_for(pilot, lambda: "username is required" in text_of(status).lower())
        app.screen.query_one("#username", Input).value = "alice"
        await pilot.press("ctrl+s")
        await wait_for(pilot, lambda: isinstance(app.screen, ProjectsScreen))


async def test_login_browse_paging_filter(fake):
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        pt = await login(app, pilot)
        assert pt.table.row_count == 50 and not pt.pager.exhausted
        assert app.server.username == "alice"
        # lazy paging: moving to the end loads the next page, not everything
        pt.table.focus()
        await pilot.press("ctrl+end")
        await wait_for(pilot, lambda: pt.table.row_count == 100, msg="second page")
        assert not pt.pager.exhausted
        # explicit 'm'
        await pilot.press("m")
        await wait_for(pilot, lambda: pt.pager.exhausted and pt.table.row_count == 123, msg="all pages")
        # server-side filter
        flt = pt.query_one(".filter", Input)
        flt.focus()
        await pilot.press(*"Mobile")
        await pilot.press("enter")
        await wait_for(pilot, lambda: pt.table.row_count == 1, msg="filtered")
        assert any(r.url.params.get("name") == "Mobile" for r in fake.requests)
        assert pt.items[0].key == "MOB"


async def test_browse_repo_files_branches_and_file_view(fake):
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        pt = await login(app, pilot)
        # PRJ is the first project
        pt.table.focus()
        await pilot.press("home", "enter")
        await wait_for(pilot, lambda: isinstance(app.screen, ReposScreen))
        rpt = app.screen.query_one(PagedTable)
        await wait_for(pilot, lambda: rpt.table.row_count == 2)
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, RepoScreen))
        repo_screen = app.screen
        files = repo_screen.query_one("#files-table", PagedTable)
        await wait_for(pilot, lambda: files.table.row_count > 0, msg="files")
        names = [e.name for e in files.items]
        assert names[0] == "src" and "README.md" in names
        # enter src directory
        await pilot.press("enter")
        await wait_for(pilot, lambda: files.items and files.items[0].name == "..", msg="src dir")
        assert {e.name for e in files.items} == {"..", "util", "app.py"}
        # open app.py
        idx = [e.name for e in files.items].index("app.py")
        files.table.move_cursor(row=idx)
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, FileViewScreen))
        code = app.screen.query_one("#code", Static)
        await wait_for(pilot, lambda: "def main" in text_of(code), msg="file content")
        await pilot.press("escape")
        await wait_for(pilot, lambda: app.screen is repo_screen)
        # backspace goes up
        files.table.focus()
        await pilot.press("backspace")
        await wait_for(pilot, lambda: files.items and files.items[0].name == "src", msg="back at root")
        # branches tab, choose develop -> files on develop
        await pilot.press("b")
        br = repo_screen.query_one("#branches-table", PagedTable)
        await wait_for(pilot, lambda: br.table.row_count == 3, msg="branches")
        br.table.focus()
        br.table.move_cursor(row=1)
        await pilot.press("enter")
        await wait_for(pilot, lambda: any(e.name == "NEW_ON_DEVELOP.md" for e in files.items), msg="develop files")
        assert any(r.url.params.get("at") == "refs/heads/develop" for r in fake.requests)


async def test_large_file_paged_view(fake):
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        await login(app, pilot)
        repo = (await app.client.search_repos(name="alpha")).values[0]
        app.push_screen(FileViewScreen(repo, "big.txt", "", "default"))
        await pilot.pause()
        scr = app.screen
        await wait_for(pilot, lambda: len(scr.lines) == 1000)
        assert scr.next_start == 1000
        await pilot.press("m")
        await wait_for(pilot, lambda: len(scr.lines) == 2000)
        await pilot.press("m")
        await wait_for(pilot, lambda: len(scr.lines) == 2500 and scr.next_start is None)


async def _open_pr(app, pilot, pr_id):
    repo = (await app.client.search_repos(name="alpha")).values[0]
    pr = await app.client.get_pull_request("PRJ", "alpha", pr_id)
    app.push_screen(PullRequestScreen(repo, pr))
    await pilot.pause()
    scr = app.screen
    await wait_for(pilot, lambda: scr.check is not None or scr.check_error, msg="merge check")
    return scr


async def test_pr_list_states_and_detail(fake):
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        await login(app, pilot)
        repo = (await app.client.search_repos(name="alpha")).values[0]
        app.push_screen(RepoScreen(repo))
        await pilot.pause()
        scr = app.screen
        await pilot.press("p")
        prs = scr.query_one("#prs-table", PagedTable)
        await wait_for(pilot, lambda: prs.table.row_count == 2, msg="open PRs")
        await pilot.press("s")  # -> MERGED
        await wait_for(pilot, lambda: prs.items and prs.items[0].state == "MERGED", msg="merged PRs")
        assert prs.table.row_count == 25 and not prs.pager.exhausted
        app.pop_screen()
        await pilot.pause()
        pr_screen = await _open_pr(app, pilot, 1)
        overview = text_of(pr_screen.query_one("#overview-body", Static))
        assert "Add feature X" in overview and "feature/x" in overview and "Carol Chen" in overview
        assert "APPROVED" in overview
        assert "can merge" in text_of(pr_screen.query_one("#merge-check", Static))
        await wait_for(pilot, lambda: "Looks good overall." in text_of(pr_screen.query_one("#activity-body", Static)))


async def test_pr_diff_view(fake):
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        await login(app, pilot)
        pr_screen = await _open_pr(app, pilot, 1)
        await pilot.press("d")
        await wait_for(pilot, lambda: isinstance(app.screen, DiffScreen))
        diff_screen = app.screen
        changes = diff_screen.query_one(PagedTable)
        await wait_for(pilot, lambda: changes.table.row_count == 4, msg="changes")
        diff = diff_screen.query_one("#diff", Static)
        await wait_for(pilot, lambda: "+def main(argv=None):" in text_of(diff), msg="first diff")
        assert "-def main():" in text_of(diff)
        await pilot.press("n")
        await wait_for(pilot, lambda: "+++ b/docs/new.md" in text_of(diff), msg="second diff")
        await pilot.press("n", "n")
        await wait_for(pilot, lambda: "Binary file changed" in text_of(diff), msg="binary diff")


async def test_merge_flow_out_of_date_then_success(fake):
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        await login(app, pilot)
        pr_screen = await _open_pr(app, pilot, 1)
        # someone else updates the PR after we loaded it
        fake.prs["PRJ/alpha"][0]["version"] = 7
        await pilot.press("M")
        await wait_for(pilot, lambda: isinstance(app.screen, MergeScreen))
        ms = app.screen
        btn = ms.query_one("#m-merge", Button)
        await wait_for(pilot, lambda: not btn.disabled, msg="merge enabled")
        # MergeScreen refreshed PR on open, so to exercise 409 make it stale again
        fake.prs["PRJ/alpha"][0]["version"] = 8
        await pilot.click("#m-merge")
        result = ms.query_one("#m-result", Static)
        await wait_for(pilot, lambda: "updated by someone else" in text_of(result), msg="409 out-of-date")
        assert "current 8" in text_of(result)
        await wait_for(pilot, lambda: ms.pr.version == 8 and not btn.disabled, msg="refreshed")
        # strategies shown, default selected
        from textual.widgets import Select
        sel = ms.query_one("#m-strategy", Select)
        assert sel.display and sel.value == "no-ff"
        await pilot.pause(0.3)  # let the layout settle (result text grew) before clicking
        await pilot.click("#m-merge")
        await wait_for(pilot, lambda: "Merged" in text_of(result), msg="merged")
        assert fake.merge_requests[-1]["query"]["version"] == "8"
        assert fake.prs["PRJ/alpha"][0]["state"] == "MERGED"
        await pilot.click("#m-close")
        await wait_for(pilot, lambda: app.screen is pr_screen and pr_screen.pr.state == "MERGED", msg="pr refreshed")


async def test_merge_vetoed_pr_cannot_merge(fake):
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        await login(app, pilot)
        pr_screen = await _open_pr(app, pilot, 2)
        check = text_of(pr_screen.query_one("#merge-check", Static))
        assert "cannot merge" in check and "Requires 2 approvals" in check and "conflicts" in check
        await pilot.press("M")
        await wait_for(pilot, lambda: isinstance(app.screen, MergeScreen))
        ms = app.screen
        result = ms.query_one("#m-result", Static)
        await wait_for(pilot, lambda: "cannot be merged" in text_of(result))
        assert ms.query_one("#m-merge", Button).disabled
        assert fake.merge_requests == []


async def test_approve_and_comment(fake):
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        await login(app, pilot)
        pr_screen = await _open_pr(app, pilot, 1)
        await pilot.press("a")
        await wait_for(pilot, lambda: {r.user.name: r.status for r in pr_screen.pr.reviewers}["alice"] == "APPROVED")
        await pilot.press("n")
        await pilot.pause()
        from textual.widgets import TextArea
        app.screen.query_one(TextArea).insert("LGTM from the TUI")
        await pilot.press("ctrl+s")
        await wait_for(pilot, lambda: fake.comments_posted == ["LGTM from the TUI"])


async def test_escape_cancels_slow_request(fake):
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        await login(app, pilot)
        gate = asyncio.Event()
        real = app.client.search_repos

        async def slow(*a, **k):
            await gate.wait()
            return await real(*a, **k)

        app.client.search_repos = slow
        await pilot.press("slash")
        await wait_for(pilot, lambda: isinstance(app.screen, ReposScreen))
        pt = app.screen.query_one(PagedTable)
        await wait_for(pilot, lambda: pt.loading, msg="loading")
        assert "Loading" in text_of(pt.status)
        pt.table.focus()
        await pilot.press("escape")
        await wait_for(pilot, lambda: not pt.loading and "Cancelled" in text_of(pt.status), msg="cancelled")
        assert isinstance(app.screen, ReposScreen)  # first Esc cancels, does not navigate
        app.client.search_repos = real
        await pilot.press("r")
        await wait_for(pilot, lambda: pt.table.row_count > 0, msg="retry worked")


async def test_ui_timeout_surfaces_error(fake):
    app = make_app(fake)
    app.ui_timeout = 0.5
    async with app.run_test(size=SIZE) as pilot:
        await login(app, pilot)

        async def hang(*a, **k):
            await asyncio.sleep(30)

        app.client.list_project_repos = hang
        app.push_screen(ReposScreen(app.screen.query_one(PagedTable).items[0]))
        await pilot.pause()
        pt = app.screen.query_one(PagedTable)
        await wait_for(pilot, lambda: "gave up" in text_of(pt.status), timeout=5, msg="timeout error")
        assert "retry" in text_of(pt.status)


async def test_clone_screen_refuses_nonempty_and_shows_ssh_hint(fake, tmp_path, monkeypatch):
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        await login(app, pilot)
        repo = (await app.client.search_repos(name="alpha")).values[0]
        app.push_screen(CloneScreen(repo, branch="feature/x"))
        await pilot.pause()
        scr = app.screen
        assert scr.query_one("#branch", Input).value == "feature/x"
        full = tmp_path / "full"
        full.mkdir()
        (full / "x").write_text("x")
        scr.query_one("#dest", Input).value = str(full)
        await pilot.press("ctrl+s")
        result = scr.query_one("#result", Static)
        await wait_for(pilot, lambda: "refusing" in text_of(result))
        # SSH with unreachable port -> hint to use HTTPS, git never runs
        import bbtui.tui.clone as clone_mod

        async def unreachable(host, port, timeout=5.0):
            return f"Timed out connecting to {host}:{port} after 6s"

        async def resolve(host, port):
            return host, port

        monkeypatch.setattr(clone_mod, "tcp_preflight", unreachable)
        monkeypatch.setattr(clone_mod, "resolve_ssh_target", resolve)
        scr.query_one("#proto-ssh", RadioButton).value = True
        scr.query_one("#dest", Input).value = str(tmp_path / "new-clone")
        await pilot.press("ctrl+s")
        await wait_for(pilot, lambda: "7999" in text_of(result) and "HTTPS" in text_of(result), msg="ssh hint")
        assert not (tmp_path / "new-clone").exists()


async def test_personal_repos_and_demo_autoconnect():
    fake = FakeBitbucket()
    app = make_app(fake, secret=DEMO_TOKEN, auto_connect=True)
    async with app.run_test(size=SIZE) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, ProjectsScreen), msg="auto connect")
        await pilot.press("tilde")
        await wait_for(pilot, lambda: isinstance(app.screen, ReposScreen))
        pt = app.screen.query_one(PagedTable)
        await wait_for(pilot, lambda: pt.table.row_count == 1)
        assert pt.items[0].slug == "scratch"
        # Esc navigates back to projects, then to login (which ends the session)
        await pilot.press("escape")
        await wait_for(pilot, lambda: isinstance(app.screen, ProjectsScreen))
        await pilot.press("escape")
        await wait_for(pilot, lambda: isinstance(app.screen, LoginScreen) and app.client is None, msg="logged out")


# Re-use the local authenticated git HTTP server fixture from test_clone.
from test_clone import TOKEN as CLONE_TOKEN, git_http_server  # noqa: E402,F401


async def test_clone_screen_end_to_end_over_http(tmp_path, git_http_server):
    url, _auth, handler = git_http_server
    fake = FakeBitbucket(token=CLONE_TOKEN)
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        await login(app, pilot, token=CLONE_TOKEN)
        repo = (await app.client.search_repos(name="alpha")).values[0]
        repo.clone_links = {"http": url}  # point the clone at the local test server
        app.push_screen(CloneScreen(repo, branch="feature"))
        await pilot.pause()
        scr = app.screen
        dest = tmp_path / "newparent" / "cloned"
        scr.query_one("#dest", Input).value = str(dest)
        await pilot.press("ctrl+s")
        # parent does not exist -> confirmation dialog
        from bbtui.tui.common import ConfirmScreen
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen), msg="confirm create parent")
        await pilot.press("y")
        result = scr.query_one("#result", Static)
        await wait_for(pilot, lambda: "Cloned into" in text_of(result), timeout=30, msg="clone finished")
        assert "No credentials were stored" in text_of(result)
        assert (dest / "README.md").exists()
        cfg = (dest / ".git" / "config").read_text()
        assert CLONE_TOKEN not in cfg and "extraheader" not in cfg.lower()
        assert f"Bearer {CLONE_TOKEN}" in handler.seen_auth
