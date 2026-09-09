import json

import pytest

from app.core.exceptions import InvalidTargetPathError, TargetAlreadyExistsError
from app.modules.publishing.file_export import check_overwrite_allowed, resolve_target_path, write_row_snapshots


def test_resolve_target_path_within_directory(tmp_path) -> None:
    export_dir = tmp_path / "exports"
    resolved = resolve_target_path("subdir/out.jsonl", str(export_dir))
    assert resolved == (export_dir / "subdir" / "out.jsonl").resolve()


def test_resolve_target_path_rejects_absolute_path(tmp_path) -> None:
    export_dir = tmp_path / "exports"
    with pytest.raises(InvalidTargetPathError):
        resolve_target_path("/etc/passwd", str(export_dir))


def test_resolve_target_path_rejects_parent_traversal(tmp_path) -> None:
    export_dir = tmp_path / "exports"
    with pytest.raises(InvalidTargetPathError):
        resolve_target_path("../../etc/passwd", str(export_dir))


def test_resolve_target_path_rejects_blank(tmp_path) -> None:
    export_dir = tmp_path / "exports"
    with pytest.raises(InvalidTargetPathError):
        resolve_target_path("   ", str(export_dir))


def test_resolve_target_path_creates_export_directory(tmp_path) -> None:
    export_dir = tmp_path / "does_not_exist_yet"
    resolve_target_path("out.jsonl", str(export_dir))
    assert export_dir.exists()


def test_check_overwrite_allowed_passes_when_file_absent(tmp_path) -> None:
    path = tmp_path / "missing.jsonl"
    check_overwrite_allowed(path, overwrite=False)  # should not raise


def test_check_overwrite_allowed_rejects_existing_file_without_flag(tmp_path) -> None:
    path = tmp_path / "existing.jsonl"
    path.write_text("data")
    with pytest.raises(TargetAlreadyExistsError):
        check_overwrite_allowed(path, overwrite=False)


def test_check_overwrite_allowed_permits_existing_file_with_flag(tmp_path) -> None:
    path = tmp_path / "existing.jsonl"
    path.write_text("data")
    check_overwrite_allowed(path, overwrite=True)  # should not raise


def test_write_row_snapshots_writes_one_json_object_per_line(tmp_path) -> None:
    path = tmp_path / "out.jsonl"
    rows = [{"id": 1, "val": "a"}, {"id": 2, "val": None}]
    count = write_row_snapshots(path, rows)
    assert count == 2

    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[0] == {"id": 1, "val": "a"}
    assert parsed[1] == {"id": 2, "val": None}  # genuine JSON null preserved


def test_write_row_snapshots_returns_actual_written_count_not_attempted(tmp_path) -> None:
    path = tmp_path / "empty.jsonl"
    count = write_row_snapshots(path, [])
    assert count == 0
