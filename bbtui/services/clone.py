"""`git clone` planning and execution.

HTTPS: the Authorization header is passed to git through *environment-provided*
configuration (``GIT_CONFIG_COUNT`` / ``GIT_CONFIG_KEY_n`` / ``GIT_CONFIG_VALUE_n``,
git >= 2.31), scoped to the server URL via ``http.<url>.extraHeader``. Therefore:

* the token is not in argv (not visible in ``ps`` output / shell history);
* the token is not written to the clone's ``.git/config`` (env config is transient);
* the remote URL stored in the clone has any ``user[:pass]@`` userinfo stripped.

SSH: runs with ``BatchMode=yes`` and a connect timeout so a missing key,
unknown host key or unreachable port fails fast instead of hanging the UI.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from ..api.auth import AuthStrategy
from ..api.models import Repository
from ..redact import redact

PROTOCOL_LABELS = {"http": "HTTPS", "ssh": "SSH"}
MIN_GIT_FOR_ENV_CONFIG = (2, 31)


class CloneError(Exception):
    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def describe(self) -> str:
        return self.message + (f"\nHint: {self.hint}" if self.hint else "")


def strip_userinfo(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme in ("http", "https") and ("@" in parts.netloc):
        host = parts.netloc.rsplit("@", 1)[1]
        return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    return url


def available_protocols(repo: Repository) -> List[str]:
    return [p for p in ("http", "ssh") if p in repo.clone_links]


def clone_url_for(repo: Repository, protocol: str) -> str:
    url = repo.clone_links.get(protocol)
    if not url:
        raise CloneError(
            f"Repository has no {PROTOCOL_LABELS.get(protocol, protocol)} clone link",
            hint="Pick the other protocol; the server may have it disabled.",
        )
    return strip_userinfo(url) if protocol == "http" else url


def ssh_endpoint(url: str) -> Optional[Tuple[str, int]]:
    """(host, port) for ssh://host:port/... URLs; scp-style user@host:path -> port 22."""
    if url.startswith("ssh://"):
        parts = urlsplit(url)
        if parts.hostname:
            return parts.hostname, parts.port or 22
        return None
    m = re.match(r"^(?:[^@/]+@)?([^:/]+):", url)
    if m:
        return m.group(1), 22
    return None


def http_scope(url: str) -> str:
    """URL prefix used to scope http.<url>.extraHeader to this server only."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


@dataclass
class DestinationCheck:
    path: str
    ok: bool
    needs_parent: bool = False
    message: str = ""


def check_destination(dest: str) -> DestinationCheck:
    raw = (dest or "").strip()
    if not raw:
        return DestinationCheck(path="", ok=False, message="Destination path is required")
    path = os.path.abspath(os.path.expanduser(raw))
    if os.path.exists(path):
        if not os.path.isdir(path):
            return DestinationCheck(path, False, message=f"{path} exists and is a file")
        try:
            entries = os.listdir(path)
        except OSError as exc:
            return DestinationCheck(path, False, message=f"Cannot read {path}: {exc}")
        if entries:
            return DestinationCheck(
                path, False, message=f"{path} already exists and is not empty; refusing to overwrite"
            )
        return DestinationCheck(path, True, message=f"{path} exists and is empty; will clone into it")
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        if os.path.exists(parent):
            return DestinationCheck(path, False, message=f"{parent} is not a directory")
        return DestinationCheck(path, True, needs_parent=True, message=f"Parent {parent} does not exist (will ask to create)")
    if not os.access(parent, os.W_OK):
        return DestinationCheck(path, False, message=f"No write permission in {parent}")
    return DestinationCheck(path, True, message=f"Will clone into {path}")


@dataclass
class ClonePlan:
    argv: List[str]
    env: Dict[str, str]
    url: str
    dest: str
    protocol: str
    ssh_target: Optional[Tuple[str, int]] = None
    secrets: List[str] = field(default_factory=list, repr=False)

    def display_command(self) -> str:
        return " ".join(shlex.quote(a) for a in self.argv)


def git_env_config(pairs: List[Tuple[str, str]]) -> Dict[str, str]:
    env = {"GIT_CONFIG_COUNT": str(len(pairs))}
    for i, (k, v) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"] = k
        env[f"GIT_CONFIG_VALUE_{i}"] = v
    return env


def build_clone_plan(
    url: str,
    dest: str,
    protocol: str,
    auth: Optional[AuthStrategy],
    branch: str = "",
    base_env: Optional[Mapping[str, str]] = None,
) -> ClonePlan:
    base_env = os.environ if base_env is None else base_env
    dest = os.path.abspath(os.path.expanduser(dest))
    argv = ["git", "clone", "--progress"]
    if branch:
        if branch.startswith("-"):
            raise CloneError(f"Invalid branch name {branch!r}")
        argv += ["--branch", branch.replace("refs/heads/", "", 1)]
    env: Dict[str, str] = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    secrets: List[str] = []
    ssh_target = None
    if protocol == "http":
        url = strip_userinfo(url)
        pairs: List[Tuple[str, str]] = [
            ("http.lowSpeedLimit", "1000"),
            ("http.lowSpeedTime", "60"),
        ]
        if auth is not None:
            header = auth.authorization_header()
            secrets.append(header)
            pairs = [
                (f"http.{http_scope(url)}.extraHeader", f"Authorization: {header}"),
                # Do not consult / store into credential helpers (e.g. osxkeychain)
                # for this one-off authenticated clone.
                ("credential.helper", ""),
            ] + pairs
        env.update(git_env_config(pairs))
    elif protocol == "ssh":
        ssh_target = ssh_endpoint(url)
        if not base_env.get("GIT_SSH_COMMAND"):
            env["GIT_SSH_COMMAND"] = (
                "ssh -o BatchMode=yes -o ConnectTimeout=15 "
                "-o ServerAliveInterval=15 -o ServerAliveCountMax=4"
            )
    else:
        raise CloneError(f"Unknown protocol {protocol!r}")
    argv += ["--", url, dest]
    return ClonePlan(argv=argv, env=env, url=url, dest=dest, protocol=protocol, ssh_target=ssh_target, secrets=secrets)


async def resolve_ssh_target(host: str, port: int) -> Optional[Tuple[str, int]]:
    """Apply ~/.ssh/config via ``ssh -G`` (no network). Returns the effective
    (hostname, port), or ``None`` when a ProxyJump/ProxyCommand is configured,
    in which case a direct TCP preflight would be meaningless."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-G", "-p", str(port), host,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), 5)
    except (OSError, asyncio.TimeoutError):
        return host, port
    cfg: Dict[str, str] = {}
    for line in out.decode(errors="replace").splitlines():
        k, _, v = line.partition(" ")
        cfg.setdefault(k.lower(), v.strip())
    for key in ("proxycommand", "proxyjump"):
        if cfg.get(key) and cfg[key].lower() != "none":
            return None
    try:
        return cfg.get("hostname") or host, int(cfg.get("port") or port)
    except ValueError:
        return host, port


