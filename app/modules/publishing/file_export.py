"""FILE_EXPORT write logic — no DB access, unit-testable in isolation.
PublishingService/the Celery task call these while orchestrating the
DB-facing work. Operates entirely on already-staged, already-approved
platform data (staging_records.row_snapshot) — never contacts the source
database, per the strict Phase 9 boundary.
"""
import json
from pathlib import Path
from typing import Any

from app.core.exceptions import InvalidTargetPathError, TargetAlreadyExistsError


def resolve_target_path(target_reference: str, export_directory: str) -> Path:
    """Resolves target_reference relative to export_directory, rejecting
    any path that would escape it. Rejects absolute target_reference
    values outright — Path's own `/` operator silently discards the left
    operand when the right is absolute, which would otherwise let an
    absolute target_reference escape the approved directory undetected."""
    if not target_reference or not target_reference.strip():
        raise InvalidTargetPathError("target_reference must not be blank")

    reference_path = Path(target_reference)
    if reference_path.is_absolute():
        raise InvalidTargetPathError(
            "target_reference must be a relative path within the approved export directory"
        )

    base = Path(export_directory).resolve()
    base.mkdir(parents=True, exist_ok=True)
    candidate = (base / reference_path).resolve()

    try:
        candidate.relative_to(base)
    except ValueError:
        raise InvalidTargetPathError("target_reference resolves outside the approved export directory") from None

    return candidate


def check_overwrite_allowed(path: Path, *, overwrite: bool) -> None:
    """No silent overwrite, ever — reject unless the caller explicitly
    requested it."""
    if path.exists() and not overwrite:
        raise TargetAlreadyExistsError(
            f"Target file already exists at {path.name} and overwrite was not requested"
        )


def write_row_snapshots(path: Path, row_snapshots: list[dict[str, Any]]) -> int:
    """Writes one JSON object per line (JSONL). Returns the number of rows
    ACTUALLY, VERIFIABLY written — re-reads the file back and counts lines,
    rather than trusting len(row_snapshots) (what was attempted). If the
    write doesn't fully complete, the caller must treat the run as FAILED,
    never PUBLISHED, regardless of how many lines did land."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in row_snapshots:
            f.write(json.dumps(row, sort_keys=True))
            f.write("\n")

    with open(path, "r", encoding="utf-8") as f:
        written_count = sum(1 for _ in f)
    return written_count
