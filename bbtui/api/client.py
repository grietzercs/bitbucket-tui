"""Async Bitbucket Server / Data Center REST 1.0 client.

Design goals (the opposite of an infinitely spinning UI):
* every request has connect/read/write/pool timeouts;
* idempotent GETs are retried a small, bounded number of times with
  exponential backoff on transport errors and 429/502/503/504; non-idempotent
  requests (POST/PUT/DELETE) are never retried automatically;
* all failures are mapped to typed :mod:`errors` with user-facing hints;
* paging is explicit (one page per call) so callers load lazily.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Callable, Dict, Optional, TypeVar
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from .. import __version__
from ..redact import redact, redact_headers
from .auth import AuthStrategy
from .errors import (
    AuthenticationError,
    BitbucketError,
    ConfigError,
    NotFoundError,
    PermissionDeniedError,
    ServerError,
    TLSError,
    UnexpectedResponseError,
    error_from_response,
    error_from_transport,
)
from .models import (
    Activity,
    Branch,
    BrowseEntry,
    Change,
    DiffResult,
    DirectoryPage,
    FilePage,
    MergeCheck,
    MergeConfig,
    Page,
    Project,
    PullRequest,
    Repository,
    ServerInfo,
    User,
)
from .tls import TLSOptions, build_verify

log = logging.getLogger("bbtui.http")

T = TypeVar("T")
API = "/rest/api/1.0"
RETRY_STATUSES = {429, 502, 503, 504}
PR_STATES = ("OPEN", "MERGED", "DECLINED", "ALL")


def normalize_base_url(url: str) -> str:
    """Validate and normalise a Bitbucket base URL.

    * strips whitespace, query, fragment and trailing slashes;
    * strips a pasted ``/rest/api/1.0`` (or deeper REST path) suffix;
    * requires an http/https scheme and a host; rejects embedded credentials.
    """
    raw = (url or "").strip()
    if not raw:
        raise ConfigError("Server URL is required")
    if "://" not in raw:
        raise ConfigError(
            f"URL must start with https:// or http:// (got {raw!r})",
            hint="Example: https://bitbucket.example.com",
        )
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ConfigError(f"Unsupported URL scheme {parts.scheme!r}; use https:// or http://")
    if not parts.hostname:
        raise ConfigError("URL has no host name")
    if parts.username or parts.password:
        raise ConfigError(
            "Do not put credentials in the URL",
            hint="Enter the username/token in the credential fields instead.",
        )
    path = parts.path.rstrip("/")
    idx = path.find("/rest/")
    if idx >= 0:
        path = path[:idx]
    netloc = parts.hostname.lower()
    if ":" in netloc:  # IPv6 literal
        netloc = f"[{netloc}]"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((scheme, netloc, path, "", ""))


def enc(segment: str) -> str:
    """URL-encode a single path segment (project key, slug, etc.)."""
    return quote(str(segment), safe="~")


def enc_path(path: str) -> str:
    """URL-encode a repository file path, keeping '/' separators."""
    return "/".join(quote(c, safe="") for c in path.strip("/").split("/") if c)


class BitbucketClient:
    def __init__(
        self,
        base_url: str,
        auth: Optional[AuthStrategy],
        *,
        tls: Optional[TLSOptions] = None,
        timeout: float = 30.0,
        connect_timeout: float = 10.0,
        max_retries: int = 2,
        backoff: float = 0.5,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.auth = auth
        self.tls = tls or TLSOptions()
        self.max_retries = max(0, int(max_retries))
        self.backoff = backoff
        self.server_username: str = ""
        self.timeout = httpx.Timeout(timeout, connect=connect_timeout)
        verify = build_verify(self.tls) if transport is None else True
        headers = {
            "Accept": "application/json",
            "User-Agent": f"bitbucket-tui/{__version__}",
        }
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=self.timeout,
            verify=verify,
            follow_redirects=False,
            transport=transport,
            trust_env=True,
        )

    # ------------------------------------------------------------------ core
    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "BitbucketClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    def auth_headers(self) -> Dict[str, str]:
        return self.auth.headers() if self.auth else {}

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Any = None,
        retry: Optional[bool] = None,
    ) -> httpx.Response:
        method = method.upper()
        idempotent = method in ("GET", "HEAD") if retry is None else retry
        url = path if path.startswith("/") else "/" + path
        headers = dict(self.auth_headers())
        if method not in ("GET", "HEAD"):
            # Bitbucket's XSRF protection rejects POSTs without this header.
            headers["X-Atlassian-Token"] = "no-check"
        clean_params = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        attempts = self.max_retries + 1 if idempotent else 1
        for attempt in range(attempts):
            t0 = time.monotonic()
            try:
                resp = await self._http.request(
                    method, url, params=clean_params, json=json, headers=headers
                )
            except httpx.TransportError as exc:
                err = error_from_transport(exc, self.base_url + url)
                log.debug(
                    "%s %s -> transport error %s (attempt %d/%d)",
                    method, url, redact(str(exc)), attempt + 1, attempts,
                )
                if attempt + 1 < attempts and not isinstance(err, TLSError):
                    await asyncio.sleep(self.backoff * (2 ** attempt))
                    continue
                raise err from exc
            elapsed = time.monotonic() - t0
            log.debug(
                "%s %s params=%s -> %s in %.2fs req_headers=%s",
                method, url, clean_params, resp.status_code, elapsed,
                redact_headers({**self._http.headers, **headers}),
            )
            user = resp.headers.get("x-ausername")
            if user:
                self.server_username = user
            if resp.status_code in RETRY_STATUSES and attempt + 1 < attempts:
                delay = self.backoff * (2 ** attempt)
                ra = resp.headers.get("retry-after")
                if ra and ra.isdigit():
                    delay = min(float(ra), 10.0)
                await asyncio.sleep(delay)
                continue
            if 200 <= resp.status_code < 300:
                return resp
            raise error_from_response(resp)
        raise BitbucketError("Request failed after retries")  # pragma: no cover

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            ctype = resp.headers.get("content-type", "?")
            snippet = redact(resp.text[:200].replace("\n", " "))
            raise UnexpectedResponseError(
                f"Expected JSON but got {ctype}: {snippet!r}", status=resp.status_code
            )

    async def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self._json(await self.request("GET", path, params=params))

    async def post_json(self, path: str, body: Any = None, params: Optional[Dict[str, Any]] = None) -> Any:
        return self._json(await self.request("POST", path, params=params, json=body if body is not None else {}))

    async def put_json(self, path: str, body: Any = None) -> Any:
        return self._json(await self.request("PUT", path, json=body if body is not None else {}))

    async def get_page(
        self,
        path: str,
        parse: Callable[[Dict[str, Any]], T],
        *,
        start: int = 0,
        limit: int = 50,
        params: Optional[Dict[str, Any]] = None,
    ) -> Page[T]:
        p = dict(params or {})
        p["start"] = start
        p["limit"] = limit
        data = await self.get_json(path, p)
        if not isinstance(data, dict):
            raise UnexpectedResponseError("Expected a paged JSON object")
        return Page.from_json(data, parse)

    async def iter_all(
        self,
        path: str,
        parse: Callable[[Dict[str, Any]], T],
        *,
        params: Optional[Dict[str, Any]] = None,
        limit: int = 100,
        max_items: Optional[int] = None,
    ) -> AsyncIterator[T]:
        """Iterate every item across pages (bounded by ``max_items``)."""
        start: Optional[int] = 0
        count = 0
        while start is not None:
            page = await self.get_page(path, parse, start=start, limit=limit, params=params)
            for v in page.values:
                yield v
                count += 1
                if max_items is not None and count >= max_items:
                    return
            start = None if page.is_last_page else page.next_page_start

    # ------------------------------------------------------------- server
    async def test_connection(self) -> ServerInfo:
        """Cheap checks: reachability + Bitbucket identity, then an authenticated call."""
        info = ServerInfo(base_url=self.base_url)
        try:
            props = await self.get_json(f"{API}/application-properties")
            if isinstance(props, dict):
                info.version = str(props.get("version") or "")
                info.display_name = str(props.get("displayName") or "")
        except (NotFoundError, UnexpectedResponseError) as exc:
            raise UnexpectedResponseError(
                f"{self.base_url} does not look like a Bitbucket Server REST endpoint ({exc.message})",
                status=exc.status,
                hint="Use the base URL you open in the browser, e.g. https://bitbucket.example.com",
            ) from exc
        # application-properties may be anonymous; this call proves the credentials.
        try:
            await self.get_json(f"{API}/projects", {"limit": 1})
        except PermissionDeniedError as exc:
            # Any authenticated user may list projects, so a refusal here means the
            # credentials were not accepted.
            raise AuthenticationError(exc.message, status=exc.status, errors=exc.errors) from exc
        info.username = self.server_username or (self.auth.username if self.auth else "")
        if not self.server_username:
            info.warnings.append(
                "Server did not report an authenticated user (X-AUSERNAME); "
                "requests may be anonymous. Personal repos/approve may not work."
            )
        if info.username:
            try:
                info.user = await self.find_user(info.username)
            except BitbucketError as exc:
                log.debug("user lookup failed: %s", exc.message)
        if info.user is None and info.username:
            info.user = User(name=info.username, slug=info.username, display_name=info.username)
        return info

    async def find_user(self, username: str) -> Optional[User]:
        data = await self.get_json(f"{API}/users", {"filter": username, "limit": 25})
        for v in (data or {}).get("values") or []:
            if str(v.get("name", "")).lower() == username.lower():
                return User.from_json(v)
        return None

    # ----------------------------------------------------- projects/repos
    async def list_projects(self, start: int = 0, limit: int = 50, name: str = "") -> Page[Project]:
        return await self.get_page(
            f"{API}/projects", Project.from_json, start=start, limit=limit, params={"name": name or None}
        )

    async def list_project_repos(self, project_key: str, start: int = 0, limit: int = 50) -> Page[Repository]:
        return await self.get_page(
            f"{API}/projects/{enc(project_key)}/repos", Repository.from_json, start=start, limit=limit
        )

    async def search_repos(
        self,
        start: int = 0,
        limit: int = 50,
        name: str = "",
        project_name: str = "",
    ) -> Page[Repository]:
        """Global repository search (``/repos``) with server-side name filtering."""
        return await self.get_page(
            f"{API}/repos",
            Repository.from_json,
            start=start,
            limit=limit,
            params={"name": name or None, "projectname": project_name or None},
        )

    async def get_repo(self, project_key: str, slug: str) -> Repository:
        data = await self.get_json(f"{API}/projects/{enc(project_key)}/repos/{enc(slug)}")
        return Repository.from_json(data)

    def _repo_path(self, project_key: str, slug: str) -> str:
        return f"{API}/projects/{enc(project_key)}/repos/{enc(slug)}"

    async def list_branches(
        self, project_key: str, slug: str, start: int = 0, limit: int = 50, filter_text: str = ""
    ) -> Page[Branch]:
        return await self.get_page(
            f"{self._repo_path(project_key, slug)}/branches",
            Branch.from_json,
            start=start,
            limit=limit,
            params={"filterText": filter_text or None, "orderBy": "MODIFICATION", "details": "false"},
        )

    async def browse_dir(
        self, project_key: str, slug: str, path: str = "", at: str = "", start: int = 0, limit: int = 200
    ) -> DirectoryPage:
        url = f"{self._repo_path(project_key, slug)}/browse"
        if path.strip("/"):
            url += "/" + enc_path(path)
        data = await self.get_json(url, {"at": at or None, "start": start, "limit": limit})
        if not isinstance(data, dict) or "children" not in data:
            raise UnexpectedResponseError(f"'{path or '/'}' is not a directory")
        base = path.strip("/")

        def parse(d: Dict[str, Any]) -> BrowseEntry:
            p = d.get("path") or {}
            rel = str(p.get("toString") or "/".join(p.get("components") or []) or p.get("name") or "")
            name = str(p.get("name") or rel)
            return BrowseEntry(
                name=rel or name,
                path=f"{base}/{rel}" if base else rel,
                type=str(d.get("type") or "FILE"),
                size=d.get("size"),
            )

        children = Page.from_json(data.get("children") or {}, parse)
        return DirectoryPage(path=base, revision=str(data.get("revision") or at or ""), children=children)

    async def browse_file(
        self, project_key: str, slug: str, path: str, at: str = "", start: int = 0, limit: int = 1000
    ) -> FilePage:
        url = f"{self._repo_path(project_key, slug)}/browse/{enc_path(path)}"
        data = await self.get_json(url, {"at": at or None, "start": start, "limit": limit})
        if not isinstance(data, dict):
            raise UnexpectedResponseError("Unexpected file browse response")
        if "children" in data:
            raise UnexpectedResponseError(f"'{path}' is a directory")
        if data.get("binary"):
            return FilePage(lines=[], start=start, is_last_page=True, next_page_start=None, binary=True)
        lines = [str(ln.get("text", "")) for ln in data.get("lines") or []]
        is_last = bool(data.get("isLastPage", True))
        nps = data.get("nextPageStart")
        if not is_last and nps is None:
            nps = start + len(lines)
        if not is_last and not lines:
            is_last, nps = True, None
        return FilePage(
            lines=lines,
            start=int(data.get("start", start) or 0),
            is_last_page=is_last,
            next_page_start=None if is_last else int(nps),
        )

    # ------------------------------------------------------- pull requests
    def _pr_path(self, project_key: str, slug: str, pr_id: Optional[int] = None) -> str:
        base = f"{self._repo_path(project_key, slug)}/pull-requests"
        return base if pr_id is None else f"{base}/{int(pr_id)}"

    async def list_pull_requests(
        self, project_key: str, slug: str, state: str = "OPEN", start: int = 0, limit: int = 25
    ) -> Page[PullRequest]:
        state = state.upper()
        if state not in PR_STATES:
            raise ConfigError(f"Invalid PR state {state!r}")
        return await self.get_page(
            self._pr_path(project_key, slug),
            PullRequest.from_json,
            start=start,
            limit=limit,
            params={"state": state, "order": "NEWEST"},
        )

    async def get_pull_request(self, project_key: str, slug: str, pr_id: int) -> PullRequest:
        return PullRequest.from_json(await self.get_json(self._pr_path(project_key, slug, pr_id)))

    async def merge_check(self, project_key: str, slug: str, pr_id: int) -> MergeCheck:
        return MergeCheck.from_json(await self.get_json(self._pr_path(project_key, slug, pr_id) + "/merge"))

    async def merge_config(self, project_key: str, slug: str) -> Optional[MergeConfig]:
        """Merge strategies for the repo; ``None`` if not visible to this user."""
        try:
            data = await self.get_json(f"{self._repo_path(project_key, slug)}/settings/pull-requests")
        except (PermissionDeniedError, NotFoundError, AuthenticationError, UnexpectedResponseError, ServerError) as exc:
            # Optional information: Bitbucket often answers missing permissions with
            # 401 (AuthorisationException). Fall back to the server's default strategy.
            log.debug("merge settings unavailable: %s", exc.message)
            return None
        cfg = MergeConfig.from_json(data or {})
        return cfg if cfg.strategies else None

    async def merge(
        self,
        project_key: str,
        slug: str,
        pr_id: int,
        version: int,
        strategy_id: Optional[str] = None,
        message: Optional[str] = None,
    ) -> PullRequest:
        """Merge a PR. ``version`` is mandatory (optimistic locking); a stale
        version yields a :class:`ConflictError` with ``is_out_of_date``."""
        body: Dict[str, Any] = {"version": int(version)}
        if strategy_id:
            body["strategyId"] = strategy_id
        if message:
            body["message"] = message
        data = await self.post_json(
            self._pr_path(project_key, slug, pr_id) + "/merge", body, params={"version": int(version)}
        )
        return PullRequest.from_json(data)

    async def decline(self, project_key: str, slug: str, pr_id: int, version: int) -> PullRequest:
        data = await self.post_json(
            self._pr_path(project_key, slug, pr_id) + "/decline",
            {"version": int(version)},
            params={"version": int(version)},
        )
        return PullRequest.from_json(data)

    async def set_review_status(
        self, project_key: str, slug: str, pr_id: int, user_slug: str, status: str = "APPROVED"
    ) -> Dict[str, Any]:
        """Approve / needs-work / unapprove as the current user."""
        status = status.upper()
        path = f"{self._pr_path(project_key, slug, pr_id)}/participants/{enc(user_slug)}"
        try:
            return await self.put_json(path, {"status": status, "approved": status == "APPROVED"})
        except NotFoundError:
            if status != "APPROVED":
                raise
            # Very old servers: deprecated approve endpoint.
            return await self.post_json(self._pr_path(project_key, slug, pr_id) + "/approve")

    async def add_comment(self, project_key: str, slug: str, pr_id: int, text: str) -> Dict[str, Any]:
        if not text.strip():
            raise ConfigError("Comment text is empty")
        return await self.post_json(self._pr_path(project_key, slug, pr_id) + "/comments", {"text": text})

    async def list_activities(
        self, project_key: str, slug: str, pr_id: int, start: int = 0, limit: int = 50
    ) -> Page[Activity]:
        return await self.get_page(
            self._pr_path(project_key, slug, pr_id) + "/activities",
            Activity.from_json,
            start=start,
            limit=limit,
        )

    async def list_changes(
        self, project_key: str, slug: str, pr_id: int, start: int = 0, limit: int = 100
    ) -> Page[Change]:
        return await self.get_page(
            self._pr_path(project_key, slug, pr_id) + "/changes",
            Change.from_json,
            start=start,
            limit=limit,
            params={"withComments": "false"},
        )

    async def pr_diff(
        self,
        project_key: str,
        slug: str,
        pr_id: int,
        path: str = "",
        context_lines: int = 3,
        src_path: str = "",
    ) -> DiffResult:
        """Structured diff for the whole PR or a single file."""
        url = self._pr_path(project_key, slug, pr_id) + "/diff"
        if path:
            url += "/" + enc_path(path)
        params = {"contextLines": context_lines, "withComments": "false", "srcPath": src_path or None}
        return DiffResult.from_json(await self.get_json(url, params))


def is_auth_error(exc: BaseException) -> bool:
    return isinstance(exc, AuthenticationError)