async def tcp_preflight(host: str, port: int, timeout: float = 5.0) -> Optional[str]:
    """Return an error string if host:port is not reachable, else None."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except asyncio.TimeoutError:
        return f"Timed out connecting to {host}:{port} after {timeout:.0f}s"
    except OSError as exc:
        return f"Cannot connect to {host}:{port}: {exc.strerror or exc}"
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return None


def explain_failure(output: str, protocol: str, ssh_target: Optional[Tuple[str, int]] = None) -> str:
    o = output.lower()
    port = ssh_target[1] if ssh_target else 22
    if protocol == "ssh":
        if any(s in o for s in ("connection refused", "timed out", "no route to host", "network is unreachable")):
            return (
                f"SSH port {port} appears unreachable from this machine (firewall/VPN). "
                "Use HTTPS instead (it uses your token)."
            )
        if "host key verification failed" in o:
            return (
                "Unknown SSH host key. Connect once in a terminal (ssh -p "
                f"{port} git@<host>) to verify and accept the key, or use HTTPS."
            )
        if "permission denied (publickey" in o:
            return (
                "SSH key rejected or not loaded. Check ~/.ssh/config IdentityFile and that the "
                "key is added to Bitbucket; if it has a passphrase run: ssh-add --apple-use-keychain <key>"
            )
    if "could not resolve host" in o:
        return "DNS lookup failed. Check VPN/network."
    if "ssl certificate problem" in o or "certificate verify failed" in o:
        return "git does not trust the server certificate. Configure http.sslCAInfo for git, or use SSH."
    if (
        "authentication failed" in o
        or "could not read username" in o
        or "returned error: 401" in o
        or "returned error: 403" in o
    ):
        return "The server rejected the credentials for git over HTTPS. Check the token has repository read permission."
    if "already exists and is not an empty directory" in o:
        return "Destination is not empty. Choose another path."
    if "remote branch" in o and "not found" in o:
        return "Branch not found on the remote."
    if "rpc failed" in o or "early eof" in o or "operation too slow" in o:
        return "Transfer stalled or was interrupted. Retry; large repos may need a faster connection."
    return ""


def config_contains_secret(dest: str, secrets: List[str]) -> bool:
    cfg = os.path.join(dest, ".git", "config")
    try:
        with open(cfg, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return False
    low = text.lower()
    if "extraheader" in low or "authorization:" in low:
        return True
    for s in secrets:
        for part in (s, s.split(" ", 1)[-1]):
            if part and part in text:
                return True
    return False


async def git_version() -> Optional[Tuple[int, int]]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
    except (OSError, asyncio.TimeoutError):
        return None
    m = re.search(r"(\d+)\.(\d+)", out.decode(errors="replace"))
    return (int(m.group(1)), int(m.group(2))) if m else None


LineCallback = Callable[[str, bool], None]  # (text, is_progress_update)


class CloneRunner:
    """Run a :class:`ClonePlan`, streaming redacted output line by line."""

    def __init__(self, plan: ClonePlan) -> None:
        self.plan = plan
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.output: List[str] = []
        self.cancelled = False

    async def run(self, on_line: Optional[LineCallback] = None) -> int:
        env = dict(os.environ)
        # Drop inherited env-config so our GIT_CONFIG_* indexes are authoritative.
        for k in list(env):
            if k.startswith("GIT_CONFIG_KEY_") or k.startswith("GIT_CONFIG_VALUE_") or k == "GIT_CONFIG_COUNT":
                del env[k]
        env.update(self.plan.env)
        self.proc = await asyncio.create_subprocess_exec(
            *self.plan.argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        assert self.proc.stdout is not None
        buf = ""
        try:
            while True:
                chunk = await self.proc.stdout.read(1024)
                if not chunk:
                    break
                buf += chunk.decode("utf-8", errors="replace")
                while True:
                    m = re.search(r"[\r\n]", buf)
                    if not m:
                        break
                    line, sep, buf = buf[: m.start()], m.group(0), buf[m.end():]
                    if line:
                        self._emit(line, sep == "\r", on_line)
            if buf:
                self._emit(buf, False, on_line)
            return await self.proc.wait()
        except asyncio.CancelledError:
            await self.terminate()
            raise

    def _emit(self, line: str, progress: bool, cb: Optional[LineCallback]) -> None:
        line = redact(line, self.plan.secrets)
        if not progress:
            self.output.append(line)
        if cb:
            cb(line, progress)

    async def terminate(self) -> None:
        self.cancelled = True
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), 5)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
