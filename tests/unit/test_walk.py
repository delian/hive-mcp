import os

import pytest

from hive_mcp.ingest.walk import open_regular, walk_after


def make_tree(root):
    for rel in ["b.txt", "a/z.txt", "a/m/x.txt", "a/m/y.txt", "c/d.txt", "e.txt", ".hid/q.txt"]:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rel)


def names(root, cursor=None):
    return [rel for rel, _ in walk_after(root, cursor)]


def test_order_is_depth_first_by_name_and_hidden_entries_are_skipped(tmp_path):
    make_tree(tmp_path)
    assert names(tmp_path) == ["a/m/x.txt", "a/m/y.txt", "a/z.txt", "b.txt", "c/d.txt", "e.txt"]
    assert names(tmp_path) == names(tmp_path)  # deterministic


def test_resuming_after_any_cursor_gives_exactly_the_rest(tmp_path):
    make_tree(tmp_path)
    everything = names(tmp_path)
    for i, cursor in enumerate(everything):
        assert names(tmp_path, cursor) == everything[i + 1 :]
    assert names(tmp_path, "a/m") == everything  # was a file, is a directory now: all new
    assert names(tmp_path, "a/gone/f.txt") == everything  # cursor's folder was deleted
    assert names(tmp_path, "b.txt/inside") == ["b.txt", "c/d.txt", "e.txt"]  # dir became a file
    assert names(tmp_path, "zzz") == []


def test_symlinks_are_not_followed_out_of_the_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    root = tmp_path / "root"
    root.mkdir()
    (root / "ok.txt").write_text("ok")
    (root / "link_dir").symlink_to(outside, target_is_directory=True)
    (root / "link_file.txt").symlink_to(outside / "secret.txt")
    assert names(root) == ["ok.txt"]
    with pytest.raises(OSError):  # also refused at open time (a swap after the walk)
        open_regular(root, "link_file.txt").close()
    with pytest.raises(OSError):
        open_regular(root, "link_dir/secret.txt").close()
    with open_regular(root, "ok.txt") as handle:
        assert handle.read() == b"ok"


def test_unreadable_directory_is_skipped(tmp_path):
    make_tree(tmp_path)
    (tmp_path / "c").chmod(0)
    try:
        if os.access(tmp_path / "c", os.R_OK):
            pytest.skip("running as root")
        assert "c/d.txt" not in names(tmp_path)
    finally:
        (tmp_path / "c").chmod(0o755)
