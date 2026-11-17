import asyncio
import os
import shutil
import subprocess
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bbtui.api import BasicAuth, BearerTokenAuth
from bbtui.api.models import Repository
from bbtui.services.clone import (
    CloneError,
    CloneRunner,
    available_protocols,
    build_clone_plan,
    check_destination,
    clone_url_for,
    config_contains_secret,
    explain_failure,
    ssh_endpoint,
    strip_userinfo,
    tcp_preflight,
)

TOKEN = "clone-secret-token-XYZ"

REPO_JSON = {
    "slug": "alpha",
    "name": "alpha",
    "project": {"key": "PRJ", "name": "Platform"},
    "links": {
        "clone": [
            {"href": "https://alice@bb.example.test/scm/prj/alpha.git", "name": "http"},
            {"href": "ssh://git@bb.example.test:7999/prj/alpha.git", "name": "ssh"},
        ]
    },
}


def test_clone_links_and_userinfo_stripping():
    repo = Repository.from_json(REPO_JSON)
    assert available_protocols(repo) == ["http", "ssh"]
    assert clone_url_for(repo, "http") == "https://bb.example.test/scm/prj/alpha.git"
    assert clone_url_for(repo, "ssh") == "ssh://git@bb.example.test:7999/prj/alpha.git"
    assert strip_userinfo("https://u:p@h/x.git") == "https://h/x.git"
    repo.clone_links.pop("ssh")
    with pytest.raises(CloneError):
        clone_url_for(repo, "ssh")


def test_ssh_endpoint():
    assert ssh_endpoint("ssh://git@bb.example.test:7999/prj/a.git") == ("bb.example.test", 7999)
    assert ssh_endpoint("git@github.com:org/repo.git") == ("github.com", 22)


@pytest.mark.parametrize("auth", [BearerTokenAuth(TOKEN), BasicAuth("alice", TOKEN)])
def test_https_plan_keeps_token_out_of_argv_and_url(tmp_path, auth):
    plan = build_clone_plan(
        "https://alice@bb.example.test/scm/prj/alpha.git", str(tmp_path / "alpha"), "http", auth, branch="refs/heads/feature/x"
    )
    joined = " ".join(plan.argv)
    header = auth.authorization_header()
    assert TOKEN not in joined and header.split()[-1] not in joined
    assert "alice@" not in plan.url and plan.url == "https://bb.example.test/scm/prj/alpha.git"
    assert plan.argv[:3] == ["git", "clone", "--progress"]
    assert plan.argv[3:5] == ["--branch", "feature/x"]
    assert plan.argv[-3:] == ["--", plan.url, str(tmp_path / "alpha")]
    # header is passed via env config, scoped to the server
    keys = {plan.env[f"GIT_CONFIG_KEY_{i}"]: plan.env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(plan.env["GIT_CONFIG_COUNT"]))}
    assert keys["http.https://bb.example.test/.extraHeader"] == f"Authorization: {header}"
    assert keys["credential.helper"] == ""
    assert plan.env["GIT_TERMINAL_PROMPT"] == "0"
    assert TOKEN not in plan.display_command()


def test_ssh_plan(tmp_path):
    plan = build_clone_plan("ssh://git@bb.example.test:7999/prj/alpha.git", str(tmp_path / "a"), "ssh", BearerTokenAuth(TOKEN), base_env={})
    assert "GIT_CONFIG_COUNT" not in plan.env
    assert "BatchMode=yes" in plan.env["GIT_SSH_COMMAND"]
    assert plan.ssh_target == ("bb.example.test", 7999)
    assert TOKEN not in " ".join(plan.argv) + " ".join(plan.env.values())
    # respects a user-provided GIT_SSH_COMMAND
    plan2 = build_clone_plan("ssh://git@h:7999/p/a.git", str(tmp_path / "b"), "ssh", None, base_env={"GIT_SSH_COMMAND": "ssh -v"})
    assert "GIT_SSH_COMMAND" not in plan2.env


def test_branch_injection_rejected(tmp_path):
    with pytest.raises(CloneError):
        build_clone_plan("https://h/x.git", str(tmp_path / "x"), "http", None, branch="--upload-pack=evil")


