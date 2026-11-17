"""In-memory fake of the Bitbucket Server REST 1.0 endpoints used by bbtui.

Used by the unit/TUI tests and by ``bbtui --demo``. It is an
``httpx.MockTransport`` handler, so no sockets are opened.

Shapes follow the Bitbucket Server REST documentation (paged responses with
``isLastPage``/``nextPageStart``, error bodies with ``errors[]``, merge vetoes,
``PullRequestOutOfDateException`` etc.).
"""

from __future__ import annotations

import base64
import copy
import json
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

import httpx

DEMO_URL = "https://bitbucket.example.test"
DEMO_TOKEN = "demo-token-0123456789"
DEMO_USER = "alice"

J = Dict[str, Any]


def _err(status: int, message: str, exception: str = "", **extra: Any) -> httpx.Response:
    e: J = {"context": None, "message": message, "exceptionName": exception or None}
    e.update(extra)
    return httpx.Response(status, json={"errors": [e]})


def _user(name: str, display: str) -> J:
    return {
        "name": name,
        "slug": name,
        "displayName": display,
        "emailAddress": f"{name}@example.test",
        "id": abs(hash(name)) % 10000,
        "active": True,
        "type": "NORMAL",
    }


USERS = {
    "alice": _user("alice", "Alice Anders"),
    "bob": _user("bob", "Bob Brown"),
    "carol": _user("carol", "Carol Chen"),
}


