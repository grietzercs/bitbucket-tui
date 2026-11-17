import base64

import httpx
import pytest
import respx

from bbtui.api import (
    AuthenticationError,
    BasicAuth,
    BearerTokenAuth,
    BitbucketClient,
    ConfigError,
    ConflictError,
    NetworkError,
    NotFoundError,
    Pager,
    PermissionDeniedError,
    TimeoutError_,
    TLSError,
    UnexpectedResponseError,
    make_auth,
    normalize_base_url,
)
from bbtui.api.errors import RateLimitedError, ServerError
from bbtui.testing.fake_server import DEMO_TOKEN, DEMO_URL, FakeBitbucket  # noqa: F401

BASE = "https://bb.example.test"


# ----------------------------------------------------------------- URL / auth
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://bb.example.test/", "https://bb.example.test"),
        ("  https://BB.Example.Test///  ", "https://bb.example.test"),
        ("https://bb.example.test/rest/api/1.0/projects", "https://bb.example.test"),
        ("https://bb.example.test/bitbucket/", "https://bb.example.test/bitbucket"),
        ("http://bb.example.test:7990", "http://bb.example.test:7990"),
        ("https://bb.example.test/?foo=1#x", "https://bb.example.test"),
    ],
)
def test_normalize_base_url(raw, expected):
    assert normalize_base_url(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "bb.example.test", "ftp://bb.example.test", "https://", "https://user:pw@bb.example.test"]
)
def test_normalize_base_url_rejects(raw):
    with pytest.raises(ConfigError):
        normalize_base_url(raw)


def test_bearer_header_and_repr():
    a = BearerTokenAuth("tok-123456")
    assert a.headers() == {"Authorization": "Bearer tok-123456"}
    assert "tok-123456" not in repr(a) and "tok-123456" not in str(a)


def test_basic_header():
    a = BasicAuth("alice", "p@ss:word")
    expected = "Basic " + base64.b64encode(b"alice:p@ss:word").decode()
    assert a.authorization_header() == expected
    assert "p@ss" not in repr(a)


def test_make_auth_validation():
    assert isinstance(make_auth("bearer", secret="x" * 10), BearerTokenAuth)
    assert isinstance(make_auth("basic", username="u", secret="pw12"), BasicAuth)
    with pytest.raises(ValueError):
        make_auth("basic", username="", secret="pw")
    with pytest.raises(ValueError):
        make_auth("bearer", secret="")
    with pytest.raises(ValueError):
        make_auth("kerberos", secret="x")


@respx.mock
async def test_auth_header_sent_bearer_and_basic():
    route = respx.get(f"{BASE}/rest/api/1.0/projects").mock(
        return_value=httpx.Response(200, json={"values": [], "isLastPage": True})
    )
    async with BitbucketClient(BASE, BearerTokenAuth("tok-abcdef")) as c:
        await c.list_projects()
    assert route.calls.last.request.headers["authorization"] == "Bearer tok-abcdef"
    async with BitbucketClient(BASE, BasicAuth("bob", "tok-abcdef")) as c:
        await c.list_projects()
    assert route.calls.last.request.headers["authorization"].startswith("Basic ")


# ------------------------------------------------------------- connection
async def test_test_connection_bearer(client):
    info = await client.test_connection()
    assert info.version == "8.19.1"
    assert info.username == "alice"
    assert info.user is not None and info.user.slug == "alice"
    assert info.warnings == []


async def test_test_connection_basic_with_pat(fake):
    async with BitbucketClient(DEMO_URL, BasicAuth("alice", DEMO_TOKEN), transport=fake.transport()) as c:
        info = await c.test_connection()
    assert info.username == "alice"


async def test_test_connection_bad_token(fake):
    async with BitbucketClient(DEMO_URL, BearerTokenAuth("wrong-token"), transport=fake.transport()) as c:
        with pytest.raises(AuthenticationError) as ei:
            await c.test_connection()
    assert ei.value.status == 401
    assert "Authentication failed" in ei.value.message
    assert ei.value.hint


