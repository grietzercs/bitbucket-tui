"""Render Bitbucket structured diffs as coloured Rich text."""

from __future__ import annotations

from typing import Optional

from rich.text import Text

from ..api.models import Change, FileDiff

CHANGE_STYLE = {
    "ADD": ("A", "green"),
    "DELETE": ("D", "red"),
    "MODIFY": ("M", "yellow"),
    "MOVE": ("R", "cyan"),
    "COPY": ("C", "cyan"),
}


def change_label(change: Change) -> Text:
    letter, style = CHANGE_STYLE.get(change.type, ("?", "magenta"))
    t = Text()
    t.append(letter, style=f"bold {style}")
    return t


def change_path(change: Change) -> Text:
    t = Text(change.path)
    if change.src_path and change.src_path != change.path:
        t = Text(f"{change.src_path} → {change.path}")
    return t


def _num(n: Optional[int]) -> str:
    return f"{n:>5}" if n else "     "


def render_file_diff(fd: FileDiff, max_lines: int = 5000) -> Text:
    out = Text()
    src = fd.source or "/dev/null"
    dst = fd.destination or "/dev/null"
    out.append(f"--- a/{src}\n" if fd.source else "--- /dev/null\n", style="bold red")
    out.append(f"+++ b/{dst}\n" if fd.destination else "+++ /dev/null\n", style="bold green")
    if fd.binary:
        out.append("\nBinary file changed (not shown).\n", style="italic yellow")
        return out
    if not fd.hunks:
        out.append("\nNo textual changes (mode change, rename only, or empty file).\n", style="italic")
        return out
    shown = 0
    for h in fd.hunks:
        header = f"@@ -{h.source_line},{h.source_span} +{h.destination_line},{h.destination_span} @@"
        out.append(header, style="bold cyan")
        if h.context:
            out.append(" " + h.context, style="cyan")
        out.append("\n")
        for ln in h.lines:
            if shown >= max_lines:
                out.append(f"\n… diff display capped at {max_lines} lines …\n", style="bold yellow")
                return out
            shown += 1
            if ln.kind == "ADDED":
                out.append(f"{_num(None)} {_num(ln.destination)} ", style="dim")
                out.append(f"+{ln.text}\n", style="green")
            elif ln.kind == "REMOVED":
                out.append(f"{_num(ln.source)} {_num(None)} ", style="dim")
                out.append(f"-{ln.text}\n", style="red")
            else:
                out.append(f"{_num(ln.source)} {_num(ln.destination)} ", style="dim")
                out.append(f" {ln.text}\n")
            if ln.truncated:
                out.append("      (line truncated by server)\n", style="italic yellow")
        if h.truncated:
            out.append("… hunk truncated by server …\n", style="bold yellow")
    if fd.truncated:
        out.append("… diff truncated by server (file too large) …\n", style="bold yellow")
    return out