def test_check_destination(tmp_path):
    assert check_destination(str(tmp_path / "new")).ok
    nonempty = tmp_path / "full"
    nonempty.mkdir()
    (nonempty / "f").write_text("x")
    c = check_destination(str(nonempty))
    assert not c.ok and "refusing" in c.message
    empty = tmp_path / "empty"
    empty.mkdir()
    assert check_destination(str(empty)).ok
    deep = check_destination(str(tmp_path / "a" / "b" / "c"))
    assert deep.ok and deep.needs_parent
    (tmp_path / "file").write_text("x")
    assert not check_destination(str(tmp_path / "file")).ok
    assert not check_destination("").ok


def test_explain_failure():
    assert "HTTPS" in explain_failure("ssh: connect to host h port 7999: Operation timed out", "ssh", ("h", 7999))
    assert "7999" in explain_failure("ssh: connect to host h port 7999: Connection refused", "ssh", ("h", 7999))
    assert "host key" in explain_failure("Host key verification failed.", "ssh").lower()
    assert "credentials" in explain_failure("fatal: Authentication failed for 'https://h/x.git'", "http")
    assert explain_failure("Receiving objects: 40% (401/1000)", "http") == ""


async def test_tcp_preflight_unreachable():
    # Port 1 on localhost is essentially never listening.
    err = await tcp_preflight("127.0.0.1", 1, timeout=2)
    assert err and "127.0.0.1:1" in err


# ----------------------------------------------- real git over local HTTP
class _AuthHandler(SimpleHTTPRequestHandler):
    expected = ""
    seen_auth = []

    def do_GET(self):  # noqa: N802
        auth = self.headers.get("Authorization", "")
        type(self).seen_auth.append(auth)
        if auth != self.expected:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="test"')
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, *args):
        pass


@pytest.fixture
def git_http_server(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    work = tmp_path / "work"
    work.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    run = partial(subprocess.run, check=True, capture_output=True, env=env)
    run(["git", "init", "-q", "-b", "main", str(work)])
    (work / "README.md").write_text("hello\n")
    run(["git", "-C", str(work), "add", "."])
    run(["git", "-C", str(work), "commit", "-q", "-m", "init"])
    run(["git", "-C", str(work), "branch", "feature"])
    served = tmp_path / "served"
    served.mkdir()
    run(["git", "clone", "-q", "--bare", str(work), str(served / "repo.git")])
    run(["git", "-C", str(served / "repo.git"), "update-server-info"])

    auth = BearerTokenAuth(TOKEN)
    handler = type("H", (_AuthHandler,), {"expected": auth.authorization_header(), "seen_auth": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(handler, directory=str(served)))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://alice@127.0.0.1:{server.server_address[1]}/repo.git", auth, handler
    server.shutdown()


async def test_real_https_style_clone_uses_header_and_does_not_persist_it(tmp_path, git_http_server):
    url, auth, handler = git_http_server
    dest = tmp_path / "clones" / "repo"
    plan = build_clone_plan(url, str(dest), "http", auth, branch="feature")
    lines = []
    runner = CloneRunner(plan)
    rc = await asyncio.wait_for(runner.run(lambda line, progress: lines.append(line)), 60)
    assert rc == 0, "\n".join(runner.output)
    assert (dest / "README.md").read_text() == "hello\n"
    # server saw the bearer header
    assert auth.authorization_header() in handler.seen_auth
    # nothing secret persisted in the clone, remote URL has no userinfo
    cfg = (dest / ".git" / "config").read_text()
    assert TOKEN not in cfg and "extraheader" not in cfg.lower()
    assert not config_contains_secret(str(dest), plan.secrets)
    remote = subprocess.run(["git", "-C", str(dest), "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
    assert remote == url.replace("alice@", "")
    head = subprocess.run(["git", "-C", str(dest), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert head == "feature"
    assert all(TOKEN not in l for l in lines)


async def test_real_clone_without_auth_fails_fast(tmp_path, git_http_server):
    url, _auth, _handler = git_http_server
    plan = build_clone_plan(url, str(tmp_path / "noauth"), "http", None)
    runner = CloneRunner(plan)
    rc = await asyncio.wait_for(runner.run(), 60)  # GIT_TERMINAL_PROMPT=0: no hang
    assert rc != 0
    assert explain_failure("\n".join(runner.output), "http")


def test_config_contains_secret_detects(tmp_path):
    d = tmp_path / "r" / ".git"
    d.mkdir(parents=True)
    (d / "config").write_text('[http]\n\textraHeader = Authorization: Bearer abc\n')
    assert config_contains_secret(str(tmp_path / "r"), ["Bearer abc"])