async def test_test_connection_not_bitbucket():
    def handler(request):
        return httpx.Response(200, text="<html>login</html>", headers={"content-type": "text/html"})

    async with BitbucketClient(BASE, BearerTokenAuth("t" * 10), transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(UnexpectedResponseError) as ei:
            await c.test_connection()
    assert "does not look like" in ei.value.message


async def test_redirect_is_reported_not_followed():
    def handler(request):
        return httpx.Response(302, headers={"location": "https://sso.example.test/login"})

    async with BitbucketClient(BASE, BearerTokenAuth("t" * 10), transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(UnexpectedResponseError) as ei:
            await c.list_projects()
    assert "sso.example.test" in ei.value.message


# ---------------------------------------------------------- error mapping
@pytest.mark.parametrize(
    "status,exc",
    [(401, AuthenticationError), (403, PermissionDeniedError), (404, NotFoundError),
     (409, ConflictError), (429, RateLimitedError), (500, ServerError)],
)
async def test_error_mapping(status, exc):
    body = {"errors": [{"message": f"boom {status}", "exceptionName": "x.Y"}]}
    transport = httpx.MockTransport(lambda r: httpx.Response(status, json=body))
    async with BitbucketClient(BASE, BearerTokenAuth("t" * 10), transport=transport, max_retries=0) as c:
        with pytest.raises(exc) as ei:
            await c.get_json("/rest/api/1.0/projects")
    assert ei.value.status == status
    assert f"boom {status}" in ei.value.message


async def test_timeout_maps_to_timeout_error():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    async with BitbucketClient(BASE, BearerTokenAuth("t" * 10), transport=httpx.MockTransport(handler), backoff=0) as c:
        with pytest.raises(TimeoutError_):
            await c.list_projects()


async def test_tls_failure_maps_and_is_not_retried():
    import ssl

    calls = []

    def handler(request):
        calls.append(1)
        try:
            raise ssl.SSLCertVerificationError("certificate verify failed: unable to get local issuer certificate")
        except ssl.SSLError as e:
            raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]", request=request) from e

    async with BitbucketClient(BASE, BearerTokenAuth("t" * 10), transport=httpx.MockTransport(handler), backoff=0) as c:
        with pytest.raises(TLSError) as ei:
            await c.list_projects()
    assert len(calls) == 1
    assert "CA" in ei.value.hint


async def test_connect_error_maps_to_network_error():
    def handler(request):
        raise httpx.ConnectError("Name or service not known", request=request)

    async with BitbucketClient(BASE, BearerTokenAuth("t" * 10), transport=httpx.MockTransport(handler), backoff=0) as c:
        with pytest.raises(NetworkError):
            await c.list_projects()


# ----------------------------------------------------------------- retries
async def test_get_retried_on_503_then_succeeds():
    responses = [httpx.Response(503), httpx.Response(502), httpx.Response(200, json={"values": [], "isLastPage": True})]
    calls = []

    def handler(request):
        calls.append(request)
        return responses[len(calls) - 1]

    async with BitbucketClient(BASE, BearerTokenAuth("t" * 10), transport=httpx.MockTransport(handler), backoff=0, max_retries=2) as c:
        page = await c.list_projects()
    assert len(calls) == 3
    assert page.values == []


async def test_get_retries_are_bounded():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectTimeout("nope", request=request)

    async with BitbucketClient(BASE, BearerTokenAuth("t" * 10), transport=httpx.MockTransport(handler), backoff=0, max_retries=2) as c:
        with pytest.raises(TimeoutError_):
            await c.list_projects()
    assert len(calls) == 3


async def test_post_is_never_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    async with BitbucketClient(BASE, BearerTokenAuth("t" * 10), transport=httpx.MockTransport(handler), backoff=0, max_retries=3) as c:
        with pytest.raises(ServerError):
            await c.merge("PRJ", "alpha", 1, version=0)
    assert len(calls) == 1
    assert calls[0].headers["x-atlassian-token"] == "no-check"


# ------------------------------------------------------------------ paging
async def test_projects_paging_with_pager(client, fake):
    pager = Pager(lambda start, limit: client.list_projects(start=start, limit=limit), limit=50)
    first = await pager.load_next()
    assert len(first) == 50 and not pager.exhausted and pager.next_start == 50
    await pager.load_next()
    await pager.load_next()
    assert pager.exhausted
    assert len(pager.items) == len(fake.projects) == 123
    assert await pager.load_next() == []
    starts = [r.url.params.get("start") for r in fake.requests if r.url.path.endswith("/projects")]
    assert starts == ["0", "50", "100"]


async def test_iter_all_bounded(client):
    items = [r async for r in client.iter_all("/rest/api/1.0/projects/BIG/repos", lambda d: d["slug"], limit=40)]
    assert len(items) == 130
    capped = [r async for r in client.iter_all("/rest/api/1.0/repos", lambda d: d, limit=10, max_items=15)]
    assert len(capped) == 15


async def test_pager_stops_on_non_advancing_cursor():
    from bbtui.api.models import Page

    async def fetch(start, limit):
        return Page(values=[start], start=start, is_last_page=False, next_page_start=start)

    pager = Pager(fetch)
    await pager.load_next()
    assert pager.exhausted


def test_page_from_json_defensive():
    from bbtui.api.models import Page

    p = Page.from_json({"values": [1, 2], "start": 10, "isLastPage": False}, lambda x: x)
    assert p.next_page_start == 12
    empty = Page.from_json({"values": [], "isLastPage": False, "nextPageStart": 5}, lambda x: x)
    assert empty.is_last_page and empty.next_page_start is None


async def test_server_side_filters(client, fake):
    page = await client.list_projects(name="mobile")
    assert [p.key for p in page.values] == ["MOB"]
    repos = await client.search_repos(name="alpha")
    assert [r.slug for r in repos.values] == ["alpha"]
    assert repos.values[0].clone_links["http"].endswith("/scm/prj/alpha.git")
    assert fake.requests[-1].url.params["name"] == "alpha"
    br = await client.list_branches("PRJ", "alpha", filter_text="feat")
    assert [b.display_id for b in br.values] == ["feature/x"]


# ------------------------------------------------------------------ browse
async def test_browse_dir_and_file(client):
    root = await client.browse_dir("PRJ", "alpha")
    names = [(e.name, e.type) for e in root.children.values]
    assert ("src", "DIRECTORY") in names and ("README.md", "FILE") in names
    sub = await client.browse_dir("PRJ", "alpha", "src")
    assert {e.path for e in sub.children.values} == {"src/app.py", "src/util"}
    f = await client.browse_file("PRJ", "alpha", "src/app.py")
    assert f.lines[2] == "def main():" and f.is_last_page
    dev = await client.browse_dir("PRJ", "alpha", at="refs/heads/develop")
    assert "NEW_ON_DEVELOP.md" in [e.name for e in dev.children.values]


async def test_browse_file_paging(client):
    p1 = await client.browse_file("PRJ", "alpha", "big.txt", limit=1000)
    assert len(p1.lines) == 1000 and not p1.is_last_page and p1.next_page_start == 1000
    p3 = await client.browse_file("PRJ", "alpha", "big.txt", start=2000, limit=1000)
    assert len(p3.lines) == 500 and p3.is_last_page and p3.lines[-1] == "line 2500"


async def test_browse_binary_and_missing(client):
    b = await client.browse_file("PRJ", "alpha", "logo.png")
    assert b.binary
    with pytest.raises(NotFoundError):
        await client.browse_file("PRJ", "alpha", "nope.txt")


async def test_path_encoding(client, fake):
    with pytest.raises(NotFoundError):
        await client.browse_file("PRJ", "alpha", "dir with space/a#b?.txt")
    raw = fake.requests[-1].url.raw_path.decode()
    assert "dir%20with%20space/a%23b%3F.txt" in raw


# ----------------------------------------------------------- pull requests
async def test_list_prs_by_state(client):
    open_ = await client.list_pull_requests("PRJ", "alpha", "OPEN")
    assert [p.id for p in open_.values] == [1, 2]
    merged = await client.list_pull_requests("PRJ", "alpha", "MERGED", limit=25)
    assert not merged.is_last_page
    pr = open_.values[0]
    assert pr.from_ref.display_id == "feature/x" and pr.to_ref.display_id == "master"
    assert {r.user.name: r.status for r in pr.reviewers} == {"alice": "UNAPPROVED", "carol": "APPROVED"}
    with pytest.raises(ConfigError):
        await client.list_pull_requests("PRJ", "alpha", "BOGUS")


async def test_merge_check_and_vetoes(client):
    ok = await client.merge_check("PRJ", "alpha", 1)
    assert ok.can_merge and ok.outcome == "CLEAN"
    bad = await client.merge_check("PRJ", "alpha", 2)
    assert not bad.can_merge and bad.conflicted and bad.vetoes[0].summary == "Requires 2 approvals"


async def test_merge_config(client):
    cfg = await client.merge_config("PRJ", "alpha")
    assert cfg.default_strategy_id == "no-ff"
    assert [s.id for s in cfg.enabled_strategies] == ["no-ff", "squash"]


async def test_merge_requires_current_version(client, fake):
    with pytest.raises(ConflictError) as ei:
        await client.merge("PRJ", "alpha", 1, version=1)
    assert ei.value.is_out_of_date and ei.value.current_version == 2
    assert "Refresh" in ei.value.hint
    merged = await client.merge("PRJ", "alpha", 1, version=2, strategy_id="squash", message="msg")
    assert merged.state == "MERGED" and merged.version == 3
    sent = fake.merge_requests[-1]
    assert sent["query"]["version"] == "2"
    assert sent["body"] == {"version": 2, "strategyId": "squash", "message": "msg"}


async def test_merge_vetoed(client):
    with pytest.raises(ConflictError) as ei:
        await client.merge("PRJ", "alpha", 2, version=0)
    err = ei.value
    assert not err.is_out_of_date and err.conflicted
    assert [v.summary for v in err.vetoes] == ["Requires 2 approvals"]
    assert "vetoed" in err.hint


async def test_activities_changes_diff(client):
    acts = await client.list_activities("PRJ", "alpha", 1)
    actions = [a.action for a in acts.values]
    assert actions == ["APPROVED", "COMMENTED", "COMMENTED", "OPENED"]
    assert acts.values[1].comment.replies[0].text == "Thanks!"
    assert acts.values[2].anchor_path == "src/app.py"
    changes = await client.list_changes("PRJ", "alpha", 1)
    assert [(c.type, c.path) for c in changes.values][:3] == [("MODIFY", "src/app.py"), ("ADD", "docs/new.md"), ("DELETE", "old.txt")]
    d = await client.pr_diff("PRJ", "alpha", 1, "src/app.py")
    kinds = [l.kind for l in d.diffs[0].hunks[0].lines]
    assert kinds == ["CONTEXT", "CONTEXT", "REMOVED", "ADDED", "ADDED", "CONTEXT"]
    whole = await client.pr_diff("PRJ", "alpha", 1)
    assert len(whole.diffs) == 4 and whole.diffs[3].binary


async def test_approve_decline_comment(client, fake):
    await client.set_review_status("PRJ", "alpha", 1, "alice", "APPROVED")
    pr = await client.get_pull_request("PRJ", "alpha", 1)
    assert {r.user.name: r.status for r in pr.reviewers}["alice"] == "APPROVED"
    await client.add_comment("PRJ", "alpha", 1, "hello there")
    assert fake.comments_posted == ["hello there"]
    with pytest.raises(ConfigError):
        await client.add_comment("PRJ", "alpha", 1, "   ")
    with pytest.raises(ConflictError):
        await client.decline("PRJ", "alpha", 2, version=99)
    declined = await client.decline("PRJ", "alpha", 2, version=0)
    assert declined.state == "DECLINED"


async def test_personal_repos(client, fake):
    page = await client.list_project_repos("~alice")
    assert [r.slug for r in page.values] == ["scratch"]
    assert fake.requests[-1].url.raw_path.decode().startswith("/rest/api/1.0/projects/~alice/repos")


async def test_non_json_success_body():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text="<html>hi</html>", headers={"content-type": "text/html"}))
    async with BitbucketClient(BASE, BearerTokenAuth("t" * 10), transport=transport) as c:
        with pytest.raises(UnexpectedResponseError) as ei:
            await c.list_projects()
    assert "text/html" in ei.value.message