class FakeBitbucket:
    def __init__(
        self,
        base_url: str = DEMO_URL,
        token: str = DEMO_TOKEN,
        username: str = DEMO_USER,
        password: str = "s3cret-pass",
        n_bulk_projects: int = 120,
        n_bulk_repos: int = 130,
        version: str = "8.19.1",
        anonymous_app_properties: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.host = re.sub(r"^https?://", "", self.base_url)
        self.token = token
        self.username = username
        self.password = password
        self.version = version
        self.anonymous_app_properties = anonymous_app_properties
        self.requests: List[httpx.Request] = []
        self.fail_next: List[Tuple[str, int]] = []  # (path-regex, status) one-shot failures
        self.delay_paths: Dict[str, float] = {}
        self._build(n_bulk_projects, n_bulk_repos)

    # ----------------------------------------------------------- fixtures
    def _project(self, key: str, name: str, desc: str = "", personal: bool = False) -> J:
        return {
            "key": key,
            "id": len(self.projects) + 1,
            "name": name,
            "description": desc,
            "public": False,
            "type": "PERSONAL" if personal else "NORMAL",
            "links": {"self": [{"href": f"{self.base_url}/projects/{key}"}]},
        }

    def _repo(self, project: J, slug: str, name: str = "", desc: str = "") -> J:
        pk = project["key"].lower()
        return {
            "slug": slug,
            "id": sum(len(r) for r in self.repos.values()) + 1,
            "name": name or slug,
            "description": desc,
            "scmId": "git",
            "state": "AVAILABLE",
            "statusMessage": "Available",
            "forkable": True,
            "public": False,
            "archived": False,
            "project": project,
            "links": {
                "clone": [
                    {"href": f"ssh://git@{self.host.split(':')[0]}:7999/{pk}/{slug}.git", "name": "ssh"},
                    {"href": f"{self.base_url.replace('://', '://' + self.username + '@')}/scm/{pk}/{slug}.git", "name": "http"},
                ],
                "self": [{"href": f"{self.base_url}/projects/{project['key']}/repos/{slug}/browse"}],
            },
        }

    def _build(self, n_projects: int, n_repos: int) -> None:
        self.projects: List[J] = []
        self.repos: Dict[str, List[J]] = {}
        prj = self._project("PRJ", "Platform", "Core platform services")
        mob = self._project("MOB", "Mobile Apps", "iOS and Android")
        big = self._project("BIG", "Big Project", "Lots of repositories")
        self.projects += [prj, mob, big]
        for i in range(n_projects):
            self.projects.append(self._project(f"P{i:03d}", f"Generated Project {i:03d}"))
        self.repos["PRJ"] = [
            self._repo(prj, "alpha", "Alpha Service", "The alpha service"),
            self._repo(prj, "beta", "Beta Service"),
        ]
        self.repos["MOB"] = [self._repo(mob, "ios-app", "iOS App"), self._repo(mob, "android-app", "Android App")]
        self.repos["BIG"] = [self._repo(big, f"repo-{i:03d}") for i in range(n_repos)]
        personal = self._project(f"~{self.username.upper()}", USERS.get(self.username, _user(self.username, self.username))["displayName"], personal=True)
        self.personal_key = personal["key"]
        self.repos[self.personal_key] = [self._repo(personal, "scratch", "Scratch")]

        self.branches = {
            "PRJ/alpha": [
                {"id": "refs/heads/master", "displayId": "master", "type": "BRANCH", "latestCommit": "a" * 40, "isDefault": True},
                {"id": "refs/heads/develop", "displayId": "develop", "type": "BRANCH", "latestCommit": "b" * 40, "isDefault": False},
                {"id": "refs/heads/feature/x", "displayId": "feature/x", "type": "BRANCH", "latestCommit": "c" * 40, "isDefault": False},
            ]
        }
        big_lines = [f"line {i}" for i in range(1, 2501)]
        self.files: Dict[str, Dict[str, Any]] = {
            # ref -> {path: content(list[str]) | "DIR" | "BINARY"}
            "master": {
                "README.md": ["# Alpha", "", "Alpha service readme."],
                "src": "DIR",
                "src/app.py": ["import sys", "", "def main():", "    print('hello')", "", "if __name__ == '__main__':", "    main()"],
                "src/util": "DIR",
                "src/util/helpers.py": ["def helper():", "    return 42"],
                "big.txt": big_lines,
                "logo.png": "BINARY",
            },
            "develop": {
                "README.md": ["# Alpha (develop)"],
                "NEW_ON_DEVELOP.md": ["new"],
            },
        }
        alice, bob, carol = USERS["alice"], USERS["bob"], USERS["carol"]
        alpha = self.repos["PRJ"][0]

        def ref(branch: str, commit: str) -> J:
            return {"id": f"refs/heads/{branch}", "displayId": branch, "latestCommit": commit, "repository": alpha}

        def part(u: J, role: str, status: str) -> J:
            return {"user": u, "role": role, "approved": status == "APPROVED", "status": status}

        def pr(pid: int, title: str, state: str, frm: str, version: int, reviewers: List[J], desc: str = "") -> J:
            return {
                "id": pid,
                "version": version,
                "title": title,
                "description": desc,
                "state": state,
                "open": state == "OPEN",
                "closed": state != "OPEN",
                "createdDate": 1700000000000 + pid * 1000,
                "updatedDate": 1700000500000 + pid * 1000,
                "fromRef": ref(frm, "c" * 40),
                "toRef": ref("master", "a" * 40),
                "locked": False,
                "author": part(bob, "AUTHOR", "UNAPPROVED"),
                "reviewers": reviewers,
                "participants": [],
                "properties": {"commentCount": 2, "openTaskCount": 0},
                "links": {"self": [{"href": f"{self.base_url}/projects/PRJ/repos/alpha/pull-requests/{pid}"}]},
            }

        self.prs: Dict[str, List[J]] = {
            "PRJ/alpha": [
                pr(1, "Add feature X", "OPEN", "feature/x", 2,
                   [part(alice, "REVIEWER", "UNAPPROVED"), part(carol, "REVIEWER", "APPROVED")],
                   "This adds **feature X**.\n\n- item one\n- item two"),
                pr(2, "Risky change with conflicts", "OPEN", "develop", 0,
                   [part(carol, "REVIEWER", "NEEDS_WORK")], "Conflicts with master."),
                pr(3, "Old merged work", "MERGED", "feature/old", 5, [part(alice, "REVIEWER", "APPROVED")]),
                pr(4, "Declined idea", "DECLINED", "feature/nope", 1, []),
            ]
        }
        for i in range(5, 5 + 60):  # many PRs for paging
            self.prs["PRJ/alpha"].append(pr(i, f"Bulk merged PR {i}", "MERGED", f"bulk/{i}", 1, []))

        self.merge_checks = {
            1: {"canMerge": True, "conflicted": False, "outcome": "CLEAN", "vetoes": []},
            2: {
                "canMerge": False,
                "conflicted": True,
                "outcome": "CONFLICTED",
                "vetoes": [
                    {"summaryMessage": "Requires 2 approvals", "detailedMessage": "You need 2 more approvals before this pull request can be merged."}
                ],
            },
        }
        self.activities = {
            1: [
                {"id": 11, "createdDate": 1700000600000, "user": carol, "action": "APPROVED"},
                {"id": 10, "createdDate": 1700000550000, "user": carol, "action": "COMMENTED", "commentAction": "ADDED",
                 "comment": {"id": 100, "version": 0, "text": "Looks good overall.", "author": carol, "createdDate": 1700000550000,
                             "comments": [{"id": 101, "text": "Thanks!", "author": bob, "createdDate": 1700000560000, "comments": []}]}},
                {"id": 9, "createdDate": 1700000540000, "user": alice, "action": "COMMENTED", "commentAction": "ADDED",
                 "commentAnchor": {"path": {"toString": "src/app.py"}, "line": 4, "lineType": "ADDED"},
                 "comment": {"id": 99, "version": 0, "text": "Nit: rename this.", "author": alice, "createdDate": 1700000540000, "comments": []}},
                {"id": 8, "createdDate": 1700000500000, "user": bob, "action": "OPENED"},
            ]
        }
        self.changes = {
            1: [
                {"path": {"toString": "src/app.py", "components": ["src", "app.py"]}, "type": "MODIFY", "nodeType": "FILE"},
                {"path": {"toString": "docs/new.md", "components": ["docs", "new.md"]}, "type": "ADD", "nodeType": "FILE"},
                {"path": {"toString": "old.txt", "components": ["old.txt"]}, "type": "DELETE", "nodeType": "FILE"},
                {"path": {"toString": "assets/logo.png"}, "type": "MODIFY", "nodeType": "FILE"},
            ]
        }
        self.diffs: Dict[str, J] = {
            "src/app.py": {
                "source": {"toString": "src/app.py"},
                "destination": {"toString": "src/app.py"},
                "hunks": [{
                    "sourceLine": 1, "sourceSpan": 4, "destinationLine": 1, "destinationSpan": 5,
                    "segments": [
                        {"type": "CONTEXT", "lines": [{"source": 1, "destination": 1, "line": "import sys"}, {"source": 2, "destination": 2, "line": ""}]},
                        {"type": "REMOVED", "lines": [{"source": 3, "destination": 3, "line": "def main():"}]},
                        {"type": "ADDED", "lines": [{"source": 4, "destination": 3, "line": "def main(argv=None):"}, {"source": 4, "destination": 4, "line": "    \"\"\"Entry point.\"\"\""}]},
                        {"type": "CONTEXT", "lines": [{"source": 4, "destination": 5, "line": "    print('hello')"}]},
                    ],
                }],
                "truncated": False,
            },
            "docs/new.md": {
                "source": None,
                "destination": {"toString": "docs/new.md"},
                "hunks": [{"sourceLine": 0, "sourceSpan": 0, "destinationLine": 1, "destinationSpan": 2,
                           "segments": [{"type": "ADDED", "lines": [{"source": 0, "destination": 1, "line": "# New"}, {"source": 0, "destination": 2, "line": "docs"}]}]}],
            },
            "old.txt": {
                "source": {"toString": "old.txt"},
                "destination": None,
                "hunks": [{"sourceLine": 1, "sourceSpan": 1, "destinationLine": 0, "destinationSpan": 0,
                           "segments": [{"type": "REMOVED", "lines": [{"source": 1, "destination": 0, "line": "obsolete"}]}]}],
            },
            "assets/logo.png": {"source": {"toString": "assets/logo.png"}, "destination": {"toString": "assets/logo.png"}, "binary": True, "hunks": []},
        }
        self.merge_settings = {
            "mergeConfig": {
                "defaultStrategy": {"id": "no-ff", "name": "Merge commit", "enabled": True},
                "strategies": [
                    {"id": "no-ff", "name": "Merge commit", "description": "Always create a new merge commit", "enabled": True},
                    {"id": "squash", "name": "Squash", "description": "Combine all commits into one", "enabled": True},
                    {"id": "ff-only", "name": "Fast-forward only", "description": "Never create a merge commit", "enabled": False},
                ],
                "type": "REPOSITORY",
            }
        }
        self.comments_posted: List[str] = []
        self.merge_requests: List[J] = []

    # -------------------------------------------------------------- utils
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def _authenticated_user(self, request: httpx.Request) -> Optional[str]:
        h = request.headers.get("authorization", "")
        if h == f"Bearer {self.token}":
            return self.username
        if h.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(h[6:]).decode().partition(":")
            except Exception:
                return None
            if user == self.username and pw in (self.token, self.password):
                return user
        return None

    @staticmethod
    def _paged(values: List[Any], request: httpx.Request, default_limit: int = 25) -> J:
        start = int(request.url.params.get("start", 0))
        limit = int(request.url.params.get("limit", default_limit))
        limit = max(1, min(limit, 1000))
        chunk = values[start : start + limit]
        last = start + limit >= len(values)
        out: J = {"size": len(chunk), "limit": limit, "isLastPage": last, "values": chunk, "start": start}
        if not last:
            out["nextPageStart"] = start + limit
        return out

    def _repo_lookup(self, key: str, slug: str) -> Optional[J]:
        for r in self.repos.get(key, []) + self.repos.get(key.upper(), []):
            if r["slug"] == slug:
                return r
        return None

    def _find_pr(self, key: str, slug: str, pid: int) -> Optional[J]:
        for p in self.prs.get(f"{key}/{slug}", []):
            if p["id"] == pid:
                return p
        return None

    # ------------------------------------------------------------ handler
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = unquote(request.url.path)
        for i, (pat, status) in enumerate(self.fail_next):
            if re.search(pat, path):
                del self.fail_next[i]
                return _err(status, f"Injected failure {status}")
        api = "/rest/api/1.0"
        if not path.startswith(api):
            return httpx.Response(404, text="<html>Not found</html>", headers={"content-type": "text/html"})
        sub = path[len(api):]

        if sub == "/application-properties" and self.anonymous_app_properties:
            user = self._authenticated_user(request)
            resp = httpx.Response(200, json={"version": self.version, "buildNumber": "8019001", "buildDate": "1", "displayName": "Bitbucket"})
            if user:
                resp.headers["X-AUSERNAME"] = user
            return resp

        user = self._authenticated_user(request)
        if user is None:
            if request.headers.get("authorization"):
                return _err(401, "Authentication failed. Please check your credentials and try again.",
                            "com.atlassian.bitbucket.auth.IncorrectPasswordAuthenticationException")
            return _err(401, "You are not permitted to access this resource",
                        "com.atlassian.bitbucket.AuthorisationException")
        resp = self._route(request, sub)
        resp.headers["X-AUSERNAME"] = user
        return resp

    def _route(self, request: httpx.Request, sub: str) -> httpx.Response:
        m = request.method
        q = request.url.params

        if sub == "/projects" and m == "GET":
            name = q.get("name", "").lower()
            vals = [p for p in self.projects if name in p["name"].lower()] if name else self.projects
            return httpx.Response(200, json=self._paged(vals, request))
        if sub == "/repos" and m == "GET":
            name = q.get("name", "").lower()
            pname = q.get("projectname", "").lower()
            vals = [r for rs in self.repos.values() for r in rs]
            if name:
                vals = [r for r in vals if name in r["name"].lower() or name in r["slug"]]
            if pname:
                vals = [r for r in vals if pname in r["project"]["name"].lower()]
            return httpx.Response(200, json=self._paged(vals, request))
        if sub == "/users" and m == "GET":
            f = q.get("filter", "").lower()
            vals = [u for u in USERS.values() if f in u["name"]]
            return httpx.Response(200, json=self._paged(vals, request))

        mm = re.match(r"^/projects/([^/]+)/repos$", sub)
        if mm and m == "GET":
            key = mm.group(1)
            if key not in self.repos and key.upper() not in self.repos:
                return _err(404, f"Project {key} does not exist.", "com.atlassian.bitbucket.project.NoSuchProjectException")
            return httpx.Response(200, json=self._paged(self.repos.get(key) or self.repos.get(key.upper()) or [], request))

        mm = re.match(r"^/projects/([^/]+)/repos/([^/]+)(/.*)?$", sub)
        if not mm:
            return _err(404, "No such resource")
        key, slug, rest = mm.group(1), mm.group(2), mm.group(3) or ""
        repo = self._repo_lookup(key, slug)
        if repo is None:
            return _err(404, f"Repository {key}/{slug} does not exist.", "com.atlassian.bitbucket.repository.NoSuchRepositoryException")
        rk = f"{repo['project']['key']}/{slug}"

        if rest == "" and m == "GET":
            return httpx.Response(200, json=repo)
        if rest == "/branches" and m == "GET":
            ft = q.get("filterText", "").lower()
            vals = [b for b in self.branches.get(rk, [{"id": "refs/heads/master", "displayId": "master", "latestCommit": "a" * 40, "isDefault": True}]) if ft in b["displayId"].lower()]
            return httpx.Response(200, json=self._paged(vals, request))
        if rest.startswith("/browse") and m == "GET":
            return self._browse(request, rk, rest[len("/browse"):].strip("/"))
        if rest == "/settings/pull-requests" and m == "GET":
            return httpx.Response(200, json=self.merge_settings)

        if rest == "/pull-requests" and m == "GET":
            state = q.get("state", "OPEN").upper()
            vals = [p for p in self.prs.get(rk, []) if state == "ALL" or p["state"] == state]
            return httpx.Response(200, json=self._paged(vals, request))
        pm = re.match(r"^/pull-requests/(\d+)(/.*)?$", rest)
        if pm:
            pid = int(pm.group(1))
            pr = self._find_pr(repo["project"]["key"], slug, pid)
            if pr is None:
                return _err(404, f"Pull request {pid} does not exist in {rk}.", "com.atlassian.bitbucket.pull.NoSuchPullRequestException")
            return self._pr_route(request, pr, pm.group(2) or "")
        return _err(404, "No such resource")

    def _browse(self, request: httpx.Request, rk: str, path: str) -> httpx.Response:
        if rk != "PRJ/alpha":
            files: Dict[str, Any] = {"README.md": ["# " + rk]}
        else:
            at = request.url.params.get("at", "") or "master"
            at = at.replace("refs/heads/", "")
            files = self.files.get(at)
            if files is None:
                return _err(404, f"Commit '{at}' does not exist in repository.", "com.atlassian.bitbucket.commit.NoSuchCommitException")
        node = files.get(path) if path else "DIR"
        if node is None:
            return _err(404, f"The path \"{path}\" does not exist at revision", "com.atlassian.bitbucket.content.NoSuchPathException")
        if node == "DIR":
            prefix = f"{path}/" if path else ""
            children = []
            for p, v in sorted(files.items(), key=lambda kv: (kv[1] != "DIR", kv[0])):
                if p.startswith(prefix) and "/" not in p[len(prefix):]:
                    name = p[len(prefix):]
                    child: J = {"path": {"components": [name], "name": name, "toString": name},
                                "type": "DIRECTORY" if v == "DIR" else "FILE"}
                    if v != "DIR":
                        child["size"] = 1234 if v == "BINARY" else sum(len(x) + 1 for x in v)
                    children.append(child)
            body = {"path": {"components": path.split("/") if path else [], "toString": path},
                    "revision": request.url.params.get("at", "refs/heads/master"),
                    "children": self._paged(children, request, 500)}
            return httpx.Response(200, json=body)
        if node == "BINARY":
            return httpx.Response(200, json={"binary": True, "path": {"toString": path}})
        lines = [{"text": t} for t in node]
        return httpx.Response(200, json=self._paged_lines(lines, request))

    def _paged_lines(self, lines: List[J], request: httpx.Request) -> J:
        page = self._paged(lines, request, 500)
        page["lines"] = page.pop("values")
        return page

    def _pr_route(self, request: httpx.Request, pr: J, rest: str) -> httpx.Response:
        m = request.method
        q = request.url.params
        pid = pr["id"]
        if rest == "" and m == "GET":
            return httpx.Response(200, json=pr)
        if rest == "/merge" and m == "GET":
            if pr["state"] != "OPEN":
                return httpx.Response(200, json={"canMerge": False, "conflicted": False, "outcome": "UNKNOWN",
                                                 "vetoes": [{"summaryMessage": f"Pull request is {pr['state'].lower()}", "detailedMessage": ""}]})
            return httpx.Response(200, json=self.merge_checks.get(pid, {"canMerge": True, "conflicted": False, "outcome": "CLEAN", "vetoes": []}))
        if rest == "/merge" and m == "POST":
            body = json.loads(request.content or b"{}")
            self.merge_requests.append({"query": dict(q), "body": body})
            version = q.get("version", body.get("version"))
            if version is None:
                return _err(400, "version is required")
            if int(version) != pr["version"]:
                return _err(409, "You are attempting to modify a pull request based on out-of-date information.",
                            "com.atlassian.bitbucket.pull.PullRequestOutOfDateException",
                            currentVersion=pr["version"], expectedVersion=int(version))
            check = self.merge_checks.get(pid, {"canMerge": True, "vetoes": []})
            if not check.get("canMerge", True):
                return _err(409, "Merging the pull request has been vetoed.",
                            "com.atlassian.bitbucket.pull.PullRequestMergeVetoedException",
                            conflicted=check.get("conflicted", False), vetoes=check.get("vetoes", []))
            pr["state"] = "MERGED"
            pr["open"] = False
            pr["closed"] = True
            pr["version"] += 1
            return httpx.Response(200, json=pr)
        if rest == "/decline" and m == "POST":
            if int(q.get("version", -1)) != pr["version"]:
                return _err(409, "out-of-date", "com.atlassian.bitbucket.pull.PullRequestOutOfDateException", currentVersion=pr["version"])
            pr["state"] = "DECLINED"
            pr["version"] += 1
            return httpx.Response(200, json=pr)
        if rest == "/activities" and m == "GET":
            return httpx.Response(200, json=self._paged(self.activities.get(pid, []), request))
        if rest == "/changes" and m == "GET":
            return httpx.Response(200, json={**self._paged(self.changes.get(pid, []), request), "fromHash": "c" * 40, "toHash": "a" * 40})
        if rest.startswith("/diff") and m == "GET":
            p = rest[len("/diff"):].strip("/")
            if p:
                d = self.diffs.get(p)
                if d is None:
                    return _err(404, f"No diff for {p}")
                diffs = [d]
            else:
                diffs = [self.diffs[c["path"]["toString"]] for c in self.changes.get(pid, []) if c["path"]["toString"] in self.diffs]
            return httpx.Response(200, json={"fromHash": "c" * 40, "toHash": "a" * 40, "contextLines": int(q.get("contextLines", 10)),
                                             "whitespace": "SHOW", "diffs": copy.deepcopy(diffs), "truncated": False})
        if rest == "/comments" and m == "POST":
            body = json.loads(request.content or b"{}")
            self.comments_posted.append(body.get("text", ""))
            return httpx.Response(201, json={"id": 500 + len(self.comments_posted), "text": body.get("text"), "author": USERS["alice"], "version": 0})
        pm = re.match(r"^/participants/([^/]+)$", rest)
        if pm and m == "PUT":
            body = json.loads(request.content or b"{}")
            slug = pm.group(1)
            u = USERS.get(slug, _user(slug, slug))
            entry = {"user": u, "role": "REVIEWER", "approved": body.get("status") == "APPROVED", "status": body.get("status", "UNAPPROVED")}
            for r in pr["reviewers"]:
                if r["user"]["slug"] == slug:
                    r.update(entry)
                    break
            else:
                pr["participants"].append({**entry, "role": "PARTICIPANT"})
            return httpx.Response(200, json=entry)
        return _err(404, "No such resource")
