from pathlib import Path

from scripts.check_file_size import HARD, main


def test_guard_fails_on_oversized_file(tmp_path: Path) -> None:
    (tmp_path / "big.py").write_text("x = 1\n" * (HARD // 4))
    assert main([str(tmp_path)]) == 1


def test_guard_passes_small_file(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("x = 1\n")
    assert main([str(tmp_path)]) == 0
