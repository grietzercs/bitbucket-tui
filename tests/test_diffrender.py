from bbtui.api.models import Change, DiffResult
from bbtui.testing.fake_server import FakeBitbucket
from bbtui.tui.diffrender import change_label, change_path, render_file_diff


def _diff(path):
    return DiffResult.from_json({"diffs": [FakeBitbucket().diffs[path]]}).diffs[0]


def test_render_modify_colours_and_numbers():
    t = render_file_diff(_diff("src/app.py"))
    plain = t.plain
    assert "--- a/src/app.py" in plain and "+++ b/src/app.py" in plain
    assert "@@ -1,4 +1,5 @@" in plain
    assert "-def main():" in plain and "+def main(argv=None):" in plain
    styles = {str(s.style) for s in t.spans}
    assert "green" in styles and "red" in styles


def test_render_add_delete_binary():
    assert "--- /dev/null" in render_file_diff(_diff("docs/new.md")).plain
    assert "+++ /dev/null" in render_file_diff(_diff("old.txt")).plain
    assert "Binary file changed" in render_file_diff(_diff("assets/logo.png")).plain


def test_render_caps_huge_diffs():
    big = {
        "source": {"toString": "f"},
        "destination": {"toString": "f"},
        "hunks": [{"sourceLine": 1, "sourceSpan": 0, "destinationLine": 1, "destinationSpan": 9000,
                   "segments": [{"type": "ADDED", "lines": [{"destination": i, "line": f"l{i}"} for i in range(9000)]}]}],
        "truncated": True,
    }
    fd = DiffResult.from_json({"diffs": [big]}).diffs[0]
    plain = render_file_diff(fd, max_lines=100).plain
    assert "capped at 100 lines" in plain
    assert plain.count(" +l") == 100


def test_change_labels():
    assert change_label(Change(path="a", type="ADD")).plain == "A"
    assert change_label(Change(path="a", type="DELETE")).plain == "D"
    assert change_path(Change(path="new", type="MOVE", src_path="old")).plain == "old → new"