async def test_merge_config_unavailable_is_not_fatal(client, fake):
    # Bitbucket frequently returns 401 (AuthorisationException) for missing repo-admin rights.
    fake.fail_next.append((r"/settings/pull-requests$", 401))
    assert await client.merge_config("PRJ", "alpha") is None
    fake.fail_next.append((r"/settings/pull-requests$", 403))
    assert await client.merge_config("PRJ", "alpha") is None
    assert (await client.merge_config("PRJ", "alpha")).default_strategy_id == "no-ff"


async def test_401_authorisation_exception_is_permission_error():
    body = {"errors": [{"message": "You are not permitted to access this resource",
                        "exceptionName": "com.atlassian.bitbucket.AuthorisationException"}]}
    transport = httpx.MockTransport(lambda r: httpx.Response(401, json=body))
    async with BitbucketClient(BASE, BearerTokenAuth("t" * 10), transport=transport) as c:
        with pytest.raises(PermissionDeniedError) as ei:
            await c.list_projects()
        assert ei.value.status == 401 and "not permitted" in ei.value.hint
        # ...but during login it is reported as an authentication failure
        def handler(request):
            if request.url.path.endswith("application-properties"):
                return httpx.Response(200, json={"version": "8.0"})
            return httpx.Response(401, json=body)
    async with BitbucketClient(BASE, BearerTokenAuth("t" * 10), transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(AuthenticationError):
            await c.test_connection()
