"""Typed models for the subset of the Bitbucket Server REST API 1.0 we use.

Parsing is deliberately lenient: unknown/missing fields get defaults so that
small differences between Bitbucket versions do not crash the UI.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Generic, List, Optional, TypeVar

from .errors import Veto

T = TypeVar("T")
J = Dict[str, Any]


def _path_str(p: Any) -> str:
    if isinstance(p, dict):
        if p.get("toString"):
            return str(p["toString"])
        comps = p.get("components") or []
        return "/".join(str(c) for c in comps)
    if isinstance(p, str):
        return p
    return ""


def format_ts(ms: Optional[int]) -> str:
    if not ms:
        return ""
    try:
        return _dt.datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError, TypeError):
        return ""


@dataclass
class Page(Generic[T]):
    values: List[T]
    start: int = 0
    limit: int = 25
    size: int = 0
    is_last_page: bool = True
    next_page_start: Optional[int] = None

    @classmethod
    def from_json(cls, data: J, parse: Callable[[J], T]) -> "Page[T]":
        values = [parse(v) for v in (data.get("values") or [])]
        is_last = bool(data.get("isLastPage", True))
        nps = data.get("nextPageStart")
        if not is_last and nps is None:
            # Defensive: some endpoints omit nextPageStart; derive it.
            nps = int(data.get("start", 0) or 0) + len(values)
        if not is_last and not values:
            # A non-last page without values would loop forever; treat as last.
            is_last = True
            nps = None
        return cls(
            values=values,
            start=int(data.get("start", 0) or 0),
            limit=int(data.get("limit", len(values)) or 0),
            size=int(data.get("size", len(values)) or 0),
            is_last_page=is_last,
            next_page_start=None if is_last else int(nps),
        )


@dataclass
class User:
    name: str
    slug: str = ""
    display_name: str = ""
    email: str = ""

    @classmethod
    def from_json(cls, d: Optional[J]) -> "User":
        d = d or {}
        return cls(
            name=str(d.get("name") or ""),
            slug=str(d.get("slug") or d.get("name") or ""),
            display_name=str(d.get("displayName") or d.get("name") or ""),
            email=str(d.get("emailAddress") or ""),
        )

    def label(self) -> str:
        if self.display_name and self.display_name != self.name:
            return f"{self.display_name} ({self.name})"
        return self.name or "?"


@dataclass
class Project:
    key: str
    name: str
    description: str = ""
    public: bool = False
    type: str = "NORMAL"
    id: int = 0

    @classmethod
    def from_json(cls, d: J) -> "Project":
        return cls(
            key=str(d.get("key") or ""),
            name=str(d.get("name") or d.get("key") or ""),
            description=str(d.get("description") or ""),
            public=bool(d.get("public", False)),
            type=str(d.get("type") or "NORMAL"),
            id=int(d.get("id") or 0),
        )

    @property
    def is_personal(self) -> bool:
        return self.key.startswith("~") or self.type == "PERSONAL"


@dataclass
class Repository:
    slug: str
    name: str
    project: Project
    description: str = ""
    state: str = "AVAILABLE"
    forkable: bool = True
    public: bool = False
    archived: bool = False
    clone_links: Dict[str, str] = field(default_factory=dict)
    web_url: str = ""
    id: int = 0

    @classmethod
    def from_json(cls, d: J) -> "Repository":
        links = d.get("links") or {}
        clone = {}
        for link in links.get("clone") or []:
            name = str(link.get("name") or "").lower()
            href = str(link.get("href") or "")
            if name and href:
                clone[name] = href
        selfs = links.get("self") or []
        web = str(selfs[0].get("href")) if selfs and isinstance(selfs[0], dict) else ""
        return cls(
            slug=str(d.get("slug") or ""),
            name=str(d.get("name") or d.get("slug") or ""),
            project=Project.from_json(d.get("project") or {}),
            description=str(d.get("description") or ""),
            state=str(d.get("state") or "AVAILABLE"),
            forkable=bool(d.get("forkable", True)),
            public=bool(d.get("public", False)),
            archived=bool(d.get("archived", False)),
            clone_links=clone,
            web_url=web,
            id=int(d.get("id") or 0),
        )

    @property
    def full_name(self) -> str:
        return f"{self.project.key}/{self.slug}"


@dataclass
class Branch:
    id: str
    display_id: str
    latest_commit: str = ""
    is_default: bool = False

    @classmethod
    def from_json(cls, d: J) -> "Branch":
        return cls(
            id=str(d.get("id") or ""),
            display_id=str(d.get("displayId") or d.get("id") or ""),
            latest_commit=str(d.get("latestCommit") or d.get("latestChangeset") or ""),
            is_default=bool(d.get("isDefault", False)),
        )


@dataclass
class BrowseEntry:
    name: str
    path: str  # full path relative to repo root
    type: str  # FILE | DIRECTORY | SUBMODULE
    size: Optional[int] = None

    @property
    def is_dir(self) -> bool:
        return self.type == "DIRECTORY"


@dataclass
class DirectoryPage:
    path: str
    revision: str
    children: Page[BrowseEntry]


@dataclass
class FilePage:
    lines: List[str]
    start: int
    is_last_page: bool
    next_page_start: Optional[int]
    binary: bool = False


@dataclass
class Ref:
    id: str
    display_id: str
    latest_commit: str = ""
    repository: Optional[Repository] = None

    @classmethod
    def from_json(cls, d: Optional[J]) -> "Ref":
        d = d or {}
        repo = d.get("repository")
        return cls(
            id=str(d.get("id") or ""),
            display_id=str(d.get("displayId") or d.get("id") or ""),
            latest_commit=str(d.get("latestCommit") or d.get("latestChangeset") or ""),
            repository=Repository.from_json(repo) if isinstance(repo, dict) else None,
        )


@dataclass
class Participant:
    user: User
    role: str = "REVIEWER"
    approved: bool = False
    status: str = "UNAPPROVED"  # APPROVED | UNAPPROVED | NEEDS_WORK

    @classmethod
    def from_json(cls, d: Optional[J]) -> "Participant":
        d = d or {}
        return cls(
            user=User.from_json(d.get("user")),
            role=str(d.get("role") or "REVIEWER"),
            approved=bool(d.get("approved", False)),
            status=str(d.get("status") or ("APPROVED" if d.get("approved") else "UNAPPROVED")),
        )


@dataclass
class PullRequest:
    id: int
    version: int
    title: str
    description: str
    state: str
    author: Participant
    from_ref: Ref
    to_ref: Ref
    reviewers: List[Participant] = field(default_factory=list)
    participants: List[Participant] = field(default_factory=list)
    created_date: Optional[int] = None
    updated_date: Optional[int] = None
    web_url: str = ""
    comment_count: int = 0
    open_task_count: int = 0
    merge_outcome: str = ""
    locked: bool = False

    @classmethod
    def from_json(cls, d: J) -> "PullRequest":
        links = d.get("links") or {}
        selfs = links.get("self") or []
        props = d.get("properties") or {}
        merge_result = props.get("mergeResult") or {}
        return cls(
            id=int(d.get("id") or 0),
            version=int(d.get("version") or 0),
            title=str(d.get("title") or ""),
            description=str(d.get("description") or ""),
            state=str(d.get("state") or ""),
            author=Participant.from_json(d.get("author")),
            from_ref=Ref.from_json(d.get("fromRef")),
            to_ref=Ref.from_json(d.get("toRef")),
            reviewers=[Participant.from_json(r) for r in d.get("reviewers") or []],
            participants=[Participant.from_json(r) for r in d.get("participants") or []],
            created_date=d.get("createdDate"),
            updated_date=d.get("updatedDate"),
            web_url=str(selfs[0].get("href")) if selfs and isinstance(selfs[0], dict) else "",
            comment_count=int(props.get("commentCount") or 0),
            open_task_count=int(props.get("openTaskCount") or 0),
            merge_outcome=str(merge_result.get("outcome") or ""),
            locked=bool(d.get("locked", False)),
        )


@dataclass
class MergeCheck:
    can_merge: bool
    conflicted: bool
    outcome: str
    vetoes: List[Veto] = field(default_factory=list)

    @classmethod
    def from_json(cls, d: J) -> "MergeCheck":
        return cls(
            can_merge=bool(d.get("canMerge", False)),
            conflicted=bool(d.get("conflicted", False)),
            outcome=str(d.get("outcome") or "UNKNOWN"),
            vetoes=[Veto.from_json(v) for v in d.get("vetoes") or []],
        )


@dataclass
class MergeStrategy:
    id: str
    name: str
    description: str = ""
    enabled: bool = True

    @classmethod
    def from_json(cls, d: J) -> "MergeStrategy":
        return cls(
            id=str(d.get("id") or ""),
            name=str(d.get("name") or d.get("id") or ""),
            description=str(d.get("description") or ""),
            enabled=bool(d.get("enabled", True)),
        )


@dataclass
class MergeConfig:
    default_strategy_id: Optional[str]
    strategies: List[MergeStrategy]

    @classmethod
    def from_json(cls, d: J) -> "MergeConfig":
        mc = d.get("mergeConfig") or {}
        default = mc.get("defaultStrategy") or {}
        return cls(
            default_strategy_id=str(default.get("id")) if default.get("id") else None,
            strategies=[MergeStrategy.from_json(s) for s in mc.get("strategies") or []],
        )

    @property
    def enabled_strategies(self) -> List[MergeStrategy]:
        return [s for s in self.strategies if s.enabled]


@dataclass
class Comment:
    id: int
    text: str
    author: User
    created_date: Optional[int] = None
    replies: List["Comment"] = field(default_factory=list)

    @classmethod
    def from_json(cls, d: J) -> "Comment":
        return cls(
            id=int(d.get("id") or 0),
            text=str(d.get("text") or ""),
            author=User.from_json(d.get("author")),
            created_date=d.get("createdDate"),
            replies=[Comment.from_json(c) for c in d.get("comments") or []],
        )


@dataclass
class Activity:
    id: int
    action: str
    user: User
    created_date: Optional[int] = None
    comment: Optional[Comment] = None
    comment_action: str = ""
    anchor_path: str = ""
    anchor_line: Optional[int] = None

    @classmethod
    def from_json(cls, d: J) -> "Activity":
        anchor = d.get("commentAnchor") or {}
        c = d.get("comment")
        return cls(
            id=int(d.get("id") or 0),
            action=str(d.get("action") or ""),
            user=User.from_json(d.get("user")),
            created_date=d.get("createdDate"),
            comment=Comment.from_json(c) if isinstance(c, dict) else None,
            comment_action=str(d.get("commentAction") or ""),
            anchor_path=_path_str(anchor.get("path")),
            anchor_line=anchor.get("line"),
        )


@dataclass
class Change:
    path: str
    type: str  # ADD | MODIFY | DELETE | MOVE | COPY | UNKNOWN
    src_path: str = ""
    node_type: str = "FILE"

    @classmethod
    def from_json(cls, d: J) -> "Change":
        return cls(
            path=_path_str(d.get("path")),
            type=str(d.get("type") or "MODIFY"),
            src_path=_path_str(d.get("srcPath")),
            node_type=str(d.get("nodeType") or "FILE"),
        )


@dataclass
class DiffLine:
    kind: str  # ADDED | REMOVED | CONTEXT
    text: str
    source: Optional[int] = None
    destination: Optional[int] = None
    truncated: bool = False


@dataclass
class DiffHunk:
    source_line: int
    source_span: int
    destination_line: int
    destination_span: int
    lines: List[DiffLine]
    context: str = ""
    truncated: bool = False


@dataclass
class FileDiff:
    source: str
    destination: str
    hunks: List[DiffHunk]
    binary: bool = False
    truncated: bool = False

    @classmethod
    def from_json(cls, d: J) -> "FileDiff":
        hunks: List[DiffHunk] = []
        for h in d.get("hunks") or []:
            lines: List[DiffLine] = []
            for seg in h.get("segments") or []:
                kind = str(seg.get("type") or "CONTEXT")
                for ln in seg.get("lines") or []:
                    lines.append(
                        DiffLine(
                            kind=kind,
                            text=str(ln.get("line") or ""),
                            source=ln.get("source"),
                            destination=ln.get("destination"),
                            truncated=bool(ln.get("truncated", False)),
                        )
                    )
            hunks.append(
                DiffHunk(
                    source_line=int(h.get("sourceLine") or 0),
                    source_span=int(h.get("sourceSpan") or 0),
                    destination_line=int(h.get("destinationLine") or 0),
                    destination_span=int(h.get("destinationSpan") or 0),
                    lines=lines,
                    context=str(h.get("context") or ""),
                    truncated=bool(h.get("truncated", False)),
                )
            )
        return cls(
            source=_path_str(d.get("source")),
            destination=_path_str(d.get("destination")),
            hunks=hunks,
            binary=bool(d.get("binary", False)),
            truncated=bool(d.get("truncated", False)),
        )


@dataclass
class DiffResult:
    diffs: List[FileDiff]
    truncated: bool = False

    @classmethod
    def from_json(cls, d: J) -> "DiffResult":
        return cls(
            diffs=[FileDiff.from_json(x) for x in d.get("diffs") or []],
            truncated=bool(d.get("truncated", False)),
        )


@dataclass
class ServerInfo:
    base_url: str
    version: str = ""
    display_name: str = ""
    username: str = ""  # from X-AUSERNAME, empty if anonymous/unknown
    user: Optional[User] = None
    warnings: List[str] = field(default_factory=list)
